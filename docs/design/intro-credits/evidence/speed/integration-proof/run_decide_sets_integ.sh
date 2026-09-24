#!/bin/bash
# Lane A's decide-level regression sets on the integration tree, one after another (nice 19).
set -uo pipefail
readonly D=/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/integ_proof
cd /home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3
: > "$D/decide_sets_integ.log"
for name in accused heldout175 lab118; do
  nice -n 19 /home/data/.venv/bin/python "$D/decide_sets_integ.py" "$name" >> "$D/decide_sets_integ.log" 2>&1
done
echo "=== done" >> "$D/decide_sets_integ.log"
cat "$D/decide_sets_integ.log"
