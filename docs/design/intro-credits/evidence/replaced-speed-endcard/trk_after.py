"""Tomb Raider King S01E12 with this worktree's season step and end-picture check (end card, v2): prod fingerprints of
its folder (post.db), the pictures decoded from /data_16tb (CPU, read-only)."""

import sqlite3
import sys

import numpy as np

W = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, W)
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio import season as S  # noqa: E402

DB = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/audit0925/post.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
pts = {}
for fid in (1571, 1572, 1573, 1574, 1899):
    (path,) = con.execute("select canonical_path from files where id=?", (fid,)).fetchone()
    (blob,) = con.execute("select points from fingerprints where file_id=? and window='intro'", (fid,)).fetchone()
    pts[path] = np.frombuffer(blob, dtype="<u4").copy()
    if fid == 1899:
        target = path
files = sorted(pts)
reader = end_picture.Reader(ffmpeg="/usr/bin/ffmpeg")


def passes(candidate):
    shares = [
        reader.share(
            target, h.partner, candidate.segment.start_s, candidate.segment.end_s, h.partner_start_s - h.start_s
        )
        for h in end_picture.partners(candidate.members)
    ]
    print("   end-picture shares", shares, "CHECK_VERSION", end_picture.CHECK_VERSION)
    return end_picture.passes(shares)


print("season_intro:", S.season_intro(target, files, pts, lambda a, b: S.season_pair_runs(pts[a], pts[b]),
                                      end_picture_passes=passes))  # fmt: skip
