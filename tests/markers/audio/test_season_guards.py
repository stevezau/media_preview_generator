"""Season audio's guards against a network ident or a cold-open music bed before the title card (spec §5.3, §14
2026-09-24), on synthetic seasons shaped like "Accused: Guilty or Innocent" (A&E), where the v3 matcher took the A&E
logo, the logo plus the cold-open music merged across its 3.5 s gap bridge, or the music for 13 of 15 intros.

Each case runs the real matcher over planted fingerprints and pins both what the matcher alone answers (so the case
really is the failure it models) and what the guarded season step answers.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S, season
from media_preview_generator.markers.audio.matcher import Hit, IntroCandidate, IntroSegment, file_hits, intro_for

EPISODES = 5  # a quorum of 2 of the 4 others
TITLE_AT = (70.0, 85.0, 100.0, 110.0, 120.0)  # the title card floats after a cold open of varying length


def _pts(seconds: float) -> int:
    return round(seconds / POINT_S)


def _noise(seed: int, size: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 2**32, size=size, dtype=np.uint64).astype("<u4")


@dataclass(frozen=True)
class Plant:
    """Shared audio planted in the episodes: at one start for all, or one start per episode (None: not in it). With
    ``every`` k only one point in k is shared (music under dialogue), and ``hole`` blanks points (start, count)."""

    seed: int
    at: float | tuple[float | None, ...]
    length_s: float
    every: int = 1
    hole: tuple[int, int] | None = None

    def into(self, body: np.ndarray, episode: int) -> None:
        start = self.at if isinstance(self.at, float) else self.at[episode]
        if start is None:
            return
        shared = np.arange(0, _pts(self.length_s), self.every)
        if self.hole:
            shared = shared[(shared < self.hole[0]) | (shared >= self.hole[0] + self.hole[1])]
        body[_pts(start) + shared] = _noise(self.seed, _pts(self.length_s))[shared]


def _season(*plants: Plant, length_s: float = 240.0) -> tuple[list[str], dict[str, np.ndarray]]:
    files = [f"/tv/Accused (2020)/Season 03/Accused (2020) - S03E{e:02d}.mkv" for e in range(1, EPISODES + 1)]
    points = {}
    for i, path in enumerate(files):
        body = _noise(1_000 + i, _pts(length_s))
        for plant in plants:
            plant.into(body, i)
        points[path] = body
    return files, points


class Pictures:
    """A fake end-picture check: the pictures differ for candidates starting near one of ``differ_at``."""

    def __init__(self, *differ_at: float) -> None:
        self.differ_at = differ_at
        self.asked: list[float] = []

    def __call__(self, candidate: IntroCandidate) -> bool:
        start = candidate.segment.start_s
        self.asked.append(start)
        return not any(abs(start - at) < 1.0 for at in self.differ_at)


def _runs(points):
    cache = {}

    def runs_between(a, b):
        if (a, b) not in cache:
            cache[(a, b)] = season.season_pair_runs(points[a], points[b])
        return cache[(a, b)]

    return runs_between


def _answers(files, points, pictures):
    runs_between = _runs(points)
    matcher = [intro_for(file_hits(f, files, runs_between), len(files) - 1) for f in files]
    guarded = [season.season_intro(f, files, points, runs_between, end_picture_passes=pictures) for f in files]
    return matcher, guarded


def _near(segment: IntroSegment | None, start: float, end: float) -> bool:
    return segment is not None and abs(segment.start_s - start) < 1.0 and abs(segment.end_s - end) < 1.0


TITLE_CARD = Plant(2, TITLE_AT, 12.0)


class TestAccusedShapes:
    def test_a_logo_at_the_file_start_shorter_than_10_s_is_passed_over_for_the_title_card(self):
        files, points = _season(Plant(1, 0.0, 9.5), Plant(2, TITLE_AT, 8.6))
        pictures = Pictures()
        matcher, guarded = _answers(files, points, pictures)
        assert all(_near(seg, 0.0, 9.4) for seg in matcher)  # the matcher alone: the logo, longer than the card
        assert all(_near(seg, at, at + 8.5) for seg, at in zip(guarded, TITLE_AT, strict=True))
        assert pictures.asked == []  # too short at the start: no decode, and the card isn't early

    def test_a_logo_and_cold_open_music_merged_by_the_gap_bridge_have_no_dense_core(self):
        # 6 s logo, 2 s of nothing shared, then 9 s of music under dialogue (one point in 6 matching): the matcher's
        # 3.5 s bridge makes one 17 s run, preferred over the 12 s title card.
        logo, music = Plant(1, 0.0, 6.0), Plant(3, 8.0, 9.0, every=6)
        files, points = _season(logo, music, TITLE_CARD)
        pictures = Pictures()
        matcher, guarded = _answers(files, points, pictures)
        assert all(_near(seg, 0.0, 16.9) for seg in matcher)
        assert all(_near(seg, at, at + 11.9) for seg, at in zip(guarded, TITLE_AT, strict=True))
        assert pictures.asked == []  # the dense core fails first: its end picture is never decoded

    def test_a_music_bed_under_the_cold_open_ends_on_different_pictures(self):
        files, points = _season(Plant(3, 12.0, 16.0), TITLE_CARD)
        pictures = Pictures(12.0)
        matcher, guarded = _answers(files, points, pictures)
        assert all(_near(seg, 12.0, 27.9) for seg in matcher)
        assert all(_near(seg, at, at + 11.9) for seg, at in zip(guarded, TITLE_AT, strict=True))
        assert pictures.asked and all(abs(start - 12.0) < 1.0 for start in pictures.asked)

    def test_the_title_card_alone_after_the_cold_open_is_taken_without_a_picture_check(self):
        files, points = _season(TITLE_CARD)
        pictures = Pictures(*TITLE_AT)  # even pictures that differ: a card after 30 s isn't checked
        matcher, guarded = _answers(files, points, pictures)
        assert guarded == matcher and all(_near(seg, at, at + 11.9) for seg, at in zip(guarded, TITLE_AT, strict=True))
        assert pictures.asked == []

    def test_no_title_card_and_every_repeat_guarded_leaves_no_intro(self):
        files, points = _season(Plant(1, 0.0, 9.5), Plant(3, 14.0, 16.0))  # 4.5 s apart: two runs
        matcher, guarded = _answers(files, points, Pictures(14.0))
        assert all(seg is not None for seg in matcher) and guarded == [None] * EPISODES


class TestRealOpenings:
    @pytest.mark.parametrize(("differ", "kept"), [((), True), ((0.0,), False)], ids=["same-picture", "other-picture"])
    def test_a_theme_at_0_00_is_kept_when_it_ends_on_the_same_picture(self, differ, kept):
        files, points = _season(Plant(4, 0.0, 40.0))
        pictures = Pictures(*differ)
        matcher, guarded = _answers(files, points, pictures)
        assert all(_near(seg, 0.0, 39.9) for seg in matcher)
        assert guarded == (matcher if kept else [None] * EPISODES)
        assert pictures.asked  # starting in the first 30 s, it was checked

    @pytest.mark.parametrize(
        ("at", "length_s", "kept"),
        [(0.0, 9.9, False), (0.0, 10.2, True), (1.9, 9.5, False), (2.1, 9.5, True)],
        ids=["9.9s-at-0", "10.2s-at-0", "9.5s-at-1.9s", "9.5s-at-2.1s"],
    )
    def test_only_a_stretch_starting_in_the_first_2_s_must_be_10_s_long(self, at, length_s, kept):
        files, points = _season(Plant(4, at, length_s))
        matcher, guarded = _answers(files, points, Pictures())
        assert all(seg is not None for seg in matcher)
        assert guarded == (matcher if kept else [None] * EPISODES)

    def test_an_opening_starting_after_30_s_is_not_checked_but_one_at_30_s_is(self):
        for at, checked in ((30.0, True), (31.0, False)):
            files, points = _season(Plant(4, at, 20.0))
            pictures = Pictures()
            _answers(files, points, pictures)
            assert bool(pictures.asked) is checked, at


class TestQuorumAsToday:
    def test_a_best_ranked_cluster_below_the_quorum_still_means_no_intro(self):
        # Shared by E1 and E2 only (1 of 4 others): it outranks the title card, and the matcher answers nothing for
        # them. The walk must not go past it to the card either.
        pair_only = Plant(5, (40.0, 45.0, None, None, None), 20.0)
        files, points = _season(pair_only, TITLE_CARD)
        matcher, guarded = _answers(files, points, Pictures())
        assert matcher[:2] == [None, None] and guarded[:2] == [None, None]
        assert guarded[2:] == matcher[2:] and all(
            _near(seg, at, at + 11.9) for seg, at in zip(guarded[2:], TITLE_AT[2:], strict=True)
        )

    def test_the_silence_guard_still_judges_what_the_walk_took(self):
        files, points = _season(TITLE_CARD)
        for path, at in zip(files, TITLE_AT, strict=True):
            points[path][_pts(at) : _pts(at + 12.0)] = season.SILENCE_POINT
        _matcher, guarded = _answers(files, points, Pictures())
        assert guarded == [None] * EPISODES


def _pair(a_points, b_points, start_s, end_s, shift_s):
    """Points for "a" and "b" and one candidate of "a" matched with "b" at ``shift_s``."""
    candidate = IntroCandidate(IntroSegment(start_s, end_s, 1), (Hit(start_s, end_s, "b", start_s + shift_s),))
    return {"a": a_points, "b": b_points}, candidate


class TestDenseCore:
    @pytest.mark.parametrize(("hole", "core"), [(3, 12.0 - POINT_S), (4, 7.5)], ids=["gap-of-4-points", "gap-of-5"])
    def test_a_core_breaks_where_more_than_3_points_in_a_row_differ(self, hole, core):
        shared = _noise(9, _pts(12.0) + 1)
        a, b = _noise(1, 400), _noise(2, 600)
        a[80 : 80 + len(shared)] = shared
        b[180 : 180 + len(shared)] = shared
        at = _pts(7.5)
        a[80 + at : 80 + at + hole] ^= np.uint32(0xFFFF)  # these points differ in 16 bits
        points, candidate = _pair(a, b, 80 * POINT_S, (80 + len(shared) - 1) * POINT_S, 100 * POINT_S)
        assert season.dense_core_s("a", candidate, points) == pytest.approx(core, abs=POINT_S)

    @pytest.mark.parametrize(("hole_at", "passes"), [(_pts(7.4), False), (_pts(8.6), True)])
    def test_the_longest_dense_stretch_must_be_8_s(self, hole_at, passes):
        files, points = _season(Plant(2, TITLE_AT, 12.0, hole=(hole_at, 6)))
        matcher, guarded = _answers(files, points, Pictures())
        assert all(seg is not None for seg in matcher)  # the matcher bridges the hole
        assert guarded == (matcher if passes else [None] * EPISODES)

    def test_the_median_partner_counts_and_a_partner_hit_twice_counts_its_longest(self):
        shared = _noise(9, 200)
        a = _noise(1, 400)
        a[50:250] = shared
        points = {"a": a}
        members = []
        for partner, core_pts in (("b", 40), ("c", 90), ("d", 150)):
            b = _noise(hash(partner) % 1000, 400)
            b[50 : 50 + core_pts] = shared[:core_pts]
            points[partner] = b
            members.append(Hit(50 * POINT_S, 249 * POINT_S, partner, 50 * POINT_S))
        members.append(Hit(60 * POINT_S, 249 * POINT_S, "b", 60 * POINT_S + 1_000 * POINT_S))  # no overlap
        candidate = IntroCandidate(IntroSegment(50 * POINT_S, 249 * POINT_S, 3), tuple(members))
        assert season.dense_core_s("a", candidate, points) == pytest.approx(89 * POINT_S)

    def test_no_overlap_with_any_partner_is_no_core(self):
        points, candidate = _pair(_noise(1, 100), _noise(2, 100), 1.0, 9.0, 500.0)
        assert season.dense_core_s("a", candidate, points) == 0.0
