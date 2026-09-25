"""Rule (e) data: every file where season audio and IntroDB disagree on the intro end (> 5 s): the length difference,
the shift, and whether season audio / IntroDB is right against the truth (sets), or the decision (prod)."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import sqlite3
import sys

sys.path.insert(
    0,
    f"{LOCAL}/base",
)
from tools.markers_eval.score import judge_intro  # noqa: E402

R = str(LOCAL)
rows = json.load(open(f"{R}/intro_base.json"))
print("== sets")
for k, r in sorted(rows.items()):
    sa = [c for c in r["cands"] if c[0] == "season_audio"]
    idb = [c for c in r["cands"] if c[0] == "introdb"]
    if not sa or not idb:
        continue
    s, i = sa[0], idb[0]
    if abs(s[2] - i[2]) <= 5000:
        continue
    tr = r["truth"]
    v_sa = judge_intro((s[1] / 1000, s[2] / 1000), tr) if tr else "no-truth"
    v_i = judge_intro((i[1] / 1000, i[2] / 1000), tr) if tr else "no-truth"
    dlen = (i[2] - i[1]) - (s[2] - s[1])
    print(f"{k.split('|')[0]:10s} {os.path.basename(k.split('|', 1)[1])[:55]:55s} len SA {(s[2] - s[1]) / 1000:6.1f} "
          f"IDB {(i[2] - i[1]) / 1000:6.1f} dlen {dlen / 1000:6.1f} shift {(s[2] - i[2]) / 1000:7.1f}  SA {v_sa:8s} "
          f"IDB {v_i:8s} ours {r['verdict']}")  # fmt: skip

print("== prod")
A = str(LOCAL)
db = sqlite3.connect(f"file:{A}/post.db?mode=ro", uri=True)
for fid, path in db.execute("select id, canonical_path from files where missing_since is null"):
    ev = db.execute(
        "select source, start_ms, end_ms from evidence where file_id=? and type='intro' and source in "
        "('season_audio','introdb','theintrodb')",
        (fid,),
    ).fetchall()
    sa = [e for e in ev if e[0] == "season_audio"]
    idb = [e for e in ev if e[0] != "season_audio"]
    for i in idb:
        if not sa or abs(sa[0][2] - i[2]) <= 5000:
            continue
        s = sa[0]
        dlen = (i[2] - i[1]) - (s[2] - s[1])
        print(f"{os.path.basename(path)[:60]:60s} {i[0]:10s} len SA {(s[2] - s[1]) / 1000:6.1f} IDB "
              f"{(i[2] - i[1]) / 1000:6.1f} dlen {dlen / 1000:6.1f} shift {(s[2] - i[2]) / 1000:7.1f}")  # fmt: skip
