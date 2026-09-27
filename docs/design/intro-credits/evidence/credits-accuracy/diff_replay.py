"""What moved between two replay.py outputs: diff_replay.py base.json work.json [--all]"""

import json
import sys
from collections import Counter

a = json.load(open(sys.argv[1]))
b = json.load(open(sys.argv[2]))
moves = Counter()
for key in sorted(a, key=lambda k: (a[k]["path"], k)):
    x, y = a[key], b.get(key)
    if y is None:
        continue
    mx = x["marker"] if x["status"] == "decided" else None
    my = y["marker"] if y["status"] == "decided" else None
    same = (mx is None and my is None) or (
        mx is not None and my is not None and abs(mx[0] - my[0]) <= 1000 and abs(mx[1] - my[1]) <= 1000
    )
    if same and "--all" not in sys.argv:
        continue
    t = key.split(":")[1]
    kind = f"{t}: {x['status']}->{y['status']}"
    moves[kind] += 1
    name = x["path"].split("/")[-1][:60]
    fmt = lambda m: "-" if m is None else f"{m[0] / 1000:.1f}-{m[1] / 1000:.1f} {','.join(m[2])}"  # noqa: E731
    print(
        f"{key} {name}\n   {x['status']:12s} {fmt(mx)} | {x['reason'][:70]}\n   {y['status']:12s} {fmt(my)} | {y['reason'][:90]}"
    )
print(dict(moves))
