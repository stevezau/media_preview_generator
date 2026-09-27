"""Lit keyframes without text between the roll's start and the chapter, for every earlier chapter move.

Usage: move_lit.py <replay_base.json> <replay_work.json>
"""

import json
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
    rows = sorted((x for x in r["key"] if start <= x[0] < chapter), key=lambda x: x[0])
    lit_blank = [round(x[0] - start, 1) for x in rows if x[1] == 0 and x[2] >= DARK]
    dark_blank = [round(x[0] - start, 1) for x in rows if x[1] == 0 and x[2] < DARK]
    out.append((len(lit_blank), key, w["path"].split("/")[-1][:40], len(rows), lit_blank[:6], len(dark_blank)))
for row in sorted(out, reverse=True):
    print("lit_blank=%d %-12s %-40s rows=%d at=%s dark_blank=%d" % row)
