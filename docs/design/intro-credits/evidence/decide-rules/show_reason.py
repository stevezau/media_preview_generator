"""Rows of a sets JSON whose reason contains a substring (or whose verdict is one of the given ones)."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import sys

rows = json.load(open(sys.argv[1]))
needle = sys.argv[2]
for k, r in sorted(rows.items()):
    if needle in r["reason"] or needle == r["verdict"]:
        print(k.split("|")[0], os.path.basename(k.split("|", 1)[1])[:60], r["verdict"], r["seg"], "truth", r["truth"])
        print("     ", r["reason"][:110])
        print("     ", r["cands"])
