"""Prod patterns for rules (a)-(e), from the base replay and the post.db evidence."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import re
import sqlite3
import sys

R = str(LOCAL)
A = str(LOCAL)
rows = json.load(open(sys.argv[1] if len(sys.argv) > 1 else f"{R}/prod_base.json"))
db = sqlite3.connect(f"file:{A}/post.db?mode=ro", uri=True)


def ev(fid, t):
    return db.execute(
        "select source, start_ms, end_ms, substr(detail,1,12) from evidence where file_id=? and type=? order by source",
        (fid, t),
    ).fetchall()


def dur(fid):
    return db.execute("select duration_ms from files where id=?", (fid,)).fetchone()[0]


def name(p):
    m = re.search(r"S\d\dE\d\d", p)
    return (p.split("/")[-3][:28] + " " + m.group(0)) if m else os.path.basename(p)[:40]


def show(k, r):
    fid, t = k.split(":")
    fid = int(fid)
    print(f"  {name(r['path']):40s} {r['status']:12s} {r['reason'][:60]:60s} {r['marker']}  dur {dur(fid)}")
    for e in ev(fid, t):
        print("        ", e)


print("== (a) intro decided by SkipDB alone")
for k, r in rows.items():
    if k.endswith(":intro") and r["status"] == "decided" and r["reason"] == "single source (skipdb)":
        show(k, r)

print("== (b) credits decided by chapters with SkipDB > 10 s away")
for k, r in rows.items():
    if not k.endswith(":credits") or r["status"] != "decided" or "chapters" not in r["marker"][2]:
        continue
    fid = int(k.split(":")[0])
    sk = [e for e in ev(fid, "credits") if e[0] in ("skipdb", "introdb", "theintrodb", "server_markers")]
    if any(abs(e[1] - r["marker"][0]) > 10_000 for e in sk):
        show(k, r)

print("== (c) online answers ending past the end of the file")
for fid, src, t, s, e, d in db.execute(
    "select f.id, e.source, e.type, e.start_ms, e.end_ms, f.duration_ms from evidence e join files f on f.id=e.file_id "
    "where f.missing_since is null and e.end_ms > f.duration_ms + 2000 and e.type is not null"
):
    k = f"{fid}:{t}"
    r = rows.get(k)
    print(f"  {src:12s} {t:8s} over {(e - d) / 1000:7.1f}s  {name(db.execute('select canonical_path from files where id=?', (fid,)).fetchone()[0])}"
          f"  -> {r and r['status']} {r and r['reason'][:60]}")  # fmt: skip

print("== needs review")
for k, r in rows.items():
    if r["status"] == "needs_review" and r["stored_status"] == "needs_review":
        show(k, r)
