"""Reference copy of evidence/detect/fp3.py's v3 intro matcher (spec §5.3), taking fingerprints instead of files.

Pure Python and slow on purpose: media_preview_generator/markers/audio/matcher.py must return exactly what this
returns on the same fingerprints (tests/markers/audio/test_matcher.py, and the Task 6 harness gate on 118 real
episodes). Don't tidy or speed this file up: it is the measured algorithm.
"""

import numpy as np

POINT_S = 4096 / 11025 / 3
MAX_BIT_DIFF = 6
MAX_GAP_S = 3.5
MIN_S = 8
QUORUM = 0.5
_POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def popcount32(x):
    b = x.view(np.uint8).reshape(-1, 4)
    return _POP[b].sum(axis=1)


def runs(a, b, min_pts):
    index = {}
    for i, v in enumerate(b.tolist()):
        index.setdefault(v, []).append(i)
    shifts = {}
    for i, v in enumerate(a.tolist()):
        for d in (-2, -1, 0, 1, 2):
            for j in index.get(v + d, ()):
                shifts[j - i] = shifts.get(j - i, 0) + 1
    gap = int(MAX_GAP_S / POINT_S)
    out = []
    for s, c in sorted(shifts.items(), key=lambda kv: -kv[1])[:40]:  # noqa: B007 - as in fp3.py
        a0, b0 = (0, s) if s >= 0 else (-s, 0)
        n = min(len(a) - a0, len(b) - b0)
        if n <= 0:
            continue
        ok = popcount32(a[a0 : a0 + n] ^ b[b0 : b0 + n]) <= MAX_BIT_DIFF
        idx = np.flatnonzero(ok)
        if len(idx) < 2:
            continue
        br = np.flatnonzero(np.diff(idx) > gap)
        st = np.concatenate(([idx[0]], idx[br + 1]))
        en = np.concatenate((idx[br], [idx[-1]]))
        for x, y in zip(st, en, strict=False):
            if y - x >= min_pts:
                out.append(((a0 + x) * POINT_S, (a0 + y) * POINT_S, (b0 + x) * POINT_S, (b0 + y) * POINT_S))
    out.sort(key=lambda r: -(r[1] - r[0]))
    keep = []
    for r in out:
        if all(not (r[0] < k[1] and k[0] < r[1]) for k in keep):
            keep.append(r)
    return keep


def analyse_points(fps, files, max_len=120):
    hits = {f: [] for f in files}
    min_pts = int(MIN_S / POINT_S)
    for i, fa in enumerate(files):
        for fb in files[i + 1 :]:
            for a0, a1, b0, b1 in runs(fps[fa], fps[fb], min_pts):
                if a1 - a0 > max_len:
                    continue
                hits[fa].append((a0, a1, fb))
                hits[fb].append((b0, b1, fa))
    out = {}
    others = len(files) - 1
    for f in files:
        best = None
        for s, e, _ in hits[f]:
            sup = {p for s2, e2, p in hits[f] if abs(s2 - s) <= 4 and abs(e2 - e) <= 4}
            key = ((e - s) >= 15, len(sup), e - s)
            if best is None or key > best_key:  # noqa: F821 - set on the first pass, as in fp3.py
                best_key = key  # noqa: F841 - read on the next pass, as in fp3.py
                grp = [(s2, e2) for s2, e2, p in hits[f] if abs(s2 - s) <= 4 and abs(e2 - e) <= 4]
                best = (float(np.median([g[0] for g in grp])), float(np.median([g[1] for g in grp])), len(sup))
        if best and best[2] < max(1, QUORUM * others):
            best = None
        out[f] = {"pairs": len(hits[f]), "segment": best}
    return out
