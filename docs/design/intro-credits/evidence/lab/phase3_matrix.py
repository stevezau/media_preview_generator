#!/usr/bin/env python3
"""Phase 3 lab matrix for Intro & Credits (plan-phase3 Task 13): on-screen credit text, on phase 1 and 2's helpers.

    ./phase3_matrix.py configure              phase 2 configure + the Synth Credits library on every server
    ./phase3_matrix.py run 1 2 3 ...          run storage rows in the given order
    ./phase3_matrix.py rows                   list the rows

Rows 12-15 run on the plex host through plex_rows.py under the owner's Q7 terms (plan-phase3 Task 13 Step 6); this
script refuses them. Each row writes results/p3-row-NN.json (git-ignored) with its premise, checks and evidence;
credentials are scrubbed. A row whose premise doesn't hold fails, whatever its checks say. Rows put lab state back in
`finally`, also when the run is stopped (SIGTERM). MLAB_DIR sets the lab folder; MLAB_SHOTS where screenshots go;
MLAB_APP_IMAGE the image app.sh recreates the app from; MLAB_HARNESS_ANSWERS the Task 11 GPU harness run row 10
compares against (its `--json` output; default: evidence/eval/phase3_credits_gpu.json beside the lab folder).
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
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import phase1_matrix as p1
import phase2_matrix as p2
from phase1_matrix import app_ok, now_iso, say, scrub, sh, wait_until  # time and wait_until: rows 5-8

HERE = Path(__file__).resolve().parent
RESULTS = p1.RESULTS
CREDITS_ROOT = "/media/synth-credits"
CREDITS_LIBRARY = "Synth Credits"
MOVIE_FOLDER = f"{CREDITS_ROOT}/Synth Credits (2024)"
MOVIE = f"{MOVIE_FOLDER}/Synth Credits (2024).mkv"  # a 40 s scene follows the roll (Q3)
OPEN_MOVIE = f"{CREDITS_ROOT}/Synth Credits Open (2025)/Synth Credits Open (2025).mkv"  # the roll runs to the end
AV1_MOVIE = f"{MOVIE_FOLDER}/Synth Credits (2024) - AV1.mkv"
HOST_FOLDER = p1.LAB / "synth" / "Synth Credits (2024)"
STAGED_AV1 = p1.LAB / "synth" / "_staging" / "Synth Credits (2024) - AV1.mkv"
TRUTH_MS = 540_000
MOVIE_MS, OPEN_MOVIE_MS = 700_000, 660_000
END_WINDOW_MS = (656_000, 661_000)  # the last name leaves the screen at 660 s
BROKEN_MODEL_ENV = "MEDIA_PREVIEW_TEXTDET_MODEL=/nonexistent/model.onnx"
OWNER_GATED = frozenset({12, 13, 14, 15})
ONLINE_SOURCES = ("theintrodb", "introdb", "skipdb")
HARNESS_ANSWERS = Path(os.environ.get("MLAB_HARNESS_ANSWERS") or p1.LAB.parent / "eval" / "phase3_credits_gpu.json")
HARNESS_SETS = ("movies40", "movie_credit_truth")
# Phase 2's configure and rescans walk this dict: the credits movie becomes one more synth library on every server.
p2.SYNTH_LIBRARIES[CREDITS_LIBRARY] = ("movie", "movies", CREDITS_ROOT)


def write_result(row: int, title: str, result: str, evidence: dict, notes: list[str] | None = None) -> dict:
    """Write one row's result file and say how it went.

    Args:
        row: The row number.
        title: The row's title.
        result: ``pass``, ``fail`` or ``fail (premise)``.
        evidence: What the row read, merged into the file.
        notes: Lines printed under the result.

    Returns:
        The written body.
    """
    RESULTS.mkdir(exist_ok=True)
    body = scrub({"row": row, "title": title, "result": result, "at": now_iso(), "notes": notes or [], **evidence})
    (RESULTS / f"p3-row-{row:02d}.json").write_text(json.dumps(body, indent=2, default=str) + "\n")
    say(f"p3 row {row}: {result} — {title}")
    for note in notes or []:
        say(f"  - {note}")
    return body


def checks_result(
    row: int,
    title: str,
    premise: dict[str, bool],
    checks: dict[str, bool],
    evidence: dict,
    notes: list[str] | None = None,
) -> dict:
    """Pass when the premise and every check hold; a failed premise is "fail (premise)", never a pass.

    Args:
        row: The row number.
        title: The row's title.
        premise: What had to be true for the checks to mean anything.
        checks: The row's own checks.
        evidence: What the row read.
        notes: Extra lines for the result file.

    Returns:
        The written body.
    """
    lines = [f"premise {k}: {v}" for k, v in premise.items()] + [f"{k}: {v}" for k, v in checks.items()] + (notes or [])
    if not all(premise.values()):
        result = "fail (premise)"
    else:
        result = "pass" if all(checks.values()) else "fail"
    return write_result(row, title, result, {"premise": premise, "checks": checks, **evidence}, lines)


def credits_evidence(path: str) -> list[dict]:
    """The file's stored credit-text evidence rows.

    Args:
        path: The file as the app sees it.

    Returns:
        Its ``credits_text`` evidence rows, in stored order.
    """
    return [e for e in p1.item_payload(path)["evidence"] if e["source"] == "credits_text"]


def near_truth(rows: list[dict], tolerance_ms: int = 10_000) -> bool:
    """Whether one stored credits answer sits within ``tolerance_ms`` of the synthetic movies' 540 s roll.

    Args:
        rows: Credit-text evidence rows.
        tolerance_ms: How far from 540 s still counts.

    Returns:
        True for exactly one credits row inside the window.
    """
    return len(rows) == 1 and rows[0]["type"] == "credits" and abs(rows[0]["start_ms"] - TRUTH_MS) <= tolerance_ms


def as_time(value: Any) -> datetime | None:
    """One ``fetched_at`` as a datetime (the API serialises it as an HTTP date, which doesn't sort as text).

    Args:
        value: The field as the API returned it.

    Returns:
        The time, or None when it can't be read.
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def app_log_lines(since: str, needle: str) -> list[str]:
    """mlab-app's log lines since an ISO time that contain ``needle``, from whichever stream carries more of them.

    Rows count these lines ("exactly one WARNING"), so both ways of being wrong matter. Concatenating the streams
    would double-count an event a sink wrote to both; de-duplicating identical text would merge two genuinely
    separate events, since the console format's timestamp only resolves to the second. Taking the larger of the two
    per-stream counts does neither. On this image only stderr carries them (measured: 1 on stderr, 0 on stdout).

    Only the *current* container's output exists: ``recreate_app`` starts a new one and the old log goes with it, so
    a row has to read its lines before anything recreates the app -- including a worker block's own exit.

    Args:
        since: ISO time to read from.
        needle: Text the line must contain.

    Returns:
        The matching lines of that stream, in order, scrubbed.
    """
    out = subprocess.run(["docker", "logs", "--since", since, "mlab-app"], capture_output=True, text=True, timeout=60)
    per_stream = [[line for line in stream.splitlines() if needle in line] for stream in (out.stdout, out.stderr)]
    return [scrub(line) for line in max(per_stream, key=len)]


def recreate_app(*, gpu: bool, extra_env: str = "") -> None:
    """app.sh recreate with or without the NVIDIA runtime and one extra environment variable (MLAB_APP_IMAGE kept).

    Args:
        gpu: Give the app the NVIDIA runtime and /dev/dri.
        extra_env: One ``NAME=value`` passed into the container, or "" for none.

    Raises:
        RuntimeError: app.sh failed.
    """
    env = {**os.environ, "MLAB_APP_GPU": "nvidia" if gpu else "", "MLAB_APP_EXTRA_ENV": extra_env}
    out = subprocess.run(["./app.sh", "recreate"], cwd=HERE, env=env, capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"app.sh recreate -> {out.returncode}: {scrub(out.stderr[-2000:])}")
    say(f"mlab-app recreated (gpu={gpu}, extra env={'set' if extra_env else 'none'})")


def credits_job(
    label: str, *, force: bool, paths: tuple[str, ...] = (MOVIE,), timeout: float = 1800
) -> tuple[dict, list[dict]]:
    """Run one markers job over the credits movies and wait for it.

    Args:
        label: Goes in the job name.
        force: Force re-detection.
        paths: The files to run over.
        timeout: How long to wait.

    Returns:
        The finished job and its Files rows.
    """
    return p2.run_job({"file_paths": list(paths), "library_name": f"Phase 3 {label}", "force": force}, timeout=timeout)


def is_credit_decode(args: str) -> bool:
    """Whether a process line is one of credit text's own ffmpeg decodes (spec §5.4's argv).

    Args:
        args: A process's full argv as ``ps`` prints it.

    Returns:
        True for a credit-text decode.
    """
    return "ffmpeg" in args and "showinfo" in args and "rawvideo" in args


def credit_decodes() -> list[str]:
    """The credit-text decodes running in mlab-app right now (a fresh ``ps``, not the sampler's history).

    Returns:
        One argv per running decode.
    """
    out = sh("docker", "exec", "mlab-app", "ps", "-eo", "args=", check=False)
    return [line.strip() for line in out.splitlines() if is_credit_decode(line)]


def decode_start_s(args: str) -> float | None:
    """A decode's ``-ss`` in seconds.

    Args:
        args: The decode's argv.

    Returns:
        The seek, or None when the argv has none.
    """
    match = re.search(r" -ss (\d+(?:\.\d+)?)", args)
    return float(match.group(1)) if match else None


class ProcessSampler(threading.Thread):
    """Credit-text decodes and text detection helpers in mlab-app, every 0.5 s, until the block ends."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.decodes: list[str] = []
        self.helpers: dict[str, str] = {}
        self.peak_helpers = 0
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            out = sh("docker", "exec", "mlab-app", "ps", "-eo", "pid=,args=", check=False)
            running = 0
            for line in out.splitlines():
                pid, _, args = line.strip().partition(" ")
                if is_credit_decode(args) and args not in self.decodes:
                    self.decodes.append(args)
                if "textdet_helper" in args and "--check" not in args:
                    running += 1
                    self.helpers.setdefault(pid, args)
            self.peak_helpers = max(self.peak_helpers, running)
            self.stop.wait(0.5)

    def webgpu_pids(self) -> list[str]:
        """The helper PIDs seen running with the WebGPU backend.

        Returns:
            Their PIDs as strings.
        """
        return [pid for pid, args in self.helpers.items() if "--backend webgpu" in args]

    def __enter__(self) -> ProcessSampler:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.join(timeout=5)


def helper_threads(pid: str) -> str:
    """A helper process's ``Threads:`` line from /proc inside mlab-app.

    Args:
        pid: The helper's PID in the container.

    Returns:
        The thread count, or "" when the process is already gone.
    """
    out = sh("docker", "exec", "mlab-app", "cat", f"/proc/{pid}/status", check=False)
    line = next((entry for entry in out.splitlines() if entry.startswith("Threads:")), "")
    return line.split()[-1] if line else ""


def container_path(host_path: str) -> str | None:
    """Where the app sees a real file the scale mounts expose (``scale_mounts.sh`` ``-v host:container:ro`` lines).

    Args:
        host_path: The file's path on storage.

    Returns:
        Its path inside mlab-app, or None when no mount holds it.
    """
    for line in (p1.LAB / "scale_mounts.sh").read_text().splitlines():
        match = re.search(r'-v "([^"]+):([^":]+):ro"', line)
        if match and (host_path == match.group(1) or host_path.startswith(match.group(1).rstrip("/") + "/")):
            return match.group(2) + host_path[len(match.group(1)) :]
    return None


def harness_picks(count: int) -> list[dict]:
    """Movies from Task 11's GPU harness run that the scale mounts expose, with that run's own answers.

    Both cells of Q3's end matter, so the first three answers *with* an end come first; the rest follow in the
    harness's own order. Every pick carries the title only outside ``path``: results name movies, never library paths.

    Args:
        count: How many movies to pick.

    Returns:
        ``{"name", "path", "start_s", "end_s"}`` per pick, at most ``count`` of them.
    """
    if not HARNESS_ANSWERS.is_file():
        return []
    details = json.loads(HARNESS_ANSWERS.read_text())["details"]
    mapped = []
    for set_name in HARNESS_SETS:
        for entry in details.get(set_name, []):
            path = container_path(entry["file"])
            if path is not None and not any(pick["name"] == entry["name"] for pick in mapped):
                mapped.append(
                    {"name": entry["name"], "path": path, "start_s": entry["text"], "end_s": entry["text_end"]}
                )
    with_end = [pick for pick in mapped if pick["end_s"] is not None][:3]
    rest = [pick for pick in mapped if pick not in with_end]
    return (with_end + rest)[:count]


def gpu_entry(gpu: dict, *, enabled: bool) -> dict:
    """One ``gpu_config`` row for a detected device.

    Every detected device needs a row: ``job_runner._build_selected_gpus`` gives a GPU that ``gpu_config`` doesn't
    mention one worker with defaults, so leaving it out turns it *on*, not off.

    Args:
        gpu: A device from ``POST /api/system/rescan-gpus``.
        enabled: Whether the device gets a worker.

    Returns:
        The settings row.
    """
    return {"device": gpu["device"], "name": gpu.get("name", ""), "type": gpu.get("type", ""),
            "enabled": enabled, "workers": 1 if enabled else 0, "ffmpeg_threads": 2}  # fmt: skip


def _worker_settings() -> dict:
    settings = app_ok("GET", "/api/settings")
    return {"gpu_config": settings.get("gpu_config") or [], "cpu_threads": settings.get("cpu_threads")}


def restore_workers(before: dict) -> None:
    """Put worker settings back and give the app a pool that matches them again.

    Posting the settings alone isn't enough: the running app live-reconciles GPU workers but never ``cpu_threads``,
    so a block that ran with no CPU workers would leave a pool with none however the settings then read. The next
    row would inherit workers nobody asked for -- the shape that already made row 3 compare a GPU run with a GPU run.

    Args:
        before: The ``gpu_config`` / ``cpu_threads`` the block found.
    """
    app_ok("POST", "/api/settings", before)
    recreate_app(gpu=True)
    say("worker settings put back and the pool rebuilt from them")


class CpuOnly:
    """Every GPU off and one CPU worker for a block (a known CPU answer); the workers found are put back after."""

    def __enter__(self) -> None:
        self.before = _worker_settings()
        gpus = app_ok("POST", "/api/system/rescan-gpus")["gpus"]
        app_ok("POST", "/api/settings", {"gpu_config": [gpu_entry(g, enabled=False) for g in gpus], "cpu_threads": 1})
        # The pool is built once per process and kept; reconciling a settings change adds workers but leaves the ones
        # already there, and the dispatcher hands the next file to whichever is free. Only a fresh app gives a pool
        # that holds exactly these workers, so a row can say which one read the file.
        recreate_app(gpu=True)
        say(f"{len(gpus)} GPU(s) off, one CPU worker")

    def __exit__(self, *exc: object) -> None:
        restore_workers(self.before)


class GpuWorker:
    """One worker on the container's NVIDIA GPU and no CPU workers for a block; the workers found are put back after."""

    def __enter__(self) -> dict:
        self.before = _worker_settings()
        gpus = app_ok("POST", "/api/system/rescan-gpus")["gpus"]
        nvidia = next((g for g in gpus if g.get("type") == "NVIDIA" and g.get("status") != "failed"), None)
        if nvidia is None:
            raise RuntimeError(
                f"mlab-app sees no working NVIDIA GPU: {[(g.get('type'), g.get('status')) for g in gpus]}"
            )
        config = [gpu_entry(g, enabled=g["device"] == nvidia["device"]) for g in gpus]
        app_ok("POST", "/api/settings", {"gpu_config": config, "cpu_threads": 0})
        recreate_app(gpu=True)  # a fresh pool: see CpuOnly
        say(f"one GPU worker on {nvidia['device']}, every other device and the CPU workers off")
        return nvidia

    def __exit__(self, *exc: object) -> None:
        restore_workers(self.before)


class OnlineSourcesOff:
    """The three online sources off for a block; the stored source list is posted back after (row 10)."""

    def __enter__(self) -> list[dict]:
        self.before = app_ok("GET", "/api/settings")["markers"]["sources"]
        off = [{**entry, "enabled": False} if entry["id"] in ONLINE_SOURCES else entry for entry in self.before]
        app_ok("POST", "/api/settings", {"markers": {"sources": off}})
        say(f"online sources off: {ONLINE_SOURCES}")
        return self.before

    def __exit__(self, *exc: object) -> None:
        app_ok("POST", "/api/settings", {"markers": {"sources": self.before}})
        say("source settings put back")


def screenshot(page_script: str, shot: Path) -> dict:
    """Run a Playwright snippet (logged in, ``pg`` open) in the shared venv; returns its JSON line and the path.

    Args:
        page_script: Python lines at 8 spaces of indent, run with ``pg`` open and logged in.
        shot: Where the full-page screenshot goes.

    Returns:
        ``{"screenshot", "page", "error"}``.
    """
    p1.SHOTS.mkdir(parents=True, exist_ok=True)
    script = f"""
import asyncio, json, sys
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(); pg = await b.new_page(viewport={{"width": 1500, "height": 1100}})
        await pg.goto("{p1.APP}/login"); await pg.fill("#token", sys.stdin.read().strip()); await pg.keyboard.press("Enter")
        await pg.wait_for_timeout(3000)
{page_script}
        await pg.screenshot(path="{shot}", full_page=True); await b.close()
asyncio.run(main())
"""
    out = subprocess.run([p1.VENV_PYTHON, "-c", script], input=p1.ENV["MLAB_APP_TOKEN"], capture_output=True, text=True,
                         timeout=180)  # fmt: skip
    last = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "null"
    return {
        "screenshot": str(shot),
        "page": json.loads(last),
        "error": scrub(out.stderr[-500:]) if out.returncode else "",
    }


ROWS: dict[int, Any] = {}


def row(number: int):
    def register(fn):
        ROWS[number] = fn
        return fn

    return register


# ---------------------------------------------------------------------------------------------------------- rows


@row(1)
def row_01_capability() -> dict:
    """Credits text is available in the CPU and the GPU container, and says why when the model is missing."""
    seen: dict[str, dict] = {}
    try:
        for name, gpu, extra in (("cpu", False, ""), ("gpu", True, ""), ("missing model", True, BROKEN_MODEL_ENV)):
            recreate_app(gpu=gpu, extra_env=extra)
            # text_detection_status runs its check on the first ask (up to 30 s), so this call waits for the answer.
            seen[name] = app_ok("GET", "/api/markers/sources/local")["credits_text"]
            if name == "missing model":
                shot = p1.SHOTS / "p3-row01-settings-unavailable.png"
                seen["page"] = screenshot(
                    '        await pg.goto("' + p1.APP + '/settings#markers"); await pg.wait_for_timeout(4000)\n'
                    "        row = pg.locator('li.markers-source[data-id=\"credits_text\"]').first\n"
                    "        print(json.dumps(await row.inner_text()))",
                    shot,
                )
    finally:
        recreate_app(gpu=True)
    premise = {"three containers answered": {"cpu", "gpu", "missing model"} <= seen.keys()}
    missing = seen.get("missing model", {})
    checks = {
        "CPU container: available": seen.get("cpu", {}).get("available") is True,
        "GPU container: available": seen.get("gpu", {}).get("available") is True,
        "missing model: not available": missing.get("available") is False,
        "missing model: the reason names the path": "/nonexistent/model.onnx" in missing.get("message", ""),
        "Settings row shows Not available": "Not available" in str(seen.get("page", {}).get("page")),
    }
    return checks_result(1, "Credit text capability and the Settings row", premise, checks, {"seen": seen})


@row(2)
def row_02_high_then_medium() -> dict:
    """High: credits text alone is Needs review, nothing published. Medium (Q1): published to all five servers, the skip
    ending at the last credit before the scene (Q3) on Plex and Jellyfin; Emby gets the start and says it skips to the
    end of the file (R1)."""
    evidence: dict[str, Any] = {}
    try:
        p2.set_publish_when("high")
        job_high, _ = credits_job("row 2 High", force=True)
        payload_high = p1.item_payload(MOVIE)
        evidence["duration_ms"] = payload_high["duration_ms"]
        evidence["high"] = {"job": job_high["id"], "evidence": credits_evidence(MOVIE),
                            "decision": payload_high["decisions"]["credits"]}  # fmt: skip
        evidence["high_published"] = {
            sid: p2.inspector_server(MOVIE, sid).get("published") for sid in p2.ALL_MARKER_SERVERS
        }
        p2.set_publish_when("medium")
        job_medium, _ = credits_job("row 2 Medium", force=False)
        evidence["medium"] = {"job": job_medium["id"], "decision": p1.item_payload(MOVIE)["decisions"]["credits"]}
        evidence["servers"] = {sid: p2.inspector_server(MOVIE, sid) for sid in p2.ALL_MARKER_SERVERS}
        evidence["inspector"] = screenshot(
            '        await pg.goto("' + p1.APP + '/bif-viewer")\n'
            "        await pg.wait_for_function(\"() => document.querySelector('#serverSelect option[value=mlab-plex]')\", timeout=20000)\n"
            '        await pg.select_option("#serverSelect", "mlab-plex")\n'
            # Plex titles the movie "Synth Credits" (the year is its own field), and the search returns both movies:
            # the row is picked by its file, not by being first.
            '        await pg.fill("#searchInput", "Synth Credits"); await pg.click("#searchBtn")\n'
            "        hit = pg.locator('.result-item[data-media-file=\"" + MOVIE + "\"]').first\n"
            "        await hit.wait_for(timeout=30000)\n"
            "        await hit.click(); await pg.click('#inspectorMarkersTabBtn')\n"
            "        await pg.wait_for_timeout(3000)\n"
            '        lane = pg.locator(\'.mk-window[data-window="ending"] .mk-lane[data-lane="Credit text"]\').first\n'
            "        print(json.dumps(await lane.inner_text()))",
            p1.SHOTS / "p3-row02-inspector.png",
        )
    finally:
        p2.set_publish_when("high")
    rows = evidence["high"]["evidence"] if "high" in evidence else []
    start = rows[0]["start_ms"] if rows else None
    end = rows[0]["end_ms"] if rows else None

    def mss(ms: int) -> str:
        return f"{ms // 60_000}:{ms // 1000 % 60:02d}"

    def published(sid: str) -> dict | None:
        markers = (evidence.get("servers", {}).get(sid) or {}).get("published") or []
        return next((m for m in markers if m["type"] == "credits"), None)

    premise = {
        "the app reads the 700 s movie (a 40 s scene follows the roll)": abs(
            (evidence.get("duration_ms") or 0) - MOVIE_MS
        )
        <= 1_000,
        "credits text answered within 10 s of 540 s": near_truth(rows),
        "the stored answer ends at the last credit (656-661 s)": end is not None
        and END_WINDOW_MS[0] <= end <= END_WINDOW_MS[1],
    }
    checks = {
        "High: credits needs review": evidence.get("high", {}).get("decision", {}).get("status") == "needs_review",
        "High: nothing of ours published": not any(evidence.get("high_published", {}).values()),
        "Medium: credits decided with the text's end": (evidence.get("medium", {}).get("decision", {}).get("marker") or {}).get("end_ms") == end,
        **{f"Medium: {sid} published start and end": (m := published(sid)) is not None and abs(m["start_ms"] - start) <= 1_000 and m["end_ms"] == end
           for sid in ("mlab-plex", "mlab-jellyfin", "mlab-jf12")},
        **{f"Medium: {sid} published the start and says it skips to the end": (m := published(sid)) is not None
           and abs(m["start_ms"] - start) <= 1_000
           and "Emby skips to the end of the file" in ((evidence["servers"][sid] or {}).get("publish_message") or "")
           for sid in p2.EMBY_SERVERS},
        "Inspector's Credit text lane shows start-end": start is not None and end is not None
        and any(f"{mss(start + d)}–{mss(end + e)}" in str(evidence.get("inspector", {}).get("page")) for d in (-1000, 0, 1000) for e in (-1000, 0, 1000)),
    }  # fmt: skip
    return checks_result(
        2, "Synthetic movie with a scene after the credits, at High then Medium", premise, checks, evidence
    )


@row(3)
def row_03_gpu_on_storage() -> dict:
    """On the P5000: the tail decode runs on CUDA, the helper keeps WebGPU after its self-test, same answer as the CPU."""
    # A device self-tests once per process, so an earlier row's GPU job would leave this row nothing to read: a fresh
    # app makes the self-test this row's own.
    recreate_app(gpu=True)
    since = now_iso()
    # The CPU leg is sampled too. Without that, a CpuOnly that quietly left the GPU on (the shape this row already hit
    # once: a device missing from gpu_config runs with defaults) makes this row compare a GPU run against a GPU run
    # and still report a full pass.
    with CpuOnly(), ProcessSampler() as cpu_sampler:
        credits_job("row 3 CPU", force=True)
    cpu_rows = credits_evidence(MOVIE)
    cpu_helpers = [args for args in cpu_sampler.helpers.values()]
    with GpuWorker() as gpu, ProcessSampler() as sampler:
        job, _ = credits_job("row 3 GPU", force=True)
        worker_page = (
            screenshot(
                '        await pg.goto("' + p1.APP + '/"); await pg.wait_for_timeout(2000)\n'
                "        print(json.dumps(await pg.locator('#workerStatusContainer').inner_text()))",
                p1.SHOTS / "p3-row03-workers.png",
            )
            if sampler.decodes
            else {}
        )
        gpu_rows = credits_evidence(MOVIE)
        # Read inside the block: leaving it recreates the app, and the self-test line goes with the old container.
        log = app_log_lines(since, "Credit text detection on")
    premise = {
        "a GPU worker ran the job": job["status"] == "completed" and bool(gpu),
        "a credit-text decode was seen": bool(sampler.decodes),
        "the CPU-only run answered": near_truth(cpu_rows),
        "the CPU-only run really decoded on the CPU": bool(cpu_sampler.decodes)
        and not any("-hwaccel" in a for a in cpu_sampler.decodes),
        "the CPU-only run detected text on the CPU helper": bool(cpu_helpers)
        and all("--backend cpu" in a for a in cpu_helpers),
    }
    checks = {
        "decode used -hwaccel cuda and scale_cuda": any("-hwaccel cuda" in a and "scale_cuda=320:180" in a for a in sampler.decodes),
        "decode used -threads 2": all(" -threads 2 " in f" {a} " for a in sampler.decodes),
        "a webgpu helper ran": bool(sampler.webgpu_pids()),
        "the self-test kept the GPU": any(": GPU (" in line for line in log),
        "GPU answer within 2 s of the CPU answer": bool(gpu_rows and cpu_rows) and abs(gpu_rows[0]["start_ms"] - cpu_rows[0]["start_ms"]) <= 2_000,
        "GPU end within 2 s of the CPU end": bool(gpu_rows and cpu_rows) and None not in (gpu_rows[0]["end_ms"], cpu_rows[0]["end_ms"])
        and abs(gpu_rows[0]["end_ms"] - cpu_rows[0]["end_ms"]) <= 2_000,
    }  # fmt: skip
    evidence = {"job": job["id"], "decodes": sampler.decodes, "helpers": sampler.helpers, "log": log,
                "cpu_decodes": cpu_sampler.decodes, "cpu_helpers": cpu_helpers,
                "cpu": cpu_rows, "gpu": gpu_rows, "workers_page": worker_page}  # fmt: skip
    return checks_result(3, "GPU decode and WebGPU text detection on storage", premise, checks, evidence)


@row(4)
def row_04_gpu_decode_failure() -> dict:
    """AV1 on the P5000 (no AV1 NVDEC): the GPU decode fails, the worker reruns the file on the CPU, the answer lands."""
    target = HOST_FOLDER / STAGED_AV1.name
    since = now_iso()
    evidence: dict[str, Any] = {}
    try:
        shutil.copyfile(STAGED_AV1, target)
        evidence["listed"] = p2.rescan_until(CREDITS_LIBRARY, [AV1_MOVIE], present=True)
        with GpuWorker(), ProcessSampler() as sampler:
            job, _ = credits_job("row 4 AV1", force=True, paths=(AV1_MOVIE,))
            # Read inside the block: leaving it recreates the app, and the retry line goes with the old container.
            evidence.update(job=job["id"], decodes=sampler.decodes, rows=credits_evidence(AV1_MOVIE),
                            retry_log=app_log_lines(since, "on the GPU and is retrying on CPU"))  # fmt: skip
    finally:
        target.unlink(missing_ok=True)
        evidence["rescan_after_removal"] = p2._rescan_best_effort(CREDITS_LIBRARY, [AV1_MOVIE])
    decodes = evidence.get("decodes", [])
    gpu_first = next((i for i, a in enumerate(decodes) if "-hwaccel cuda" in a), None)
    premise = {"a CUDA decode of the AV1 file was tried": gpu_first is not None}
    checks = {
        "the worker logged the CPU retry": bool(evidence.get("retry_log")),
        "a CPU decode followed": gpu_first is not None and any("-hwaccel" not in a for a in decodes[gpu_first + 1 :]),
        "the answer is within 10 s of 540 s": near_truth(evidence.get("rows", [])),
    }
    return checks_result(4, "GPU decode failure reruns on the CPU", premise, checks, evidence)


@row(5)
def row_05_helper_killed_in_a_request() -> dict:
    """A helper killed while it holds a request: that device reads on the CPU for the rest of the run of the app."""
    since = now_iso()
    evidence: dict[str, Any] = {}
    killed: str | None = None
    try:
        with GpuWorker(), ProcessSampler() as sampler:
            if not sampler.webgpu_pids():
                credits_job("row 5 warm-up", force=True)
            before = sorted(sampler.webgpu_pids())
            evidence["webgpu_pids_before"] = before
            if before:
                killed = before[0]
                # Stopped, not killed: the job's first text request then blocks on a live helper, which is the
                # in-a-request path. Killing it while idle is the between-requests path Task 5 unit-tests.
                sh("docker", "exec", "mlab-app", "kill", "-STOP", killed)
                started = p1.start_markers_job(
                    {"file_paths": [MOVIE], "library_name": "Phase 3 row 5 helper killed", "force": True}
                )
                evidence["decode_seen"] = wait_until("a credit-text decode", credit_decodes, timeout=300, every=1)
                sh("docker", "exec", "mlab-app", "kill", "-KILL", killed)
                killed_job = p1.wait_job(started["id"])
                evidence["killed_job"] = {"job": killed_job["id"], "status": killed_job["status"],
                                          "rows": credits_evidence(MOVIE)}  # fmt: skip
                evidence["warnings"] = app_log_lines(since, "moves to the CPU for the rest of this run of the app")
                again, _ = credits_job("row 5 after the kill", force=True)
                evidence["after_job"] = {"job": again["id"], "status": again["status"],
                                         "rows": credits_evidence(MOVIE)}  # fmt: skip
                evidence["webgpu_pids_after"] = sorted(sampler.webgpu_pids())
    finally:
        if killed is not None:
            sh("docker", "exec", "mlab-app", "kill", "-CONT", killed, check=False)
            sh("docker", "exec", "mlab-app", "kill", "-KILL", killed, check=False)
        # The device stays on the CPU for the app's lifetime, and rows 6-9 need the GPU helper again.
        recreate_app(gpu=True)
    premise = {
        "a webgpu helper was running": bool(evidence.get("webgpu_pids_before")),
        "the job was decoding when the helper died": bool(evidence.get("decode_seen")),
    }
    checks = {
        "exactly one move-to-the-CPU warning": len(evidence.get("warnings", [])) == 1,
        "the job still completed": evidence.get("killed_job", {}).get("status") == "completed",
        "the answer still landed within 10 s of 540 s": near_truth(evidence.get("killed_job", {}).get("rows", [])),
        "the next forced job started no new webgpu helper": evidence.get("webgpu_pids_after")
        == evidence.get("webgpu_pids_before"),
        # Without these two, "no new webgpu helper" would read the same whether the device fell back to the CPU or
        # credit text stopped working altogether.
        "the next forced job still completed": evidence.get("after_job", {}).get("status") == "completed",
        "the next forced job still answered on the CPU": near_truth(evidence.get("after_job", {}).get("rows", [])),
    }
    return checks_result(5, "A text detection helper killed during a request", premise, checks, evidence)


@row(6)
def row_06_cancel_mid_decode() -> dict:
    """A job cancelled while it decodes: the decode stops and the stored answer is left alone."""
    stored = credits_evidence(MOVIE)
    evidence: dict[str, Any] = {"stored_before": stored}
    started = p1.start_markers_job({"file_paths": [MOVIE], "library_name": "Phase 3 row 6 cancel", "force": True})
    try:
        evidence["decode_seen"] = wait_until("a credit-text decode", credit_decodes, timeout=60, every=1)
    except TimeoutError as exc:
        evidence["decode_seen"] = []
        evidence["decode_error"] = str(exc)
    finally:
        app_ok("POST", f"/api/jobs/{started['id']}/cancel")
    try:
        evidence["stopped_within_10s"] = bool(
            wait_until("no credit-text decode", lambda: not credit_decodes(), timeout=10, every=1)
        )
    except TimeoutError:
        evidence["stopped_within_10s"] = False
    job = p1.wait_job(started["id"], timeout=600)
    after = credits_evidence(MOVIE)
    evidence.update(job=job["id"], status=job["status"], stored_after=after)
    premise = {
        "one answer was stored before the row": len(stored) == 1,
        "the job was decoding when it was cancelled": bool(evidence.get("decode_seen")),
    }
    checks = {
        "the decode stopped within 10 s": evidence["stopped_within_10s"] is True,
        "the job is cancelled": job["status"] == "cancelled",
        "the stored answer is untouched": len(after) == 1 and after[0]["fetched_at"] == stored[0]["fetched_at"],
    }
    return checks_result(6, "Cancel mid-decode", premise, checks, evidence)


@row(7)
def row_07_reuse_and_force() -> dict:
    """A normal job reuses the stored answer; a forced one reads the file again."""
    # The row's own baseline: a forced run that finished. A cancelled forced run (row 6) leaves the answer due again,
    # and then the "normal" job below would read the file for a reason that has nothing to do with reuse.
    baseline_job, _ = credits_job("row 7 baseline", force=True)
    stored = credits_evidence(MOVIE)
    with ProcessSampler() as normal:
        normal_job, _ = credits_job("row 7 normal", force=False)
    after_normal = credits_evidence(MOVIE)
    with ProcessSampler() as forced:
        forced_job, _ = credits_job("row 7 forced", force=True)
    after_forced = credits_evidence(MOVIE)
    was, now = (
        as_time(stored[0]["fetched_at"]) if stored else None,
        as_time(after_forced[0]["fetched_at"]) if after_forced else None,
    )
    premise = {"the row's own forced run stored one answer": len(stored) == 1}
    checks = {
        "the normal job decoded nothing": not normal.decodes,
        "the normal job kept the stored answer": len(after_normal) == 1
        and after_normal[0]["fetched_at"] == stored[0]["fetched_at"],
        "the forced job decoded again": bool(forced.decodes),
        "the forced job stored a newer answer": None not in (was, now) and now > was,
    }
    evidence = {"baseline_job": baseline_job["id"], "normal_job": normal_job["id"], "forced_job": forced_job["id"],
                "normal_decodes": normal.decodes, "forced_decodes": forced.decodes, "stored": stored,
                "after_normal": after_normal, "after_forced": after_forced}  # fmt: skip
    return checks_result(7, "Stored answer reused, a forced run reads again", premise, checks, evidence)


@row(8)
def row_08_detection_unavailable() -> dict:
    """With detection gone, a stored credit-text answer may sit in review but never decide; it decides again after."""
    stored = credits_evidence(MOVIE)
    evidence: dict[str, Any] = {"stored_before": stored}
    try:
        p2.set_publish_when("medium")
        recreate_app(gpu=True, extra_env=BROKEN_MODEL_ENV)
        evidence["source"] = app_ok("GET", "/api/markers/sources/local")["credits_text"]
        job, _ = credits_job("row 8 detection unavailable", force=False)
        evidence["unavailable"] = {"job": job["id"], "decision": p1.item_payload(MOVIE)["decisions"]["credits"],
                                   "rows": credits_evidence(MOVIE)}  # fmt: skip
    finally:
        recreate_app(gpu=True)
        back, _ = credits_job("row 8 detection back", force=False)
        evidence["restored"] = {"job": back["id"], "decision": p1.item_payload(MOVIE)["decisions"]["credits"],
                                "source": app_ok("GET", "/api/markers/sources/local")["credits_text"]}  # fmt: skip
        p2.set_publish_when("high")
    premise = {
        "one answer was stored before the row": len(stored) == 1,
        "the app can't read credit text": evidence.get("source", {}).get("available") is False,
    }
    checks = {
        "the stored answer didn't decide credits": evidence.get("unavailable", {}).get("decision", {}).get("status")
        != "decided",
        "the stored answer is still there": len(evidence.get("unavailable", {}).get("rows", [])) == 1,
        "credits decide again once detection is back": evidence["restored"]["decision"]["status"] == "decided",
    }
    return checks_result(8, "Detection unavailable: a stored answer can't decide", premise, checks, evidence)


@row(9)
def row_09_resources() -> dict:
    """A GPU credit-text job keeps to two ffmpeg threads and at most two detection helpers."""
    with GpuWorker(), ProcessSampler() as sampler:
        job, _ = credits_job("row 9 resources", force=True)
        pids = sorted(sampler.webgpu_pids())
        threads = {pid: helper_threads(pid) for pid in pids}
    premise = {
        "a forced GPU job completed": job["status"] == "completed",
        "a credit-text decode was seen": bool(sampler.decodes),
        "a webgpu helper was seen": bool(pids),
    }
    checks = {
        "every decode used -threads 2": all(" -threads 2 " in f" {a} " for a in sampler.decodes),
        # One GPU worker over one file, so this is a ceiling the row stays under, not one it exercises: the note
        # records what actually ran.
        "no more than two detection helpers at once": sampler.peak_helpers <= 2,
    }
    # Not a check: ONNX Runtime and Dawn start their own threads; THREADS = 2 sets only the intra-op pool.
    notes = [
        f"webgpu helper threads: {threads or 'none read'}",
        f"helpers running at once, peak: {sampler.peak_helpers}",
    ]
    evidence = {"job": job["id"], "decodes": sampler.decodes, "helpers": sampler.helpers,
                "peak_helpers": sampler.peak_helpers, "helper_threads": threads}  # fmt: skip
    return checks_result(9, "Resources during a GPU credit-text job", premise, checks, evidence, notes)


@row(10)
def row_10_real_movies() -> dict:
    """Ten real movies: the app's own credit-text answers are Task 11's GPU harness answers."""
    picks = harness_picks(10)
    evidence: dict[str, Any] = {"harness_answers": str(HARNESS_ANSWERS),
                                "movies": [{"name": p["name"], "harness_start_s": p["start_s"], "harness_end_s": p["end_s"]}
                                           for p in picks]}  # fmt: skip
    seen: dict[str, list[dict]] = {}
    try:
        with OnlineSourcesOff():
            p2.set_publish_when("medium")
            with GpuWorker():  # the harness cache is a GPU run
                job, _ = credits_job(
                    "row 10 harness movies", force=True, paths=tuple(p["path"] for p in picks), timeout=7200
                )
            evidence["job"] = job["id"]
            seen = {p["name"]: credits_evidence(p["path"]) for p in picks}
    finally:
        p2.set_publish_when("high")
    compared = []
    for pick in picks:
        rows = seen.get(pick["name"], [])
        got = rows[0] if len(rows) == 1 else None
        compared.append({
            "name": pick["name"],
            "harness_start_s": pick["start_s"],
            "app_start_ms": got["start_ms"] if got else None,
            "harness_end_s": pick["end_s"],
            "app_end_ms": got["end_ms"] if got else None,
            "start_matches": got is not None and pick["start_s"] is not None
            and abs(got["start_ms"] - round(pick["start_s"] * 1000)) <= 1_000,
            # Both that an end was found at all and where: the ends the harness found match to the millisecond, so
            # comparing only None-ness would let a moved end through.
            "end_matches": got is not None and (got["end_ms"] is None) == (pick["end_s"] is None)
            and (pick["end_s"] is None or abs(got["end_ms"] - round(pick["end_s"] * 1000)) <= 1_000),
        })  # fmt: skip
    evidence["compared"] = compared
    premise = {
        "ten harness movies are mounted": len(picks) == 10,
        "both ends of Q3 are covered": any(p["end_s"] is not None for p in picks)
        and any(p["end_s"] is None for p in picks),
        "every movie stored one answer": all(len(seen.get(p["name"], [])) == 1 for p in picks),
    }
    checks = {
        "every start is within 1 s of the harness": all(c["start_matches"] for c in compared),
        "every end matches the harness's, None for None": all(c["end_matches"] for c in compared),
    }
    # The comparison is against Task 11's GPU run, on the app's GPU path: same decode path, not the same numbers as
    # the CPU harness, which disagrees with the app on three of these titles.
    notes = [f"compared against {HARNESS_ANSWERS.name} (--decode gpu)"]
    return checks_result(10, "The app reproduces the harness on real movies", premise, checks, evidence, notes)


@row(11)
def row_11_regressions() -> dict:
    """Phase 1 and phase 2 rows still pass on the phase-3 image."""
    image = sh("docker", "inspect", "-f", "{{.Config.Image}}", "mlab-app").strip()
    wanted = os.environ.get("MLAB_APP_IMAGE") or "media_preview_generator:intro-credits"
    runs: dict[str, dict] = {}
    for script, rows in (("./phase2_matrix.py", ("1", "3", "8")), ("./phase1_matrix.py", ("2", "7"))):
        # The matrix scripts aren't executable in a checkout, so they run through this interpreter.
        out = subprocess.run([sys.executable, script, "run", *rows], cwd=HERE, capture_output=True, text=True,
                             timeout=10_800)  # fmt: skip
        runs[f"{script} {' '.join(rows)}"] = {"exit": out.returncode, "tail": scrub(out.stdout[-2000:]),
                                              "stderr": scrub(out.stderr[-1000:])}  # fmt: skip
        say(f"{script} run {' '.join(rows)} -> {out.returncode}")
    premise = {"mlab-app runs the phase 3 image": image == wanted}
    checks = {f"{name} exited 0": run["exit"] == 0 for name, run in runs.items()}
    return checks_result(11, "Phase 1 and 2 regressions on the phase-3 image", premise, checks,
                         {"image": image, "wanted_image": wanted, "runs": runs})  # fmt: skip


@row(16)
def row_16_roll_to_the_end() -> dict:
    """A roll that runs to the end of the file (Q3): no end is stored and the skip goes to the end of the file."""
    evidence: dict[str, Any] = {}
    try:
        p2.set_publish_when("medium")
        with ProcessSampler() as sampler:
            job, _ = credits_job("row 16 roll to the end", force=True, paths=(OPEN_MOVIE,))
        payload = p1.item_payload(OPEN_MOVIE)
        evidence.update(job=job["id"], rows=credits_evidence(OPEN_MOVIE), decodes=sampler.decodes,
                        duration_ms=payload["duration_ms"], decision=payload["decisions"]["credits"],
                        servers={sid: p2.inspector_server(OPEN_MOVIE, sid) for sid in p2.ALL_MARKER_SERVERS})  # fmt: skip
    finally:
        p2.set_publish_when("high")
    rows = evidence.get("rows", [])
    start_ms = rows[0]["start_ms"] if rows else None
    decodes = evidence.get("decodes", [])
    refines = [a for a in decodes if " -t 21.000 " in f" {a} "]
    past_start = [a for a in decodes if start_ms is not None and (decode_start_s(a) or 0) >= start_ms / 1000]
    marker = (evidence.get("decision") or {}).get("marker") or {}

    def published(sid: str) -> dict | None:
        markers = (evidence.get("servers", {}).get(sid) or {}).get("published") or []
        return next((m for m in markers if m["type"] == "credits"), None)

    premise = {
        "the app reads the 660 s movie (the roll ends it)": abs((evidence.get("duration_ms") or 0) - OPEN_MOVIE_MS)
        <= 1_000,
        "credits text answered within 10 s of 540 s": near_truth(rows),
        # Both refine windows are -t 21.000 and the sampler saw one of them, so it was watching while the file's
        # windows were refined -- without that the check below would only be saying the sampler saw nothing.
        "the tail and a refine window were both decoded": len(decodes) >= 2 and len(refines) >= 1,
    }
    checks = {
        "the stored answer has no end": bool(rows) and rows[0]["end_ms"] is None,
        "the decision skips to the end of the file": marker.get("end_ms") is not None
        and abs(marker["end_ms"] - OPEN_MOVIE_MS) <= 1_000,
        **{f"{sid} published credits ending at the end of the file": (m := published(sid)) is not None
           and m["end_ms"] is not None and abs(m["end_ms"] - OPEN_MOVIE_MS) <= 2_000
           for sid in ("mlab-plex", "mlab-jellyfin", "mlab-jf12")},
        **{f"{sid} says nothing about skipping to the end": "Emby skips to the end of the file"
           not in ((evidence.get("servers", {}).get(sid) or {}).get("publish_message") or "")
           for sid in p2.EMBY_SERVERS},
        "no window past the credits was seen decoding": not past_start,
    }  # fmt: skip
    # The stored end being None is decisive for what was stored. It implies no end window was decoded only because
    # this fixture's coarse end sits inside the 30 s bound (rule_j.KEEP_AFTER_CREDITS_S): in general credits_end can
    # also return None after decoding the window, when the refined end fails that same check. A 0.5 s sampler can
    # miss a 21 s window's decode, so the process check above corroborates, it doesn't stand on its own.
    notes = [f"decodes seen: {len(decodes)} ({len(refines)} refine window(s))"]
    return checks_result(16, "A roll that runs to the end of the file", premise, checks, evidence, notes)


@row(17)
def row_17_locked_credits_from_credit_text() -> dict:
    """A locked credits marker whose detected answer came from credit text: a forced run still detects, doesn't
    replace it, and the Inspector shows both."""
    # phase4_matrix imports this module, so its helpers are imported when the row runs, not at load.
    import phase4_matrix as p4

    lock = (560_000, 640_000)  # off the roll's 540 s and inside the movie's last 25 %
    evidence: dict[str, Any] = {}
    try:
        p2.set_publish_when("medium")
        credits_job("row 17 detect", force=True)
        detected = p1.item_payload(MOVIE)
        evidence["detected"] = {
            "decision": detected["decisions"]["credits"],
            "credit_text": credits_evidence(MOVIE),
            "served": p4.served(MOVIE),
        }
        status, saved, _ = p1.save_markers(MOVIE, [p4.marker("credits", lock)])
        evidence["saved"] = {"status": status, "served": p4.served(MOVIE), "stored": p4.stored_times(MOVIE)}
        fetched_before = max(e["fetched_at"] for e in credits_evidence(MOVIE))
        job, files = credits_job("row 17 forced", force=True)
        forced = p1.item_payload(MOVIE)
        evidence["forced"] = {
            "job": job["id"],
            "statuses": p4.job_statuses(files, MOVIE),
            "decision": forced["decisions"]["credits"],
            "credit_text": credits_evidence(MOVIE),
            "served": p4.served(MOVIE),
            "stored": p4.stored_times(MOVIE),
        }
    finally:
        cleanup_errors = p1.run_cleanup(
            lambda: p1.unlock_markers(MOVIE, ["credits"]),
            lambda: p2.set_publish_when("high"),
            lambda: credits_job("row 17 cleanup", force=True),
            lambda: evidence.update(cleanup={"served": p4.served(MOVIE), "stored": p4.stored_times(MOVIE)}),
        )
    detected_decision = evidence["detected"]["decision"]["marker"] or {}
    forced_decision = evidence["forced"]["decision"]["marker"] or {}
    forced_text = evidence["forced"]["credit_text"]
    premise = {
        "credit text alone decided the credits at Medium, near the roll's 540 s": "credits_text"
        in (detected_decision.get("decided_by") or [])
        and near_truth(evidence["detected"]["credit_text"])
        and not detected_decision.get("locked"),
        "the lock took on every server before the forced run": status == 200
        and evidence["saved"]["served"] == p4.expected(None, lock),
    }
    checks = {
        "the forced run completed with every server up to date": job["status"] == "completed"
        and evidence["forced"]["statuses"] == dict.fromkeys(p2.ALL_MARKER_SERVERS, "markers_up_to_date"),
        "it detected again: the credit text evidence is newer and still near 540 s": near_truth(forced_text)
        and max(e["fetched_at"] for e in forced_text) > fetched_before,
        "it did not replace the lock: the decision is still the user's, at the locked times": (
            forced_decision.get("start_ms"), forced_decision.get("end_ms"), forced_decision.get("locked")
        ) == (*lock, True)
        and forced_decision.get("decided_by") == ["user"],
        "the Inspector shows both: the locked decision and the credit text row": forced["decisions"]["credits"][
            "reason"
        ]
        == "locked by user"
        and any(e["source"] == "credits_text" and e["type"] == "credits" for e in forced_text),
        "every server still serves the locked times and not credit text's": evidence["forced"]["served"]
        == p4.expected(None, lock),
        "markers.db holds the lock": evidence["forced"]["stored"] == {"credits": (*lock, 1)},
        "unlock and a High run leave none of the locked times on any server": all(
            all(t != "credits" or s != lock[0] for t, s, _ in rows) for rows in evidence["cleanup"]["served"].values()
        ),
        "and the row is no longer locked in markers.db": evidence["cleanup"]["stored"].get("credits", (0, 0, 0))[2] == 0,
        "the lab was put back (unlocked, High, movie re-run)": not cleanup_errors,
    }  # fmt: skip
    return checks_result(17, "A locked credits marker whose answer came from credit text", premise, checks, evidence)


# ------------------------------------------------------------------------------------------------------- driver


def configure() -> None:
    """Phase 2's configure plus the Synth Credits library and its two movies on every lab server."""
    p2.configure()
    listed = p2.rescan_until(CREDITS_LIBRARY, [MOVIE, OPEN_MOVIE], present=True)
    for sid in p2.ALL_MARKER_SERVERS:
        libs = app_ok("POST", f"/api/servers/{sid}/refresh-libraries")["libraries"]
        # A library the app first sees here comes back off, and the Inspector's search skips off libraries: row 2
        # would find no result to open.
        if any(lib["name"] == CREDITS_LIBRARY and not lib["enabled"] for lib in libs):
            on = [{**lib, "enabled": True} if lib["name"] == CREDITS_LIBRARY else lib for lib in libs]
            app_ok("PUT", f"/api/servers/{sid}", {"libraries": on})
            say(f"{sid}: {CREDITS_LIBRARY} turned on in the app")
    say("Synth Credits listed:", listed, "local sources:", app_ok("GET", "/api/markers/sources/local"))


def main(argv: list[str]) -> int:
    """Run the matrix.

    Args:
        argv: ``configure``, ``rows``, or ``run`` and row numbers.

    Returns:
        0 when every row asked for passed, 1 when one failed, 2 for a bad command line, 3 when every row asked for
        was owner-gated and nothing ran.
    """
    # A stopped run (SIGTERM) still runs the finally blocks that recreate the app and put lab state back.
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
    wanted = [int(a) for a in argv[1:]]
    failed = 0
    for number in wanted:
        if number in OWNER_GATED:
            say(f"row {number} runs on the plex host through plex_rows.py (Task 13 Step 6, Q7): not run here")
            continue
        failed += ROWS[number]()["result"] != "pass"
    if wanted and all(number in OWNER_GATED for number in wanted):
        # Nothing ran and no result file was written. Exiting 0 would read to a caller exactly like a pass.
        say("nothing ran: every row asked for is owner-gated to the plex host")
        return 3
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
