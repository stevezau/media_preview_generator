import collections
import os
import pickle
import sys

D = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh"
res = pickle.load(open(f"{D}/live_check_replay.pkl", "rb"))
TOL = {"intro": 5000, "credits": 10000}
mode = sys.argv[1] if len(sys.argv) > 1 else "list"


def fmt(ms):
    return "-" if ms is None else f"{ms / 1000:.0f}"


def near(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def support(r):
    """Which independent sources (not server_markers) land near ours vs near Plex's start."""
    tol = TOL[r["type"]]
    ours, plex = r["ours"][0], r["plex"][0]
    near_ours, near_plex, other = [], [], []
    for src, s, e, _detail in r["evidence"]:
        if src == "server_markers" or s is None:
            continue
        if near(s, ours, tol):
            near_ours.append(src)
        elif near(s, plex, tol):
            near_plex.append(src)
        else:
            other.append(f"{src}@{fmt(s)}")
    return near_ours, near_plex, other


verdicts = collections.Counter()
rows = []
for r in res:
    if r["agree"]:
        continue
    near_ours, near_plex, other = support(r)
    before_same = r["before"] and r["before"][2] == tuple(r["ours"])
    if near_plex and not near_ours:
        v = "plex-supported"
    elif near_ours and not near_plex:
        v = "ours-supported"
    elif near_ours and near_plex:
        v = "both-supported"
    else:
        v = "no-independent-support"
    r["verdict"] = v
    verdicts[(r["type"], r["speed"].split(" (")[0], v)] += 1
    rows.append(r)

if mode == "list":
    for r in sorted(rows, key=lambda r: (r["type"], r["speed"], r["path"])):
        name = os.path.basename(r["path"])[:70]
        no, npx, oth = support(r)
        print(
            f"{r['type']:7} {r['speed'][:14]:14} fps={r['fps']:6} plex={fmt(r['plex'][0])}-{fmt(r['plex'][1])} "
            f"ours={fmt(r['ours'][0])}-{fmt(r['ours'][1])} dur={fmt(r['dur'])} {r['verdict']:22} "
            f"near_ours={','.join(sorted(set(no)))} near_plex={','.join(sorted(set(npx)))} other={','.join(oth[:4])} "
            f"| {r['reason'][:60]} | {r['replaced'][:10]} | {name}"
        )
print()
for k, v in sorted(verdicts.items()):
    print(k, v)
