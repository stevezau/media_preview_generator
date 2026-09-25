#!/bin/bash
# The online set for the base tree and each variant named on the command line.
set -euo pipefail
readonly R="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCAL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/decide-rules/local
readonly PY=/home/data/.venv/bin/python
mkdir -p "$LOCAL"
cd "$LOCAL"
echo "=== base"
nice -n 19 "$PY" "$R/online_set.py" base --json online_v_base.json
for v in "$@"; do
  echo "=== variant $v"
  nice -n 19 "$PY" "$R/online_set.py" base --variant "$v" --json "online_v_${v}.json"
done
