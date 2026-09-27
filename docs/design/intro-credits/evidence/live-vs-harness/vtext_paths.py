"""vtext.py for a {path: is_episode} file: the tree's credit text through the harness caches (GPU cuda:0)."""

import json
import os
import shutil
import sys
import time
from pathlib import Path

tree, paths_file, out_path = sys.argv[1], sys.argv[2], Path(sys.argv[3]).resolve()
os.environ.setdefault(
    "MARKERS_EVAL_EVIDENCE", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
)
os.environ.setdefault(
    "MEDIA_PREVIEW_TEXTDET_MODEL",
    "/home/data/.cache/uv/archive-v0/z8hlsUamKYTC1MWB/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx",
)
files = json.load(open(paths_file))
sys.path.insert(0, tree)
os.chdir(tree)
import numpy as np  # noqa: E402

import media_preview_generator  # noqa: E402

assert media_preview_generator.__file__.startswith(tree)
from media_preview_generator.markers.credits.frames import FRAME_H, FRAME_W  # noqa: E402
from media_preview_generator.markers.probe import ffprobe_path_for  # noqa: E402
from tools.markers_eval.cache import ProbeCache  # noqa: E402
from tools.markers_eval.credits_text import CreditsTextCache, _detection_on, decode_digest  # noqa: E402
from tools.markers_eval.decode_cache import DecodeCache  # noqa: E402

root = Path.home() / ".cache/markers_eval"
ffmpeg = shutil.which("ffmpeg")
probes = ProbeCache(root, ffprobe=ffprobe_path_for(ffmpeg))
detection = _detection_on("gpu", "cuda:0")
results = json.load(open(out_path)) if out_path.exists() else {}
try:
    detection.detect_boxes(np.zeros((1, FRAME_H, FRAME_W), dtype=np.uint8))
    decodes = DecodeCache(root, digest=decode_digest(), backend=detection.backend)
    cache = CreditsTextCache(
        root,
        ffmpeg=ffmpeg,
        decode="gpu",
        gpu_device="cuda:0",
        detect_boxes=detection.detect_boxes,
        backend=detection.backend,
        probe=probes.probe,
        decodes=decodes,
    )
    for n, (path, episode) in enumerate(files.items(), 1):
        if path in results and "error" not in results[path]:
            continue
        t0 = time.monotonic()
        try:
            r = cache.result(path, is_episode=bool(episode))
        except Exception as exc:
            results[path] = {"error": f"{type(exc).__name__}: {exc}"}
            print(n, "ERROR", exc, flush=True)
            continue
        results[path] = r
        print(
            n,
            len(files),
            f"{time.monotonic() - t0:.1f}s",
            r["start_s"],
            r["end_s"],
            os.path.basename(path)[:60],
            flush=True,
        )
        json.dump(results, open(out_path, "w"))
finally:
    detection.close()
