"""Diff two prod replay JSONs (status, reason, marker) with short names.

Usage: compare_prod.py <before.json> <after.json> [type]
"""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import re
import sys

a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
only = sys.argv[3] if len(sys.argv) > 3 else None
moves = collections.Counter()
for k in sorted(a, key=lambda k: a[k]["path"]):
    x, y = a[k], b[k]
    if (x["status"], x["reason"], x["marker"]) == (y["status"], y["reason"], y["marker"]):
        continue
    if only and not k.endswith(only):
        continue
    moves[(k.split(":")[1], x["status"], y["status"])] += 1
    m = re.search(r"S\d\dE\d\d", x["path"])
    name = x["path"].split("/")[-3][:28] + " " + m.group(0) if m else os.path.basename(x["path"])[:40]
    print(f"   {k.split(':')[1]:8s} {name:42s} {x['status']:12s}->{y['status']:12s} {x['marker']} -> {y['marker']}")
    print(f"        {x['reason'][:80]} -> {y['reason'][:80]}")
print("moves", dict(moves))
