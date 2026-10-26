"""What the images do at run time, read from their Dockerfiles and wrapper.sh: the HEALTHCHECK's port, whether a
container starts a node or the app, the exposed port, the Plex database image's role guard."""

from __future__ import annotations

import itertools
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "Dockerfile"
PLEX_DB = ROOT / "nodes" / "plex-db" / "Dockerfile"
# Runs the HEALTHCHECK's Python with urlopen printing the URL it was given instead of calling it.
_RECORD_URL = "import sys, urllib.request; urllib.request.urlopen = lambda url, **_: print(url); exec(sys.argv[1])"


def _instructions(dockerfile: Path) -> list[str]:
    joined = dockerfile.read_text().replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def _healthcheck_code(dockerfile: Path) -> str:
    [line] = [line for line in _instructions(dockerfile) if line.startswith("HEALTHCHECK ")]
    command = shlex.split(line.split(" CMD ", 1)[1])
    assert command[:3] == ["exec", "python3", "-c"], command
    return command[3]


# Every setting the node's port and host come from (nodes/server/wsgi.py), with the file form of each.
_NODE_ADDRESS_SETTINGS = (
    "NODE_PORT", "FILE__NODE_PORT", "AGENT_PORT", "FILE__AGENT_PORT", "NODE_HOST", "FILE__NODE_HOST",
)  # fmt: skip


def _url(dockerfile: Path, **env: str) -> str:
    clean = {k: v for k, v in os.environ.items() if k not in ("MODE", "WEB_PORT", *_NODE_ADDRESS_SETTINGS)}
    done = subprocess.run(
        [sys.executable, "-c", _RECORD_URL, _healthcheck_code(dockerfile)],
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return done.stdout.strip()


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({"MODE": "node"}, "http://127.0.0.1:9494/v1/health"),
        # wrapper.sh reads an empty NODE_PORT as the default (${NODE_PORT:-9494}); the check must look there too.
        ({"MODE": "node", "NODE_PORT": ""}, "http://127.0.0.1:9494/v1/health"),
        ({"MODE": "node", "NODE_PORT": "9500"}, "http://127.0.0.1:9500/v1/health"),
        # The node strips its port: an env file saved with CRLF, or spaces, still serve there (nodes/server/wsgi.py).
        ({"MODE": "node", "NODE_PORT": "9500\r"}, "http://127.0.0.1:9500/v1/health"),
        ({"MODE": "node", "NODE_PORT": " 9500 "}, "http://127.0.0.1:9500/v1/health"),
        ({}, "http://localhost:8080/api/health"),
        ({"WEB_PORT": ""}, "http://localhost:8080/api/health"),
        ({"WEB_PORT": "8181"}, "http://localhost:8181/api/health"),
    ],
)
def test_the_main_images_healthcheck_asks_the_port_the_server_listens_on(env, url):
    assert _url(MAIN, **env) == url


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({}, "http://127.0.0.1:9494/v1/health"),
        ({"NODE_PORT": ""}, "http://127.0.0.1:9494/v1/health"),
        ({"AGENT_PORT": "9600"}, "http://127.0.0.1:9600/v1/health"),
        ({"NODE_PORT": "9500\r"}, "http://127.0.0.1:9500/v1/health"),
        ({"AGENT_PORT": "9600\r"}, "http://127.0.0.1:9600/v1/health"),
    ],
)
def test_the_plex_database_images_healthcheck_asks_the_port_the_node_listens_on(env, url):
    assert _url(PLEX_DB, **env) == url


_MODE_SPELLINGS = ["node", "Node", "NODE", " node\t", "", "app", "nodes", "no de"]


def _wrapper_starts(tmp_path: Path, mode: str) -> str:
    """Run wrapper.sh with stand-in programs; return "node" or "app" for what it started."""
    script = (ROOT / "wrapper.sh").read_text()
    assert "\ncd /app\n" in script
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        # Not tini/docker-init, so the init: true guard lets the script go on.
        "ps": "echo s6-svscan",
        "gosu": 'shift; exec "$@"',
        "python3": 'echo "python3 $*"',
        "gunicorn": 'echo "gunicorn $*"',
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(f"#!/bin/sh\n{body}\n")
        stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("MODE", "NODE_PORT", "WEB_PORT", "DEV_RELOAD")}
    done = subprocess.run(
        ["bash", "-c", script.replace("\ncd /app\n", "\n", 1)],
        env={**env, "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}", "MODE": mode},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    started = done.stdout.strip().splitlines()[-1]
    if started.startswith("python3 -m media_preview_generator.nodes.server.wsgi "):
        return "node"
    assert started.startswith("gunicorn "), done.stdout
    return "app"


@pytest.mark.parametrize("mode", _MODE_SPELLINGS)
def test_wrapper_starts_a_node_exactly_when_the_code_thinks_it_is_one(tmp_path, monkeypatch, mode):
    from media_preview_generator.utils import is_node_mode

    monkeypatch.setenv("MODE", mode)
    assert _wrapper_starts(tmp_path, mode) == ("node" if is_node_mode() else "app")


@pytest.mark.parametrize("mode", _MODE_SPELLINGS)
def test_the_main_images_healthcheck_follows_the_same_mode_rule(monkeypatch, mode):
    from media_preview_generator.utils import is_node_mode

    monkeypatch.setenv("MODE", mode)
    expected = "http://127.0.0.1:9494/v1/health" if is_node_mode() else "http://localhost:8080/api/health"
    assert _url(MAIN, MODE=mode) == expected


def test_the_main_image_exposes_only_the_web_ui_port():
    # nginx-proxy given only VIRTUAL_HOST routes to a container's one exposed port; with two it picks 80 (502).
    exposed = [line.split()[1:] for line in _instructions(MAIN) if line.startswith("EXPOSE ")]
    assert exposed == [["8080"]]


# Every label linuxserver/ffmpeg:8.1.2-cli-ls73 sets (`docker image inspect`). The image inherits them, so each one
# must be overridden or it describes linuxserver's FFmpeg image instead of this one (#428).
_BASE_IMAGE_LABELS = (
    "build_version", "maintainer", "org.opencontainers.image.authors", "org.opencontainers.image.created",
    "org.opencontainers.image.description", "org.opencontainers.image.documentation",
    "org.opencontainers.image.licenses", "org.opencontainers.image.ref.name", "org.opencontainers.image.revision",
    "org.opencontainers.image.source", "org.opencontainers.image.title", "org.opencontainers.image.url",
    "org.opencontainers.image.vendor", "org.opencontainers.image.version",
)  # fmt: skip


def test_the_main_image_overrides_every_label_its_base_image_sets():
    instructions = _instructions(MAIN)
    last_from = max(i for i, line in enumerate(instructions) if line.startswith("FROM "))
    assert instructions[last_from].startswith("FROM linuxserver/ffmpeg:"), instructions[last_from]
    labels = dict(
        pair.split("=", 1)
        for line in instructions[last_from:]
        if line.startswith("LABEL ")
        for pair in shlex.split(line)[1:]
    )
    assert sorted(set(_BASE_IMAGE_LABELS) - labels.keys()) == []
    assert not [value for value in labels.values() if "linuxserver" in value.lower()]
    assert labels["org.opencontainers.image.source"] == "https://github.com/stevezau/media_preview_generator"
    assert labels["org.opencontainers.image.revision"] == "${GIT_SHA}"


@pytest.mark.parametrize("dockerfile", [MAIN, PLEX_DB], ids=["main", "plex-db"])
def test_the_healthcheck_asks_the_port_a_file_names(dockerfile, tmp_path):
    # The node reads FILE__NODE_PORT before NODE_PORT (nodes/server/wsgi.py); a check of NODE_PORT alone would call
    # a node listening where its secret file says unhealthy.
    port_file = tmp_path / "port"
    port_file.write_bytes("\ufeff9700\r\n".encode())  # as Notepad saves it
    url = _url(dockerfile, MODE="node", NODE_PORT="9500", FILE__NODE_PORT=str(port_file))
    assert url.endswith(":9700/v1/health")


_PORT_SETTINGS = ("FILE__NODE_PORT", "NODE_PORT", "FILE__AGENT_PORT", "AGENT_PORT")


def _node_listens_on(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> int | None:
    """The port the node's own server picks with ``env``: its FILE__ files first, then its port settings."""
    from media_preview_generator.nodes.server import wsgi
    from media_preview_generator.nodes.server.app import _env_from_files

    for name in _NODE_ADDRESS_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    node_env = dict(env)
    _env_from_files(node_env)
    for name, value in node_env.items():
        monkeypatch.setenv(name, value)
    return wsgi._port(wsgi._parse([]).port)


@pytest.mark.parametrize("dockerfile", [MAIN, PLEX_DB], ids=["main", "plex-db"])
@pytest.mark.parametrize(
    "given",
    [mix for size in range(len(_PORT_SETTINGS) + 1) for mix in itertools.combinations(_PORT_SETTINGS, size)],
    ids=lambda mix: "+".join(mix) or "none",
)
def test_the_healthcheck_asks_the_port_the_node_picks_from_every_mix_of_its_settings(
    dockerfile, given, tmp_path, monkeypatch
):
    # The node takes FILE__NODE_PORT, NODE_PORT, FILE__AGENT_PORT, AGENT_PORT in that order; a check that skipped
    # FILE__AGENT_PORT called a node listening on its file's port unhealthy forever.
    env = {"MODE": "node"}
    for port, name in enumerate(given, start=9601):
        if name.startswith("FILE__"):
            port_file = tmp_path / name
            port_file.write_text(f"{port}\n")
            env[name] = str(port_file)
        else:
            env[name] = str(port)
    listens_on = _node_listens_on(monkeypatch, env)
    assert listens_on == (9601 if given else 9494)
    assert _url(dockerfile, **env) == f"http://127.0.0.1:{listens_on}/v1/health"


@pytest.mark.parametrize("dockerfile", [MAIN, PLEX_DB], ids=["main", "plex-db"])
@pytest.mark.parametrize(
    ("host", "asked"),
    [
        ("", "127.0.0.1"),
        ("0.0.0.0", "127.0.0.1"),
        # Waitress listens IPv6-only on ::, so 127.0.0.1 is refused there.
        ("::", "[::1]"),
        ("172.17.0.1", "172.17.0.1"),
        ("fd42::101", "[fd42::101]"),
    ],
    ids=["unset", "any-ipv4", "any-ipv6", "one-address", "one-ipv6-address"],
)
def test_the_healthcheck_asks_the_address_the_node_listens_on(dockerfile, host, asked):
    # With --network host and NODE_HOST set to one address, a check of 127.0.0.1 read a serving node as unhealthy.
    assert _url(dockerfile, MODE="node", NODE_HOST=host) == f"http://{asked}:9494/v1/health"


@pytest.mark.parametrize("dockerfile", [MAIN, PLEX_DB], ids=["main", "plex-db"])
def test_the_healthcheck_asks_the_address_a_file_names(dockerfile, tmp_path):
    host_file = tmp_path / "host"
    host_file.write_text("10.0.0.5\n")
    assert _url(dockerfile, MODE="node", FILE__NODE_HOST=str(host_file)) == "http://10.0.0.5:9494/v1/health"


def test_the_plex_database_image_names_the_only_role_it_carries():
    # The node refuses NODE_ROLES=worker when NODE_IMAGE_ROLES says the image can't run it (nodes/server/app.py).
    env = " ".join(line for line in _instructions(PLEX_DB) if line.startswith("ENV "))
    assert re.search(r"\bNODE_IMAGE_ROLES=plex_db\b", env), env
    assert "NODE_IMAGE_ROLES" not in " ".join(_instructions(MAIN))


def test_gunicorn_lets_its_worker_go_before_s6_kills_the_container():
    """s6 kills what is left S6_KILL_GRACETIME after a stop's SIGTERM (3 s unless set), and gunicorn's worker waits all
    of its --graceful-timeout for a browser tab's websocket: past the grace, gunicorn's own stop never finishes."""
    env = " ".join(line for line in _instructions(MAIN) if line.startswith("ENV "))
    found = re.search(r"\bS6_KILL_GRACETIME=(\d+)\b", env)
    grace_s = int(found.group(1)) / 1000 if found else 3.0
    graceful = re.search(r"--graceful-timeout (\d+)", (ROOT / "wrapper.sh").read_text())
    assert graceful is not None

    assert int(graceful.group(1)) + 1 <= grace_s
