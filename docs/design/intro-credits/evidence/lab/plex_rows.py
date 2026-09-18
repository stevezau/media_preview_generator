#!/usr/bin/env python3
"""Phase 3 rows 12-15, run inside a throwaway app container on the plex host (plan-phase3 Task 13 Step 6).

    python3 plex_rows.py 12|13|14|15     prints one JSON result

Only the synthetic movie in /work is read; nothing is written outside the container.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from loguru import logger

from media_preview_generator.markers.credits.detector import find_credits
from media_preview_generator.markers.credits.textdet import synthetic_frames
from media_preview_generator.markers.credits.textdet_helper import TextDetectorPool
from media_preview_generator.markers.probe import ffprobe_path_for, probe_media

MOVIE = "/work/Synth Credits (2024).mkv"
LOG: list[str] = []
logger.add(lambda message: LOG.append(str(message).strip()), level="INFO")


def intel_render_node() -> str | None:
    """The Intel GPU's render node, by its sysfs vendor id (the host's node numbers aren't assumed).

    Returns:
        ``/dev/dri/renderDNNN``, or None when no Intel render node is there.
    """
    for vendor in sorted(Path("/sys/class/drm").glob("renderD*/device/vendor")):
        if vendor.read_text().strip() == "0x8086":
            return f"/dev/dri/{vendor.parent.parent.name}"
    return None


DEVICES = {"NVIDIA": "cuda:0", "INTEL": intel_render_node()}


def counts_row(vendor: str) -> dict:
    """One vendor's helper against the CPU helper on the same 20 synthetic frames.

    Args:
        vendor: ``NVIDIA`` or ``INTEL``.

    Returns:
        The backend the helper came up on, whether the counts match the CPU's, how long it took, and the
        "Credit text detection on ..." lines the pool logged.
    """
    pool = TextDetectorPool()
    frames = synthetic_frames(20)
    try:
        started = time.monotonic()
        gpu_counts = pool.count_boxes(frames, gpu=vendor, gpu_device_path=DEVICES[vendor])
        backend = pool.backend_of(vendor, DEVICES[vendor])
        cpu_counts = pool.count_boxes(frames, gpu=None, gpu_device_path=None)
        return {"vendor": vendor, "backend": backend, "same_counts": gpu_counts == cpu_counts,
                "seconds": round(time.monotonic() - started, 1),
                "log": [line for line in LOG if "Credit text detection on" in line]}  # fmt: skip
    finally:
        pool.close_all()


def movie_duration_ms() -> int:
    """The synthetic movie's own duration.

    Probed, never hardcoded: a number copied from the fixture is a number that goes stale when the fixture is
    reshaped, and a wrong duration silently changes the answer (`keeps_a_scene_after` decides whether an end is kept
    at all), so this row would fail for a reason that has nothing to do with VAAPI.

    Returns:
        The container duration in milliseconds.

    Raises:
        RuntimeError: ffprobe reports no duration.
    """
    probe = probe_media(MOVIE, ffprobe=ffprobe_path_for("ffmpeg"))
    if not probe.duration_ms:
        raise RuntimeError(f"no duration for {MOVIE}")
    return probe.duration_ms


def vaapi_row() -> dict:
    """The synthetic movie read through the Intel VAAPI decode path, beside the CPU's answer.

    Returns:
        Both answers, the duration they were read with, and whether they agree within 2 s at each end.
    """
    pool = TextDetectorPool()
    try:
        common = {"duration_ms": movie_duration_ms(), "is_episode": False, "ffmpeg": "ffmpeg"}
        cpu = find_credits(MOVIE, count_boxes=lambda p: pool.count_boxes(p, gpu=None, gpu_device_path=None),
                           gpu=None, gpu_device_path=None, **common)  # fmt: skip
        intel = find_credits(
            MOVIE,
            count_boxes=lambda p: pool.count_boxes(p, gpu="INTEL", gpu_device_path=DEVICES["INTEL"]),
            gpu="INTEL",
            gpu_device_path=DEVICES["INTEL"],
            **common,
        )
        return {"duration_ms": common["duration_ms"],
                "cpu_start_s": cpu.start_s, "intel_start_s": intel.start_s, "cpu_end_s": cpu.end_s, "intel_end_s": intel.end_s,
                "within_2s": cpu.start_s is not None and intel.start_s is not None and abs(cpu.start_s - intel.start_s) <= 2
                and cpu.end_s is not None and intel.end_s is not None and abs(cpu.end_s - intel.end_s) <= 2}  # fmt: skip
    finally:
        pool.close_all()


def main(row: str) -> int:
    """Run one row and print its JSON result.

    Args:
        row: ``12``, ``13``, ``14`` or ``15``.

    Returns:
        0 when the row ran, 1 when its premise doesn't hold, 2 for anything else.
    """
    premise = {"an NVIDIA GPU is visible": Path("/dev/nvidiactl").exists(),
               "an Intel render node was found": DEVICES["INTEL"] is not None}  # fmt: skip
    if row != "12" and not premise["an Intel render node was found"]:
        print(json.dumps({"row": int(row), "premise": premise, "pass": False}, indent=1))
        return 1
    if row == "12":
        result = counts_row("NVIDIA")
        result["pass"] = premise["an NVIDIA GPU is visible"] and result["backend"] == "webgpu" and result["same_counts"]
    elif row == "13":
        result = counts_row("INTEL")
        result["pass"] = result["backend"] == "cpu" and any("slower than the CPU" in line for line in result["log"])
    elif row == "14":
        runs, deadline = [], time.monotonic() + 40
        while time.monotonic() < deadline:  # a new pool per run: every run starts a helper and self-tests again
            runs.append(counts_row("INTEL")["backend"])
        # Judged from the host's pmon / intel_gpu_top output pasted into the notes, not here.
        result = {"runs": runs, "pass": None}
    elif row == "15":
        result = vaapi_row()
        result["pass"] = result["within_2s"]
    else:
        print(__doc__)
        return 2
    print(json.dumps({"row": int(row), "premise": premise, "intel_node": DEVICES["INTEL"], **result}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
