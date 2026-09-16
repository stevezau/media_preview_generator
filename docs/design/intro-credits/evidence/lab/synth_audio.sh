#!/bin/bash
# Synthetic VP9/Opus episodes for the season audio lab: no chapters, a shared 30 s theme at a different offset in
# every episode, everything else unique per episode. Season audio (not chapters, not online sources) must find the
# theme. VP9/Opus because Playwright's Chromium has no H.264.
#
#   ./synth_audio.sh          encode missing files
#   ./synth_audio.sh force    re-encode everything
#
# MLAB_DIR sets the lab folder that holds synth/ (default: this script's folder), so a checkout elsewhere writes into
# the folder the running lab containers mount.
#
# Output (git-ignored; up.sh and app.sh mount the show folder read-only at /media/synth-audio):
#   synth/Synth Audio (2022)/Season 01/Synth Audio (2022) - S01E0N.webm   N = 1..4, 300 s
#   synth/Synth Audio (2022)/Season 02/Synth Audio (2022) - S02E01.webm   300 s
#   synth/_staging/Synth Audio (2022) - S02E02.webm                        300 s (the weekly-release row copies it in)
# Checked with the matcher (season_intros) on the encoded files: every Season 01 theme found within 1 s of its start
# and 2 s of its end, and S02E01 + S02E02 alone too. Theme 30 s (seed 7, 4 notes/s from 220 Hz); unique parts at 3
# notes/s from 262 Hz, seeds 100*season+episode before the theme and +50 after it.
set -euo pipefail

readonly HERE="${MLAB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
readonly SHOW="Synth Audio (2022)"
readonly ROOT="${HERE}/synth/${SHOW}"
readonly STAGING_DIR="${HERE}/synth/_staging"
readonly WORK_DIR="${HERE}/synth/.work-audio"
readonly FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
readonly LENGTH=300
readonly THEME=30
readonly FORCE="${1:-}"

# "season episode" -> theme start (s); every start is inside the first 35% of 300 s and the theme never reaches the end.
declare -A THEME_START=(["1 1"]=20 ["1 2"]=45 ["1 3"]=5 ["1 4"]=70 ["2 1"]=15 ["2 2"]=60)

command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "font not found: $FONT" >&2; exit 1; }
[[ -d "${HERE}/synth" ]] || { echo "no synth folder in ${HERE} (set MLAB_DIR to the lab folder)" >&2; exit 1; }
mkdir -p "${ROOT}/Season 01" "${ROOT}/Season 02" "$STAGING_DIR" "$WORK_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

# melody SEED NOTES_PER_S BASE_HZ -> an aevalsrc expression: two voices of pseudo-random notes (a sine hash of the note
# number and SEED) over two octaves. The same SEED gives the same audio, so the theme (seed 7) is identical in every
# episode, and the unique parts (per-episode seeds, another rhythm and register) share nothing with it or each other.
# Commas are escaped for the filtergraph parser.
melody() {
    local seed="$1" rate="$2" base="$3"
    local k1="floor(24*mod(sin(floor(t*${rate})*12.9898+${seed}*78.233)*43758.5453\\,1))"
    local k2="floor(12*mod(sin(floor(t*${rate}/2)*4.1414+${seed}*19.19)*23421.631\\,1))"
    echo "0.3*sin(2*PI*${base}*pow(2\\,${k1}/12)*t)+0.2*sin(2*PI*${base}/2*pow(2\\,${k2}/12)*t)"
}

encode() {
    local season="$1" episode="$2" out="$3"
    if [[ -f "$out" && "$FORCE" != "force" ]]; then
        echo "exists: ${out#"$HERE"/}"
        return
    fi
    local start="${THEME_START["$season $episode"]}"
    local after=$((LENGTH - start - THEME))
    local seed=$((season * 100 + episode))
    local tmp
    tmp="${WORK_DIR}/$(basename "$out")"
    local label
    label="S$(printf %02d "$season")E$(printf %02d "$episode")"
    # Unique parts before and after the theme: melodies with per-episode seeds. (Noise doesn't work: chromaprint
    # hashes any stationary noise alike, so noise with different seeds matched across episodes at every shift.)
    nice -n 19 ffmpeg -hide_banner -loglevel error -y \
        -f lavfi -i "testsrc2=s=640x360:r=24:d=${start}" \
        -f lavfi -i "smptebars=s=640x360:r=24:d=${THEME}" \
        -f lavfi -i "testsrc2=s=640x360:r=24:d=${after}" \
        -f lavfi -i "aevalsrc=$(melody "$seed" 3 262):s=48000:d=${start}" \
        -f lavfi -i "aevalsrc=$(melody 7 4 220):s=48000:d=${THEME}" \
        -f lavfi -i "aevalsrc=$(melody $((seed + 50)) 3 262):s=48000:d=${after}" \
        -filter_complex "[3:a]aformat=channel_layouts=stereo[a0];[4:a]aformat=channel_layouts=stereo[a1];[5:a]aformat=channel_layouts=stereo[a2];[0:v][a0][1:v][a1][2:v][a2]concat=n=3:v=1:a=1[cv][a];[cv]drawtext=fontfile=${FONT}:fontsize=24:fontcolor=yellow:box=1:boxcolor=black@0.6:x=20:y=h-50:text='${label} theme ${start}s-$((start + THEME))s %{pts\\:hms}'[v]" \
        -map "[v]" -map "[a]" -metadata title="${SHOW} ${label}" \
        -c:v libvpx-vp9 -b:v 200k -deadline realtime -cpu-used 8 -row-mt 1 -threads 4 -g 48 \
        -c:a libopus -b:a 64k "$tmp"
    # Move into place only when complete, so a server scan never sees a half-written file.
    mv -f "$tmp" "$out"
    echo "wrote:  ${out#"$HERE"/}"
}

for e in 1 2 3 4; do
    encode 1 "$e" "${ROOT}/Season 01/${SHOW} - S01E0${e}.webm"
done
encode 2 1 "${ROOT}/Season 02/${SHOW} - S02E01.webm"
encode 2 2 "${STAGING_DIR}/${SHOW} - S02E02.webm"
