import json, sys, numpy as np
from PIL import Image
pre = sys.argv[1]; vs = sys.argv[2].split(","); want = [float(x) for x in sys.argv[3].split(",")]
P, T = {}, {}
for v in vs:
    d = json.load(open(f"{pre}_{v}.json")); P[v] = np.load(f"{pre}_{v}.npz")["planes"]
    T[v] = [r[0] for dec in d["decodes"] for r in dec]
    assert len(T[v]) == len(P[v]), (v, len(T[v]), len(P[v]))
def grad(x):
    x = x.astype(np.float32); return float(np.abs(np.diff(x, axis=1)).mean() + np.abs(np.diff(x, axis=0)).mean())
# global stats over key pass
n = int(sys.argv[4]) if len(sys.argv) > 4 else min(len(P[v]) for v in vs)
for v in vs:
    x = P[v][:n].astype(np.float32)
    print(f"{v:7s} mean {x.mean():6.2f} p1 {np.percentile(x,1):5.1f} p99 {np.percentile(x,99):5.1f} min {x.min():3.0f} max {x.max():3.0f} grad {np.mean([grad(p) for p in P[v][:n]]):.3f}")
for a in vs:
    for b in vs:
        if a < b:
            d = P[a][:n].astype(np.float32) - P[b][:n].astype(np.float32)
            print(f"{a}-{b}: mean {d.mean():+.3f} MAE {np.abs(d).mean():.3f} max|d| {np.abs(d).max():.0f}")
for w in want:
    row = []
    for v in vs:
        i = min(range(len(T[v])), key=lambda k: abs(T[v][k] - w)); row.append(P[v][i])
        print(f"  {w} {v}: idx {i} pts {T[v][i]} mean {P[v][i].mean():.2f} grad {grad(P[v][i]):.3f}")
    Image.fromarray(np.concatenate(row, axis=0)).resize((640, 360 * len(vs)), Image.NEAREST).save(f"{pre}_{w:.0f}.png")
