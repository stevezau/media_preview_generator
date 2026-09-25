#!/bin/bash
# Makes the two reference clips the GPU decode check (markers/credits/decode_check.py) decodes on every GPU and on the
# CPU. They are checked in, so the app never needs an encoder; run this only to change them, then commit both clips.
#
# 1280x720 (a size every hardware decoder takes), 9 frames at 1 fps, a keyframe every 3 frames with a B and a P frame
# between: 8-bit H.264 (High) and 10-bit HEVC (Main 10). Credit text at 9-14 px scrolls over black, a dark caption
# sits on a light band and a fractal zooms in one corner, so the downscaled frames hang on small, sharp edges -- where
# decoders have disagreed before -- and on motion between frames. Single-threaded encoders and bitexact muxing make a
# rerun on the same host byte-identical; another host's fonts or encoder builds may make other, equally valid clips.
#
# Usage: make_reference_clips.sh [output folder, default: this script's folder]
set -euo pipefail

readonly FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
readonly OUT="${1:-$(dirname "$0")}"
readonly FRAMES=9
readonly GOP=3

text="drawbox=x=560:y=0:w=720:h=720:color=black:t=fill"
y=40
for size in 9 10 11 12 14; do
    text+=",drawtext=fontfile=${FONT}:fontsize=${size}:fontcolor=white:x=600:y=${y}-3*t"
    text+=":text='EXECUTIVE PRODUCER  A. N. OTHER  ${size}  %{n}'"
    y=$((y + 80))
done
text+=",drawbox=x=0:y=640:w=560:h=80:color=0xE0E0E0:t=fill"
text+=",drawtext=fontfile=${FONT}:fontsize=10:fontcolor=black:x=10+8*t:y=660"
text+=":text='small dark caption text on a light band 0123456789'"

readonly GRAPH="[0:v][1:v]overlay=x=40:y=40[base];[base]${text}[v]"
readonly INPUTS=(
    -f lavfi -i "color=c=black:size=1280x720:rate=1,format=yuv420p,geq=lum='48+X/24+12*sin(Y/29+T)':cb='128+Y/12':cr=150,trim=end_frame=${FRAMES}"
    -f lavfi -i "mandelbrot=size=240x136:rate=1:start_scale=0.5,trim=end_frame=${FRAMES}"
)

ffmpeg -v error -y "${INPUTS[@]}" -filter_complex "${GRAPH}" -map "[v]" \
    -c:v libx264 -preset slow -crf 30 -g "${GOP}" -keyint_min "${GOP}" -sc_threshold 0 -bf 1 \
    -x264-params b-adapt=0 -threads 1 -pix_fmt yuv420p -fflags +bitexact -flags:v +bitexact \
    "${OUT}/h264-8bit.mkv"
ffmpeg -v error -y "${INPUTS[@]}" -filter_complex "${GRAPH}" -map "[v]" \
    -c:v libx265 -preset slow -crf 32 -profile:v main10 -pix_fmt yuv420p10le \
    -x265-params "keyint=${GOP}:min-keyint=${GOP}:scenecut=0:bframes=1:b-adapt=0:open-gop=0:pools=1:frame-threads=1:log-level=error" \
    -fflags +bitexact -flags:v +bitexact \
    "${OUT}/hevc-10bit.mkv"
