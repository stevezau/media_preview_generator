"""Scratch: a contact sheet of frames every STEP seconds over a range (read-only on media; sheets go to ./sheets).

Usage: strip.py <path-substring-or-key> <from_s> <to_s> <step_s> <out name> [search root]
The file is found by substring under the TV/Movies roots, or by key in targets.json.
"""

import glob
import io
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
W = 256
who, t0, t1, step, name = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
targets = json.load(open(os.path.join(HERE, "targets.json")))
if who.startswith("ct:"):
    path = next(p for p in json.load(open(os.path.join(HERE, "ct", "ct_base.json"))) if who[3:] in p)
elif who.startswith("/"):
    path = who
elif who in targets:
    path = targets[who][0]["path"]
else:
    found = [p for root in ("/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb") for p in
             glob.glob(f"{root}/TV Shows/*/*/*{who}*") + glob.glob(f"{root}/Movies/*/*{who}*")]  # fmt: skip
    path = sorted(found)[0]
print(path)


def frame(t: float) -> Image.Image:
    cmd = ["nice", "-n", "19", "/usr/bin/ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={W}:-2", "-f", "image2pipe", "-vcodec", "png", "-"]  # fmt: skip
    out = subprocess.run(cmd, capture_output=True, timeout=120).stdout
    return Image.open(io.BytesIO(out)).convert("RGB") if out else Image.new("RGB", (W, 144), (80, 0, 0))


times = []
t = t0
while t <= t1 + 1e-6:
    times.append(t)
    t += step
tiles = [frame(t) for t in times]
cols = 6
h = max(im.height for im in tiles)
img = Image.new("RGB", (W * cols, (h + 2) * ((len(tiles) + cols - 1) // cols)), (255, 255, 255))
draw = ImageDraw.Draw(img)
for i, (t, im) in enumerate(zip(times, tiles, strict=True)):
    x, y = (i % cols) * W, (i // cols) * (h + 2)
    img.paste(im, (x, y))
    draw.rectangle((x, y, x + 58, y + 12), fill=(0, 0, 0))
    draw.text((x + 2, y + 1), f"{t:.1f}", fill=(255, 255, 0))
os.makedirs(os.path.join(HERE, "sheets"), exist_ok=True)
img.save(os.path.join(HERE, "sheets", name), quality=70)
