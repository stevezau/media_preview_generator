#!/bin/bash
# Usage: bench_intel2.sh TAIL_START REFINE_START DLFMT PATH -- keep surfaces on the GPU, select (fps=1) there, then
# hwdownload at full size and swscale: the proposed chain.
set -euo pipefail
readonly IMAGE=425d989cc346
tail_start="$1"; refine_start="$2"; fmt="$3"; path="$4"
run() {
  local label="$1"; shift
  local name="mpg-inteltest-bench-$$-$RANDOM" cmd
  cmd=$(printf '%q ' docker run --rm --name "$name" --cpus 2 --device /dev/dri:/dev/dri \
    -v /data_16tb:/data_16tb:ro -v /data_16tb2:/data_16tb2:ro -v /data_16tb3:/data_16tb3:ro -v /data_28tb:/data_28tb:ro \
    --entrypoint nice "$IMAGE" -n 19 /usr/local/bin/ffmpeg -nostdin -hide_banner -benchmark -threads 2 "$@")
  ssh -n -o BatchMode=yes plex "$cmd" 2>&1 | tr '\r' '\n' | grep -E "^bench:|^frame=|rror" | tail -3 | tr '\n' ' ' | sed "s/^/$label: /"
  echo
}
hw=(-hwaccel vaapi -hwaccel_device /dev/dri/renderD128 -hwaccel_output_format vaapi)
key=(-skip_frame nokey -ss "$tail_start" -copyts -i "$path" -an -sn -dn -fps_mode passthrough)
fine=(-ss "$refine_start" -t 40 -copyts -i "$path" -an -sn -dn -fps_mode passthrough)
out=(-f rawvideo -y /dev/null)
for i in 1 2; do
  run "key  gpu-select+download" "${hw[@]}" "${key[@]}" -vf "hwdownload,format=$fmt,scale=320:180,format=nv12" "${out[@]}"
  run "fine gpu-select+download" "${hw[@]}" "${fine[@]}" -vf "fps=1,hwdownload,format=$fmt,scale=320:180,format=nv12" "${out[@]}"
done
