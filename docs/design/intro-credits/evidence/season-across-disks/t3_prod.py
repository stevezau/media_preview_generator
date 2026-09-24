"""Task 3 prod replay: every prod season audio answer (prod_markers.db snapshot, read-only) replayed with THIS
WORKTREE's season step (speed clock included) in three states:

  base   the worktree before task 3: every stretch needs its dense core, the file's own folder only
  fix1   the dense-core exemptions, own folder only
  fix13  the exemptions and the season folders on every disk of the library (``season.season_videos``)

Fingerprints are prod's (own speed); frame rates and retimed fingerprints come from the eval cache (~/.cache/
markers_eval, ffprobe/ffmpeg read-only on /data). End-picture shares are prod's, then earlier decodes, then decoded
on the NVIDIA GPU. A group member without a prod fingerprint is fingerprinted into the eval cache (FILL=1) or left out.

Prints each answer that differs from prod's stored one, per state, and the counts. Writes only t3_prod_* files here.
"""

import collections
import json
import os
import pickle
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKTREE)
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.servers.base import Library, ServerConfig, ServerType  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

assert S.__file__.startswith(WORKTREE), S.__file__

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
LIBRARY = ServerConfig(
    id="plex",
    type=ServerType.PLEX,
    name="Plex",
    enabled=True,
    url="http://plex",
    auth={},
    libraries=[Library("2", "TV Shows", tuple(f"{root}/TV Shows" for root in ROOTS))],
)
FILL = bool(os.environ.get("FILL"))

db = sqlite3.connect(f"file:{os.path.join(HERE, 'prod_markers.db')}?mode=ro", uri=True)
ids, pts = {}, {}
for fid, path, blob in db.execute(
    "select f.id, f.canonical_path, fp.points from fingerprints fp join files f on f.id = fp.file_id "
    "where fp.window = 'intro'"
):
    ids[path] = fid
    pts[path] = np.frombuffer(blob, dtype="<u4").copy()
for fid, path in db.execute("select id, canonical_path from files"):
    ids.setdefault(path, fid)
prod_shares = {
    (a, b, s, e, o): share
    for a, b, s, e, o, share in db.execute(
        "select file_a, file_b, start_ms, end_ms, offset_ms, share from season_end_pictures"
    )
}
answers = {
    path: (None if a is None else (a / 1000, b / 1000))
    for path, a, b in db.execute(
        "select f.canonical_path, e.start_ms, e.end_ms from evidence e join files f on f.id = e.file_id "
        "where e.source = 'season_audio' and f.missing_since is null and f.is_movie = 0"
    )
}

cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
reader = end_picture.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
DCACHE = os.path.join(HERE, "t3_prod_decoded.pkl")
decoded = {}
for seed in (os.path.join(HERE, "kq_decoded_shares.pkl"), DCACHE):
    if os.path.exists(seed):
        decoded.update(pickle.load(open(seed, "rb")))
new_decodes = 0
filled: list[str] = []
missing_members: set[str] = set()


def points_of(path):
    if path in pts and len(pts[path]):
        return pts[path]
    if FILL:
        pts[path] = cache.points(path)
        filled.append(path)
        return pts[path]
    missing_members.add(path)
    return None


def file_id(path):
    return ids.get(path, path)


def passes(target, candidate):
    global new_decodes
    got = []
    for hit in end_picture.partners(candidate.members):
        off = hit.partner_start_s - hit.start_s
        seg = candidate.segment
        key = (
            file_id(target),
            file_id(hit.partner),
            round(seg.start_s * 1000),
            round(seg.end_s * 1000),
            round(off * 1000),
        )
        if key in prod_shares:
            got.append(prod_shares[key])
            continue
        if key not in decoded:
            try:
                decoded[key] = reader.share(target, hit.partner, seg.start_s, seg.end_s, off)
            except end_picture.ReadFailedError:
                decoded[key] = None
            new_decodes += 1
        got.append(decoded[key])
    return end_picture.passes(got)


REAL_NEEDS_CORE = S.needs_dense_core
runs_cache = {}


def replay(target, *, merged, exempt):
    videos = S.season_videos(target, [LIBRARY]) if merged else None
    episodes = S.season_group(target, videos).episodes
    fps = {f: p for f in episodes if (p := points_of(f)) is not None and len(p)}
    files = sorted(fps)
    if target not in files or len(files) < 2:
        return None, len(files)
    clock = S.season_clock({f: cache.speed(f) for f in files})
    for f, factor in clock.factors.items():
        fps[f] = cache.retimed(f, factor)

    def runs_between(a, b):
        key = (a, b, clock.factors.get(a), clock.factors.get(b))
        if key not in runs_cache:
            runs_cache[key] = S.season_pair_runs(fps[a], fps[b])
        return runs_cache[key]

    S.needs_dense_core = REAL_NEEDS_CORE if exempt else (lambda _segment: True)
    picked = S.guarded_pick(
        target,
        files,
        fps,
        runs_between,
        end_picture_passes=lambda c: passes(target, S.in_own_times(c, target, clock.factors)),
    )
    if picked is not None and S._mostly_silence(fps[target], picked):
        picked = None
    return (None if picked is None else S.in_own_time(picked, clock.factors.get(target))), len(files)


def same(seg, prod):
    if seg is None or prod is None:
        return seg is None and prod is None
    return abs(seg.start_s - prod[0]) < 0.6 and abs(seg.end_s - prod[1]) < 0.6


def show(seg):
    return None if seg is None else (round(seg.start_s, 1), round(seg.end_s, 1), seg.support)


if __name__ == "__main__":
    states = {"base": dict(merged=False, exempt=False), "fix1": dict(merged=False, exempt=True),
              "fix13": dict(merged=True, exempt=True)}  # fmt: skip
    counts = {state: collections.Counter() for state in states}
    rows = []
    for path, prod in sorted(answers.items()):
        if path not in pts or not os.path.exists(path):
            for state in states:
                counts[state]["not replayable"] += 1
            continue
        out = {state: replay(path, **kwargs) for state, kwargs in states.items()}
        for state, (seg, _size) in out.items():
            counts[state]["agree" if same(seg, prod) else "differ"] += 1
        row = {
            "path": path,
            "prod": prod,
            **{state: (show(seg), size) for state, (seg, size) in out.items()},
        }
        rows.append(row)
        changed = [s for s in states if not same(out[s][0], prod)]
        if changed or show(out["base"][0]) != show(out["fix13"][0]):
            name = re.sub(r" \[.*", "", os.path.basename(path))[:70]
            print(f"{name:70} prod {prod} | base {row['base']} | fix1 {row['fix1']} | fix13 {row['fix13']}", flush=True)
    for state, c in counts.items():
        print(state, dict(c))
    print("group members without a prod fingerprint:", len(missing_members), "filled:", len(filled))
    for path in sorted(missing_members)[:40]:
        print("   missing", path)
    print("new decodes", new_decodes)
    pickle.dump(decoded, open(DCACHE, "wb"))
    json.dump(rows, open(os.path.join(HERE, "t3_prod_rows.json"), "w"), indent=1, default=str)
