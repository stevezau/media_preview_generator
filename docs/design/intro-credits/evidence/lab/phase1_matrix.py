#!/usr/bin/env python3
"""Phase 1 lab matrix for Intro & Credits (spec §10.3, plan Task 19), driven through mlab-app's API and the lab servers.

    ./phase1_matrix.py configure        add the four lab servers to mlab-app, refresh libraries, Intro & Credits settings
    ./phase1_matrix.py status           capability state per server
    ./phase1_matrix.py run 14 1 2 ...   run matrix rows in the given order
    ./phase1_matrix.py scale ...        Task 20 Step 4 scale run on the real library (see `scale` for its steps)

Each row writes results/row-NN.json (git-ignored: evidence/**/*.json) with its result and evidence. Credentials come
from ./env and are scrubbed from everything written or printed. Rows change lab state; phase1-results.md lists the
order used and how to reset the lab. Row 1 re-evaluates its recorded job while results/row-01.json exists. Row 17 runs
in two calls (its verify job waits at least 600 s). REEVALUATE=1 re-checks row 7 from its recorded steps.
MLAB_SHOTS sets where screenshots go.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LAB = Path(__file__).resolve().parent
RESULTS = LAB / "results"
APP = "http://127.0.0.1:18080"
PLEX = "http://127.0.0.1:32402"
JELLYFINS = {"mlab-jellyfin": "http://127.0.0.1:18097", "mlab-jf12": "http://127.0.0.1:18098"}
EMBY = "http://127.0.0.1:18096"
PLEX_CONFIG = "/plexcfg/Library/Application Support/Plex Media Server"
SYNTH_ROOT = "/media/synth-chapters"
SYNTH_SHOW = f"{SYNTH_ROOT}/Synth Chapters (2021)"
SYNTH_SEASON = f"{SYNTH_SHOW}/Season 01"
SYNTH_HOST_SEASON = LAB / "synth" / "Synth Chapters (2021)" / "Season 01"
RICK_SEASON = "/media/tv/Rick and Morty (2013)/Season 01"
TICKS_PER_MS = 10_000

# Chapter truth written by synth_chapters.sh: episode -> (intro start, intro end, credits start, credits end) in ms.
SYNTH_TRUTH = {
    1: (10_000, 40_000, 100_000, 120_000),
    2: (17_000, 47_000, 100_000, 120_000),
    3: (25_000, 55_000, 100_000, 120_000),
}


def synth_path(episode: int, suffix: str = "") -> str:
    return f"{SYNTH_SEASON}/Synth Chapters (2021) - S01E{episode:02d}{suffix}.webm"


# --------------------------------------------------------------------------------------------------- env and output


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


def write_result(row: int, title: str, result: str, evidence: dict, notes: list[str] | None = None) -> dict:
    RESULTS.mkdir(exist_ok=True)
    body = scrub({"row": row, "title": title, "result": result, "at": now_iso(), "notes": notes or [], **evidence})
    (RESULTS / f"row-{row:02d}.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    say(f"row {row}: {result} — {title}")
    for note in notes or []:
        say(f"  - {note}")
    return body


# --------------------------------------------------------------------------------------------------------- HTTP


def http(method: str, url: str, *, headers: dict | None = None, body: Any = None, timeout: int = 60) -> tuple[int, Any]:
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
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text) if text else None
    except json.JSONDecodeError:
        return status, text


def app(method: str, path: str, body: Any = None, *, auth: bool = True, timeout: int = 60) -> tuple[int, Any]:
    headers = {"X-Auth-Token": ENV["MLAB_APP_TOKEN"]} if auth else {}
    return http(method, f"{APP}{path}", headers=headers, body=body, timeout=timeout)


def app_ok(method: str, path: str, body: Any = None) -> Any:
    status, data = app(method, path, body)
    if status >= 300:
        raise RuntimeError(f"{method} {path} -> {status}: {scrub(data)}")
    return data


def plex(method: str, path: str, **params: Any) -> tuple[int, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{PLEX}{path}{'&' if '?' in path else '?'}{query}" if query else f"{PLEX}{path}"
    return http(method, url, headers={"X-Plex-Token": ENV["PLEX_TOKEN"], "Accept": "application/json"})


def jf(server_id: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    token = ENV["JF_TOKEN"] if server_id == "mlab-jellyfin" else ENV["JF12_TOKEN"]
    headers = {"Authorization": f'MediaBrowser Token="{token}"'}
    return http(method, f"{JELLYFINS[server_id]}{path}", headers=headers, body=body)


def emby(method: str, path: str, body: Any = None) -> tuple[int, Any]:
    sep = "&" if "?" in path else "?"
    return http(method, f"{EMBY}/emby{path}{sep}api_key={ENV['EMBY_TOKEN']}", body=body)


def sh(*cmd: str, check: bool = True, timeout: int = 300) -> str:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and out.returncode != 0:
        raise RuntimeError(f"{scrub(' '.join(cmd))} -> {out.returncode}: {scrub(out.stderr[-2000:])}")
    return out.stdout


def wait_until(what: str, fn, timeout: float = 300, every: float = 2) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(every)


# ------------------------------------------------------------------------------------------------- configuration


def server_entries() -> list[dict]:
    return [
        {
            "id": "mlab-plex",
            "type": "plex",
            "name": "Lab Plex",
            "url": "http://mlab-plex:32400",
            "auth": {"method": "token", "token": ENV["PLEX_TOKEN"]},
            "output": {"plex_config_folder": PLEX_CONFIG},
        },
        {
            "id": "mlab-jellyfin",
            "type": "jellyfin",
            "name": "Lab Jellyfin 10.11",
            "url": "http://mlab-jellyfin:8096",
            "auth": {"method": "api_key", "api_key": ENV["JF_TOKEN"]},
        },
        {
            "id": "mlab-jf12",
            "type": "jellyfin",
            "name": "Lab Jellyfin 12.0",
            "url": "http://mlab-jf12:8096",
            "auth": {"method": "api_key", "api_key": ENV["JF12_TOKEN"]},
        },
        {
            "id": "mlab-emby",
            "type": "emby",
            "name": "Lab Emby",
            "url": "http://mlab-emby:8096",
            "auth": {"method": "api_key", "api_key": ENV["EMBY_TOKEN"], "user_id": ENV["EMBY_UID"]},
        },
    ]


MARKERS_ON = {
    "mlab-plex": {"enabled": True, "library_ids": None, "plex": {"db_write_confirmed_at": None}},
    "mlab-jellyfin": {"enabled": True, "library_ids": None},
    "mlab-jf12": {"enabled": True, "library_ids": None},
}


def ensure_plex_synth_library() -> str:
    """The Plex section for the synth show (created once), returning its key."""
    _, data = plex("GET", "/library/sections")
    for section in data["MediaContainer"].get("Directory", []):
        if any(loc["path"] == SYNTH_ROOT for loc in section.get("Location", [])):
            return section["key"]
    status, body = plex(
        "POST",
        "/library/sections",
        name="Synth Chapters",
        type="show",
        agent="tv.plex.agents.series",
        scanner="Plex TV Series",
        language="en-US",
        location=SYNTH_ROOT,
    )
    if status >= 300:
        raise RuntimeError(f"Plex section create -> {status}: {body}")
    return ensure_plex_synth_library()


def plex_episodes(section_key: str) -> list[dict]:
    _, data = plex("GET", f"/library/sections/{section_key}/all", type=4)
    return data["MediaContainer"].get("Metadata", [])


def configure() -> None:
    app_ok("POST", "/api/setup/complete")
    status, existing = app("GET", "/api/servers")
    existing_ids = {s["id"] for s in (existing.get("servers", existing) if isinstance(existing, dict) else existing)}
    for entry in server_entries():
        if entry["id"] in existing_ids:
            app_ok("PUT", f"/api/servers/{entry['id']}", {k: v for k, v in entry.items() if k != "id"})
            say(f"updated server {entry['id']}")
        else:
            app_ok("POST", "/api/servers", entry)
            say(f"added server {entry['id']}")

    section = ensure_plex_synth_library()
    plex("GET", f"/library/sections/{section}/refresh")
    wait_until("Plex to list the synth episodes", lambda: len(plex_episodes(section)) >= 3, timeout=180)
    say(f"Plex section {section} lists {len(plex_episodes(section))} synth episodes")

    for entry in server_entries():
        libs = app_ok("POST", f"/api/servers/{entry['id']}/refresh-libraries")
        say(f"{entry['id']} libraries:", [(lib["id"], lib["name"], lib["remote_paths"]) for lib in libs["libraries"]])

    for server_id, block in MARKERS_ON.items():
        block = json.loads(json.dumps(block))
        if "plex" in block:
            block["plex"]["db_write_confirmed_at"] = now_iso()
        app_ok("PUT", f"/api/servers/{server_id}", {"markers": block})
        say(f"Intro & Credits on for {server_id}")

    stored = app_ok("GET", "/api/settings")["markers"]
    sources = [
        {**s, "enabled": True, "api_key": ""}
        if s["id"] == "theintrodb"
        else {k: v for k, v in s.items() if k != "api_key"}
        for s in stored["sources"]
    ]
    app_ok("POST", "/api/settings", {"markers": {"sources": sources}})
    say("markers settings:", app_ok("GET", "/api/settings")["markers"])


def status() -> dict:
    out = {}
    for entry in server_entries():
        payload = app_ok("GET", f"/api/markers/servers/{entry['id']}/status")
        out[entry["id"]] = {
            "enabled": payload["enabled"],
            "capability": payload["capability"],
            "can_show": payload["can_show"],
            "libraries": [(lib["id"], lib["name"], lib["default_selected"]) for lib in payload["libraries"]],
        }
        cap = payload["capability"]
        say(f"{entry['id']}: enabled={payload['enabled']} state={cap['state']} — {cap['message']}")
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "status.json").write_text(json.dumps(scrub(out), indent=2) + "\n")
    return out


# ------------------------------------------------------------------------------------------------ lab state reads

TERMINAL = ("completed", "failed", "cancelled")


def start_markers_job(body: dict) -> dict:
    job = app_ok("POST", "/api/markers/jobs", body)
    say(f"job {job['id'][:8]} created: {job['library_name']}")
    return job


def wait_job(job_id: str, timeout: float = 1800) -> dict:
    def done() -> dict | None:
        job = app_ok("GET", f"/api/jobs/{job_id}")
        return job if job["status"] in TERMINAL else None

    job = wait_until(f"job {job_id[:8]}", done, timeout=timeout, every=3)
    say(f"job {job_id[:8]} {job['status']}: {job['progress'].get('outcome')}")
    return job


def job_files(job_id: str) -> list[dict]:
    return app_ok("GET", f"/api/jobs/{job_id}/files?per_page=500")["files"]


def item_payload(path: str) -> dict:
    return app_ok("GET", f"/api/markers/item?{urllib.parse.urlencode({'path': path})}")


def source_usage() -> dict:
    return {k: v["used"] for k, v in app_ok("GET", "/api/markers/sources/usage").items()}


def plex_db(sql: str) -> list[list[str]]:
    out = sh(str(LAB / "plexdb.sh"), "-separator", "\t", sql)
    return [line.split("\t") for line in out.splitlines() if line]


def plex_marker_rows() -> list[dict]:
    rows = plex_db(
        "select t.id, t.metadata_item_id, t.text, t.time_offset, t.end_time_offset, t.[index], t.extra_data "
        "from taggings t join tags g on g.id=t.tag_id where g.tag_type=12 and t.text in ('intro','credits') "
        "order by t.metadata_item_id, t.text, t.time_offset"
    )
    keys = ("id", "item", "text", "start", "end", "index", "extra_data")
    return [dict(zip(keys, r, strict=False)) for r in rows]


def plex_parts() -> dict[str, dict]:
    """Local file path -> {item, part, extra_data} for every Plex part."""
    rows = plex_db(
        "select mi.id, p.id, p.file, p.extra_data from metadata_items mi join media_items m on m.metadata_item_id=mi.id "
        "join media_parts p on p.media_item_id=m.id where mi.metadata_type in (1,4)"
    )
    return {r[2]: {"item": r[0], "part": r[1], "extra_data": r[3] if len(r) > 3 else ""} for r in rows}


def plex_served(rating_key: str, *, per_user: bool = False) -> list[dict]:
    """Intro/credits Plex serves for an item.

    Item markers (taggings rows) carry the marker tag's ``id``. A per-user marker from ``metadata_item_setting_markers``
    has no top-level id and is left out unless ``per_user``: the lab Plex has one (marker_type 1, served as "intro" on
    Rick and Morty S01E07) left over from the Sep 12 marker-API research.
    """
    _, data = plex("GET", f"/library/metadata/{rating_key}", includeMarkers=1)
    meta = data["MediaContainer"]["Metadata"][0]
    return [
        {"type": m["type"], "start": m["startTimeOffset"], "end": m["endTimeOffset"], "final": m.get("final", False)}
        for m in meta.get("Marker", [])
        if m["type"] in ("intro", "credits") and ("id" in m) != per_user
    ]


def jf_items(server_id: str) -> dict[str, dict]:
    """File path -> item for every episode and movie on a Jellyfin server."""
    _, data = jf(server_id, "GET", "/Items?Recursive=true&IncludeItemTypes=Episode,Movie&Fields=Path,MediaSources")
    out = {}
    for item in data["Items"]:
        for source in item.get("MediaSources") or [{"Path": item.get("Path"), "Id": item["Id"]}]:
            if source.get("Path"):
                out[source["Path"]] = {
                    "id": source.get("Id") or item["Id"],
                    "item_id": item["Id"],
                    "name": item["Name"],
                }
    return out


def jf_segments(server_id: str, item_id: str) -> list[dict]:
    _, data = jf(server_id, "GET", f"/MediaSegments/{item_id}")
    return [
        {"type": s["Type"], "start_ticks": s["StartTicks"], "end_ticks": s["EndTicks"]}
        for s in sorted((data or {}).get("Items", []), key=lambda s: (s["Type"], s["StartTicks"]))
    ]


def plugin_marker_files(container: str) -> dict[str, str]:
    out = sh(
        "docker",
        "exec",
        container,
        "find",
        "/config/plugins/Jellyfin.Plugin.MediaPreviewBridge/markers",
        "-type",
        "f",
        "-printf",
        "%f\t%T@\t%s\n",
        check=False,
    )
    return {line.split("\t")[0]: line.split("\t", 1)[1] for line in out.splitlines() if "\t" in line}


def plex_wait_idle(min_wait: float = 5, timeout: float = 600) -> None:
    time.sleep(min_wait)
    quiet = 0

    def idle() -> bool:
        nonlocal quiet
        _, data = plex("GET", "/activities")
        busy = [a for a in data["MediaContainer"].get("Activity", []) if a.get("type") != "butler"]
        quiet = 0 if busy else quiet + 1
        return quiet >= 2

    wait_until("Plex activities to finish", idle, timeout=timeout, every=3)


def synth_snapshot() -> dict:
    """What every server serves for the synth episodes and Rick and Morty S01 right now."""
    parts = plex_parts()
    items = {sid: jf_items(sid) for sid in JELLYFINS}
    snap: dict[str, Any] = {"plex": {}, "jellyfin": {sid: {} for sid in JELLYFINS}}
    for path, part in sorted(parts.items()):
        if path.startswith((SYNTH_ROOT, RICK_SEASON)):
            snap["plex"][path] = {"item": part["item"], "served": plex_served(part["item"])}
    for sid in JELLYFINS:
        for path, item in sorted(items[sid].items()):
            if path.startswith((SYNTH_ROOT, RICK_SEASON)):
                snap["jellyfin"][sid][path] = {"id": item["id"], "segments": jf_segments(sid, item["id"])}
    return snap


# ------------------------------------------------------------------------------------------------------------ rows

ROWS: dict[int, Any] = {}


def row(number: int):
    def register(fn):
        ROWS[number] = fn
        return fn

    return register


class StatsSampler(threading.Thread):
    """`docker stats` for mlab-app until stopped (row 15)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.samples: list[dict] = []
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            out = sh("docker", "stats", "--no-stream", "--format", "{{json .}}", "mlab-app", check=False)
            if out.strip():
                data = json.loads(out.strip().splitlines()[-1])
                self.samples.append(
                    {
                        "t": now_iso(),
                        "cpu": data["CPUPerc"],
                        "mem": data["MemUsage"].split(" / ")[0],
                        "pids": data["PIDs"],
                    }
                )
            self.stop.wait(0.2)


def _mib(text: str) -> float:
    units = {"B": 1 / 2**20, "KiB": 1 / 1024, "MiB": 1, "GiB": 1024}
    for unit in ("KiB", "MiB", "GiB", "B"):
        if text.endswith(unit):
            return float(text[: -len(unit)]) * units[unit]
    return float("nan")


def backfill_paths() -> list[str]:
    return [SYNTH_SHOW, RICK_SEASON]


@row(1)
def row_01_backfill() -> dict:
    """Backfill job on Synth Chapters + Rick and Morty S01: completes; outcome counts; per-server rows.

    The job runs once: when results/row-01.json already names a job, that job is evaluated again (a second run would
    be row 4's). Delete the file to run a fresh backfill.
    """
    previous = RESULTS / "row-01.json"
    if previous.exists() and json.loads(previous.read_text()).get("job", {}).get("id"):
        recorded = json.loads(previous.read_text())
        job = app_ok("GET", f"/api/jobs/{recorded['job']['id']}")
        usage_before, usage_after = recorded["usage_before"], recorded["usage_after"]
        samples, window = recorded["resources"]["samples"], recorded["resources"]["window"]
    else:
        RESULTS.mkdir(exist_ok=True)
        baseline = {"plex_marker_rows": plex_marker_rows(), "snapshot": synth_snapshot(), "usage": source_usage()}
        (RESULTS / "baseline-before-row-01.json").write_text(json.dumps(scrub(baseline), indent=2) + "\n")
        usage_before = baseline["usage"]
        app_ok("PUT", "/api/settings/log-level", {"log_level": "DEBUG"})
        since = datetime.now(timezone.utc)
        sampler = StatsSampler()
        sampler.start()
        try:
            job = start_markers_job({"file_paths": backfill_paths(), "library_name": "Phase 1 row 1 backfill"})
            job = wait_job(job["id"], timeout=3600)
        finally:
            sampler.stop.set()
            sampler.join(timeout=30)
            app_ok("PUT", "/api/settings/log-level", {"log_level": "INFO"})
        samples, window = sampler.samples, [since.isoformat(), datetime.now(timezone.utc).isoformat()]
        usage_after = source_usage()
    files = job_files(job["id"])
    lookups = online_lookup_times(window)

    per_server: dict[str, dict[str, int]] = {}
    for f in files:
        for srv in f.get("servers") or []:
            per_server.setdefault(srv["id"], {})
            per_server[srv["id"]][srv["status"]] = per_server[srv["id"]].get(srv["status"], 0) + 1
    synth = [f for f in files if f["file"].startswith(SYNTH_ROOT)]
    synth_ok = len(synth) == 3 and all(f["outcome"] == "markers_published" for f in synth)
    synth_servers_ok = all(
        {s["id"] for s in f.get("servers") or [] if s["status"] == "markers_written"}
        >= {"mlab-plex", "mlab-jellyfin", "mlab-jf12"}
        for f in synth
    )
    passed = job["status"] == "completed" and synth_ok and synth_servers_ok and len(files) == 14
    duration = (datetime.fromisoformat(job["completed_at"]) - datetime.fromisoformat(job["started_at"])).total_seconds()
    notes = [
        f"job {job['id'][:8]} {job['status']} in {duration:.1f} s; outcome counts "
        f"{ {k: v for k, v in (job['progress'].get('outcome') or {}).items() if v} }",
        f"per-server statuses {per_server}",
        f"online lookups (used today) before {usage_before} after {usage_after}",
    ]
    return write_result(
        1,
        "Backfill job on Synth Chapters + Rick and Morty S01",
        "pass" if passed else "fail",
        {
            "job": {k: job.get(k) for k in ("id", "status", "created_at", "started_at", "completed_at", "priority")},
            "outcome_counts": job["progress"].get("outcome"),
            "per_server_status_counts": per_server,
            "files": files,
            "usage_before": usage_before,
            "usage_after": usage_after,
            "resources": {"samples": samples, "online_lookups": lookups, "window": window},
        },
        notes,
    )


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LOOKUP = re.compile(r"^(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d).*?(TheIntroDB|IntroDB|SkipDB) lookup \{.*→ HTTP (\d+)")


def online_lookup_times(window: list[str]) -> list[dict]:
    """Online requests the app logged (DEBUG) inside ``window``: [{t, source, status}]."""
    out = subprocess.run(
        ["docker", "logs", "--since", window[0], "--until", window[1], "mlab-app"], capture_output=True, text=True
    )
    found = []
    for line in (out.stdout + out.stderr).splitlines():
        match = _LOOKUP.match(_ANSI.sub("", line))
        if match:
            found.append({"t": match.group(1), "source": match.group(2), "status": int(match.group(3))})
    return found


def decided(payload: dict) -> dict[str, dict]:
    """Marker type -> decided marker (start_ms, end_ms) from an Inspector payload."""
    return {t: d["marker"] for t, d in payload["decisions"].items() if d["status"] == "decided" and d.get("marker")}


def library_files(prefix: str) -> list[str]:
    return sorted(p for p in plex_parts() if p.startswith(prefix))


@row(2)
def row_02_plex_served() -> dict:
    """Plex includeMarkers=1 equals the decisions: synth = chapters exactly (credits after the -2 s DB shift); Rick and
    Morty = the agreed markers; types without agreement keep Plex's own rows."""
    parts = plex_parts()
    baseline = json.loads((RESULTS / "baseline-before-row-01.json").read_text())["snapshot"]["plex"]
    db_rows = plex_marker_rows()
    checks = []
    for path in library_files(SYNTH_SEASON) + library_files(RICK_SEASON):
        item = parts[path]["item"]
        served = plex_served(item)
        payload = item_payload(path)
        wanted = decided(payload)
        for mtype in ("intro", "credits"):
            got = [(m["start"], m["end"]) for m in served if m["type"] == mtype]
            if mtype in wanted:
                expect = [(wanted[mtype]["start_ms"], wanted[mtype]["end_ms"])]
                basis = "decision"
            else:
                expect = [
                    (m["start"], m["end"]) for m in baseline.get(path, {}).get("served", []) if m["type"] == mtype
                ]
                basis = f"Plex's own rows kept ({payload['decisions'][mtype]['status']})"
            check = {"file": path.rsplit("/", 1)[-1], "item": item, "type": mtype, "served": got, "expected": expect}
            check["basis"] = basis
            check["ok"] = got == expect
            if path.startswith(SYNTH_ROOT):
                episode = int(path[-7:-5])
                truth = SYNTH_TRUTH[episode]
                chapter = [(truth[0], truth[1])] if mtype == "intro" else [(truth[2], truth[3])]
                stored = [(int(r["start"]), int(r["end"])) for r in db_rows if r["item"] == item and r["text"] == mtype]
                check["chapter_truth"] = chapter
                check["db_stored"] = stored
                shift = 2_000 if mtype == "credits" else 0
                check["ok"] = check["ok"] and got == chapter and [s for s, _ in stored] == [chapter[0][0] - shift]
            checks.append(check)
    per_user = {parts[p]["item"]: plex_served(parts[p]["item"], per_user=True) for p in library_files(RICK_SEASON)}
    per_user = {k: v for k, v in per_user.items() if v}
    passed = all(c["ok"] for c in checks)
    bad = [
        f"{c['file'][:40]} {c['type']}: served {c['served']} expected {c['expected']}" for c in checks if not c["ok"]
    ]
    kept = [f"{c['file'][:32]} {c['type']}" for c in checks if c["basis"].startswith("Plex's own")]
    notes = bad + [f"types left to Plex's own rows (not decided): {kept}"]
    if per_user:
        notes.append(
            f"per-user markers also served (metadata_item_setting_markers, not written by the app): {per_user}"
        )
    return write_result(
        2,
        "Plex serves the decided markers (includeMarkers=1)",
        "pass" if passed else "fail",
        {"checks": checks, "per_user_markers": per_user},
        notes,
    )


@row(3)
def row_03_jellyfin_segments() -> dict:
    """Both Jellyfins' /MediaSegments show Intro/Outro for the same files, ticks equal to the decisions."""
    kinds = {"intro": "Intro", "credits": "Outro"}
    checks = []
    items = {sid: jf_items(sid) for sid in JELLYFINS}
    for path in library_files(SYNTH_SEASON) + library_files(RICK_SEASON):
        wanted = decided(item_payload(path))
        expect = sorted(
            (kinds[t], m["start_ms"] * TICKS_PER_MS, m["end_ms"] * TICKS_PER_MS)
            for t, m in wanted.items()
            if t in kinds
        )
        row_check = {"file": path.rsplit("/", 1)[-1], "expected": expect}
        for sid in JELLYFINS:
            item = items[sid].get(path)
            segs = jf_segments(sid, item["id"]) if item else []
            row_check[sid] = sorted((s["type"], s["start_ticks"], s["end_ticks"]) for s in segs)
        row_check["ok"] = all(row_check[sid] == expect for sid in JELLYFINS)
        checks.append(row_check)
    passed = all(c["ok"] for c in checks)
    notes = [
        f"{c['file'][:40]}: expected {c['expected']} got {[c[s] for s in JELLYFINS]}" for c in checks if not c["ok"]
    ]
    return write_result(
        3,
        "Jellyfin 10.11 and 12.0 /MediaSegments match the decisions",
        "pass" if passed else "fail",
        {"checks": checks},
        notes,
    )


SHOTS = Path(os.environ.get("MLAB_SHOTS", "/tmp/mlab-phase1-shots"))
VENV_PYTHON = "/home/data/.venv/bin/python"
SOUTH_PARK_SEASON = "/media/tv/South Park (1997)/Season 01"
MOVIES = "/media/movies"


def plex_item_state(paths: list[str]) -> dict:
    """Taggings rows, part extra_data and served item markers for the Plex items holding ``paths``."""
    parts = plex_parts()
    items = sorted({parts[p]["item"] for p in paths if p in parts})
    rows = [r for r in plex_marker_rows() if r["item"] in items]
    return {
        "rows": rows,
        "parts": {p: parts[p]["extra_data"] for p in sorted(parts) if parts[p]["item"] in items},
        "served": {i: plex_served(i) for i in items},
    }


def jf_state(paths: list[str]) -> dict:
    out = {}
    for sid in JELLYFINS:
        items = jf_items(sid)
        out[sid] = {p: jf_segments(sid, items[p]["id"]) if p in items else None for p in paths}
    return out


def all_backfill_files() -> list[str]:
    return library_files(SYNTH_SEASON) + library_files(RICK_SEASON)


@row(13)
def row_13_jellyfin_web_skip() -> dict:
    """jellyfin-web 10.11 and 12.0 show Skip Intro on a synth episode (our segment), via lab/jf_client.py."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = synth_path(1)
    checks = []
    for sid, base in JELLYFINS.items():
        item = jf_items(sid)[path]
        shot = SHOTS / f"row13-{sid}.png"
        out = subprocess.run(
            [VENV_PYTHON, str(LAB / "jf_client.py"), item["id"], str(shot), base],
            capture_output=True,
            text=True,
            timeout=240,
        )
        text = (out.stdout + out.stderr).strip()
        found = "skip button found: True" in text
        checks.append(
            {
                "server": sid,
                "item": item["id"],
                "segments": jf_segments(sid, item["id"]),
                "output": text[-800:],
                "screenshot": str(shot),
                "ok": found,
            }
        )
    notes = [f"{c['server']}: {c['output'].splitlines()[-1] if c['output'] else 'no output'}" for c in checks]
    return write_result(
        13,
        "Jellyfin web players show Skip Intro for our segment",
        "pass" if all(c["ok"] for c in checks) else "fail",
        {"checks": checks},
        notes,
    )


@row(4)
def row_04_second_backfill() -> dict:
    """Second backfill: every file Up to date, zero writes (Plex rows/parts unchanged, plugin files unchanged)."""
    paths = all_backfill_files()
    before = {
        "plex": plex_item_state(paths),
        "plugin_files": {c: plugin_marker_files(c) for c in JELLYFINS},
        "usage": source_usage(),
    }
    job = start_markers_job({"file_paths": backfill_paths(), "library_name": "Phase 1 row 4 second backfill"})
    job = wait_job(job["id"])
    files = job_files(job["id"])
    after = {
        "plex": plex_item_state(paths),
        "plugin_files": {c: plugin_marker_files(c) for c in JELLYFINS},
        "usage": source_usage(),
    }
    outcomes = {f["file"].rsplit("/", 1)[-1]: f["outcome"] for f in files}
    # A file with a marker still in review folds to Needs review (outcome precedence); its servers must still be up to
    # date, and no server row may say written.
    server_statuses = {s["status"] for f in files for s in f["servers"]}
    all_up_to_date = (
        len(files) == len(paths)
        and all(
            o == "markers_up_to_date" or (o == "markers_needs_review" and "needs review" in f["reason"])
            for f, o in zip(files, outcomes.values(), strict=True)
        )
        and server_statuses <= {"markers_up_to_date", "markers_needs_review"}
    )
    same_rows = [r["id"] for r in before["plex"]["rows"]] == [r["id"] for r in after["plex"]["rows"]]
    same_parts = before["plex"]["parts"] == after["plex"]["parts"]
    same_plugin = before["plugin_files"] == after["plugin_files"]
    passed = job["status"] == "completed" and all_up_to_date and same_rows and same_parts and same_plugin
    notes = [
        f"job {job['id'][:8]} {job['status']}; outcomes {sorted(set(outcomes.values()))}; server rows {sorted(server_statuses)}",
        f"Plex taggings row ids unchanged: {same_rows} ({len(after['plex']['rows'])} rows); parts extra_data unchanged: {same_parts}",
        f"plugin marker files (name, mtime, size) unchanged on both Jellyfins: {same_plugin}",
        f"online lookups used today before {before['usage']} after {after['usage']}",
    ]
    return write_result(
        4,
        "Second backfill: all Up to date, zero writes",
        "pass" if passed else "fail",
        {"job": job, "files": files, "before": before, "after": after},
        notes,
    )


def jf_task(sid: str, key: str) -> dict:
    _, tasks = jf(sid, "GET", "/ScheduledTasks")
    return next(t for t in tasks if t["Key"] == key)


def jf_wait_task_idle(sid: str, key: str, started_after: str | None = None, timeout: float = 600) -> None:
    def idle() -> bool:
        task = jf_task(sid, key)
        if task["State"] != "Idle":
            return False
        last = (task.get("LastExecutionResult") or {}).get("EndTimeUtc") or ""
        return started_after is None or last >= started_after

    time.sleep(3)
    wait_until(f"{sid} task {key}", idle, timeout=timeout, every=3)


def jf_wait_healthy(sid: str, timeout: float = 300) -> None:
    def healthy() -> bool:
        try:
            status, _ = jf(sid, "GET", "/System/Info")
        except OSError:
            return False
        return status == 200

    wait_until(f"{sid} to answer", healthy, timeout=timeout, every=3)
    time.sleep(10)


def jf_series_id(sid: str, path: str) -> str:
    _, data = jf(sid, "GET", "/Items?Recursive=true&IncludeItemTypes=Series&Fields=Path")
    return next(i["Id"] for i in data["Items"] if i.get("Path") == path)


@row(6)
def row_06_jellyfin_wipe_matrix() -> dict:
    """Both Jellyfins: library scan, refresh with ReplaceAllMetadata, Media Segment Scan, restart -> segments kept."""
    paths = all_backfill_files()
    reference = jf_state(paths)
    steps = []

    def check(step: str) -> None:
        now = jf_state(paths)
        diff = {sid: [p.rsplit("/", 1)[-1] for p in paths if now[sid][p] != reference[sid][p]] for sid in JELLYFINS}
        steps.append({"step": step, "changed": diff, "ok": not any(diff.values())})
        say(f"  {step}: changed {diff}")

    for sid in JELLYFINS:
        t0 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        jf(sid, "POST", "/Library/Refresh")
        jf_wait_task_idle(sid, "RefreshLibrary", started_after=t0)
    check("library scan (Scan Media Library)")

    for sid in JELLYFINS:
        series = jf_series_id(sid, SYNTH_SHOW)
        jf(
            sid,
            "POST",
            f"/Items/{series}/Refresh?Recursive=true&MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh"
            "&ReplaceAllMetadata=true&ReplaceAllImages=false",
        )
        rick = jf_items(sid)[library_files(RICK_SEASON)[0]]["id"]
        jf(
            sid,
            "POST",
            f"/Items/{rick}/Refresh?MetadataRefreshMode=FullRefresh&ImageRefreshMode=FullRefresh"
            "&ReplaceAllMetadata=true&ReplaceAllImages=false",
        )
    time.sleep(30)
    check("refresh with ReplaceAllMetadata (synth series recursive + Rick and Morty S01E01)")

    for sid in JELLYFINS:
        t0 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        task = jf_task(sid, "TaskExtractMediaSegments")
        jf(sid, "POST", f"/ScheduledTasks/Running/{task['Id']}")
        jf_wait_task_idle(sid, "TaskExtractMediaSegments", started_after=t0)
    check("Media Segment Scan task")

    for sid in JELLYFINS:
        sh("docker", "restart", sid)
    for sid in JELLYFINS:
        jf_wait_healthy(sid)
    check("container restart")

    passed = all(s["ok"] for s in steps)
    return write_result(
        6,
        "Jellyfin wipe matrix: segments kept",
        "pass" if passed else "fail",
        {"reference": reference, "steps": steps},
        [f"{s['step']}: {'kept' if s['ok'] else s['changed']}" for s in steps],
    )


def plex_set_prefs(**prefs: str) -> None:
    status, body = plex("PUT", "/:/prefs", **prefs)
    if status >= 300:
        raise RuntimeError(f"Plex prefs {prefs} -> {status}: {body}")


@row(5)
def row_05_plex_wipe_matrix() -> dict:
    """Plex (synth S01E03): forced metadata refresh, section scan, analyze with detection off, non-forced detection ->
    still served; forced credits detection -> Plex replaces ours (phase 2 reconciles; behaviour recorded)."""
    path = synth_path(3)
    parts = plex_parts()
    item = parts[path]["item"]
    _, meta = plex("GET", f"/library/metadata/{item}")
    season = meta["MediaContainer"]["Metadata"][0]["parentRatingKey"]
    _, sections = plex("GET", "/library/sections")
    section = next(
        s["key"]
        for s in sections["MediaContainer"]["Directory"]
        if any(loc["path"] == SYNTH_ROOT for loc in s["Location"])
    )
    reference = plex_item_state([path])
    _, prefs = plex("GET", "/:/prefs")
    detection = {
        s["id"]: s["value"]
        for s in prefs["MediaContainer"]["Setting"]
        if s["id"] in ("GenerateIntroMarkerBehavior", "GenerateCreditsMarkerBehavior", "MarkerSource")
    }
    steps = []

    def check(step: str, expect_kept: bool = True) -> None:
        plex_wait_idle()
        now = plex_item_state([path])
        kept = now["served"] == reference["served"] and [r["id"] for r in now["rows"]] == [
            r["id"] for r in reference["rows"]
        ]
        steps.append(
            {
                "step": step,
                "kept": kept,
                "served": now["served"][item],
                "rows": now["rows"],
                "part_extra_data": now["parts"][path],
                "ok": kept or not expect_kept,
            }
        )
        say(f"  {step}: kept={kept} served={now['served'][item]}")

    plex("PUT", f"/library/metadata/{item}/refresh", force=1)
    check("metadata refresh (force)")
    plex("GET", f"/library/sections/{section}/refresh")
    check("section scan")
    plex_set_prefs(GenerateIntroMarkerBehavior="never", GenerateCreditsMarkerBehavior="never")
    try:
        plex("PUT", f"/library/metadata/{item}/analyze")
        check("analyze with intro/credits detection set to never")
    finally:
        plex_set_prefs(
            GenerateIntroMarkerBehavior=detection["GenerateIntroMarkerBehavior"],
            GenerateCreditsMarkerBehavior=detection["GenerateCreditsMarkerBehavior"],
        )
    plex("PUT", f"/library/metadata/{item}/credits")
    plex("PUT", f"/library/metadata/{season}/intro")
    check("non-forced detection (episode credits, season intro)")
    plex("PUT", f"/library/metadata/{item}/credits", force=1)
    check("forced credits detection (credits?force=1)", expect_kept=False)

    # Plex's credits detector fails on the synthetic VP9 files (Plex log: "Completed credits detection for item …
    # (success: 0, failures: N)"), which leaves ours in place. A real episode shows whether a successful run replaces.
    rick = library_files(RICK_SEASON)[0]
    rick_item = plex_parts()[rick]["item"]
    rick_before = plex_item_state([rick])
    plex("PUT", f"/library/metadata/{rick_item}/credits", force=1)
    plex_wait_idle(min_wait=10, timeout=480)
    rick_after = plex_item_state([rick])
    rick_job = wait_job(
        start_markers_job({"file_paths": [rick], "library_name": "Phase 1 row 5 after Plex re-detect"})["id"]
    )
    rick_restored = plex_item_state([rick])
    replaced = rick_after["served"][rick_item] != rick_before["served"][rick_item]
    rick_step = {
        "step": "forced credits detection on Rick and Morty S01E01, then a normal Intro & Credits job",
        "served_before": rick_before["served"][rick_item],
        "served_after_plex": rick_after["served"][rick_item],
        "job": {"id": rick_job["id"], "status": rick_job["status"], "files": job_files(rick_job["id"])},
        "served_after_job": rick_restored["served"][rick_item],
        "replaced_by_plex": replaced,
    }

    rick_step["plex_row"] = next(iter(server_rows(rick_job["id"])), {}).get("servers", {}).get("mlab-plex")
    rick_step["restored"] = rick_step["served_after_job"] == rick_step["served_before"]
    passed = all(s["ok"] for s in steps) and (
        not replaced or (rick_step["restored"] and rick_step["plex_row"] == "markers_written")
    )
    notes = [f"{s['step']}: {'kept' if s['kept'] else 'changed -> ' + str(s['served'])}" for s in steps]
    notes.append(
        f"Rick and Morty S01E01 after the normal job: Plex row {rick_step['plex_row']}, ours restored {rick_step['restored']}"
    )
    notes.append(
        f"Rick and Morty S01E01 forced credits: before {rick_step['served_before']} -> after Plex "
        f"{rick_step['served_after_plex']} (replaced: {replaced}) -> after a normal job "
        f"{rick_step['served_after_job']} ({[(f['outcome'], f['reason']) for f in rick_step['job']['files']]})"
    )
    notes.append(f"Plex detection prefs during the run: {detection}")
    steps.append(rick_step)
    return write_result(
        5,
        "Plex wipe matrix",
        "pass" if passed else "fail",
        {
            "item": item,
            "season": season,
            "section": section,
            "reference": reference,
            "steps": steps,
            "prefs": detection,
        },
        notes,
    )


def refresh_all_servers() -> None:
    _, sections = plex("GET", "/library/sections")
    for s in sections["MediaContainer"]["Directory"]:
        if any(loc["path"] == SYNTH_ROOT for loc in s["Location"]):
            plex("GET", f"/library/sections/{s['key']}/refresh")
    for sid in JELLYFINS:
        t0 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        jf(sid, "POST", "/Library/Refresh")
        jf_wait_task_idle(sid, "RefreshLibrary", started_after=t0)
    plex_wait_idle()


def evidence_fetch_times(payload: dict) -> dict[str, str]:
    return {e["source"]: e["fetched_at"] for e in payload["evidence"] if e["source"] == "chapters"}


def served_everywhere(path: str) -> dict:
    parts = plex_parts()
    out = {"mlab-plex": plex_served(parts[path]["item"]) if path in parts else None}
    out.update({sid: segs for sid, segs in ((s, jf_state([path])[s][path]) for s in JELLYFINS)})
    return out


def truth_everywhere(episode: int) -> dict:
    t = SYNTH_TRUTH[episode]
    return {
        "mlab-plex": [
            {"type": "credits", "start": t[2], "end": t[3], "final": False},
            {"type": "intro", "start": t[0], "end": t[1], "final": False},
        ],
        **{
            sid: [
                {"type": "Intro", "start_ticks": t[0] * TICKS_PER_MS, "end_ticks": t[1] * TICKS_PER_MS},
                {"type": "Outro", "start_ticks": t[2] * TICKS_PER_MS, "end_ticks": t[3] * TICKS_PER_MS},
            ]
            for sid in JELLYFINS
        },
    }


def same_markers(served: dict, truth: dict) -> dict[str, bool]:
    def norm(v: list | None) -> list:
        return sorted(
            (m["type"], m.get("start", m.get("start_ticks")), m.get("end", m.get("end_ticks"))) for m in (v or [])
        )

    return {sid: norm(served.get(sid)) == norm(truth[sid]) for sid in truth}


@row(7)
def row_07_file_change() -> dict:
    """Touch a synth file's mtime -> the next job re-probes and the servers end up showing our markers.

    A: touch E02 -> job -> servers rescan (they drop markers) -> normal job reads back and writes them again.
    B: touch E03 -> servers rescan -> job (the usual order).
    C: touch E02 -> servers rescan -> forced job: a restore is reported "Markers written".
    """
    steps = []

    def touch(episode: int) -> None:
        os.utime(SYNTH_HOST_SEASON / synth_path(episode).rsplit("/", 1)[-1], None)

    def job_step(step: str, episode: int, *, force: bool = False) -> dict:
        path = synth_path(episode)
        before = evidence_fetch_times(item_payload(path))
        body = {"file_paths": [path], "library_name": f"Phase 1 row 7 {step}", "force": force}
        job = wait_job(start_markers_job(body)["id"])
        rows = server_rows(job["id"])
        served = served_everywhere(path)
        entry = {
            "step": step,
            "job": job["id"],
            "status": job["status"],
            "rows": rows,
            "reprobed": evidence_fetch_times(item_payload(path)).get("chapters", "") > before.get("chapters", ""),
            "per_server": rows[0]["servers"] if rows else {},
            "served": served,
            "equals_chapters": same_markers(served, truth_everywhere(episode)),
        }
        steps.append(entry)
        say(f"  {step}: {entry['per_server']} equals chapters {entry['equals_chapters']}")
        return entry

    def rescan_step(step: str, episode: int) -> dict:
        refresh_all_servers()
        served = served_everywhere(synth_path(episode))
        entry = {"step": step, "served": served, "equals_chapters": same_markers(served, truth_everywhere(episode))}
        steps.append(entry)
        say(f"  {step}: equals chapters {entry['equals_chapters']}")
        return entry

    def all_written(entry: dict) -> bool:
        return set(entry["per_server"].values()) == {"markers_written"} and all(entry["equals_chapters"].values())

    if os.environ.get("REEVALUATE") and (RESULTS / "row-07.json").exists():
        steps = json.loads((RESULTS / "row-07.json").read_text())["steps"]
        a1, a2, a3, b1, b2, c1, c2 = steps
    else:
        touch(2)
        a1 = job_step("A1 job right after touching E02", 2)
        a2 = rescan_step("A2 servers rescanned E02", 2)
        a3 = job_step("A3 normal job after the rescan", 2)
        touch(3)
        b1 = rescan_step("B1 touch E03, servers rescanned before any job", 3)
        b2 = job_step("B2 job after the rescan", 3)
        touch(2)
        c1 = rescan_step("C1 touch E02 again, servers rescanned", 2)
        c2 = job_step("C2 forced job", 2, force=True)

    checks = {
        # Only the mtime changed and every server still shows exactly these markers (read back), so Up to date is right.
        "A1 re-probed; every server shows the chapters (written or up to date)": a1["reprobed"]
        and set(a1["per_server"].values()) <= {"markers_written", "markers_up_to_date"}
        and all(a1["equals_chapters"].values()),
        "A2 the rescan dropped markers somewhere": not all(a2["equals_chapters"].values()),
        "A3 normal job wrote them back (Markers written)": all(
            a3["per_server"][sid] == "markers_written" for sid, same in a2["equals_chapters"].items() if not same
        )
        and all(a3["equals_chapters"].values()),
        "B2 re-probed and wrote all three": b2["reprobed"] and all_written(b2),
        "C2 forced restore reported Markers written": all(
            c2["per_server"][sid] == "markers_written" for sid, same in c1["equals_chapters"].items() if not same
        )
        and all(c2["equals_chapters"].values()),
    }
    notes = [f"{k}: {v}" for k, v in checks.items()] + [
        f"A2 served after rescan {a2['served']}",
        f"A3 rows {a3['per_server']}; B1 equals chapters {b1['equals_chapters']}; C1 {c1['equals_chapters']}; C2 rows {c2['per_server']}",
    ]
    return write_result(
        7,
        "File change re-probes and servers end up with our markers",
        "pass" if all(checks.values()) else "fail",
        {"checks": checks, "steps": steps},
        notes,
    )


def add_version(original: str, new: str, source: Path) -> str:
    """Copy ``source`` next to ``original`` as ``new`` (once), rescan, wait for Plex to put both in one item."""
    target = SYNTH_HOST_SEASON / new.rsplit("/", 1)[-1]
    if not target.exists():
        shutil.copyfile(source, target)
    refresh_all_servers()
    wait_until(
        f"Plex to add {new.rsplit('/', 1)[-1]} to the item of {original.rsplit('/', 1)[-1]}",
        lambda: new in plex_parts() and plex_parts()[new]["item"] == plex_parts()[original]["item"],
        timeout=300,
        every=5,
    )
    return plex_parts()[original]["item"]


def server_rows(job_id: str) -> list[dict]:
    return [
        {
            "file": f["file"].rsplit("/", 1)[-1],
            "outcome": f["outcome"],
            "reason": f["reason"],
            "servers": {
                s["id"]: s["status"] + (f" ({s['reason_code']})" if s.get("reason_code") else "") for s in f["servers"]
            },
            "messages": {s["id"]: s["message"] for s in f["servers"] if s.get("message")},
        }
        for f in job_files(job_id)
    ]


def part_markers(state: dict) -> dict[str, dict]:
    return {
        p.rsplit("/", 1)[-1]: {k: v for k, v in json.loads(x).items() if k in ("pv:intros", "pv:credits")}
        for p, x in state["parts"].items()
    }


def _served_is_truth(served: list[dict], episode: int) -> bool:
    t = SYNTH_TRUTH[episode]
    return sorted((m["type"], m["start"], m["end"]) for m in served) == [("credits", t[2], t[3]), ("intro", t[0], t[1])]


def _version_case(label: str, original: str, second: str, source: Path, episode: int, *, force_first: bool) -> dict:
    """Add a second version, run the original alone (second undecided), then the second; record every state."""
    item = add_version(original, second, source)
    steps = [
        {
            "step": f"{label}0 second version added",
            "plex": plex_item_state([original]),
            "jellyfin_items": jf_items_for([original, second]),
        }
    ]
    body = {"file_paths": [original], "force": force_first, "library_name": f"Phase 1 row 8 {label} original"}
    job = wait_job(start_markers_job(body)["id"])
    first = {
        "step": f"{label}1 job on the original, second version undecided",
        "job": job["id"],
        "rows": server_rows(job["id"]),
        "plex": plex_item_state([original]),
    }
    steps.append(first)
    job = wait_job(start_markers_job({"file_paths": [second], "library_name": f"Phase 1 row 8 {label} second"})["id"])
    both = {
        "step": f"{label}2 job on the second version",
        "job": job["id"],
        "rows": server_rows(job["id"]),
        "plex": plex_item_state([original]),
        "jellyfin": jf_state([original, second]),
        "jellyfin_items": jf_items_for([original, second]),
    }
    steps.append(both)
    parts = part_markers(both["plex"])
    checks = {
        f"{label}1 Plex waits (row + file outcome) and hides the item's markers": first["rows"][0]["servers"]
        .get("mlab-plex", "")
        .startswith("markers_waiting")
        and first["rows"][0]["outcome"] == "markers_waiting"
        and not first["plex"]["served"][item],
        f"{label}2 both parts carry the same pv:intros and pv:credits": len(parts) == 2
        and all(v == next(iter(parts.values())) and "pv:intros" in v and "pv:credits" in v for v in parts.values()),
        f"{label}2 Plex serves the chapters": _served_is_truth(both["plex"]["served"][item], episode),
        f"{label}2 Jellyfin 12.0 written for the second version": both["rows"][0]["servers"].get("mlab-jf12")
        == "markers_written",
    }
    return {"item": item, "steps": steps, "checks": checks}


@row(8)
def row_08_multi_version() -> dict:
    """Multi-version items (spec §13 item 6).

    A: S01E01 + "Extended" (+10 s tail; durations differ by more than 2 s, credits still in the last 25%).
    B: S01E03 + an identical "Copy", starting with a forced job on the original.
    Each: the original alone makes Plex wait and hide the item's markers; deciding the second version writes the shared
    set to both parts. Jellyfin 12.0 lists both second files as alternate versions (row 19 checks their segments).
    """
    episode_b = int(os.environ.get("ROW8_EPISODE", "3"))
    extended = synth_path(1, " - Extended")
    part_a = _version_case(
        "A", synth_path(1), extended, LAB / "synth" / "_staging" / extended.rsplit("/", 1)[-1], 1, force_first=False
    )
    part_b = _version_case(
        "B",
        synth_path(episode_b),
        synth_path(episode_b, " - Copy"),
        SYNTH_HOST_SEASON / synth_path(episode_b).rsplit("/", 1)[-1],
        episode_b,
        force_first=True,
    )
    checks = {**part_a["checks"], **part_b["checks"]}
    notes = [f"{k}: {v}" for k, v in checks.items()]
    for label, part in (("A", part_a), ("B", part_b)):
        first, both = part["steps"][1], part["steps"][2]
        notes.append(f"{label}1 rows {first['rows']}; Plex served {first['plex']['served'][part['item']]}")
        notes.append(
            f"{label}2 rows {both['rows']}; Plex served {both['plex']['served'][part['item']]}; "
            f"Jellyfin items {both['jellyfin_items']}"
        )
    return write_result(
        8,
        "Multi-version items",
        "pass" if all(checks.values()) else "fail",
        {"checks": checks, "part_a": part_a, "part_b": part_b},
        notes,
    )


def jf_items_for(paths: list[str]) -> dict:
    out = {}
    for sid in JELLYFINS:
        items = jf_items(sid)
        out[sid] = {p.rsplit("/", 1)[-1]: items.get(p) for p in paths}
    return out


def sonarr_payload(episode: int) -> dict:
    path = synth_path(episode)
    return {
        "eventType": "Download",
        "series": {"title": "Synth Chapters", "path": SYNTH_SHOW},
        "episodes": [{"seasonNumber": 1, "episodeNumber": episode, "title": f"Synth {episode}"}],
        "episodeFile": {"path": path, "relativePath": f"Season 01/{path.rsplit('/', 1)[-1]}"},
    }


def send_webhook_now(episode: int) -> str:
    """POST a Sonarr download for a synth episode and fire its debounce batch at once; returns the send time."""
    t0 = now_iso()
    status, body = http(
        "POST",
        f"{APP}/api/webhooks/sonarr",
        headers={"X-Auth-Token": ENV["MLAB_APP_TOKEN"]},
        body=sonarr_payload(episode),
    )
    if status >= 300:
        raise RuntimeError(f"webhook -> {status}: {body}")
    pending = app_ok("GET", "/api/webhooks/pending")["pending"]
    for batch in pending:
        app_ok("POST", f"/api/webhooks/pending/{urllib.parse.quote(batch['key'], safe='')}/fire-now")
    return t0


def jobs_since(t0: str) -> list[dict]:
    jobs = app_ok("GET", "/api/jobs?page=0&include_retry_attempts=1")["jobs"]
    return [j for j in jobs if j["created_at"] >= t0]


@row(9)
def row_09_webhook_follow_up() -> dict:
    """Sonarr webhook for a synth episode -> preview job HIGH, then Intro & Credits job NORMAL with follows_job_id;
    the dashboard lists the follow-up under it."""
    t0 = send_webhook_now(2)
    found = wait_until(
        "the preview job and its follow-up",
        lambda: (
            lambda js: (
                js
                if any(j.get("kind") == "intro_credits" for j in js)
                and any(j.get("kind") != "intro_credits" for j in js)
                else None
            )
        )(jobs_since(t0)),
        timeout=120,
    )
    preview = next(j for j in found if j.get("kind") != "intro_credits")
    follow = next(j for j in found if j.get("kind") == "intro_credits")
    shot = dashboard_check(preview["id"], follow["id"])
    preview = wait_job(preview["id"], timeout=900)
    follow = wait_job(follow["id"], timeout=900)
    files = job_files(follow["id"])
    linked = follow["config"].get("follows_job_id") == preview["id"]
    order_ok = (follow.get("started_at") or "") >= (preview.get("completed_at") or "~")
    passed = (
        preview["priority"] == 1
        and follow["priority"] == 2
        and linked
        and order_ok
        and follow["status"] == "completed"
        and shot["follow_up_row_after_preview"]
    )
    notes = [
        f"preview job {preview['id'][:8]} priority {preview['priority']} {preview['status']} ({preview['started_at']} -> {preview['completed_at']})",
        f"Intro & Credits job {follow['id'][:8]} priority {follow['priority']} {follow['status']} follows {str(follow['config'].get('follows_job_id'))[:8]} (started {follow['started_at']})",
        f"follow-up files: {[(f['outcome'], [(s['id'], s['status']) for s in f['servers']]) for f in files]}",
        f"dashboard: {shot}",
    ]
    return write_result(
        9,
        "Webhook: preview job then linked Intro & Credits job",
        "pass" if passed else "fail",
        {"preview": preview, "follow_up": follow, "follow_up_files": files, "dashboard": shot},
        notes,
    )


def dashboard_check(preview_id: str, follow_id: str) -> dict:
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot = SHOTS / "row09-dashboard.png"
    script = f"""
import asyncio, json, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={{"width": 1500, "height": 1000}})
        await pg.goto("{APP}/login"); await pg.fill("#token", sys.stdin.read().strip()); await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(3000); await pg.goto("{APP}/"); await pg.wait_for_timeout(6000)
        ids = await pg.evaluate("() => Array.from(document.querySelectorAll('tr.job-row')).map(r => [r.id, r.className, (r.querySelector('.job-follows')||{{}}).textContent || ''])")
        await pg.screenshot(path="{shot}", full_page=True); await b.close()
        print(json.dumps(ids))
asyncio.run(main())
"""
    out = subprocess.run(
        [VENV_PYTHON, "-c", script], input=ENV["MLAB_APP_TOKEN"], capture_output=True, text=True, timeout=120
    )
    rows = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else []
    ids = [r[0] for r in rows]
    p, f = f"job-row-{preview_id}", f"job-row-{follow_id}"
    after = p in ids and f in ids and ids.index(f) == ids.index(p) + 1
    follow_row = next((r for r in rows if r[0] == f), None)
    return {
        "screenshot": str(shot),
        "follow_up_row_after_preview": after and bool(follow_row and "job-row-follow-up" in follow_row[1]),
        "follow_row": follow_row,
        "error": scrub(out.stderr[-500:]) if out.returncode else "",
    }


@row(10)
def row_10_per_job_pause() -> dict:
    """Pause a running Intro & Credits backfill -> a previews webhook job still runs -> resume.

    Forces a re-detect of South Park S01 (the online lookups make each file take a while) and pauses once at least one
    file is done.
    """
    previous = RESULTS / "row-10.json"
    earlier = json.loads(previous.read_text()) if previous.exists() else None
    if os.environ.get("ROW10_JOB"):
        # Continue a job an earlier attempt already paused (that attempt's webhook was deduplicated: the app drops a
        # repeat of the same source + file for 600 s).
        job = app_ok("GET", f"/api/jobs/{os.environ['ROW10_JOB']}")
        paused = at_pause = job
    else:
        job = start_markers_job(
            {"file_paths": [SOUTH_PARK_SEASON], "force": True, "library_name": "Phase 1 row 10 pause (forced)"}
        )

        def mid_run() -> bool:
            current = app_ok("GET", f"/api/jobs/{job['id']}")
            return current["status"] == "running" and current["progress"]["processed_items"] >= 1

        wait_until("the backfill to finish a file", mid_run, every=0.2, timeout=300)
        paused = app_ok("POST", f"/api/jobs/{job['id']}/pause")
        at_pause = app_ok("GET", f"/api/jobs/{job['id']}")
    time.sleep(5)
    settled = app_ok("GET", f"/api/jobs/{job['id']}")
    t0 = send_webhook_now(int(os.environ.get("ROW10_WEBHOOK_EPISODE", "1")))
    found = wait_until(
        "the webhook preview job",
        lambda: [j for j in jobs_since(t0) if j.get("kind") != "intro_credits"],
        timeout=120,
    )
    preview = wait_job(found[0]["id"], timeout=900)
    during = app_ok("GET", f"/api/jobs/{job['id']}")
    followers = [j for j in jobs_since(t0) if j.get("kind") == "intro_credits"]
    resumed = app_ok("POST", f"/api/jobs/{job['id']}/resume")
    final = wait_job(job["id"], timeout=1800)
    followers = [wait_job(j["id"], timeout=900) for j in followers]
    processed = lambda j: j["progress"]["processed_items"]  # noqa: E731
    held = (
        settled["paused"]
        and during["paused"]
        and processed(during) == processed(settled)
        and during["status"] == "running"
    )
    passed = held and preview["status"] == "completed" and final["status"] == "completed" and not resumed.get("paused")
    notes = [
        f"paused at {processed(at_pause)}/{at_pause['progress']['total_items']} (pause response paused={paused.get('paused')})",
        f"5 s later {processed(settled)}; while the preview job ran {processed(during)}, status {during['status']}, paused={during['paused']}",
        f"preview job {preview['id'][:8]} {preview['status']} priority {preview['priority']} ({preview['started_at']} -> {preview['completed_at']})",
        f"follow-up Intro & Credits jobs: {[(j['id'][:8], j['status'], j['started_at']) for j in followers]}",
        f"after resume: {final['status']} {final['progress']['processed_items']}/{final['progress']['total_items']}; outcomes { {k: v for k, v in (final['progress'].get('outcome') or {}).items() if v} }",
    ]
    return write_result(
        10,
        "Per-job pause",
        "pass" if passed else "fail",
        {
            "job": final,
            "at_pause": at_pause,
            "settled": settled,
            "during_preview": during,
            "preview": preview,
            "followers": followers,
            "earlier_run": {k: earlier.get(k) for k in ("at", "result", "notes")} if earlier else None,
        },
        notes,
    )


@row(11)
def row_11_network_share() -> dict:
    """Plex DB on a network share is not reproducible in the lab without root: covered by unit tests only."""
    repo = LAB.parents[4]
    out = subprocess.run(
        [
            VENV_PYTHON,
            "-m",
            "pytest",
            "--no-cov",
            "-n",
            "0",
            "-q",
            "tests/markers/test_fs.py",
            "tests/markers/test_plex_db_publisher.py",
            "-k",
            "network or mount or local_db or lock_holder",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=600,
    )
    tail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[-300:]
    return write_result(
        11,
        "Plex DB on a network share",
        "not lab-tested",
        {"pytest_exit": out.returncode, "pytest_tail": tail},
        [
            f"unit tests (tests/markers/test_fs.py + test_plex_db_publisher.py -k network/mount/local_db/lock_holder): {tail}"
        ],
    )


@row(12)
def row_12_plex_client() -> dict:
    return write_result(
        12,
        "Plex client shows Skip Intro (spec §13 item 3)",
        "pending owner",
        {
            "ask": "Open Synth Chapters S01E02 or S01E03 on the lab Plex in any Plex app, play from 0:05, confirm Skip Intro at 0:17 (E02) / 0:25 (E03) and Skip Credits at 1:40 (E01 has intro only since row 8)."
        },
    )


def job_logs(job_id: str) -> list[str]:
    status, data = app("GET", f"/api/jobs/{job_id}/logs")
    if status >= 300 or not data:
        return []
    lines = data.get("logs", data) if isinstance(data, dict) else data
    return [str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in lines]


def plex_view(path: str) -> dict:
    """What the Inspector says about one file on Plex: plan, reason, current and published markers."""
    for srv in item_payload(path)["servers"]:
        if srv["server_id"] == "mlab-plex":
            return {k: srv.get(k) for k in ("plan", "plan_reason", "current", "published", "version_count")}
    return {}


def set_redetect(mode: str) -> dict:
    app_ok("PUT", "/api/servers/mlab-plex", {"markers": {"plex": {"on_plex_redetect": mode}}})
    stored = app_ok("GET", "/api/markers/servers/mlab-plex/status")["settings"]
    say(f"  mlab-plex on_plex_redetect -> {mode}; stored settings {stored}")
    return stored


def credits_of(served: list[dict]) -> list[tuple[int, int]]:
    return [(m["start"], m["end"]) for m in served if m["type"] == "credits"]


@row(16)
def row_16_keep_plex() -> dict:
    """Plex's forced credits detection replaces ours: with restore the next normal job writes ours back ("written");
    with keep_plex, Plex's credits stay ("Keeping Plex's credits") and later runs (forced included) don't touch them."""
    rick = library_files(RICK_SEASON)[0]
    item = plex_parts()[rick]["item"]
    steps = []

    def plex_redetect_credits(step: str) -> dict:
        plex("PUT", f"/library/metadata/{item}/credits", force=1)
        plex_wait_idle(min_wait=10, timeout=480)
        entry = {
            "step": step,
            "served": plex_served(item),
            "rows": [r for r in plex_marker_rows() if r["item"] == item],
        }
        steps.append(entry)
        say(f"  {step}: credits {credits_of(entry['served'])}")
        return entry

    def job(step: str, *, force: bool = False) -> dict:
        body = {"file_paths": [rick], "force": force, "library_name": f"Phase 1 row 16 {step}"}
        done = wait_job(start_markers_job(body)["id"])
        entry = {
            "step": step,
            "job": done["id"],
            "rows": server_rows(done["id"]),
            "logs": [line for line in job_logs(done["id"]) if "eeping" in line or "Plex" in line][-10:],
            "served": plex_served(item),
            "credit_row_ids": [r["id"] for r in plex_marker_rows() if r["item"] == item and r["text"] == "credits"],
            "inspector": plex_view(rick),
        }
        steps.append(entry)
        say(
            f"  {step}: {entry['rows'][0]['servers'].get('mlab-plex')}; credits {credits_of(entry['served'])}; "
            f"inspector {entry['inspector'].get('plan')} {entry['inspector'].get('plan_reason')!r}"
        )
        return entry

    set_redetect("restore")
    ours = credits_of(plex_served(item))
    replaced = plex_redetect_credits("R1 forced credits detection (restore mode)")
    restored = job("R2 normal job (restore)")
    set_redetect("keep_plex")
    replaced2 = plex_redetect_credits("K1 forced credits detection (keep_plex mode)")
    kept = job("K2 normal job (keep_plex)")
    kept_again = job("K3 second normal job (keep_plex)")
    kept_forced = job("K4 forced job (keep_plex)", force=True)
    set_redetect("restore")
    back = job("B1 normal job after switching back to restore")

    plex_credits = credits_of(replaced2["served"])
    checks = {
        "plex replaced ours (restore mode)": credits_of(replaced["served"]) != ours,
        "restore: job wrote ours back": restored["rows"][0]["servers"].get("mlab-plex") == "markers_written"
        and credits_of(restored["served"]) == ours,
        "plex replaced ours (keep_plex mode)": plex_credits != ours,
        "keep_plex: Plex's credits kept": credits_of(kept["served"]) == plex_credits,
        "keep_plex: says Keeping Plex's credits": "Keeping Plex's credits".lower()
        in (kept["inspector"].get("plan_reason") or "").lower(),
        "keep_plex: job Files panel says Keeping Plex's credits": "keeping plex's credits"
        in kept["rows"][0]["messages"].get("mlab-plex", "").lower(),
        "keep_plex: later runs don't touch them": credits_of(kept_again["served"]) == plex_credits
        and credits_of(kept_forced["served"]) == plex_credits
        and kept["credit_row_ids"] == kept_again["credit_row_ids"] == kept_forced["credit_row_ids"],
    }
    notes = [f"{k}: {v}" for k, v in checks.items()] + [
        f"ours {ours}; Plex's after K1 {plex_credits}",
        f"K2 Plex row {kept['rows'][0]['servers'].get('mlab-plex')} {kept['rows'][0]['messages'].get('mlab-plex')!r}, "
        f"Inspector plan {kept['inspector'].get('plan')} "
        f"{kept['inspector'].get('plan_reason')!r}; K2 log lines {kept['logs']}",
        f"K3 {kept_again['rows'][0]['servers'].get('mlab-plex')}; K4 (forced) {kept_forced['rows'][0]['servers'].get('mlab-plex')}",
        f"B1 back to restore: {back['rows'][0]['servers'].get('mlab-plex')}, credits {credits_of(back['served'])}",
    ]
    return write_result(
        16,
        "Plex re-detection: restore vs keep_plex",
        "pass" if all(checks.values()) else "fail",
        {"item": item, "checks": checks, "steps": steps},
        notes,
    )


ROW17_STATE = RESULTS / "row-17-state.json"


def jellyfin_refresh_only() -> None:
    for sid in JELLYFINS:
        t0 = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        jf(sid, "POST", "/Library/Refresh")
        jf_wait_task_idle(sid, "RefreshLibrary", started_after=t0)


def dashboard_row_text(job_id: str) -> dict:
    SHOTS.mkdir(parents=True, exist_ok=True)
    shot = SHOTS / f"row17-dashboard-{job_id[:8]}.png"
    script = f"""
import asyncio, json, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={{"width": 1500, "height": 1000}})
        await pg.goto("{APP}/login"); await pg.fill("#token", sys.stdin.read().strip()); await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(3000); await pg.goto("{APP}/"); await pg.wait_for_timeout(6000)
        text = await pg.evaluate("() => {{ const r = document.getElementById('job-row-{job_id}'); return r ? r.innerText : '' }}")
        await pg.screenshot(path="{shot}", full_page=True); await b.close()
        print(json.dumps(text))
asyncio.run(main())
"""
    out = subprocess.run(
        [VENV_PYTHON, "-c", script], input=ENV["MLAB_APP_TOKEN"], capture_output=True, text=True, timeout=120
    )
    text = json.loads(out.stdout.strip().splitlines()[-1]) if out.stdout.strip() else ""
    return {
        "row_text": " ".join(text.split()),
        "screenshot": str(shot),
        "error": scrub(out.stderr[-300:]) if out.returncode else "",
    }


@row(17)
def row_17_verify_job() -> dict:
    """A replaced file sent by a webhook: our job publishes, both Jellyfins rescan it and drop the segments, and the
    delayed verify job ("Checking again in …") puts them back.

    Runs in two calls: the first sets it up and stops (the verify job waits at least 600 s); run the row again to
    finish.
    """
    episode = int(os.environ.get("ROW17_EPISODE", "2"))
    path = synth_path(episode)
    if not ROW17_STATE.exists():
        os.utime(SYNTH_HOST_SEASON / path.rsplit("/", 1)[-1], None)
        t0 = send_webhook_now(episode)
        history = app_ok("GET", "/api/webhooks/history")["events"][:2]
        found = wait_until(
            "the preview job and its follow-up",
            lambda: (
                lambda js: (
                    js
                    if any(j.get("kind") == "intro_credits" for j in js)
                    and any(j.get("kind") != "intro_credits" for j in js)
                    else None
                )
            )(jobs_since(t0)),
            timeout=120,
        )
        preview = wait_job(next(j for j in found if j.get("kind") != "intro_credits")["id"], timeout=600)
        follow = wait_job(next(j for j in found if j.get("kind") == "intro_credits")["id"], timeout=600)
        served_after_job = {sid: jf_state([path])[sid][path] for sid in JELLYFINS}
        verify = wait_until(
            "the verify job",
            lambda: [j for j in jobs_since(t0) if (j.get("config") or {}).get("verify")],
            timeout=60,
        )[0]
        jellyfin_refresh_only()
        served_after_rescan = {sid: jf_state([path])[sid][path] for sid in JELLYFINS}
        pending = app_ok("GET", f"/api/jobs/{verify['id']}")
        state = {
            "t0": t0,
            "episode": episode,
            "webhook_history": history,
            "preview": {k: preview[k] for k in ("id", "status", "priority")},
            "follow_up": {
                "id": follow["id"],
                "status": follow["status"],
                "rows": server_rows(follow["id"]),
                "logs": [line for line in job_logs(follow["id"]) if "checked again" in line or "replaced" in line],
            },
            "served_after_job": served_after_job,
            "verify": {
                "id": verify["id"],
                "name": verify["library_name"],
                "config": verify.get("config"),
                "progress_while_waiting": pending["progress"].get("current_item"),
                "status": pending["status"],
            },
            "dashboard_while_waiting": dashboard_row_text(verify["id"]),
            "served_after_rescan": served_after_rescan,
        }
        RESULTS.mkdir(exist_ok=True)
        ROW17_STATE.write_text(json.dumps(scrub(state), indent=2, default=str) + "\n")
        say(
            f"row 17 set up: verify job {verify['id'][:8]} due {verify.get('config', {}).get('retry_not_before')}; "
            f"after rescan {served_after_rescan}; dashboard {state['dashboard_while_waiting']['row_text']!r}"
        )
        return {"result": "pending"}

    state = json.loads(ROW17_STATE.read_text())
    episode = state["episode"]
    path = synth_path(episode)
    verify = wait_job(state["verify"]["id"], timeout=560)
    served = {sid: jf_state([path])[sid][path] for sid in JELLYFINS}
    truth = truth_everywhere(episode)
    restored = {sid: same_markers({sid: served[sid]}, {sid: truth[sid]})[sid] for sid in JELLYFINS}
    rows = server_rows(verify["id"])
    dropped = {
        sid: not same_markers({sid: state["served_after_rescan"][sid]}, {sid: truth[sid]})[sid] for sid in JELLYFINS
    }
    written = {sid: rows[0]["servers"].get(sid) == "markers_written" for sid in JELLYFINS} if rows else {}
    checks = {
        "follow-up job wrote the replaced file": all(
            v in ("markers_written", "markers_up_to_date") for v in state["follow_up"]["rows"][0]["servers"].values()
        ),
        "verify job queued (verify: true, named Verify: …)": bool(state["verify"]["config"].get("verify"))
        and state["verify"]["name"].startswith("Verify:"),
        "dashboard shows Checking again in …": "checking again in"
        in state["dashboard_while_waiting"]["row_text"].lower(),
        "both Jellyfins dropped the segments on rescan": all(dropped.values()),
        "verify job restored them (written)": all(restored.values()) and all(written.values()),
    }
    notes = [f"{k}: {v}" for k, v in checks.items()] + [
        f"follow-up {state['follow_up']['id'][:8]} rows {state['follow_up']['rows']}; logs {state['follow_up']['logs']}",
        f"verify {verify['id'][:8]} {verify['status']} started {verify['started_at']} (queued as {state['verify']['name']!r}, "
        f"waiting text {state['verify']['progress_while_waiting']!r}); rows {rows}",
        f"dashboard while waiting: {state['dashboard_while_waiting']['row_text']!r}",
        f"after rescan {state['served_after_rescan']}; after verify {served}",
    ]
    ROW17_STATE.unlink()
    return write_result(
        17,
        "Delayed verify job after a replaced file",
        "pass" if all(checks.values()) else "fail",
        {"checks": checks, "setup": state, "verify_job": verify, "verify_rows": rows, "served_after_verify": served},
        notes,
    )


@row(18)
def row_18_extras() -> dict:
    """A folder job with a -trailer file: the trailer is skipped ("Extras aren't checked for markers"), no retry."""
    folder = f"{MOVIES}/Toy Story (1995)"
    t0 = now_iso()
    job = wait_job(start_markers_job({"file_paths": [folder], "library_name": "Phase 1 row 18 extras"})["id"])
    files = job_files(job["id"])
    time.sleep(10)
    later = [
        j
        for j in app_ok("GET", "/api/jobs?page=0&include_retry_attempts=1")["jobs"]
        if j["created_at"] >= t0 and j["id"] != job["id"]
    ]
    trailers = [f for f in files if "-trailer" in f["file"]]
    checks = {
        "a trailer was in the folder": bool(trailers),
        "trailer skipped with the extras reason": all(
            f["outcome"] == "markers_skipped" and "Extras aren't checked for markers" in f["reason"] for f in trailers
        ),
        "no retry job": not any(j["library_name"].startswith("Retry:") for j in later),
    }
    notes = [f"{k}: {v}" for k, v in checks.items()] + [
        f"files {[(f['file'].rsplit('/', 1)[-1], f['outcome'], f['reason']) for f in files]}",
        f"jobs created since: {[(j['id'][:8], j['library_name'], j['status']) for j in later]}",
    ]
    return write_result(
        18,
        "Extras are skipped",
        "pass" if all(checks.values()) else "fail",
        {"job": job["id"], "files": files, "later_jobs": later, "checks": checks},
        notes,
    )


def play_and_find_skip(base: str, item_id: str, shot: Path, seek: float = 22) -> dict:
    """Sign in to jellyfin-web as lab/lab, play ``item_id``, seek into the intro, look for Skip Intro."""
    script = f"""
import asyncio, json
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        pg = await b.new_page(viewport={{"width": 1280, "height": 720}})
        await pg.goto("{base}/web/#/login.html"); await pg.wait_for_timeout(3000)
        await pg.fill("#txtManualName", "lab"); await pg.fill("#txtManualPassword", "lab")
        await pg.click("button[type=submit]"); await pg.wait_for_timeout(4000)
        await pg.goto("{base}/web/#/details?id={item_id}"); await pg.wait_for_timeout(4000)
        await pg.click("button.btnPlay, .detailButton.btnPlay, button[data-action=resume], button[title=Play]", timeout=15000)
        await pg.wait_for_timeout(12000)
        await pg.evaluate("() => {{ const v=document.querySelector('video'); if (v) v.currentTime = {seek}; }}")
        found = False; txt = ""
        for i in range(20):
            await pg.wait_for_timeout(1000)
            txt = await pg.evaluate("() => Array.from(document.querySelectorAll('button')).map(b=>b.innerText.trim()).filter(Boolean).join(' | ')")
            if "Skip Intro" in txt: found = True; break
        await pg.screenshot(path="{shot}")
        info = await pg.evaluate("() => {{ const v=document.querySelector('video'); return v ? [v.currentTime, v.duration, (v.currentSrc.split('?')[0]) + ' mediaSourceId=' + (new URL(v.currentSrc).searchParams.get('mediaSourceId') || '')] : null }}")
        await b.close()
        print(json.dumps({{"found": found, "video": info, "buttons": txt[-200:]}}))
asyncio.run(main())
"""
    out = subprocess.run([VENV_PYTHON, "-c", script], capture_output=True, text=True, timeout=240)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"found": False, "error": scrub((out.stdout + out.stderr)[-600:])}


@row(19)
def row_19_jf12_alternate_versions() -> dict:
    """Jellyfin 12.0 alternate versions (row 8's second files) get markers; the web player shows Skip Intro on one."""
    SHOTS.mkdir(parents=True, exist_ok=True)
    episode_b = int(os.environ.get("ROW8_EPISODE", "3"))
    pairs = {1: synth_path(1, " - Extended"), episode_b: synth_path(episode_b, " - Copy")}
    items = jf_items("mlab-jf12")
    checks, detail = {}, {}
    for episode, alt in pairs.items():
        item = items.get(alt)
        segs = jf_segments("mlab-jf12", item["id"]) if item else None
        truth = truth_everywhere(episode)["mlab-jf12"]
        is_alternate = bool(item) and item["id"] != item["item_id"]
        ok = same_markers({"mlab-jf12": segs}, {"mlab-jf12": truth})["mlab-jf12"]
        detail[alt.rsplit("/", 1)[-1]] = {"item": item, "alternate_of_primary": is_alternate, "segments": segs}
        checks[f"{alt.rsplit('/', 1)[-1]} is an alternate version with our segments"] = is_alternate and ok
    extended = items.get(pairs[1])
    play = (
        play_and_find_skip(JELLYFINS["mlab-jf12"], extended["id"], SHOTS / "row19-jf12-extended.png")
        if extended
        else {}
    )
    duration = (play.get("video") or [0, 0])[1] or 0
    checks["web player: Skip Intro on the Extended alternate (130 s)"] = (
        bool(play.get("found")) and abs(duration - 130) < 2
    )
    notes = [f"{k}: {v}" for k, v in checks.items()] + [f"segments {detail}", f"player {play}"]
    return write_result(
        19,
        "Jellyfin 12.0 alternate versions",
        "pass" if all(checks.values()) else "fail",
        {"checks": checks, "detail": detail, "player": play},
        notes,
    )


@row(15)
def row_15_resources() -> dict:
    """Peak CPU/RSS of mlab-app during the row 1 backfill; TheIntroDB requests per 10 s <= 30."""
    data = json.loads((RESULTS / "row-01.json").read_text())
    samples = data["resources"]["samples"]
    cpu = [float(s["cpu"].rstrip("%")) for s in samples]
    mem = [_mib(s["mem"]) for s in samples]
    stamps = sorted(
        datetime.strptime(x["t"], "%Y/%m/%d %H:%M:%S")
        for x in data["resources"]["online_lookups"]
        if x["source"] == "TheIntroDB"
    )
    peak_window = 0
    for i, start in enumerate(stamps):
        peak_window = max(peak_window, sum(1 for t in stamps[i:] if (t - start).total_seconds() < 10))
    passed = bool(samples) and peak_window <= 30
    notes = [
        f"{len(samples)} docker stats samples; peak CPU {max(cpu, default=0):.1f}%; peak memory {max(mem, default=0):.0f} MiB",
        f"TheIntroDB lookups during the job: {len(stamps)}; most in any 10 s window: {peak_window}",
    ]
    return write_result(
        15,
        "Resource use during the Rick and Morty backfill",
        "pass" if passed else "fail",
        {
            "peak_cpu_percent": max(cpu, default=None),
            "peak_mem_mib": max(mem, default=None),
            "theintrodb_lookups": len(stamps),
            "theintrodb_peak_per_10s": peak_window,
            "job_window": data["resources"]["window"],
        },
        notes,
    )


@row(14)
def row_14_security() -> dict:
    """Every /api/markers/* route without auth -> 401; path traversal on the item route -> 400."""
    routes = json.loads(
        sh(
            "docker",
            "exec",
            "mlab-app",
            "python3",
            "-c",
            "import json;from flask import Flask;from media_preview_generator.web.routes import api;"
            "a=Flask('x');a.register_blueprint(api);"
            "print(json.dumps([[r.rule,sorted(m for m in r.methods if m not in ('HEAD','OPTIONS'))]"
            " for r in a.url_map.iter_rules() if r.rule.startswith('/api/markers')]))",
        )
        .strip()
        .splitlines()[-1]
    )
    checks = []
    for rule, methods in routes:
        path = rule.replace("<server_id>", "mlab-jellyfin")
        for method in methods:
            body = {} if method == "POST" else None
            code, _ = app(method, path, body, auth=False)
            checks.append({"method": method, "path": path, "status": code, "ok": code == 401})
    traversal = []
    for raw in ("/etc/passwd", "/media/synth-chapters/../../etc/passwd", "../../etc/passwd"):
        code, body = app("GET", f"/api/markers/item?{urllib.parse.urlencode({'path': raw})}")
        traversal.append({"path": raw, "status": code, "body": body, "ok": code == 400})
    code, body = app("POST", "/api/markers/item/redetect", {"path": "/etc/passwd"})
    traversal.append({"path": "redetect /etc/passwd", "status": code, "body": body, "ok": code == 400})
    passed = all(c["ok"] for c in checks + traversal)
    return write_result(
        14,
        "Security: /api/markers/* without auth -> 401; path traversal -> 400",
        "pass" if passed else "fail",
        {"routes": routes, "unauthenticated": checks, "traversal": traversal},
    )


# ------------------------------------------------------------------------------------------------------- scale run
# Plan Task 20 Step 4: a full backfill of the real seasons/movies up.sh mounts from `scale_score.py pick`. Each step is
# resumable and returns within ~9 minutes, so it can be called again until it says done. Raw data: results/scale/.

SCALE = RESULTS / "scale"
SCALE_STEP_S = 540
PROD_PLEX_DB = "/config/plex/Library/Application Support/Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db"
LAB_PLEX_QUIET_PREFS = {
    "GenerateIntroMarkerBehavior": "never",
    "GenerateCreditsMarkerBehavior": "never",
    "GenerateChapterThumbBehavior": "never",
    "LoudnessAnalysisBehavior": "never",
    "MusicAnalysisBehavior": "never",
    "ButlerTaskDeepMediaAnalysis": "0",
    "ButlerTaskUpgradeMediaAnalysis": "0",
}
WATCHED = ("mlab-app", "mlab-plex", "mlab-jellyfin", "mlab-jf12")


def scale_state(name: str) -> dict:
    path = SCALE / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def scale_save(name: str, data: Any) -> None:
    SCALE.mkdir(parents=True, exist_ok=True)
    (SCALE / f"{name}.json").write_text(json.dumps(scrub(data), indent=1, default=str, ensure_ascii=False) + "\n")


def prod_dump() -> None:
    """Prod Plex's own intro/credits rows and every movie/episode part, read-only over ssh (never written)."""
    queries = {
        "prod_plex_markers": (
            "select p.file, t.metadata_item_id, t.text, t.time_offset, t.end_time_offset, t.extra_data, m.duration "
            "from taggings t join tags g on g.id=t.tag_id join media_items m on m.metadata_item_id=t.metadata_item_id "
            "join media_parts p on p.media_item_id=m.id where g.tag_type=12 and t.text in ('intro','credits') "
            "order by p.file, t.text, t.time_offset",
            ("file", "item", "type", "start", "end", "extra", "duration"),
        ),
        "prod_plex_parts": (
            "select p.file, mi.id, mi.metadata_type, m.duration, mi.library_section_id from media_parts p "
            "join media_items m on m.id=p.media_item_id join metadata_items mi on mi.id=m.metadata_item_id "
            "where mi.metadata_type in (1,4) and m.deleted_at is null",
            ("file", "item", "mtype", "duration", "section"),
        ),
    }
    for name, (sql, keys) in queries.items():
        remote = f"nice -n 19 sqlite3 -separator '\t' 'file:{PROD_PLEX_DB}?mode=ro' \"{sql}\""
        out = sh("ssh", "-o", "BatchMode=yes", "plex", remote, timeout=600)
        rows = []
        for line in out.splitlines():
            values = line.split("\t")
            row = dict(zip(keys, values, strict=False))
            for key in ("item", "start", "end", "duration", "mtype", "section"):
                if key in row:
                    row[key] = int(row[key]) if row[key] else None
            rows.append(row)
        scale_save(name, rows)
        say(f"{name}: {len(rows)} rows")


def lab_plex_quiet() -> None:
    """Lab Plex: no intro/credits/chapter-thumbnail/loudness analysis while the scale folders are mounted."""
    _, prefs = plex("GET", "/:/prefs")
    before = {s["id"]: s["value"] for s in prefs["MediaContainer"]["Setting"] if s["id"] in LAB_PLEX_QUIET_PREFS}
    if not (SCALE / "lab_plex_prefs_before.json").exists():
        scale_save("lab_plex_prefs_before", before)
    plex_set_prefs(**LAB_PLEX_QUIET_PREFS)
    say(f"lab Plex prefs before {before} -> {LAB_PLEX_QUIET_PREFS}")


def jf12_movies_library() -> None:
    """Jellyfin 12.0 had no movies library; add one with the 10.11 movies library's options (no trickplay/chapter images)."""
    _, libs = jf("mlab-jf12", "GET", "/Library/VirtualFolders")
    if any(lib["Name"] == "movies" for lib in libs):
        return
    _, reference = jf("mlab-jellyfin", "GET", "/Library/VirtualFolders")
    options = next(lib for lib in reference if lib["Name"] == "movies")["LibraryOptions"]
    query = urllib.parse.urlencode(
        {"name": "movies", "collectionType": "movies", "paths": MOVIES, "refreshLibrary": "false"}
    )
    status, body = jf("mlab-jf12", "POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})
    if status >= 300:
        raise RuntimeError(f"Jellyfin 12.0 movies library -> {status}: {body}")
    say("added the movies library to Jellyfin 12.0")


def expected_lab_files() -> dict[str, str]:
    """Container path -> host path for every video under the scale mounts and the older real-media mounts."""
    return {c: e["host"] for c, e in json.loads((SCALE / "truth.json").read_text()).items()}


def server_counts() -> dict:
    wanted = expected_lab_files()
    plex_paths = set(plex_parts())
    out = {"expected": len(wanted), "mlab-plex": len(wanted.keys() & plex_paths)}
    for sid in JELLYFINS:
        out[sid] = len(wanted.keys() & set(jf_items(sid)))
    return out


def scale_scan(target: str) -> int:
    """Trigger (once) and wait for a library scan on `plex`, `mlab-jellyfin` or `mlab-jf12`; 3 = still running."""
    state = scale_state("scans")
    deadline = time.monotonic() + SCALE_STEP_S
    if target not in state:
        state[target] = {"started": now_iso()}
        if target == "plex":
            _, sections = plex("GET", "/library/sections")
            for s in sections["MediaContainer"]["Directory"]:
                if any(loc["path"] in ("/media/tv", MOVIES) for loc in s["Location"]):
                    plex("GET", f"/library/sections/{s['key']}/refresh")
        else:
            jf(target, "POST", "/Library/Refresh")
        scale_save("scans", state)
        time.sleep(15)
    while time.monotonic() < deadline:
        if target == "plex":
            _, data = plex("GET", "/activities")
            busy = [a for a in data["MediaContainer"].get("Activity", []) if a.get("type") != "butler"]
            detail = [(a.get("title"), a.get("progress")) for a in busy]
        else:
            task = jf_task(target, "RefreshLibrary")
            busy = task["State"] != "Idle"
            detail = (task["State"], task.get("CurrentProgressPercentage"))
        if not busy:
            time.sleep(20)
            counts = server_counts()
            state[target]["done"] = now_iso()
            state[target]["counts"] = counts
            scale_save("scans", state)
            say(f"{target} scan idle; files known per server {counts}")
            return 0
        say(f"{target} scanning: {detail} load {os.getloadavg()[0]:.1f}")
        time.sleep(30)
    return 3


def sample_resources(job_id: str | None) -> dict:
    out = sh("docker", "stats", "--no-stream", "--format", "{{json .}}", *WATCHED, check=False, timeout=60)
    stats = {}
    for line in out.splitlines():
        data = json.loads(line)
        stats[data["Name"]] = {"cpu": data["CPUPerc"], "mem": data["MemUsage"].split(" / ")[0], "pids": data["PIDs"]}
    sample: dict[str, Any] = {"t": now_iso(), "load": os.getloadavg(), "containers": stats}
    if job_id:
        job = app_ok("GET", f"/api/jobs/{job_id}")
        sample["job"] = {
            "status": job["status"],
            "processed": job["progress"].get("processed_items"),
            "total": job["progress"].get("total_items"),
            "current": job["progress"].get("current_item"),
            "paused": job.get("paused"),
        }
    with (SCALE / f"samples-{job_id[:8] if job_id else 'idle'}.jsonl").open("a") as fh:
        fh.write(json.dumps(scrub(sample)) + "\n")
    return sample


def all_job_files(job_id: str) -> list[dict]:
    """Every per-file result of a job (the files route pages at 500)."""
    files, page = [], 1
    while True:
        data = app_ok("GET", f"/api/jobs/{job_id}/files?per_page=500&page={page}")
        files.extend(data["files"])
        if data.get("list_truncated"):
            say(f"job {job_id[:8]} file list truncated: {data.get('truncated_outcomes')}")
        if page * 500 >= data["filtered_count"]:
            return files
        page += 1


def plex_full_state() -> dict:
    """Every Plex marker row id and every part's extra_data (to prove a job made no Plex writes)."""
    return {
        "rows": {r["id"]: [r["item"], r["text"], r["start"], r["end"], r["extra_data"]] for r in plex_marker_rows()},
        "parts": {p: v["extra_data"] for p, v in plex_parts().items()},
    }


def scale_job(name: str, *, force: bool = False) -> int:
    """Run (resumably) an all-libraries Intro & Credits job at the default (low) priority; 3 = still running.

    SCALE_PATHS (a JSON list) runs a folder/file job over those paths instead.
    """
    state = scale_state(name)
    if not state.get("job_id"):
        before = {
            "plex": plex_full_state(),
            "plugin_files": {c: plugin_marker_files(c) for c in JELLYFINS},
            "usage": app_ok("GET", "/api/markers/sources/usage"),
        }
        scale_save(f"{name}-before", before)
        app_ok("PUT", "/api/settings/log-level", {"log_level": "DEBUG"})
        state = {"started": now_iso(), "force": force}
        body: dict[str, Any] = {"library_name": f"Phase 1 scale: {name}", "force": force}
        if os.environ.get("SCALE_PATHS"):
            body["file_paths"] = json.loads(os.environ["SCALE_PATHS"])
        job = start_markers_job(body)
        state["job_id"] = job["id"]
        scale_save(name, state)
    job_id = state["job_id"]
    deadline = time.monotonic() + SCALE_STEP_S
    while time.monotonic() < deadline:
        sample = sample_resources(job_id)
        job = sample["job"]
        say(
            f"{name} {job_id[:8]} {job['status']} {job['processed']}/{job['total']} load {sample['load'][0]:.1f} "
            f"app {sample['containers'].get('mlab-app', {}).get('cpu')} {sample['containers'].get('mlab-app', {}).get('mem')} "
            f"| {str(job['current'])[:70]}"
        )
        if job["status"] in TERMINAL:
            break
        time.sleep(20)
    else:
        return 3
    finished = now_iso()
    app_ok("PUT", "/api/settings/log-level", {"log_level": "INFO"})
    job = app_ok("GET", f"/api/jobs/{job_id}")
    files = all_job_files(job_id)
    later = [
        {k: j.get(k) for k in ("id", "library_name", "status", "created_at", "kind", "priority")}
        for j in app_ok("GET", "/api/jobs?page=0&per_page=200&include_retry_attempts=1")["jobs"]
        if j["created_at"] >= state["started"] and j["id"] != job_id
    ]
    after = {
        "plex": plex_full_state(),
        "plugin_files": {c: plugin_marker_files(c) for c in JELLYFINS},
        "usage": app_ok("GET", "/api/markers/sources/usage"),
    }
    scale_save(f"{name}-after", after)
    state.update(
        {
            "finished": finished,
            "job": job,
            "files": files,
            "other_jobs": later,
            "lookups": online_lookup_times([state["started"], finished]),
            "logs_tail": job_logs(job_id)[-200:],
        }
    )
    scale_save(name, state)
    outcomes = Counter(f["outcome"] for f in files)
    say(f"{name} {job['status']}: {len(files)} files {dict(outcomes)}; other jobs since start: {len(later)}")
    return 0


def scale_collect(name: str = "backfill") -> None:
    """For every file of the job: the Inspector's decisions/evidence, Plex's served markers, both Jellyfins' segments."""
    from concurrent.futures import ThreadPoolExecutor

    files = scale_state(name)["files"]
    parts = plex_parts()
    items = {sid: jf_items(sid) for sid in JELLYFINS}

    def one(f: dict) -> dict:
        path = f["file"]
        entry = {"file": path, "outcome": f["outcome"], "reason": f["reason"], "servers": f.get("servers") or []}
        try:
            payload = item_payload(path)
        except RuntimeError as exc:
            entry["payload_error"] = str(exc)[:300]
            payload = {}
        entry["decisions"] = payload.get("decisions")
        entry["evidence"] = payload.get("evidence")
        entry["duration_ms"] = payload.get("duration_ms")
        entry["is_movie"] = payload.get("is_movie")
        entry["inspector_servers"] = [
            {k: s.get(k) for k in ("server_id", "plan", "plan_reason", "current", "published", "item_status", "error")}
            for s in payload.get("servers") or []
        ]
        part = parts.get(path)
        entry["plex"] = {"item": part["item"], "served": plex_served(part["item"])} if part else None
        entry["jellyfin"] = {
            sid: (
                {"id": items[sid][path]["id"], "segments": jf_segments(sid, items[sid][path]["id"])}
                if path in items[sid]
                else None
            )
            for sid in JELLYFINS
        }
        return entry

    with ThreadPoolExecutor(4) as pool:
        collected = list(pool.map(one, files))
    scale_save(f"collected-{name}", collected)
    say(f"collected {len(collected)} files")


KINDS = {"intro": "Intro", "credits": "Outro"}


def scale_served(name: str = "backfill") -> dict:
    """Served = decided: Plex includeMarkers=1 and both Jellyfins' /MediaSegments against the app's decisions."""
    collected = scale_state(f"collected-{name}")
    baseline = scale_state(f"{name}-before")["plex"]["rows"]
    counts: Counter = Counter()
    mismatches = []
    for entry in collected:
        decisions = entry.get("decisions") or {}
        wanted = {
            t: d["marker"] for t, d in decisions.items() if d["status"] == "decided" and d.get("marker") and t in KINDS
        }
        rows = {s["id"]: s["status"] for s in entry["servers"]}
        # Plex: a written/up-to-date row must serve exactly the decided intro/credits; a type not decided keeps what Plex
        # had before the job (none for the scale folders: lab detection is off).
        if entry.get("plex") and rows.get("mlab-plex"):
            item = entry["plex"]["item"]
            before = [(r[1], r[2], r[3]) for r in baseline.values() if str(r[0]) == str(item)]
            for mtype in KINDS:
                got = sorted((m["start"], m["end"]) for m in entry["plex"]["served"] if m["type"] == mtype)
                if mtype in wanted:
                    expect = [(wanted[mtype]["start_ms"], wanted[mtype]["end_ms"])]
                    ok = got == expect
                    counts[f"plex {mtype} decided {'ok' if ok else 'MISMATCH'} (row {rows.get('mlab-plex')})"] += 1
                else:
                    ok = not got or bool(before)
                    counts[
                        f"plex {mtype} undecided {'nothing served' if not got else 'Plex own kept' if before else 'SERVED WITHOUT DECISION'}"
                    ] += 1
                    expect = "nothing or Plex's own"
                if not ok:
                    mismatches.append(
                        {
                            "file": entry["file"],
                            "server": "mlab-plex",
                            "type": mtype,
                            "served": got,
                            "decided": expect,
                            "row": rows.get("mlab-plex"),
                        }
                    )
        elif entry.get("plex") is None and wanted:
            counts["plex item missing for a decided file"] += 1
        for sid in JELLYFINS:
            jfe = (entry.get("jellyfin") or {}).get(sid)
            if not jfe or not rows.get(sid):
                counts[f"{sid} row {rows.get(sid)}{' (no item)' if not jfe else ''}"] += 1
                continue
            expect = sorted(
                (KINDS[t], m["start_ms"] * TICKS_PER_MS, m["end_ms"] * TICKS_PER_MS) for t, m in wanted.items()
            )
            got = sorted(
                (s["type"], s["start_ticks"], s["end_ticks"]) for s in jfe["segments"] if s["type"] in KINDS.values()
            )
            ok = got == expect
            counts[f"{sid} {'ok' if ok else 'MISMATCH'} (row {rows.get(sid)})"] += 1
            if not ok:
                mismatches.append(
                    {"file": entry["file"], "server": sid, "served": got, "decided": expect, "row": rows.get(sid)}
                )
    result = {"counts": dict(sorted(counts.items())), "mismatches": mismatches}
    scale_save(f"served-{name}", result)
    say(json.dumps(result["counts"], indent=1))
    say(f"{len(mismatches)} mismatches")
    return result


def scale_writes(name: str) -> dict:
    """What a job changed: Plex rows added/removed/changed, parts extra_data changed, plugin marker files changed."""
    before, after = scale_state(f"{name}-before"), scale_state(f"{name}-after")
    rb, ra = before["plex"]["rows"], after["plex"]["rows"]
    result = {
        "plex_rows_added": len(ra.keys() - rb.keys()),
        "plex_rows_removed": len(rb.keys() - ra.keys()),
        "plex_rows_changed": sum(1 for k in ra.keys() & rb.keys() if ra[k] != rb[k]),
        "plex_parts_extra_data_changed": sum(
            1 for p in after["plex"]["parts"] if before["plex"]["parts"].get(p) != after["plex"]["parts"][p]
        ),
        "plugin_files": {
            c: {
                "added": len(after["plugin_files"][c].keys() - before["plugin_files"][c].keys()),
                "removed": len(before["plugin_files"][c].keys() - after["plugin_files"][c].keys()),
                "changed": sum(
                    1
                    for k in after["plugin_files"][c].keys() & before["plugin_files"][c].keys()
                    if after["plugin_files"][c][k] != before["plugin_files"][c][k]
                ),
            }
            for c in JELLYFINS
        },
        "usage_before": {k: v["used"] for k, v in before["usage"].items()},
        "usage_after": {k: v["used"] for k, v in after["usage"].items()},
        "theintrodb_remaining_after": after["usage"]["theintrodb"].get("remaining"),
    }
    state = scale_state(name)
    result["http_lookups_logged"] = dict(Counter(f"{x['source']} {x['status']}" for x in state.get("lookups", [])))
    scale_save(f"writes-{name}", result)
    say(json.dumps(result, indent=1))
    return result


def scale(argv: list[str]) -> int:
    step = argv[0] if argv else ""
    if step == "prod-dump":
        prod_dump()
    elif step == "quiet":
        lab_plex_quiet()
        jf12_movies_library()
    elif step == "scan":
        return scale_scan(argv[1])
    elif step == "counts":
        say(server_counts())
    elif step == "job":
        return scale_job(argv[1], force=len(argv) > 2 and argv[2] == "force")
    elif step == "collect":
        scale_collect(argv[1] if len(argv) > 1 else "backfill")
    elif step == "served":
        scale_served(argv[1] if len(argv) > 1 else "backfill")
    elif step == "writes":
        scale_writes(argv[1])
    else:
        print(
            "scale prod-dump | quiet | scan plex|mlab-jellyfin|mlab-jf12 | counts | job NAME [force] | collect NAME | served NAME | writes NAME"
        )
        return 2
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "scale":
        return scale(argv[1:])
    if not argv or argv[0] not in ("configure", "status", "run"):
        print(__doc__)
        return 2
    if argv[0] == "configure":
        configure()
        status()
        return 0
    if argv[0] == "status":
        status()
        return 0
    failed = 0
    for number in [int(a) for a in argv[1:]]:
        result = ROWS[number]()
        failed += result["result"] == "fail"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
