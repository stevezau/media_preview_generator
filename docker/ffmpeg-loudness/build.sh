#!/bin/bash
set -euo pipefail

readonly FFMPEG_VERSION="8.1.2"
readonly FFMPEG_ARCHIVE_URL="https://ffmpeg.org/releases/ffmpeg-${FFMPEG_VERSION}.tar.xz"
readonly FFMPEG_ARCHIVE_SHA256="464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c"
readonly PATCH_FILE="/tmp/ffmpeg-loudness/ebur128.patch"
readonly SOURCE_README="/tmp/ffmpeg-loudness/README.md"
readonly BUILD_ROOT="/tmp/ffmpeg-loudness-build"
readonly PREFIX="/opt/ffmpeg-loudness"
readonly JOBS="${FFMPEG_LOUDNESS_JOBS:-4}"

if [[ ! "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
    echo "FFMPEG_LOUDNESS_JOBS must be a positive integer" >&2
    exit 2
fi

mkdir -p "$BUILD_ROOT/download" "$PREFIX/bin" "$PREFIX/share/source"
curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors --retry-connrefused \
    "$FFMPEG_ARCHIVE_URL" -o "$BUILD_ROOT/download/ffmpeg-${FFMPEG_VERSION}.tar.xz"
printf '%s  %s\n' "$FFMPEG_ARCHIVE_SHA256" \
    "$BUILD_ROOT/download/ffmpeg-${FFMPEG_VERSION}.tar.xz" | sha256sum --check --status

cp "$BUILD_ROOT/download/ffmpeg-${FFMPEG_VERSION}.tar.xz" "$PREFIX/share/source/"
cp "$PATCH_FILE" "$PREFIX/share/source/ebur128.patch"
cp "$SOURCE_README" "$PREFIX/share/source/README.md"
cp /tmp/ffmpeg-loudness/build.sh "$PREFIX/share/source/build.sh"

tar -xf "$BUILD_ROOT/download/ffmpeg-${FFMPEG_VERSION}.tar.xz" -C "$BUILD_ROOT"
cd "$BUILD_ROOT/ffmpeg-${FFMPEG_VERSION}"
patch --batch --fuzz=0 -p1 < "$PATCH_FILE"
cp COPYING.LGPLv2.1 LICENSE.md "$PREFIX/share/source/"

configure_args=(
    --prefix="$PREFIX"
    --bindir="$PREFIX/bin"
    --disable-autodetect
    --disable-network
    --disable-devices
    --disable-doc
    --disable-debug
    --disable-ffplay
    --disable-ffprobe
    --disable-shared
    --enable-static
    --disable-encoders
    --enable-encoder=pcm_s16le
    --disable-muxers
    --enable-muxer=null
    --disable-filters
    --enable-filter=abuffer
    --enable-filter=abuffersink
    --enable-filter=aformat
    --enable-filter=anull
    --enable-filter=aresample
    --enable-filter=loudnorm
    --disable-protocols
    --enable-protocol=file
    --enable-protocol=pipe
    --enable-zlib
    --enable-bzlib
    --enable-lzma
)

printf '%s\n' "${configure_args[@]}" > "$PREFIX/share/source/configure-args.txt"
./configure "${configure_args[@]}"
make -j"$JOBS" ffmpeg
install -m 755 ffmpeg "$PREFIX/bin/ffmpeg"

# Exercise a real decode/filter/encode-to-null path without relying on optional
# lavfi devices or any host media files.
python3 - "$BUILD_ROOT/smoke.wav" <<'PY'
import math
import struct
import sys
import wave

sample_rate = 48000
frames = sample_rate * 4
with wave.open(sys.argv[1], "wb") as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(sample_rate)
    output.writeframes(b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / sample_rate)))
        for i in range(frames)
    ))
PY

"$PREFIX/bin/ffmpeg" -hide_banner -v info -i "$BUILD_ROOT/smoke.wav" \
    -af 'loudnorm=I=-16:TP=-1:LRA=9:print_format=json' -f null - 2> "$BUILD_ROOT/smoke.log"
python3 - "$BUILD_ROOT/smoke.log" <<'PY'
import json
import math
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
reports = re.findall(r'\{\s*"input_i".*?\}', text, flags=re.DOTALL)
if not reports:
    raise SystemExit("loudnorm smoke test produced no JSON report")
report = json.loads(reports[-1])
fields = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
for field in fields:
    if field not in report or not math.isfinite(float(report[field])):
        raise SystemExit(f"loudnorm smoke test produced no finite {field}")
PY
