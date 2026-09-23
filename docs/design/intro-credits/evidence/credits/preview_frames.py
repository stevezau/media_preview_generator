"""Credit-text rows from frames made exactly like the app's preview frames.

Mirrors processing/generator.py + ffmpeg_runner.py: `-skip_frame:v nokey`, `fps=fps=1/N:round=up`,
`scale=w=320:h=240:force_original_aspect_ratio=decrease`, JPEG `-q:v 4` (user's and default quality).
Only the tail window is decoded (input seek) to keep the test affordable.
Output JSONL: {"file", "interval", "rows": [[t, boxes, area, luma, std], ...]}
"""

import glob
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR(det_limit_side_len=320, det_limit_type="max")


def preview_rows(path: str, start: float, interval: int) -> list:
    with tempfile.TemporaryDirectory(dir="/tmp/claude-1000") as d:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-skip_frame:v",
            "nokey",
            "-ss",
            f"{start:.2f}",
            "-i",
            path,
            "-an",
            "-sn",
            "-dn",
            "-q:v",
            "4",
            "-vf",
            f"fps=fps={round(1 / interval, 6)}:round=up,scale=w=320:h=240:force_original_aspect_ratio=decrease",
            f"{d}/img-%06d.jpg",
        ]
        subprocess.run(cmd, check=False)
        rows = []
        for i, jpg in enumerate(sorted(Path(d).glob("img-*.jpg"))):
            img = np.array(Image.open(jpg).convert("RGB"))
            g = img.mean(axis=2)
            b, _ = ocr.text_det(img)
            n = 0 if b is None else len(b)
            area = 0.0
            if b is not None:
                for bx in np.array(b, dtype=float):
                    area += 0.5 * abs(np.dot(bx[:, 0], np.roll(bx[:, 1], 1)) - np.dot(bx[:, 1], np.roll(bx[:, 0], 1)))
            rows.append(
                [
                    round(start + i * interval, 2),
                    n,
                    round(area / g.size, 4),
                    round(float(g.mean()), 1),
                    round(float(g.std()), 1),
                ]
            )
        return rows


items = {}
for f in glob.glob("credits/features*.jsonl"):
    for line in open(f):
        it = json.loads(line)
        items[it["file"]] = it
windows = {
    it["file"]: it["window"] for src in ("credits/movies40.json", "credits/tv40.json") for it in json.load(open(src))
}
interval, limit, out_path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
done = {json.loads(l)["file"] for l in open(out_path)} if Path(out_path).exists() else set()
with open(out_path, "a") as out:
    for file, it in sorted(items.items())[:limit]:
        if file in done:
            continue
        rows = preview_rows(file, max(0.0, it["duration"] - windows[file]), interval)
        out.write(json.dumps({"file": file, "interval": interval, "rows": rows}) + "\n")
        out.flush()
        print(interval, len(rows), file.split("/")[-1][:50], flush=True)
