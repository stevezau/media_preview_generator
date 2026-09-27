"""The 70/30 tuning / held-out split of the verdict and Plex sets, by file, random.Random(20260927)."""

import json
import random

from common import HERE

items = json.load(open(HERE / "items.json"))
paths = sorted({it["path"] for it in items["verdict"] + items["plex"]})
rng = random.Random(20260927)
rng.shuffle(paths)
cut = round(len(paths) * 0.7)
split = {p: ("tune" if i < cut else "held") for i, p in enumerate(paths)}
json.dump(split, open(HERE / "split.json", "w"), indent=0)
by_id = {}
for it in items["verdict"] + items["plex"]:
    by_id[it["id"]] = split[it["path"]]
print(len(paths), "files;", cut, "tune;", len(paths) - cut, "held-out")
named = ["S03", "S04", "C03", "P01", "P03", "P07", "S13", "S17", "S113", "S18", "S22", "S23", "S34", "S36", "S39",
         "S40", "S41", "S47", "S92", "S96", "S101", "S58", "S67", "S71", "P08", "P51"]  # fmt: skip
print("audit-named cases:", {k: by_id.get(k) for k in named})
