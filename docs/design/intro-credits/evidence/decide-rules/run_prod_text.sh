#!/bin/bash
# Credit text answers for the prod files the rules touch (read-only media, nice 19, one job).
set -euo pipefail
readonly R="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCAL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/decide-rules/local
export MEDIA_PREVIEW_TEXTDET_MODEL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/bench/textdet-model/ch_PP-OCRv4_det_infer.onnx
mkdir -p "$LOCAL"
cd "$LOCAL"
nice -n 19 /home/data/.venv/bin/python prod_text.py "%Somebody Somewhere%Season 03%" "$@"
