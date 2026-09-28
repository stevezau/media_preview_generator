"""Recompute, with the tree's own end-picture check, every stored share of check version 3 below the pass line in a
markers.db copy, and store the new share there (usage: CODE=tree reshare_db.py <copy of markers.db>). Check version 4
only makes frames more alike (a flat frame beside one that isn't is compared by correlation), so a passing share stays
passing. Decodes on storage's GPU (the one nearest-pixel scaler gives the same frames on every vendor), nice 19."""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE  # noqa: E402

sys.path.insert(0, CODE)
from media_preview_generator.markers.audio import end_picture as E  # noqa: E402

db = sys.argv[1]
c = sqlite3.connect(db)
reader = E.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
rows = c.execute(
    "select e.file_a, e.file_b, e.start_ms, e.end_ms, e.offset_ms, e.share, fa.canonical_path, fb.canonical_path "
    "from season_end_pictures e join files fa on fa.id=e.file_a join files fb on fb.id=e.file_b "
    "where e.check_version=3 and e.share < 0.75"
).fetchall()
print("recompute", len(rows), flush=True)
changed = []
for a, b, s, e, off, share, pa, pb in rows:
    if not (os.path.exists(pa) and os.path.exists(pb)):
        continue
    try:
        v = reader.share(pa, pb, s / 1000, e / 1000, off / 1000)
    except Exception as exc:
        print("  fail", os.path.basename(pa)[:40], exc, flush=True)
        continue
    if v is not None and abs(v - share) > 1e-9:
        changed.append((a, b, s, e, off, share, v))
        c.execute(
            "update season_end_pictures set share=? where file_a=? and file_b=? and start_ms=? and end_ms=? and offset_ms=?",
            (v, a, b, s, e, off),
        )
        print(
            f"  {a}->{b} {os.path.basename(pa)[:55]} {share:.2f} -> {v:.2f}{'  PASS' if v >= 0.75 else ''}", flush=True
        )
c.commit()
print("changed", len(changed), "now passing", sum(1 for x in changed if x[-1] >= 0.75))
print("targets", sorted({x[0] for x in changed}))
