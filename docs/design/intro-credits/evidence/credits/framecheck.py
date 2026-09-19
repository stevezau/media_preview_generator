"""Contact sheet of frames around chapter truth and our detection, to judge who is right.

usage: framecheck.py <file> <truth_s> <detected_s> <out.jpg>
Row 1: truth-10 .. truth+20 ; Row 2: detected-10 .. detected+20 (every 5 s), labelled.
"""

import subprocess
import sys

from PIL import Image, ImageDraw

W, H = 320, 180
path, truth, det, out = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), sys.argv[4]


def grab(t: float) -> Image.Image:
    r = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "2",
            "-ss",
            f"{max(0, t):.2f}",
            "-i",
            path,
            "-frames:v",
            "1",
            "-vf",
            f"scale={W}:{H}",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        capture_output=True,
    )
    if len(r.stdout) != W * H * 3:
        return Image.new("RGB", (W, H), "gray")
    return Image.frombytes("RGB", (W, H), r.stdout)


offsets = [-10, -5, 0, 5, 10, 20]
sheet = Image.new("RGB", (W * len(offsets), (H + 16) * 2), "black")
draw = ImageDraw.Draw(sheet)
for row, (name, base) in enumerate((("truth", truth), ("ours", det))):
    for col, off in enumerate(offsets):
        sheet.paste(grab(base + off), (col * W, row * (H + 16) + 16))
        draw.text((col * W + 4, row * (H + 16) + 2), f"{name} {off:+d}s ({base + off:.0f})", fill="yellow")
sheet.save(out, quality=80)
