"""Throwaway experiment: movie credits start via text-density on sampled frames.

Usage: python credits_ocr.py <file> [<file>...]   (prints JSON lines)
"""

import json
import re
import subprocess
import sys
import time

import numpy as np
from rapidocr_onnxruntime import RapidOCR

WINDOW_S = 900
FPS = 0.5
W, H = 640, 360
MIN_BOXES = 3
MAX_GAP_S = 20
MIN_RUN_S = 30

ocr = RapidOCR()


def duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out)


def frames(path: str, start: float):
    """Decode keyframes only; timestamps parsed from showinfo on stderr."""
    import threading
    cmd = ["ffmpeg", "-v", "info", "-hide_banner", "-skip_frame", "nokey", "-ss", f"{start:.2f}", "-i", path, "-an", "-sn",
           "-vf", f"scale={W}:{H},format=gray,showinfo", "-fps_mode", "vfr", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pts: list[float] = []
    def reader():
        for line in p.stderr:
            m = re.search(rb"pts_time:([0-9.]+)", line)
            if m:
                pts.append(float(m.group(1)))
    th = threading.Thread(target=reader, daemon=True); th.start()
    i = 0
    while True:
        buf = p.stdout.read(W * H)
        if len(buf) < W * H:
            break
        while len(pts) <= i and th.is_alive():
            time.sleep(0.005)
        ts = start + (pts[i] if i < len(pts) else i * 4.0)
        yield ts, np.frombuffer(buf, dtype=np.uint8).reshape(H, W)
        i += 1
    p.wait()


def analyse(path: str) -> dict:
    t0 = time.time()
    dur = duration(path)
    start = max(0.0, dur - WINDOW_S)
    rows = []
    for ts, g in frames(path, start):
        boxes, _ = ocr.text_det(np.stack([g] * 3, axis=-1))
        n = 0 if boxes is None else len(boxes)
        rows.append((ts, n, float(g.mean())))
    # Walk runs of text frames allowing gaps; take the LAST run of sufficient length that
    # starts after 70% of the file (credits are at the end; post-credit scenes allowed via gaps).
    json.dump(rows, open("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/detect/rows_" + str(round(dur)) + ".json", "w"))
    text_ts = [ts for ts, n, _ in rows if n >= MIN_BOXES]
    runs = []
    for ts in text_ts:
        if runs and ts - runs[-1][1] <= MAX_GAP_S:
            runs[-1][1] = ts
        else:
            runs.append([ts, ts])
    runs = [r for r in runs if r[1] - r[0] >= MIN_RUN_S]
    credits = None
    if runs:
        best = max(runs, key=lambda r: r[1] - r[0])
        s = best[0]
        # snap back to a dark frame within 10 s before the first text frame
        dark = [ts for ts, n, luma in rows if s - 10 <= ts < s and luma < 20]
        credits = round(dark[0] if dark else s, 1)
    return {
        "file": path.split("/")[-1][:60],
        "duration": round(dur),
        "credits_start": credits,
        "runs": [[round(a), round(b)] for a, b in runs],
        "secs": round(time.time() - t0, 1),
    }


if __name__ == "__main__":
    for f in sys.argv[1:]:
        print(json.dumps(analyse(f)), flush=True)
