"""What the credit text keyframe pass returns for a window that holds no keyframe (spec §14 2026-09-23).

Run in the app image, with this checkout mounted so ``decode_command`` is the one under test:

    docker run --rm --gpus all -v "$PWD":/src:ro -v /tmp/empty_window:/w -e PYTHONPATH=/src \\
      --entrypoint /usr/bin/python3 ghcr.io/stevezau/media_preview_generator:pr-241 \\
      /src/docs/design/intro-credits/evidence/credits/empty_window_decode.py /w

It makes three 100 s clips with a keyframe every 10 s (0, 10, ... 90 s) -- H.264 in MP4 and in Matroska, and VP9 in
WebM -- and runs the app's own keyframe pass (``frames.decode_command``, VP9 with its packet drop) on the GPU and on the
CPU over one window that holds keyframes and two that hold none. For each it prints ffmpeg's exit code and the
timestamps of the frames it put out. Results: ``empty-window-decode.md`` beside this file.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from media_preview_generator.markers.credits import frames

SOURCE = "testsrc2=size=1280x720:rate=24:duration=100"
CLIPS = {
    "h264.mp4": ["-c:v", "libx264", "-bf", "2"],
    "h264.mkv": ["-c:v", "libx264", "-bf", "2"],
    "vp9.webm": ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8"],
}
# [12, 32) holds the 20 s and 30 s keyframes; [41, 43) and [41, 49.9) hold none.
WINDOWS = ((12.0, 20.0), (41.0, 2.0), (41.0, 8.9))


def main(out: Path) -> None:
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout.splitlines()[0]
    print(version)
    for name, codec in CLIPS.items():
        clip = out / name
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", SOURCE, *codec,
                        "-g", "240", "-keyint_min", "240", "-sc_threshold", "0", "-pix_fmt", "yuv420p", str(clip)],
                       check=True)  # fmt: skip
        for gpu, device in (("NVIDIA", "0"), (None, None)):
            for start_s, length_s in WINDOWS:
                command, hw_active = frames.decode_command(
                    "ffmpeg", str(clip), start_s=start_s, length_s=length_s, keyframes_only=True, fps=None, gpu=gpu,
                    gpu_device_path=device, drop_non_key=name.startswith("vp9"),
                )  # fmt: skip
                run = subprocess.run(command, capture_output=True)
                pts = [float(p) for p in re.findall(rb"pts_time:\s*(\S+)", run.stderr)]
                where = "GPU" if hw_active else "CPU"
                print(f"{name:9} {where} [{start_s:g}, {start_s + length_s:g}) exit {run.returncode} frames at {pts}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
