#!/bin/bash
# Rule (b) measurements: credits sets (chapters, with and without Plex's markers), the online set, and the prod replay
# as stored, with the next run's Plex read (--next), and with credit text read too (--next --text).
set -euo pipefail
readonly R="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCAL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/decide-rules/local
readonly PY=/home/data/.venv/bin/python
mkdir -p "$LOCAL"
cd "$LOCAL"
nice -n 19 "$PY" "$R/prod_replay.py" base prod_next_base.json --next
nice -n 19 "$PY" "$R/prod_replay.py" base prod_nexttext_base.json --next --text
for v in "$@"; do
  echo "=== $v"
  nice -n 19 "$PY" "$R/credits_sets.py" base --chapters --variant "$v" --json "cred_v_${v}.json"
  nice -n 19 "$PY" "$R/credits_sets.py" base --no-plex --chapters --variant "$v" --json "cred_npc_${v}.json"
  nice -n 19 "$PY" "$R/online_set.py" base --variant "$v" --json "online_v_${v}.json"
  nice -n 19 "$PY" "$R/prod_replay.py" base "prod_v_${v}.json" --variant "$v"
  nice -n 19 "$PY" "$R/prod_replay.py" base "prod_next_${v}.json" --next --variant "$v"
  nice -n 19 "$PY" "$R/prod_replay.py" base "prod_nexttext_${v}.json" --next --text --variant "$v"
done
