"""The app's credit text answer (find_credits through the harness caches) for every credits file of the verdict and
Plex sets, from one tree. GPU decode on cuda:0 with the worker's CPU rerun, nice 19 (run it under nice).

Usage: vtext.py <tree> <out.json> [--ids S03,S04,...]
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

tree = sys.argv[1]
out_path = Path(sys.argv[2]).resolve()
os.environ.setdefault(
    "MARKERS_EVAL_EVIDENCE", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
)
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
from tools.markers_eval.credits_text import CreditsTextCache, _detection_on, decode_digest  # noqa: E402
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402

HERE = Path(__file__).resolve().parent
items = json.load(open(HERE / "items.json"))
wanted = None
if "--ids" in sys.argv:
    wanted = set(sys.argv[sys.argv.index("--ids") + 1].split(","))
if "--ids-file" in sys.argv:
    wanted = set(open(sys.argv[sys.argv.index("--ids-file") + 1]).read().strip().split(","))
files = {}
for it in items["verdict"] + items["plex"]:
    if it["type"] != "credits" or not it["on_disk"]:
        continue
    if wanted is not None and it["id"] not in wanted:
        continue
    files[it["path"]] = it["episode"]
extra = HERE / "extra_files.json"
if extra.exists() and wanted is None:
    for path, episode in json.load(open(extra)).items():
        if os.path.exists(path):
            files.setdefault(path, episode)

root = Path(os.environ.get("MARKERS_EVAL_CACHE") or Path.home() / ".cache/markers_eval")
ffmpeg = shutil.which("ffmpeg")
probes = ProbeCache(root, ffprobe=ffprobe_path_for(ffmpeg))
detection = _detection_on("gpu", "cuda:0")
results = json.load(open(out_path)) if out_path.exists() else {}
try:
    detection.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
    decodes = DecodeCache(root, digest=decode_digest(), backend=detection.backend)
    cache = CreditsTextCache(root, ffmpeg=ffmpeg, decode="gpu", gpu_device="cuda:0",
                             detect_boxes=detection.detect_boxes, backend=detection.backend, probe=probes.probe,
                             decodes=decodes)  # fmt: skip
    print("detector", cache.detector_digest, "decode", decodes.digest, "backend", detection.backend(), flush=True)
    for n, (path, episode) in enumerate(files.items(), 1):
        if path in results and "error" not in results[path] and "--again" not in sys.argv:
            continue
        t0 = time.monotonic()
        try:
            r = cache.result(path, is_episode=episode)
        except Exception as exc:  # noqa: BLE001 - scratch runner: record and go on
            results[path] = {"error": f"{type(exc).__name__}: {exc}"}
            print(n, len(files), "ERROR", os.path.basename(path), exc, flush=True)
            continue
        results[path] = r
        print(n, len(files), f"{time.monotonic() - t0:.1f}s", r["start_s"], r["end_s"], r["scale"],
              os.path.basename(path)[:60], flush=True)  # fmt: skip
        if n % 10 == 0:
            json.dump(results, open(out_path, "w"))
    json.dump(results, open(out_path, "w"))
    print("decodes", decodes.decoded, "reused", decodes.reused, "fallbacks", sorted(cache.gpu_fallbacks), flush=True)
finally:
    detection.close()
