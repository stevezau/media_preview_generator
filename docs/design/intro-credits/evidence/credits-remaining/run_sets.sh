#!/bin/bash
# The harness sets (80, 205, Accused, I Survived, the online cases) on one tree, GPU decode, under nice.
# Usage: run_sets.sh <tree> <tag> [--changed-since earlier.json]
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PY=/home/data/.venv/bin/python
tree=$1
tag=$2
shift 2
mkdir -p "${HERE}/local"
nice -n 19 "$PY" "${HERE}/../credits-accuracy/harness.py" "$tree" credits-text --decode gpu \
    --sets 80,205,accused,isurvived --online --json "${HERE}/local/ct_${tag}.json" "$@" \
    >"${HERE}/local/ct_${tag}.log" 2>&1 || echo "harness exit $?" >>"${HERE}/local/ct_${tag}.log"
echo DONE >>"${HERE}/local/ct_${tag}.log"
