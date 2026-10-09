#!/bin/bash
# Generate the synthetic HDR10 test clip for the media-processing test suite.
#
# Produces a short 640x360 HEVC Main10 clip tagged HDR10 (BT.2020 + SMPTE
# ST 2084). Dolby Vision clips are not generated: a real RPU needs a DV
# encoder, so tests mock pymediainfo's hdr_format string for DV profiles.
#
# Run once and commit the output (< 1 MB). Not run in CI.
set -euo pipefail

FIXTURES_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly FIXTURES_DIR

# Use jellyfin-ffmpeg if present; fall back to system ffmpeg.
if [ -x /usr/lib/jellyfin-ffmpeg/ffmpeg ]; then
    readonly FFMPEG="/usr/lib/jellyfin-ffmpeg/ffmpeg"
else
    readonly FFMPEG="ffmpeg"
fi

echo "==> Using ffmpeg: $FFMPEG"
echo "==> Output dir:   $FIXTURES_DIR"

# Reusable pattern input: a 1-second testsrc2 (animated bars + timer).
# 640x360 @ 24fps for 1s — keeps repo artifacts under 1 MB each while
# still carrying full HDR10 metadata in the container.
readonly PATTERN='testsrc2=size=640x360:rate=24:duration=1'

# HDR10: HEVC Main10 with BT.2020 + SMPTE ST 2084 + static metadata.
echo "==> Generating hdr10_tiny.mkv"
"$FFMPEG" -y -hide_banner -loglevel warning \
    -f lavfi -i "$PATTERN" \
    -c:v libx265 -preset ultrafast -crf 30 \
    -pix_fmt yuv420p10le \
    -x265-params "colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50):max-cll=1000,400" \
    "$FIXTURES_DIR/hdr10_tiny.mkv"

echo "==> Done"
ls -lh "$FIXTURES_DIR"/*.mkv
