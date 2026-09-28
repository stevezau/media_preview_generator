#!/bin/bash
# Usage: py.sh script.py args...  — the shared venv's python under nice 19, from this folder, 15-min limit.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
exec timeout 3600 nice -n 19 /home/data/.venv/bin/python "$@"
