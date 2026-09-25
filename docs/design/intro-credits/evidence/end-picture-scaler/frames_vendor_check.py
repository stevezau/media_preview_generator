"""The end-picture decode on the CPU and on CUDA, one tree at a time: are the 320x180 luma planes byte-identical?

    nice -n 19 python frames_vendor_check.py <tree> [clip ...]

Without clips, makes the integration test's two (12 s of mandelbrot at 1280x720, 8-bit H.264 and 10-bit HEVC) in
./clips. A clip may also be a library file (read only), decoded at 8.2-11.7 s or at ``path@start``.
"""

import os
import subprocess
import sys

import numpy as np

TREE = sys.argv[1]
sys.path.insert(0, TREE)
from media_preview_generator.markers.audio import end_picture as ep  # noqa: E402
from media_preview_generator.markers.credits import frames  # noqa: E402
from media_preview_generator.markers.probe import ffprobe_path_for, stream_starts  # noqa: E402

assert ep.__file__.startswith(TREE)
FFMPEG = "/usr/bin/ffmpeg"
HERE = os.path.dirname(os.path.abspath(__file__))


def clips():
    root = os.path.join(HERE, "clips")
    os.makedirs(root, exist_ok=True)
    source = "mandelbrot=size=1280x720:rate=24,trim=duration=12,setpts=PTS-STARTPTS"
    encodes = {
        "8-bit": ["-c:v", "libx264", "-g", "48", "-pix_fmt", "yuv420p"],
        "10-bit": ["-c:v", "libx265", "-x265-params", "keyint=48:log-level=error", "-pix_fmt", "yuv420p10le"],
    }
    out = []
    for name, codec in encodes.items():
        path = os.path.join(root, f"{name}.mkv")
        if not os.path.exists(path):
            subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", source, *codec, path], check=True)
        out.append(path)
    return out


def decode(path, start, gpu, device):
    planes, runs = [], []
    shrink, run_decode = ep._shrink, frames.run_decode

    def keep(chunk):
        planes.extend(plane.copy() for plane in chunk)
        return shrink(chunk)

    def note(command, *, hw_active, **kwargs):
        runs.append((hw_active, command[command.index("-vf") + 1]))
        return run_decode(command, hw_active=hw_active, **kwargs)

    ep._shrink, frames.run_decode = keep, note
    try:
        starts = stream_starts(path, ffprobe=ffprobe_path_for(FFMPEG))
        kwargs = {"ffmpeg": FFMPEG, "gpu": gpu, "gpu_device_path": device, "container_start_s": starts.container_s}
        if "download_format" in ep.decode_frames.__code__.co_varnames:
            kwargs["download_format"] = frames.DOWNLOAD_FORMATS.get(starts.pix_fmt or "")
        got = ep.decode_frames(path, start, 3.5, **kwargs)
    finally:
        ep._shrink, frames.run_decode = shrink, run_decode
    return [t for t, _ in got], np.array(planes), runs


for spec in sys.argv[2:] or clips():
    path, _, at = spec.partition("@")
    start = float(at) if at else 8.2
    cpu_t, cpu, cpu_runs = decode(path, start, None, None)
    gpu_t, gpu, gpu_runs = decode(path, start, "NVIDIA", "cuda:0")
    same = cpu_t == gpu_t and cpu.shape == gpu.shape and np.array_equal(cpu, gpu)
    diff = np.abs(cpu.astype(int) - gpu.astype(int)) if cpu.shape == gpu.shape else None
    print(os.path.basename(path)[:60], f"frames {len(cpu)}/{len(gpu)}", "IDENTICAL" if same else "DIFFERENT",
          "" if diff is None else f"mean|d| {diff.mean():.3f} max|d| {diff.max()}",
          f"cpu {cpu_runs[-1][1]!r} gpu {gpu_runs[-1][0]} {gpu_runs[-1][1]!r}", flush=True)  # fmt: skip
