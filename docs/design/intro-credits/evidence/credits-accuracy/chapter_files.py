"""Every prod file whose credits are decided by chapters (alone or with confirming sources), on disk here, for the
chapter-snap measurement: extra_files.json {path: is_episode}."""

import json
import os

from common import HERE, prod_db

db = prod_db()
rows = db.execute(
    "select f.canonical_path, f.season_key, m.decided_by from markers m join files f on f.id=m.file_id "
    "where m.type='credits' and f.missing_since is null"
).fetchall()
out = {}
for path, season, by in rows:
    if "chapters" not in json.loads(by):
        continue
    if os.path.exists(path):
        out[path] = season is not None
json.dump(out, open(HERE / "extra_files.json", "w"), indent=0)
print(len(rows), "credits markers;", len(out), "chapter-decided on disk;", sum(out.values()), "episodes")
