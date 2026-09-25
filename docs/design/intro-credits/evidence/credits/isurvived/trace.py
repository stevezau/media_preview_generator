"""Trace the app's own find_credits on one file: every reading (320x180, then 640x360), its coarse start, and each
keyframe row in a window with its boxes, as decoded and as rule J found the runs on them. Read-only on the media.

Usage (storage, from a checkout or a copy of one; one heavy job at a time)::

    nice -n 19 python trace.py TREE PATH [--movie] [--vendor] [--decode gpu|cpu] [--from S] [--to S]

``--vendor`` decodes the 320x180 keyframe pass with each vendor's own scaler (version 3's: ``scale_cuda`` on NVIDIA,
swscale's bicubic on the CPU) instead of the one nearest-pixel scaler. Decodes go through the harness's decode cache
(``~/.cache/markers_eval``) under the backend ``TRACE_BACKEND`` names (default ``cpu``; ``webgpu cuda:0`` reads what a
GPU harness run decoded, and then nothing is written back), with ``TRACE_FFMPEG`` as the ffmpeg binary (the harness
keys its decodes on ``/usr/bin/ffmpeg``).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("worktree")
parser.add_argument("path")
parser.add_argument("--vendor", action="store_true", help="v3's per-vendor scaler for the 320x180 keyframe pass")
parser.add_argument("--decode", default="gpu")
parser.add_argument("--from", dest="lo", type=float, default=0.0)
parser.add_argument("--to", dest="hi", type=float, default=1e9)
parser.add_argument("--nocache", action="store_true")
parser.add_argument("--movie", action="store_true")
args = parser.parse_args()
sys.path.insert(0, args.worktree)
os.environ.setdefault(
    "MEDIA_PREVIEW_TEXTDET_MODEL",
    "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/bench/textdet-model/"
    "ch_PP-OCRv4_det_infer.onnx",
)
from media_preview_generator.markers.credits import detector, frames, rule_j, textdet, textdet_helper  # noqa: E402
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms  # noqa: E402
from tools.markers_eval.credits_text import decode_digest  # noqa: E402
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402

det = textdet.TextDetector(textdet.cpu_session(textdet_helper.model_path()), backend="cpu")

if args.vendor:
    real = frames.decode_command

    def vendor(*a, **k):
        if k.get("scale", 1) == 1:
            k["vendor_scaler"] = True
        return real(*a, **k)

    frames.decode_command = vendor

orig_read = detector._read_credits
orig_coarse = rule_j.coarse_start
state = {"scale": None}


def fmt(row):
    boxes = rule_j.boxes_of(row)
    return f"{row[0]:9.2f} n={row[1]:2d} L={row[2]:6.1f} " + " ".join(f"[{b[0]},{b[1]},{b[2]},{b[3]}]" for b in boxes)


def read(*a, **k):
    state["scale"] = k.get("scale")
    res = orig_read(*a, **k)
    print(
        f"== reading scale={k.get('scale')} from_s={k.get('from_s')}: start={res.start_s} end={res.end_s} "
        f"overlays={res.overlays}"
    )
    rows = res.run_rows if res.run_rows else res.key_rows
    runs = rule_j.credit_runs(rows)
    print("   runs (on run rows):", [(round(rows[first][0], 1), round(rows[last][0], 1)) for first, last in runs])
    print("   key rows in window (as decoded):")
    for row in res.key_rows:
        if args.lo <= row[0] <= args.hi:
            print("     ", fmt(row))
    if res.run_rows:
        print("   run rows in window (without 320 text):")
        for row in res.run_rows:
            if args.lo <= row[0] <= args.hi:
                print("     ", fmt(row))
    return res


def coarse(*a, **k):
    out = orig_coarse(*a, **k)
    print(f"  [scale {state['scale']}] coarse_start -> {out}")
    return out


detector._read_credits = read
rule_j.coarse_start = coarse
dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", args.path],
                           capture_output=True, text=True, check=True).stdout.strip())  # fmt: skip
dur_ms = int(round(dur * 1000))
window_ms, cap_ms = credits_limits_ms(is_episode=not args.movie, tv_window_s=None, movie_window_s=None)
earliest = earliest_credits_start_ms(dur_ms, is_movie=args.movie, credits_window_ms=window_ms,
                                     movie_credits_max_from_end_ms=cap_ms) / 1000.0  # fmt: skip
gpu = "NVIDIA" if args.decode == "gpu" else None
if os.environ.get("TRACE_BACKEND"):
    DecodeCache._keep = lambda *a, **k: None  # read-only on the harness cache
cache = DecodeCache(
    Path.home() / ".cache/markers_eval", digest=decode_digest(), backend=lambda: os.environ.get("TRACE_BACKEND", "cpu")
)
kw = dict(duration_ms=dur_ms, is_episode=not args.movie, ffmpeg=os.environ.get("TRACE_FFMPEG", "ffmpeg"), detect_boxes=det.detect, gpu=gpu,
          gpu_device_path="cuda:0" if gpu else None, earliest_start_s=earliest)  # fmt: skip
if args.nocache:
    res = detector.find_credits(args.path, **kw)
else:
    with cache.serving():
        res = detector.find_credits(args.path, **kw)
print(
    f"RESULT start={res.start_s} end={res.end_s} scale={res.scale} dur={dur} decoded={cache.decoded} "
    f"reused={cache.reused}"
)
