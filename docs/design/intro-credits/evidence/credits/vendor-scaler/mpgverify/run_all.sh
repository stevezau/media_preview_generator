#!/bin/bash
# Verification only: every file through cpu, cuda, vaapi, one at a time, niced.
set -uo pipefail
readonly OUT=/tmp/mpg-verify/out
readonly H=/tmp/mpg-verify/harness
while IFS=$'\t' read -r tag kind path; do
  [[ -z "$tag" ]] && continue
  ep=()
  [[ "$kind" == "e" ]] && ep=(--episode)
  for v in cpu cuda vaapi; do
    [[ -f "$OUT/${tag}_${v}.json" ]] && continue
    echo "start $(date +%T) $tag $v" >> "$OUT/run.log"
    nice -n 19 python3 "$H/verify.py" "$v" "$tag" "$path" "$OUT" "${ep[@]}" >> "$OUT/run.log" 2>> "$OUT/run_${tag}_${v}.err" < /dev/null
    echo "exit $? $(date +%T) $tag $v" >> "$OUT/run.log"
  done
done < "$H/files.tsv"
echo "ALLDONE $(date +%T)" >> "$OUT/run.log"
