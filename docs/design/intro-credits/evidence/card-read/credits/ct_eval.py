"""Scratch: credit text starts, base vs a work run, on every set with a truth: right (within 5 s, or inside a frame
check's lo..hi), early, late, none; and every file whose verdict changed.

Usage: ct_eval.py ct_work3.json
"""

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
base = json.load(open(os.path.join(HERE, "ct_base.json")))
work = json.load(open(os.path.join(HERE, sys.argv[1])))
# Frame checks of this lane (2026-09-27): epilogue text isn't credits (the owner's rule, §5.4; A55/C03's precedent),
# a dedication or a help-line notice is no story, so either side of it is right.
FRAME = {
    "Ocean with David Attenborough": (4774.0, 4774.0, 4776.0, "epilogue 4744-4771, 'Directors' 4774"),
    "To Dye For The Documentary": (4926.0, 4926.0, 4926.0, "epilogue cards 4887-4923, 'written and directed by' 4926"),
    "Gandhari (2026)": (6531.0, 6531.0, 6544.0, "epilogue to 6518, story shot 6520-6530, title 6531, first card 6544"),
    "A Beautiful Imperfection": (6129.0, 6123.0, 6135.9, "checks.json C03: dedication 6123 is no story"),
    "#SKYKING": (5289.0, 5289.0, 5289.0, "epilogue text to 5287, 'Directed and Produced by' 5289"),
}
TOL = 5.0


def verdict(start, truth, frame=None):
    if frame is not None:
        _, lo, hi, _ = frame
    elif truth is None:
        return "no truth"
    else:
        lo = hi = truth
    if start is None:
        return "none"
    if lo - TOL <= start <= hi + TOL:
        return "right"
    return "early" if start < lo - TOL else "late"


tally: dict[str, list[Counter]] = defaultdict(lambda: [Counter(), Counter()])
changes = []
for path in sorted(set(base) & set(work)):
    b, w = base[path], work[path]
    if "error" in b or "error" in w:
        continue
    name = os.path.basename(path)
    frame = next((v for k, v in FRAME.items() if k in name), None)
    truth = b.get("truth")
    if isinstance(truth, dict):
        truth = truth.get("start")
    vb, vw = verdict(b.get("start_s"), truth, frame), verdict(w.get("start_s"), truth, frame)
    label = b.get("set", "?")
    tally[label][0][vb] += 1
    tally[label][1][vw] += 1
    if vb != vw or b.get("start_s") != w.get("start_s"):
        changes.append((label, name[:48], b.get("start_s"), w.get("start_s"), truth, vb, vw))
for label, (cb, cw) in sorted(tally.items()):
    keys = ("right", "early", "late", "none", "no truth")
    print(
        f"{label:22} base "
        + " ".join(f"{k} {cb[k]}" for k in keys)
        + " | work "
        + " ".join(f"{k} {cw[k]}" for k in keys)
    )
print()
for row in changes:
    print("  ", row)
