"""Throwaway, read-only: credit text v7 on 10 Things I Hate About You, the app's own find_credits + chapter_origin, on
the worker device named in argv (Intel VAAPI or NVIDIA), with the app's text detection pool."""

import sys
import time

from media_preview_generator.markers.credits import detector, frames
from media_preview_generator.markers.credits.textdet_helper import get_textdet_pool
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms

print("CREDITS_TEXT_VERSION", detector.CREDITS_TEXT_VERSION, flush=True)
path, gpu, dev = sys.argv[1], sys.argv[2], sys.argv[3]
gpu = None if gpu == "cpu" else gpu
dev = None if dev == "cpu" else dev
duration_ms, chapter_ms = 5854464, 5529232
window_ms, cap = credits_limits_ms(is_episode=False, tv_window_s=None, movie_window_s=None)
earliest = (
    earliest_credits_start_ms(
        duration_ms, is_movie=True, credits_window_ms=window_ms, movie_credits_max_from_end_ms=cap
    )
    / 1000
)
pool = get_textdet_pool()
t0 = time.monotonic()
r = detector.find_credits(
    path,
    duration_ms=duration_ms,
    is_episode=False,
    tail_s=frames.tail_length_s(is_episode=False, tv_s=None, movie_s=None),
    ffmpeg="ffmpeg",
    detect_boxes=lambda planes: pool.detect_boxes(
        planes, gpu=gpu, gpu_device_path=dev, gpu_worker=gpu is not None, on_cpu=print
    ),
    gpu=gpu,
    gpu_device_path=dev,
    earliest_start_s=earliest,
)
print("start", r.start_s, "end", r.end_s, "rows", len(r.key_rows), f"{time.monotonic() - t0:.1f}s", flush=True)
print("origin", detector.chapter_origin(r, chapter_ms), flush=True)
print(
    "keyframe boxes 5520-5745:", [(round(row[0]), row[1]) for row in r.key_rows if 5520 <= row[0] <= 5745], flush=True
)
