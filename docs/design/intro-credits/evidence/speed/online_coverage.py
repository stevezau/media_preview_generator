"""How many files of the three regression sets have IntroDB/TheIntroDB evidence in prod's markers.db (read only)."""

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
os.environ.setdefault(
    "MARKERS_EVAL_EVIDENCE", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
)
from tools.markers_eval.data import load_v3_results  # noqa: E402

HERE = Path(__file__).parent
db = sqlite3.connect(f"file:{HERE.parent / 'prod_markers.db'}?mode=ro", uri=True)
rows = db.execute(
    "select f.canonical_path, e.source, e.type, e.start_ms, e.end_ms from evidence e join files f on f.id=e.file_id "
    "where e.source in ('introdb','theintrodb') and e.type is not null"
).fetchall()
online = {}
for path, source, mtype, start, end in rows:
    online.setdefault(os.path.basename(path), []).append((source, mtype, start, end))
sets = {
    "lab118": [e.file for e in load_v3_results() if e.truth_intro],
    "heldout175": list(json.load(open(HERE / "heldout_truth.json"))),
    "accused": [
        f
        for f, t in json.load(
            open(
                "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/"
                "evidence/e"
                "val/accused_truth.json"
            )
        ).items()
    ],
}
for name, files in sets.items():
    have = [f for f in files if os.path.basename(f) in online]
    print(name, len(files), "with online intro/credits evidence in prod:", len(have))
print("prod files with online evidence", len(online))
