"""Per-instant end-picture verdicts for TRK E12 against E03/E05 at picture shifts -1..+1 s (CPU decode, 2 fps, the
app's frames) - why the half-second sync search alone doesn't pass it."""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ep_sync_sets as V  # noqa: E402

EP = V.EP
reader = EP.Reader(ffmpeg="/usr/bin/ffmpeg")
D = "/data_16tb/TV Shows/Tomb Raider King (2026) {tvdb-452039}/Season 01/"
E12 = D + "Tomb Raider King (2026) - S01E12 - TBA [WEBRip-1080p][AAC 2.0][x265].mkv"
E03 = D + "Tomb Raider King (2026) - S01E03 - One Suited for Domination [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
E05 = (
    D + "Tomb Raider King (2026) - S01E05 - The Owner of the Tree of Life [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
)
start, end = 0.3096, 89.2265
times = EP.sample_times(start, end)
own_starts = reader._read_starts(E12)
own = reader._decoded(E12, own_starts, times, 0.0)
print("E12 frames at", [round(t, 2) for t, _ in own])
for name, partner, off in (("E03", E03, -0.372), ("E05", E05, -0.248)):
    p_starts = reader._read_starts(partner)
    wide = [times[0] - 1.0, *times, times[-1] + 1.0]
    theirs = reader._decoded(partner, p_starts, wide, off)
    print(name, "frames at", [round(t, 2) for t, _ in theirs][:14])
    for d in (-1.0, -0.5, -0.25, 0.0, 0.25, 0.5, 1.0):
        verdicts = []
        for t in times:
            x, y = EP._nearest(own, t), EP._nearest(theirs, t + off + d)
            verdicts.append("-" if x is None or y is None else ("Y" if EP.frames_alike(x, y) else "n"))
        print(f"   shift {d:+.2f}: {''.join(verdicts)}  share {EP.share_alike(times, own, 0.0, theirs, off + d)}")
