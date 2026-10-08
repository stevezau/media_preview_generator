# Bundled loudness analyser

The image builds `/opt/ffmpeg-loudness/bin/ffmpeg` from the official FFmpeg
8.1.2 release with Plex's CPU loudness optimisation. Loudness jobs select this
binary automatically. Preview generation continues to use its usual FFmpeg.
Outside the image, a missing or non-executable analyser falls back to the
configured FFmpeg; explicit calls to the analysis/parity tools retain their
chosen binary.

The build keeps FFmpeg's native decoders, demuxers and parsers, including
zlib/bzip2/lzma support. It only enables the filters and null output needed
for loudness measurement. It uses local files and pipes, without network
protocols or external codec libraries. No CPU-specific `-march=native` flags
are used, so each image targets its architecture rather than the build host.

`build.sh` pins the source archive by SHA-256 and applies the patch without
fuzz. Build dependencies stay in a separate Docker stage. The runtime image
includes the source archive, patch and build recipe under
`/opt/ffmpeg-loudness/share/source/`.

Build just the analyser with:

```sh
docker build --target ffmpeg-loudness-builder -t mpg-loudness-build .
```

The normal application image build includes it automatically. Build
parallelism defaults to four compiler jobs; override it with
`--build-arg FFMPEG_LOUDNESS_JOBS=N`.

## Verify a build

`verify.py` compares complete loudnorm JSON reports from stock and patched
binaries on synthetic audio, then times stereo and 5.1 inputs. It writes no
Plex metadata and never reads user media. The host FFmpeg creates the fixtures;
AAC, AC3, EAC3, FLAC and Opus fixtures are required, while DTS core and TrueHD
are included when the host has their encoders.

```sh
python3 docker/ffmpeg-loudness/verify.py \
  --reference /path/to/stock/ffmpeg \
  --candidate /path/to/patched/ffmpeg \
  --fixture-ffmpeg /usr/bin/ffmpeg \
  --long-seconds 60 --repeats 3 > /tmp/ffmpeg-loudness-verify.txt
```

The command exits nonzero if the binaries are identical or any loudnorm report
differs. Use `--parity-only` to skip the long timing runs. See `verify.py --help`
for fixture-duration and repetition options.

## Patch provenance

`ebur128.patch` carries the change to `libavfilter/ebur128.c` between Plex's
[earlier FFmpeg source](https://downloads.plex.tv/ffmpeg-source/plex-media-server-ffmpeg-gpl-a336ba97d4.tar.gz)
(`a336ba97d45e9b418410d3ae5cd4d0a91a2fe498`) and
[later FFmpeg source](https://downloads.plex.tv/ffmpeg-source/plex-media-server-ffmpeg-gpl-f1731a7d24.tar.gz)
(`f1731a7d24d219866c7fc9ff69f8b21d8efc8e34`). The earlier file is
byte-for-byte identical to [Jellyfin FFmpeg 8.1.2's file](https://github.com/jellyfin/jellyfin-ffmpeg/blob/v8.1.2-2/libavfilter/ebur128.c),
and the official FFmpeg 8.1.2 release, so the patch applies directly to both.
The file has the
same SHA-256 (`bce02f6c8f0518d538596c7c46c6bbce68e5f50042f68a17cc04e6c533d1a459`)
in Jellyfin tags `v8.1.2-1` through `v8.1.2-5`.

The change processes independent channel filter states together, tracks sample
peaks across channels, and caches channel-weighted energy in 100 ms slices for
loudness gates and short-term windows. It retains the original whole-sample
calculation for partial windows. The patch changes only `ebur128.c`; its
existing FFmpeg LGPL 2.1-or-later and libebur128 MIT copyright notices remain
in that file. Attribute the added code to Plex's FFmpeg source above when
redistributing it, and provide the corresponding modified FFmpeg source and
license notices with any binary built from this patch.
