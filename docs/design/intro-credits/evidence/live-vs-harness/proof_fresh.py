"""Usage: proof_fresh.py <replay of the shipped tree> <replay of the work tree> (both on local/after/db).

The fresh audit sample (accuracy-after, seed 20260929) before/after this lane, from two replays of today's
markers.db (the shipped tree and the work tree, each as the live app would run them). An item whose marker doesn't move
keeps its frame verdict; one that moves is judged on the frame truth its verdict states (listed). Split: 70/30 by file,
random.Random(20260929), the held-out part read once."""

import collections
import json
import random
import sys

from common import AFTER, DB_NOW

W = f"{AFTER}/work/"
base, work = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
items = json.load(open(W + "fresh_sample.json"))
verd = json.load(open(W + "fresh_verdicts.json"))
moved_items = {it["id"]: it for it in json.load(open(W + "moved_items.json"))}
moved_verd = json.load(open(W + "moved_verdicts.json"))
paths = sorted({it["path"] for it in items} | {it["path"] for it in moved_items.values()})
rng = random.Random(20260929)
order = paths[:]
rng.shuffle(order)
held = set(order[: round(0.3 * len(order))])
# Frame truths for the items whose marker moves (from their verdict text, frame-checked by the audit).
TRUTH = {"F48": ("intro", None), "RI02": ("intro", (0.0, 111.6))}


def marker(rep, fid, t):
    row = rep.get(f"{fid}:{t}")
    return None if row is None or row["status"] != "decided" else tuple(x / 1000 for x in row["marker"][:2])


def judge(sid, m, v_before):
    if sid not in TRUTH:
        return v_before
    t, truth = TRUTH[sid]
    if m is None:
        return "missed" if truth is None or True else "none"
    s, e = m
    return "right" if truth and abs(s - truth[0]) <= 6.1 and abs(e - truth[1]) <= 5 else "wrong"


rows = collections.Counter()
changed = []
for it in items:
    sid, fid, t = it["sid"], it["fid"], it["type"]
    v = verd.get(sid, ["unclear"])[0]
    mb, mw = marker(base, fid, t), marker(work, fid, t)
    vb = v
    vw = v if mb == mw else judge(sid, mw, v)
    part = "held" if it["path"] in held else "tune"
    for p in (part, "all"):
        rows[(p, "base", vb)] += 1
        rows[(p, "work", vw)] += 1
        rows[(p, "base", "published")] += mb is not None
        rows[(p, "work", "published")] += mw is not None
    if mb != mw:
        changed.append((sid, part, t, mb, vb, mw, vw))
for p in ("tune", "held", "all"):
    for w in ("base", "work"):
        wrong = rows[(p, w, "wrong")]
        pub = rows[(p, w, "published")]
        print(
            f"fresh {p:4s} {w:4s} published {pub:2d}  wrong {wrong:2d}  right {rows[(p, w, 'right')]:2d}  missed(no marker) {rows[(p, w, 'missed')]}"
        )
for c in changed:
    print("  changed", c)
# The lost-right intros (moved set, 'removed_lost').
for rid, it in moved_items.items():
    if moved_verd.get(rid, [""])[0] != "removed_lost":
        continue
    import sqlite3

    c = sqlite3.connect(f"file:{DB_NOW}?mode=ro", uri=True)
    (fid,) = c.execute("select id from files where canonical_path=?", (it["path"],)).fetchone()
    mb, mw = marker(base, fid, it["type"]), marker(work, fid, it["type"])
    part = "held" if it["path"] in held else "tune"
    print(f"  lost-right {rid} {part} {it['path'].split('/')[-1][:50]} now {mb} -> {mw}")
