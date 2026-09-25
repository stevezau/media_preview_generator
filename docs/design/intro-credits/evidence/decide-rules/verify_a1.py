"""Frame sheets for the lone SkipDB intros rule (a) sends to review whose verdict the audit didn't give."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import sqlite3
import subprocess

R = str(LOCAL)
A = str(LOCAL)
SHEET = f"{A}/sheet.py"
db = sqlite3.connect(f"file:{A}/post.db?mode=ro", uri=True)
JOBS = [
    ("%Westworld%S04E05%", "ww_s04e05", [(502.8, "skipdb start"), (591.9, "skipdb end"), (602.7, "audio end")]),
    ("%Westworld%S04E06%", "ww_s04e06", [(507.6, "skipdb end"), (518.4, "audio end")]),
    ("%Somebody Somewhere%S03E07%", "ss_s03e07", [(24.5, "skipdb start"), (45.2, "skipdb end")]),
    ("%Somebody Somewhere%S03E06%", "ss_s03e06", [(66.3, "skipdb start"), (76.7, "skipdb end")]),
]
for like, name, marks in JOBS:
    (path,) = db.execute(
        "select canonical_path from files where canonical_path like ? and missing_since is null", (like,)
    ).fetchone()
    specs = [f"{label} {t:.1f}:{t - 5:.1f}:{t + 5:.1f}:1" for t, label in marks]
    subprocess.run(["/home/data/.venv/bin/python", SHEET, f"{R}/verify/{name}_intro.jpg", path, *specs], check=True)
    print(name)
