"""Contact sheet of a video's frames over one or more windows, each frame labelled with its time (read-only on the
video; frames go to a temp dir in the scratchpad).

Usage: sheet.py OUT.jpg VIDEO "label:start:end:step" ["label:start:end:step" ...]
Times in seconds. Runs ffmpeg under nice 19 with 2 threads, one window at a time.
"""

import os
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

W, H, COLS = 256, 144, 8


def mmss(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m)}:{s:04.1f}"


def window(video: str, start: float, end: float, step: float, tmp: str) -> list[tuple[float, Image.Image]]:
    start = max(0.0, start)
    pattern = os.path.join(tmp, f"w{start:.1f}_%04d.jpg")
    cmd = [
        "nice",
        "-n",
        "19",
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-threads",
        "2",
        "-ss",
        f"{start:.3f}",
        "-i",
        video,
        "-t",
        f"{end - start:.3f}",
        "-map",
        "0:v:0",
        "-vf",
        f"fps=1/{step},scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2",
        "-q:v",
        "5",
        pattern,
    ]
    subprocess.run(cmd, check=True, timeout=900)
    frames = sorted(f for f in os.listdir(tmp) if f.startswith(f"w{start:.1f}_"))
    return [(start + i * step, Image.open(os.path.join(tmp, f)).convert("RGB")) for i, f in enumerate(frames)]


def main() -> None:
    out, video, specs = sys.argv[1], sys.argv[2], sys.argv[3:]
    font = ImageFont.load_default()
    rows: list[tuple[str, list[tuple[float, Image.Image]]]] = []
    with tempfile.TemporaryDirectory(dir=os.path.dirname(out)) as tmp:
        for spec in specs:
            label, a, b, s = spec.rsplit(":", 3)
            rows.append((label, window(video, float(a), float(b), float(s), tmp)))
        height = 0
        for label, frames in rows:
            height += 16 + ((len(frames) + COLS - 1) // COLS) * (H + 14)
        sheet = Image.new("RGB", (COLS * W, height), (30, 30, 30))
        draw = ImageDraw.Draw(sheet)
        y = 0
        for label, frames in rows:
            draw.text((4, y + 2), label, fill=(255, 255, 0), font=font)
            y += 16
            for i, (t, im) in enumerate(frames):
                x, yy = (i % COLS) * W, y + (i // COLS) * (H + 14)
                sheet.paste(im, (x, yy))
                draw.text((x + 3, yy + H), f"{t:.1f}s  {mmss(t)}", fill=(255, 255, 255), font=font)
            y += ((len(frames) + COLS - 1) // COLS) * (H + 14)
        sheet.save(out, quality=80)


if __name__ == "__main__":
    main()
