#!/usr/bin/env python3
"""Phase 4 lab row 12: the Plex marker agent, proven with two containers (plan phase 4 Task 10).

The app runs in a container with **no** access to Plex's config volume, so it cannot open Plex's database at all;
the agent runs beside the lab Plex with that volume mounted. Every marker the app writes here goes through the agent.

    ./phase4_row12_up.sh                 create mlab-plex-agent and mlab-app-remote
    ./phase4_row12_agent.py configure    add the lab Plex to mlab-app-remote and point it at the agent
    ./phase4_row12_agent.py run          every step of the row, in order
    ./phase4_row12_agent.py run isolated published read_back locked wrong_key skew wrong_plex stopped cleaned_up

Results land in results/phase4-row-12.json (git-ignored) with credentials scrubbed; the written-up row is
phase4-row12-agent.md. MLAB_DIR sets the lab folder holding env and synth/ (default: this script's folder).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
LAB = Path(os.environ.get("MLAB_DIR") or HERE).resolve()
RESULTS = LAB / "results"
APP = "http://127.0.0.1:18081"
PLEX = "http://127.0.0.1:32402"
AGENT_URL = "http://mlab-plex-agent:9494"
OLD_AGENT_URL = "http://mlab-plex-agent-old:9494"
APP_PLEX_CONFIG = "/plexcfg/Library/Application Support/Plex Media Server"
SYNTH_ROOT = "/media/synth-chapters"
EPISODE = f"{SYNTH_ROOT}/Synth Chapters (2021)/Season 01/Synth Chapters (2021) - S01E01.webm"
# What synth_chapters.sh wrote into episode 1's chapters (ms), and what Plex stores for credits (served − 2 s).
TRUTH = {"intro": (10_000, 40_000), "credits": (100_000, 120_000)}
PLEX_OWN_INTRO = (990, 29_306)


def load_env() -> dict[str, str]:
    env = {}
    for line in (LAB / "env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


ENV = load_env()
_SECRETS = sorted({v for k, v in ENV.items() if v and ("TOKEN" in k or "KEY" in k)}, key=len, reverse=True)


def scrub(value: Any) -> Any:
    if isinstance(value, str):
        for secret in _SECRETS:
            value = value.replace(secret, "****")
        return value
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value


def say(*parts: Any) -> None:
    print(*(scrub(str(p)) for p in parts), flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http(method: str, url: str, *, headers: dict | None = None, body: Any = None, timeout: int = 90):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except (urllib.error.URLError, OSError):
        # A container that is still starting (or stopped on purpose): the callers poll, so this is an answer too.
        return 0, None
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text) if text else None
    except json.JSONDecodeError:
        return status, text


def app(method: str, path: str, body: Any = None, *, timeout: int = 90):
    return http(
        method, f"{APP}{path}", headers={"X-Auth-Token": ENV["MLAB_APP_REMOTE_TOKEN"]}, body=body, timeout=timeout
    )


def app_ok(method: str, path: str, body: Any = None, *, timeout: int = 90) -> Any:
    status, data = app(method, path, body, timeout=timeout)
    if status >= 300:
        raise RuntimeError(f"{method} {path} -> {status}: {scrub(data)}")
    return data


def plex(method: str, path: str, **params: Any):
    query = urllib.parse.urlencode(params)
    url = f"{PLEX}{path}?{query}" if query else f"{PLEX}{path}"
    return http(method, url, headers={"X-Plex-Token": ENV["PLEX_TOKEN"], "Accept": "application/json"})


def sh(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def plex_db(sql: str) -> list[list[str]]:
    out = sh(str(HERE / "plexdb.sh"), "-separator", "\t", sql)
    return [line.split("\t") for line in out.splitlines() if line]


def wait_until(what: str, check, *, timeout: float = 300, every: float = 3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = check()
        if answer:
            return answer
        time.sleep(every)
    raise TimeoutError(f"waited {timeout:.0f}s for {what}")


# ------------------------------------------------------------------------------------------------ configuration


def plex_server_entry(agent_url: str = AGENT_URL, token: str | None = None) -> dict:
    return {
        "id": "mlab-plex",
        "type": "plex",
        "name": "Lab Plex (through the agent)",
        "url": "http://mlab-plex:32400",
        "auth": {"method": "token", "token": ENV["PLEX_TOKEN"]},
        # A Plex config folder that exists in this container but is NOT Plex's: an empty volume of its own. The
        # database file is on no path this app can see, which is the point of the row.
        "output": {"plex_config_folder": APP_PLEX_CONFIG},
        "markers": {
            "enabled": True,
            "library_ids": None,
            "plex": {
                "db_write_confirmed_at": now_iso(),
                "on_plex_redetect": "restore",
                "agent": {
                    "enabled": True,
                    "url": agent_url,
                    "token": ENV["MLAB_AGENT_TOKEN"] if token is None else token,
                },
            },
        },
    }


def configure() -> None:
    app_ok("POST", "/api/setup/complete")
    status, existing = app("GET", "/api/servers")
    ids = {s["id"] for s in (existing.get("servers") if isinstance(existing, dict) else existing) or []}
    entry = plex_server_entry()
    if entry["id"] in ids:
        app_ok("PUT", f"/api/servers/{entry['id']}", {k: v for k, v in entry.items() if k != "id"})
    else:
        app_ok("POST", "/api/servers", entry)
    libs = app_ok("POST", "/api/servers/mlab-plex/refresh-libraries")
    say("libraries:", [(lib["id"], lib["name"], lib["remote_paths"]) for lib in libs["libraries"]])
    say("status:", json.dumps(scrub(server_status()["capability"]), indent=2))


def server_status() -> dict:
    return app_ok("GET", "/api/markers/servers/mlab-plex/status?refresh=1")


def set_agent(*, url: str = AGENT_URL, token: str | None = None, enabled: bool = True) -> dict:
    block = {
        "plex": {
            "agent": {"enabled": enabled, "url": url, "token": ENV["MLAB_AGENT_TOKEN"] if token is None else token}
        }
    }
    app_ok("PUT", "/api/servers/mlab-plex", {"markers": block})
    return server_status()


def set_redetect(rule: str) -> None:
    app_ok("PUT", "/api/servers/mlab-plex", {"markers": {"plex": {"on_plex_redetect": rule}}})


def set_detection(*, intro: bool, credits: bool) -> None:
    app_ok("POST", "/api/settings", {"markers": {"detect": {"intro": intro, "credits": credits}}})


# ------------------------------------------------------------------------------------------------- lab state reads


def rating_key() -> str:
    """The lab Plex's item id for the synth episode."""
    rows = plex_db(
        "SELECT mi.metadata_item_id FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id "
        f"WHERE mp.file = '{EPISODE}'"
    )
    if not rows:
        raise RuntimeError(f"Plex doesn't have {EPISODE}")
    return rows[0][0]


def marker_rows() -> list[tuple[str, int, int]]:
    """The item's marker rows as Plex stores them."""
    key = rating_key()
    rows = plex_db(
        "SELECT t.text, t.time_offset, t.end_time_offset FROM taggings t JOIN tags g ON g.id = t.tag_id "
        f"WHERE t.metadata_item_id = {key} AND g.tag_type = 12 AND g.tag = '' ORDER BY t.text, t.time_offset"
    )
    return [(r[0], int(r[1]), int(r[2])) for r in rows]


def served_markers() -> list[tuple[str, int, int]]:
    """What Plex's API serves for the item (what a player would skip)."""
    _, data = plex("GET", f"/library/metadata/{rating_key()}", includeMarkers=1)
    out = []
    for item in data["MediaContainer"]["Metadata"]:
        for marker in item.get("Marker", []):
            out.append((marker["type"], marker["startTimeOffset"], marker["endTimeOffset"]))
    return sorted(out)


def clear_marker_rows() -> None:
    key = rating_key()
    sh(
        str(HERE / "plexdb.sh"),
        f"DELETE FROM taggings WHERE metadata_item_id = {key} AND tag_id IN "
        "(SELECT id FROM tags WHERE tag_type = 12 AND tag = '')",
    )
    sh(str(HERE / "plexdb.sh"), f"UPDATE media_parts SET extra_data = NULL WHERE media_item_id IN "
       f"(SELECT id FROM media_items WHERE metadata_item_id = {key})")  # fmt: skip


def insert_plex_own_intro() -> None:
    """A marker row that looks like Plex's own detection, so "Keep Plex's" has something to keep."""
    key = rating_key()
    tag = plex_db("SELECT id FROM tags WHERE tag_type = 12 AND tag = '' LIMIT 1")[0][0]
    sh(
        str(HERE / "plexdb.sh"),
        "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
        f"created_at, extra_data) VALUES ({key}, {tag}, 0, 'intro', {PLEX_OWN_INTRO[0]}, {PLEX_OWN_INTRO[1]}, '', "
        f'{int(time.time())}, \'{{"pv:version":"5"}}\')',
    )


TERMINAL = ("completed", "failed", "cancelled")


def run_job(body: dict, *, timeout: float = 900) -> list[dict]:
    job = app_ok("POST", "/api/markers/jobs", body)
    say(f"job {job['id'][:8]}: {job['library_name']}")

    def done():
        current = app_ok("GET", f"/api/jobs/{job['id']}")
        return current if current["status"] in TERMINAL else None

    finished = wait_until(f"job {job['id'][:8]}", done, timeout=timeout)
    files = app_ok("GET", f"/api/jobs/{job['id']}/files?per_page=100")["files"]
    say(f"  {finished['status']}: {[(f['outcome'], f.get('reason')) for f in files]}")
    return files


def item_payload() -> dict:
    return app_ok("GET", f"/api/markers/item?{urllib.parse.urlencode({'path': EPISODE})}")


def plex_row(files: list[dict]) -> dict:
    """The mlab-plex row of a job's first file (the job API uses ``id``; the editor's save uses ``server_id``)."""
    row = next((s for f in files for s in (f.get("servers") or []) if s.get("id") == "mlab-plex"), None)
    return row or {}


# ------------------------------------------------------------------------------------------------------- the steps

STEPS: dict[str, Any] = {}


def step(name: str):
    def wrap(fn):
        STEPS[name] = fn
        return fn

    return wrap


@step("isolated")
def step_isolated() -> dict:
    """The app container genuinely cannot see Plex's database — with the agent off it has nothing to write."""
    probe = subprocess.run(
        ["docker", "exec", "mlab-app-remote", "ls", f"{APP_PLEX_CONFIG}/Plug-in Support/Databases"],
        capture_output=True,
        text=True,
    )
    mounts = sh("docker", "inspect", "-f", "{{range .Mounts}}{{.Name}}:{{.Destination}} {{end}}", "mlab-app-remote")
    without_agent = set_agent(enabled=False)["capability"]
    with_agent = set_agent()["capability"]
    return {
        "plex_database_visible_in_app_container": probe.returncode == 0,
        "app_container_ls_error": probe.stderr.strip(),
        "app_container_mounts": mounts,
        "capability_without_the_agent": {"state": without_agent["state"], "message": without_agent["message"]},
        "capability_with_the_agent": {
            "state": with_agent["state"],
            "message": with_agent["message"],
            "db_path": with_agent["details"].get("db_path"),
            "agent": with_agent["details"].get("agent"),
        },
        "ok": probe.returncode != 0
        and "mlab_plex_config" not in mounts
        and without_agent["state"] != "ready"
        and with_agent["state"] == "ready",
    }


@step("published")
def step_published() -> dict:
    """Markers written into Plex's database through the agent, and served by Plex."""
    clear_marker_rows()
    set_redetect("restore")
    files = run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: publish through the agent"})
    rows, served = marker_rows(), served_markers()
    return {
        "outcome": files[0]["outcome"] if files else None,
        "plex_server_row": plex_row(files),
        "taggings_rows": rows,
        "served_by_plex": served,
        "ok": bool(files)
        and files[0]["outcome"] == "markers_published"
        and plex_row(files).get("status") == "markers_written"
        and dict((t, (s, e)) for t, s, e in served) == TRUTH,
    }


@step("read_back")
def step_read_back() -> dict:
    """Check servers reads the item back through the agent and finds our markers still there."""
    files = run_job({"file_paths": [EPISODE], "library_name": "Row 12: read back through the agent"})
    payload = item_payload()
    servers = [
        {
            "server_id": s["server_id"],
            "item_status": s.get("item_status"),
            "plan": s.get("plan"),
            "current": s.get("current"),
        }
        for s in payload.get("servers", [])
    ]
    return {
        "outcome": files[0]["outcome"] if files else None,
        "item_servers": servers,
        "taggings_rows": marker_rows(),
        "ok": bool(files)
        and files[0]["outcome"] in ("markers_up_to_date", "markers_published")
        and [s["plan"] for s in servers] == ["up_to_date"]
        and marker_rows() == [("credits", 98_000, 120_000), ("intro", 10_000, 40_000)],
    }


@step("locked")
def step_locked() -> dict:
    """A marker the user locked replaces Plex's own rows even under "Keep Plex's" — through the agent."""
    clear_marker_rows()
    set_redetect("keep_plex")
    run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: decide before the lock"})
    clear_marker_rows()
    insert_plex_own_intro()
    before = marker_rows()
    status, saved = app(
        "POST",
        "/api/markers/item/markers",
        {"path": EPISODE, "markers": [{"type": "intro", "start_ms": 20_000, "end_ms": 44_000}]},
    )
    after = marker_rows()
    rows = (saved or {}).get("servers", []) if status == 200 else []
    row = next((s for s in rows if s["server_id"] == "mlab-plex"), {})
    set_redetect("restore")
    return {
        "http_status": status,
        "plex_rows_before": before,
        "plex_rows_after": after,
        "server_row": row,
        "ok": status == 200
        and before == [("intro", *PLEX_OWN_INTRO)]
        and ("intro", 20_000, 44_000) in after
        and "intro" in (row.get("replaced_own") or []),
    }


@step("wrong_key")
def step_wrong_key() -> dict:
    """A key that doesn't match: refused, named, and nothing written."""
    before = marker_rows()
    capability = set_agent(token="not-the-shared-key")["capability"]
    files = run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: wrong key"})
    after = marker_rows()
    set_agent()
    return {
        "capability": {"state": capability["state"], "message": capability["message"]},
        "agent_details": capability["details"].get("agent"),
        "plex_server_row": plex_row(files),
        "rows_unchanged": before == after,
        "ok": capability["state"] == "agent_unavailable"
        and "key" in capability["message"]
        and before == after
        and capability["details"].get("agent", {}).get("state") == "rejected",
    }


@step("skew")
def step_skew() -> dict:
    """An agent of another version: refused with both versions named, and nothing written."""
    before = marker_rows()
    subprocess.run(["docker", "rm", "-f", "mlab-plex-agent-old"], capture_output=True)
    # The same image, copied to a writable folder and patched at start-up to report an older version and a protocol
    # this app doesn't speak: an agent left behind on the Plex host while the app moved on.
    sh(
        "docker", "run", "-d", "--name", "mlab-plex-agent-old", "--network", "mlab", "--user", "1000:1000",
        "-p", "127.0.0.1:19495:9494",
        "-e", f"AGENT_TOKEN={ENV['MLAB_AGENT_TOKEN']}",
        "-e", "PLEX_CONFIG_DIR=/plexcfg/Library/Application Support/Plex Media Server",
        "-v", "mlab_plex_config:/plexcfg", "--entrypoint", "sh", "plex-marker-agent:lab", "-c",
        "cp /app/*.py /tmp/ && sed -i 's/^AGENT_VERSION = .*/AGENT_VERSION = \"0.3.0\"/;"
        "s/^PROTOCOLS = .*/PROTOCOLS = [99]/' /tmp/plex_marker_agent.py && "
        "exec gunicorn wsgi:app --chdir /tmp --bind 0.0.0.0:9494 --workers 1 --threads 4",
    )  # fmt: skip
    old_version = wait_until(
        "the old agent to answer",
        lambda: (http("GET", "http://127.0.0.1:19495/v1/health")[1] or {}).get("agent"),
        timeout=60,
    )
    capability = set_agent(url=OLD_AGENT_URL)["capability"]
    files = run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: version skew"})
    after = marker_rows()
    set_agent()
    subprocess.run(["docker", "rm", "-f", "mlab-plex-agent-old"], capture_output=True)
    return {
        "old_agent_reports": old_version,
        "capability": {"state": capability["state"], "message": capability["message"]},
        "agent_details": capability["details"].get("agent"),
        "plex_server_row": plex_row(files),
        "rows_unchanged": before == after,
        "ok": capability["state"] == "agent_unavailable"
        and "0.3.0" in capability["message"]
        and "1.0.0" in capability["message"]
        and before == after,
    }


@step("wrong_plex")
def step_wrong_plex() -> dict:
    """An agent beside a different Plex: refused before anything is written."""
    before = marker_rows()
    subprocess.run(["docker", "rm", "-f", "mlab-plex-agent-elsewhere"], capture_output=True)
    subprocess.run(["docker", "volume", "rm", "-f", "mlab_other_plexcfg"], capture_output=True)
    sh("docker", "volume", "create", "mlab_other_plexcfg")
    # Another Plex's config folder: its own Preferences.xml, with its own machine identifier.
    sh(
        "docker", "run", "--rm", "-v", "mlab_other_plexcfg:/plexcfg", "alpine", "sh", "-c",
        'mkdir -p "/plexcfg/Library/Application Support/Plex Media Server" && '
        'printf \'<?xml version="1.0" encoding="utf-8"?>\\n<Preferences ProcessedMachineIdentifier="another-plex-9999"/>\\n\' '
        '> "/plexcfg/Library/Application Support/Plex Media Server/Preferences.xml" && chown -R 1000:1000 /plexcfg',
    )  # fmt: skip
    sh(
        "docker", "run", "-d", "--name", "mlab-plex-agent-elsewhere", "--network", "mlab", "--user", "1000:1000",
        "-p", "127.0.0.1:19496:9494",
        "-e", f"AGENT_TOKEN={ENV['MLAB_AGENT_TOKEN']}",
        "-e", "PLEX_CONFIG_DIR=/plexcfg/Library/Application Support/Plex Media Server",
        "-v", "mlab_other_plexcfg:/plexcfg", "plex-marker-agent:lab",
    )  # fmt: skip
    wait_until(
        "the other agent to answer",
        lambda: http("GET", "http://127.0.0.1:19496/v1/health")[0] == 200,
        timeout=60,
    )
    capability = set_agent(url="http://mlab-plex-agent-elsewhere:9494")["capability"]
    files = run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: the wrong Plex's agent"})
    after = marker_rows()
    set_agent()
    subprocess.run(["docker", "rm", "-f", "mlab-plex-agent-elsewhere"], capture_output=True)
    subprocess.run(["docker", "volume", "rm", "-f", "mlab_other_plexcfg"], capture_output=True)
    return {
        "capability": {"state": capability["state"], "message": capability["message"]},
        "agent_details": capability["details"].get("agent"),
        "plex_server_row": plex_row(files),
        "rows_unchanged": before == after,
        "ok": capability["state"] == "agent_unavailable"
        and "different Plex server" in capability["message"]
        and before == after,
    }


@step("stopped")
def step_stopped() -> dict:
    """The agent stopped: the file waits with a message a user can act on, and publishes once it is back."""
    clear_marker_rows()
    sh("docker", "stop", "mlab-plex-agent")
    try:
        capability = server_status()["capability"]
        files = run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: agent stopped"})
        while_stopped = marker_rows()
    finally:
        sh("docker", "start", "mlab-plex-agent")
        wait_until("the agent to answer again", lambda: http("GET", "http://127.0.0.1:19494/v1/health")[0] == 200)
    again = run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: agent back"})
    return {
        "capability_while_stopped": {"state": capability["state"], "message": capability["message"]},
        "server_row_while_stopped": plex_row(files),
        "rows_while_stopped": while_stopped,
        "outcome_after_restart": again[0]["outcome"] if again else None,
        "rows_after_restart": marker_rows(),
        "ok": capability["state"] == "agent_unavailable"
        and while_stopped == []
        and bool(again)
        and plex_row(again).get("status") == "markers_written",
    }


@step("cleaned_up")
def step_cleaned_up() -> dict:
    """Nothing decided any more: the rows this app left are taken off again, through the agent."""
    before = marker_rows()
    # A locked marker is the user's and survives a settings change (spec §5.5 rule 1), so the lock goes first.
    unlocked = app("DELETE", "/api/markers/item/markers", {"path": EPISODE, "types": ["intro", "credits"]})[0]
    set_detection(intro=False, credits=False)
    try:
        files = run_job({"file_paths": [EPISODE], "force": True, "library_name": "Row 12: nothing decided"})
        after = marker_rows()
    finally:
        set_detection(intro=True, credits=True)
    return {
        "unlock_status": unlocked,
        "rows_before": before,
        "rows_after": after,
        "outcome": files[0]["outcome"] if files else None,
        "extra_data_after": plex_db(
            "SELECT COALESCE(extra_data, '') FROM media_parts WHERE media_item_id IN "
            f"(SELECT id FROM media_items WHERE metadata_item_id = {rating_key()})"
        ),
        "ok": before != [] and after == [],
    }


def run(names: list[str]) -> None:
    RESULTS.mkdir(exist_ok=True)
    out_file = RESULTS / "phase4-row-12.json"
    recorded = json.loads(out_file.read_text()) if out_file.exists() else {}
    for name in names:
        say(f"--- {name}")
        evidence = STEPS[name]()
        recorded[name] = {"at": now_iso(), **evidence}
        out_file.write_text(json.dumps(scrub(recorded), indent=2, default=str) + "\n")
        say(f"{name}: {'PASS' if evidence.get('ok') else 'FAIL'}")
    passed = [n for n in recorded if recorded[n].get("ok")]
    say(f"row 12: {len(passed)}/{len(recorded)} steps pass — {', '.join(sorted(recorded))}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    if args[0] == "configure":
        configure()
        return
    if args[0] == "status":
        say(json.dumps(scrub(server_status()["capability"]), indent=2))
        return
    if args[0] == "run":
        run(args[1:] or list(STEPS))
        return
    raise SystemExit(f"unknown command {args[0]!r}")


if __name__ == "__main__":
    main()
