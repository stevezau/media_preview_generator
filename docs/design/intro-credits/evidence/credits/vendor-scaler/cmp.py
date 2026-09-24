import json, sys, numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.credits import rule_j
pre = sys.argv[1]; vs = sys.argv[2].split(",")
D = {v: json.load(open(f"{pre}_{v}.json")) for v in vs}
for v in vs:
    d = D[v]
    print(v, "start", d["start"], "end", d["end"], "decodes", [len(x) for x in d["decodes"]], "overlays", d["overlays"])
    print("   cmds:", [" ".join(c[c.index("-vf")+1:c.index("-vf")+2]) for c in d["commands"]])
# align key rows by pts
base = vs[0]
keys = {v: {round(r[0], 2): r for r in D[v]["key"]} for v in vs}
allpts = sorted(set().union(*[set(k) for k in keys.values()]))
only = {v: [p for p in allpts if p not in keys[v]] for v in vs}
print("pts missing per vendor:", {v: len(x) for v, x in only.items()})
lum = {v: np.array([keys[v][p][2] for p in allpts if all(p in keys[w] for w in vs)]) for v in vs}
cnt = {v: np.array([keys[v][p][1] for p in allpts if all(p in keys[w] for w in vs)]) for v in vs}
for v in vs[1:]:
    dl = lum[v] - lum[base]
    print(f"luma {v}-{base}: mean {dl.mean():+.2f} min {dl.min():+.1f} max {dl.max():+.1f}; count diff frames {np.sum(cnt[v]!=cnt[base])}/{len(cnt[v])} sum {cnt[v].sum()} vs {cnt[base].sum()}")
def cls(r): return ("C" if rule_j.is_credit(tuple(r[:3]) + (tuple(map(tuple, r[3])),)) else ".")
common = [p for p in allpts if all(p in keys[w] for w in vs)]
diffs = [p for p in common if len({(keys[v][p][1], cls(keys[v][p])) for v in vs}) > 1]
print("frames whose count or credit-class differs:", len(diffs))
for p in diffs[:80]:
    print(f"  {p:9.2f} " + "  ".join(f"{v}:n={keys[v][p][1]} L={keys[v][p][2]:5.1f} {cls(keys[v][p])}" for v in vs))
