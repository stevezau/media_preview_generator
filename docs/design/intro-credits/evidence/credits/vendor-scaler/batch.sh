#!/bin/bash
# Usage: batch.sh LIST (lines: tag<TAB>kind(m|e)<TAB>path); runs each file through every variant, one at a time.
set -uo pipefail
export MEDIA_PREVIEW_TEXTDET_MODEL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/bench/textdet-model/ch_PP-OCRv4_det_infer.onnx
export OMP_NUM_THREADS=2
readonly PY=/home/data/.venv/bin/python
readonly VARIANTS="${VARIANTS:-intel nvidia cpu inteldl nvidiadl}"
while IFS=$'\t' read -r tag kind path; do
  [[ -z "$tag" ]] && continue
  ep=(); [[ "$kind" == "e" ]] && ep=(--episode)
  for v in $VARIANTS; do
    [[ -f "${tag}_${v}.json" ]] && continue
    case "$v" in
      inteldl) args=(intel --download) ;;
      nvidiadl) args=(nvidia --download) ;;
      *) args=("$v") ;;
    esac
    timeout 1500 nice -n 19 "$PY" run_vendor.py "${args[0]}" "$path" "${tag}_${v}" "${args[@]:1}" "${ep[@]}" < /dev/null 2>&1 | tail -1 | sed "s/^/$tag $v: /"
  done
done < "$1"
