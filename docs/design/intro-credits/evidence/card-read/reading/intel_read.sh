#!/bin/bash
# Copies this branch's package, the recognition model and intel_read.py to plex:/tmp/cardread-check, then runs the
# script in a throwaway container of plex's image (Intel iGPU and NVIDIA passed in as the previews container gets
# them; nothing mounted from /data, nothing written but /tmp/cardread-check), and removes the copy.
set -euo pipefail
readonly HERE="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local"
readonly WT="/home/data/workspace/plex_generate_vid_previews"
readonly MODELS="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/card-read/local/models"
readonly REMOTE="/tmp/cardread-check"
readonly IMAGE="stevezzau/media_preview_generator:dev"
ssh -o BatchMode=yes plex "rm -rf $REMOTE && mkdir -p $REMOTE"
tar -C "$WT" -cf - --exclude=__pycache__ media_preview_generator | ssh -o BatchMode=yes plex "tar -C $REMOTE -xf -"
scp -q "$MODELS/latin_PP-OCRv5_rec_mobile.onnx" "$HERE/intel_read.py" "plex:$REMOTE/"
ssh -o BatchMode=yes plex "docker run --rm --name cardread-check --device /dev/dri:/dev/dri --runtime=nvidia \
  -e NVIDIA_VISIBLE_DEVICES=all -e NVIDIA_DRIVER_CAPABILITIES=all \
  -v $REMOTE:/check:ro -e PYTHONPATH=/check -e MEDIA_PREVIEW_TEXTREC_MODEL=/check/latin_PP-OCRv5_rec_mobile.onnx \
  -w /check --entrypoint python3 $IMAGE /check/intel_read.py; rm -rf $REMOTE"
