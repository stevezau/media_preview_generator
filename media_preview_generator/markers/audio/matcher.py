"""Season intro matcher v3 (spec §5.3): a vectorised port of evidence/detect/fp3.py.

For every pair of episodes: the 40 best alignment shifts (inverted index, values within ±2), runs of points whose
fingerprints differ in ≤ 6 bits with gaps ≤ 3.5 s, at least 8 s long, all non-overlapping runs kept (runs over 120 s are dropped per episode). Per episode: cluster
its runs within ±4 s, rank by (≥ 15 s, supporting episodes, length), and require support from half of the other
episodes (at least one). Orders that break ties are the reference's (tools/markers_eval/fp3_reference.py); the tests
compare the two run for run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import NamedTuple

import numpy as np

from . import POINT_S

MATCHER_VERSION = 3
MAX_BIT_DIFF = 6
MAX_GAP_S = 3.5
MIN_RUN_S = 8.0
MAX_INTRO_S = 120.0
PREFERRED_MIN_S = 15.0
CLUSTER_TOLERANCE_S = 4.0
QUORUM = 0.5
TOP_SHIFTS = 40
_VALUE_SHIFTS = (-2, -1, 0, 1, 2)
_MIN_PTS = int(MIN_RUN_S / POINT_S)
_GAP_PTS = int(MAX_GAP_S / POINT_S)
_MATCHES_PER_CHUNK = 1 << 18
_POPCOUNT8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

Hit = tuple[float, float, str]


class Run(NamedTuple):
    """Matching stretch between episode a and episode b (seconds from each file's start)."""

    a_start_s: float
    a_end_s: float
    b_start_s: float
    b_end_s: float


class IntroSegment(NamedTuple):
    """An episode's intro and how many other episodes support it."""

    start_s: float
    end_s: float
    support: int


def _popcount32(x: np.ndarray) -> np.ndarray:
    return _POPCOUNT8[x.view(np.uint8).reshape(-1, 4)].sum(axis=1)


def _top_shifts(a: np.ndarray, b: np.ndarray) -> list[int]:
    """The best alignment shifts (j - i), most matching values first; ties keep the order first seen scanning ``a``.

    The reference counts shifts in a dict while looping i over ``a``, d over -2..2 and j over ``b``'s positions of
    that value, then sorts stably by count: the key below is that loop's position, so the first sighting of each
    shift is its minimum key, whatever order the pairs are visited in. Pairs are expanded a chunk of ``a``'s positions
    at a time because silence fingerprints to one constant value: two silent 15-minute openings make 53 million
    point pairs, about 5 GB if expanded at once.
    """
    if len(a) == 0 or len(b) == 0:
        return []
    order = np.argsort(b)
    sorted_b = b[order].astype(np.int64)
    a64 = a.astype(np.int64)
    lefts = [np.searchsorted(sorted_b, a64 + d, side="left") for d in _VALUE_SHIFTS]
    matches = [
        np.searchsorted(sorted_b, a64 + d, side="right") - left for d, left in zip(_VALUE_SHIFTS, lefts, strict=True)
    ]
    matches_up_to = np.cumsum(np.sum(matches, axis=0))
    width = len(b) + 1
    # Shift s = j - i lies in (-len(a), len(b)); it is counted at index s + len(a).
    counts = np.zeros(len(a) + len(b), dtype=np.int64)
    first_seen = np.full(len(a) + len(b), np.iinfo(np.int64).max, dtype=np.int64)
    lo = 0
    while lo < len(a):
        before = int(matches_up_to[lo - 1]) if lo else 0
        hi = max(lo + 1, int(np.searchsorted(matches_up_to, before + _MATCHES_PER_CHUNK, side="right")))
        for d_index in range(len(_VALUE_SHIFTS)):
            per_i = matches[d_index][lo:hi]
            total = int(per_i.sum())
            if total == 0:
                continue
            i = np.repeat(np.arange(lo, hi, dtype=np.int64), per_i)
            within = np.arange(total, dtype=np.int64) - np.repeat(np.cumsum(per_i) - per_i, per_i)
            j = order[np.repeat(lefts[d_index][lo:hi], per_i) + within].astype(np.int64)
            slot = j - i + len(a)
            counts += np.bincount(slot, minlength=len(counts))
            np.minimum.at(first_seen, slot, (i * len(_VALUE_SHIFTS) + d_index) * width + j)
        lo = hi
    seen = np.flatnonzero(counts)
    ranked = seen[np.lexsort((first_seen[seen], -counts[seen]))]
    return [int(slot) - len(a) for slot in ranked[:TOP_SHIFTS]]


def pair_runs(a: np.ndarray, b: np.ndarray) -> list[Run]:
    """Every non-overlapping matching run between two fingerprints, longest first on side ``a``.

    Args:
        a: The first episode's points (its path sorts first).
        b: The second episode's points.

    Returns:
        Runs of at least 8 s; overlaps on side ``a`` resolved in favour of the longer run.
    """
    a = np.ascontiguousarray(a, dtype="<u4")
    b = np.ascontiguousarray(b, dtype="<u4")
    found: list[Run] = []
    for s in _top_shifts(a, b):
        a0, b0 = (0, s) if s >= 0 else (-s, 0)
        n = min(len(a) - a0, len(b) - b0)
        if n <= 0:
            continue
        idx = np.flatnonzero(_popcount32(a[a0 : a0 + n] ^ b[b0 : b0 + n]) <= MAX_BIT_DIFF)
        if len(idx) < 2:
            continue
        breaks = np.flatnonzero(np.diff(idx) > _GAP_PTS)
        starts = np.concatenate(([idx[0]], idx[breaks + 1]))
        ends = np.concatenate((idx[breaks], [idx[-1]]))
        for x, y in zip(starts.tolist(), ends.tolist(), strict=True):
            if y - x >= _MIN_PTS:
                found.append(Run((a0 + x) * POINT_S, (a0 + y) * POINT_S, (b0 + x) * POINT_S, (b0 + y) * POINT_S))
    found.sort(key=lambda r: -(r.a_end_s - r.a_start_s))
    kept: list[Run] = []
    for run in found:
        if all(not (run.a_start_s < k.a_end_s and k.a_start_s < run.a_end_s) for k in kept):
            kept.append(run)
    return kept


def file_hits(target: str, files: Sequence[str], runs_between: Callable[[str, str], list[Run]]) -> list[Hit]:
    """One episode's intro-length runs against every other episode, in the reference's pair-loop order.

    Args:
        target: The episode (one of ``files``).
        files: The group, sorted.
        runs_between: Runs for ``(earlier, later)``; asked only in that order.

    Returns:
        ``(start_s, end_s, partner)`` on the target's side, runs over 120 s left out.
    """
    k = list(files).index(target)
    hits: list[Hit] = []
    for earlier in files[:k]:
        hits.extend((r.b_start_s, r.b_end_s, earlier) for r in runs_between(earlier, target) if not _too_long(r))
    for later in files[k + 1 :]:
        hits.extend((r.a_start_s, r.a_end_s, later) for r in runs_between(target, later) if not _too_long(r))
    return hits


def _too_long(run: Run) -> bool:
    return run.a_end_s - run.a_start_s > MAX_INTRO_S


def intro_for(hits: Sequence[Hit], others: int) -> IntroSegment | None:
    """The best supported intro among one episode's hits.

    Args:
        hits: From :func:`file_hits`.
        others: How many other episodes were compared.

    Returns:
        Median start/end of the winning cluster and its support, or None below the quorum.
    """
    tol = CLUSTER_TOLERANCE_S
    best: IntroSegment | None = None
    best_key: tuple | None = None
    for s, e, _ in hits:
        cluster = [(s2, e2, p) for s2, e2, p in hits if abs(s2 - s) <= tol and abs(e2 - e) <= tol]
        support = len({p for _, _, p in cluster})
        key = ((e - s) >= PREFERRED_MIN_S, support, e - s)
        if best_key is None or key > best_key:
            best_key = key
            best = IntroSegment(
                float(np.median([c[0] for c in cluster])), float(np.median([c[1] for c in cluster])), support
            )
    if best is not None and best.support < max(1, QUORUM * others):
        return None
    return best


def season_intros(points: Mapping[str, np.ndarray]) -> dict[str, IntroSegment | None]:
    """Intros for a whole group at once (the harness; the pipeline decides one episode at a time).

    Args:
        points: Fingerprint per episode key.

    Returns:
        Intro (or None) per key.
    """
    files = sorted(points)
    cache: dict[tuple[str, str], list[Run]] = {}

    def runs_between(x: str, y: str) -> list[Run]:
        if (x, y) not in cache:
            cache[(x, y)] = pair_runs(points[x], points[y])
        return cache[(x, y)]

    return {f: intro_for(file_hits(f, files, runs_between), len(files) - 1) for f in files}
