#!/bin/bash
# Prod replay base vs work and the diff. Usage: replay.sh <tag> <base text json|-> <work text json|-> [--audio x.json]
set -euo pipefail
readonly H="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PY=/home/data/.venv/bin/python
tag=$1
base_text=$2
work_text=$3
shift 3
cd "$H"
base_args=()
work_args=()
[[ "$base_text" != "-" ]] && base_args=(--text "$base_text")
[[ "$work_text" != "-" ]] && work_args=(--text "$work_text")
nice -n 19 "$PY" replay.py base "$H/replay_base_${tag}.json" "${base_args[@]}" "$@"
nice -n 19 "$PY" replay.py work "$H/replay_work_${tag}.json" "${work_args[@]}" "$@"
"$PY" diff_replay.py "$H/replay_base_${tag}.json" "$H/replay_work_${tag}.json"
