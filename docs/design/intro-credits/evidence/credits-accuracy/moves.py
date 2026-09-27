"""Credits/intro starts and ends that moved more than 5 s between two replays, by rule, with audit truth where known.

Usage: moves.py <replay_base.json> <replay_work.json>
"""

import json
import sys
from collections import Counter

from common import HERE

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
items = json.load(open(HERE / "items.json"))
truth = {}
for it in items["verdict"] + items["plex"]:
    if it.get("truth"):
        truth[f"{it['fid']}:{it['type']}"] = (it["id"], it["truth"]["start"], it["truth"].get("approx"))
kinds = Counter()
rows = []
for key, b in base.items():
    w = work.get(key)
    if w is None:
        continue
    mb = b["marker"] if b["status"] == "decided" else None
    mw = w["marker"] if w["status"] == "decided" else None
    if mb is None or mw is None:
        if (mb is None) != (mw is None):
            rows.append((key, b["path"], mb, mw, w["reason"], "status"))
        continue
    ds, de = (mw[0] - mb[0]) / 1000, (mw[1] - mb[1]) / 1000
    if abs(ds) <= 5 and abs(de) <= 5:
        continue
    t = key.split(":")[1]
    direction = "earlier" if ds < -5 else ("later" if ds > 5 else "end-only")
    kinds[(t, direction, w["reason"][:40])] += 1
    rows.append((key, b["path"], mb, mw, w["reason"], direction))
for k, n in sorted(kinds.items()):
    print(n, k)
for key, path, mb, mw, reason, direction in rows:
    tr = truth.get(key)
    f = lambda m: "-" if m is None else f"{m[0] / 1000:.1f}-{m[1] / 1000:.1f}"  # noqa: E731
    print(f"{direction:8s} {key:10s} {path.split('/')[-1][:55]:55s} {f(mb)} -> {f(mw)} {reason[:45]} {tr or ''}")
