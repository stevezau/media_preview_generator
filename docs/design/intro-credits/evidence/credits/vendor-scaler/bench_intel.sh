#!/bin/bash
# Usage: bench_intel.sh TAIL_START REFINE_START PATH -- one app-shaped keyframe pass and one 40 s 1 fps refine pass on the
# plex Intel iGPU (throwaway container, same image as prod), today's scale_vaapi chain vs decode+download+swscale.
set -euo pipefail
readonly IMAGE=425d989cc346
tail_start="$1"; refine_start="$2"; path="$3"
run() {
  local label="$1"; shift
  local name="mpg-inteltest-bench-$$-$RANDOM" cmd
  cmd=$(printf '%q ' docker run --rm --name "$name" --cpus 2 --device /dev/dri:/dev/dri \
    -v /data_16tb:/data_16tb:ro -v /data_16tb2:/data_16tb2:ro -v /data_16tb3:/data_16tb3:ro -v /data_28tb:/data_28tb:ro \
    --entrypoint nice "$IMAGE" -n 19 /usr/local/bin/ffmpeg -nostdin -hide_banner -benchmark -threads 2 "$@")
  ssh -n -o BatchMode=yes plex "$cmd" 2>&1 | tr '\r' '\n' | grep -E "^bench:|^frame=" | tail -3 | tr '\n' ' ' | sed "s/^/$label: /"
  echo
}
hw=(-hwaccel vaapi -hwaccel_device /dev/dri/renderD128)
key=(-skip_frame nokey -ss "$tail_start" -copyts -i "$path" -an -sn -dn -fps_mode passthrough)
fine=(-ss "$refine_start" -t 40 -copyts -i "$path" -an -sn -dn -fps_mode passthrough)
out=(-f rawvideo -y /dev/null)
for i in 1 2; do
  run "key  today   " "${hw[@]}" -hwaccel_output_format vaapi "${key[@]}" -vf "scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12" "${out[@]}"
  run "key  download" "${hw[@]}" "${key[@]}" -vf "scale=320:180,format=nv12" "${out[@]}"
  run "fine today   " "${hw[@]}" -hwaccel_output_format vaapi "${fine[@]}" -vf "fps=1,scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12" "${out[@]}"
  run "fine download" "${hw[@]}" "${fine[@]}" -vf "fps=1,scale=320:180,format=nv12" "${out[@]}"
done
