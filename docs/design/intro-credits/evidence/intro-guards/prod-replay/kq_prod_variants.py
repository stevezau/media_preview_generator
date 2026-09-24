"""Run shipped vs variant season steps over every prod TV file (prod fingerprints, per-tier or merged groups)."""
import os, re, sys, sqlite3, pickle
import kq_variants as V
import kq_replay as R
names = sys.argv[1:] or ["shipped"]
db = sqlite3.connect("prod_markers.db")
rows = db.execute("select f.canonical_path, d.status from files f join decisions d on d.file_id=f.id and d.type='intro' where f.missing_since is null and f.is_movie=0").fetchall()
f = lambda s: f"{s.start_s:.1f}-{s.end_s:.1f}" if s else "None"
changed = 0
for p, st in sorted(rows):
    if p not in R.pts or not os.path.exists(p):
        continue
    files = R.group_of(p)
    if p not in files or len(files) < 2:
        continue
    res = [V.walk(p, files, R.pts, R.runs_between, **V.VARIANTS[n]) for n in names]
    if any((a is None) != (res[0] is None) or (a and res[0] and abs(a.end_s - res[0].end_s) > 0.3) for a in res[1:]):
        changed += 1
        print(f"{os.path.basename(os.path.dirname(os.path.dirname(p)))[:34]:34} {re.search(r'S\d+E\d+', p).group(0)} {st:13} " + "  ".join(f"{n}={f(s)}" for n, s in zip(names, res)), flush=True)
print("changed", changed)
pickle.dump(V.shares, open(V.CACHE, "wb"))
