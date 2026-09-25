#!/bin/bash
# Credits sets without Plex's markers (a Jellyfin/Emby-only library), with and without chapters, base vs variants.
set -euo pipefail
readonly R="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCAL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/decide-rules/local
readonly PY=/home/data/.venv/bin/python
mkdir -p "$LOCAL"
cd "$LOCAL"
echo "=== base, no plex, no chapters"
nice -n 19 "$PY" "$R/credits_sets.py" base --no-plex --json cred_np_base.json
echo "=== base, no plex, chapters"
nice -n 19 "$PY" "$R/credits_sets.py" base --no-plex --chapters --json cred_npc_base.json
for v in "$@"; do
  echo "=== $v, no plex, no chapters"
  nice -n 19 "$PY" "$R/credits_sets.py" base --no-plex --variant "$v" --json "cred_np_${v}.json"
  echo "=== $v, no plex, chapters"
  nice -n 19 "$PY" "$R/credits_sets.py" base --no-plex --chapters --variant "$v" --json "cred_npc_${v}.json"
done
