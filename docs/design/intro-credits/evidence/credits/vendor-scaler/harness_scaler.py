"""Investigation only: the credits-text harness with every decode path downloading the decoded frame and scaling it
with one swscale filter (``--scaler FLAGS``), and text detection on the CPU (boxes match the GPU helper's)."""

import runpy
import sys

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
args = sys.argv[1:]
flags = args[args.index("--scaler") + 1]
del args[args.index("--scaler"): args.index("--scaler") + 2]
from media_preview_generator.markers.credits import frames, textdet_helper  # noqa: E402

frames._GPU_SCALE_VENDORS = ()
frames._scale_filter = lambda gpu, hw_active, keep_on_gpu: f"scale=320:180:flags={flags},format=nv12"


def _cpu_only(self, key, gpu):
    self._use_cpu(key, "the scaler harness keeps text detection on the CPU")
    return False


textdet_helper.TextDetectorPool._gpu_allowed = _cpu_only
sys.argv = ["python -m tools.markers_eval", *args]
runpy.run_module("tools.markers_eval", run_name="__main__", alter_sys=True)
