"""Run the 43 verified online cases through the REAL source parsers + decide() (no network).

SkipDB: the build uses the read API (match exact <=2 s / shifted <=15 s); simulated from the ODbL dump by
nearest-duration row, match = exact/shifted/out-of-range by |dump duration - file duration|.
"""
import json, sys
import os as _os
from pathlib import Path as _Path
# MPG_REPO: the checkout whose code is exercised (default: the repo holding this folder).
# MPG_EVIDENCE: the local-only evidence data (gitignored truth files; default: <repo>/docs/design/intro-credits/evidence).
REPO = _os.environ.get("MPG_REPO") or str(_Path(globals().get('__file__') or 'online43_decide.py').resolve().parents[5])
EVIDENCE = _os.environ.get("MPG_EVIDENCE") or f"{REPO}/docs/design/intro-credits/evidence"
HERE = _os.path.dirname(_os.path.abspath(globals().get('__file__') or 'online43_decide.py'))
sys.path.insert(0, REPO)
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide
from media_preview_generator.markers.models import MarkerType as T
from media_preview_generator.markers.sources import theintrodb, introdb, skipdb

EV = f"{EVIDENCE}/online"
cases = json.load(open(f"{EV}/online_results.json"))
dump = json.load(open(f"{EV}/skipdb-dump.json"))["segments"]

def skip_segments(c):
    rows = [x for x in dump if x["imdb_id"] == c["imdb"] and x.get("season") == c["season"] and x.get("episode") == c["episode"]]
    out = {}
    for key, st in (("intro", "intro"), ("outro", "outro"), ("recap", "recap"), ("preview", "preview")):
        cand = [x for x in rows if x["segment_type"] == st and x.get("start_ms") is not None]
        if not cand:
            continue
        best = min(cand, key=lambda x: abs((x.get("duration_ms") or 0) - c["dur"] * 1000))
        diff = abs((best.get("duration_ms") or 0) - c["dur"] * 1000)
        match = "exact" if diff <= 2000 else "shifted" if diff <= 15000 else "out-of-range"
        out[key] = {**best, "match": match}
    return out

def verdict(mtype, m, c):
    if mtype is T.INTRO:
        ts, te = c["intro"] or (None, None)
        if ts is None:
            return "WRONG(no truth intro)"
        s, e = m.start_ms / 1000, m.end_ms / 1000
        # wrong = skips story: starts >3 s before truth start, or ends >5 s after truth end, or no overlap
        if e > te + 5 or s < ts - 5 or e < ts or s > te:
            return f"WRONG pub {s:.1f}-{e:.1f} truth {ts:.1f}-{te:.1f}"
        return "ok"
    cs = c["credits_start"]
    s = m.start_ms / 1000
    if cs is None:
        return "WRONG(no truth credits)"
    if s < cs - 10:
        return f"WRONG(early) pub {s:.1f} truth {cs:.1f}"
    if s > cs + 30:
        return f"late pub {s:.1f} truth {cs:.1f}"
    return "ok"

def run(order, level):
    tally = {}
    rows = []
    for r in cases:
        c = r["case"]
        cands = []
        if "theintrodb" in order and isinstance(r.get("tidb"), dict):
            cands += theintrodb._candidates(r["tidb"])
        if "introdb" in order and isinstance(r.get("idb"), dict):
            cands += introdb._candidates(r["idb"])
        if "skipdb" in order:
            cands += skipdb._candidates(skip_segments(c))
        ctx = DecisionContext(int(c["dur"] * 1000), False, level, frozenset({T.INTRO, T.CREDITS}), order)
        out = decide(cands, ctx, {})
        for mtype in (T.INTRO, T.CREDITS):
            d = out[mtype]
            key = (mtype.value, d.status.value)
            if d.status is DecisionStatus.DECIDED:
                v = verdict(mtype, d.marker, c)
                key = (mtype.value, "decided-" + v.split()[0])
                if v != "ok":
                    rows.append(f"  {c['show']} S{c['season']:02d}E{c['episode']:02d} {mtype.value}: {v} by {d.marker.decided_by}")
            tally[key] = tally.get(key, 0) + 1
    return tally, rows

for label, order, level in (
    ("DEFAULT (introdb+skipdb, high)", ("chapters", "introdb", "skipdb", "server_markers"), "high"),
    ("TheIntroDB enabled, high", ("chapters", "theintrodb", "introdb", "skipdb", "server_markers"), "high"),
    ("TheIntroDB enabled, medium", ("chapters", "theintrodb", "introdb", "skipdb", "server_markers"), "medium"),
):
    tally, rows = run(order, level)
    print(label)
    for k in sorted(tally):
        print("  ", k, tally[k])
    print("\n".join(rows))
