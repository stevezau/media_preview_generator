"""In-container check: ONNX Runtime WebGPU EP vs CPU on saved credits frames. Prints adapters seen and speed."""
import os, sys, time
import numpy as np
import onnxruntime as ort
import onnxruntime_ep_webgpu as webgpu_ep
from rapidocr_onnxruntime import RapidOCR

if os.environ.get("USE_APP_VULKAN_PROBE"):
    # Reuse the app's DV5 libplacebo probe: it works out the env (EGL vendor JSON / VK_DRIVER_FILES) that makes the
    # NVIDIA Vulkan ICD load inside the container. Must be applied before the Vulkan loader starts in this process.
    from media_preview_generator.gpu import get_vulkan_device_info, get_vulkan_env_overrides
    info = get_vulkan_device_info()
    env = get_vulkan_env_overrides()
    print("app vulkan probe:", info, "env overrides:", env, flush=True)
    os.environ.update(env)
frames = np.load("/work/frames.npy")
imgs = [np.stack([f] * 3, axis=-1) for f in frames]
ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
devs = [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]
print("webgpu devices:", [dict(d.device.metadata) | {"vendor_id": hex(d.device.vendor_id), "device_id": hex(d.device.device_id)} for d in devs], flush=True)
if not devs:
    print("NO WEBGPU DEVICE"); sys.exit(0)
pref = os.environ.get("POWER_PREF", "high-performance")
opts = ort.SessionOptions(); opts.intra_op_num_threads = 2
opts.add_provider_for_devices(devs[:1], {"powerPreference": pref})
sess = ort.InferenceSession("/work/ch_PP-OCRv4_det_infer.onnx", sess_options=opts)
print("session providers:", sess.get_providers(), "powerPreference:", pref, flush=True)

class Infer:
    def __init__(self, s): self.s, self.n = s, s.get_inputs()[0].name
    def __call__(self, x): return self.s.run(None, {self.n: x})

cpu = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
gpu = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
gpu.text_det.infer = Infer(sess)
res = {}
for name, det in (("cpu", cpu.text_det), ("webgpu", gpu.text_det)):
    for im in imgs[:5]: det(im)
    t = time.perf_counter(); c = []
    for im in imgs:
        b, _ = det(im); c.append(0 if b is None else len(b))
    res[name] = np.array(c)
    print(f"{name:8} {1000 * (time.perf_counter() - t) / len(imgs):6.1f} ms/frame", flush=True)
print(f"identical box counts {np.mean(res['cpu'] == res['webgpu']) * 100:.1f}% over {len(imgs)} frames")
