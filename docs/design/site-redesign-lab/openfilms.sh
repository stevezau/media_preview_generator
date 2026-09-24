#!/bin/bash
# Download the films in films.json into the lab media folder, laid out the way Plex, Jellyfin and
# Emby expect movies: Movies/<Title> (<Year>)/<Title> (<Year>).<ext> plus poster.jpg. Idempotent: a
# file that is already there is skipped. Never writes under /data*.
#
#   ./openfilms.sh      download what's missing, then print each film's ffprobe facts
#
# On storage, Movies is a symlink to /home/data/lab-media/open-films (where the films were first
# downloaded); every script and container uses the Movies path.
set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly MEDIA_ROOT="${OPENFILMS_DIR:-/home/data/mlab-openfilms}"
readonly MOVIES="${MEDIA_ROOT}/Movies"

for path in "$MEDIA_ROOT" "$(realpath -m "$MOVIES")"; do
    if [[ "$path" == /data* ]]; then
        echo "refusing to write under /data*: ${path}" >&2
        exit 1
    fi
done
mkdir -p "$MOVIES" "${MEDIA_ROOT}/captures"

fetch() {
    local url="$1" out="$2"
    if [[ -s "$out" ]]; then
        echo "have   ${out#"$MEDIA_ROOT"/}"
        return
    fi
    echo "fetch  ${out#"$MEDIA_ROOT"/}"
    curl -fL --retry 3 --retry-delay 5 -o "${out}.part" "$url"
    if [[ "$url" == *.zip ]]; then
        # Blender now serves its 1080p masters zipped, one file per archive.
        unzip -p "${out}.part" >"${out}.unzipped"
        rm "${out}.part"
        mv "${out}.unzipped" "$out"
    else
        mv "${out}.part" "$out"
    fi
}

# The video's extension, from its URL without any query string or .zip wrapper.
video_ext() {
    local path="${1%%\?*}"
    path="${path%.zip}"
    local ext="${path##*.}"
    echo "${ext,,}"
}

# Some posters exist only as PNG (Tears of Steel's on Commons); the servers' local artwork here is poster.jpg.
to_jpeg() {
    local file="$1"
    if [[ "$(ffprobe -v error -show_entries stream=codec_name -of csv=p=0 "$file")" != mjpeg ]]; then
        ffmpeg -v error -y -i "$file" -frames:v 1 -q:v 2 "${file}.tmp.jpg"
        mv "${file}.tmp.jpg" "$file"
        echo "jpeg   ${file#"$MEDIA_ROOT"/}"
    fi
}

# Netflix's Cosmos Laundromat HDR file is PQ-encoded (P3PQ in its name and on its page) but carries no
# colour tags, so players and this app would treat it as SDR. Tag it by stream copy (no re-encode):
# transfer 16 = PQ, primaries 12 = P3-D65, matrix 9 = BT.2020 non-constant.
tag_pq() {
    local file="$1" codec transfer tagged
    IFS='|' read -r codec transfer < <(ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_name,color_transfer -of compact=p=0:nk=1 "$file")
    if [[ "$transfer" == smpte2084 ]]; then
        return
    fi
    tagged="${file%.*}.tagged.${file##*.}"
    ffmpeg -v error -y -i "$file" -map 0 -c copy \
        -bsf:v "${codec}_metadata=transfer_characteristics=16:colour_primaries=12:matrix_coefficients=9" "$tagged"
    mv "$tagged" "$file"
    echo "tagged ${file#"$MEDIA_ROOT"/} as PQ"
}

# The poster goes last: `read` merges empty tab-separated fields, and poster is the only one that can be empty.
jq -r '.[] | select(.video != null) | [.title, (.year | tostring), .video, (.tags_added // false | tostring), (.poster // "")] | @tsv' \
    "${HERE}/films.json" |
    while IFS=$'\t' read -r title year video tags_added poster; do
        dir="${MOVIES}/${title} (${year})"
        file="${dir}/${title} (${year}).$(video_ext "$video")"
        mkdir -p "$dir"
        fetch "$video" "$file"
        if [[ "$tags_added" == true ]]; then
            tag_pq "$file"
        fi
        if [[ -n "$poster" ]]; then
            fetch "$poster" "${dir}/poster.jpg"
            to_jpeg "${dir}/poster.jpg"
        fi
    done

# The trailing slash makes find descend into Movies when it is a symlink.
find "${MOVIES}/" -type f \( -name '*.mp4' -o -name '*.mkv' -o -name '*.mov' -o -name '*.webm' \) -print0 | sort -z |
    while IFS= read -r -d '' file; do
        printf '%s\t' "$(basename "$file")"
        ffprobe -v error -select_streams v:0 \
            -show_entries stream=codec_name,profile,width,height,r_frame_rate,pix_fmt,color_transfer,color_primaries,color_space:format=duration \
            -of compact=p=0:nk=1 "$file"
    done
