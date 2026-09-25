"""Tomb Raider King S01E12 (file 1899): the season step's clusters and end-picture partners, from prod fingerprints."""

import sqlite3
import sys

import numpy as np

W = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, W)
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio import season as S  # noqa: E402
from media_preview_generator.markers.audio.matcher import file_hits, intro_candidates  # noqa: E402

DB = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/audit0925/post.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
ids = [int(x) for x in sys.argv[1:]] or [1571, 1572, 1573, 1574, 1899]
target_id = ids[-1]
paths, pts = {}, {}
for fid in ids:
    (path,) = con.execute("select canonical_path from files where id=?", (fid,)).fetchone()
    (blob,) = con.execute("select points from fingerprints where file_id=? and window='intro'", (fid,)).fetchone()
    paths[fid] = path
    pts[path] = np.frombuffer(blob, dtype="<u4").copy()
files = sorted(pts)
target = paths[target_id]
rb = lambda a, b: S.season_pair_runs(pts[a], pts[b])  # noqa: E731
for other in files:
    if other != target:
        a, b = sorted((target, other))
        print("pair", [k for k, v in paths.items() if v == other][0], [tuple(round(x, 1) for x in r) for r in rb(a, b)])
hits = file_hits(target, files, rb)
for c in intro_candidates(hits)[:6]:
    seg = c.segment
    ps = end_picture.partners(c.members)
    print(
        f"cand {seg.start_s:.1f}-{seg.end_s:.1f} support {seg.support} needs_core {S.needs_dense_core(seg)} "
        f"core {S.dense_core_s(target, c, pts):.1f}s early {end_picture.is_early(seg.start_s)}"
    )
    for h in c.members:
        pid = [k for k, v in paths.items() if v == h.partner][0]
        print(
            f"    hit {h.start_s:.1f}-{h.end_s:.1f} partner {pid} at {h.partner_start_s:.1f} off {h.partner_start_s - h.start_s:+.2f}"
        )
    print(
        "    end-picture partners:",
        [([k for k, v in paths.items() if v == h.partner][0], round(h.partner_start_s - h.start_s, 3)) for h in ps],
    )
print("season_intro(all pass):", S.season_intro(target, files, pts, rb, end_picture_passes=lambda c: True))
