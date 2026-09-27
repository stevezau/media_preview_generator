"""The app's credit text answer, with its rows, for every file of the harness sets (80, 205, Accused, I Survived) from
one tree, through the harness's caches (run the harness on that tree first, or this decodes). GPU decode on cuda:0,
the worker's CPU rerun; run it under nice.

Usage: allsets.py <tree> <out.json>
"""

import json
import os
import shutil
import sys
from pathlib import Path

tree = sys.argv[1]
out_path = Path(sys.argv[2]).resolve()
EVIDENCE = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
os.environ.setdefault("MARKERS_EVAL_EVIDENCE", EVIDENCE)
os.environ.setdefault(
    "MEDIA_PREVIEW_TEXTDET_MODEL",
    "/home/data/.cache/uv/archive-v0/z8hlsUamKYTC1MWB/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx",
)
sys.path.insert(0, tree)
os.chdir(tree)
import numpy as np  # noqa: E402

import media_preview_generator  # noqa: E402

assert media_preview_generator.__file__.startswith(tree), media_preview_generator.__file__
from media_preview_generator.markers.credits.frames import FRAME_H, FRAME_W  # noqa: E402
from media_preview_generator.markers.probe import ffprobe_path_for  # noqa: E402
from tools.markers_eval.cache import ProbeCache  # noqa: E402
from tools.markers_eval.credits_text import (  # noqa: E402
    REGRESSION_SETS,
    CreditsTextCache,
    _detection_on,
    _truth,
    decode_digest,
    on_disk,
)
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402


def load(name: str):
    return json.loads(Path(EVIDENCE, "credits", f"{name}.json").read_text())


adjudicated = load("adjudicated")
files = []
for name, episode in (("movies40", False), ("tv40", True), ("movie_credit_truth", False)):
    for f in load(name):
        files.append((name, f["file"], _truth(f, adjudicated), episode))
for name, rel in REGRESSION_SETS.items():
    for path, start in json.loads(Path(EVIDENCE, rel).read_text()).items():
        if not path.startswith("_"):
            files.append((name, path, float(start), True))

root = Path(os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
ffmpeg = shutil.which("ffmpeg")
probes = ProbeCache(root, ffprobe=ffprobe_path_for(ffmpeg))
detection = _detection_on("gpu", "cuda:0")
out = {}
try:
    detection.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
    decodes = DecodeCache(root, digest=decode_digest(), backend=detection.backend)
    cache = CreditsTextCache(root, ffmpeg=ffmpeg, decode="gpu", gpu_device="cuda:0",
                             detect_boxes=detection.detect_boxes, backend=detection.backend, probe=probes.probe,
                             decodes=decodes)  # fmt: skip
    for name, path, truth, episode in files:
        if not on_disk(path):
            continue
        r = cache.result(path, is_episode=episode)
        out[path] = {"set": name, "truth": truth, "episode": episode,
                     "duration": probes.probe(path).duration_ms, **r}  # fmt: skip
    print("files", len(out), "decodes", decodes.decoded, "reused", decodes.reused, flush=True)
finally:
    detection.close()
json.dump(out, open(out_path, "w"))
