import json, os, sys
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.credits import rule_j
ref = "intel"
others = sys.argv[2].split(",")
def cls(r): return rule_j.is_credit((r[0], r[1], r[2], tuple(map(tuple, r[3]))))
print("tag        vs      same_pts nkey  max|dL|  dark_flips  count_diff  credit_flips   starts(intel -> other)")
for line in open(sys.argv[1]):
    tag = line.split("\t")[0]
    if not os.path.exists(f"{tag}_{ref}.json"): continue
    A = json.load(open(f"{tag}_{ref}.json"))
    ka = {round(r[0], 3): r for r in A["decodes"][0] and A["key"]}
    for o in others:
        f = f"{tag}_{o}.json"
        if not os.path.exists(f): continue
        B = json.load(open(f)); kb = {round(r[0], 3): r for r in B["key"]}
        same = set(ka) == set(kb)
        common = sorted(set(ka) & set(kb))
        dl = max(abs(ka[p][2] - kb[p][2]) for p in common)
        dark = sum((ka[p][2] < 30) != (kb[p][2] < 30) for p in common)
        cd = sum(ka[p][1] != kb[p][1] for p in common)
        cf = sum(cls(ka[p]) != cls(kb[p]) for p in common)
        print(f"{tag:10s} {o:9s} {str(same):5s} {len(common):4d}  {dl:5.1f}   {dark:5d}      {cd:5d}      {cf:5d}       {A['start']} -> {B['start']}")
