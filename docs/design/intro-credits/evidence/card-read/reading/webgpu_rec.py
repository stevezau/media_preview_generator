"""Scratch: can ONNX Runtime's WebGPU EP load and run the Latin recognition model on the P5000 (helper environment)?"""

import os
import sys
import time

CODE = "/home/data/workspace/plex_generate_vid_previews"
sys.path.insert(0, CODE)
os.chdir(CODE)
from media_preview_generator.gpu.vulkan_probe import get_vulkan_env_overrides  # noqa: E402

os.environ.update(get_vulkan_env_overrides())
import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402

from media_preview_generator.markers.credits import devices, textdet, textrec  # noqa: E402

REC = sys.argv[1]
found = textdet.webgpu_devices()
print([dict(d.device.metadata) for d in found])
index = devices.choose_ep_device([dict(d.device.metadata) for d in found], "0000:02:00.0")
print("index", index)
ort.set_default_logger_severity(3)
opts = textdet._session_options(2)
opts.graph_optimization_level = getattr(ort.GraphOptimizationLevel, sys.argv[2])
opts.log_severity_level = 3
opts.add_provider_for_devices([found[index]], {})
try:
    s = ort.InferenceSession(REC, sess_options=opts)
    print("providers", s.get_providers())
    x = np.zeros((1, 3, 48, 320), np.float32)
    t0 = time.time()
    y = s.run(None, {s.get_inputs()[0].name: x})[0]
    print("ran", y.shape, f"{time.time() - t0:.2f}s")
except Exception as exc:  # noqa: BLE001
    print("FAILED", type(exc).__name__, str(exc)[:3000])
