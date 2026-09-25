"""What the end-picture version bump re-decodes on production: the season step of every present TV file, replayed from
the production markers.db snapshot's fingerprints (read-only) with one tree's season step, counting the end-picture
checks its walk asks for.

    nice -n 19 python ep_prod_cost.py <tree> <prod_markers.db>

A check's verdict decides whether the walk goes on to the next cluster, so the count is bracketed: every check passing
(the walk stops at the first early cluster: fewest decodes) and every check failing (it walks every early quorum cluster:
most), plus production's own stored shares as the verdict where it has one (passing otherwise). Same-season groups only
(a lone episode's previous-season check is left out) and every file at its own speed. Nothing is decoded; the only
reads of /data are folder listings and existence checks.
"""

import collections
import os
import sqlite3
import sys

import numpy as np

TREE, DB = sys.argv[1], sys.argv[2]
sys.path.insert(0, TREE)
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio import season as S  # noqa: E402

assert S.__file__.startswith(TREE)
db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
ids, pts = {}, {}
for fid, path, blob in db.execute(
    "SELECT f.id, f.canonical_path, fp.points FROM fingerprints fp JOIN files f ON f.id = fp.file_id "
    "WHERE fp.window = 'intro'"
):
    ids[path] = fid
    pts[path] = np.frombuffer(blob, dtype="<u4").copy()
stored = {
    (a, b, s, e, o): share
    for a, b, s, e, o, share in db.execute(
        "SELECT file_a, file_b, start_ms, end_ms, offset_ms, share FROM season_end_pictures"
    )
}
stored_targets = {a for a, *_ in stored}
tv = [
    path
    for (path,) in db.execute("SELECT canonical_path FROM files WHERE is_movie = 0 AND missing_since IS NULL")
    if path in pts and os.path.exists(path)
]
pairs: dict[tuple[str, str], list] = {}


def runs_between(x, y):
    if (x, y) not in pairs:
        pairs[(x, y)] = S.season_pair_runs(pts[x], pts[y])
    return pairs[(x, y)]


def replay(mode):
    checked_files, shares, windows = set(), set(), set()

    def for_target(target):
        def check(candidate):
            segment = candidate.segment
            times = end_picture.sample_times(segment.start_s, segment.end_s)
            verdicts = []
            checked_files.add(target)
            windows.add((target, round(times[0], 2)))
            for hit in end_picture.partners(candidate.members):
                offset = hit.partner_start_s - hit.start_s
                shares.add((target, hit.partner, round(segment.start_s, 2), round(segment.end_s, 2), round(offset, 2)))
                windows.add((hit.partner, round(times[0] + offset, 2)))
                key = (ids[target], ids[hit.partner], round(segment.start_s * 1000), round(segment.end_s * 1000),
                       round(offset * 1000))  # fmt: skip
                verdicts.append(stored.get(key, 1.0) if mode == "stored" else (1.0 if mode == "pass" else 0.0))
            return end_picture.passes(verdicts)

        return check

    for target in tv:
        group = sorted(f for f in S.season_group(target).episodes if f in pts and len(pts[f]))
        if target in group and len(group) >= 2:
            S.guarded_pick(target, group, pts, runs_between, end_picture_passes=for_target(target))
    return checked_files, shares, windows


print(f"present TV files with a fingerprint: {len(tv)}; stored shares {len(stored)} for {len(stored_targets)} files")
for mode in ("pass", "stored", "fail"):
    files, shares, windows = replay(mode)
    seasons = collections.Counter(os.path.dirname(f) for f in files)
    print(f"{mode:6} files {len(files):4}  seasons {len(seasons):3}  shares {len(shares):5}  decode windows "
          f"{len(windows):5}  (files with a stored share among them: {len({ids[f] for f in files} & stored_targets)})",
          flush=True)  # fmt: skip
print(f"pairs matched: {len(pairs)}")
