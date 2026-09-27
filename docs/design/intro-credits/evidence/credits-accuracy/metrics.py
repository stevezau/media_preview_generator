"""Before/after on the audit's verdict set and the Plex-comparison files, from two replay outputs.

Usage: metrics.py <replay_base.json> <replay_work.json> [--show]

A decision is judged against the item's frame-checked truth: credits right within 5 s of the first card; an intro right
with both edges within 5 s. Harmful (skips story): credits more than 5 s early, an intro ending more than 5 s late.
Where the truth is only "the audited marker was right" (approx) and the new decision moved more than 5 s from it, the
item is listed as CHECK until a frame check (checks.json: {"<id>": {"start": s, "end": s|null, "note": ...}}) settles it.
"""

import json
import sys
from collections import Counter

from common import HERE

base = json.load(open(sys.argv[1]))
work = json.load(open(sys.argv[2]))
items = json.load(open(HERE / "items.json"))
split = json.load(open(HERE / "split.json"))
checks = json.load(open(HERE / "checks.json")) if (HERE / "checks.json").exists() else {}
SHOW = "--show" in sys.argv
TOL = 5.0


def marker(rep, it):
    row = rep.get(f"{it['fid']}:{it['type']}")
    if row is None or row["status"] != "decided" or row["marker"] is None:
        return None
    return row["marker"][0] / 1000, row["marker"][1] / 1000


def judge(it, m, truth):
    if truth is None:
        return "unclear"
    if m is None:
        return "none"
    s, e = m
    if it["type"] == "credits":
        lo, hi = truth.get("lo", truth["start"]), truth.get("hi", truth["start"])
        if lo - TOL <= s <= hi + TOL:
            return "right"
        return "harm" if s < lo - TOL else "late"
    ds, de = s - truth["start"], e - truth["end"]
    if abs(ds) <= TOL and abs(de) <= TOL:
        return "right"
    return "harm" if de > TOL else "wrong"


def truth_for(it, m_old, m_new):
    if it["id"] in checks:
        return checks[it["id"]], False
    truth = it.get("truth")
    if truth is None:
        return None, False
    if truth.get("approx") and m_new is not None and m_old is not None:
        moved = abs(m_new[0] - m_old[0]) > TOL or (it["type"] == "intro" and abs(m_new[1] - m_old[1]) > TOL)
        if moved:
            return truth, True
    return truth, False


def tally(pool, which):
    out = {}
    for part in ("tune", "held", "all"):
        c = Counter()
        for it in pool:
            if part != "all" and split[it["path"]] != part:
                continue
            m_old, m_new = marker(base, it), marker(work, it)
            truth, check = truth_for(it, m_old, m_new)
            m = m_old if which == "base" else m_new
            v = judge(it, m, truth)
            if which == "work" and check:
                v = "CHECK"
            c[(it["type"], v)] += 1
        out[part] = c
    return out


def summary(c, mtype):
    judged = sum(n for (t, v), n in c.items() if t == mtype and v != "unclear")
    wrong = sum(n for (t, v), n in c.items() if t == mtype and v in ("late", "harm", "wrong", "none"))
    harm = sum(n for (t, v), n in c.items() if t == mtype and v == "harm")
    check = sum(n for (t, v), n in c.items() if t == mtype and v == "CHECK")
    return f"{wrong}/{judged} wrong ({100 * wrong / max(judged, 1):.1f}%), {harm} skip story" + (
        f", {check} to check" if check else ""
    )


verdict = [it for it in items["verdict"]]
# The five Plex-comparison movie chapters the audit counted in its verdict set.
verdict += [it for it in items["plex"] if it["id"] in ("P01", "P03", "P07", "P20", "P25")]
plex = [it for it in items["plex"] if it["type"] == "credits"]
for name, pool in (("verdict", verdict), ("plex", plex)):
    b, w = tally(pool, "base"), tally(pool, "work")
    for part in ("tune", "held", "all"):
        print(f"{name:7s} {part:4s} credits  base {summary(b[part], 'credits'):45s} work {summary(w[part], 'credits')}")
        if name == "verdict":
            print(f"{name:7s} {part:4s} intros   base {summary(b[part], 'intro'):45s} work {summary(w[part], 'intro')}")

# Plex's own read on the Plex-comparable files.
pc = Counter()
for it in plex:
    if it.get("truth") is None:
        continue
    for part in ("tune", "held", "all"):
        if part != "all" and split[it["path"]] != part:
            continue
        d = it["plex_s"] - it["truth"]["start"] if it["plex_v"] != "right" or not it["truth"].get("approx") else 0.0
        v = it["plex_v"]
        pc[(part, "wrong" if v == "wrong" else ("right" if v == "right" else "unclear"))] += 1
        pc[(part, "harm")] += int(v == "wrong" and d < -TOL)
for part in ("tune", "held", "all"):
    judged = pc[(part, "wrong")] + pc[(part, "right")]
    print(f"plex's  {part:4s} credits  {pc[(part, 'wrong')]}/{judged} wrong ({100 * pc[(part, 'wrong')] / max(judged, 1):.1f}%), "
          f"{pc[(part, 'harm')]} skip story")  # fmt: skip

if SHOW:
    for it in verdict + [p for p in plex if p not in verdict]:
        m_old, m_new = marker(base, it), marker(work, it)
        truth, check = truth_for(it, m_old, m_new)
        vo, vn = judge(it, m_old, truth), judge(it, m_new, truth)
        if check:
            vn = "CHECK"
        if m_old != m_new or vo != vn:
            t = None if truth is None else (truth["start"], truth.get("end"))
            print(f"{it['id']:6s} {split[it['path']]:4s} {it['type']:7s} {it['path'].split('/')[-1][:45]:45s} "
                  f"{m_old} {vo} -> {m_new} {vn} truth {t}")  # fmt: skip
