#!/bin/bash
# Baseline (dev 4a34687): credit text on the verdict/Plex/chapter files, then the harness sets. One heavy job at a time.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PY=/home/data/.venv/bin/python
readonly BASE="${HERE}/base"
cd "$HERE"
nice -n 19 "$PY" vtext.py "$BASE" "${HERE}/vtext_base.json" >"${HERE}/vtext_base.log" 2>&1 || echo "vtext failed" >>"${HERE}/vtext_base.log"
nice -n 19 "$PY" harness.py "$BASE" credits-text --decode gpu --sets 80,205,accused,isurvived --online \
    --json "${HERE}/ct_base.json" >"${HERE}/ct_base.log" 2>&1 || echo "harness exit $?" >>"${HERE}/ct_base.log"
echo DONE >>"${HERE}/ct_base.log"
