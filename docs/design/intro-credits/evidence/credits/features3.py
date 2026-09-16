"""Credit features, GPU end to end, one file at a time (storage is shared — keep CPU use to ~1-2 cores).

Per file:
  keyframes : every keyframe in the tail window (NVDEC, -skip_frame nokey), true pts from showinfo.
              Preview frames are made the same way (keyframe-only), so any preview interval can be simulated.
  fine      : full decode at 1 fps over [truth-60 s, truth+40 s] — stands in for the short refinement decode.
Text detection: RapidOCR detector on CUDA at the frame's own 320 px size.
Rows: [pts, boxes, area_frac, luma_mean, luma_std]
"""

import glob
import json
import re
import subprocess
import sys
import time

import numpy as np
import onnxruntime as ort
from rapidocr_onnxruntime import RapidOCR

ort.preload_dlls()
W, H = 320, 180
FRAME = W * H * 3 // 2
ocr = RapidOCR(
    det_limit_side_len=320, det_limit_type="max", det_use_cuda=True, intra_op_num_threads=1, inter_op_num_threads=1
)
PTS = re.compile(rb"pts_time:\s*(-?[\d.]+)")


def decode(path: str, start: float, length: float | None, keyframes_only: bool, fps: float | None) -> list:
    vf = f"scale_cuda={W}:{H}:format=nv12,hwdownload,format=nv12,showinfo"
    if fps:
        vf = f"fps={fps}," + vf
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-threads", "2", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if keyframes_only:
        cmd += ["-skip_frame", "nokey"]
    cmd += ["-ss", f"{start:.3f}"]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    cmd += ["-copyts", "-i", path, "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-vf", vf, "-f", "rawvideo", "-"]
    p = subprocess.run(cmd, capture_output=True)
    pts = [float(m) for m in PTS.findall(p.stderr)]
    n = min(len(p.stdout) // FRAME, len(pts))
    rows = []
    for i in range(n):
        g = np.frombuffer(p.stdout, dtype=np.uint8, count=W * H, offset=i * FRAME).reshape(H, W)
        b, _ = ocr.text_det(np.stack([g] * 3, axis=-1))
        boxes = 0 if b is None else len(b)
        area = 0.0
        if b is not None:
            for bx in np.array(b, dtype=float):
                area += 0.5 * abs(np.dot(bx[:, 0], np.roll(bx[:, 1], 1)) - np.dot(bx[:, 1], np.roll(bx[:, 0], 1)))
        rows.append(
            [round(pts[i], 3), boxes, round(area / (W * H), 4), round(float(g.mean()), 1), round(float(g.std()), 1)]
        )
    return rows


kind, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
done = {json.loads(line)["file"] for line in open(dst)} if glob.glob(dst) else set()
with open(dst, "a") as out:
    for it in json.load(open(src)):
        if it["file"] in done:
            continue
        t0 = time.time()
        start = max(0.0, it["duration"] - it["window"])
        key = decode(it["file"], start, None, True, None)
        fine_start = max(0.0, it["credits_start"] - 60)
        fine = decode(it["file"], fine_start, 100, False, 1)
        rec = {
            "kind": kind,
            "file": it["file"],
            "duration": it["duration"],
            "truth": it["credits_start"],
            "window_start": start,
            "key": key,
            "fine": fine,
        }
        out.write(json.dumps(rec) + "\n")
        out.flush()
        print(kind, round(time.time() - t0), len(key), len(fine), it["file"].split("/")[-1][:50], flush=True)
