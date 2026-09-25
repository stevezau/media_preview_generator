"""Does a GPU's decode change a credits answer? The app's credits detection on each file, decoded on the CPU and on a
GPU (spec §5.4, the GPU decode check; §14 2026-09-25 "compared with the CPU's, as a diagnostic").

Each file runs ``detector.find_credits`` twice as the app runs it: decoding on the CPU, then on the GPU. Text
detection is one in-process CPU ONNX session for both, so the decoder is the only difference. Per file: the answer
(start, end, scale) per decoder, how many read frames' text boxes differ, and how the luma planes the detector saw
differ. Reads the files only.

Measured on storage's Quadro P5000 (ffmpeg 8.0.1, 2026-09-25), 15 files from the library (7 MPEG-2: Tom Dowd and the
Language of Music and 6 Ren & Stimpy DVD episodes; 8 MPEG-4 Part 2 XviD/DivX: Peter Kay Live at the Top of the
Tower, Paragraph 175, The Untold Story of Emmett Louis Till, Afghan Star, and one episode each of Alias Smith and
Jones, Outnumbered, Top Gear and Ren & Stimpy): the same answer on all 15 (12 with credits, 3 without). Nearly every
frame differed: MPEG-2 by at most 3 levels on 1.7-3.1% of luma pixels, MPEG-4 by at most 12 on 1.4-9.0%. Text boxes
differed on 210 of 7,934 MPEG-2 frames read and 53 of 1,505 MPEG-4 ones. The file list is local-only.

Usage: MEDIA_PREVIEW_TEXTDET_MODEL=bench/textdet-model/ch_PP-OCRv4_det_infer.onnx \\
       python decode_answers_gpu_vs_cpu.py FILES.tsv OUT.jsonl [--gpu NVIDIA] [--device cuda:0] [--ffmpeg ffmpeg]
FILES.tsv: one file per line, ``<codec label> <tab> episode|movie <tab> <path>``.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import numpy as np

from media_preview_generator.markers.credits import detector, textdet
from media_preview_generator.markers.decide import credits_limits_ms, earliest_credits_start_ms
from media_preview_generator.markers.probe import probe_media


class Capture:
    """The detector's text boxes on the CPU, keeping every plane it was handed."""

    def __init__(self, det: textdet.TextDetector) -> None:
        self.det = det
        self.planes: list[np.ndarray] = []

    def __call__(self, planes: np.ndarray):
        self.planes.append(planes.copy())
        return self.det.detect(planes)


def rows(result) -> list[tuple[float, tuple]]:
    return [
        (round(row[0], 3), tuple(tuple(box) for box in row[3]))
        for row in (*result.key_rows, *result.fine_rows, *result.end_rows)
    ]


def run(args, det, path: str, episode: bool, gpu: str | None, device: str | None, dur_ms: int, earliest: float):
    capture = Capture(det)
    started = time.monotonic()
    try:
        result = detector.find_credits(path, duration_ms=dur_ms, is_episode=episode, ffmpeg=args.ffmpeg,
                                       detect_boxes=capture, gpu=gpu, gpu_device_path=device,
                                       earliest_start_s=earliest)  # fmt: skip
        out = {"start_s": result.start_s, "end_s": result.end_s, "scale": result.scale, "rows": rows(result)}
        out["error"] = None
    except Exception as exc:  # recorded, not hidden
        out = {"start_s": None, "end_s": None, "scale": None, "rows": [], "error": f"{type(exc).__name__}: {exc}"}
    out["secs"] = round(time.monotonic() - started, 1)
    return out, capture.planes


def plane_difference(cpu: list[np.ndarray], gpu: list[np.ndarray]) -> dict:
    if [p.shape for p in cpu] != [p.shape for p in gpu]:
        return {"same_shapes": False, "cpu_calls": len(cpu), "gpu_calls": len(gpu)}
    frames = differing = pixels = total = level = 0
    for x, y in zip(cpu, gpu):
        d = np.abs(x.astype(np.int16) - y.astype(np.int16))
        frames += d.shape[0]
        differing += int((d.reshape(d.shape[0], -1).max(axis=1) > 0).sum())
        pixels += int((d > 0).sum())
        total += d.size
        level = max(level, int(d.max()))
    return {
        "same_shapes": True,
        "frames": frames,
        "frames_differing": differing,
        "pixels_differing_pct": round(100.0 * pixels / max(total, 1), 3),
        "max_level_diff": level,
    }


def rows_differing(cpu: list, gpu: list) -> tuple[int, int]:
    x, y = dict(cpu), dict(gpu)
    times = set(x) | set(y)
    return sum(1 for t in times if x.get(t) != y.get(t)), len(times)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", help="the TSV file list")
    parser.add_argument("out", help="JSON lines, appended")
    parser.add_argument("--gpu", default="NVIDIA")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    det = textdet.TextDetector(textdet.cpu_session(os.environ["MEDIA_PREVIEW_TEXTDET_MODEL"], 4), backend="cpu")
    with open(args.files) as listing:
        files = [line.rstrip("\n").split("\t") for line in listing if line.strip()]
    with open(args.out, "a") as out:
        for label, what, path in files:
            episode = what == "episode"
            dur_ms = probe_media(path, ffprobe=args.ffprobe, timeout_s=60).duration_ms
            window_ms, cap_ms = credits_limits_ms(is_episode=episode, tv_window_s=None, movie_window_s=None)
            earliest = earliest_credits_start_ms(dur_ms, is_movie=not episode, credits_window_ms=window_ms,
                                                 movie_credits_max_from_end_ms=cap_ms) / 1000.0  # fmt: skip
            cpu, cpu_planes = run(args, det, path, episode, None, None, dur_ms, earliest)
            gpu, gpu_planes = run(args, det, path, episode, args.gpu, args.device, dur_ms, earliest)
            answer = ("start_s", "end_s", "scale")
            record = {
                "label": label,
                "path": path,
                "cpu": {k: v for k, v in cpu.items() if k != "rows"},
                "gpu": {k: v for k, v in gpu.items() if k != "rows"},
                "same_answer": all(cpu[k] == gpu[k] for k in answer),
                "rows_differing": rows_differing(cpu["rows"], gpu["rows"]),
                "planes": plane_difference(cpu_planes, gpu_planes),
            }
            out.write(json.dumps(record) + "\n")
            out.flush()
            print(label, record["same_answer"], record["rows_differing"], record["planes"], os.path.basename(path),
                  flush=True)  # fmt: skip


if __name__ == "__main__":
    main()
