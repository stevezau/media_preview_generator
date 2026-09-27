#!/bin/bash
# Work tree (pre-merge, same decode digest as base): credit text on the verdict/Plex/chapter files from the decode cache.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PY=/home/data/.venv/bin/python
readonly WORK="${CREDFIX_WORK:-$(cd "${HERE}/../../../../.." && pwd)}"
cd "$HERE"
[[ -f "${HERE}/vtext_work.json" ]] && mv "${HERE}/vtext_work.json" "${HERE}/vtext_work_old.json"
nice -n 19 "$PY" vtext.py "$WORK" "${HERE}/vtext_work.json" >"${HERE}/vtext_work.log" 2>&1 || echo "vtext failed" >>"${HERE}/vtext_work.log"
echo DONE >>"${HERE}/vtext_work.log"
