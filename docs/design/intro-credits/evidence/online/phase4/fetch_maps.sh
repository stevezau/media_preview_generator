#!/bin/bash
# Caches the two anime id maps beside this script. Both are git-ignored.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly UA="MediaPreviewGenerator-research/0.1 (intro-credits source evaluation)"

get() {
  local name="$1" url="$2"
  if [[ -s "$HERE/$name" ]]; then
    echo "cached $name ($(stat -c%s "$HERE/$name") bytes)"
    return
  fi
  # -f so an error page is never cached as if it were the map; the next run would call it "cached".
  if ! nice -n 19 curl -fsSL -A "$UA" --max-time 180 -o "$HERE/$name" "$url"; then
    rm -f "$HERE/$name"
    echo "FAILED $name" >&2
    exit 1
  fi
  echo "$name ($(stat -c%s "$HERE/$name") bytes)"
}

# anidb <-> mal <-> tvdb <-> tmdb <-> imdb; weekly "automated list update"; no LICENSE in the repo.
get fribb-full.json "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
# anidb <-> tvdb with defaulttvdbseason + episodeoffset -- the season/offset table; no LICENSE in the repo.
get animelists.xml "https://raw.githubusercontent.com/Anime-Lists/anime-lists/master/anime-list-master.xml"
