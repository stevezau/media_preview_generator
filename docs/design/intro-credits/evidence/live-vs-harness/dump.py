"""One file's rows in a markers.db: dump.py <db> <path substring> [type]."""

import sqlite3
import sys

db, pat = sys.argv[1], sys.argv[2]
typ = sys.argv[3] if len(sys.argv) > 3 else None
c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for f in c.execute("select * from files where canonical_path like ?", (f"%{pat}%",)):
    fid = f["id"]
    print(
        "##",
        fid,
        f["canonical_path"][-110:],
        "season",
        f["season_key"],
        "dur",
        f["duration_ms"],
        "missing",
        f["missing_since"],
    )

    def q(sql, *args, fid=fid):
        return [dict(r) for r in c.execute(sql, (fid, *args))]

    for r in q(
        "select source,origin,label,type,start_ms,end_ms,confidence,substr(detail,1,200) detail,fetched_at from evidence where file_id=? order by source"
    ):
        if typ and r["type"] not in (typ, None):
            continue
        print("  ev", r)
    print("  vers", [(r["source"], r["version"]) for r in q("select * from evidence_versions where file_id=?")])
    for r in q("select * from decisions where file_id=?"):
        if typ and r["type"] != typ:
            continue
        print("  dec", r)
    for r in q("select * from markers where file_id=?"):
        print("  mk", r)
    for r in q("select * from detector_runs where file_id=?"):
        print("  run", r)
    for r in q("select server_id, status, markers_json, updated_at from publish_state where file_id=?"):
        print("  pub", r)
