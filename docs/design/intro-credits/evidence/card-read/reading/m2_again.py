"""Scratch: the research's M2 cards read again through the app's own path (GPU decode, luma, one scaler, textrec) at
two sizes, to pick the reading size.

Usage: m2_again.py <scale>...   -> m2_<scale>.json
"""

import json
import os
import sys
import time

CODE = os.environ.get(
    "CODE", "/home/data/workspace/plex_generate_vid_previews"
)
sys.path.insert(0, CODE)
DET = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/ch_PP-OCRv4_det_infer.onnx"
REC = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models/latin_PP-OCRv5_rec_mobile.onnx"
import numpy as np  # noqa: E402

from media_preview_generator.markers.credits import cards, frames, textdet, textrec  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
R = json.load(open(os.path.join(HERE, "..", "priorart", "p2", "results.json")))
det = textdet.TextDetector(textdet.cpu_session(DET, 2), backend="cpu")
reader = textrec.TextReader(det, textrec.cpu_session(REC, 2))
for scale in [int(s) for s in sys.argv[1:]]:
    out = []
    for r in R:
        if "path" not in r:
            continue
        planes = []

        def capture(p):
            planes.extend(np.array(x) for x in p)
            return [() for _ in p]

        thin = frames.keyframe_thinning(r["path"], "ffmpeg")
        frames.decode_rows(r["path"], ffmpeg="ffmpeg", start_s=r["t"], length_s=1.0, keyframes_only=False, fps=1,
                           gpu="NVIDIA", gpu_device_path="cuda:0", download_format=thin.download_format,
                           detect_boxes=capture, scale=scale)  # fmt: skip
        t0 = time.time()
        lines = reader.read(planes[:1])[0] if planes else []
        took = time.time() - t0
        out.append({"label": r["label"], "kind": r["kind"], "lines": lines, "prose": cards.is_prose(lines),
                    "research_prose": None, "s": took})  # fmt: skip
        print(scale, r["label"], r["kind"], "PROSE" if cards.is_prose(lines) else "     ", f"{took:.1f}s",
              " | ".join(lines)[:140], flush=True)  # fmt: skip
    json.dump(out, open(os.path.join(HERE, f"m2_{scale}.json"), "w"), indent=1)
