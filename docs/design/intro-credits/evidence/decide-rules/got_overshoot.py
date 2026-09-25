"""How far past the file's end IntroDB's Game of Thrones credits end (post.db evidence, read-only)."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import re
import sqlite3

A = str(LOCAL)
db = sqlite3.connect(f"file:{A}/post.db?mode=ro", uri=True)
cols = [r[1] for r in db.execute("pragma table_info(evidence)")]
print(cols)
rows = db.execute(
    "select f.canonical_path, f.duration_ms, e.* from evidence e join files f on f.id = e.file_id "
    "where f.canonical_path like '%Game of Thrones%' and e.source in ('introdb', 'theintrodb') and f.missing_since is null"
).fetchall()
for r in rows:
    path, dur = r[0], r[1]
    rec = dict(zip(cols, r[2:]))
    if rec.get("type") != "credits" or rec.get("end_ms") is None:
        continue
    code = re.search(r"S\d\dE\d\d", path).group(0)
    over = (rec["end_ms"] - dur) / 1000
    if over > 0:
        print(code, rec["source"], f"past by {over:.1f} s")
