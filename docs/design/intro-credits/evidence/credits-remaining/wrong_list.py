"""Every credits item of the verdict and Plex sets one replay gets wrong, with what decided it and credit text's own
answer (2026-09-27 remaining-credits lane).

Usage: wrong_list.py <replay.json> <vtext.json>
"""

import json
import sys

from common import ACCURACY as HERE

rep = json.load(open(sys.argv[1]))
text = json.load(open(sys.argv[2]))
items = json.load(open(HERE / "items.json"))
split = json.load(open(HERE / "split.json"))
checks = json.load(open(HERE / "checks.json")) if (HERE / "checks.json").exists() else {}
TOL = 5.0
seen = set()
for it in items["verdict"] + items["plex"]:
    if it["type"] != "credits":
        continue
    truth = checks.get(it["id"], it.get("truth"))
    row = rep.get(f"{it['fid']}:credits")
    if truth is None or row is None:
        continue
    m = row["marker"] if row["status"] == "decided" else None
    s = None if m is None else m[0] / 1000
    lo, hi = truth.get("lo", truth["start"]), truth.get("hi", truth["start"])
    if s is not None and lo - TOL <= s <= hi + TOL:
        continue
    t = text.get(it["path"], {})
    off = None if s is None else round(s - (lo if s < lo else hi), 1)
    key = (it["path"], it["id"][0] == "P")
    print(f"{it['id']:5s} {split[it['path']]:4s} {'EARLY' if off is not None and off < 0 else 'late ' if off else 'none '}"
          f" off={off} ours={s} by={None if m is None else m[2]} text={t.get('start_s')} truth={truth['start']}"
          f"{'~' if truth.get('approx') else ''} {it['path'].split('/')[-1][:55]}")  # fmt: skip
