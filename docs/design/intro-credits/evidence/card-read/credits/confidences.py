"""Scratch: per-line recognition confidence on cards, by name and second (CPU, read-only on media).

Usage: confidences.py "<name substring>@<second>" ...
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
from media_preview_generator.markers.credits import frames, textdet, textrec  # noqa: E402

MODELS = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models"
detector = textdet.TextDetector(textdet.cpu_session(f"{MODELS}/ch_PP-OCRv4_det_infer.onnx"), backend="cpu")
reader = textrec.TextReader(detector, textrec.cpu_session(f"{MODELS}/latin_PP-OCRv5_rec_mobile.onnx", 4))
known = json.load(open(HERE / "ct_base.json"))
ffmpeg = shutil.which("ffmpeg")
for arg in sys.argv[1:]:
    name, at = arg.rsplit("@", 1)
    path = next(p for p in known if name in os.path.basename(p))
    got = []

    def read_text(planes):
        for plane in planes:
            image = np.stack([plane] * 3, axis=-1)
            quads = detector.boxes(image)
            lines = []
            for i in textrec.reading_order(quads):
                text, conf = reader.read_line(textrec.crop(image, quads[i]))
                lines.append((round(conf, 2), text))
            got.append(lines)
        return [[t for _, t in lines] for lines in got]

    frames.read_text_at(path, ffmpeg=ffmpeg, at_s=float(at), scale=4, gpu=None, gpu_device_path=None,
                        read_text=read_text, start_time_s=0.0)  # fmt: skip
    confs = [c for c, _ in got[0]] if got else []
    print(f"== {name} @ {at}: median {np.median(confs) if confs else None:.2f} min {min(confs) if confs else None}")
    for c, t in got[0] if got else []:
        print(f"   {c:.2f} {t[:80]}")
