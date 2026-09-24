"""Investigation only: the app's find_credits on one file through one vendor's decode path, capturing every decoded
frame's Y plane and boxes.

Usage: run_vendor.py VENDOR PATH OUT_PREFIX [--episode] [--filter-override EXPR]
VENDOR: intel (remote VAAPI on plex, throwaway container), nvidia (local CUDA), cpu (local).
--scale-override replaces the scale filter chain for this vendor (the one-variable experiments).
Text detection always runs here on the storage CPU (same ONNX session for every vendor).
"""

import argparse
import itertools
import json
import os
import shlex
import sys
import time

import numpy as np

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.credits import detector, frames, rule_j, textdet  # noqa: E402
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms  # noqa: E402
from media_preview_generator.markers.probe import probe_media  # noqa: E402

IMAGE = "425d989cc346"
MOUNTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
_counter = itertools.count(1)

p = argparse.ArgumentParser()
p.add_argument("vendor", choices=["intel", "nvidia", "cpu"])
p.add_argument("path")
p.add_argument("out")
p.add_argument("--episode", action="store_true")
p.add_argument("--download", action="store_true", help="GPU decode, download full frames, scale on the CPU")
p.add_argument("--cpu-ffmpeg-remote", action="store_true", help="run the cpu vendor's ffmpeg in the plex container")
p.add_argument("--extra-hw-frames", type=int, default=None)
p.add_argument("--scale-override", default=None, help="replacement for frames._scale_filter output")
a = p.parse_args()

model = os.environ["MEDIA_PREVIEW_TEXTDET_MODEL"]
det = textdet.TextDetector(textdet.cpu_session(model, 2), backend="cpu")

orig_decode_command = frames.decode_command
orig_scale = frames._scale_filter
if a.scale_override:
    frames._scale_filter = lambda gpu, hw, keep: a.scale_override

if a.download:
    frames._GPU_SCALE_VENDORS = ()
commands = []


def remote_decode_command(ffmpeg, path, **kw):
    argv, active = orig_decode_command(ffmpeg, path, **kw)
    if a.extra_hw_frames and "-hwaccel_output_format" in argv:
        k = argv.index("-hwaccel_output_format") + 2
        argv = argv[:k] + ["-extra_hw_frames", str(a.extra_hw_frames)] + argv[k:]
    commands.append(argv)
    if a.vendor != "intel" and not (a.vendor == "cpu" and a.cpu_ffmpeg_remote):
        return ["nice", "-n", "19", *argv], active
    n = next(_counter)
    name = f"mpg-inteltest-{os.getpid()}-{n}"
    vols = [x for m in MOUNTS for x in ("-v", f"{m}:{m}:ro")]
    docker = ["docker", "run", "--rm", "--name", name, "--cpus", "2", "--device", "/dev/dri:/dev/dri", *vols,
              "--entrypoint", "nice", IMAGE, "-n", "19", "/usr/local/bin/ffmpeg", *argv[1:]]
    return ["ssh", "-o", "BatchMode=yes", "plex", shlex.join(docker)], active


frames.decode_command = remote_decode_command
orig_run_decode = frames.run_decode
decodes = []


def logged_run_decode(command, **kw):
    rows = orig_run_decode(command, **kw)
    decodes.append([[x[0], x[1], x[2]] for x in rows])
    return rows


frames.run_decode = logged_run_decode

planes_log = []
boxes_log = []


def detect(planes):
    found = det.detect(planes)
    planes_log.append(planes.copy())
    boxes_log.extend(found)
    return found


probe = probe_media(a.path, ffprobe="/usr/bin/ffprobe", timeout_s=60)
dur_ms = probe.duration_ms
window_ms, cap_ms = credits_limits_ms(is_episode=a.episode, tv_window_s=None, movie_window_s=None)
earliest = earliest_credits_start_ms(dur_ms, is_movie=not a.episode, credits_window_ms=window_ms,
                                     movie_credits_max_from_end_ms=cap_ms) / 1000.0
gpu = {"intel": "INTEL", "nvidia": "NVIDIA", "cpu": None}[a.vendor]
dev = {"intel": "/dev/dri/renderD128", "nvidia": "cuda:0", "cpu": None}[a.vendor]
t0 = time.monotonic()
r = detector.find_credits(a.path, duration_ms=dur_ms, is_episode=a.episode, ffmpeg="/usr/bin/ffmpeg",
                          detect_boxes=detect, gpu=gpu, gpu_device_path=dev, earliest_start_s=earliest)
secs = time.monotonic() - t0
key = [list(x[:3]) + [list(map(list, x[3]))] for x in r.key_rows]
fine = [list(x[:3]) + [list(map(list, x[3]))] for x in r.fine_rows]
end = [list(x[:3]) + [list(map(list, x[3]))] for x in r.end_rows]
out = {"vendor": a.vendor, "path": a.path, "override": a.scale_override, "download": a.download, "remote_cpu": a.cpu_ffmpeg_remote, "start": r.start_s, "end": r.end_s,
       "secs": round(secs, 1), "dur_ms": dur_ms, "earliest": earliest, "commands": commands,
       "decodes": decodes, "key": key, "fine": fine, "endrows": end, "overlays": [list(b) for b in r.overlays]}
json.dump(out, open(a.out + ".json", "w"))
allp = np.concatenate(planes_log) if planes_log else np.zeros((0, 180, 320), np.uint8)
np.savez_compressed(a.out + ".npz", planes=allp)
print(a.vendor, os.path.basename(a.path)[:50], "start", r.start_s, "end", r.end_s, "keyrows", len(key),
      "fine", len(fine), "secs", round(secs, 1), flush=True)
