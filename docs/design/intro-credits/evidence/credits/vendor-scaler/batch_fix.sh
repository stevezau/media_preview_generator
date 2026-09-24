#!/bin/bash
# Usage: batch_fix.sh LIST FLAGS -- the proposed chain on Intel (plex) and NVIDIA (storage): surfaces stay on the GPU,
# fps=1 selects there, hwdownload at full size in the stream's own format, one swscale for every vendor.
set -uo pipefail
export MEDIA_PREVIEW_TEXTDET_MODEL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/credits/bench/textdet-model/ch_PP-OCRv4_det_infer.onnx
export OMP_NUM_THREADS=2
readonly PY=/home/data/.venv/bin/python
list="$1"; flags="$2"
while IFS=$'\t' read -r tag kind path; do
  [[ -z "$tag" ]] && continue
  ep=(); [[ "$kind" == "e" ]] && ep=(--episode)
  pixfmt=$(ffprobe -v error -select_streams v:0 -show_entries stream=pix_fmt -of csv=p=0 "$path" < /dev/null 2>/dev/null | head -1 | tr -d ',')
  fmt=nv12; [[ "$pixfmt" == *10le ]] && fmt=p010le
  for v in intel nvidia; do
    out="${tag}_${v}fix${flags}"
    [[ -f "${out}.json" ]] && continue
    timeout 1500 nice -n 19 "$PY" run_vendor.py "$v" "$path" "$out" --extra-hw-frames 8 \
      --scale-override "hwdownload,format=${fmt},scale=320:180:flags=${flags},format=nv12" "${ep[@]}" < /dev/null 2>&1 \
      | tail -1 | sed "s/^/$tag ${v}fix${flags}: /"
  done
done < "$list"
