#!/bin/bash
# One tree's credit text on every file this lane measures, then its replay: the verdict/Plex/chapter files (vtext.py),
# the harness sets (the harness itself, then allsets.py from its caches) and the audit's markers.db replayed with that
# credit text. Decodes are cached by the code that turns a command into rows (rule_j.py, detector.py and decide.py
# left out), so a tree that changes only those decodes nothing but new windows. One heavy job at a time, under nice.
# Usage: run_all.sh <tree> <tag> <base|work> [text|sets|replay ...]
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ACC="${HERE}/../credits-accuracy"
readonly PY=/home/data/.venv/bin/python
tree=$1
tag=$2
which=$3
shift 3
steps=("$@")
[[ ${#steps[@]} -eq 0 ]] && steps=(text sets replay)
mkdir -p "${HERE}/local"
for step in "${steps[@]}"; do
    case "$step" in
        text)
            (cd "$ACC" && nice -n 19 "$PY" vtext.py "$tree" "${HERE}/local/vtext_${tag}.json" \
                >"${HERE}/local/vtext_${tag}.log" 2>&1) || echo "vtext failed" >>"${HERE}/local/vtext_${tag}.log"
            ;;
        sets)
            "${HERE}/run_sets.sh" "$tree" "$tag"
            nice -n 19 "$PY" "${HERE}/allsets.py" "$tree" "${HERE}/local/allsets_${tag}.json" \
                >"${HERE}/local/allsets_${tag}.log" 2>&1 || echo "allsets failed" >>"${HERE}/local/allsets_${tag}.log"
            ;;
        rows)
            # The harness sets' answers alone (no harness report): an ablation tree read from the decode cache.
            nice -n 19 "$PY" "${HERE}/allsets.py" "$tree" "${HERE}/local/allsets_${tag}.json" \
                >"${HERE}/local/allsets_${tag}.log" 2>&1 || echo "allsets failed" >>"${HERE}/local/allsets_${tag}.log"
            ;;
        replay)
            if [[ "$which" == "base" ]]; then
                export CREDFIX_BASE="$tree"
            else
                export CREDFIX_WORK="$tree"
            fi
            (cd "$ACC" && nice -n 19 "$PY" replay.py "$which" "${HERE}/local/replay_${tag}.json" \
                --text "${HERE}/local/vtext_${tag}.json" --audio "${ACC}/audio_inject.json" \
                >"${HERE}/local/replay_${tag}.log" 2>&1) || echo "replay failed" >>"${HERE}/local/replay_${tag}.log"
            ;;
        *)
            echo "unknown step ${step}" >&2
            exit 1
            ;;
    esac
    echo "${step} done" >>"${HERE}/local/run_${tag}.log"
done
