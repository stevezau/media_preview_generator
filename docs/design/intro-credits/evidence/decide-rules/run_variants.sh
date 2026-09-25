#!/bin/bash
# Every decide-level set (intro, credits, prod replay) for each variant named on the command line, on the base tree.
set -euo pipefail
readonly R="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCAL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/decide-rules/local
readonly PY=/home/data/.venv/bin/python
mkdir -p "$LOCAL"
cd "$LOCAL"
for v in "$@"; do
  echo "=== variant $v"
  nice -n 19 "$PY" "$R/intro_sets.py" base --skipdb --variant "$v" --json "intro_v_${v}.json"
  nice -n 19 "$PY" "$R/credits_sets.py" base --chapters --variant "$v" --json "cred_v_${v}.json"
  nice -n 19 "$PY" "$R/prod_replay.py" base "prod_v_${v}.json" --variant "$v"
done
