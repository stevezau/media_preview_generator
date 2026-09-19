#!/usr/bin/env python3
"""Phase 2 lab matrix for Intro & Credits (plan-phase2 Task 17), on top of phase1_matrix's helpers.

    ./phase2_matrix.py configure        phase 1 configure + Emby 4.10/4.9 + the synth audio/movie libraries on every
                                        server (and Plex's plex-only location), Intro & Credits on everywhere
    ./phase2_matrix.py run 1 21 2 ...   run rows in the given order
    ./phase2_matrix.py rows             list the rows

Each row writes results/p2-row-NN.json (git-ignored) with its result and evidence; credentials are scrubbed. Rows change
lab state; phase2-results.md lists the order used and how to reset the lab. MLAB_DIR sets the lab folder holding env,
synth/ and results/ (default: this script's folder); MLAB_SHOTS where screenshots go.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import phase1_matrix as p1
from phase1_matrix import ENV, app, app_ok, http, now_iso, plex, say, scrub, sh, wait_until

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
LAB = p1.LAB
RESULTS = p1.RESULTS
SYNTH = LAB / "synth"
EMBY49 = "http://127.0.0.1:18099"
EMBY_URLS = {"mlab-emby": p1.EMBY, "mlab-emby49": EMBY49}
EMBY_KEYS = {"mlab-emby": ("EMBY_TOKEN", "EMBY_UID"), "mlab-emby49": ("EMBY49_TOKEN", "EMBY49_UID")}
AUDIO_ROOT = "/media/synth-audio"
AUDIO_SHOW = f"{AUDIO_ROOT}/Synth Audio (2022)"
AUDIO_S1 = f"{AUDIO_SHOW}/Season 01"
AUDIO_S2 = f"{AUDIO_SHOW}/Season 02"
AUDIO_HOST = SYNTH / "Synth Audio (2022)"
STAGED_S2E02 = SYNTH / "_staging" / "Synth Audio (2022) - S02E02.webm"
# Theme truth written by synth_audio.sh: (season, episode) -> (start_ms, end_ms).
AUDIO_TRUTH = {(1, 1): (20_000, 50_000), (1, 2): (45_000, 75_000), (1, 3): (5_000, 35_000), (1, 4): (70_000, 100_000),
               (2, 1): (15_000, 45_000), (2, 2): (60_000, 90_000)}  # fmt: skip
MOVIE_ROOT = "/media/synth-movies"
MOVIE_FOLDER = f"{MOVIE_ROOT}/Synth Movie (2023)"
MOVIE_VERSIONS = tuple(f"{MOVIE_FOLDER}/Synth Movie (2023) - {v}.webm" for v in ("1080p", "720p"))
PLEXONLY_ROOT = "/media/plexonly"
PLEXONLY_SEASON = f"{PLEXONLY_ROOT}/Synth Chapters (2021)/Season 01"
PLEXONLY_HOST_SEASON = SYNTH / "_plexonly" / "Synth Chapters (2021)" / "Season 01"
STAGED_PLEXONLY = SYNTH / "_staging" / "Synth Chapters (2021) - S01E03 - Plexonly.webm"
STAGED_EXTENDED = SYNTH / "_staging" / "Synth Chapters (2021) - S01E01 - Extended.webm"
EMBY_SERVERS = ("mlab-emby", "mlab-emby49")
ALL_MARKER_SERVERS = ("mlab-plex", "mlab-jellyfin", "mlab-jf12", *EMBY_SERVERS)
EMBY_MARKER_TYPES = ("IntroStart", "IntroEnd", "CreditsStart")
# Libraries the phase 2 rows add to every lab server: name -> (Plex type, Jellyfin/Emby collection, path).
SYNTH_LIBRARIES = {
    "Synth Chapters": ("show", "tvshows", p1.SYNTH_ROOT),
    "Synth Audio": ("show", "tvshows", AUDIO_ROOT),
    "Synth Movies": ("movie", "movies", MOVIE_ROOT),
}
PLEX_AGENTS = {
    "show": ("tv.plex.agents.series", "Plex TV Series"),
    "movie": ("tv.plex.agents.movie", "Plex Movie"),
}
PLEX_DETECTION_PREFS = ("GenerateIntroMarkerBehavior", "GenerateCreditsMarkerBehavior")
CASES = LAB.parent / "online" / "cases.json"
# Phase 1 rows the regression runs, in phase 1 run 2's order (11 is unit-only, 12 is row 20 here, 15 is row 18 here, 17
# runs last in two calls).
LOCAL_ROWS = (14, 1, 2, 3, 13, 4, 6, 5, 7, 8, 9, 10, 16, 18, 19)


def audio_path(season: int, episode: int) -> str:
    return f"{AUDIO_SHOW}/Season {season:02d}/Synth Audio (2022) - S{season:02d}E{episode:02d}.webm"


def write_result(row: int, title: str, result: str, evidence: dict, notes: list[str] | None = None) -> dict:
    RESULTS.mkdir(exist_ok=True)
    body = scrub({"row": row, "title": title, "result": result, "at": now_iso(), "notes": notes or [], **evidence})
    (RESULTS / f"p2-row-{row:02d}.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    say(f"p2 row {row}: {result} — {title}")
    for note in notes or []:
        say(f"  - {note}")
    return body


def checks_result(
    row: int, title: str, checks: dict[str, bool], evidence: dict, notes: list[str] | None = None
) -> dict:
    """``write_result`` for a row judged by named checks: pass when every check holds; each check becomes a note."""
    lines = [f"{name}: {ok}" for name, ok in checks.items()] + (notes or [])
    return write_result(row, title, "pass" if all(checks.values()) else "fail", {"checks": checks, **evidence}, lines)


# ------------------------------------------------------------------------------------------------------ servers


def emby_call(server_id: str, method: str, path: str, body: Any = None) -> tuple[int, Any]:
    sep = "&" if "?" in path else "?"
    token = ENV[EMBY_KEYS[server_id][0]]
    return http(method, f"{EMBY_URLS[server_id]}/emby{path}{sep}api_key={token}", body=body)


def emby_uid(server_id: str) -> str:
    return ENV[EMBY_KEYS[server_id][1]]


def emby_items(server_id: str) -> dict[str, dict]:
    """File path -> {id, chapters} for every episode and movie on an Emby (each version is its own item).

    Read without a user: the per-user list shows a movie's versions as one item.
    """
    query = "Recursive=true&IncludeItemTypes=Episode,Movie&Fields=Path,Chapters"
    _, data = emby_call(server_id, "GET", f"/Items?{query}")
    return {i["Path"]: {"id": i["Id"], "chapters": i.get("Chapters") or []} for i in data["Items"] if i.get("Path")}


def emby_marks(chapters: list[dict]) -> list[dict]:
    """Marker chapters (IntroStart/IntroEnd/CreditsStart, ms) of an Emby item."""
    marks = [
        {"type": c["MarkerType"], "ms": c["StartPositionTicks"] // p1.TICKS_PER_MS}
        for c in chapters
        if c.get("MarkerType") in EMBY_MARKER_TYPES
    ]
    return sorted(marks, key=lambda m: (m["ms"], m["type"]))


def emby_chapters(server_id: str) -> dict[str, list[dict]]:
    """File path -> marker chapters for every episode and movie on an Emby."""
    return {path: emby_marks(item["chapters"]) for path, item in emby_items(server_id).items()}


def emby_bridge(server_id: str, method: str, item_id: str, body: Any = None) -> tuple[int, Any]:
    return emby_call(server_id, method, f"/MediaPreviewBridge/Markers/{item_id}", body)


def expected_emby(payload: dict) -> list[dict]:
    """The marker chapters Emby should list for a file's decisions (R1: the decided credits start, always)."""
    decided = p1.decided(payload)
    marks = []
    if "intro" in decided:
        marks += [
            {"type": "IntroStart", "ms": decided["intro"]["start_ms"]},
            {"type": "IntroEnd", "ms": decided["intro"]["end_ms"]},
        ]
    if "credits" in decided:
        marks.append({"type": "CreditsStart", "ms": decided["credits"]["start_ms"]})
    return sorted(marks, key=lambda m: (m["ms"], m["type"]))


def same_marks(got: list[dict], want: list[dict], tolerance_ms: int = 1_000) -> bool:
    return len(got) == len(want) and all(
        g["type"] == w["type"] and abs(g["ms"] - w["ms"]) <= tolerance_ms
        for g, w in zip(sorted(got, key=lambda m: m["type"]), sorted(want, key=lambda m: m["type"]), strict=True)
    )


def expected_jellyfin(payload: dict) -> list[tuple]:
    kinds = {"intro": "Intro", "credits": "Outro"}
    return sorted(
        (kinds[t], m["start_ms"] * p1.TICKS_PER_MS, m["end_ms"] * p1.TICKS_PER_MS)
        for t, m in p1.decided(payload).items()
        if t in kinds
    )


def jf_tuples(server_id: str, item_id: str) -> list[tuple]:
    return sorted((s["type"], s["start_ticks"], s["end_ticks"]) for s in p1.jf_segments(server_id, item_id))


def plex_sections() -> list[dict]:
    _, data = plex("GET", "/library/sections")
    return [
        {
            "key": s["key"],
            "title": s["title"],
            "type": s["type"],
            "agent": s["agent"],
            "scanner": s["scanner"],
            "language": s.get("language", "en-US"),
            "locations": [loc["path"] for loc in s.get("Location", [])],
        }
        for s in data["MediaContainer"].get("Directory", [])
    ]


def plex_section(path: str) -> dict | None:
    return next((s for s in plex_sections() if path in s["locations"]), None)


def plex_query(method: str, path: str, params: list[tuple[str, str]]) -> tuple[int, Any]:
    """A Plex call with repeated query keys (``location`` twice), which ``phase1_matrix.plex`` can't send."""
    url = f"{p1.PLEX}{path}?{urllib.parse.urlencode(params)}"
    return http(method, url, headers={"X-Plex-Token": ENV["PLEX_TOKEN"], "Accept": "application/json"})


def ensure_plex_libraries() -> dict[str, str]:
    """Plex sections for the synth libraries (created once) and /media/plexonly as a second Synth Chapters location."""
    keys = {}
    for name, (kind, _, path) in SYNTH_LIBRARIES.items():
        section = plex_section(path)
        if section is None:
            agent, scanner = PLEX_AGENTS[kind]
            status, body = plex(
                "POST", "/library/sections", name=name, type=kind, agent=agent, scanner=scanner, language="en-US",
                location=path,
            )  # fmt: skip
            if status >= 300:
                raise RuntimeError(f"Plex section {name} -> {status}: {body}")
            section = wait_until(f"Plex section {name}", lambda p=path: plex_section(p), timeout=60)
        keys[name] = section["key"]
    chapters = plex_section(p1.SYNTH_ROOT)
    if PLEXONLY_ROOT not in chapters["locations"]:
        params = [("name", chapters["title"]), ("agent", chapters["agent"]), ("scanner", chapters["scanner"])]
        params += [("language", chapters["language"]), ("location", p1.SYNTH_ROOT), ("location", PLEXONLY_ROOT)]
        status, body = plex_query("PUT", f"/library/sections/{chapters['key']}", params)
        if status >= 300 or PLEXONLY_ROOT not in plex_section(p1.SYNTH_ROOT)["locations"]:
            raise RuntimeError(f"Plex plex-only location -> {status}: {body}")
    for key in keys.values():
        plex("GET", f"/library/sections/{key}/refresh")
    return keys


def jf_libraries(server_id: str) -> list[dict]:
    return p1.jf(server_id, "GET", "/Library/VirtualFolders")[1]


def ensure_jf_libraries(server_id: str) -> dict[str, str]:
    """The synth libraries on a Jellyfin (created once, options copied from its Synth Chapters/movies libraries)."""
    ids = {}
    for name, (_, collection, path) in SYNTH_LIBRARIES.items():
        libs = jf_libraries(server_id)
        found = next((lib for lib in libs if lib["Name"] == name), None)
        if found is None:
            like = next(
                lib for lib in libs if lib["Name"] == ("movies" if collection == "movies" else "Synth Chapters")
            )
            options = {**like["LibraryOptions"], "PathInfos": []}
            query = urllib.parse.urlencode(
                {"name": name, "collectionType": collection, "paths": path, "refreshLibrary": "true"}
            )
            status, body = p1.jf(server_id, "POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})
            if status >= 300:
                raise RuntimeError(f"{server_id} library {name} -> {status}: {body}")
            found = wait_until(
                f"{server_id} library {name}",
                lambda n=name: next((lib for lib in jf_libraries(server_id) if lib["Name"] == n), None),
                timeout=60,
            )
        ids[name] = found["ItemId"]
    return ids


def emby_libraries(server_id: str) -> list[dict]:
    return emby_call(server_id, "GET", "/Library/VirtualFolders")[1]


def ensure_emby_libraries(server_id: str) -> dict[str, str]:
    """The synth libraries on an Emby (created once, options copied from its Synth Chapters library)."""
    ids = {}
    for name, (_, collection, path) in SYNTH_LIBRARIES.items():
        libs = emby_libraries(server_id)
        found = next((lib for lib in libs if lib["Name"] == name), None)
        if found is None:
            like = next(lib for lib in libs if lib["Name"] == "Synth Chapters")
            options = {**like["LibraryOptions"], "PathInfos": [{"Path": path}]}
            query = urllib.parse.urlencode({"name": name, "collectionType": collection, "refreshLibrary": "true"})
            status, body = emby_call(server_id, "POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})
            if status >= 300:
                raise RuntimeError(f"{server_id} library {name} -> {status}: {body}")
            found = wait_until(
                f"{server_id} library {name}",
                lambda n=name: next((lib for lib in emby_libraries(server_id) if lib["Name"] == n), None),
                timeout=60,
            )
        ids[name] = found["ItemId"]
    return ids


def refresh_library(name: str) -> None:
    """Scan one synth library on every lab server (Plex section, Jellyfin/Emby library item, recursive)."""
    _, _, path = SYNTH_LIBRARIES[name]
    for section in plex_sections():
        if path in section["locations"]:
            plex("GET", f"/library/sections/{section['key']}/refresh")
    query = "Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default&ReplaceAllMetadata=false"
    for sid in p1.JELLYFINS:
        lib = next(lib for lib in jf_libraries(sid) if lib["Name"] == name)
        p1.jf(sid, "POST", f"/Items/{lib['ItemId']}/Refresh?{query}&ReplaceAllImages=false")
    for sid in EMBY_SERVERS:
        lib = next(lib for lib in emby_libraries(sid) if lib["Name"] == name)
        emby_call(sid, "POST", f"/Items/{lib['ItemId']}/Refresh?{query}&ReplaceAllImages=false")


def _rescan_best_effort(name: str, gone: list[str]) -> dict | str:
    """``rescan_until`` for files a row removed, from a ``finally``: a timeout is recorded, not raised."""
    try:
        return rescan_until(name, gone, present=False)
    except Exception as exc:
        say(f"  servers still list {[p.rsplit('/', 1)[-1] for p in gone]}: {exc}")
        return f"{type(exc).__name__}: {exc}"


def listed(path: str) -> dict[str, bool]:
    """Whether each lab server lists a file."""
    out = {"mlab-plex": path in p1.plex_parts()}
    out.update({sid: path in p1.jf_items(sid) for sid in p1.JELLYFINS})
    out.update({sid: path in emby_items(sid) for sid in EMBY_SERVERS})
    return out


def rescan_until(name: str, paths: list[str], *, present: bool, timeout: float = 900) -> dict[str, bool]:
    """Scan a synth library everywhere until every server lists (or no longer lists) ``paths``.

    A library scan is asked again every 2 minutes, since Jellyfin and Emby can finish one before they see a new file.
    """
    deadline = time.monotonic() + timeout
    while True:
        refresh_library(name)
        until = min(deadline, time.monotonic() + 120)
        while time.monotonic() < until:
            time.sleep(5)
            state = {p: listed(p) for p in paths}
            if all(all(v == present for v in s.values()) for s in state.values()):
                p1.plex_wait_idle()
                return {p.rsplit("/", 1)[-1]: s for p, s in state.items()}
        if time.monotonic() >= deadline:
            raise TimeoutError(f"servers still {'missing' if present else 'listing'} {state}")


def server_entries() -> list[dict]:
    return [
        *p1.server_entries(),
        {
            "id": "mlab-emby49",
            "type": "emby",
            "name": "Lab Emby 4.9",
            "url": "http://mlab-emby49:8096",
            "auth": {"method": "api_key", "api_key": ENV["EMBY49_TOKEN"], "user_id": ENV["EMBY49_UID"]},
        },
    ]


def set_publish_when(level: str) -> None:
    app_ok("POST", "/api/settings", {"markers": {"publish_when": level}})
    say(f"publish_when -> {app_ok('GET', '/api/settings')['markers']['publish_when']}")


def set_detect(**types: bool) -> dict:
    app_ok("POST", "/api/settings", {"markers": {"detect": types}})
    stored = app_ok("GET", "/api/settings")["markers"]["detect"]
    say(f"detect -> {stored}")
    return stored


def plex_prefs() -> dict[str, str]:
    _, prefs = plex("GET", "/:/prefs")
    return {s["id"]: s["value"] for s in prefs["MediaContainer"]["Setting"] if s["id"] in PLEX_DETECTION_PREFS}


class PlexDetection:
    """Lab Plex's own intro and credits detection turned on for a block, and put back as found afterwards.

    The lab keeps it off (the scale run's quiet prefs); only rows that need Plex's own detection turn it on.
    """

    def __enter__(self) -> dict[str, str]:
        self.before = plex_prefs()
        p1.plex_set_prefs(**dict.fromkeys(PLEX_DETECTION_PREFS, "asap"))
        say(f"Plex detection prefs {self.before} -> asap")
        return self.before

    def __exit__(self, *exc: object) -> None:
        p1.plex_set_prefs(**self.before)
        say(f"Plex detection prefs back to {plex_prefs()}")


def plex_item(path: str) -> str:
    return p1.plex_parts()[path]["item"]


def plex_season(path: str) -> str:
    _, meta = plex("GET", f"/library/metadata/{plex_item(path)}")
    return meta["MediaContainer"]["Metadata"][0]["parentRatingKey"]


def plex_media_count(item: str) -> int:
    _, meta = plex("GET", f"/library/metadata/{item}")
    return len([m for m in meta["MediaContainer"]["Metadata"][0].get("Media", []) if "proxyType" not in m])


def plex_rows(item: str) -> list[dict]:
    return [r for r in p1.plex_marker_rows() if r["item"] == item]


def server_row(files: list[dict], name: str, server_id: str) -> dict:
    """One server's row for a file in a job's Files list (``{}`` when absent)."""
    entry = next((f for f in files if f["file"].rsplit("/", 1)[-1] == name), None)
    return next((s for s in (entry or {}).get("servers") or [] if s["id"] == server_id), {})


def inspector_server(path: str, server_id: str) -> dict:
    return next((s for s in p1.item_payload(path)["servers"] if s["server_id"] == server_id), {})


def run_job(body: dict, timeout: float = 1800) -> tuple[dict, list[dict]]:
    job = p1.wait_job(p1.start_markers_job(body)["id"], timeout=timeout)
    return job, p1.job_files(job["id"])


def job_warnings(job_id: str) -> list[str]:
    return [line for line in p1.job_logs(job_id) if "WARNING" in line or "Couldn't" in line]


def configure() -> None:
    p1.configure()
    status, _ = app("GET", "/api/servers/mlab-emby49")
    entry = server_entries()[-1]
    if status == 200:
        app_ok("PUT", "/api/servers/mlab-emby49", {k: v for k, v in entry.items() if k != "id"})
    else:
        app_ok("POST", "/api/servers", entry)
    setup: dict[str, Any] = {"plex": ensure_plex_libraries()}
    for sid in p1.JELLYFINS:
        setup[sid] = ensure_jf_libraries(sid)
    for sid in EMBY_SERVERS:
        setup[sid] = ensure_emby_libraries(sid)
    wanted = [audio_path(1, e) for e in range(1, 5)] + [audio_path(2, 1), *MOVIE_VERSIONS]
    setup["listed"] = rescan_until("Synth Audio", wanted[:5], present=True)
    setup["listed"].update(rescan_until("Synth Movies", wanted[5:], present=True))
    for sid in ALL_MARKER_SERVERS:
        libs = app_ok("POST", f"/api/servers/{sid}/refresh-libraries")["libraries"]
        setup[f"app libraries {sid}"] = [(lib["id"], lib["name"]) for lib in libs]
    for sid in EMBY_SERVERS:
        app_ok("PUT", f"/api/servers/{sid}", {"markers": {"enabled": True, "library_ids": None}})
    for sid in ALL_MARKER_SERVERS:
        cap = app_ok("GET", f"/api/markers/servers/{sid}/status")["capability"]
        setup[f"capability {sid}"] = cap
        say(f"{sid}: {cap['state']} — {cap['message']}")
    setup["local_sources"] = app_ok("GET", "/api/markers/sources/local")
    say("local sources:", setup["local_sources"])
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "p2-lab-setup.json").write_text(json.dumps(scrub({"at": now_iso(), **setup}), indent=2) + "\n")
    say("lab setup:", {k: v for k, v in setup.items() if not k.startswith(("capability", "app libraries"))})


class ChromaprintSampler(threading.Thread):
    """How many chromaprint ffmpeg processes run in mlab-app, every 0.5 s, until stopped (rows 2 and 18)."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak = 0
        self.argv: list[str] = []
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            out = sh("docker", "exec", "mlab-app", "ps", "-eo", "args", check=False)
            lines = [line for line in out.splitlines() if "-f chromaprint" in line and "ps -eo" not in line]
            self.peak = max(self.peak, len(lines))
            if lines and not self.argv:
                self.argv = lines[0].split()
            self.stop.wait(0.5)

    def threads_ok(self) -> bool:
        """``-threads 2`` in the argv seen (True when no chromaprint process ran)."""
        if not self.argv:
            return True
        return "-threads" in self.argv and self.argv[self.argv.index("-threads") + 1] == "2"


ROWS: dict[int, Any] = {}


def row(number: int):
    def register(fn):
        ROWS[number] = fn
        return fn

    return register


# ---------------------------------------------------------------------------------------------------------- rows


def built_plugin_version() -> str:
    csproj = (REPO / "emby-plugin" / "MediaPreviewBridge.Emby.csproj").read_text()
    return re.search(r"<Version>([^<]+)</Version>", csproj).group(1)


@row(1)
def row_01_capability() -> dict:
    """Every server ready; both Embys run the built plugin; season audio available with the ffmpeg the image has."""
    override = os.environ.get("MLAB_EMBY_PLUGIN_VERSION")
    version = override or built_plugin_version()
    status = {sid: app_ok("GET", f"/api/markers/servers/{sid}/status") for sid in ALL_MARKER_SERVERS}
    local = app_ok("GET", "/api/markers/sources/local")["season_audio"]
    checks = {f"{sid} ready": s["capability"]["state"] == "ready" for sid, s in status.items()}
    checks.update(
        {f"{sid} plugin {version}": status[sid]["capability"]["details"].get("plugin_version") == version for sid in EMBY_SERVERS}
    )  # fmt: skip
    checks["season audio available"] = local["available"] is True and bool(local["ffmpeg"])
    notes = [
        f"{sid}: {s['enabled']} {s['capability']['state']} {s['capability']['details']}" for sid, s in status.items()
    ]
    notes.append(f"local sources: {local}")
    notes.append(
        f"plugin version expected {version} ({'MLAB_EMBY_PLUGIN_VERSION override' if override else 'from the csproj'})"
    )
    evidence = {"status": status, "local": local, "expected_plugin_version": version, "version_override": override}
    return checks_result(1, "Capability on five servers", checks, evidence, notes)


def _near(rows: list[dict], source: str, truth: tuple[int, int]) -> bool:
    """An intro row of ``source`` within row 2's tolerances of the theme: end ±5 s, start ±15 s."""
    start, end = truth
    return any(
        e["source"] == source and e["type"] == "intro" and abs(e["end_ms"] - end) <= 5_000
        and abs(e["start_ms"] - start) <= 15_000
        for e in rows
    )  # fmt: skip


def _served_intros(paths: list[str]) -> dict[str, dict[str, list]]:
    """Per file: the intros each server serves now."""
    parts, emby = p1.plex_parts(), {sid: emby_chapters(sid) for sid in EMBY_SERVERS}
    jf = {sid: p1.jf_items(sid) for sid in p1.JELLYFINS}
    served = {}
    for path in paths:
        served[path.rsplit("/", 1)[-1]] = {
            "mlab-plex": [m for m in p1.plex_served(parts[path]["item"]) if m["type"] == "intro"],
            **{sid: [s for s in p1.jf_segments(sid, jf[sid][path]["id"]) if s["type"] == "Intro"] for sid in p1.JELLYFINS},
            **{sid: [m for m in emby[sid][path] if m["type"].startswith("Intro")] for sid in EMBY_SERVERS},
        }  # fmt: skip
    return served


def _season_audio_backfill(level: str) -> dict:
    set_publish_when(level)
    paths = [audio_path(1, e) for e in range(1, 5)] + [audio_path(2, 1)]
    # Plex's own intro rows (row 3 runs Plex's detection on this season) are Plex's, not ours: they must stay as they are.
    before = _served_intros(paths)
    sampler = ChromaprintSampler()
    sampler.start()
    try:
        job, _ = run_job(
            {"file_paths": [AUDIO_SHOW], "library_name": f"Phase 2 row 2 season audio ({level})", "force": True}
        )
    finally:
        sampler.stop.set()
        sampler.join()
    evidence: dict[str, Any] = {"job": {k: job[k] for k in ("id", "status", "progress")}, "episodes": {}}
    for season, episode in AUDIO_TRUTH:
        if (season, episode) == (2, 2):
            continue
        payload = p1.item_payload(audio_path(season, episode))
        audio = [e for e in payload["evidence"] if e["source"] in ("season_audio", "season_audio_previous")]
        evidence["episodes"][f"S{season:02d}E{episode:02d}"] = {
            "intro": payload["decisions"]["intro"],
            "season_audio_evidence": audio,
        }
    published = {
        path.rsplit("/", 1)[-1]: {s["server_id"]: s["published"] for s in p1.item_payload(path)["servers"]}
        for path in paths
    }
    evidence.update(
        {
            "served_intros_before": before,
            "served_intros": _served_intros(paths),
            "published": published,
            "chromaprint_peak": sampler.peak,
            "chromaprint_argv": sampler.argv,
        }
    )
    return evidence


def _season_audio_checks(evidence: dict) -> dict[str, bool]:
    """Row 2's checks from its recorded evidence (``evidence[level]`` for "high", then "medium")."""
    checks = {}
    for level, run in evidence.items():
        episodes = run["episodes"]
        checks[f"{level}: job completed"] = run["job"]["status"] == "completed"
        for label, episode in episodes.items():
            if label.startswith("S01"):
                truth = AUDIO_TRUTH[(1, int(label[-2:]))]
                checks[f"{level}: {label} needs review with season audio near the theme"] = episode["intro"][
                    "status"
                ] == "needs_review" and _near(episode["season_audio_evidence"], "season_audio", truth)
        s2e01 = episodes["S02E01"]
        checks[f"{level}: S02E01 not decided, no same-season season audio answer"] = s2e01["intro"][
            "status"
        ] != "decided" and not any(e["source"] == "season_audio" and e["type"] for e in s2e01["season_audio_evidence"])
        argv = run["chromaprint_argv"]
        # Nothing of ours on any server: no published marker in the app's records, Jellyfin and Emby show no intro, and
        # Plex shows only the intro rows it had before the job (its own detection's, from row 3).
        checks[f"{level}: no server serves an intro of ours"] = not any(
            any(v.values()) for v in run["published"].values()
        ) and all(
            v["mlab-plex"] == run["served_intros_before"][name]["mlab-plex"]
            and not any(x for sid, x in v.items() if sid != "mlab-plex")
            for name, v in run["served_intros"].items()
        )
        if level == "high":
            # The show's fingerprints were deleted before this run, so it has to fingerprint.
            checks["high: chromaprint ffmpeg ran, at most two at once, each with -threads 2"] = (
                0 < run["chromaprint_peak"] <= 2 and "-threads" in argv and argv[argv.index("-threads") + 1] == "2"
            )
        else:
            # A forced run reads its evidence again but reuses cached fingerprints, and the High run cached them all.
            checks["medium: no chromaprint ffmpeg (every fingerprint cached by the High run)"] = (
                run["chromaprint_peak"] == 0 and not argv
            )
    # The hint uses the previous season's cached fingerprints only (spec §6.2 step 4, test_season.py
    # test_no_cached_previous_season_gives_no_hint_and_fingerprints_nothing_else): a run where S02E01 went before S01 was
    # fingerprinted has an empty hint, and the next run, once S01 is cached, has it.
    checks["S02E01's previous-season hint is near the theme once S01 is cached (second run)"] = _near(
        evidence["medium"]["episodes"]["S02E01"]["season_audio_evidence"], "season_audio_previous", AUDIO_TRUTH[(2, 1)]
    )
    return checks


def delete_fingerprints(folder: str) -> str:
    """Delete the app's cached fingerprints of every file under ``folder`` (markers.db in the lab app's config volume;
    the image has no sqlite3 CLI). Returns the number of rows deleted."""
    statement = (
        f"DELETE FROM fingerprints WHERE file_id IN (SELECT id FROM files WHERE canonical_path LIKE '{folder}/%')"
    )
    return sh(
        "docker", "exec", "mlab-app", "python3", "-c",
        "import sqlite3, sys; c = sqlite3.connect('/config/markers.db'); n = c.execute(sys.argv[1]).rowcount; c.commit(); print(n)",
        statement,
    ).strip()  # fmt: skip


@row(2)
def row_02_season_audio_backfill() -> dict:
    """Synth Audio S01 + S02E01, forced, at High and then at Medium: season audio finds every S01 theme but never
    decides alone (R2) — every intro Needs review; S02E01 gets no same-season answer, only the previous-season hint
    (from S01's cached fingerprints, so from the second run); no server serves an intro. The show's fingerprints are
    deleted first, so the High run fingerprints (at most two chromaprint ffmpeg at once, each with -threads 2) and the
    Medium run reuses them. Row 4's S02E02 is set aside for the row (S02E01 must be alone in its season) and put back.
    P2_REEVALUATE=1 re-checks the recorded runs."""
    recorded = RESULTS / "p2-row-02.json"
    setup: dict[str, Any] = {}
    if os.environ.get("P2_REEVALUATE") and recorded.exists():
        body = json.loads(recorded.read_text())
        evidence = {level: body[level] for level in ("high", "medium")}
        setup = body.get("setup", {})
    else:
        s2e02 = AUDIO_HOST / "Season 02" / STAGED_S2E02.name
        setup["s2e02_set_aside"] = s2e02.exists()
        try:
            if setup["s2e02_set_aside"]:
                s2e02.unlink()
                setup["s2e02_gone"] = rescan_until("Synth Audio", [audio_path(2, 2)], present=False)
            setup["fingerprints_deleted"] = delete_fingerprints(AUDIO_SHOW)
            evidence = {level: _season_audio_backfill(level) for level in ("high", "medium")}
        finally:
            set_publish_when("high")
            if setup["s2e02_set_aside"] and not s2e02.exists():
                shutil.copyfile(STAGED_S2E02, s2e02)
                try:
                    setup["s2e02_back"] = rescan_until("Synth Audio", [audio_path(2, 2)], present=True)
                except TimeoutError as exc:  # best effort: the next scan lists it
                    setup["s2e02_back"] = str(exc)
    checks = _season_audio_checks(evidence)
    notes = [
        f"setup {setup}",
        *(
            f"{level}: job {evidence[level]['job']['id'][:8]} {evidence[level]['job']['progress'].get('outcome')}; "
            f"chromaprint peak {evidence[level]['chromaprint_peak']}"
            for level in evidence
        ),
    ]
    notes += [
        f"{level} {label}: intro {e['intro']['status']}; audio {[(a['source'], a['start_ms'], a['end_ms'], a['label']) for a in e['season_audio_evidence']]}"
        for level in evidence for label, e in evidence[level]["episodes"].items()
    ]  # fmt: skip
    notes.append(
        "expectation changed: S02E01's hint in the first run of a fresh show can be empty — it uses cached previous-season "
        "fingerprints only, and S02E01 ran before S01 was fingerprinted"
    )
    return checks_result(
        2, "Season audio backfill at High and Medium on five servers", checks, {"setup": setup, **evidence}, notes
    )


@row(3)
def row_03_high_alone() -> dict:
    """S01E02 at High is Needs review even when Plex's own intro agrees (G3); records Plex's own intro rows for S01."""
    set_publish_when("high")
    s1 = [audio_path(1, e) for e in range(1, 5)]
    with PlexDetection() as prefs_before:
        season = plex_season(s1[0])
        plex("PUT", f"/library/metadata/{season}/intro", force=1)
        p1.plex_wait_idle(min_wait=15, timeout=900)
        plex_intros = {p.rsplit("/", 1)[-1]: [r for r in plex_rows(plex_item(p)) if r["text"] == "intro"] for p in s1}
    job, files = run_job({"file_paths": [s1[1]], "library_name": "Phase 2 row 3 High alone", "force": True})
    payload = p1.item_payload(s1[1])
    intro = payload["decisions"]["intro"]
    server_rows_ = [e for e in payload["evidence"] if e["source"].startswith("server_markers") and e["type"] == "intro"]
    audio = [e for e in payload["evidence"] if e["source"] == "season_audio" and e["type"] == "intro"]
    agrees = any(abs(s["end_ms"] - a["end_ms"]) <= 5_000 for s in server_rows_ for a in audio)
    published = {p.rsplit("/", 1)[-1]: inspector_server(p, "mlab-plex").get("published") for p in s1}
    checks = {
        "job completed": job["status"] == "completed",
        "Plex's own intro and season audio agree within 5 s on S01E02 (the G3 premise)": agrees,
        "S01E02 intro needs review at High": intro["status"] == "needs_review",
        "nothing of ours published to Plex on S01 (Plex's rows are its own)": not any(published.values()),
    }
    notes = [
        f"Plex's own intro rows after forced season detection: { {k: [(r['start'], r['end']) for r in v] for k, v in plex_intros.items()} }",
        f"S01E02 evidence: server markers {[(e['source'], e['origin'], e['start_ms'], e['end_ms']) for e in server_rows_]}; "
        f"season audio {[(e['start_ms'], e['end_ms'], e['label']) for e in audio]}; a server marker agrees within 5 s: {agrees}",
        f"S01E02 intro {intro['status']}: {intro['reason']}",
        f"Plex detection prefs before {prefs_before}",
    ]  # fmt: skip
    evidence = {"plex_intro_rows": plex_intros, "payload": payload, "job": job["id"], "files": files, "agrees": agrees}
    return checks_result(3, "High alone: season audio + server markers never decide (G3)", checks, evidence, notes)


@row(4)
def row_04_weekly_release() -> dict:
    """A new S02E02 arrives by webhook: its job matches it against S02E01 and queues one Season job holding only
    S02E01 (NORMAL, from a webhook follow-up); S02E01 then has a same-season season audio answer near the theme and
    still Needs review (R2/G3); nothing is served."""
    set_publish_when("medium")
    try:
        return _weekly_release()
    finally:
        set_publish_when("high")


def _weekly_release() -> dict:
    target = AUDIO_HOST / "Season 02" / STAGED_S2E02.name
    if not target.exists():
        shutil.copyfile(STAGED_S2E02, target)
    new = audio_path(2, 2)
    listed_state = rescan_until("Synth Audio", [new], present=True)
    t0 = now_iso()
    status, body = http("POST", f"{p1.APP}/api/webhooks/sonarr", headers={"X-Auth-Token": ENV["MLAB_APP_TOKEN"]}, body={
        "eventType": "Download",
        "series": {"title": "Synth Audio", "path": AUDIO_SHOW},
        "episodes": [{"seasonNumber": 2, "episodeNumber": 2, "title": "Synth Audio 2x2"}],
        "episodeFile": {"path": new, "relativePath": f"Season 02/{STAGED_S2E02.name}"},
    })  # fmt: skip
    if status >= 300:
        return write_result(4, "Weekly release", "fail", {"webhook": [status, body]})
    for batch in app_ok("GET", "/api/webhooks/pending")["pending"]:
        app_ok("POST", f"/api/webhooks/pending/{urllib.parse.quote(batch['key'], safe='')}/fire-now")

    def season_job() -> dict | None:
        return next((j for j in p1.jobs_since(t0) if j["library_name"].startswith("Season: ")), None)

    found = wait_until("the Season job", season_job, timeout=1800, every=5)
    p1.wait_job(found["id"], timeout=1800)
    for job in p1.jobs_since(t0):
        p1.wait_job(job["id"], timeout=1800)
    jobs = [{k: j[k] for k in ("id", "library_name", "status", "priority", "kind")} for j in p1.jobs_since(t0)]
    season_jobs = [j for j in jobs if j["library_name"].startswith("Season: ")]
    season_files = [f["file"] for f in p1.job_files(found["id"])]
    episodes = {e: p1.item_payload(audio_path(2, e)) for e in (1, 2)}

    def near(payload: dict, episode: int) -> bool:
        start, end = AUDIO_TRUTH[(2, episode)]
        return any(
            e["source"] == "season_audio" and e["type"] == "intro" and abs(e["end_ms"] - end) <= 5_000
            and abs(e["start_ms"] - start) <= 15_000
            for e in payload["evidence"]
        )  # fmt: skip

    served = {e: listed_intros(audio_path(2, e)) for e in (1, 2)}
    checks = {
        "exactly one Season job": len(season_jobs) == 1,
        "named Season: Synth Audio (2022) · Season 02": found["library_name"]
        == "Season: Synth Audio (2022) · Season 02",
        "it holds only S02E01": season_files == [audio_path(2, 1)],
        "at NORMAL (queued by a webhook follow-up)": found["priority"] == 2,
        "S02E01 has a same-season season audio answer near the theme": near(episodes[1], 1),
        "S02E01 intro still needs review": episodes[1]["decisions"]["intro"]["status"] == "needs_review",
        "S02E02 intro needs review": episodes[2]["decisions"]["intro"]["status"] == "needs_review",
        "no intro served on S02": not any(any(v.values()) for v in served.values()),
    }
    notes = [
        f"jobs since the webhook: {[(j['id'][:8], j['library_name'], j['kind'], j['priority'], j['status']) for j in jobs]}",
        f"S02E02 near its theme too: {near(episodes[2], 2)}",
    ] + [
        f"S02E0{e}: {p['decisions']['intro']['status']} {[(x['source'], x['start_ms'], x['end_ms'], x['label']) for x in p['evidence'] if x['source'].startswith('season_audio')]}"
        for e, p in episodes.items()
    ]  # fmt: skip
    evidence = {
        "listed": listed_state,
        "jobs": jobs,
        "season_files": season_files,
        "episodes": episodes,
        "served": served,
    }
    return checks_result(4, "Weekly release: new episode + one Season job", checks, evidence, notes)


def listed_intros(path: str) -> dict[str, list]:
    """The intros every server serves for a file right now."""
    parts = p1.plex_parts()
    out = {
        "mlab-plex": [m for m in p1.plex_served(parts[path]["item"]) if m["type"] == "intro"] if path in parts else []
    }
    for sid in p1.JELLYFINS:
        item = p1.jf_items(sid).get(path)
        out[sid] = [s for s in p1.jf_segments(sid, item["id"]) if s["type"] == "Intro"] if item else []
    for sid in EMBY_SERVERS:
        out[sid] = [m for m in emby_chapters(sid).get(path, []) if m["type"].startswith("Intro")]
    return out


def rick_truth() -> dict[int, tuple[float, float]]:
    return {c["episode"]: tuple(c["intro"]) for c in json.loads(CASES.read_text()) if c["show"] == "Rick and Morty" and c["season"] == 1 and c.get("intro")}  # fmt: skip


def rick_files() -> dict[int, str]:
    files = {}
    for path in p1.library_files(p1.RICK_SEASON):
        match = re.search(r"S01E(\d\d)", path)
        if match:
            files[int(match.group(1))] = path
    return files


@row(5)
def row_05_rick_high() -> dict:
    """Rick and Morty S01 at High, a normal job: season audio is in decided_by on at least one episode (agreeing with
    an online source) and no decided intro is wrong against the 11 online truth cases."""
    sys.path.insert(0, str(REPO))
    from tools.markers_eval.score import judge_intro

    set_publish_when("high")
    before = {e: p1.item_payload(p)["decisions"]["intro"] for e, p in rick_files().items()}
    job, _ = run_job({"file_paths": [p1.RICK_SEASON], "library_name": "Phase 2 row 5 Rick and Morty High"})
    truth = rick_truth()
    verdicts, with_audio, detail = {"useful": 0, "wrong": 0, "missed": 0}, [], {}
    for episode, path in sorted(rick_files().items()):
        intro = p1.item_payload(path)["decisions"]["intro"]
        marker = intro["marker"] if intro["status"] == "decided" else None
        if marker and "season_audio" in marker["decided_by"]:
            with_audio.append(episode)
        verdict = None
        if episode in truth:
            segment = (marker["start_ms"] / 1000, marker["end_ms"] / 1000) if marker else None
            verdict = judge_intro(segment, truth[episode])
            verdicts[verdict] += 1
        detail[f"E{episode:02d}"] = {
            "status": intro["status"],
            "marker": marker,
            "verdict": verdict,
            "before": before.get(episode),
        }
    checks = {
        "job completed": job["status"] == "completed",
        "season audio in decided_by on at least one episode": bool(with_audio),
        "no decided intro wrong": verdicts["wrong"] == 0,
    }
    notes = [
        f"useful/wrong/missed on the {len(truth)} online truth cases: {verdicts}",
        f"episodes whose intro decided_by has season_audio: {with_audio}",
    ] + [f"{k}: {v['status']} {v['marker'] and (v['marker']['start_ms'], v['marker']['end_ms'], v['marker']['decided_by'])} {v['verdict']}" for k, v in detail.items()]  # fmt: skip
    return checks_result(
        5, "Rick and Morty S01 at High", checks, {"job": job, "verdicts": verdicts, "episodes": detail}, notes
    )


def emby_served_check(server_id: str, paths: list[str]) -> dict[str, dict]:
    """Per file: the Emby item's marker chapters against the decisions (±1 s) and its plain chapter count."""
    items = emby_items(server_id)
    out = {}
    for path in paths:
        item = items.get(path)
        payload = p1.item_payload(path)
        want = expected_emby(payload)
        got = emby_marks(item["chapters"]) if item else None
        out[path.rsplit("/", 1)[-1]] = {
            "item": item and item["id"],
            "expected": want,
            "got": got,
            "plain_chapters": len([c for c in (item or {}).get("chapters", []) if c.get("MarkerType") == "Chapter"]),
            "ok": got is not None and same_marks(got, want),
        }
    return out


def synth_chapter_paths() -> list[str]:
    return [p1.synth_path(e) for e in (1, 2, 3)]


@row(6)
def row_06_emby_write_and_serve() -> dict:
    """Emby 4.10 lists our marker chapters at the decisions (R1: credits start always) next to the file's own chapters;
    the Extended copy (10 s tail) is its own item with its own CreditsStart and the "Emby skips to the end of the file"
    note; PlaybackInfo of the grouped item is recorded (evidence gap from Task 10)."""
    audio = [audio_path(1, e) for e in range(1, 5)]
    run_job({"file_paths": [p1.SYNTH_SHOW, AUDIO_S1], "library_name": "Phase 2 row 6 Emby write"})
    served = emby_served_check("mlab-emby", synth_chapter_paths() + audio)
    extended = p1.synth_path(1, " - Extended")
    target = p1.SYNTH_HOST_SEASON / extended.rsplit("/", 1)[-1]
    shutil.copyfile(STAGED_EXTENDED, target)
    try:
        listed_state = rescan_until("Synth Chapters", [extended], present=True)
        job, files = run_job({"file_paths": [p1.synth_path(1), extended], "library_name": "Phase 2 row 6 Extended"})
        versions = emby_served_check("mlab-emby", [p1.synth_path(1), extended])
        ext_row = server_row(files, extended.rsplit("/", 1)[-1], "mlab-emby")
        inspector = inspector_server(extended, "mlab-emby")
        playback = emby_playback_info("mlab-emby", versions[p1.synth_path(1).rsplit("/", 1)[-1]]["item"])
    finally:
        target.unlink(missing_ok=True)
        removed = _rescan_best_effort("Synth Chapters", [extended])
    ext_name, e01_name = extended.rsplit("/", 1)[-1], p1.synth_path(1).rsplit("/", 1)[-1]
    note = "Emby skips to the end of the file"
    checks = {
        "Synth Chapters E01–E03 and Synth Audio S01 marker chapters = decisions": all(v["ok"] for v in served.values()),
        "Synth Chapters keep their 4 plain chapters": all(served[p.rsplit("/", 1)[-1]]["plain_chapters"] == 4 for p in synth_chapter_paths()),
        "Synth Audio S01 has no intro on Emby (G3: needs review)": not any(m["type"].startswith("Intro") for p in audio for m in served[p.rsplit("/", 1)[-1]]["got"] or []),
        "Extended job completed": job["status"] == "completed",
        "Extended and E01 are separate Emby items": versions[ext_name]["item"] not in (None, versions[e01_name]["item"]),
        "Extended gets CreditsStart at the decided start (R1)": versions[ext_name]["ok"] and any(m["type"] == "CreditsStart" for m in versions[ext_name]["got"] or []),
        "E01 keeps its own markers": versions[e01_name]["ok"],
        "Files row for mlab-emby says Emby skips to the end of the file": note in (ext_row.get("message") or ""),
        "no 'can't show credits that end before the file does' text": "can't show credits" not in json.dumps([files, inspector]),
    }  # fmt: skip
    notes = [
        f"Extended Files row {ext_row}; Inspector plan {inspector.get('plan')} {inspector.get('plan_reason')!r}",
        f"PlaybackInfo of E01's item: {playback}",
    ] + [
        f"{k}: {v['got']} (expected {v['expected']}, {v['plain_chapters']} plain)"
        for k, v in {**served, **versions}.items()
    ]
    evidence = {
        "served": served,
        "versions": versions,
        "listed": listed_state,
        "removed": removed,
        "files": files,
        "playback": playback,
    }
    return checks_result(6, "Emby write and serve (4.10), per version, R1", checks, evidence, notes)


def emby_playback_info(server_id: str, item_id: str) -> list[dict]:
    """Which MediaSource of a grouped Emby item carries which chapters (Task 10 evidence gap)."""
    query = f"UserId={emby_uid(server_id)}&Fields=Chapters"
    _, data = emby_call(server_id, "POST", f"/Items/{item_id}/PlaybackInfo?{query}", {})
    out = []
    for source in (data or {}).get("MediaSources") or []:
        chapters = source.get("Chapters")
        out.append(
            {
                "id": source.get("Id"),
                "item_id": source.get("ItemId"),
                "file": (source.get("Path") or "").rsplit("/", 1)[-1],
                "chapters_listed": chapters is not None,
                "marker_chapters": emby_marks(chapters or []),
                "plain_chapters": len([c for c in chapters or [] if c.get("MarkerType") == "Chapter"]),
            }
        )
    return out


def emby_refresh(server_id: str, item_id: str, mode: str, replace_all: bool) -> None:
    query = urllib.parse.urlencode(
        {"Recursive": "false", "MetadataRefreshMode": mode, "ImageRefreshMode": "Default",
         "ReplaceAllMetadata": str(replace_all).lower(), "ReplaceAllImages": "false"}
    )  # fmt: skip
    emby_call(server_id, "POST", f"/Items/{item_id}/Refresh?{query}")


def emby_wait_healthy(server_id: str) -> None:
    def answers() -> bool:
        try:
            return emby_call(server_id, "GET", "/System/Info")[0] == 200
        except OSError:
            return False

    wait_until(f"{server_id} to answer", answers, timeout=300, every=3)
    time.sleep(15)


def emby_scan(server_id: str, timeout: float = 600) -> None:
    """A full Emby library scan, waited for (the RefreshLibrary task ends after it started)."""
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    emby_call(server_id, "POST", "/Library/Refresh")

    def finished() -> bool:
        task = next(t for t in emby_call(server_id, "GET", "/ScheduledTasks")[1] if t.get("Key") == "RefreshLibrary")
        end = (task.get("LastExecutionResult") or {}).get("EndTimeUtc", "")
        return task["State"] == "Idle" and end[:19] >= started

    time.sleep(3)
    wait_until(f"{server_id} library scan", finished, timeout=timeout, every=3)


def emby_wipe_steps(server_id: str, path: str, steps: tuple[str, ...]) -> list[dict]:
    """Refreshes/scans/restart on one Emby item; after each, its marker chapters against the decisions."""
    want = expected_emby(p1.item_payload(path))
    item = emby_items(server_id)[path]["id"]
    out = []
    for step in steps:
        if step == "FullRefresh + ReplaceAllMetadata":
            emby_refresh(server_id, item, "FullRefresh", True)
        elif step == "Default refresh":
            emby_refresh(server_id, item, "Default", False)
        elif step == "ValidationOnly refresh":
            emby_refresh(server_id, item, "ValidationOnly", False)
        elif step == "library scan":
            emby_scan(server_id)
        elif step == "docker restart":
            sh("docker", "restart", server_id, timeout=300)
            emby_wait_healthy(server_id)
        try:
            wait_until(step, lambda: same_marks(emby_chapters(server_id).get(path, []), want), timeout=30, every=2)
        except TimeoutError:
            pass
        time.sleep(5)
        got = emby_chapters(server_id).get(path, [])
        out.append({"step": step, "got": got, "ok": same_marks(got, want)})
        say(f"  {server_id} {step}: {got}")
    return out


EMBY_WIPE_STEPS = (
    "FullRefresh + ReplaceAllMetadata",
    "Default refresh",
    "ValidationOnly refresh",
    "library scan",
    "docker restart",
)


def reencode_e03() -> dict:
    """Replace Synth Chapters S01E03 in the lab folder with a new encode of the same layout (same chapters, other size).

    synth_chapters.sh encodes the missing file again from its chapter list. (Re-encoding the file itself moves every
    chapter 7 ms later: the Opus codec delay.)
    """
    source = p1.SYNTH_HOST_SEASON / p1.synth_path(3).rsplit("/", 1)[-1]
    before = source.stat().st_size
    source.unlink()
    sh("env", f"MLAB_DIR={LAB}", str(HERE / "synth_chapters.sh"), timeout=900)
    return {"size_before": before, "size_after": source.stat().st_size}


@row(7)
def row_07_emby_wipe_matrix() -> dict:
    """Emby 4.10, Synth Chapters S01E02: markers present after FullRefresh (healed), Default, ValidationOnly, library
    scan and a restart. Then S01E03 is replaced by a different-size file: the plugin says Stale and Emby lists no marker
    chapters; the next normal job writes them again and Stale is false."""
    steps = emby_wipe_steps("mlab-emby", p1.synth_path(2), EMBY_WIPE_STEPS)
    e03 = p1.synth_path(3)
    item = emby_items("mlab-emby")[e03]["id"]
    sizes = reencode_e03()

    def emby_dropped() -> bool:
        return not emby_chapters("mlab-emby").get(e03)

    # Emby's real-time monitor usually re-reads the replaced file on its own; a library scan makes sure.
    try:
        wait_until("Emby's monitor to re-read the replaced S01E03", emby_dropped, timeout=90, every=5)
    except TimeoutError:
        emby_scan("mlab-emby")
    _, stale = emby_bridge("mlab-emby", "GET", item)
    marks_after_replace = emby_chapters("mlab-emby").get(e03, [])
    job, files = run_job({"file_paths": [e03], "library_name": "Phase 2 row 7 replaced E03"})
    _, fresh = emby_bridge("mlab-emby", "GET", item)
    marks_after_job = emby_chapters("mlab-emby").get(e03, [])
    emby_row = server_row(files, e03.rsplit("/", 1)[-1], "mlab-emby")
    checks = {f"S01E02 markers after {s['step']}": s["ok"] for s in steps}
    checks.update(
        {
            "replaced S01E03: plugin says Stale": stale.get("Stale") is True,
            "replaced S01E03: Emby lists no marker chapters": marks_after_replace == [],
            "next normal job: Emby row markers_written": emby_row.get("status") == "markers_written",
            "after the job: Stale false and markers = decisions": fresh.get("Stale") is False
            and same_marks(marks_after_job, expected_emby(p1.item_payload(e03))),
        }
    )
    notes = [f"S01E03 sizes {sizes}; plugin GET after replace {stale}; after job {fresh}; Emby row {emby_row}"]
    evidence = {"steps": steps, "e03": {"sizes": sizes, "stale": stale, "fresh": fresh, "files": files}}
    return checks_result(7, "Emby wipe matrix (4.10) and a replaced file", checks, evidence, notes)


@row(8)
def row_08_emby_web_skip() -> dict:
    """Emby web (4.10) shows Skip Intro inside Synth Chapters S01E02's intro (spec §12 phase 2 "done when")."""
    import emby_plugin_check as epc

    check = epc.EmbyCheck("mlab-emby")
    check.item = emby_items("mlab-emby")[p1.synth_path(2)]["id"]
    seen = check.web_skip("p2-row08-skip-intro")
    marks = emby_chapters("mlab-emby").get(p1.synth_path(2), [])
    checks = {"skip found: True": seen["found"]}
    notes = [
        f"client: {seen['client']}",
        f"S01E02 marker chapters {marks}",
        "native Emby apps (TV, mobile): can't be automated here — needs owner",
    ]
    return checks_result(8, "Emby web Skip Intro", checks, {"web": seen, "marks": marks}, notes)


@row(9)
def row_09_emby49() -> dict:
    """Emby 4.9: row 6's marker chapters = decisions, and row 7's FullRefresh heal."""
    audio = [audio_path(1, e) for e in range(1, 5)]
    served = emby_served_check("mlab-emby49", synth_chapter_paths() + audio)
    steps = emby_wipe_steps("mlab-emby49", p1.synth_path(2), ("FullRefresh + ReplaceAllMetadata",))
    checks = {
        "marker chapters = decisions": all(v["ok"] for v in served.values()),
        "Synth Chapters keep their 4 plain chapters": all(served[p.rsplit("/", 1)[-1]]["plain_chapters"] == 4 for p in synth_chapter_paths()),
        "FullRefresh: markers back": steps[0]["ok"],
    }  # fmt: skip
    notes = [f"{k}: {v['got']} (expected {v['expected']})" for k, v in served.items()]
    return checks_result(9, "Emby 4.9 write, serve and heal", checks, {"served": served, "steps": steps}, notes)


def plex_delete_our_rows(item: str) -> list[dict]:
    """Delete an item's intro/credits taggings rows from lab Plex's database (a server dropping markers)."""
    rows = [r for r in p1.plex_marker_rows() if r["item"] == item]
    if rows:
        ids = ",".join(r["id"] for r in rows)
        sh(str(HERE / "plexdb.sh"), f"delete from taggings where id in ({ids})")
    return rows


def reconcile_job(timeout: float = 1800) -> tuple[dict, list[dict], dict]:
    answer = app_ok("POST", "/api/markers/reconcile")
    job = p1.wait_job(answer["job_id"], timeout=timeout)
    return job, p1.job_files(job["id"]), answer


@row(10)
def row_10_reconcile_restores() -> dict:
    """Markers Plex, Jellyfin and Emby dropped come back from one Check servers job, Plex's credits ``final`` flag
    included; a second run lists no files. A forced job first puts right a ``final`` flag left from a deleted version."""
    set_publish_when("high")
    p1.set_redetect("restore")
    ep = p1.synth_path(1)
    truth = p1.SYNTH_TRUTH[1]
    item = plex_item(ep)
    jf_item = p1.jf_items("mlab-jellyfin")[ep]["id"]
    emby_item = emby_items("mlab-emby")[ep]["id"]
    flag_before = p1.plex_served(item)
    flag_job, flag_files = run_job({"file_paths": [ep], "force": True, "library_name": "Phase 2 row 10 final flag"})
    reference = {
        "plex": p1.plex_served(item),
        "jellyfin": jf_tuples("mlab-jellyfin", jf_item),
        "emby": emby_chapters("mlab-emby")[ep],
    }
    deleted = plex_delete_our_rows(item)
    p1.jf("mlab-jellyfin", "DELETE", f"/MediaPreviewBridge/Markers/{jf_item}")
    emby_bridge("mlab-emby", "DELETE", emby_item)
    before = {
        "plex": p1.plex_served(item),
        "jellyfin": jf_tuples("mlab-jellyfin", jf_item),
        "emby": emby_chapters("mlab-emby")[ep],
    }
    first, files, answer = reconcile_job()
    after = {
        "plex": p1.plex_served(item),
        "jellyfin": jf_tuples("mlab-jellyfin", jf_item),
        "emby": emby_chapters("mlab-emby")[ep],
    }
    second, second_files, second_answer = reconcile_job()
    listed_files = [f["file"] for f in files]
    name = ep.rsplit("/", 1)[-1]
    credits_before_drop = [m for m in reference["plex"] if m["type"] == "credits"]
    checks = {
        "forced job completed": flag_job["status"] == "completed",
        "Plex's credits run to the file's end, so they are final before the drop": bool(credits_before_drop) and all(m["final"] for m in credits_before_drop),
        "every server dropped them first": not before["plex"] and not before["jellyfin"] and not before["emby"],
        "first run completed and lists the file": first["status"] == "completed" and ep in listed_files,
        "Plex intro back at the chapter": any(m["type"] == "intro" and abs(m["start"] - truth[0]) <= 1_000 for m in after["plex"]),
        "all three serve the same markers as before the drop, Plex's final flag included": after == reference,
        "rows written on the three": all(server_row(files, name, s).get("status") == "markers_written" for s in ("mlab-plex", "mlab-jellyfin", "mlab-emby")),
        "second run completed with no files": second["status"] == "completed" and not second_files,
    }  # fmt: skip
    notes = [
        f"forced job {flag_job['id'][:8]}: Plex served {flag_before} before it, {reference['plex']} after; Plex row {server_row(flag_files, ep.rsplit('/', 1)[-1], 'mlab-plex').get('status')}",
        f"first {first['id'][:8]} ({answer}) files {[f.rsplit('/', 1)[-1] for f in listed_files]} outcome {first['progress'].get('outcome')}",
        f"second {second['id'][:8]} ({second_answer}) files {len(second_files)}; log {[x for x in p1.job_logs(second['id']) if 'Check servers' in x][-2:]}",
        f"Plex rows deleted {[(r['text'], r['start'], r['end'], r['extra_data']) for r in deleted]}; Plex served before the drop {reference['plex']}, after {after['plex']}",
    ]  # fmt: skip
    evidence = {
        "flag_before": flag_before,
        "flag_files": flag_files,
        "reference": reference,
        "before": before,
        "after": after,
        "files": files,
        "second_files": second_files,
    }
    return checks_result(
        10, "Check servers restores dropped markers on Plex, Jellyfin and Emby", checks, evidence, notes
    )


@row(11)
def row_11_plex_version_drift() -> dict:
    """Plex version drift (ledger L184) under the landed rules (spec §14 2026-09-15): a version only Plex can read is
    added to S01E03's item. Check servers lists E03 (versions changed) and takes our markers off the item ("Waiting for
    this item's other versions to agree"); a normal job keeps it waiting; with the copy gone, a normal job writes again."""
    e03 = p1.synth_path(3)
    name = e03.rsplit("/", 1)[-1]
    item = plex_item(e03)
    plexonly = f"{PLEXONLY_SEASON}/{STAGED_PLEXONLY.name}"
    target = PLEXONLY_HOST_SEASON / STAGED_PLEXONLY.name
    served_before = p1.plex_served(item)
    shutil.copyfile(STAGED_PLEXONLY, target)
    try:
        refresh_library("Synth Chapters")
        wait_until("Plex to add the plex-only version", lambda: plex_media_count(item) == 2 and plexonly in p1.plex_parts(), timeout=300, every=5)  # fmt: skip
        p1.plex_wait_idle()
        drift_job, drift_files, _ = reconcile_job()
        drift_row = server_row(drift_files, name, "mlab-plex")
        served_drift = p1.plex_served(item)
        normal_job, normal_files = run_job({"file_paths": [e03], "library_name": "Phase 2 row 11 two versions"})
        normal_row = server_row(normal_files, name, "mlab-plex")
        served_normal = p1.plex_served(item)
    finally:
        target.unlink(missing_ok=True)
        try:
            refresh_library("Synth Chapters")
            wait_until("Plex to drop the plex-only version", lambda: plex_media_count(item) == 1, timeout=300, every=5)
            p1.plex_wait_idle()
        except Exception as exc:  # best effort here; the row's last step fails if Plex still has the copy
            say(f"  Plex still lists the plex-only version: {exc}")
    back_job, back_files = run_job({"file_paths": [e03], "library_name": "Phase 2 row 11 copy removed"})
    back_row = server_row(back_files, name, "mlab-plex")
    served_back = p1.plex_served(item)
    waiting = "Waiting for this item's other versions to agree"
    checks = {
        "Plex served ours before": bool(served_before),
        "Check servers lists E03": e03 in [f["file"] for f in drift_files],
        "then Plex serves none of ours and the row waits naming the versions": not served_drift
        and drift_row.get("status") == "markers_waiting" and waiting in (drift_row.get("message") or ""),
        "a normal job keeps it waiting, nothing served": normal_row.get("status") == "markers_waiting" and not served_normal,
        "copy removed: a normal job writes again": back_row.get("status") == "markers_written" and p1.same_markers({"mlab-plex": served_back}, {"mlab-plex": p1.truth_everywhere(3)["mlab-plex"]})["mlab-plex"],
    }  # fmt: skip
    notes = [
        f"Check servers {drift_job['id'][:8]} row {drift_row}; normal {normal_job['id'][:8]} row {normal_row}; after removal {back_job['id'][:8]} row {back_row}",
        f"served before {served_before}; after Check servers {served_drift}; after normal {served_normal}; after removal {served_back}",
        "expectation changed: the row predates Plex version drift (Task 9); a normal job no longer leaves our markers on a cut nobody checked",
    ]  # fmt: skip
    evidence = {"drift_files": drift_files, "normal_files": normal_files, "back_files": back_files}
    return checks_result(11, "Plex version drift: a version only Plex can read", checks, evidence, notes)


def plex_index_rows(item: str) -> list[dict]:
    rows = p1.plex_db(
        "select t.id, t.[index], t.text, t.time_offset, t.end_time_offset, t.extra_data from taggings t join tags g "
        f"on g.id=t.tag_id where g.tag_type=12 and t.metadata_item_id={int(item)} order by t.[index]"
    )
    return [dict(zip(("id", "index", "text", "start", "end", "extra_data"), r, strict=False)) for r in rows]


def part_marker_keys(path: str) -> list[str]:
    extra = p1.plex_parts()[path]["extra_data"]
    return sorted(k for k in (json.loads(extra) if extra else {}) if k.startswith("pv:"))


@row(12)
def row_12_plex_p3_p4() -> dict:
    """P3 steps 2–4: an intro published next to Plex's own credits rows ([index] order, then Plex's forced credits
    detection). P4: after we remove our intro (pv:intros key deleted), does Plex's non-forced intro detection add its
    own? Both answers are recorded; the row passes when the lab steps ran and our markers are back at the end."""
    set_publish_when("high")
    p1.set_redetect("restore")
    rick = rick_files()[1]
    rick_item = plex_item(rick)
    e02 = p1.synth_path(2)
    e02_item = plex_item(e02)
    steps: dict[str, Any] = {}
    try:
        with PlexDetection():
            set_detect(intro=False, credits=False)
            run_job({"file_paths": [rick], "force": True, "library_name": "Phase 2 row 12 P3 clear ours"})
            steps["P3 0 ours removed"] = {"served": p1.plex_served(rick_item), "rows": plex_index_rows(rick_item)}
            plex("PUT", f"/library/metadata/{rick_item}/credits", force=1)
            p1.plex_wait_idle(min_wait=10, timeout=900)
            steps["P3 1 Plex own credits"] = {"served": p1.plex_served(rick_item), "rows": plex_index_rows(rick_item)}
            set_detect(intro=True, credits=False)
            run_job({"file_paths": [rick], "force": True, "library_name": "Phase 2 row 12 P3 intro only"})
            steps["P3 2 our intro only"] = {"served": p1.plex_served(rick_item), "rows": plex_index_rows(rick_item)}
            plex("PUT", f"/library/metadata/{rick_item}/credits", force=1)
            p1.plex_wait_idle(min_wait=10, timeout=900)
            steps["P3 4 after Plex forced credits"] = {
                "served": p1.plex_served(rick_item),
                "rows": plex_index_rows(rick_item),
            }

            steps["P4 0 before"] = {"keys": part_marker_keys(e02), "served": p1.plex_served(e02_item)}
            set_detect(intro=False, credits=True)
            run_job({"file_paths": [e02], "force": True, "library_name": "Phase 2 row 12 P4 remove intro"})
            steps["P4 1 intro removed"] = {
                "keys": part_marker_keys(e02),
                "served": p1.plex_served(e02_item),
                "rows": plex_index_rows(e02_item),
            }
            plex("PUT", f"/library/metadata/{plex_season(e02)}/intro")
            p1.plex_wait_idle(min_wait=15, timeout=900)
            steps["P4 2 after Plex non-forced intro detection"] = {
                "keys": part_marker_keys(e02),
                "served": p1.plex_served(e02_item),
                "rows": plex_index_rows(e02_item),
            }
    finally:
        set_detect(intro=True, credits=True)
    restore_rick, _ = run_job({"file_paths": [rick], "library_name": "Phase 2 row 12 restore Rick"})
    restore_e02, _ = run_job({"file_paths": [e02], "library_name": "Phase 2 row 12 restore E02"})
    steps["end"] = {"rick": p1.plex_served(rick_item), "e02": p1.plex_served(e02_item)}  # fmt: skip

    def types(served: list[dict]) -> list[str]:
        return sorted(m["type"] for m in served)

    p3_both = (
        types(steps["P3 2 our intro only"]["served"]) == ["credits", "intro"]
        if steps["P3 1 Plex own credits"]["served"]
        else None
    )
    plex_own_intro = [r for r in steps["P4 2 after Plex non-forced intro detection"]["rows"] if r["text"] == "intro"]
    checks = {
        "P3: Plex had no rows after ours were removed": not steps["P3 0 ours removed"]["served"],
        "P3: Plex's forced credits detection added credits": any(m["type"] == "credits" for m in steps["P3 1 Plex own credits"]["served"]),
        "P3: Plex serves our intro and its credits": bool(p3_both),
        "P4: removing our intro deleted the pv:intros key": "pv:intros" in steps["P4 0 before"]["keys"] and "pv:intros" not in steps["P4 1 intro removed"]["keys"],
        "end: Rick S01E01 and Synth S01E02 written back (restore)": restore_rick["status"] == restore_e02["status"] == "completed"
        and p1.same_markers({"mlab-plex": steps["end"]["e02"]}, {"mlab-plex": p1.truth_everywhere(2)["mlab-plex"]})["mlab-plex"],
    }  # fmt: skip
    notes = [
        f"P3 [index] with our intro + Plex's credits: {[(r['index'], r['text'], r['start']) for r in steps['P3 2 our intro only']['rows']]}",
        f"P3 after Plex's forced credits: served {steps['P3 4 after Plex forced credits']['served']}; [index] {[(r['index'], r['text'], r['start']) for r in steps['P3 4 after Plex forced credits']['rows']]}",
        f"P4 answer: Plex's non-forced intro detection {'added' if plex_own_intro else 'did not add'} its own intro row after our removal: {[(r['start'], r['end'], r['extra_data']) for r in plex_own_intro]}; part keys {steps['P4 2 after Plex non-forced intro detection']['keys']}",
    ]  # fmt: skip
    return checks_result(
        12,
        "Plex P3 (intro next to Plex's credits) and P4 (Plex re-detects after removal)",
        checks,
        {"steps": steps},
        notes,
    )


@row(13)
def row_13_final_flag() -> dict:
    """L274: with Keep Plex's, Plex's forced intro detection on Rick and Morty S01 keeps the final flag on the credits
    rows, and the next normal job's plan doesn't call our credits Plex's."""
    rick = rick_files()[1]
    item = plex_item(rick)
    p1.set_redetect("keep_plex")
    try:
        before = p1.plex_served(item)
        with PlexDetection():
            plex("PUT", f"/library/metadata/{plex_season(rick)}/intro", force=1)
            p1.plex_wait_idle(min_wait=20, timeout=1500)
        after = p1.plex_served(item)
        job, files = run_job({"file_paths": [rick], "library_name": "Phase 2 row 13 after Plex intro detection"})
        view = inspector_server(rick, "mlab-plex")
        row_ = server_row(files, rick.rsplit("/", 1)[-1], "mlab-plex")
    finally:
        p1.set_redetect("restore")
    # Plex's detection replaced the whole season's intros; a normal job under "Use ours" writes ours back on all of them.
    restored, _ = run_job({"file_paths": [p1.RICK_SEASON], "library_name": "Phase 2 row 13 restore"})
    credits_before = [m for m in before if m["type"] == "credits"]
    credits_after = [m for m in after if m["type"] == "credits"]
    credits_changed = [(m["start"], m["end"]) for m in credits_after] != [
        (m["start"], m["end"]) for m in credits_before
    ]
    reason = f"{view.get('plan_reason')} {row_.get('message')}".lower()
    intros_before = [(m["start"], m["end"]) for m in before if m["type"] == "intro"]
    intros_after = [(m["start"], m["end"]) for m in after if m["type"] == "intro"]
    checks = {
        "Plex's forced intro detection ran: the intro times changed": bool(intros_after)
        and intros_after != intros_before,
        "credits rows unchanged by intro detection": not credits_changed,
        "last credits row keeps final": bool(credits_after)
        and credits_after[-1]["final"] == credits_before[-1]["final"],
        "plan/row doesn't call our credits Plex's": "credits" not in reason or credits_changed,
        "restore job completed": restored["status"] == "completed",
    }
    notes = [
        f"served before {before}; after Plex's forced intro detection {after}",
        f"Inspector plan {view.get('plan')} {view.get('plan_reason')!r}; row {row_}",
    ]
    return checks_result(13, "L274: Plex keeps final on credits after its forced intro detection", checks, {"before": before, "after": after, "view": view, "files": files, "job": job["id"]}, notes)  # fmt: skip


def app_item_files(server_id: str, item_id: str) -> list[str] | None:
    """The version files the app recorded for a server item (markers.db ``item_versions``, read-only)."""
    out = sh(
        "docker", "exec", "mlab-app", "python3", "-c",
        "import json, sqlite3, sys; c = sqlite3.connect('file:/config/markers.db?mode=ro', uri=True); "
        "r = c.execute('select files_json from item_versions where server_id=? and item_id=?', sys.argv[1:3]).fetchone(); "
        "print(r[0] if r else 'null')",
        server_id, item_id,
    ).strip()  # fmt: skip
    return json.loads(out)


def plex_split(item: str) -> None:
    """Split a merged Plex item back into one item per version (Plex's "Split Apart")."""
    status, body = plex("PUT", f"/library/metadata/{item}/split")
    if status >= 300:
        raise RuntimeError(f"Plex split {item} -> {status}: {body}")


@row(14)
def row_14_movie_versions() -> dict:
    """L263: the two-version synth movie. Jellyfin 12.0 and 10.11 serve Outro 100–120 s on both versions; each Emby
    version item gets CreditsStart 100 s. Plex's movie agent matches nothing, so Plex lists each version as its own
    local item: both are published separately, then merged (Plex's "Merge") into one item with two versions, which the
    app records with both files and still serves credits 100–120 s. A merged item from an earlier run is split first."""
    item = plex_item(MOVIE_VERSIONS[0])
    if plex_media_count(item) > 1:
        plex_split(item)
        wait_until(
            "Plex to split the movie versions", lambda: len({plex_item(v) for v in MOVIE_VERSIONS}) == 2, timeout=120
        )
        p1.plex_wait_idle()
    job, files = run_job({"file_paths": [MOVIE_FOLDER], "library_name": "Phase 2 row 14 movie versions"})
    detail: dict[str, Any] = {}
    checks = {"job completed": job["status"] == "completed"}
    outro = ("Outro", 100_000 * p1.TICKS_PER_MS, 120_000 * p1.TICKS_PER_MS)
    for sid in p1.JELLYFINS:
        items = p1.jf_items(sid)
        segs = {v.rsplit("/", 1)[-1]: jf_tuples(sid, items[v]["id"]) if v in items else None for v in MOVIE_VERSIONS}
        detail[sid] = {"items": {v.rsplit("/", 1)[-1]: items.get(v) for v in MOVIE_VERSIONS}, "segments": segs}
        checks[f"{sid}: both versions listed with Outro 100–120 s"] = all(s == [outro] for s in segs.values())
    separate_items = sorted({plex_item(v) for v in MOVIE_VERSIONS})
    separate = {i: p1.plex_served(i) for i in separate_items}
    recorded_separate = {i: app_item_files("mlab-plex", i) for i in separate_items}
    checks["the app's records of the separate items list one version file each"] = sorted(
        f for files_ in recorded_separate.values() for f in files_ or []
    ) == sorted(MOVIE_VERSIONS) and all(len(files_ or []) == 1 for files_ in recorded_separate.values())
    checks["Plex: two separate items first, each serving credits 100–120 s"] = len(separate_items) == 2 and all(
        [(m["type"], m["start"], m["end"]) for m in v] == [("credits", 100_000, 120_000)] for v in separate.values()
    )
    plex("PUT", f"/library/metadata/{separate_items[0]}/merge", ids=",".join(separate_items[1:]))
    wait_until(
        "Plex to merge the movie versions", lambda: len({plex_item(v) for v in MOVIE_VERSIONS}) == 1, timeout=120
    )
    p1.plex_wait_idle()
    merged_job, merged_files = run_job(
        {"file_paths": [MOVIE_FOLDER], "library_name": "Phase 2 row 14 merged Plex item"}
    )
    item = plex_item(MOVIE_VERSIONS[0])
    served = p1.plex_served(item)
    recorded = app_item_files("mlab-plex", item)
    rows = [server_row(merged_files, v.rsplit("/", 1)[-1], "mlab-plex") for v in MOVIE_VERSIONS]
    checks["job after the Plex merge completed"] = merged_job["status"] == "completed"
    checks["Plex: one item with both versions serves credits 100–120 s"] = plex_media_count(item) == 2 and [
        (m["type"], m["start"], m["end"]) for m in served
    ] == [("credits", 100_000, 120_000)]
    checks["the app's record of the merged item lists both version files"] = sorted(recorded or []) == sorted(
        MOVIE_VERSIONS
    )
    checks["both versions' Plex rows written or up to date after the merge"] = all(
        r.get("status") in ("markers_written", "markers_up_to_date") for r in rows
    )
    detail["mlab-plex"] = {"separate": separate, "recorded_separate": recorded_separate, "merged_item": item, "media": plex_media_count(item), "served": served,
                           "recorded_files": recorded, "rows_after_merge": rows}  # fmt: skip
    for sid in EMBY_SERVERS:
        emby = emby_served_check(sid, list(MOVIE_VERSIONS))
        detail[sid] = emby
        checks[f"{sid}: each version item CreditsStart 100 s"] = len({v["item"] for v in emby.values()}) == 2 and all(v["ok"] and v["got"] == [{"type": "CreditsStart", "ms": 100_000}] for v in emby.values())  # fmt: skip
    notes = [f"{k}: {v}" for k, v in detail.items()] + [
        f"rows {[(f['file'].rsplit('/', 1)[-1], [(s['id'], s['status']) for s in f['servers']]) for f in files + merged_files]}"
    ]
    return checks_result(
        14, "Movie versions on every server (L263)", checks, {"detail": detail, "files": files + merged_files}, notes
    )


@row(15)
def row_15_cassettes() -> dict:
    """The markers cassettes replay with the lab servers they were recorded from stopped."""
    tests = [
        t
        for t in ("tests/test_servers_markers_vcr.py", "tests/test_servers_emby_markers_vcr.py", "tests/test_servers_jellyfin_vcr.py")
        if (REPO / t).exists()
    ]  # fmt: skip
    stopped = ("mlab-plex", "mlab-jellyfin", "mlab-emby")
    try:
        sh("docker", "stop", *stopped, timeout=300)
        out = subprocess.run(
            [p1.VENV_PYTHON, "-m", "pytest", "--no-cov", "-n", "0", "-q", "--record-mode=none", *tests],
            cwd=REPO, capture_output=True, text=True, timeout=900,
        )  # fmt: skip
    finally:
        sh("docker", "start", *stopped, timeout=300)
        p1.jf_wait_healthy("mlab-jellyfin")
        emby_wait_healthy("mlab-emby")
        wait_until("Plex to answer", lambda: _answers(p1.PLEX), timeout=300, every=5)
        p1.plex_wait_idle()
    tail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[-300:]
    checks = {"pytest passed with the servers stopped": out.returncode == 0}
    return checks_result(
        15, "Cassette replay with lab servers stopped", checks, {"tests": tests, "tail": tail}, [f"{tests}: {tail}"]
    )


def _answers(base: str) -> bool:
    try:
        return http("GET", f"{base}/identity", timeout=5)[0] == 200
    except OSError:
        return False


SEASON_VIEW_SCRIPT = """
import asyncio, json, sys
from playwright.async_api import async_playwright
APP, SHOT, EPISODE = sys.argv[1], sys.argv[2], sys.argv[3]
async def main():
    out = {}
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={"width": 1500, "height": 1100})
        await pg.goto(f"{APP}/login"); await pg.fill("#token", sys.stdin.read().strip()); await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(3000); await pg.goto(f"{APP}/bif-viewer")
        await pg.wait_for_function("() => document.querySelector('#serverSelect option[value=mlab-plex]')", timeout=20000)
        await pg.select_option("#serverSelect", "mlab-plex")
        await pg.fill("#searchInput", "Synth Chapters"); await pg.click("#searchBtn")
        result = pg.locator(f'.result-item[data-media-file="{EPISODE}"]')
        await result.first.wait_for(timeout=30000); await result.first.click()
        await pg.click("#inspectorMarkersTabBtn"); await pg.wait_for_timeout(2000)
        await pg.click("label[for='markersViewSeason']")
        body = pg.locator("#markersSeasonBody")
        await pg.wait_for_function("() => { const b = document.querySelector('#markersSeasonBody'); return b && b.innerText && !b.innerText.includes('Loading') }", timeout=30000)
        await pg.wait_for_timeout(1500)
        text = lambda sel: body.locator(sel).first.inner_text()
        out["title"] = await text(".mk-season-title"); out["sub"] = await text(".mk-season-sub")
        out["ready"] = await text(".mk-season-ready")
        out["publish"] = await text("#markersSeasonPublishBtn")
        out["rows"] = await pg.evaluate("() => Array.from(document.querySelectorAll('#markersSeasonBody tr[data-episode]')).map(r => [r.dataset.episode, Array.from(r.querySelectorAll('.mk-dot')).map(d => d.className + ' | ' + (d.title || ''))])")
        await pg.screenshot(path=SHOT, full_page=True)
        await pg.click("#markersSeasonPublishBtn"); await pg.wait_for_timeout(3000)
        out["toast"] = await pg.evaluate("() => (document.getElementById('toastBody') || {}).innerText || ''")
        await pg.screenshot(path=SHOT.replace('.png', '-published.png'), full_page=True)
        await b.close()
    print(json.dumps(out))
asyncio.run(main())
"""


@row(16)
def row_16_season_view() -> dict:
    """The Season view in the real app (R4, controller note 3): Synth Chapters S01 lists its episodes, "Publish N to 5
    servers" with N = the ready count, every published dot green; Publish queues a NORMAL job named "Intro & Credits:
    Synth Chapters (2021) · Season 1" that completes."""
    p1.SHOTS.mkdir(parents=True, exist_ok=True)
    episode = p1.synth_path(1)
    api = app_ok("GET", f"/api/markers/season?{urllib.parse.urlencode({'path': episode})}")
    t0 = now_iso()
    shot = p1.SHOTS / "p2-row16-season-view.png"
    run = subprocess.run(
        [p1.VENV_PYTHON, "-c", SEASON_VIEW_SCRIPT, p1.APP, str(shot), episode],
        input=ENV["MLAB_APP_TOKEN"], capture_output=True, text=True, timeout=240,
    )  # fmt: skip
    try:
        seen = json.loads(run.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return write_result(
            16, "Season view in the real app", "fail", {"error": scrub((run.stdout + run.stderr)[-1500:])}
        )
    wanted_name = "Intro & Credits: Synth Chapters (2021) · Season 1"
    job = wait_until(
        "the Publish job",
        lambda: next((j for j in p1.jobs_since(t0) if j["library_name"] == wanted_name), None),
        timeout=60,
    )
    job = p1.wait_job(job["id"], timeout=900)
    ready = api["counts"]["ready"]
    dots_ok = all(
        all("mk-dot-ok" in d for d in dots)
        for ep, dots in seen["rows"]
        if next(e for e in api["episodes"] if e["episode"] == ep)["intro"]["status"] == "decided"
    )
    checks = {
        "title Synth Chapters (2021) · Season 1": seen["title"] == "Synth Chapters (2021) · Season 1",
        f"Publish {ready} to 5 servers": seen["publish"] == f"Publish {ready} to 5 servers" and ready > 0,
        f"{ready} ready": seen["ready"] == f"{ready} ready",
        "every decided episode's five dots green": dots_ok and all(len(d) == 5 for _, d in seen["rows"]),
        "Publish queued the named job at NORMAL": job["priority"] == 2,
        "it completed": job["status"] == "completed",
    }
    notes = [f"page {seen}", f"api counts {api['counts']}; job {job['id'][:8]} {job['status']} {job['progress'].get('outcome')}", f"screenshots {shot}"]  # fmt: skip
    return checks_result(16, "Season view in the real app", checks, {"seen": seen, "api": api, "job": job}, notes)


@row(17)
def row_17_security() -> dict:
    """Without the token the phase 2 routes answer 401; the Season route refuses paths outside the libraries (400)."""
    unauth = {
        "GET /api/markers/season?path=x": app("GET", "/api/markers/season?path=x", auth=False)[0],
        "POST /api/markers/season/publish": app("POST", "/api/markers/season/publish", {"path": "x"}, auth=False)[0],
        "GET /api/markers/sources/local": app("GET", "/api/markers/sources/local", auth=False)[0],
        "POST /api/markers/reconcile": app("POST", "/api/markers/reconcile", {}, auth=False)[0],
    }
    traversal = {
        raw: app("GET", f"/api/markers/season?{urllib.parse.urlencode({'path': raw})}")
        for raw in ("/etc/passwd", f"{AUDIO_ROOT}/../../etc/passwd")
    }
    checks = {f"{k} -> 401": v == 401 for k, v in unauth.items()}
    checks.update({f"season path {k} -> 400": v[0] == 400 for k, v in traversal.items()})
    return checks_result(
        17, "Security of the phase 2 routes", checks, {"unauthenticated": unauth, "traversal": traversal}
    )


@row(18)
def row_18_resources() -> dict:
    """A forced job on Rick and Morty S01 with its fingerprints deleted: at most two chromaprint ffmpeg at once with
    -threads 2; peak CPU and memory next to phase 1 row 15 (86 %, 99 MiB)."""
    deleted = delete_fingerprints(p1.RICK_SEASON)
    sampler, stats = ChromaprintSampler(), p1.StatsSampler()
    sampler.start()
    stats.start()
    try:
        job, _ = run_job(
            {"file_paths": [p1.RICK_SEASON], "force": True, "library_name": "Phase 2 row 18 resources"}, timeout=3600
        )
    finally:
        sampler.stop.set()
        stats.stop.set()
        sampler.join()
        stats.join(timeout=30)
    cpu = [float(s["cpu"].rstrip("%")) for s in stats.samples]
    mem = [p1._mib(s["mem"]) for s in stats.samples]
    checks = {
        "fingerprints deleted": deleted not in ("", "0"),
        "job completed": job["status"] == "completed",
        "chromaprint ffmpeg ran": sampler.peak > 0,
        "at most two at once with -threads 2": sampler.peak <= 2 and sampler.threads_ok(),
    }
    duration = job["started_at"] and job["completed_at"]
    notes = [
        f"fingerprint rows deleted {deleted}; job {job['id'][:8]} {job['started_at']} -> {job['completed_at']} ({duration and 'done'})",
        f"chromaprint peak {sampler.peak}, argv {' '.join(sampler.argv)}",
        f"{len(stats.samples)} docker stats samples: peak CPU {max(cpu, default=0):.1f}%, peak memory {max(mem, default=0):.0f} MiB (phase 1 row 15: 86 %, 99 MiB)",
    ]
    evidence = {"peak_cpu": max(cpu, default=None), "peak_mem_mib": max(mem, default=None), "chromaprint_peak": sampler.peak, "argv": sampler.argv, "job": job}  # fmt: skip
    return checks_result(18, "Resources during a forced Rick and Morty S01 job", checks, evidence, notes)


@row(19)
def row_19_phase1_regression() -> dict:
    """Phase 1's matrix on the phase 2 lab (row 11 unit-only, row 12 is row 20 here, row 15 is row 18 here), from a fresh
    markers.db. Plex's own detection is on for its duration (rows 5 and 16 need it). Row 17 runs in two calls around its
    600 s verify wait, on S01E03 (ROW17_EPISODE): row 9 sends S01E02's webhook minutes earlier and the app drops a repeat
    of the same file for 600 s. Earlier phase 1 results are moved to results/phase1-before-phase2/ first.
    P2_ROW19_COLLECT=1 only collects the phase 1 rows' result files (after re-running single rows by hand)."""
    numbers = (*LOCAL_ROWS, 17)
    if not os.environ.get("P2_ROW19_COLLECT"):
        archive = RESULTS / "phase1-before-phase2"
        if not archive.exists():
            archive.mkdir(parents=True)
            for path in [*RESULTS.glob("row-*.json"), RESULTS / "baseline-before-row-01.json", RESULTS / "status.json"]:
                if path.exists():
                    path.rename(archive / path.name)
        os.environ.setdefault("ROW17_EPISODE", "3")
        with PlexDetection():
            for number in LOCAL_ROWS:
                try:
                    p1.ROWS[number]()
                except Exception as exc:  # a crashed row is a failed row; the regression keeps going
                    say(f"phase 1 row {number} crashed: {exc}")
        p1.ROWS[17]()
        # Row 17's second call waits at most 560 s for the verify job, which waits 600 s from the first call.
        say("row 17: waiting for its verify job's delay")
        time.sleep(600)
        p1.ROWS[17]()
    results = {}
    for number in numbers:
        path = RESULTS / f"row-{number:02d}.json"
        results[number] = json.loads(path.read_text())["result"] if path.exists() else "missing"
    checks = {f"phase 1 row {n}": r == "pass" for n, r in results.items()}
    return checks_result(19, "Phase 1 regression", checks, {"results": results}, [f"results {results}"])


@row(20)
def row_20_plex_app() -> dict:
    """Plex apps show Skip Intro / Skip Credits on the lab Plex (ledger L276): needs the owner."""
    return write_result(20, "Plex app shows Skip Intro / Skip Credits (ledger L276)", "needs owner", {
        "ask": "Open Synth Chapters S01E02 on the claimed lab Plex in any Plex app: Skip Intro at 0:17 and Skip Credits at "
        "1:40. Synth Audio S01E02 has no intro to skip (season audio never decides alone, R2/G3).",
    })  # fmt: skip


@row(21)
def row_21_check_servers_schedule() -> dict:
    """Fresh config: no schedules. A saved Check servers schedule run twice at once queues exactly one LOW Check servers
    job; the schedule is deleted afterwards."""
    existing = app_ok("GET", "/api/schedules")["schedules"]
    t0 = now_iso()
    schedule = app_ok("POST", "/api/schedules", {
        "name": "Phase 2 row 21 Check servers", "interval_minutes": 720,
        "config": {"job_type": "intro_credits", "reconcile": True},
    })  # fmt: skip
    try:
        runs = [app("POST", f"/api/schedules/{schedule['id']}/run")[0] for _ in range(2)]
        time.sleep(5)
        jobs = [j for j in p1.jobs_since(t0) if (j.get("config") or {}).get("reconcile")]
        for job in jobs:
            p1.wait_job(job["id"], timeout=1800)
        stored = app_ok("GET", f"/api/schedules/{schedule['id']}")
    finally:
        deleted = app("DELETE", f"/api/schedules/{schedule['id']}")[0]
    checks = {
        "no schedules on a fresh config": existing == [],
        "both Run now calls accepted": runs == [200, 200],
        "exactly one Check servers job": len(jobs) == 1,
        "it is LOW and linked to the schedule": len(jobs) == 1 and jobs[0]["priority"] == 3 and jobs[0]["parent_schedule_id"] == schedule["id"],
        "schedule deleted": deleted == 200 and app_ok("GET", "/api/schedules")["schedules"] == [],
    }  # fmt: skip
    notes = [f"jobs {[(j['id'][:8], j['library_name'], j['priority'], j['parent_schedule_id'][:8]) for j in jobs]}", f"schedule last_run {stored.get('last_run')}"]  # fmt: skip
    return checks_result(
        21, "Check servers schedule on a fresh config", checks, {"schedule": schedule, "jobs": jobs}, notes
    )


@row(22)
def row_22_deleted_jellyfin_item() -> dict:
    """A published Jellyfin item deleted (the phase 1 multi-version copies removed from the lab folder and scanned):
    Check servers raises no "Couldn't read" warning for it, and the run after lists nothing."""
    copies = [p1.synth_path(1, " - Extended"), p1.synth_path(3, " - Copy")]
    published = {p.rsplit("/", 1)[-1]: inspector_server(p, "mlab-jellyfin").get("published") for p in copies}
    items = {p.rsplit("/", 1)[-1]: (p1.jf_items("mlab-jellyfin").get(p) or {}).get("id") for p in copies}
    for path in copies:
        (p1.SYNTH_HOST_SEASON / path.rsplit("/", 1)[-1]).unlink(missing_ok=True)
    removed = rescan_until("Synth Chapters", copies, present=False)
    first, files, _ = reconcile_job()
    second, second_files, _ = reconcile_job()
    warnings = {j["id"][:8]: job_warnings(j["id"]) for j in (first, second)}
    checks = {
        "the copies were published to Jellyfin 10.11": all(published.values()),
        "both runs completed": first["status"] == second["status"] == "completed",
        "no Couldn't read warning": not any("Couldn't read" in w for ws in warnings.values() for w in ws),
        "the run after lists nothing": not second_files,
    }
    notes = [
        f"deleted items {items}; first run files {[f['file'].rsplit('/', 1)[-1] for f in files]}",
        f"warnings {warnings}",
    ]
    return checks_result(22, "Check servers and a deleted Jellyfin item", checks, {"removed": removed, "files": files, "warnings": warnings}, notes)  # fmt: skip


@row(23)
def row_23_emby_replacing_round_trip() -> dict:
    """Controller note 9: emby_plugin_check check 22 on both Embys — the plugin's own serialiser writes a nested Replacing
    (read while a POST waits in Emby's chapter write, then the container is killed) and Load reads it back."""
    import emby_plugin_check as epc

    results = {}
    for container in EMBY_SERVERS:
        check = epc.EmbyCheck(container)
        check.run({22})
        results[container] = next((r for r in check.results if r["check"] == 22), {"result": "FAIL"})
    checks = {f"{c}: check 22 PASS": r["result"] == "PASS" for c, r in results.items()}
    notes = [f"{c}: store file while held {r.get('store_file_while_held')}; POST after restart {r.get('post_after_restart', {}).get('body')}" for c, r in results.items()]  # fmt: skip
    return checks_result(
        23, "Emby plugin Replacing round trip through a killed write", checks, {"results": results}, notes
    )


def main(argv: list[str]) -> int:
    # A stopped run (SIGTERM) still runs the finally blocks that start containers again and put lab state back.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if not argv or argv[0] not in ("configure", "run", "rows"):
        print(__doc__)
        return 2
    if argv[0] == "configure":
        configure()
        return 0
    if argv[0] == "rows":
        for number, fn in sorted(ROWS.items()):
            print(f"{number:>2}  {(fn.__doc__ or '').strip().splitlines()[0]}")
        return 0
    failed = 0
    for number in [int(a) for a in argv[1:]]:
        failed += ROWS[number]()["result"] == "fail"
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
