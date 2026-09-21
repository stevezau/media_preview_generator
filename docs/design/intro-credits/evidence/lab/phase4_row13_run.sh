#!/bin/bash
# Row 13 step 2-3: the phase 2 and phase 3 matrices on the phase-4 images, in the order their results docs prescribe,
# one row per process (a row that fails does not stop the run; the log says which one did).
#
#   MLAB_DIR=/path/to/lab-folder ./phase4_row13_run.sh
#
# Start from the state phase4_row13_reset.py leaves (our markers off the lab servers, fresh mlab-app config, then
# ./phase2_matrix.py configure). Phase 2 row 19 IS the phase 1 regression and does not reset markers.db itself; it takes
# about 15 minutes for its 600 s wait. Phase 3 rows 2-9 build on row 2's stored answer, so its order matters, and its
# rows 12-15 belong to the plex host (phase3-results.md), so they are not run here.
set -euo pipefail

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${MLAB_DIR:?set MLAB_DIR to the lab folder that holds env, synth/ and results/}"
export MLAB_DIR
export MLAB_APP_IMAGE="${MLAB_APP_IMAGE:-plex-previews:phase4-lab}"
export MLAB_AGENT_IMAGE="${MLAB_AGENT_IMAGE:-plex-marker-agent:phase4-lab}"
readonly PYTHON="${MLAB_PYTHON:-python3}"
readonly LOG="${MLAB_DIR}/results/row13-run.log"
readonly PHASE2_ROWS=(23 1 21 17 19 22 2 3 4 18 5 6 8 7 9 10 11 12 13 14 16 15 20 24)
readonly PHASE3_ROWS=(1 2 3 4 5 6 7 8 9 10 11 16)

mkdir -p "${MLAB_DIR}/results"
# Earlier passes must not stand in for a row that crashes before it writes its result file.
readonly ARCHIVE="${MLAB_DIR}/results/before-row13-run-$(date -u +%Y%m%dT%H%M%S)"
mkdir -p "$ARCHIVE"
# Phase 1's rows 20 and 21 belong to the phase-4 list, so `row-[01]?` leaves them.
for pattern in 'p2-row-??' 'p3-row-??' 'row-[01]?'; do
    for old in "${MLAB_DIR}/results/"${pattern}.json; do
        [[ -e "$old" ]] && mv "$old" "$ARCHIVE/"
    done
done
: >"$LOG"
cd "$HERE"

run_row() {
    local phase=$1 row=$2
    echo "=== ${phase} row ${row} ($(date -u +%H:%M:%S))" >>"$LOG"
    local status=0
    nice -n 19 "$PYTHON" "${phase}_matrix.py" run "$row" >>"$LOG" 2>&1 || status=$?
    echo "=== ${phase} row ${row} exit ${status} ($(date -u +%H:%M:%S))" >>"$LOG"
}

for row in "${PHASE2_ROWS[@]}"; do
    run_row phase2 "$row"
done

echo "=== phase3 configure ($(date -u +%H:%M:%S))" >>"$LOG"
nice -n 19 "$PYTHON" phase3_matrix.py configure >>"$LOG" 2>&1 || echo "=== phase3 configure FAILED" >>"$LOG"
for row in "${PHASE3_ROWS[@]}"; do
    run_row phase3 "$row"
done
echo "=== ALL DONE" >>"$LOG"
