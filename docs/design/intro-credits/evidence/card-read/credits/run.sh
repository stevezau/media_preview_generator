#!/bin/bash
# Usage: run.sh base|work TAG SETS [ctrun args...]  — one tree's credit text on the sets, niced, log in logs/.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly BASE_TREE="$HERE/../base"
readonly WORK_TREE="/home/data/workspace/plex_generate_vid_previews"
readonly BASE_DIGEST_FILE="$HERE/base_decode_digest.txt"
which="$1"; tag="$2"; sets="$3"; shift 3
mkdir -p "$HERE/logs"
if [[ "$which" == "base" ]]; then
    tree="$BASE_TREE"
    extra=()
else
    tree="$WORK_TREE"
    extra=(--decode-digest "$(cat "$BASE_DIGEST_FILE")")
fi
nice -n 19 /home/data/.venv/bin/python "$HERE/ctrun.py" "$tree" "$HERE/ct_${tag}.json" "$sets" "${extra[@]}" "$@" \
    >> "$HERE/logs/ct_${tag}.log" 2>&1
echo "DONE $sets" >> "$HERE/logs/ct_${tag}.log"
