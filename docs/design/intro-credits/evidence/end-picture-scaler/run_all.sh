#!/bin/bash
# The four runs, one at a time at nice 19: the tree before the change (40311c3) and after it, each on the NVIDIA GPU
# and the CPU. BEFORE_TREE and AFTER_TREE are copies of media_preview_generator/ and tools/ at those two states.
set -euo pipefail

readonly HERE="$(cd "$(dirname "$0")" && pwd)"
readonly PY="${PY:-/home/data/.venv/bin/python}"
readonly BEFORE_TREE="${BEFORE_TREE:?copy of the tree at 40311c3}"
readonly AFTER_TREE="${AFTER_TREE:?copy of the tree with the change}"
export EP_OUT="${EP_OUT:-$HERE/local}"
mkdir -p "$EP_OUT"

run() {
    local tree="$1" label="$2" device="$3"
    nice -n 19 "$PY" "$HERE/ep_sets.py" "$tree" "$label" "$device" > "$EP_OUT/$label.log" 2>&1
}

run "$BEFORE_TREE" before_nvidia nvidia
run "$BEFORE_TREE" before_cpu cpu
run "$AFTER_TREE" after_nvidia nvidia
run "$AFTER_TREE" after_cpu cpu
"$PY" "$HERE/compare_runs.py" before_nvidia before_cpu after_nvidia after_cpu \
    --same after_nvidia after_cpu --moved before_nvidia after_nvidia --moved before_cpu before_nvidia
