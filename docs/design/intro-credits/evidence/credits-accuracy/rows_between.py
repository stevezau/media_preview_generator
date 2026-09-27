"""Key rows between a chapter start and the credit text start for named files (work answers).

Usage: rows_between.py <substring> <chapter_s> [pad_s]
"""

import json
import sys

from common import HERE

w = json.load(open(HERE / "vtext_work.json"))
sub, chapter = sys.argv[1], float(sys.argv[2])
pad = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
for p, r in w.items():
    if sub not in p:
        continue
    start = r["start_s"]
    lo, hi = min(start, chapter) - pad, max(start, chapter) + pad
    print(p.split("/")[-1][:60], "start", start, "chapter", chapter, "overlays", r["overlays"])
    for row in r["key"]:
        if lo <= row[0] <= hi:
            boxes = row[3] if len(row) > 3 else []
            heights = sorted(b[3] - b[1] for b in boxes)
            mids = sorted(round((b[0] + b[2] + 1) / 2) for b in boxes)
            print(f"  {row[0]:9.2f} n={row[1]:2d} luma={row[2]:5.1f} h={heights[:6]} mid={mids[:6]}")
