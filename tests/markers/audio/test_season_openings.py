"""Season audio's picking rules, on synthetic seasons shaped like the real failures:

- two openings in one season (SPY x FAMILY S01: each found by 10 or 11 of 24 others, under the season's quorum), and
  the shapes that must stay unanswered (openings interleaved like The Simpsons S03's two cuts, a stretch shared only
  with an episode that has the season's opening);
- a bumper at the start of every file ahead of the title sequence (Star Trek: Strange New Worlds S04's "Star Trek 60");
- a title sequence cut by the fingerprint window (Alias S02E09, 881-906 s with a 900 s window).

Each case runs the real matcher over planted fingerprints and pins what the matcher alone answers (so the case is the
failure it models) and what the season step answers.
"""

from __future__ import annotations

import numpy as np
import pytest

from media_preview_generator.markers.audio import POINT_S, season
from media_preview_generator.markers.audio.matcher import (
    Hit,
    IntroCandidate,
    IntroSegment,
    file_hits,
    floats,
    meets_opening_quorum,
)
from tests.markers.audio.helpers import Plant, alike, answers, near, noise, pts, runs


def _season(*plants: Plant, episodes: int, length_s: float = 400.0, names: str = "S01E{:02d}"):
    files = [f"/tv/Show (2022)/Season 01/Show (2022) - {names.format(e)}.mkv" for e in range(1, episodes + 1)]
    points = {}
    for i, path in enumerate(files):
        body = noise(1_000 + i, pts(length_s))
        for plant in plants:
            plant.into(body, i)
        points[path] = body
    return files, points


# SPY x FAMILY S01's shape on 10 episodes: E01-E05 open with one theme, E06-E10 with another, each after a cold open of
# its own length. Each is found by 4 of the 9 others: under the season's quorum of 4.5.
FIRST_AT = (40.0, 55.0, 70.0, 85.0, 100.0, None, None, None, None, None)
SECOND_AT = (None, None, None, None, None, 45.0, 60.0, 75.0, 90.0, 105.0)
FIRST = Plant(11, FIRST_AT, 60.0)
SECOND = Plant(12, SECOND_AT, 60.0)


class TestTwoOpenings:
    def test_each_half_of_the_season_gets_its_own_opening(self):
        files, points = _season(FIRST, SECOND, episodes=10)
        matcher, step = answers(files, points)
        assert matcher == [None] * 10  # 4 of 9: under the season's quorum
        for i, segment in enumerate(step):
            at = FIRST_AT[i] if FIRST_AT[i] is not None else SECOND_AT[i]
            assert near(segment, at, at + 60.0 - POINT_S), (i, segment)
            assert segment.support == 4

    def test_an_episode_without_either_opening_stays_unanswered(self):
        first_at = (*FIRST_AT[:4], None, *FIRST_AT[5:])  # E05 lost its opening (a premiere without one)
        files, points = _season(Plant(11, first_at, 60.0), SECOND, episodes=10)
        _, step = answers(files, points)
        assert step[4] is None
        assert all(segment is not None for i, segment in enumerate(step) if i != 4)

    def test_openings_that_alternate_through_the_season_are_no_two_openings(self):
        # The Simpsons S03's shape: two cuts of the opening, taken in turn by episodes all through the season.
        odd = tuple(40.0 + 5 * i if i % 2 == 0 else None for i in range(10))
        even = tuple(45.0 + 5 * i if i % 2 == 1 else None for i in range(10))
        files, points = _season(Plant(11, odd, 60.0), Plant(12, even, 60.0), episodes=10)
        matcher, step = answers(files, points)
        assert matcher == [None] * 10 and step == [None] * 10

    def test_a_stretch_shared_with_an_episode_that_has_the_seasons_opening_isnt_one(self):
        # E10 has no opening; a 20 s stretch of it (a recap) is also in E09, which has the season's opening.
        opening = Plant(11, (*(40.0 + 5 * i for i in range(9)), None), 60.0)
        recap = Plant(13, (*[None] * 8, 200.0, 30.0), 20.0)
        files, points = _season(opening, recap, episodes=10)
        runs_between = runs(points)
        (candidate, *_) = season.intro_candidates(file_hits(files[9], files, runs_between))
        assert candidate.segment.support == 1
        _, step = answers(files, points)
        assert step[9] is None
        assert all(near(step[i], 40.0 + 5 * i, 100.0 + 5 * i - POINT_S) for i in range(9))

    def test_a_stretch_under_15_s_is_no_opening_on_either_side(self):
        files, points = _season(Plant(11, FIRST_AT, 12.0), SECOND, episodes=10)
        _, step = answers(files, points)
        # E01-E05's 12 s stretch has no opening quorum of its own, and isn't a second opening for E06-E10 either.
        assert step == [None] * 10

    def test_names_without_episode_numbers_have_no_order_to_split_by(self):
        files, points = _season(FIRST, SECOND, episodes=10, names="{:02d}")
        _, step = answers(files, points)
        assert step == [None] * 10

    def test_the_second_opening_needs_its_own_quorum(self):
        # Only E09 and E10 share a second stretch: 1 of the other 4 episodes without the first opening's stretch.
        files, points = _season(
            FIRST, Plant(12, (*[None] * 8, 45.0, 60.0), 60.0), Plant(14, (*[None] * 5, 80.0, 90.0, None, None, None), 30.0),
            episodes=10,
        )  # fmt: skip
        runs_between = runs(points)
        candidates = season.intro_candidates(file_hits(files[0], files, runs_between))
        assert meets_opening_quorum(files[0], candidates[0], files, runs_between, season._season_and_episode) is False


class TestFloats:
    @staticmethod
    def _candidate(*offsets: float) -> IntroCandidate:
        hits = tuple(Hit(10.0, 40.0, f"/p{i}", 10.0 + offset) for i, offset in enumerate(offsets))
        return IntroCandidate(IntroSegment(10.0, 40.0, len(hits)), hits)

    @pytest.mark.parametrize(
        ("offsets", "expected"),
        [((0.0, 1.0, -3.9), False), ((5.0, -60.0, 0.0), True), ((5.0, 0.0, 0.0, 30.0), False), ((4.1, -4.1), True)],
    )
    def test_most_partners_must_hold_it_more_than_4_s_away(self, offsets, expected):
        assert floats(self._candidate(*offsets)) is expected


# SNW S04's shape: a 28 s bumper at 0 s in every file, found by all others; the title sequence after a cold open of
# varying length, found by 3 of the 4 others (E05's is another cut).
BUMPER = Plant(21, (0.0,) * 5, 28.0)
TITLE_AT = (300.0, 180.0, 240.0, 120.0, None)
TITLE = Plant(22, TITLE_AT, 100.0)


class TestFileStartBumper:
    def test_a_bumper_at_every_files_start_gives_way_to_the_title_sequence(self):
        files, points = _season(BUMPER, TITLE, episodes=5, length_s=500.0)
        matcher, step = answers(files, points)
        assert all(near(segment, 0.0, 28.0 - POINT_S) for segment in matcher)  # ranked by support: the bumper
        for i in range(4):
            assert near(step[i], TITLE_AT[i], TITLE_AT[i] + 100.0 - POINT_S), (i, step[i])
        assert near(step[4], 0.0, 28.0 - POINT_S)  # nothing later floats in E05: the bumper stays

    def test_an_intro_at_the_file_start_stays_when_nothing_later_floats(self):
        fixed_later = Plant(23, (200.0,) * 5, 30.0)  # a stretch at the same time in every episode
        files, points = _season(Plant(21, (0.0,) * 5, 35.0), fixed_later, episodes=5, length_s=500.0)
        _, step = answers(files, points)
        assert all(near(segment, 0.0, 35.0 - POINT_S) for segment in step)

    def test_a_later_stretch_under_the_quorum_doesnt_take_over(self):
        files, points = _season(BUMPER, Plant(22, (300.0, 180.0, None, None, None), 100.0), episodes=5, length_s=500.0)
        _, step = answers(files, points)
        assert all(near(segment, 0.0, 28.0 - POINT_S) for segment in step)

    def test_the_later_stretch_must_pass_the_guards(self):
        files, points = _season(BUMPER, TITLE, episodes=5, length_s=500.0)
        points[files[0]] = points[files[0]][: pts(TITLE_AT[0] + 50.0)]  # E01's title sequence is cut by its window
        segment = season.season_intro(files[0], files, points, runs(points), end_picture_passes=alike)
        assert near(segment, 0.0, 28.0 - POINT_S)


class TestCutByWindow:
    def test_a_title_sequence_running_past_the_window_is_passed_over(self):
        # Alias S02E09: its title sequence starts 16 s before its fingerprint ends; the others' lie well inside theirs.
        files, points = _season(Plant(31, (384.0, 100.0, 150.0, 200.0), 25.0), episodes=4)
        matcher, step = answers(files, points)
        assert matcher[0] is not None and matcher[0].end_s > 396.5  # it ends where the fingerprint does
        assert step[0] is None
        assert all(
            near(segment, at, at + 25.0 - POINT_S) for segment, at in zip(step[1:], (100.0, 150.0, 200.0), strict=False)
        )

    @pytest.mark.parametrize(("before_last_s", "cut"), [(0.0, True), (3.4, True), (3.6, False), (30.0, False)])
    def test_cut_means_ending_within_the_gap_bridge_of_the_last_point(self, before_last_s, cut):
        points = np.zeros(pts(400.0), dtype="<u4")
        last_s = (len(points) - 1) * POINT_S
        assert season.cut_by_window(IntroSegment(300.0, last_s - before_last_s, 3), points) is cut
