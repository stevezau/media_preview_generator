#!/bin/bash
# Lab regression (spec §10.3, phase4-results.md "Row 13") of this branch: start the lab, take our markers off with
# the old app, a fresh mlab-app on the branch's image, configure, then lab_rows.sh (phase4_row13_run.sh's rows, fail-fast). Scripts from this checkout,
# lab state (env, synth/, results/) from MLAB_DIR (the main checkout's lab folder: env and results are local-only).
# Usage: MLAB_DIR=/path/to/lab lab_run.sh <image> [all|start|reset|run]
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPTS="${HERE}/../lab"
: "${MLAB_DIR:?set MLAB_DIR to the lab folder that holds env, synth/ and results/}"
export MLAB_DIR
export MLAB_PYTHON="${MLAB_PYTHON:-/home/data/.venv/bin/python}"
readonly IMAGE=$1
readonly STEP="${2:-all}"
readonly LOG="${HERE}/local/lab_run.log"
readonly CONFIG_VOLUME="${CONFIG_VOLUME:-mlab_app_config_credrem}"
mkdir -p "${HERE}/local"

cd "$SCRIPTS"
log() { echo "=== $* ($(date -u +%H:%M:%S))" >>"$LOG"; }

if [[ "$STEP" == "all" || "$STEP" == "start" ]]; then
    log "start lab"
    docker start mlab-plex mlab-emby mlab-emby49 mlab-jellyfin mlab-jf12 mlab-app >>"$LOG" 2>&1
    for _ in $(seq 1 90); do
        curl -fs -o /dev/null http://127.0.0.1:18080/api/health && break
        sleep 2
    done
fi
if [[ "$STEP" == "all" || "$STEP" == "reset" ]]; then
    log "reset (old app)"
    nice -n 19 "$MLAB_PYTHON" phase4_row13_reset.py >>"$LOG" 2>&1 || { log "reset FAILED"; exit 1; }
    # A new empty config volume rather than removing mlab_app_config, which other lanes' runs keep.
    log "fresh app on ${IMAGE} (config volume ${CONFIG_VOLUME})"
    if docker volume inspect "$CONFIG_VOLUME" >/dev/null 2>&1; then
        log "config volume ${CONFIG_VOLUME} exists: pick a new name (CONFIG_VOLUME=...)"
        exit 1
    fi
    MLAB_APP_CONFIG_VOLUME="$CONFIG_VOLUME" MLAB_APP_IMAGE="$IMAGE" MLAB_APP_GPU=nvidia ./app.sh recreate >>"$LOG" 2>&1
    nice -n 19 "$MLAB_PYTHON" phase2_matrix.py configure >>"$LOG" 2>&1
fi
if [[ "$STEP" == "all" || "$STEP" == "run" ]]; then
    # phase4_row13_run.sh's rows and order, stopping at the first row worse than #320's run (lab_rows.sh).
    log "rows"
    "${HERE}/lab_rows.sh" "$IMAGE" >>"$LOG" 2>&1 || log "rows exit $?"
fi
log "LAB DONE"
