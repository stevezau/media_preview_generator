"""Credit text alone, base against work, on every file of the harness sets (allsets.py output of each tree): per set,
right within 5 s and 10 s, early and late by more than 10 s and 30 s, none; then every file whose start moved.

Truth is each set's own (the 205's and the 80's are chapter starts, 3 frame-checked). A move is listed with both
verdicts so each can be frame-checked.

Usage: compare_sets.py <base.json> <work.json>
"""

import json
import sys
from collections import Counter, defaultdict

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))


def cell(start, truth):
    if start is None:
        return "none"
    d = start - truth
    if abs(d) <= 5:
        return "5s"
    if abs(d) <= 10:
        return "10s"
    if d < -30:
        return "early>30"
    if d < -10:
        return "early>10"
    if d > 30:
        return "late>30"
    return "late>10"


def shown(start, truth):
    return "None" if start is None else f"{start:8.1f} ({start - truth:+6.1f} {cell(start, truth)})"


tally = defaultdict(lambda: [Counter(), Counter()])
moved = []
for path, b in base.items():
    w = work.get(path)
    if w is None:
        continue
    name, truth = b["set"], b["truth"]
    tally[name][0][cell(b["start_s"], truth)] += 1
    tally[name][1][cell(w["start_s"], truth)] += 1
    if (b["start_s"] is None) != (w["start_s"] is None) or (
        b["start_s"] is not None and abs(b["start_s"] - w["start_s"]) > 0.5
    ):
        moved.append((name, b["start_s"], w["start_s"], truth, path))
cols = ("5s", "10s", "late>10", "late>30", "early>10", "early>30", "none")
print("set                  " + " ".join(f"{c:>9s}" for c in cols))
for name, (tb, tw) in sorted(tally.items()):
    print(f"{name:20s} " + " ".join(f"{tb[c]:>4d}>{tw[c]:<4d}" for c in cols))
print()
for name, old, new, truth, path in sorted(moved):
    print(f"{name:18s} {shown(old, truth)} -> {shown(new, truth)} truth {truth:8.1f} {path.split('/')[-1][:60]}")
