#!/bin/bash
# Synthetic movies for the credit text lab rows. Each: 540 s of text-free gradients, then 120 s of white names scrolling
# up over black (a line every 3 s). "Synth Credits" goes on with a 40 s scene after the roll (Q3: the skip ends at the
# last credit, ~660 s); "Synth Credits Open" ends with the roll (the skip runs to the end). Truth: start 540 s.
#
# Why 540 s of gradients and only a 40 s scene: the app's own sanity bound (decide.sanity_problem) throws out credits
# that start before the last 25 % of the file, and Q3 keeps an end only when more than 30 s follows the roll. The
# gradients therefore have to be at least three times the roll plus the scene, and the scene has to clear 30 s.
#
#   ./synth_credits.sh          encode missing files
#   ./synth_credits.sh force    re-encode everything
#
# MLAB_DIR sets the lab folder that holds synth/ (default: this script's folder).
#
# Output (git-ignored; up.sh and app.sh mount both movie folders read-only under /media/synth-credits):
#   synth/Synth Credits (2024)/Synth Credits (2024).mkv                 H.264, keyframe every 2 s, 700 s
#   synth/Synth Credits Open (2025)/Synth Credits Open (2025).mkv       H.264, 660 s
#   synth/_staging/Synth Credits (2024) - AV1.mkv                       AV1 (Pascal has no AV1 NVDEC: the GPU-decode-failure row)
set -euo pipefail

readonly HERE="${MLAB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
readonly SCENE_DIR="${HERE}/synth/Synth Credits (2024)"
readonly OPEN_DIR="${HERE}/synth/Synth Credits Open (2025)"
readonly STAGING_DIR="${HERE}/synth/_staging"
readonly FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
readonly FORCE="${1:-}"
readonly GRADIENT_S=540
readonly ROLL_S=120
readonly SCENE_S=40

command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "font not found: $FONT" >&2; exit 1; }
[[ -d "${HERE}/synth" ]] || { echo "no synth folder in ${HERE} (set MLAB_DIR to the lab folder)" >&2; exit 1; }
mkdir -p "$SCENE_DIR" "$OPEN_DIR" "$STAGING_DIR"

roll=""
for i in $(seq 0 39); do
    roll+="${roll:+,}drawtext=fontfile=${FONT}:text='CREDIT NAME ${i}':fontcolor=white:fontsize=24:x=(w-tw)/2:y=h-20*t+$((i * 60))"
done

# encode <out> <scene seconds, 0 for none> <codec args…>
encode() {
    local out="$1" scene_s="$2"; shift 2
    [[ -f "$out" && "$FORCE" != "force" ]] && return 0
    local inputs=(-f lavfi -i "gradients=size=640x360:rate=24:speed=0.02,trim=duration=${GRADIENT_S},setpts=PTS-STARTPTS"
                  -f lavfi -i "color=c=black:size=640x360:rate=24:duration=${ROLL_S},${roll}")
    local graph="[0:v][1:v]concat=n=2:v=1:a=0[v]"
    if (( scene_s > 0 )); then
        inputs+=(-f lavfi -i "gradients=size=640x360:rate=24:speed=0.05:seed=9,trim=duration=${scene_s},setpts=PTS-STARTPTS")
        graph="[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
    fi
    nice -n 19 ffmpeg -v error -y "${inputs[@]}" -filter_complex "$graph" -map "[v]" -g 48 "$@" "$out"
}

encode "${SCENE_DIR}/Synth Credits (2024).mkv" "$SCENE_S" -c:v libx264 -preset veryfast -pix_fmt yuv420p
encode "${OPEN_DIR}/Synth Credits Open (2025).mkv" 0 -c:v libx264 -preset veryfast -pix_fmt yuv420p
encode "${STAGING_DIR}/Synth Credits (2024) - AV1.mkv" "$SCENE_S" -c:v libsvtav1 -preset 10 -pix_fmt yuv420p
ls -la "$SCENE_DIR" "$OPEN_DIR" "$STAGING_DIR" | grep -i 'synth credits'
