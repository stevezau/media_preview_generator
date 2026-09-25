"""Tomb Raider King: the picture offset between E12 and its partners at the audio alignment (read-only on /data).

Decodes 78-90 s of each file at 12 fps (64x36 grey, the end-picture check's frames) and finds the shift of the
partner's picture that best matches E12's over the OP's last 12 s (mean correlation of non-flat frame pairs).
"""

import subprocess
import sys

import numpy as np

D = "/data_16tb/TV Shows/Tomb Raider King (2026) {tvdb-452039}/Season 01/"
D3 = "/data_16tb3/TV Shows/Tomb Raider King (2026) {tvdb-452039}/Season 01/"
FILES = {
    "E12": D + "Tomb Raider King (2026) - S01E12 - TBA [WEBRip-1080p][AAC 2.0][x265].mkv",
    "E02": D
    + "Tomb Raider King (2026) - S01E02 - Those Who Seek to Own Relics [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv",
    "E03": D
    + "Tomb Raider King (2026) - S01E03 - One Suited for Domination [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv",
    "E05": D
    + "Tomb Raider King (2026) - S01E05 - The Owner of the Tree of Life [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv",
}
# Audio offsets (partner time - E12 time) from the season matcher's hits.
OFFSETS = {"E02": -0.37, "E03": -0.372, "E05": -0.248}
FPS = 12
LO, LEN = 76.0, 14.0


def grey(path, start):
    cmd = [
        "nice",
        "-n",
        "19",
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-threads",
        "2",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{LEN}",
        "-i",
        path,
        "-an",
        "-vf",
        f"fps={FPS},scale=64:36,format=gray",
        "-f",
        "rawvideo",
        "-",
    ]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 36, 64).astype(np.float32)


def corr(x, y):
    if x.std() < 4 or y.std() < 4:
        return np.nan
    zx, zy = (x - x.mean()).ravel(), (y - y.mean()).ravel()
    return float(zx @ zy / (np.linalg.norm(zx) * np.linalg.norm(zy)))


a = grey(FILES["E12"], LO)
for name in sys.argv[1:] or ["E02", "E03", "E05"]:
    b = grey(FILES[name], LO + OFFSETS[name])
    n = min(len(a), len(b))
    best = []
    for k in range(-12, 13):  # partner frame index shift, 1/12 s each
        vals = [corr(a[i], b[i + k]) for i in range(12, n - 12)]
        best.append((np.nanmean(vals), k / FPS))
    best.sort(reverse=True)
    print(
        name,
        "best picture shifts (mean corr, partner picture later by s):",
        [(round(c, 3), round(s, 3)) for c, s in best[:4]],
        "at 0:",
        [round(c, 3) for c, s in best if s == 0.0],
    )
