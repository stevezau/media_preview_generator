import sqlite3
import sys

from common import DB_NOW, DB_PRE320, DB_PRE323

DBS = [("pre320", DB_PRE320), ("pre323", DB_PRE323), ("after", DB_NOW)]
typ = sys.argv[1]
for pat in sys.argv[2:]:
    print("###", pat, typ)
    for name, db in DBS:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for fid, path, miss in c.execute(
            "select id, canonical_path, missing_since from files where canonical_path like ?", (f"%{pat}%",)
        ):
            d = c.execute(
                "select status, reason, proposed_start_ms, proposed_end_ms, decided_by from decisions where file_id=? and type=?",
                (fid, typ),
            ).fetchone()
            m = c.execute(
                "select start_ms,end_ms,decided_by from markers where file_id=? and type=?", (fid, typ)
            ).fetchone()
            ev = c.execute(
                "select source,start_ms,end_ms,label from evidence where file_id=? and type=?", (fid, typ)
            ).fetchall()
            vers = dict(c.execute("select source, version from evidence_versions where file_id=?", (fid,)).fetchall())
            print(f"  {name:6s} {fid} {path.split('/')[1]:10s} miss={bool(miss)} dec={d} mk={m}")
            print(f"         ev={ev} v(ct)={vers.get('credits_text')} v(sa)={vers.get('season_audio')}")
