#!/bin/bash
# Every phase-4 row, then the five rows added to the earlier matrices, in the order phase4-results.md lists them.
# A failing row doesn't stop the run; the log says which one failed.
#
#   MLAB_DIR=/path/to/lab-folder ./phase4_run_all.sh
#
# Needs the lab up on the phase-4 images (phase4-resume.md), MLAB_APP_IMAGE / MLAB_AGENT_IMAGE if they aren't the
# default tags, and the throwaway containers of rows 10 and 12 (phase4_health_up.sh, phase4_row12_up.sh).
set -uo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${MLAB_DIR:?set MLAB_DIR to the lab folder that holds env, synth/ and results/}"
export MLAB_DIR
export MLAB_APP_IMAGE="${MLAB_APP_IMAGE:-plex-previews:phase4-lab}"
export MLAB_AGENT_IMAGE="${MLAB_AGENT_IMAGE:-plex-marker-agent:phase4-lab}"
readonly PYTHON="${MLAB_PYTHON:-python3}"
readonly LOG="${MLAB_DIR}/results/phase4-run.log"

mkdir -p "${MLAB_DIR}/results"
: >"$LOG"
cd "$HERE"

run() {
    echo "=== $* ($(date -u +%H:%M:%S))" >>"$LOG"
    nice -n 19 "$PYTHON" "$@" >>"$LOG" 2>&1
    echo "=== exit $? ($(date -u +%H:%M:%S))" >>"$LOG"
}

run phase4_matrix.py run 1 2 3 4 5 6 7 8 9 10 11
run phase1_matrix.py run 20 21
run phase2_matrix.py run 25 26
run phase3_matrix.py run 17
run phase4_matrix.py run 12
echo "=== ALL DONE" >>"$LOG"
