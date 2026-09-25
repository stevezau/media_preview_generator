#!/bin/bash
# Frame sheets of Tomb Raider King's OP end: E12 against E03 at the audio alignment (read-only on /data).
set -euo pipefail
readonly D="/data_16tb/TV Shows/Tomb Raider King (2026) {tvdb-452039}/Season 01/"
readonly OUT="$(dirname "$0")"
sheet() {
    local name="$1" file="$2" start="$3" label="$4"
    nice -n 19 ffmpeg -nostdin -loglevel error -threads 2 -ss "$start" -t 6 -i "${D}${file}" \
        -vf "fps=4,scale=240:-2,drawtext=text='${label} %{pts}':x=4:y=4:fontsize=14:fontcolor=yellow:box=1:boxcolor=black,tile=12x2" \
        -frames:v 1 -y "${OUT}/${name}"
}
sheet trk_e12_83_89.jpg "Tomb Raider King (2026) - S01E12 - TBA [WEBRip-1080p][AAC 2.0][x265].mkv" 83 "E12 +83"
sheet trk_e03_83_89.jpg "Tomb Raider King (2026) - S01E03 - One Suited for Domination [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv" 82.63 "E03 +82.63"
