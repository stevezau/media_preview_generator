"""TRK E12 under the end-picture variants (CPU decode)."""

import os
import sys

os.environ["EP_GPU"] = ""
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ep_variants_sets as V  # noqa: E402

D = "/data_16tb/TV Shows/Tomb Raider King (2026) {tvdb-452039}/Season 01/"
E12 = D + "Tomb Raider King (2026) - S01E12 - TBA [WEBRip-1080p][AAC 2.0][x265].mkv"
E03 = D + "Tomb Raider King (2026) - S01E03 - One Suited for Domination [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
E05 = (
    D + "Tomb Raider King (2026) - S01E05 - The Owner of the Tree of Life [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
)
V.frames = {}
for partner, off in ((E03, -0.372), (E05, -0.248)):
    key = (E12, partner, 0.31, 89.227, off)
    print(
        os.path.basename(partner)[:40],
        {v: V.variant_share(key, v) for v in ("near", "card")},
        V.verdicts(V.decoded(key), "near"),
    )
