"""Scratch: the tree's is_prose on M2's cards as the app read them (m2_<scale>.json), by kind."""

import json
import os
import sys

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.credits import cards  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
for s in (6, 4):
    rows = json.load(open(os.path.join(HERE, f"m2_{s}.json")))
    kinds: dict[str, list] = {}
    for r in rows:
        kinds.setdefault(r["kind"], []).append((r["label"], cards.is_prose(r["lines"])))
    print(s, {k: f"{sum(p for _, p in v)}/{len(v)}" for k, v in kinds.items()},
          {k: [l for l, p in v if p] for k, v in kinds.items() if k != "epilogue"},
          "epilogue missed", [l for l, p in kinds["epilogue"] if not p])  # fmt: skip
