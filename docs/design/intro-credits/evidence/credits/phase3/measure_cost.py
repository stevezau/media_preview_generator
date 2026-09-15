"""M4: seconds per file for the spec §5.4 frames + text detection, three ways (prototype code paths, no app code): the
keyframe tail, then the two 1 fps windows the detector adds (21 s before the credits start, and 21 s after the roll when
a scene follows it, Q3; timed here around the truth start for every file). The second window starts at the truth start
+ 60 s, clamped to 21 s before the end of the file (the rows recorded on 2026-09-16 predate the clamp). Each row records
the detection session's first provider and ffmpeg's return codes (tail, first window, second window).

    measure_cost.py <model.onnx> <n movies> <n episodes>     (run with the Python 3.12 venv from M1)
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import onnxruntime_ep_webgpu as webgpu_ep
from rapidocr_onnxruntime import RapidOCR

EVIDENCE = Path(__file__).resolve().parents[2]
W, H = 320, 180
PTS = re.compile(rb"pts_time:\s*(-?[\d.]+)")


def decode(path: str, start: float, gpu: bool, length: float | None = None) -> tuple[list[np.ndarray], int]:
    vf = f"scale_cuda={W}:{H}:format=nv12,hwdownload,format=nv12" if gpu else f"scale={W}:{H},format=nv12"
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-threads", "2"]
    if gpu:
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    if length is None:
        cmd += ["-skip_frame", "nokey", "-ss", f"{start:.3f}"]
    else:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}"]
        vf = f"fps=1,{vf}"
    cmd += ["-copyts", "-i", path, "-an", "-sn", "-dn",
            "-fps_mode", "passthrough", "-vf", f"{vf},showinfo", "-f", "rawvideo", "-"]
    result = subprocess.run(cmd, capture_output=True)
    out = result.stdout
    size = W * H * 3 // 2
    frames = [np.frombuffer(out, np.uint8, W * H, i * size).reshape(H, W) for i in range(len(out) // size)]
    return frames, result.returncode


def detector(model: str, webgpu: bool):
    ocr = RapidOCR(det_limit_side_len=320, det_limit_type="max", intra_op_num_threads=2, inter_op_num_threads=1)
    if webgpu:
        ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
        devices = [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.add_provider_for_devices(devices[:1], {})
        session = ort.InferenceSession(model, sess_options=opts)
        name = session.get_inputs()[0].name
        ocr.text_det.infer = lambda x: session.run(None, {name: x})
        return ocr.text_det, session.get_providers()[0]
    return ocr.text_det, ocr.text_det.infer.session.get_providers()[0]


def main() -> None:
    model, movies, episodes = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    files = [(r, 900.0) for r in json.loads((EVIDENCE / "credits/movies40.json").read_text())[:movies]]
    files += [(r, 450.0) for r in json.loads((EVIDENCE / "credits/tv40.json").read_text())[:episodes]]
    mode = sys.argv[4] if len(sys.argv) > 4 else "cpu"
    det, provider = detector(model, webgpu=mode == "gpu")
    for n, (row, tail) in enumerate(files, 1):
        started = time.perf_counter()
        frames, tail_code = decode(row["file"], max(0.0, row["duration"] - tail), gpu=mode == "gpu")
        decoded = time.perf_counter()
        for frame in frames:
            det(np.stack([frame] * 3, axis=-1))
        done = time.perf_counter()
        refine, codes = [], [tail_code]
        for window_start in (row["credits_start"] - 20, min(row["credits_start"] + 60, row["duration"] - 21)):
            window, code = decode(row["file"], max(0.0, window_start), gpu=mode == "gpu", length=21.0)
            refine += window
            codes.append(code)
        for frame in refine:
            det(np.stack([frame] * 3, axis=-1))
        refined = time.perf_counter()
        kind = "movie" if tail == 900.0 else "episode"
        print(json.dumps({"n": n, "kind": kind, "mode": mode, "provider": provider, "ffmpeg_codes": codes,
                          "frames": len(frames), "refine_frames": len(refine),
                          "decode_s": round(decoded - started, 1), "detect_s": round(done - decoded, 1),
                          "refine_s": round(refined - done, 1), "total_s": round(refined - started, 1)}), flush=True)


if __name__ == "__main__":
    main()
