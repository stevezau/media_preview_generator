"""Scratch: is_prose of the frame read 0, 1, 2 s after the rule-J start, for every file whose start the first work run
moved (ct_work.json prose_start_s), against its first card's read (the full walk). CPU, read-only on media.

Usage: probe_offsets.py [offsets...]
"""

import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
tree = "/home/data/workspace/plex_generate_vid_previews"
sys.path.insert(0, tree)
from loguru import logger  # noqa: E402

logger.remove()
from media_preview_generator.markers.credits import cards, frames, textdet, textrec  # noqa: E402

MODELS = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models"
det = textdet.TextDetector(textdet.cpu_session(f"{MODELS}/ch_PP-OCRv4_det_infer.onnx"), backend="cpu")
reader = textrec.TextReader(det, textrec.cpu_session(f"{MODELS}/latin_PP-OCRv5_rec_mobile.onnx", 4))
ffmpeg = shutil.which("ffmpeg")
offsets = [float(x) for x in sys.argv[1:]] or [0.0, 1.0, 2.0]
work = json.load(open(HERE / "ct_work.json"))
for path, r in sorted(work.items(), key=lambda kv: os.path.basename(kv[0])):
    if "error" in r or r.get("prose_start_s") is None:
        continue
    start = r["prose_start_s"]
    row = []
    for off in offsets:
        lines = frames.read_text_at(path, ffmpeg=ffmpeg, at_s=start + off, scale=cards.READ_SCALE, gpu=None,
                                    gpu_device_path=None, read_text=reader.read, start_time_s=None)  # fmt: skip
        row.append(f"+{off:g}:{'P' if cards.is_prose(lines) else '-'}{len(lines)}")
    print(f"{os.path.basename(path)[:45]:45} {start:8.1f} {' '.join(row)}", flush=True)
_ = np
