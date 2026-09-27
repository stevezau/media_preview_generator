"""Run tools.markers_eval from a chosen tree with the local-only evidence folder (scratch wrapper).

Usage: harness.py <tree> <markers_eval args...>
"""

import os
import sys

tree = sys.argv[1]
os.environ.setdefault(
    "MARKERS_EVAL_EVIDENCE", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
)
os.environ.setdefault(
    "MEDIA_PREVIEW_TEXTDET_MODEL",
    "/home/data/.cache/uv/archive-v0/z8hlsUamKYTC1MWB/rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx",
)
sys.path.insert(0, tree)
os.chdir(tree)
from tools.markers_eval.__main__ import main  # noqa: E402

import media_preview_generator  # noqa: E402

assert media_preview_generator.__file__.startswith(tree), media_preview_generator.__file__
sys.exit(main(sys.argv[2:]))
