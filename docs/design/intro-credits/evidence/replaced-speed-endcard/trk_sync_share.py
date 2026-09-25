"""Tomb Raider King E12's end-picture shares with the picture sync search (0, -0.5, +0.5 s), CPU decode."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ep_sync_sets as V  # noqa: E402

V.reader = V.EP.Reader(ffmpeg="/usr/bin/ffmpeg")
D = "/data_16tb/TV Shows/Tomb Raider King (2026) {tvdb-452039}/Season 01/"
E12 = D + "Tomb Raider King (2026) - S01E12 - TBA [WEBRip-1080p][AAC 2.0][x265].mkv"
E03 = D + "Tomb Raider King (2026) - S01E03 - One Suited for Domination [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
E05 = (
    D + "Tomb Raider King (2026) - S01E05 - The Owner of the Tree of Life [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
)
start, end = 0.3096, 89.2265
for name, partner, off in (("E03", E03, -0.372), ("E05", E05, -0.248)):
    base = V.reader.share(E12, partner, start, end, off)
    print(name, "share as aligned:", base, "with the sync search:", V.sync_share(E12, partner, start, end, off))
