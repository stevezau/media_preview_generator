"""Compare a variant's JSON outputs against the base ones: per-set tallies and every changed row.

Usage: compare.py <variant> [--quiet]
"""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json
import os
import re
import sys

R = str(LOCAL)
v = sys.argv[1]
quiet = "--quiet" in sys.argv
PAIRS = [
    ("intro", f"{R}/intro_base_skipdb.json", f"{R}/intro_v_{v}.json"),
    ("credits", f"{R}/cred_base_chap.json", f"{R}/cred_v_{v}.json"),
    ("online", f"{R}/online_v_base.json", f"{R}/online_v_{v}.json"),
]


def tally(rows):
    t = collections.defaultdict(collections.Counter)
    for k, r in rows.items():
        t[k.split("|")[0]][r["verdict"]] += 1
    return t


for kind, a_path, b_path in PAIRS:
    if not os.path.exists(b_path) or not os.path.exists(a_path):
        continue
    a, b = json.load(open(a_path)), json.load(open(b_path))
    ta, tb = tally(a), tally(b)
    for s in sorted(ta):
        if ta[s] != tb[s]:
            print(f"{kind:7s} {s:24s} {dict(sorted(ta[s].items()))} -> {dict(sorted(tb[s].items()))}")
    if quiet:
        continue
    for k in sorted(a):
        x, y = a[k], b[k]
        if (x["verdict"], x["seg"], x["reason"]) != (y["verdict"], y["seg"], y["reason"]):
            name = os.path.basename(k.split("|", 1)[1])[:60]
            print(
                f"   {k.split('|')[0][:12]:12s} {name:60s} {x['verdict']:8s}->{y['verdict']:8s} {x['seg']} -> {y['seg']}"
            )
            print(f"        {x['reason'][:70]} -> {y['reason'][:70]}  truth {y['truth']}")

a = json.load(open(f"{R}/prod_base.json"))
b = json.load(open(f"{R}/prod_v_{v}.json"))
moves = collections.Counter()
for k in sorted(a, key=lambda k: a[k]["path"]):
    x, y = a[k], b[k]
    if (x["status"], x["reason"], x["marker"]) == (y["status"], y["reason"], y["marker"]):
        continue
    moves[(k.split(":")[1], x["status"], y["status"])] += 1
    if quiet:
        continue
    m = re.search(r"S\d\dE\d\d", x["path"])
    name = x["path"].split("/")[-3][:28] + " " + m.group(0) if m else os.path.basename(x["path"])[:40]
    print(
        f"   prod {k.split(':')[1]:8s} {name:42s} {x['status']:12s}->{y['status']:12s} {x['marker']} -> {y['marker']}"
    )
    print(f"        {x['reason'][:70]} -> {y['reason'][:70]}")
print("prod moves", dict(moves))
