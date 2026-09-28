"""Each partner's intro-length runs with one episode, as the app's season step reads them from a markers.db copy
(usage: DB=<copy> [OWN_FOLDER=1] season_members.py <path substring>...): which episodes support its opening."""

import os
import re
import sys

import season_repro as R  # noqa: I001 - puts the tree under test first on sys.path


def episode_of(path: str) -> str:
    m = re.search(r"S(\d+)E(\d+)", os.path.basename(path))
    return m.group(0) if m else os.path.basename(path)[:20]


def show(target: str) -> None:
    rec = R.store.get_file(target)
    eps = R.group_of(target)
    records, points = {target: rec}, {target: R.S._cached_points(R.ctx, rec)}
    for p in eps:
        m = R.store.get_file(p)
        if m is not None and (pts := R.S._cached_points(R.ctx, m)) is not None:
            records[p], points[p] = m, pts
    matching = R.S._matching(R.ctx, rec, records, points, group_size=len(eps), previous_files=None,
                             retimed=lambda member, factor: None)  # fmt: skip
    files = matching.files
    print("##", episode_of(target), "disk", target.split("/")[1])
    for other in files:
        if other == target:
            continue
        a, b = (target, other) if files.index(target) < files.index(other) else (other, target)
        runs = R.S._cached_runs(R.ctx, records, a, b, matching.points[a], matching.points[b],
                                matching.clock.pair_version(a, b))  # fmt: skip
        desc = []
        for run in runs:
            s, e, ps, pe = run if a == target else (run[2], run[3], run[0], run[1])
            desc.append(f"{s:6.1f}-{e:6.1f}~{ps:6.1f}-{pe:6.1f}")
        print(f"   {episode_of(other)} {other.split('/')[1]:11s} runs: {'; '.join(desc)[:150]}")


for pat in sys.argv[1:]:
    (path,) = R.store._conn.execute(
        "select canonical_path from files where canonical_path like ? and missing_since is null", (f"%{pat}%",)
    ).fetchone()
    show(path)
