#!/usr/bin/env python3
"""Run the shipped credit-text detector on every episode of one season, inside an app container (phase 3 close-out).

    python3 plex_season_detect.py "<season folder>" [<GPU type> <device>]    e.g. NVIDIA cuda:0

One JSON line per episode, then the text detection backend and the pool's "Credit text detection" lines. Warnings
and errors go to stderr. Reads the season folder only (mount it read-only); writes nothing outside the process.
"""

from __future__ import annotations

import json
import os
import sys
import time

from loguru import logger

from media_preview_generator.markers.credits.detector import find_credits
from media_preview_generator.markers.credits.textdet_helper import TextDetectorPool
from media_preview_generator.markers.probe import ffprobe_path_for, probe_media

USAGE = __doc__.splitlines()[2].strip()
LOG: list[str] = []


def detect(path: str, pool: TextDetectorPool, gpu: str | None, device: str | None) -> dict:
    """One file's answer, or the error that stopped it.

    Args:
        path: The episode.
        pool: The text detection helpers.
        gpu: The GPU type, None for the CPU.
        device: The GPU's device.

    Returns:
        The file's duration, chapter count and time taken, plus its start/end or an ``error``.
    """
    started = time.monotonic()
    row: dict = {"file": os.path.basename(path)}
    try:
        probe = probe_media(path, ffprobe=ffprobe_path_for("ffmpeg"))
        if not probe.duration_ms:
            raise ValueError("duration unknown")
        row.update(duration_s=round(probe.duration_ms / 1000, 1), chapters=len(probe.chapters))
        result = find_credits(
            path,
            duration_ms=probe.duration_ms,
            is_episode=True,
            ffmpeg="ffmpeg",
            count_boxes=lambda planes: pool.count_boxes(planes, gpu=gpu, gpu_device_path=device),
            gpu=gpu,
            gpu_device_path=device,
        )
        row.update(start_s=result.start_s, end_s=result.end_s)
    except Exception as exc:  # every failure kind is a row of its own; the season carries on
        row["error"] = f"{type(exc).__name__}: {exc}"
    row["seconds"] = round(time.monotonic() - started, 1)
    return row


def main(argv: list[str]) -> int:
    """Detect every ``.mkv`` in the season folder.

    Args:
        argv: The season folder, optionally followed by a GPU type and its device.

    Returns:
        0, or 2 on a usage error.
    """
    if len(argv) not in (1, 3):
        print(f"usage: {USAGE}", file=sys.stderr)
        return 2
    season = argv[0]
    gpu, device = (argv[1], argv[2]) if len(argv) == 3 else (None, None)
    logger.remove()
    logger.add(lambda message: LOG.append(str(message).strip()), level="INFO")
    logger.add(sys.stderr, level="WARNING")
    pool = TextDetectorPool()
    backend = None
    try:
        for name in sorted(n for n in os.listdir(season) if n.endswith(".mkv")):
            print(json.dumps(detect(os.path.join(season, name), pool, gpu, device)), flush=True)
        backend = pool.backend_of(gpu, device) if gpu else "cpu"
    finally:
        pool.close_all()
    print(json.dumps({"backend": backend, "log": [line for line in LOG if "Credit text detection" in line]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
