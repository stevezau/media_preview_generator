"""Read-only frame grabs from the storage copy of /data, tiled into one sheet per case with time labels."""

import os
import subprocess
import sys

from PIL import Image, ImageDraw

OUT = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh/frames"
os.makedirs(OUT, exist_ok=True)
CASES = {
    "physical100": (
        "/data_16tb2/TV Shows/Physical 100 Mexico (2026) {tvdb-481514}/Season 01/Physical 100 Mexico (2026) - S01E01 - The Calling of the 100 [NF][WEBDL-1080p][EAC3 5.1][x264].mkv",
        [3470, 3484, 3490, 3510, 3540, 3570, 3582, 3590],
    ),
    "somebody": (
        "/data_16tb2/TV Shows/Somebody Somewhere (2022) {tvdb-385730}/Season 03/Somebody Somewhere (2022) - S03E02 - Dinky Dinkies [HMAX MULTi][WEBDL-1080p][EAC3 5.1][h265]-FUZEER.mkv",
        [1640, 1648, 1655, 1665, 1680, 1695, 1702, 1712],
    ),
    "accused709": (
        "/data_16tb2/TV Shows/Accused Guilty or Innocent (2020) {tvdb-379555}/Season 07/Accused Guilty or Innocent (2020) - S07E09 - Deadly Ex-marine Or Family Defender [AMZN][WEBDL-1080p][EAC3 2.0][h264]-playWEB.mkv",
        [2330, 2338, 2342, 2350, 2450, 2510, 2520, 2540],
    ),
}
which = sys.argv[1:] or list(CASES)
for name in which:
    path, times = CASES[name]
    tiles = []
    for t in times:
        f = f"{OUT}/{name}_{t}.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(t),
                "-i",
                path,
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                f,
            ],
            check=True,
        )
        im = Image.open(f).convert("RGB")
        ImageDraw.Draw(im).rectangle([0, 0, 90, 22], fill="black")
        ImageDraw.Draw(im).text((4, 4), f"{t}s", fill="yellow")
        tiles.append(im)
    w, h = tiles[0].size
    sheet = Image.new("RGB", (w * 4, h * ((len(tiles) + 3) // 4)))
    for i, im in enumerate(tiles):
        sheet.paste(im, ((i % 4) * w, (i // 4) * h))
    sheet.save(f"{OUT}/{name}_sheet.jpg", quality=80)
    print(f"{OUT}/{name}_sheet.jpg")
