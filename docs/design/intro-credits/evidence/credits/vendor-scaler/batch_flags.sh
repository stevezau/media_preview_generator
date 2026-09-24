#!/bin/bash
# Usage: batch_flags.sh LIST FLAGS...  -- CPU decode with scale=320:180:flags=F for each F (keyframe + refine passes).
set -uo pipefail
export MEDIA_PREVIEW_TEXTDET_MODEL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/bench/textdet-model/ch_PP-OCRv4_det_infer.onnx
export OMP_NUM_THREADS=2
readonly PY=/home/data/.venv/bin/python
list="$1"; shift
while IFS=$'\t' read -r tag kind path; do
  [[ -z "$tag" ]] && continue
  ep=(); [[ "$kind" == "e" ]] && ep=(--episode)
  for f in "$@"; do
    out="${tag}_cpu${f}"
    [[ -f "${out}.json" ]] && continue
    timeout 1500 nice -n 19 "$PY" run_vendor.py cpu "$path" "$out" --scale-override "scale=320:180:flags=${f},format=nv12" "${ep[@]}" < /dev/null 2>&1 | tail -1 | sed "s/^/$tag cpu$f: /"
  done
done < "$list"
