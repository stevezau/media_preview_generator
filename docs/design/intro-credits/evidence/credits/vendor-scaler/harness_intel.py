"""Investigation only: the credits-text harness with every GPU decode run as today's Intel VAAPI command in a
throwaway container of the prod image on plex (one at a time), text detection on the storage CPU."""

import itertools
import os
import runpy
import shlex
import sys

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.credits import frames, textdet_helper  # noqa: E402

IMAGE = "425d989cc346"
MOUNTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
_counter = itertools.count(1)
_orig = frames.decode_command


def intel_decode_command(ffmpeg, path, **kw):
    if kw.get("gpu") is None:
        return _orig(ffmpeg, path, **kw)
    kw = {**kw, "gpu": "INTEL", "gpu_device_path": "/dev/dri/renderD128"}
    argv, active = _orig(ffmpeg, path, **kw)
    name = f"mpg-inteltest-h-{os.getpid()}-{next(_counter)}"
    vols = [x for m in MOUNTS for x in ("-v", f"{m}:{m}:ro")]
    docker = ["docker", "run", "--rm", "--name", name, "--cpus", "2", "--device", "/dev/dri:/dev/dri", *vols,
              "--entrypoint", "nice", IMAGE, "-n", "19", "/usr/local/bin/ffmpeg", *argv[1:]]
    return ["ssh", "-n", "-o", "BatchMode=yes", "plex", shlex.join(docker)], active


frames.decode_command = intel_decode_command


def _cpu_only(self, key, gpu):
    self._use_cpu(key, "the Intel harness keeps text detection on the CPU")
    return False


textdet_helper.TextDetectorPool._gpu_allowed = _cpu_only
sys.argv = ["python -m tools.markers_eval", *sys.argv[1:]]
runpy.run_module("tools.markers_eval", run_name="__main__", alter_sys=True)
