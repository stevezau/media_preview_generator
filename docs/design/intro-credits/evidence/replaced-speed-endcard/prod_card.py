"""Production's end-picture checks (post-#312 markers.db snapshot, read-only) that fail today, measured again with this
worktree's check (v2, the end card): which clusters would pass now, with the file's other intro evidence beside them.
NVIDIA decode of /data* (read-only)."""

import collections
import os
import sqlite3
import sys

W = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, W)
from media_preview_generator.markers.audio import end_picture as EP  # noqa: E402

DB = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/audit0925/post.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
path_of = dict(con.execute("select id, canonical_path from files"))
reader = EP.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
clusters = collections.defaultdict(list)
for a, b, start, end, off, share in con.execute(
    "select file_a, file_b, start_ms, end_ms, offset_ms, share from season_end_pictures"
):
    clusters[(a, start, end)].append((b, off, share))
flips = 0
for (a, start, end), partners in sorted(clusters.items()):
    old = [share for _b, _off, share in partners]
    if EP.passes(old):
        continue
    target = path_of[a]
    new = []
    for b, off, share in partners:
        if not (os.path.exists(target) and os.path.exists(path_of[b])):
            new.append(None)
            continue
        new.append(share if share is not None and share >= 1.0 else
                   reader.share(target, path_of[b], start / 1000, end / 1000, off / 1000))  # fmt: skip
    evidence = con.execute(
        "select source, start_ms, end_ms from evidence where file_id=? and type='intro'", (a,)
    ).fetchall()
    flipped = EP.passes(new)
    flips += flipped
    print(f"{'PASSES NOW' if flipped else 'still fails'} {os.path.basename(target)[:70]:70} {start / 1000:6.1f}-"
          f"{end / 1000:6.1f} old {old} new {[None if s is None else round(s, 2) for s in new]} evidence {evidence}")  # fmt: skip
print("clusters that pass now:", flips)
