#!/bin/bash
# Credit text on sflix's own hardware, read-only: a throwaway container of the dev image with the Intel iGPU and the
# library mounted read-only runs intel_probe.py (the app's find_credits + chapter_origin) on one file. Nothing of the
# app's container or config is touched. Usage: run_intel.sh "<path on plex>" [INTEL /dev/dri/renderD128 | cpu cpu]
set -euo pipefail
readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
path="$1"
gpu="${2:-INTEL}"
device="${3:-/dev/dri/renderD128}"
scp -q "${HERE}/intel_probe.py" plex:/tmp/lvh_intel_probe.py
ssh plex "docker run --rm --device /dev/dri:/dev/dri -v /data_16tb:/data_16tb:ro -v /data_16tb2:/data_16tb2:ro \
  -v /data_16tb3:/data_16tb3:ro -v /tmp/lvh_intel_probe.py:/probe.py:ro --entrypoint nice \
  stevezzau/media_preview_generator:dev -n 19 python3 /probe.py '${path}' '${gpu}' '${device}' 2>&1 | grep -v DEBUG"
