"""Per-file credit text wall time (GPU decode + detection) from a vtext log, split episode/movie and chapter/other."""

import json
import re
import statistics
import sys

from common import HERE

items = json.load(open(HERE / "items.json"))
extra = json.load(open(HERE / "extra_files.json"))
names = {}
for it in items["verdict"] + items["plex"]:
    names[it["path"].split("/")[-1][:60]] = ("listed", it["episode"])
for p, ep in extra.items():
    names.setdefault(p.split("/")[-1][:60], ("chapter", ep))
times = {}
for line in open(sys.argv[1]):
    m = re.match(r"^\d+ \d+ ([\d.]+)s \S+ \S+ \S+ (.*)$", line.rstrip("\n"))
    if m:
        times[m.group(2)] = float(m.group(1))
for kind in ("chapter", "listed"):
    for ep in (True, False):
        xs = sorted(t for n, t in times.items() if names.get(n) == (kind, ep))
        if xs:
            print(kind, "episode" if ep else "movie", len(xs), "median", round(statistics.median(xs), 1),
                  "p90", round(xs[int(len(xs) * 0.9)], 1), "max", max(xs), "total", round(sum(xs)))  # fmt: skip
print("unmatched", len([n for n in times if n not in names]))
