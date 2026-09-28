"""Scratch: read every text second after a credits start at full size through the app's decode path (luma, one
scaler), and print what the Latin recognition model reads and whether the prose rule calls it prose.

Usage: read_cards.py <target key> <start|seconds> <length> [scale]
"""

import json
import os
import re
import sys
import time

CODE = os.environ.get(
    "CODE", "/home/data/workspace/plex_generate_vid_previews"
)
sys.path.insert(0, CODE)
DET = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/ch_PP-OCRv4_det_infer.onnx"
REC = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/latin_PP-OCRv5_rec_mobile.onnx"
os.environ.setdefault("MEDIA_PREVIEW_TEXTDET_MODEL", DET)
import numpy as np  # noqa: E402

from media_preview_generator.markers.credits import frames, textdet, textrec  # noqa: E402

WORD = re.compile(r"[^\W\d_]+")


def prose(lines):
    for text in lines:
        words = WORD.findall(text)
        if text.rstrip().endswith("."):
            return True
        if len(words) >= 7 and 2 * sum(w[0].islower() for w in words) > len(words):
            return True
    return False


T = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.json")))[sys.argv[1]][0]
path = T["path"]
start = T["start"] if sys.argv[2] == "start" else float(sys.argv[2])
length = float(sys.argv[3])
scale = int(sys.argv[4]) if len(sys.argv) > 4 else 6
det = textdet.TextDetector(textdet.cpu_session(DET, 4), backend="cpu")
reader = textrec.TextReader(det, textrec.cpu_session(REC, 4))
planes_seen = []


def capture(planes):
    planes_seen.extend(np.array(p) for p in planes)
    return [() for _ in planes]


thinning = frames.keyframe_thinning(path, "ffmpeg")
t0 = time.time()
rows = frames.decode_rows(path, ffmpeg="ffmpeg", start_s=max(0.0, start - 2), length_s=length, keyframes_only=False,
                          fps=1, gpu="NVIDIA", gpu_device_path="cuda:0", download_format=thinning.download_format,
                          detect_boxes=capture, scale=scale)  # fmt: skip
t1 = time.time()
print(os.path.basename(path)[:90], "truth", T["truth"], "start", T["start"], f"decode {t1 - t0:.1f}s")
for (pts, _n, luma, _b), plane in zip(rows, planes_seen, strict=True):
    t2 = time.time()
    lines = reader.read(plane[None])[0]
    took = time.time() - t2
    if lines:
        print(
            f"{pts:8.1f} luma {luma:5.1f} {'PROSE' if prose(lines) else '     '} {took:4.1f}s | "
            + " | ".join(lines)[:220]
        )
    else:
        print(f"{pts:8.1f} luma {luma:5.1f}")
