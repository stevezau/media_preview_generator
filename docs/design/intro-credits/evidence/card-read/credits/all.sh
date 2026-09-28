#!/bin/bash
# Base then work on every credits set, cheapest first (80, then the audit's files, Accused, I Survived, the 205).
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for sets in 80 items accused isurvived 205; do
    "$HERE/run.sh" base base "$sets"
    "$HERE/run.sh" work work "$sets"
done
echo ALLDONE >> "$HERE/logs/all.log"
