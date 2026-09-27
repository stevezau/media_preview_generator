"""Edge-touching text and box sizes between the roll's start and the chapter, for every earlier chapter move.

Usage: move_edges.py <replay_base.json> <replay_work.json>
"""

import json
import statistics
import sys

from common import HERE

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
texts = json.load(open(HERE / "vtext_work.json"))
out = []
for key, w in work.items():
    if not w["reason"].startswith("chapters, the start moved"):
        continue
    chapter = base[key]["marker"][0] / 1000
    start = w["marker"][0] / 1000
    r = texts.get(w["path"])
    if r is None or start >= chapter:
        continue
    rows = [x for x in r["key"] if start <= x[0] < chapter and x[1] >= 1 and len(x) > 3]
    boxes = [b for x in rows for b in x[3]]
    edge_rows = sum(any(b[0] <= 1 or b[2] >= 318 for b in x[3]) for x in rows)
    edge_boxes = sum(b[0] <= 1 or b[2] >= 318 for b in boxes)
    tall = sorted(b[3] - b[1] for b in boxes)
    counts = sorted(x[1] for x in rows)
    out.append((edge_rows, key, w["path"].split("/")[-1][:40], len(rows), edge_boxes, len(boxes),
                tall[-1] if tall else None, statistics.median(counts) if counts else None))  # fmt: skip
for row in sorted(out, reverse=True)[:30]:
    print("edge_rows=%d %-12s %-40s texted_rows=%d edge_boxes=%d/%d tallest=%s median_boxes=%s" % row)
