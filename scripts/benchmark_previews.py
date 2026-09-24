#!/usr/bin/env python3
"""Time Plex's built-in preview generation against this app, on the same files and the same machine.

Runs on `storage` against the site lab (docs/design/site-redesign-lab/, plan Task 4): the lab Plex
`mlab-plex` (given the GPU, hardware acceleration on) and this branch's app `mlab-site-app`, the
Open Films library, the same frame interval and JPEG quality. Appends raw timings to
docs/benchmark/results.csv and records the setup in docs/benchmark/environment.json; `summary`
writes docs/benchmark/summary.json. The site may quote a speed number only from summary.json.

    scripts/benchmark_previews.py environment
    for round in 1 2 3; do                     # interleaved, so each tool sees the same background load
        for tool in app-gpu app-cpu plex; do scripts/benchmark_previews.py "$tool" --runs 1; done
    done
    scripts/benchmark_previews.py summary
    scripts/benchmark_previews.py restore      # the app's own Plex previews again, at its normal settings

`--interval N` sets both tools to one frame every N seconds (Plex's pref too, put back afterwards)
and keeps that scenario's results.csv and summary.json in docs/benchmark/interval-<N>s/. Without it
both use Plex's own setting, and only that default scenario feeds docs/benchmark/summary.json.

Every run first waits until no lab app has a job and the lab Plex has no activity. It then starts
with no Plex preview files for these films (this script deletes them in Plex's data folder, inside
the lab's own Docker volume) and with the films already in the page cache (each is read once first),
so disk speed isn't what gets measured. `storage` keeps running its own services during a run, so
each row's notes record how busy the host's CPUs and the GPU were.

- Plex: the clock starts when the first `Plex Transcoder` process working on one of the films shows
  up in `docker top mlab-plex` (polled every 0.25 s) and stops at the newest index-sd.bif mtime. No
  such process within 180 s of starting Plex's GenerateMediaIndexFiles task = `untimed`, and so is
  a run where Plex goes idle without writing every preview.
- App: the job's own started_at to completed_at, Plex output only (Jellyfin and Emby are disabled
  in the app for the run, so it writes the same one preview file per film that Plex writes). The
  app's running worker pool is resized to the run's worker counts. The job is a manual one with
  force_regenerate: the app's frame cache would otherwise hand runs 2 and 3 the frames run 1
  extracted. A run is `timed` only if the job extracted frames for every film and published them.

Same interval is not always the same decoding work: when a film's keyframes are further apart than
the interval, the app decodes every frame so each thumbnail is a different picture, while Plex
decodes keyframes only (`-skip_frame nokey`) and repeats the last one. The app logs "Slow path" for
each such film. At an interval no shorter than every film's longest keyframe gap, both decode
keyframes only.

While a Plex run is going, Plex's preview generation is on for Open Films only: every other library
that has the setting on is switched off. Each Plex and app setting the script changes is put back
to its prior value and read back afterwards; a mismatch stops the script.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
LAB = REPO_ROOT / "docs" / "design" / "site-redesign-lab"
BENCH = REPO_ROOT / "docs" / "benchmark"
ENVIRONMENT = BENCH / "environment.json"
PLEXDB = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/plexdb.sh")
PLEX_DATA = "/config/Library/Application Support/Plex Media Server"
TOOLS = ("plex-builtin", "app-gpu", "app-cpu")
MIN_RUNS = 3
MAX_SPREAD = 0.25
APP_CPU_WORKERS = 4
# The app's defaults for a detected GPU it has no stored settings for (job_runner._build_selected_gpus).
APP_GPU_WORKERS = 1
APP_FFMPEG_THREADS = 2
# Plex's transcoder writes preview JPEGs with `-q 3`; the app's thumbnail_quality is the same -q:v scale.
PLEX_JPEG_QUALITY = 3
APP_FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"  # the binary the app runs (config._JELLYFIN_FFMPEG_PATH)
LAB_APPS = ((18080, "MLAB_APP_TOKEN"), (18081, "MLAB_APP_REMOTE_TOKEN"), (18082, "MLAB_APP_HEALTH_TOKEN"))
SITE_APP_OTHER_SERVERS = ("site-jellyfin", "site-emby")
APP_KEYS = ("thumbnail_interval", "thumbnail_quality", "cpu_threads", "gpu_config")
FIELDS = ["tool", "run", "status", "files", "wall_seconds", "started_at", "finished_at", "notes"]


def read_rows(path: Path) -> list[dict[str, str]]:
    """Rows of results.csv, or an empty list if it doesn't exist yet."""
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def summarize(rows: list[dict[str, str]]) -> dict:
    """Medians per tool and the Plex / app-GPU ratio, or no ratio when it can't be trusted.

    Args:
        rows: results.csv rows (`tool`, `run`, `status`, `wall_seconds`, ...).

    Returns:
        {"tools": {tool: {"runs", "timed", "median_seconds"}}, "ratio_plex_over_app_gpu",
        "ratio_display", "reason"}; the two ratio fields are None unless both Plex and app-GPU have
        at least MIN_RUNS runs, every one of them timed, each set within MAX_SPREAD of its median.
    """
    tools: dict[str, dict] = {}
    spread: dict[str, float] = {}
    for tool in TOOLS:
        mine = [row for row in rows if row["tool"] == tool]
        timed = [float(row["wall_seconds"]) for row in mine if row["status"] == "timed"]
        median = statistics.median(timed) if timed else None
        tools[tool] = {"runs": len(mine), "timed": len(timed), "median_seconds": median}
        spread[tool] = (max(timed) - min(timed)) / median if timed and median else 0.0

    def refuse(reason: str) -> dict:
        return {"tools": tools, "ratio_plex_over_app_gpu": None, "ratio_display": None, "reason": reason}

    plex, gpu = tools["plex-builtin"], tools["app-gpu"]
    if plex["runs"] < MIN_RUNS or plex["timed"] != plex["runs"]:
        return refuse(f"Plex's built-in generation was not timed reliably in {MIN_RUNS} runs")
    if gpu["runs"] < MIN_RUNS or gpu["timed"] != gpu["runs"]:
        return refuse(f"the app's GPU runs did not all complete ({MIN_RUNS} needed)")
    if spread["plex-builtin"] > MAX_SPREAD or spread["app-gpu"] > MAX_SPREAD:
        return refuse(f"runs varied by more than {MAX_SPREAD:.0%} of their median")
    ratio = plex["median_seconds"] / gpu["median_seconds"]
    return {
        "tools": tools,
        "ratio_plex_over_app_gpu": round(ratio, 2),
        "ratio_display": f"{ratio:.1f}",
        "reason": f"{MIN_RUNS}+ timed runs each, within {MAX_SPREAD:.0%}",
    }


# ------------------------------------------------------------------------------------------- lab I/O


def _lab() -> ModuleType:
    sys.path.insert(0, str(LAB))
    import lab_setup  # the lab's API helpers, tokens and constants

    return lab_setup


def _sh(*command: str, check: bool = True) -> str:
    return subprocess.run(command, capture_output=True, text=True, check=check).stdout


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def _append(row: dict, results: Path) -> None:
    results.parent.mkdir(parents=True, exist_ok=True)
    new = not results.is_file()
    with results.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        if new:
            writer.writeheader()
        writer.writerow(row)
    print(f"{row['tool']} run {row['run']}: {row['status']} {row['wall_seconds']} s {row['notes']}", flush=True)


def _part_hashes() -> dict[str, str]:
    """Film path -> Plex bundle hash, read from the lab Plex's own database."""
    out = _sh(str(PLEXDB), "select file, hash from media_parts where file like '/media/openfilms/%';")
    return dict(line.split("|", 1) for line in out.splitlines() if "|" in line)


def _bif(hash_: str) -> str:
    return f"{PLEX_DATA}/Media/localhost/{hash_[0]}/{hash_[1:]}.bundle/Contents/Indexes/index-sd.bif"


def _bif_stats(bifs: list[str]) -> list[tuple[float, int] | None]:
    """(mtime, image count from the BIF header) per file, None where it's missing or half-written."""
    script = (
        'for f in "$@"; do if [ -f "$f" ]; then printf "%s " "$(date -r "$f" +%s.%N)"; '
        'head -c 16 "$f" | od -An -tu4 -j12 -N4; else echo -; fi; done'
    )
    out = _sh("docker", "exec", "mlab-plex", "sh", "-c", script, "sh", *bifs)
    stats: list[tuple[float, int] | None] = []
    for line in out.splitlines():
        parts = line.split()
        stats.append((float(parts[0]), int(parts[1])) if len(parts) == 2 else None)
    return stats


def _clear_previews() -> list[str]:
    bifs = [_bif(h) for h in _part_hashes().values()]
    _sh("docker", "exec", "mlab-plex", "rm", "-f", *bifs)
    return bifs


def _warm_cache(lab: ModuleType) -> None:
    for path in sorted(lab.MEDIA_HOST.glob("*/*")):
        if path.suffix.lower() in lab.VIDEO_SUFFIXES:
            subprocess.run(["cat", str(path)], stdout=subprocess.DEVNULL, check=True)


def _cpu_times() -> tuple[int, int]:
    """(busy, total) jiffies over all CPUs since boot; iowait counts as idle."""
    user, nice, system, idle, iowait, irq, softirq, steal = (
        int(v) for v in Path("/proc/stat").read_text(encoding="utf-8").split()[1:9]
    )
    busy = user + nice + system + irq + softirq + steal
    return busy, busy + idle + iowait


def _cpu_busy(seconds: float = 5) -> float:
    """Fraction of all the host's CPU time that was busy over the next `seconds`."""
    busy, total = _cpu_times()
    time.sleep(seconds)
    busy2, total2 = _cpu_times()
    return (busy2 - busy) / max(total2 - total, 1)


def _gpu_processes() -> list[tuple[str, int, int]]:
    """(command, sm %, decoder %) for every process on the GPU, from one `nvidia-smi pmon` sample."""
    out = _sh("nvidia-smi", "pmon", "-c", "1", "-s", "u", check=False)
    columns: list[str] = []
    found = []
    for line in out.splitlines():
        if line.startswith("# gpu"):
            columns = line[1:].split()
            continue
        if line.startswith("#") or not columns:
            continue
        values = line.split(None, len(columns) - 1)
        if len(values) < len(columns) or values[1] == "-":
            continue
        row = dict(zip(columns, values, strict=True))

        def pct(key: str, row: dict = row) -> int:
            return int(row[key]) if row.get(key, "-").isdigit() else 0

        found.append((row["command"].strip(), pct("sm"), pct("dec")))
    return found


class _Sampler(threading.Thread):
    """Watches the host while a run is timed: how busy its CPUs are, and which processes use the GPU."""

    def __init__(self, ours: tuple[str, ...]) -> None:
        super().__init__(daemon=True)
        self.ours = ours
        self.cpu_busy = 0.0
        self.ours_on_gpu = False
        self.ours_decoder_max = 0
        self.others_gpu_max = 0
        self._halt = threading.Event()

    def run(self) -> None:
        busy, total = _cpu_times()
        while not self._halt.is_set():
            for command, sm, dec in _gpu_processes():
                if command.startswith(self.ours):
                    self.ours_on_gpu = True
                    self.ours_decoder_max = max(self.ours_decoder_max, dec)
                else:
                    self.others_gpu_max = max(self.others_gpu_max, sm, dec)
            self._halt.wait(1)
        busy2, total2 = _cpu_times()
        self.cpu_busy = (busy2 - busy) / max(total2 - total, 1)

    def stop(self) -> None:
        self._halt.set()
        self.join()


def _busy(lab: ModuleType) -> str:
    """Why the lab isn't quiet enough to time a run, or '' when it is."""
    apps = [(port, lab.ENV[key]) for port, key in LAB_APPS] + [(18083, lab.ENV["MLAB_SITE_APP_TOKEN"])]
    for port, token in apps:
        status = lab.http("GET", f"http://127.0.0.1:{port}/api/system/status", headers={"X-Auth-Token": token})
        if status.get("running_job") or status.get("pending_jobs"):
            return f"a job on the lab app at :{port}"
    if lab.plex("GET", "/activities")["MediaContainer"].get("size"):
        return "the lab Plex has activities running"
    return ""


def _wait_quiet(lab: ModuleType, timeout: float = 6 * 3600) -> None:
    deadline = time.monotonic() + timeout
    reported = 0.0
    while why := _busy(lab):
        if time.monotonic() > deadline:
            raise SystemExit(f"the lab never went quiet enough to time a run: {why}")
        if time.monotonic() - reported > 300:
            print(f"waiting for a quiet lab: {why}", flush=True)
            reported = time.monotonic()
        time.sleep(10)


def _plex_prefs(lab: ModuleType) -> dict[str, dict]:
    return {s["id"]: s for s in lab.plex("GET", "/:/prefs")["MediaContainer"]["Setting"]}


def _plex_value(value: object) -> object:
    """A pref value as Plex takes it in a PUT: booleans as 1/0 (it reports them as True or 'true')."""
    if isinstance(value, bool):
        return int(value)
    return {"true": 1, "false": 0}.get(value, value) if isinstance(value, str) else value


def _section_bif_settings(lab: ModuleType) -> dict[str, object]:
    """Section key -> enableBIFGeneration, for the sections that have the setting at all."""
    found = {}
    for section in lab.plex("GET", "/library/sections")["MediaContainer"]["Directory"]:
        prefs = lab.plex("GET", f"/library/sections/{section['key']}/prefs")["MediaContainer"]["Setting"]
        value = next((s["value"] for s in prefs if s["id"] == "enableBIFGeneration"), None)
        if value is not None:
            found[section["key"]] = value
    return found


def _transcoders_on_films() -> list[str]:
    out = _sh("docker", "top", "mlab-plex", "-eo", "pid,args", check=False)
    return [line for line in out.splitlines() if "Plex Transcoder" in line and "/media/openfilms/" in line]


# ------------------------------------------------------------------------------------------------ runs


def run_plex(run: int, interval: int | None, results: Path) -> None:
    """Time one run of Plex's own GenerateMediaIndexFiles task over the films and append it to `results`.

    Args:
        run: Run number recorded in the row.
        interval: Seconds between frames, set on Plex for the run; None keeps Plex's own setting.
        results: The scenario's results.csv.
    """
    lab = _lab()
    prefs = _plex_prefs(lab)
    target = {"GenerateBIFBehavior": "scheduled", "HardwareAcceleratedCodecs": True}
    if interval:
        target["GenerateBIFFrameInterval"] = interval
    missing = [pref for pref in (*target, "GenerateBIFFrameInterval") if pref not in prefs]
    if missing:
        ids = sorted(pref for pref in prefs if "BIF" in pref or "Hardware" in pref)
        raise SystemExit(f"Plex has no pref {missing}; BIF/hardware prefs it does have: {ids}. Update this script.")
    ours = lab.plex_section()
    saved_sections = _section_bif_settings(lab)
    if ours not in saved_sections:
        raise SystemExit(f"Plex section {ours} has no enableBIFGeneration setting")
    wanted_sections = {key: key == ours for key in saved_sections}
    changed_sections = {k: v for k, v in saved_sections.items() if _plex_value(v) != _plex_value(wanted_sections[k])}
    changed_server = {k: prefs[k]["value"] for k in target if _plex_value(prefs[k]["value"]) != _plex_value(target[k])}
    films = lab.films()
    interval = interval or prefs["GenerateBIFFrameInterval"]["value"]

    _wait_quiet(lab)
    busy_before = _cpu_busy()
    bifs = _clear_previews()
    _warm_cache(lab)
    sampler = _Sampler(("Plex Transcod",))
    try:
        for key in changed_sections:
            lab.plex("PUT", f"/library/sections/{key}/prefs", enableBIFGeneration=_plex_value(wanted_sections[key]))
        if changed_server:
            lab.plex("PUT", "/:/prefs", **{k: _plex_value(target[k]) for k in changed_server})
        sampler.start()
        lab.plex("POST", "/butler/GenerateMediaIndexFiles")
        asked = time.time()
        started, first_args = None, ""
        while time.time() - asked < 180:
            if procs := _transcoders_on_films():
                started, first_args = time.time(), procs[0]
                break
            time.sleep(0.25)
        if started is None:
            _append({"tool": "plex-builtin", "run": run, "status": "untimed", "files": len(films), "wall_seconds": "",
                     "started_at": _now_iso(asked), "finished_at": "",
                     "notes": "no Plex Transcoder on the films within 180 s"}, results)  # fmt: skip
            return
        idle_since = None
        while True:
            stats = _bif_stats(bifs)
            busy = bool(_transcoders_on_films()) or bool(lab.plex("GET", "/activities")["MediaContainer"].get("size"))
            if all(s is not None and s[0] >= asked for s in stats) and not busy:
                break
            idle_since = None if busy else (idle_since or time.time())
            if idle_since and time.time() - idle_since > 120:
                written = sum(s is not None and s[0] >= asked for s in stats)
                _append({"tool": "plex-builtin", "run": run, "status": "untimed", "files": len(films),
                         "wall_seconds": "", "started_at": _now_iso(started), "finished_at": "",
                         "notes": f"Plex went idle with {written}/{len(films)} previews written"}, results)  # fmt: skip
                return
            if time.time() - started > 4 * 3600:
                raise SystemExit("Plex took over 4 hours; stopping (run recorded nothing)")
            time.sleep(1)
        sampler.stop()
        finished = max(s[0] for s in stats)
        notes = (
            f"interval={interval}s frames={sum(s[1] for s in stats)} plex_gpu_seen={sampler.ours_on_gpu} "
            f"hwaccel_in_args={'-hwaccel' in first_args} butler_to_first_transcoder={started - asked:.1f}s "
            f"host_cpu_busy_before={busy_before:.0%} host_cpu_busy_during={sampler.cpu_busy:.0%} "
            f"other_gpu_max={sampler.others_gpu_max}%"
        )
        _append({"tool": "plex-builtin", "run": run, "status": "timed", "files": len(films),
                 "wall_seconds": f"{finished - started:.1f}", "started_at": _now_iso(started),
                 "finished_at": _now_iso(finished), "notes": notes}, results)  # fmt: skip
    finally:
        if sampler.is_alive():
            sampler.stop()
        for key, value in changed_sections.items():
            lab.plex("PUT", f"/library/sections/{key}/prefs", enableBIFGeneration=_plex_value(value))
        if changed_server:
            lab.plex("PUT", "/:/prefs", **{k: _plex_value(v) for k, v in changed_server.items()})
        now_prefs = _plex_prefs(lab)
        now_sections = _section_bif_settings(lab)
        drift = [k for k, v in changed_server.items() if now_prefs[k]["value"] != v]
        drift += [f"section {k}" for k, v in saved_sections.items() if now_sections.get(k) != v]
        if drift:
            raise SystemExit(f"Plex settings did not come back: {drift}")


def _app_settings(lab: ModuleType) -> dict:
    return {k: v for k, v in lab.app("GET", "/api/settings").items() if k in APP_KEYS}


def _app_server_enabled(lab: ModuleType, server: str) -> bool:
    stored = lab.app("GET", f"/api/servers/{server}")
    return bool(stored.get("server", stored)["enabled"])


def _live_workers(lab: ModuleType) -> dict[str, int]:
    listed = lab.app("GET", "/api/jobs/workers")
    counts = {"GPU": 0, "CPU": 0}
    for worker in listed.get("workers", listed) if isinstance(listed, dict) else listed:
        counts[worker["worker_type"]] += 1
    return counts


def _set_live_workers(lab: ModuleType, wanted: dict[str, int]) -> None:
    """Resize the app's running worker pool to `wanted`.

    Saving cpu_threads only changes what a fresh pool is built with; the running pool keeps its CPU
    workers until they are added or removed the way the dashboard's worker buttons do it.
    """
    for kind, count in wanted.items():
        diff = count - _live_workers(lab)[kind]
        if diff:
            lab.app(
                "POST", f"/api/workers/{'add' if diff > 0 else 'remove'}", {"worker_type": kind, "count": abs(diff)}
            )
    if (live := _live_workers(lab)) != wanted:
        raise SystemExit(f"the app's worker pool is {live}, wanted {wanted}")


def _job_problems(job: dict, count: int) -> list[str]:
    """Why an app job doesn't count as a full run: not completed, or not every film extracted and published."""
    problems = [] if job["status"] == "completed" else [f"status={job['status']}"]
    outcome = job.get("progress", {}).get("outcome", {})
    if outcome.get("generated") != count or outcome.get("failed"):
        problems.append(f"generated={outcome.get('generated')} failed={outcome.get('failed')}")
    publishers = {p["server_id"]: p for p in job.get("publishers", [])}
    if set(publishers) != {"site-plex"}:
        problems.append(f"published to {sorted(publishers)}")
    plex = publishers.get("site-plex", {})
    if plex.get("frame_sources") != {"extracted": count} or plex.get("counts", {}).get("published") != count:
        problems.append(f"frame_sources={plex.get('frame_sources')} counts={plex.get('counts')}")
    return problems


def _run_app_job(lab: ModuleType, films: list[str], title: str) -> dict:
    job = lab.app("POST", "/api/jobs/manual", {"file_paths": films, "force_regenerate": True})

    def finished() -> dict | None:
        current = lab.app("GET", f"/api/jobs/{job['id']}")
        return current if current["status"] in ("completed", "failed", "cancelled") else None

    return lab.wait_until(title, finished, timeout=4 * 3600, every=1)


def _with_only_plex(
    lab: ModuleType, settings: dict, workers: dict[str, int] | None, action: Callable[[], None]
) -> None:
    """Run `action()` with Jellyfin and Emby off in the app, `settings` and `workers` applied, then put it all back."""
    saved = _app_settings(lab)
    saved_servers = {server: _app_server_enabled(lab, server) for server in SITE_APP_OTHER_SERVERS}
    saved_workers = _live_workers(lab)
    try:
        for server in SITE_APP_OTHER_SERVERS:
            lab.app("PUT", f"/api/servers/{server}", {"enabled": False})
        if settings:
            lab.app("POST", "/api/settings", settings)
            applied = _app_settings(lab)
            if any(applied[k] != v for k, v in settings.items()):
                raise SystemExit(f"the app did not take the benchmark settings: {applied}")
        if workers:
            _set_live_workers(lab, workers)
        action()
    finally:
        lab.app("POST", "/api/settings", saved)
        for server, enabled in saved_servers.items():
            lab.app("PUT", f"/api/servers/{server}", {"enabled": enabled})
        _set_live_workers(lab, saved_workers)
        drift = [k for k, v in _app_settings(lab).items() if saved.get(k) != v]
        drift += [s for s, enabled in saved_servers.items() if _app_server_enabled(lab, s) != enabled]
        if drift:
            raise SystemExit(f"app settings did not come back: {drift}")


def run_app(mode: str, run: int, interval: int | None, results: Path) -> None:
    """Time one app job over the films, Plex output only, and append it to `results`.

    Args:
        mode: "gpu" (the app's default GPU worker, no CPU workers) or "cpu" (APP_CPU_WORKERS, no GPU).
        run: Run number recorded in the row.
        interval: Seconds between frames; None uses Plex's own setting.
        results: The scenario's results.csv.
    """
    lab = _lab()
    interval = interval or int(_plex_prefs(lab)["GenerateBIFFrameInterval"]["value"])
    gpus = [g for g in lab.app("GET", "/api/system/status")["gpus"] if g.get("status") == "ok"]
    if not gpus:
        raise SystemExit("the app sees no working GPU; check the container's GPU flags")
    gpu_on = mode == "gpu"
    gpu_config = [
        {"device": g["device"], "name": g["name"], "type": g["type"], "enabled": gpu_on,
         "workers": APP_GPU_WORKERS if gpu_on else 0, "ffmpeg_threads": APP_FFMPEG_THREADS}
        for g in gpus
    ]  # fmt: skip
    settings = {
        "thumbnail_interval": interval,
        "thumbnail_quality": PLEX_JPEG_QUALITY,
        "gpu_config": gpu_config,
        "cpu_threads": 0 if gpu_on else APP_CPU_WORKERS,
    }
    films = lab.films()
    workers = {"GPU": APP_GPU_WORKERS * len(gpus), "CPU": 0} if gpu_on else {"GPU": 0, "CPU": APP_CPU_WORKERS}

    def timed_run() -> None:
        _wait_quiet(lab)
        busy_before = _cpu_busy()
        bifs = _clear_previews()
        _warm_cache(lab)
        if (live := _live_workers(lab)) != workers:
            raise SystemExit(f"the app's worker pool changed to {live} before the run")
        sampler = _Sampler(("ffmpeg",))
        sampler.start()
        sent = time.time()
        try:
            job = _run_app_job(lab, films, f"benchmark {mode} {run}")
        finally:
            sampler.stop()
        created, started, finished = (lab.parse_timestamp(job[k]) for k in ("created_at", "started_at", "completed_at"))
        stats = _bif_stats(bifs)
        problems = _job_problems(job, len(films))
        if not all(s is not None and s[0] >= sent for s in stats):
            problems.append("not every Plex preview was written")
        if sampler.ours_on_gpu != gpu_on:
            problems.append(f"ffmpeg_on_gpu={sampler.ours_on_gpu}")
        frames = sum(s[1] for s in stats if s is not None)
        notes = (
            f"interval={interval}s quality=q{PLEX_JPEG_QUALITY} frames={frames} "
            f"gpu_workers={workers['GPU']} cpu_workers={workers['CPU']} "
            f"ffmpeg_on_gpu={sampler.ours_on_gpu} decoder_max={sampler.ours_decoder_max}% "
            f"queue={started - created:.1f}s host_cpu_busy_before={busy_before:.0%} "
            f"host_cpu_busy_during={sampler.cpu_busy:.0%} "
            f"other_gpu_max={sampler.others_gpu_max}% job={job['id'][:8]}"
        )
        _append({"tool": f"app-{mode}", "run": run, "status": "failed" if problems else "timed", "files": len(films),
                 "wall_seconds": f"{finished - started:.1f}", "started_at": _now_iso(started),
                 "finished_at": _now_iso(finished),
                 "notes": notes + (f" problems: {'; '.join(problems)}" if problems else "")}, results)  # fmt: skip

    _with_only_plex(lab, settings, workers, timed_run)


def restore() -> None:
    """Put the app's own Plex previews back at its normal settings, after the benchmark overwrote them.

    Only the Plex previews change during the benchmark. A plain `lab_setup.py generate` could take
    frames from the app's frame cache, which is keyed by file and not by frame interval, so this
    forces fresh extraction instead.
    """
    lab = _lab()
    films = lab.films()

    def remake() -> None:
        _clear_previews()
        job = _run_app_job(lab, films, "restore lab previews")
        problems = _job_problems(job, len(films))
        print(f"restore job {job['id'][:8]}: {job['status']}" + (f" ({'; '.join(problems)})" if problems else ""))
        if problems:
            raise SystemExit(1)

    _with_only_plex(lab, {}, None, remake)


def environment() -> None:
    """Record the machine, versions, settings and films in docs/benchmark/environment.json."""
    lab = _lab()
    prefs = _plex_prefs(lab)
    files = []
    for path in sorted(lab.MEDIA_HOST.glob("*/*")):
        if path.suffix.lower() not in lab.VIDEO_SUFFIXES:
            continue
        probe = json.loads(_sh("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=codec_name,profile,width,height,r_frame_rate,color_transfer:format=duration,size",
                               "-of", "json", str(path)))  # fmt: skip
        files.append({"file": path.name, **probe["streams"][0], **probe["format"]})
    lscpu = dict(
        (key.strip(), value.strip())
        for key, _, value in (line.partition(":") for line in _sh("lscpu").splitlines())
        if key.strip() in ("Model name", "CPU(s)", "Socket(s)", "Core(s) per socket", "Thread(s) per core")
    )
    app_paths = ("media_preview_generator", "Dockerfile", "pyproject.toml")
    image = _sh("docker", "inspect", "mlab-site-app", "--format", "{{.Config.Image}} {{.Image}}").split()
    record = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "host_cpu": lscpu.get("Model name"),
        "host_cpus": lscpu.get("CPU(s)"),
        "host_topology": f"{lscpu.get('Socket(s)')} sockets x {lscpu.get('Core(s) per socket')} cores, "
        f"{lscpu.get('Thread(s) per core')} thread per core",
        "host_memory_gb": round(int(Path("/proc/meminfo").read_text(encoding="utf-8").split()[1]) / 2**20),
        "gpu": _sh("nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader").strip(),
        "other_gpu_processes": sorted(
            {p.strip() for p in _sh("nvidia-smi", "--query-compute-apps=process_name", "--format=csv,noheader").splitlines() if p.strip()}
        ),
        "plex_version": lab.plex("GET", "/identity")["MediaContainer"]["version"],
        "plex_prefs": {k: prefs[k]["value"] for k in prefs if "BIF" in k or k.startswith("HardwareAccelerated") or k == "HardwareDevicePath"},
        "plex_transcoder": _sh("docker", "exec", "mlab-plex", "/usr/lib/plexmediaserver/Plex Transcoder", "-version").splitlines()[0],
        "app_image": {"tag": image[0], "id": image[1][:19], "created": _sh("docker", "image", "inspect", image[1], "--format", "{{.Created}}").strip()},
        "app_code_commit": _sh("git", "-C", str(REPO_ROOT), "log", "-1", "--format=%h", "--", *app_paths).strip(),
        "app_ffmpeg": _sh("docker", "exec", "mlab-site-app", APP_FFMPEG, "-version").splitlines()[0],
        "app_gpus": [{k: g.get(k) for k in ("name", "device", "acceleration", "status")} for g in lab.app("GET", "/api/system/status")["gpus"]],
        "app_settings_at_rest": _app_settings(lab),
        "app_settings_for_runs": {
            "thumbnail_interval": "Plex's GenerateBIFFrameInterval, or --interval for both tools",
            "thumbnail_quality": PLEX_JPEG_QUALITY,
            "app-gpu": {"gpu_workers": APP_GPU_WORKERS, "ffmpeg_threads": APP_FFMPEG_THREADS, "cpu_threads": 0},
            "app-cpu": {"gpu_workers": 0, "cpu_threads": APP_CPU_WORKERS},
            "servers": "Plex only (Jellyfin and Emby disabled for the run)",
            "force_regenerate": True,
        },
        "cache": "warm: every film is read once with cat before each run; no preview files exist at the start",
        "host_cpu_busy_now": round(_cpu_busy(), 2),
        "files": files,
    }  # fmt: skip
    BENCH.mkdir(parents=True, exist_ok=True)
    ENVIRONMENT.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {ENVIRONMENT}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", choices=["environment", "plex", "app-gpu", "app-cpu", "summary", "restore"])
    parser.add_argument("--runs", type=int, default=MIN_RUNS)
    parser.add_argument("--interval", type=int, help="seconds between frames for both tools (default: Plex's own)")
    args = parser.parse_args()
    bench = BENCH / f"interval-{args.interval}s" if args.interval else BENCH
    results = bench / "results.csv"
    if args.what == "environment":
        environment()
    elif args.what == "summary":
        summary = summarize(read_rows(results))
        (bench / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2))
    elif args.what == "restore":
        restore()
    else:
        tool = "plex-builtin" if args.what == "plex" else args.what
        done = len([row for row in read_rows(results) if row["tool"] == tool])
        for run in range(done + 1, done + args.runs + 1):
            if args.what == "plex":
                run_plex(run, args.interval, results)
            else:
                run_app(args.what.removeprefix("app-"), run, args.interval, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
