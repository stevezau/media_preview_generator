#!/bin/bash
# Before (base = dev 40311c3) and after (the worktree) on every decide-level set and the prod replay. nice 19, no media
# reads (every answer comes from stored dumps and caches).
set -euo pipefail
readonly R="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOCAL=/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/decide-rules/local
readonly PY=/home/data/.venv/bin/python
mkdir -p "$LOCAL"
cd "$LOCAL"
mkdir -p final
for t in base work; do
  echo "=================== $t"
  echo "--- intro sets as #312 measured them (season audio, IntroDB, Plex)"
  nice -n 19 "$PY" "$R/intro_sets.py" "$t" --json "final/intro_${t}.json"
  echo "--- intro sets + SkipDB (the app's default sources)"
  nice -n 19 "$PY" "$R/intro_sets.py" "$t" --skipdb --json "final/intro_skipdb_${t}.json"
  echo "--- credits sets: credit text, Plex, SkipDB"
  nice -n 19 "$PY" "$R/credits_sets.py" "$t" --json "final/cred_${t}.json"
  echo "--- credits sets: + chapters"
  nice -n 19 "$PY" "$R/credits_sets.py" "$t" --chapters --json "final/cred_chap_${t}.json"
  echo "--- credits sets: no Plex markers"
  nice -n 19 "$PY" "$R/credits_sets.py" "$t" --no-plex --json "final/cred_np_${t}.json"
  echo "--- credits sets: chapters, no Plex markers"
  nice -n 19 "$PY" "$R/credits_sets.py" "$t" --chapters --no-plex --json "final/cred_npc_${t}.json"
  echo "--- online 43"
  nice -n 19 "$PY" "$R/online_set.py" "$t" --json "final/online_${t}.json"
  echo "--- prod replay"
  nice -n 19 "$PY" "$R/prod_replay.py" "$t" "final/prod_${t}.json"
  nice -n 19 "$PY" "$R/prod_replay.py" "$t" "final/prod_text_${t}.json" --text
  nice -n 19 "$PY" "$R/prod_replay.py" "$t" "final/prod_nexttext_${t}.json" --next --text
done
