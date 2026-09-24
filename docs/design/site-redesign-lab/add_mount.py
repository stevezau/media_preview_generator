#!/usr/bin/env python3
"""Recreate a lab container with one extra read-only bind mount, keeping everything else identical.

The marker-lab servers (docs/design/intro-credits/evidence/lab/up.sh on feat/markers-detection)
carry ~190 per-folder bind mounts each, so retyping their `docker run` is how a mount gets lost. This
rebuilds the run arguments from `docker inspect`, runs the SAME image (by its tag only while the tag
still names that image ID, else by the ID, so a moved tag can't upgrade the server), keeps the
hostname (Plex, Jellyfin and Emby name themselves after it), adds the new mount, and keeps the old
container, stopped and renamed `<name>-pre-openfilms`, for rollback. Named volumes hold all server
state, so the new container is the same server.

    ./add_mount.py add mlab-jellyfin /home/data/mlab-openfilms/Movies /media/openfilms/Movies \
        --health http://127.0.0.1:18097/System/Info/Public
    ./add_mount.py add mlab-plex /home/data/mlab-openfilms/Movies /media/openfilms/Movies --gpu \
        --health http://127.0.0.1:32402/identity
    PLEX_CLAIM=claim-xxxx ./add_mount.py add mlab-plex ... --env PLEX_CLAIM   # value read from the environment
    ./add_mount.py rollback mlab-jellyfin

Secret-looking variables (PLEX_CLAIM and the like) go to `docker run` through a 0600 env file, never
the argv that `ps` shows. Refuses a host path under /data* (after resolving symlinks), a missing host
folder, a container that already mounts the target, and container settings it doesn't reproduce
(--mount, --gpus, a custom entrypoint, more than one network).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BACKUP_SUFFIX = "-pre-openfilms"
SECRET_HINTS = ("TOKEN", "CLAIM", "KEY", "PASS", "SECRET")
STOP_TIMEOUT = "60"  # seconds; Jellyfin and Emby can take longer than docker's default 10 to close their databases
# The NVIDIA runtime the lab's GPU containers use (docker's default runtime here is runc).
GPU_ARGS = [
    "--runtime=nvidia", "-e", "NVIDIA_VISIBLE_DEVICES=all", "-e", "NVIDIA_DRIVER_CAPABILITIES=all",
    "--device", "/dev/dri:/dev/dri",
]  # fmt: skip


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"docker {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def inspect(name: str) -> dict:
    return json.loads(docker("inspect", name))[0]


def exists(name: str) -> bool:
    return subprocess.run(["docker", "inspect", name], capture_output=True).returncode == 0


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def is_secret(key: str) -> bool:
    return any(hint in key.upper() for hint in SECRET_HINTS)


def image_reference(info: dict) -> str:
    """The tag the container was created from while it still names the same image, else the image ID."""
    tag = info["Config"]["Image"]
    current = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", tag], capture_output=True, text=True)
    return tag if current.returncode == 0 and current.stdout.strip() == info["Image"] else info["Image"]


def container_env(info: dict, image: dict, extra_env: list[str]) -> dict[str, str]:
    """Variables the container sets beyond its image's, then `extra_env` (KEY=VALUE, or KEY read from os.environ)."""
    image_env = set(image.get("Env") or [])
    env = {}
    for entry in info["Config"].get("Env") or []:
        if entry not in image_env:
            key, _, value = entry.partition("=")
            env[key] = value
    for entry in extra_env:
        key, sep, value = entry.partition("=")
        env[key] = value if sep else os.environ[key]
    return env


def run_args(info: dict, extra: list[str], env: dict[str, str], env_file: str) -> list[str]:
    """`docker run` arguments that recreate the container described by `info`, plus `extra` before the image."""
    config, host = info["Config"], info["HostConfig"]
    image = json.loads(docker("image", "inspect", info["Image"]))[0]["Config"]
    require(not host.get("Mounts"), "container uses --mount; extend add_mount.py first")
    require(not host.get("DeviceRequests"), "container uses --gpus; extend add_mount.py first")
    require(
        config.get("Entrypoint") == image.get("Entrypoint"),
        "container overrides the entrypoint; extend add_mount.py first",
    )
    networks = list(info["NetworkSettings"]["Networks"])
    require(len(networks) == 1, f"expected exactly one network, found {networks}")
    args = ["run", "-d", "--name", info["Name"].lstrip("/"), "--hostname", config["Hostname"], "--network", networks[0]]
    if config.get("User"):
        args += ["--user", config["User"]]
    for key, value in env.items():
        if not is_secret(key):
            args += ["-e", f"{key}={value}"]
    if any(is_secret(key) for key in env):
        args += ["--env-file", env_file]
    for port, bindings in sorted((host.get("PortBindings") or {}).items()):
        for binding in bindings or []:
            host_side = f"{binding['HostIp']}:{binding['HostPort']}" if binding.get("HostIp") else binding["HostPort"]
            args += ["-p", f"{host_side}:{port}"]
    for bind in host.get("Binds") or []:
        args += ["-v", bind]
    if host.get("Runtime") and host["Runtime"] != "runc":
        args += ["--runtime", host["Runtime"]]
    for device in host.get("Devices") or []:
        args += ["--device", f"{device['PathOnHost']}:{device['PathInContainer']}:{device['CgroupPermissions']}"]
    restart = (host.get("RestartPolicy") or {}).get("Name")
    if restart and restart != "no":
        args += ["--restart", restart]
    command = (config.get("Cmd") or []) if config.get("Cmd") != image.get("Cmd") else []
    return [*args, *extra, image_reference(info), *command]


def write_env_file(env: dict[str, str]) -> str:
    handle, path = tempfile.mkstemp(prefix="add_mount-", suffix=".env")  # created 0600
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        out.writelines(f"{key}={value}\n" for key, value in env.items() if is_secret(key))
    return path


def wait_healthy(url: str, timeout: float = 240) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # nosec B310 - the operator's own --health URL
                if response.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(3)
    raise SystemExit(f"{url} did not answer 200 within {timeout:.0f}s")


def add(name: str, host_path: str, container_path: str, gpu: bool, health: str, extra_env: list[str]) -> None:
    for path in (host_path, os.path.realpath(host_path)):
        require(not path.startswith("/data"), f"refusing a host path under /data*: {path}")
    # Docker would create a missing bind source as an empty root-owned folder.
    require(os.path.isdir(host_path), f"no folder at {host_path}")
    backup = name + BACKUP_SUFFIX
    require(not exists(backup), f"{backup} already exists: roll back or remove it first")
    before = inspect(name)
    binds = before["HostConfig"].get("Binds") or []
    require(all(bind.split(":")[1] != container_path for bind in binds), f"{name} already mounts {container_path}")
    new_bind = f"{host_path}:{container_path}:ro"
    image = json.loads(docker("image", "inspect", before["Image"]))[0]["Config"]
    env = container_env(before, image, extra_env)
    env_file = write_env_file(env)
    try:
        args = run_args(before, ["-v", new_bind, *(GPU_ARGS if gpu else [])], env, env_file)
        print("docker " + " ".join(args))
        docker("stop", "-t", STOP_TIMEOUT, name)
        docker("rename", name, backup)
        try:
            docker(*args)
            wait_healthy(health)
        except BaseException:
            docker("rm", "-f", name, check=False)
            docker("rename", backup, name)
            docker("start", name)
            raise
    finally:
        os.unlink(env_file)
    after = inspect(name)
    require(
        sorted(after["HostConfig"]["Binds"]) == sorted([*binds, new_bind]), "bind mounts are not the old set plus one"
    )
    require(after["Image"] == before["Image"], "the image changed")
    require(after["Config"]["Image"] == before["Config"]["Image"], "the image reference changed")
    require(after["Config"]["Hostname"] == before["Config"]["Hostname"], "the hostname changed")
    require(after["HostConfig"]["PortBindings"] == before["HostConfig"]["PortBindings"], "port bindings changed")
    print(
        f"{name}: {len(binds)} -> {len(binds) + 1} binds{' + GPU' if gpu else ''}; old container kept as {backup} (stopped)"
    )


def rollback(name: str) -> None:
    backup = name + BACKUP_SUFFIX
    require(exists(backup), f"no {backup} to roll back to")
    docker("rm", "-f", name, check=False)
    docker("rename", backup, name)
    docker("start", name)
    print(f"{name}: restored from {backup}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser = sub.add_parser("add")
    add_parser.add_argument("name")
    add_parser.add_argument("host_path")
    add_parser.add_argument("container_path")
    add_parser.add_argument("--gpu", action="store_true", help="also give it the NVIDIA runtime and /dev/dri")
    add_parser.add_argument("--health", required=True, help="URL that answers 200 once the server is up")
    add_parser.add_argument(
        "--env", action="append", default=[], help="extra KEY=VALUE, or KEY to read its value from the environment"
    )
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("name")
    args = parser.parse_args()
    if args.command == "add":
        add(args.name, args.host_path, args.container_path, args.gpu, args.health, args.env)
    else:
        rollback(args.name)


if __name__ == "__main__":
    main()
