"""From t3_split.log (split season folders) and a directory-only walk: how many season folders of the TV library there
are in all, how many are split across disks, and how many files the split ones hold. Read-only."""

import collections
import os
import re

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
HERE = os.path.dirname(os.path.abspath(__file__))
seasons = collections.defaultdict(set)
for root in ROOTS:
    tv = f"{root}/TV Shows"
    if not os.path.isdir(tv):
        continue
    for show in os.listdir(tv):
        show_dir = os.path.join(tv, show)
        if not os.path.isdir(show_dir):
            continue
        for season in os.listdir(show_dir):
            if os.path.isdir(os.path.join(show_dir, season)):
                seasons[(show, season)].add(root)
split_log = [ln for ln in open(os.path.join(HERE, "t3_split.log")) if ln.startswith("  ")]
per_disks = collections.Counter(len(re.findall(r"=\d+", ln)) for ln in split_log)
print("relative season folders in all:", len(seasons))
print("on more than one disk (directory walk):", sum(len(v) > 1 for v in seasons.values()))
print("split with videos on more than one disk (t3_split.log):", len(split_log), "by disk count", dict(per_disks))
print("disks named:", collections.Counter(r for v in seasons.values() for r in v))
