"""Shared helpers for the 2026-09-27 credits-accuracy lane (evidence, not shipped).

The scripts sit beside their local-only data (every *.json here and local/, gitignored: they name library paths).
local/audit is the 2026-09-27 audit's own folder: its report, its work files and its copy of sflix's markers.db.
The tree under test is $CREDFIX_WORK (default: the repo this folder is in); the base tree is ./base, an export of the
commit measured against (git archive 4a34687 media_preview_generator tools | tar -x -C base).
"""

import json
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUDIT = HERE / "local" / "audit"
WORK = AUDIT / "work"
PROD_DB = AUDIT / "db" / "markers.db"
MAIN_EVIDENCE = HERE.parent
REPO = HERE.parents[4]
WORK_TREE = os.environ.get("CREDFIX_WORK", str(REPO))


def prod_db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)


def load(name: str):
    return json.loads((WORK / name).read_text())


def sample_by_id() -> dict:
    return {x["sid"]: x for x in load("sample.json")}


def census_by_id() -> dict:
    return {x["cid"]: x for x in load("census.json")}


def show(db: sqlite3.Connection, fid: int) -> None:
    f = db.execute("select canonical_path,duration_ms,season_key,is_movie from files where id=?", (fid,)).fetchone()
    print("  ", f)
    for r in db.execute(
        "select source,origin,label,type,start_ms,end_ms,confidence,substr(detail,1,60),fetched_at from evidence "
        "where file_id=? order by type,source",
        (fid,),
    ):
        print("    ", r)
    for r in db.execute("select * from decisions where file_id=?", (fid,)):
        print("    D", r)
    for r in db.execute("select * from markers where file_id=?", (fid,)):
        print("    M", r)
    for r in db.execute("select * from version_reruns where file_id=?", (fid,)):
        print("    V", r)
