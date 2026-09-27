#!/bin/bash
# phase4_row13_run.sh's rows in its order, stopping at the first row that fails where #320's branch run passed (the
# base: evidence/credits-accuracy/README.md "Lab regression"; those rows fail the same way on dev). Resume with
# FROM="phase2 7" to start at that row. Start from the state lab_run.sh's reset step leaves.
# Usage: MLAB_DIR=/path/to/lab lab_rows.sh <image>
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPTS="${HERE}/../lab"
: "${MLAB_DIR:?set MLAB_DIR to the lab folder that holds env, synth/ and results/}"
export MLAB_DIR
export MLAB_APP_IMAGE=$1
export MLAB_APP_GPU=nvidia
# The volume lab_run.sh made the fresh app on: phase 3's rows recreate the app through app.sh.
export MLAB_APP_CONFIG_VOLUME="${CONFIG_VOLUME:-mlab_app_config_credrem}"
readonly PYTHON="${MLAB_PYTHON:-/home/data/.venv/bin/python}"
readonly LOG="${HERE}/local/lab_rows.log"
readonly PHASE2_ROWS=(23 1 21 17 19 22 2 3 4 18 5 6 8 7 9 10 11 12 13 14 16 15 20 24)
readonly PHASE3_ROWS=(1 2 3 4 5 6 7 8 9 10 11 16)
# Rows #320's branch run failed, each the same way on dev (the matrices' own drift).
readonly BASE_FAILS=" phase2-2 phase2-3 phase2-4 phase2-5 phase2-6 phase2-10 phase2-12 phase2-18 phase3-2 phase3-3 phase3-5 phase3-8 phase3-9 phase3-10 phase3-11 phase3-16 "
started=${FROM:+no}
started=${started:-yes}
mkdir -p "${HERE}/local" "${MLAB_DIR}/results"
if [[ "$started" == "yes" ]]; then
    # As phase4_row13_run.sh: earlier passes must not stand in for a row that crashes before it writes its result.
    archive="${MLAB_DIR}/results/before-credrem-run-$(date -u +%Y%m%dT%H%M%S)"
    mkdir -p "$archive"
    for pattern in 'p2-row-??' 'p3-row-??' 'row-[01]?'; do
        for old in "${MLAB_DIR}/results/"${pattern}.json; do
            [[ -e "$old" ]] && mv "$old" "$archive/"
        done
    done
fi
cd "$SCRIPTS"

run_row() {
    local phase=$1 row=$2
    if [[ "$started" == "no" ]]; then
        [[ "$phase $row" == "$FROM" ]] || return 0
        started=yes
    fi
    echo "=== ${phase} row ${row} ($(date -u +%H:%M:%S))" >>"$LOG"
    local status=0
    nice -n 19 "$PYTHON" "${phase}_matrix.py" run "$row" >>"$LOG" 2>&1 || status=$?
    echo "=== ${phase} row ${row} exit ${status} ($(date -u +%H:%M:%S))" >>"$LOG"
    if [[ "$status" -ne 0 && "$BASE_FAILS" != *" ${phase}-${row} "* ]]; then
        echo "=== WORSE THAN BASE: ${phase} row ${row}" >>"$LOG"
        exit 1
    fi
}

for row in "${PHASE2_ROWS[@]}"; do
    run_row phase2 "$row"
done
if [[ "$started" == "yes" ]]; then
    echo "=== phase3 configure ($(date -u +%H:%M:%S))" >>"$LOG"
    nice -n 19 "$PYTHON" phase3_matrix.py configure >>"$LOG" 2>&1 || echo "=== phase3 configure FAILED" >>"$LOG"
fi
for row in "${PHASE3_ROWS[@]}"; do
    run_row phase3 "$row"
done
echo "=== ALL DONE" >>"$LOG"
