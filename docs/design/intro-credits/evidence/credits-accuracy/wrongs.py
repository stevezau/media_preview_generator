"""The work replay's wrong credits on the verdict and Plex sets, with Plex's own answer beside each.

Usage: wrongs.py <replay_work.json>
"""

import json
import sys

from common import HERE

work = json.load(open(sys.argv[1]))
items = json.load(open(HERE / "items.json"))
split = json.load(open(HERE / "split.json"))
checks = json.load(open(HERE / "checks.json"))
TOL = 5.0
seen = set()
for it in items["verdict"] + items["plex"]:
    if it["type"] != "credits" or it["id"] in seen:
        continue
    seen.add(it["id"])
    truth = checks.get(it["id"]) or it.get("truth")
    if truth is None:
        continue
    row = work.get(f"{it['fid']}:credits")
    m = None if row is None or row["status"] != "decided" or row["marker"] is None else row["marker"][0] / 1000
    lo, hi = truth.get("lo", truth["start"]), truth.get("hi", truth["start"])
    if m is not None and lo - TOL <= m <= hi + TOL:
        continue
    verdict = "none" if m is None else ("EARLY" if m < lo - TOL else "late")
    off = None if m is None else round(m - truth["start"], 1)
    plex = f"plex {it.get('plex_v')} {it.get('plex_s')}" if "plex_v" in it else ""
    print(f"{it['id']:5s} {split[it['path']]:4s} {verdict:5s} off={off} {it['path'].split('/')[-1][:50]:50s} "
          f"by={row and row['marker'] and row['marker'][2]} {plex}")  # fmt: skip
