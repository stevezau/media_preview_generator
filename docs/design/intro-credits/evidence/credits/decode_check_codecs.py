"""Which codecs a GPU decodes exactly as the CPU does, through the credits decode's own command (spec §5.4, the GPU
decode check).

Encodes one picture -- small credit text over black and a fractal, like the packaged reference clips -- in each codec,
then runs it through ``decode_check.DecodeChecks`` with that one clip on the given GPU: the check's own comparison of
every frame's timestamp and Y plane at 320x180 and 640x360 against the CPU's decode. Prints the check's log line per
codec.

Measured on storage's Quadro P5000 (ffmpeg 8.0.1, 2026-09-25): h264 and vp9 match (as does the packaged hevc clip);
mpeg2video and mpeg4 (Part 2) differ on 9 of 9 frames -- by at most 2 levels, on 0.14% and 77% of luma pixels, in a
pixel-by-pixel comparison of the same decodes. Those standards leave the inverse transform to each decoder. The check
is a diagnostic only and every codec's credits decode on the worker's GPU: on real MPEG-2 and MPEG-4 Part 2 files the
GPU's frames differ the same way but the answers don't (``decode_answers_gpu_vs_cpu.py``).

Usage: python decode_check_codecs.py [--gpu NVIDIA] [--device cuda:0] [--ffmpeg ffmpeg] [--out DIR]
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

from loguru import logger

from media_preview_generator.markers.credits import decode_check

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
# codec name (as ffprobe reports it) → encoder arguments; all 8-bit 4:2:0, a keyframe every 3 frames.
ENCODERS = {
    "h264": ["-c:v", "libx264", "-g", "3", "-bf", "1"],
    "mpeg2video": ["-c:v", "mpeg2video", "-g", "3", "-bf", "1", "-q:v", "4"],
    "mpeg4": ["-c:v", "mpeg4", "-g", "3", "-bf", "1", "-q:v", "4"],
    "vp9": ["-c:v", "libvpx-vp9", "-g", "3", "-deadline", "realtime", "-cpu-used", "8", "-b:v", "2M"],
}


def encode(ffmpeg: str, codec: str, out: Path) -> Path:
    text = ",".join(
        f"drawtext=fontfile={FONT}:fontsize={size}:fontcolor=white:x=600:y={40 + 80 * i}-3*t"
        f":text='EXECUTIVE PRODUCER  A. N. OTHER  {size}'"
        for i, size in enumerate((9, 10, 11, 12, 14))
    )
    graph = f"[0:v][1:v]overlay=x=40:y=40,drawbox=x=560:y=0:w=720:h=720:color=black:t=fill,{text}[v]"
    path = out / f"{codec}.mkv"
    subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x402060:size=1280x720:rate=1:duration=9",
         "-f", "lavfi", "-i", "mandelbrot=size=240x136:rate=1:start_scale=0.5,trim=end_frame=9",
         "-filter_complex", graph, "-map", "[v]", *ENCODERS[codec], "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gpu", default="NVIDIA")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--out", type=Path, default=None, help="where the encoded clips go (default: a temp folder)")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        out = args.out or Path(temp)
        out.mkdir(parents=True, exist_ok=True)
        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(message.record["message"]), level="INFO")
        try:
            for codec in ENCODERS:
                # An absolute name: ReferenceClip.path joins it onto CLIPS_DIR, which keeps it as it is.
                clip = decode_check.ReferenceClip(str(encode(args.ffmpeg, codec, out)), "nv12")
                decode_check.DecodeChecks(clips=(clip,)).check_device(args.gpu, args.device, ffmpeg=args.ffmpeg)
        finally:
            logger.remove(sink)
        for line in lines:
            print(line)


if __name__ == "__main__":
    main()
