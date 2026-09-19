"""v3 matcher port == fp3 reference, run for run and segment for segment; planted intros are found."""

from __future__ import annotations

import tracemalloc

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S
from media_preview_generator.markers.audio import matcher as m
from tools.markers_eval import fp3_reference as ref

MIN_PTS = int(ref.MIN_S / ref.POINT_S)


def _key(e: int) -> str:
    return f"/tv/Show (2020)/Season 01/Show (2020) - S01E{e:02d}.mkv"


def _season(seed: int, *, episodes: int = 4, length: int = 900, intro_len: int = 200, silence: int = 40):
    """Random fingerprints sharing one intro (with single-bit noise) and one repeated-value block (ties)."""
    rng = np.random.default_rng(seed)
    intro = rng.integers(0, 2**32, size=intro_len, dtype=np.uint64).astype("<u4")
    silence_value = np.uint32(rng.integers(0, 2**32, dtype=np.uint64))
    out, offsets = {}, {}
    for e in range(1, episodes + 1):
        body = rng.integers(0, 2**32, size=length, dtype=np.uint64).astype("<u4")
        at = int(rng.integers(0, length - intro_len))
        noisy = intro.copy()
        flip = rng.random(intro_len) < 0.3
        noisy[flip] ^= np.uint32(1) << rng.integers(0, 32, size=int(flip.sum())).astype(np.uint32)
        body[at : at + intro_len] = noisy
        if silence:
            s_at = int(rng.integers(0, length - silence))
            body[s_at : s_at + silence] = silence_value
        out[_key(e)] = body
        offsets[_key(e)] = at
    return out, offsets


@pytest.mark.parametrize("seed", range(12))
def test_pair_runs_equal_the_reference(seed):
    fps, _ = _season(seed, episodes=2)
    a, b = fps[_key(1)], fps[_key(2)]
    assert [tuple(r) for r in m.pair_runs(a, b)] == ref.runs(a, b, MIN_PTS)
    assert [tuple(r) for r in m.pair_runs(b, a)] == ref.runs(b, a, MIN_PTS)


@pytest.mark.parametrize(("seed", "episodes"), [(s, n) for s in range(6) for n in (2, 3, 5)])
def test_season_intros_equal_the_reference(seed, episodes):
    fps, _ = _season(100 + seed, episodes=episodes)
    files = sorted(fps)
    expected = {f: row["segment"] for f, row in ref.analyse_points(fps, files).items()}
    got = {f: (tuple(seg) if seg else None) for f, seg in m.season_intros(fps).items()}
    assert got == expected


def test_constant_fingerprints_rank_tied_shifts_like_the_reference():
    # Every shift s and -s ties on count; the reference keeps the one first seen while scanning a.
    a = np.full(300, 12345, dtype="<u4")
    b = np.full(300, 12345, dtype="<u4")
    assert [tuple(r) for r in m.pair_runs(a, b)] == ref.runs(a, b, MIN_PTS)


def test_planted_intro_is_found_where_it_was_planted():
    fps, offsets = _season(7, episodes=3, silence=0)
    result = m.season_intros(fps)
    for key, seg in result.items():
        assert seg is not None
        assert abs(seg.start_s - offsets[key] * POINT_S) <= 4 * POINT_S
        assert abs(seg.end_s - (offsets[key] + 199) * POINT_S) <= 4 * POINT_S
        assert seg.support == 2


def test_one_other_episode_is_enough():
    fps, _ = _season(8, episodes=2, silence=0)
    assert all(seg is not None and seg.support == 1 for seg in m.season_intros(fps).values())


def test_runs_longer_than_120_s_are_not_intros():
    fps, _ = _season(9, episodes=3, intro_len=int(121 / POINT_S) + 2, length=1400, silence=0)
    assert all(seg is None for seg in m.season_intros(fps).values())


def test_runs_shorter_than_8_s_are_ignored():
    fps, _ = _season(10, episodes=2, intro_len=int(8 / POINT_S) - 2, silence=0)
    assert m.pair_runs(fps[_key(1)], fps[_key(2)]) == []


def test_quorum_needs_half_of_the_other_episodes():
    fps, _ = _season(11, episodes=2, silence=0)
    rng = np.random.default_rng(99)
    for e in (3, 4, 5):  # three unrelated episodes: 1 supporter of 4 others < 50%
        fps[_key(e)] = rng.integers(0, 2**32, size=900, dtype=np.uint64).astype("<u4")
    result = m.season_intros(fps)
    assert result[_key(1)] is None and result[_key(2)] is None


def test_empty_fingerprint_matches_nothing():
    a = np.zeros(0, dtype="<u4")
    b = np.arange(500, dtype="<u4")
    assert m.pair_runs(a, b) == [] and m.pair_runs(b, a) == []


def test_file_hits_order_is_the_pair_loop_order():
    calls = []

    def runs_between(x, y):
        calls.append((x, y))
        return [m.Run(1.0, 20.0, 2.0, 21.0)] if (x, y) != ("b", "c") else []

    hits = m.file_hits("b", ["a", "b", "c", "d"], runs_between)
    assert hits == [(2.0, 21.0, "a"), (1.0, 20.0, "d")]
    assert calls == [("a", "b"), ("b", "c"), ("b", "d")]


def test_runs_over_120_s_are_dropped_from_hits():
    hits = m.file_hits("a", ["a", "b"], lambda x, y: [m.Run(0.0, 121.0, 0.0, 121.0), m.Run(0.0, 30.0, 5.0, 35.0)])
    assert hits == [(0.0, 30.0, "b")]


def _random_points(rng: np.random.Generator, n: int) -> np.ndarray:
    return rng.integers(0, 2**32, size=n, dtype=np.uint64).astype("<u4")


def _hard_season(seed: int, episodes: int, length: int = 1800) -> dict[str, np.ndarray]:
    """Fingerprints that reach the reference's ties and edges, inserted in reverse key order.

    Shared stretches of 7-125 s appear 0-2 times per episode, trimmed by up to 6 s, with multi-bit noise around the
    6-bit limit, ±1/±2 value noise (the index's tolerance), dropouts around the 3.5 s gap, and repeated-value blocks
    whose values sit within ±2 of each other (0, 1 and 2**32 - 2, 2**32 - 1).
    """
    rng = np.random.default_rng(seed)
    shared = [_random_points(rng, int(rng.uniform(7, 125) / POINT_S)) for _ in range(5)]
    block_values = (0, 1, 2**32 - 1, 2**32 - 2, int(rng.integers(0, 2**32)))
    out = {}
    for e in range(episodes, 0, -1):
        body = _random_points(rng, length)
        for stretch in shared:
            for _ in range(int(rng.choice([0, 1, 1, 2]))):
                copy = stretch[int(rng.integers(0, 48)) : len(stretch) - int(rng.integers(0, 48))].astype(np.int64)
                masks = rng.integers(0, 2**32, size=(3, len(copy)), dtype=np.int64)
                noisy = rng.random(len(copy)) < 0.15
                copy[noisy] ^= (masks[0] & masks[1] & masks[2])[noisy]
                nudged = rng.random(len(copy)) < 0.1
                copy[nudged] += rng.choice([-2, -1, 1, 2], size=int(nudged.sum()))
                if len(copy) > 100 and rng.random() < 0.5:
                    at = int(rng.integers(0, len(copy) - 40))
                    copy[at : at + int(rng.integers(20, 36))] = rng.integers(0, 2**32, dtype=np.int64)
                at = int(rng.integers(0, length - len(copy)))
                body[at : at + len(copy)] = (copy % 2**32).astype("<u4")
        for _ in range(int(rng.integers(1, 3))):
            size = int(rng.integers(50, 200))
            at = int(rng.integers(0, length - size))
            body[at : at + size] = block_values[int(rng.integers(0, len(block_values)))]
        out[_key(e)] = body
    return out


@pytest.mark.parametrize("seed", range(16))
def test_hard_seasons_equal_the_reference(seed):
    fps = _hard_season(seed, episodes=2 + seed % 5)
    files = sorted(fps)
    expected = {f: row["segment"] for f, row in ref.analyse_points(fps, files).items()}
    got = {f: (tuple(seg) if seg else None) for f, seg in m.season_intros(fps).items()}
    assert got == expected
    for x in files:
        for y in files:
            if x != y:
                assert [tuple(r) for r in m.pair_runs(fps[x], fps[y])] == ref.runs(fps[x], fps[y], MIN_PTS)


def test_long_shared_silences_are_matched_in_bounded_memory():
    # Chromaprint gives silence one constant value (0x256df977 for algorithm 1), so two long silent openings pair
    # every silent point of one with every silent point of the other: 1500 each is 2.25 million value matches.
    fps, _ = _season(12, episodes=2, length=2400, silence=0)
    a, b = fps[_key(1)], fps[_key(2)]
    a[300:1800] = 0x256DF977
    b[700:2200] = 0x256DF978  # within ±1 and 4 bits of a's value
    tracemalloc.start()
    try:
        got = m.pair_runs(a, b)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert [tuple(r) for r in got] == ref.runs(a, b, MIN_PTS)
    assert peak < 64 * 2**20


@pytest.mark.parametrize("seed", range(4))
def test_hard_seasons_equal_the_reference_when_matches_are_expanded_in_small_chunks(monkeypatch, seed):
    monkeypatch.setattr(m, "_MATCHES_PER_CHUNK", 97)
    fps = _hard_season(seed, episodes=2 + seed % 5)
    files = sorted(fps)
    for x in files:
        for y in files:
            if x != y:
                assert [tuple(r) for r in m.pair_runs(fps[x], fps[y])] == ref.runs(fps[x], fps[y], MIN_PTS)


# The crafted pairs below first pin the reference's runs to the planted positions: two random points within 6 bits
# of each other can stretch a run by chance, and the seeds are ones where that doesn't happen.


def test_more_than_40_equally_good_shifts_keep_the_40_seen_first():
    rng = np.random.default_rng(3)
    stretches = [_random_points(rng, 70) for _ in range(45)]
    a = np.concatenate(stretches)
    b = np.concatenate(stretches[::-1])  # 45 shifts, 70 values each
    expected = ref.runs(a, b, MIN_PTS)
    assert sorted((round(r[0] / POINT_S), round(r[1] / POINT_S)) for r in expected) == [
        (70 * k, 70 * k + 69) for k in range(40)
    ]
    assert [tuple(r) for r in m.pair_runs(a, b)] == expected


def test_overlapping_equal_runs_keep_the_shift_seen_first():
    # The reference sees shifts in loop order: position in a, then value offset -2..2, then position in b.
    rng = np.random.default_rng(4)
    one_bit = np.uint32(1 << 10)  # within 6 bits but not within ±2, so the point is in the run but not in the index
    x = _random_points(rng, 70) | np.uint32(2)  # bit 1 set: x - 2 differs from x in that bit only
    y = _random_points(rng, 70) | np.uint32(2)
    a = _random_points(rng, 600)
    a[100:170] = x
    a[400:470] = y
    b = _random_points(rng, 1300)
    # Both copies of x are first seen at a[100]; offset -2 comes before 0, so the later copy (b[500]) wins.
    b[200:270] = x
    b[500:570] = x - np.uint32(2)
    # Equal counts (68); the exact copy is first seen at a[401], the -2 copy at a[402], so the exact copy wins.
    exact_y = y.copy()
    exact_y[[0, 35]] ^= one_bit
    offset_y = y - np.uint32(2)
    offset_y[:2] ^= one_bit
    b[1000:1070] = exact_y
    b[1100:1170] = offset_y
    expected = ref.runs(a, b, MIN_PTS)
    assert sorted(tuple(round(v / POINT_S) for v in r[:3]) for r in expected) == [(100, 169, 500), (400, 469, 1000)]
    assert [tuple(r) for r in m.pair_runs(a, b)] == expected


def test_longest_overlapping_run_wins_over_a_better_aligned_shorter_one():
    rng = np.random.default_rng(3)
    z = _random_points(rng, 200)
    a = _random_points(rng, 600)
    a[100:300] = z
    b = _random_points(rng, 1200)
    b[100:200] = z[:100]  # 100 aligned values, 100-point run
    far = z ^ np.uint32(1 << 20)  # 200 points within 6 bits, only every tenth value aligned
    far[::10] = z[::10]
    b[600:800] = far
    expected = ref.runs(a, b, MIN_PTS)
    assert [(round(r[0] / POINT_S), round(r[1] / POINT_S), round(r[2] / POINT_S)) for r in expected] == [
        (100, 299, 600)
    ]
    assert [tuple(r) for r in m.pair_runs(a, b)] == expected


def test_touching_runs_are_all_kept():
    # y is longest, so it is kept first: x ends where y starts, z starts where y ends.
    rng = np.random.default_rng(5)
    x = _random_points(rng, 70)
    y = _random_points(rng, 80)
    z = _random_points(rng, 70)
    y[0] = x[-1]
    z[0] = y[-1]
    a = _random_points(rng, 600)
    a[100:170] = x
    a[169:249] = y
    a[248:318] = z
    b = _random_points(rng, 1400)
    b[300:370] = x
    b[800:880] = y
    b[1100:1170] = z
    expected = ref.runs(a, b, MIN_PTS)
    assert sorted((round(r[0] / POINT_S), round(r[1] / POINT_S)) for r in expected) == [
        (100, 169),
        (169, 248),
        (248, 317),
    ]
    assert [tuple(r) for r in m.pair_runs(a, b)] == expected


def test_run_exactly_8_s_long_and_gap_exactly_3_5_s_are_kept():
    rng = np.random.default_rng(5)
    a = _random_points(rng, 800)
    b = _random_points(rng, 1600)
    b[200:265] = a[100:165]  # first to last point: 64 points, the 8 s minimum
    b[1000:1040] = a[400:440]
    b[1040:1067] = ~a[440:467]  # 27 unmatched points: 28 steps between matches, the 3.5 s maximum gap
    b[1067:1107] = a[467:507]
    expected = ref.runs(a, b, MIN_PTS)
    assert sorted((round(r[0] / POINT_S), round(r[1] / POINT_S)) for r in expected) == [(100, 164), (400, 506)]
    assert [tuple(r) for r in m.pair_runs(a, b)] == expected


_CRAFTED_RUNS = {
    "a run of exactly 120 s is an intro": (["a", "b", "c"], {("a", "b"): [(10.0, 130.0, 12.0, 132.0)]}),
    "equal keys keep the first hit": (
        ["a", "b", "c"],
        {("a", "b"): [(10.0, 30.0, 10.0, 30.0)], ("a", "c"): [(50.0, 70.0, 50.0, 70.0)]},
    ),
    "edges exactly 4 s apart cluster": (
        ["a", "b", "c"],
        {("a", "b"): [(10.0, 40.0, 10.0, 40.0)], ("a", "c"): [(14.0, 44.0, 14.0, 44.0)]},
    ),
    "exactly 15 s beats more support": (
        ["a", "b", "c"],
        {
            ("a", "b"): [(100.0, 115.0, 100.0, 115.0), (200.0, 214.0, 200.0, 214.0)],
            ("a", "c"): [(200.5, 214.5, 200.5, 214.5)],
        },
    ),
    "a partner supports once however many runs": (
        ["a", "b", "c"],
        {
            ("a", "c"): [(10.0, 40.0, 300.0, 330.0), (100.0, 130.0, 301.0, 331.0)],
            ("b", "c"): [(0.0, 20.0, 500.0, 520.0)],
        },
    ),
    "length is checked on the earlier file's side": (["a", "b", "c"], {("a", "b"): [(10.0, 40.0, 10.0, 131.0)]}),
    "two supporters of five others is short of the quorum": (
        ["a", "b", "c", "d", "e", "f"],
        {("a", "b"): [(10.0, 40.0, 10.0, 40.0)], ("a", "c"): [(11.0, 41.0, 11.0, 41.0)]},
    ),
}


@pytest.mark.parametrize(("files", "runs"), list(_CRAFTED_RUNS.values()), ids=list(_CRAFTED_RUNS))
def test_hit_ranking_equals_the_reference_on_crafted_runs(monkeypatch, files, runs):
    monkeypatch.setattr(ref, "runs", lambda a, b, min_pts: runs.get((a, b), []))
    expected = {f: row["segment"] for f, row in ref.analyse_points({f: f for f in files}, files).items()}

    def runs_between(x, y):
        return [m.Run(*r) for r in runs.get((x, y), [])]

    got = {f: m.intro_for(m.file_hits(f, files, runs_between), len(files) - 1) for f in files}
    assert {f: (tuple(seg) if seg else None) for f, seg in got.items()} == expected
