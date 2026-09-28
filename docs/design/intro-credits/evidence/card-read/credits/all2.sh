#!/bin/bash
# The work tree again (after the list, crawl and confidence guards) on every credits set, cheapest first, as TAG.
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly TAG="${1:-work2}"
for sets in targets 80 items accused isurvived 205; do
    "$HERE/run.sh" work "$TAG" "$sets"
done
echo "ALLDONE $TAG" >> "$HERE/logs/all.log"
