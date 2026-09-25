"""Files whose verdict or segment differs between two intro_sets.py/credits JSON outputs."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import sys

a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
moves = collections.Counter()
for k in sorted(a):
    x, y = a[k], b.get(k)
    if y is None or (x["verdict"], x["seg"]) == (y["verdict"], y["seg"]):
        continue
    moves[(k.split("|")[0], x["verdict"], y["verdict"])] += 1
    name = os.path.basename(k.split("|", 1)[1])[:70]
    print(f"{k.split('|')[0]:10s} {name}")
    print(f"     {x['verdict']:8s} {x['seg']} {x['reason'][:70]}")
    print(f"  -> {y['verdict']:8s} {y['seg']} {y['reason'][:70]}")
    print(f"     truth {y['truth']} cands {y['cands']}")
for k, v in sorted(moves.items()):
    print(k, v)
