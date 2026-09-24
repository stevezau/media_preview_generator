import os, re, sys, sqlite3
os.environ["DECODE"] = "1"
import kq_replay as R
roots = R.ROOTS
db = sqlite3.connect("prod_markers.db")
rows = db.execute("select f.canonical_path, d.status, d.proposed_start_ms, d.proposed_end_ms from files f join decisions d on d.file_id=f.id and d.type='intro' where f.missing_since is null and f.is_movie=0").fetchall()
out = []
for p, st, a, b in rows:
    if p not in R.pts or not os.path.exists(p):
        continue
    root = next(r for r in roots if p.startswith(r + "/"))
    rel = os.path.dirname(p)[len(root):]
    tiers = [r for r in roots if os.path.isdir(r + rel)]
    if len(tiers) < 2:
        continue
    os.environ.pop("MERGE", None)
    _, sep = R.describe(p, 40)
    os.environ["MERGE"] = "1"
    _, mer = R.describe(p, 40)
    f = lambda s: f"{s.start_s:.1f}-{s.end_s:.1f}" if s else "None"
    se = re.search(r"S\d+E\d+", p).group(0)
    print(f"{os.path.basename(os.path.dirname(os.path.dirname(p)))[:34]:34} {se} tiers={len(tiers)} status={st:13} per-tier={f(sep):13} merged={f(mer)}", flush=True)
import pickle; pickle.dump(R.dcache, open(R.dcache_path, "wb"))
