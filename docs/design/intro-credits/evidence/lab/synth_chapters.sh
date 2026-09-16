#!/bin/bash
# Synthetic VP9/Opus episodes with named chapters for the Intro & Credits lab. The chapters are the truth: the app's
# chapter source must publish exactly these intro/credits times. VP9/Opus because Playwright's Chromium has no H.264.
#
#   ./synth_chapters.sh          encode missing files
#   ./synth_chapters.sh force    re-encode everything
#
# Output (git-ignored; up.sh and app.sh mount the show folder read-only at /media/synth-chapters):
#   synth/Synth Chapters (2021)/Season 01/Synth Chapters (2021) - S01E0N.webm   120 s, chapters:
#       Chapter 1 | Intro (30 s, start differs per episode) | Chapter 2 | Credits 1:40-2:00
#   synth/_staging/Synth Chapters (2021) - S01E01 - Extended.webm               130 s: E01 plus a 10 s tail chapter
#   synth/_staging/Synth Chapters (2021) - S01E03 - Plexonly.webm               116 s: another cut of E03 (credits to
#                                                                                the end)
#   synth/Synth Movie (2023)/Synth Movie (2023) - 1080p.webm and - 720p.webm    120 s each, two versions of one movie:
#       Opening | Story | End Credits 1:40-2:00
#   synth/_plexonly/Synth Chapters (2021)/Season 01/                             empty; up.sh mounts it into mlab-plex only
# Staged files are mounted nowhere. The multi-version row copies Extended next to S01E01 so the Plex item gains a
# second version whose duration differs by more than 2 s; the Plex version-drift row copies Plexonly into
# synth/_plexonly, a folder only Plex sees (the version the app can't read).
#
# MLAB_DIR sets the lab folder that holds synth/ (default: this script's folder), so a checkout elsewhere writes into
# the folder the running lab containers mount.
set -euo pipefail

readonly HERE="${MLAB_DIR:-$(cd "$(dirname "$0")" && pwd)}"
readonly SHOW="Synth Chapters (2021)"
readonly SEASON_DIR="${HERE}/synth/${SHOW}/Season 01"
readonly STAGING_DIR="${HERE}/synth/_staging"
readonly WORK_DIR="${HERE}/synth/.work"
readonly MOVIE_DIR="${HERE}/synth/Synth Movie (2023)"
readonly PLEXONLY_DIR="${HERE}/synth/_plexonly/${SHOW}/Season 01"
readonly FONT="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
readonly INTRO_LENGTH=30
readonly CREDITS_START=100
readonly CREDITS_END=120
# At most 13 s: credits must still start in the last 25% of the longer file (100 s of 130 s), so both versions
# agree on credits while their durations differ by more than 2 s.
readonly TAIL=10
readonly FORCE="${1:-}"

# Intro start (seconds) per episode, all inside the first 35% of 120 s.
declare -A INTRO_START=([1]=10 [2]=17 [3]=25)

command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }
[[ -f "$FONT" ]] || { echo "font not found: $FONT" >&2; exit 1; }
[[ -d "${HERE}/synth" ]] || { echo "no synth folder in ${HERE} (set MLAB_DIR to the lab folder)" >&2; exit 1; }
# The plex-only folder must exist before up.sh mounts it: Docker would create a missing bind source owned by root.
mkdir -p "$SEASON_DIR" "$STAGING_DIR" "$WORK_DIR" "$MOVIE_DIR" "$PLEXONLY_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

# encode OUT EPISODE SEGMENT...   (SEGMENT = "title|start|end|kind", kind = chapter/intro/credits/tail)
encode() {
    local out="$1" episode="$2"
    shift 2
    if [[ -f "$out" && "$FORCE" != "force" ]]; then
        echo "exists: ${out#"$HERE"/}"
        return
    fi
    local meta="${WORK_DIR}/chapters.txt"
    local tmp
    tmp="${WORK_DIR}/$(basename "$out")"
    local -a inputs=()
    local graph="" pads=""
    local n=0 seg title start end kind length vsrc freq
    printf ';FFMETADATA1\ntitle=%s\n' "$(basename "$out" .webm)" >"$meta"
    for seg in "$@"; do
        IFS='|' read -r title start end kind <<<"$seg"
        length=$((end - start))
        printf '[CHAPTER]\nTIMEBASE=1/1000\nSTART=%d\nEND=%d\ntitle=%s\n' $((start * 1000)) $((end * 1000)) "$title" >>"$meta"
        case "$kind" in
            intro) vsrc="smptebars=s=640x360:r=24"; freq=880 ;;
            credits) vsrc="color=c=0x202040:s=640x360:r=24"; freq=220 ;;
            tail) vsrc="color=c=gray:s=640x360:r=24"; freq=330 ;;
            *) vsrc="testsrc2=s=640x360:r=24"; freq=440 ;;
        esac
        inputs+=(-f lavfi -i "${vsrc}:d=${length}" -f lavfi -i "sine=f=${freq}:sample_rate=48000:d=${length}")
        graph+="[$((n * 2 + 1)):v]drawtext=fontfile=${FONT}:fontsize=28:fontcolor=white:box=1:boxcolor=black@0.6"
        graph+=":x=20:y=20:text='S01E$(printf %02d "$episode") ${title} ${start}s-${end}s'[v${n}];"
        pads+="[v${n}][$((n * 2 + 2)):a]"
        n=$((n + 1))
    done
    graph+="${pads}concat=n=${n}:v=1:a=1[cv][a];"
    graph+="[cv]drawtext=fontfile=${FONT}:fontsize=24:fontcolor=yellow:box=1:boxcolor=black@0.6:x=20:y=h-50"
    graph+=":text='%{pts\\:hms}'[v]"
    nice -n 19 ffmpeg -hide_banner -loglevel error -y -f ffmetadata -i "$meta" "${inputs[@]}" \
        -filter_complex "$graph" -map "[v]" -map "[a]" -map_metadata 0 -map_chapters 0 \
        -c:v libvpx-vp9 -b:v 300k -deadline realtime -cpu-used 8 -row-mt 1 -g 48 \
        -c:a libopus -b:a 64k "$tmp"
    # Move into place only when complete, so a server scan never sees a half-written file.
    mv -f "$tmp" "$out"
    echo "wrote:  ${out#"$HERE"/}"
}

# episode_segments EPISODE -> the 120 s layout, one segment per line
episode_segments() {
    local intro_start="${INTRO_START[$1]}"
    local intro_end=$((intro_start + INTRO_LENGTH))
    echo "Chapter 1|0|${intro_start}|chapter"
    echo "Intro|${intro_start}|${intro_end}|intro"
    echo "Chapter 2|${intro_end}|${CREDITS_START}|chapter"
    echo "Credits|${CREDITS_START}|${CREDITS_END}|credits"
}

for episode in 1 2 3; do
    mapfile -t segments < <(episode_segments "$episode")
    encode "${SEASON_DIR}/${SHOW} - S01E0${episode}.webm" "$episode" "${segments[@]}"
done

mapfile -t segments < <(episode_segments 1)
segments+=("Chapter 3|${CREDITS_END}|$((CREDITS_END + TAIL))|tail")
encode "${STAGING_DIR}/${SHOW} - S01E01 - Extended.webm" 1 "${segments[@]}"

# A 116 s cut of S01E03 that only Plex will see (the version-drift row copies it into synth/_plexonly and removes it).
encode "${STAGING_DIR}/${SHOW} - S01E03 - Plexonly.webm" 3 \
    "Chapter 1|0|25|chapter" "Intro|25|55|intro" "Chapter 2|55|100|chapter" "Credits|100|116|credits"

# Two versions of one movie (Jellyfin 12.0 alternate versions, Emby version items, one Plex item): same chapters. The
# title metadata names the version, so the files differ.
for version in 1080p 720p; do
    encode "${MOVIE_DIR}/Synth Movie (2023) - ${version}.webm" 1 \
        "Opening|0|30|chapter" "Story|30|100|chapter" "End Credits|100|120|credits"
done
