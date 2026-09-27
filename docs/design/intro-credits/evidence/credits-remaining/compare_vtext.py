"""Credit text's own answer, base against work, on the verdict, Plex and chapter files (vtext.py output of each tree):
every file whose start moved more than 0.5 s, with the audit's frame-checked truth where it has one.

Usage: compare_vtext.py <base.json> <work.json>
"""

import json
import sys
from collections import Counter

from common import audit_truths, verdict

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
truths = audit_truths()
counts = Counter()
for path, b in sorted(base.items(), key=lambda kv: kv[0].split("/")[-1]):
    w = work.get(path)
    if w is None or "error" in b or "error" in w:
        continue
    old, new = b.get("start_s"), w.get("start_s")
    if (old is None) == (new is None) and (old is None or abs(old - new) <= 0.5):
        continue
    truth = truths.get(path)
    vo = verdict(old, truth) if truth else "?"
    vn = verdict(new, truth) if truth else "?"
    counts[(vo, vn)] += 1
    t = None if truth is None else (truth.get("lo", truth["start"]), truth.get("hi", truth["start"]))
    print(f"{old!s:>9} {vo:5s} -> {new!s:>9} {vn:5s} truth {t} {path.split('/')[-1][:70]}")
print(dict(counts))
