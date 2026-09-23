"""M3 (run inside the app image): what onnxruntime-ep-webgpu 0.3.0 lists per device, which provider options it accepts,
and how long a process that created a WebGPU session takes to exit (ORT PR #29591: shutdown hang without adapters).

    python3 measure_webgpu_devices.py <model.onnx>
"""

import json
import os
import subprocess
import sys
import time

if os.environ.get("USE_APP_VULKAN_PROBE"):
    from media_preview_generator.gpu import get_vulkan_device_info, get_vulkan_env_overrides

    print("probe:", get_vulkan_device_info(), "overrides:", get_vulkan_env_overrides(), flush=True)
    os.environ.update(get_vulkan_env_overrides())

import numpy as np
import onnxruntime as ort
import onnxruntime_ep_webgpu as webgpu_ep

MODEL = sys.argv[1]
ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
devices = [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]
print("devices:", json.dumps([
    {"type": str(d.device.type), "vendor": d.device.vendor, "vendor_id": hex(d.device.vendor_id),
     "device_id": hex(d.device.device_id), "metadata": dict(d.device.metadata), "ep_metadata": dict(d.ep_metadata),
     "ep_options": dict(d.ep_options)}
    for d in devices
], indent=1), flush=True)
feed = np.zeros((1, 3, 192, 320), dtype=np.float32)
for index, device in enumerate(devices):
    for options in ({}, {"powerPreference": "high-performance"}, {"deviceId": str(index)}):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        try:
            opts.add_provider_for_devices([device], options)
            session = ort.InferenceSession(MODEL, sess_options=opts)
            started = time.perf_counter()
            for _ in range(20):
                session.run(None, {session.get_inputs()[0].name: feed})
            ms = 1000 * (time.perf_counter() - started) / 20
            # A failed EP falls back to a CPU session without raising: the provider tells the two apart.
            print(f"device {index} options {options}: ok {ms:.1f} ms/frame on {session.get_providers()[0]}", flush=True)
        except Exception as exc:  # noqa: BLE001 - measuring what the EP rejects
            print(f"device {index} options {options}: {type(exc).__name__}: {exc}", flush=True)
if "--child" in sys.argv:
    print("SESSIONS_DONE", flush=True)  # the parent times the exit from here, not the sessions and runs above
else:
    child = subprocess.Popen([sys.executable, __file__, MODEL, "--child"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    for line in child.stdout:
        if line.startswith("SESSIONS_DONE"):
            break
    done = time.monotonic()
    try:
        code = child.wait(timeout=120)
        print(f"child exit {code} {time.monotonic() - done:.1f} s after its sessions were done")
    except subprocess.TimeoutExpired:
        child.kill()
        print("child still running 120 s after its sessions were done: the shutdown hang (killed)")
