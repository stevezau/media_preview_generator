"""Frames of Star Trek SNW S04E10 and S04E08 at a few instants (read-only on /data), tiled into one image per file."""

import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = {
    "e10": "/data_16tb2/TV Shows/Star Trek Strange New Worlds (2022) {tvdb-382389}/Season 04/Star Trek Strange New Worlds (2022) - S04E10 - Tomorrows Enterprise [WEBDL-2160p][EAC3 5.1][h265]-CAKES.mkv",
    "e08": "/data_16tb2/TV Shows/Star Trek Strange New Worlds (2022) {tvdb-382389}/Season 04/Star Trek Strange New Worlds (2022) - S04E08 - Orders of Magnitude [WEBDL-2160p][EAC3 5.1][h265]-playWEB.mkv",
}
TIMES = (1, 5, 10, 15, 20, 26, 30, 35)
for name, path in FILES.items():
    frames = []
    for t in TIMES:
        out = os.path.join(HERE, f"snw_{name}_{t:02d}.jpg")
        subprocess.run(
            ["nice", "-n", "19", "/usr/bin/ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", path, "-frames:v", "1",
             "-vf", "scale=480:-2", out],
            check=True,
        )  # fmt: skip
        frames.append(out)
    tiled = os.path.join(HERE, f"snw_{name}_tiles.jpg")
    inputs = []
    for f in frames:
        inputs += ["-i", f]
    subprocess.run(
        ["/usr/bin/ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex",
         f"xstack=inputs={len(frames)}:layout=0_0|w0_0|w0+w1_0|w0+w1+w2_0|0_h0|w0_h0|w0+w1_h0|w0+w1+w2_h0", tiled],
        check=True,
    )  # fmt: skip
    print(tiled)
