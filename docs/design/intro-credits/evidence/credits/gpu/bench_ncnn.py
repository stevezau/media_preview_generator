"""Vendor-neutral GPU text detection check: ncnn (Vulkan) vs ONNX Runtime CPU, same PP-OCRv4 det model.

Frames: keyframes (320x180 luma → 3 channels) from the credits tails of a few eval files.
Same RapidOCR pre/post-processing for every engine; only the network call is swapped.
Reports ms/frame and per-frame box-count agreement vs ONNX Runtime CPU.
"""

import json
import subprocess
import sys
import time

import ncnn
import numpy as np
from rapidocr_onnxruntime import RapidOCR

W, H = 320, 180
MODEL = "ncnnmodel/ch_PP_OCRv4_det_infer.ncnn"
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


class NcnnInfer:
    def __init__(self, gpu: int | None, threads: int = 2):
        self.net = ncnn.Net()
        self.net.opt.use_vulkan_compute = gpu is not None
        if gpu is not None:
            self.net.set_vulkan_device(gpu)
        self.net.opt.num_threads = threads
        self.net.load_param(MODEL + ".param")
        self.net.load_model(MODEL + ".bin")

    def __call__(self, x: np.ndarray):
        ex = self.net.create_extractor()
        ex.input("in0", ncnn.Mat(np.ascontiguousarray(x[0])))
        _, out = ex.extract("out0")
        return [np.array(out)[None, ...]]


imgs = [f for it in FILES for f in frames(it)]
print(
    f"{len(imgs)} frames from {len(FILES)} files; vulkan devices:",
    [ncnn.get_gpu_info(i).device_name() for i in range(ncnn.get_gpu_count())],
    flush=True,
)

engines = {}
ort = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
engines["onnxruntime CPU 2 threads"] = ort.text_det
for name, gpu in (("ncnn Vulkan GPU0", 0), ("ncnn CPU 2 threads", None)):
    o = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
    o.text_det.infer = NcnnInfer(gpu)
    engines[name] = o.text_det

counts = {}
for name, det in engines.items():
    for im in imgs[:3]:
        det(im)
    t = time.perf_counter()
    c = []
    for im in imgs:
        b, _ = det(im)
        c.append(0 if b is None else len(b))
    counts[name] = c
    print(f"{name:28} {1000 * (time.perf_counter() - t) / len(imgs):6.1f} ms/frame", flush=True)

ref = np.array(counts["onnxruntime CPU 2 threads"])
for name, c in counts.items():
    c = np.array(c)
    print(
        f"{name:28} same box count {np.mean(c == ref) * 100:5.1f}%  |diff|<=1 {np.mean(abs(c - ref) <= 1) * 100:5.1f}%"
        f"  credit-frame flag (>=1 box) agree {np.mean((c >= 1) == (ref >= 1)) * 100:5.1f}%"
        f"  (>=3 boxes) agree {np.mean((c >= 3) == (ref >= 3)) * 100:5.1f}%"
    )
