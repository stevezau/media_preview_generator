"""Text gaps between the roll's start and the chapter for every credits chapter the work replay moved earlier.

Usage: move_gaps.py <replay_base.json> <replay_work.json>
"""

import json
import statistics
import sys

from common import HERE

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
texts = json.load(open(HERE / "vtext_work.json"))
DARK = 30.0
out = []
for key, w in work.items():
    if not w["reason"].startswith("chapters, the start moved"):
        continue
    chapter = base[key]["marker"][0] / 1000
    start = w["marker"][0] / 1000
    r = texts.get(w["path"])
    if r is None or start >= chapter:
        continue
    rows = sorted(r["key"], key=lambda x: x[0])
    texted = [x[0] for x in rows if x[1] >= 1 and start < x[0] < chapter]
    credit = [x[0] for x in rows if start < x[0] < chapter and ((x[2] < DARK and x[1] >= 1) or x[1] >= 3)]
    keys = [x[0] for x in rows if start <= x[0] <= chapter]

    def gap(ts):
        stops = [start, *ts, chapter]
        return round(max(b - a for a, b in zip(stops, stops[1:], strict=False)), 2)

    spacing = round(statistics.median(b - a for a, b in zip(keys, keys[1:], strict=False)), 2) if len(keys) > 2 else 0
    out.append((gap(texted), gap(credit), spacing, key, w["path"].split("/")[-1][:42]))
for g, c, s, key, name in sorted(out, reverse=True):
    print(f"text_gap={g:6} credit_gap={c:6} key_spacing={s:5} {key:12s} {name}")
