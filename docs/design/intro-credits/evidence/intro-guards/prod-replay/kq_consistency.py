import os, re, sqlite3
import kq_variants as V, kq_replay as R
db = sqlite3.connect("prod_markers.db")
ev = {p: (a, b) for p, a, b in db.execute("select f.canonical_path, e.start_ms, e.end_ms from evidence e join files f on f.id=e.file_id where e.source='season_audio' and f.missing_since is null and f.is_movie=0")}
agree = differ = 0
for p, (a, b) in sorted(ev.items()):
    if p not in R.pts or not os.path.exists(p): continue
    files = R.group_of(p)
    seg = V.walk(p, files, R.pts, R.runs_between) if p in files and len(files) > 1 else None
    prod = (a / 1000, b / 1000) if a is not None else None
    same = (seg is None and prod is None) or (seg is not None and prod is not None and abs(seg.start_s - prod[0]) < 0.6 and abs(seg.end_s - prod[1]) < 0.6)
    if same: agree += 1
    else:
        differ += 1
        print(os.path.basename(p)[:60], "prod", prod, "replay", (round(seg.start_s,1), round(seg.end_s,1)) if seg else None)
print("agree", agree, "differ", differ)
