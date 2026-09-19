"""Vendor-neutral GPU text detection check: ONNX Runtime WebGPU plugin EP (Dawn → Vulkan) vs ONNX Runtime CPU.

Same original PP-OCRv4 det ONNX model, same RapidOCR pre/post-processing; only the session's EP differs.
usage: bench_webgpu.py <every-nth-eval-file>
"""

import json
import subprocess
import sys
import time

import numpy as np
import onnxruntime as ort
import onnxruntime_ep_webgpu as webgpu_ep
from rapidocr_onnxruntime import RapidOCR

W, H = 320, 180
MODEL = "ncnnmodel/ch_PP-OCRv4_det_infer.onnx"
FILES = [json.loads(line) for line in open("credits/f3.jsonl")][:: int(sys.argv[1]) if len(sys.argv) > 1 else 10]


def frames(item):
    start = max(0.0, item["truth"] - 60)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-threads",
        "2",
        "-skip_frame",
        "nokey",
        "-ss",
        f"{start:.2f}",
        "-t",
        "120",
        "-i",
        item["file"],
        "-an",
        "-sn",
        "-dn",
        "-fps_mode",
        "passthrough",
        "-vf",
        f"scale={W}:{H},format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (W * H)
    return [np.stack([np.frombuffer(raw, np.uint8, W * H, i * W * H).reshape(H, W)] * 3, axis=-1) for i in range(n)]


class SessionInfer:
    def __init__(self, session: ort.InferenceSession):
        self.s = session
        self.name = session.get_inputs()[0].name

    def __call__(self, x: np.ndarray):
        return self.s.run(None, {self.name: x})


ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
devices = [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]
print("webgpu EP devices:", [(d.ep_name, d.device.type, d.device.vendor, dict(d.device.metadata)) for d in devices])
opts = ort.SessionOptions()
opts.add_provider_for_devices(devices[:1], {})
opts.intra_op_num_threads = 2
t = time.perf_counter()
gpu_sess = ort.InferenceSession(MODEL, sess_options=opts)
print(f"webgpu session created in {time.perf_counter() - t:.1f}s", flush=True)

imgs = [f for it in FILES for f in frames(it)]
print(f"{len(imgs)} frames from {len(FILES)} files", flush=True)

cpu = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
gpu = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
gpu.text_det.infer = SessionInfer(gpu_sess)

counts, maps = {}, {}
for name, det in (("onnxruntime CPU 2 threads", cpu.text_det), ("onnxruntime WebGPU", gpu.text_det)):
    for im in imgs[:5]:
        det(im)
    t = time.perf_counter()
    c = []
    for im in imgs:
        b, _ = det(im)
        c.append(0 if b is None else len(b))
    counts[name] = np.array(c)
    print(f"{name:28} {1000 * (time.perf_counter() - t) / len(imgs):6.1f} ms/frame", flush=True)

ref = counts["onnxruntime CPU 2 threads"]
c = counts["onnxruntime WebGPU"]
print(
    f"box count identical {np.mean(c == ref) * 100:.1f}%  |diff|<=1 {np.mean(abs(c - ref) <= 1) * 100:.1f}%  "
    f">=1 box agree {np.mean((c >= 1) == (ref >= 1)) * 100:.1f}%  >=3 agree {np.mean((c >= 3) == (ref >= 3)) * 100:.1f}%  "
    f"frames with text (cpu) {int((ref >= 1).sum())}"
)
pre = cpu.text_det.get_preprocess(320)(imgs[len(imgs) // 2])
a = cpu.text_det.infer(pre)[0]
b = gpu_sess.run(None, {gpu_sess.get_inputs()[0].name: pre})[0]
print(f"probability map max abs diff {np.abs(a - b).max():.5f}")
