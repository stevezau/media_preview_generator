"""Features of every credits chapter the work replay moved by credit text: what the frames hold between the roll's
start and the chapter, against what they hold on the roll at the chapter.

Usage: move_features.py <replay_base.json> <replay_work.json>
"""

import json
import statistics
import sys

from common import HERE

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
texts = json.load(open(HERE / "vtext_work.json"))
DARK = 30.0


def feats(rows):
    texted = [r for r in rows if r[1] >= 1]
    lit_texted = [r for r in texted if r[2] >= DARK]
    heights = [b[3] - b[1] for r in texted for b in (r[3] if len(r) > 3 else [])]
    mids = [(b[0] + b[2] + 1) / 2 for r in texted for b in (r[3] if len(r) > 3 else [])]
    return (
        len(rows),
        len(texted),
        len(lit_texted),
        round(statistics.median(heights), 1) if heights else None,
        round(statistics.median(mids)) if mids else None,
        round(statistics.median(r[2] for r in rows), 1) if rows else None,
    )


for key, w in sorted(work.items(), key=lambda kv: kv[1]["path"]):
    if not w["reason"].startswith("chapters, the start moved"):
        continue
    b = base[key]
    chapter = b["marker"][0] / 1000
    start = w["marker"][0] / 1000
    r = texts.get(w["path"])
    if r is None:
        continue
    rows = sorted(r["key"], key=lambda x: x[0])
    if start < chapter:
        between = [x for x in rows if start <= x[0] < chapter]
    else:
        between = [x for x in rows if chapter <= x[0] < start]
    after = [x for x in rows if max(start, chapter) <= x[0] < max(start, chapter) + 20]
    print(f"{key:12s} {w['path'].split('/')[-1][:42]:42s} {chapter:8.1f} -> {start:8.1f} "
          f"between n/texted/lit_texted/h/mid/luma={feats(between)} after={feats(after)}")  # fmt: skip
