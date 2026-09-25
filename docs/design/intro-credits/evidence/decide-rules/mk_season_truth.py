"""A season-truth input for Westworld S04 and Somebody Somewhere S03 (prod paths from post.db), so the harness runs the
app's season step on them. Truth is the audit's where it was frame-checked, else a placeholder (only answers are read)."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import sqlite3

A = str(LOCAL)
R = str(LOCAL)
db = sqlite3.connect(f"file:{A}/post.db?mode=ro", uri=True)
KNOWN = {"S04E01": [563.0, 660.0], "S04E08": [217.5, 317.0]}
out = {}
for (path,) in db.execute(
    "select canonical_path from files where missing_since is null and (canonical_path like '%Westworld%Season 04%' "
    "or canonical_path like '%Somebody Somewhere%Season 03%') order by canonical_path"
):
    code = next((k for k in KNOWN if k in path), None)
    out[path] = KNOWN[code] if code else [0.0, 1.0]
json.dump(out, open(f"{R}/season_truth_ww4_ss3.json", "w"), indent=0)
print(len(out))
