"""Scratch (runs in a throwaway container on plex, code mounted at /check): the text helper on the Intel iGPU and on
the NVIDIA GPU, reading card-sized frames and detecting boxes, against the CPU. Read-only; nothing is written.
"""

import json
import os

import cv2
import numpy as np

from media_preview_generator.markers.credits import textdet, textdet_helper as th

LINES = [
    ["The investigation is now closed."],
    ["Robert Hanssen is now serving a life sentence in", "the Supermax Federal Penitentiary in Florence,"],
    ["directed by", "Billy Ray"],
    ["EXECUTIVE PRODUCERS", "MALCOLM BRINKWORTH", "XANDER BRINKWORTH"],
]
planes = np.full((len(LINES), 720, 1280), 16, np.uint8)
for plane, lines in zip(planes, LINES, strict=True):
    for i, words in enumerate(lines):
        cv2.putText(plane, words, (120, 300 + 60 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.4, 235, 3, cv2.LINE_AA)
frames = textdet.synthetic_frames(20)
pool = th.TextDetectorPool()
out = {}
try:
    out["cpu_read"] = pool.read_text(planes, gpu=None, gpu_device_path=None)
    out["cpu_boxes"] = pool.detect_boxes(frames, gpu=None, gpu_device_path=None)
    for gpu, device in (("INTEL", "/dev/dri/renderD128"), ("NVIDIA", "cuda:0")):
        fell_back = []
        read = pool.read_text(planes, gpu=gpu, gpu_device_path=device, gpu_worker=True, on_cpu=fell_back.append)
        boxes = pool.detect_boxes(frames, gpu=gpu, gpu_device_path=device, gpu_worker=True, on_cpu=fell_back.append)
        out[gpu] = {
            "backend": pool.backend_of(gpu, device),
            "reads_as_cpu": read == out["cpu_read"],
            "boxes_as_cpu": boxes == out["cpu_boxes"],
            "cpu_fallback": fell_back,
            "read": read,
        }
finally:
    pool.close_all()
out["env_rec_model"] = os.environ.get(th.REC_MODEL_ENV)
print(json.dumps({k: v for k, v in out.items() if k != "cpu_boxes"}, indent=1, default=str))
