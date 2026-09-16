"""Rule J (spec §5.4): each step as a matrix of small row sets, then the 80-file regression fixture."""

from __future__ import annotations

import gzip
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

from media_preview_generator.markers.credits import rule_j
from media_preview_generator.markers.credits.rule_j import Coarse

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "markers" / "credits_rule_j_80.json.gz"


def dark(t: float, boxes: int = 0, luma: float = 10.0) -> tuple[float, int, float]:
    return (t, boxes, luma)


def bright(t: float, boxes: int = 0, luma: float = 120.0) -> tuple[float, int, float]:
    return (t, boxes, luma)


class _UnreadRows:
    """Fine rows the code under test must not read."""

    def _read(self, *args: object) -> None:
        raise AssertionError("the fine rows were read")

    __iter__ = __len__ = __getitem__ = _read


class TestCreditFrame:
    @pytest.mark.parametrize(
        ("row", "expected"),
        [
            ((0.0, 1, 29.9), True),     # dark: one box is enough
            ((0.0, 0, 29.9), False),
            ((0.0, 2, 30.0), False),    # 30 is bright: needs 3
            ((0.0, 3, 30.0), True),
            ((0.0, 3, 200.0), True),
        ],
    )  # fmt: skip
    def test_dark_frames_need_one_box_bright_frames_three(self, row, expected):
        assert rule_j.is_credit(row) is expected


class TestRuns:
    def test_a_24_s_gap_joins_and_a_longer_one_splits(self):
        joined = [dark(0, 1), dark(10, 1), dark(20, 1), bright(30), dark(44, 1), dark(54, 1)]
        assert rule_j.credit_runs(joined) == [(0, 5)]
        split = [dark(0, 1), dark(10, 1), dark(20, 1), bright(30), dark(44.5, 1), dark(54.5, 1), dark(64.5, 1)]
        assert rule_j.credit_runs(split) == [(0, 2), (4, 6)]

    def test_dark_empty_frames_never_break_a_run_but_bright_ones_do(self):
        bridged = [dark(0, 1), dark(10, 1), dark(20, 1), dark(30), dark(40), dark(50, 1), dark(60, 1)]
        assert rule_j.credit_runs(bridged) == [(0, 6)]
        broken = [dark(0, 1), dark(10, 1), dark(20, 1), bright(30), dark(40), dark(50, 1), dark(60, 1)]
        assert rule_j.credit_runs(broken) == [(0, 2)]  # 50–60 s is only 10 s long

    def test_an_empty_frame_at_luma_30_is_bright_and_breaks_a_run(self):
        rows = [dark(0, 1), dark(10, 1), dark(20, 1), bright(30, 0, 30.0), dark(40), dark(50, 1), dark(60, 1)]
        assert rule_j.credit_runs(rows) == [(0, 2)]

    @pytest.mark.parametrize(("length", "kept"), [(14.9, False), (15.0, True)])
    def test_runs_shorter_than_15_s_are_dropped(self, length, kept):
        rows = [dark(100, 1), dark(100 + length, 1)]
        assert rule_j.credit_runs(rows) == ([(0, 1)] if kept else [])


class TestCoarse:
    def test_the_last_run_is_picked(self):
        rows = [dark(0, 1), dark(20, 1), bright(30), bright(60), dark(100, 1), dark(116, 1)]
        assert rule_j.coarse_start(rows) == Coarse(index=4, end_index=5, pts_s=100.0)

    def test_a_lone_text_frame_before_the_roll_is_not_the_start(self):
        # Undisputed: one story-scene text frame 20 s before the roll joins it over the 24 s gap; the anchor skips it.
        story = [bright(t) for t in range(0, 20, 2)]
        rows = [*story, dark(20, 1), *[bright(t) for t in range(22, 40, 2)], *[dark(t, 2) for t in range(40, 70, 2)]]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 40.0

    def test_a_neighbour_exactly_1_5_typical_gaps_away_anchors_the_start(self):
        story = [bright(t) for t in range(0, 40, 2)]
        rows = [*story, dark(40, 2), dark(43, 2), *[dark(t, 2) for t in range(45, 70, 2)]]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 40.0  # typical gap 2 s: 43 − 40 is exactly 1.5 of them

    def test_the_typical_gap_indexes_half_the_row_count_into_the_sorted_gaps(self):
        # Four rows, gaps 20 / 2 / 10: index 4 // 2 = 2 is the largest gap (20 s, so 30 s anchors), not the middle 10 s.
        rows = [dark(0, 1), dark(20, 1), dark(22, 1), dark(32, 1)]
        assert rule_j.coarse_start(rows) == Coarse(index=0, end_index=3, pts_s=0.0)

    def test_two_rows_do_not_raise(self):
        assert rule_j.coarse_start([dark(0, 1), dark(20, 1)]) == Coarse(index=0, end_index=1, pts_s=0.0)

    def test_no_run_means_no_answer(self):
        assert rule_j.coarse_start([bright(0), bright(10, 2), dark(20)]) is None
        assert rule_j.credits_start([bright(0)], []) is None


class TestRefine:
    ROWS = [bright(0), bright(60), dark(100, 1), dark(102, 1), dark(110, 1), dark(125, 1)]

    def test_the_walk_back_stops_at_a_gap_longer_than_2_5_s(self):
        coarse = rule_j.coarse_start(self.ROWS)
        # Credit frames at luma 20 (dark, but not a fade): 101 → 100 → 98, then 95 is 3 s earlier.
        fine = [bright(85), dark(86, 0, 5.0), dark(87, 0, 8.0), dark(88, 1, 20.0), dark(90, 1, 20.0), dark(92, 2, 20.0),
                dark(95, 1, 20.0), dark(98, 1, 20.0), dark(100, 1, 20.0), dark(101, 1, 20.0)]  # fmt: skip
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 98.0

    def test_walks_back_through_contiguous_credit_frames_then_over_the_fade(self):
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [
            bright(85),
            dark(86, 0, 5.0),
            dark(87, 0, 8.0),
            *[dark(t, 1, 20.0) for t in (88, 90, 92, 94, 96, 98, 100, 101)],
        ]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 86.0

    def test_the_fade_includes_dark_credit_frames(self):
        # Luma 10 is below the fade threshold too: the fade step walks over dark credit cards as well (prototype).
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [bright(85), dark(86, 0, 5.0), dark(87, 0, 8.0), dark(88, 1), dark(90, 1), dark(92, 2), dark(95, 1),
                dark(98, 1), dark(100, 1), dark(101, 1)]  # fmt: skip
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 86.0

    def test_fade_back_steps_over_dark_frames_up_to_4_s_apart(self):
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [bright(90), dark(93, 0, 6.0), dark(96, 0, 4.0), dark(99, 1), dark(100, 1)]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 93.0

    def test_fade_back_stops_at_the_window_floor(self):
        coarse = rule_j.coarse_start(self.ROWS)
        # Floor = 100 − 20 = 80 s: the walk reaches 83, the fade steps to 81 and not to 79 or 77.
        fine = [
            dark(77, 0, 6.0),
            dark(79, 0, 6.0),
            dark(81, 0, 6.0),
            *[dark(t, 1, 20.0) for t in (83, 85, 87, 89, 91, 93, 95, 97, 99, 100)],
        ]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 81.0

    @pytest.mark.parametrize(("luma", "expected"), [(11.9, 96.0), (12.0, 100.0)])
    def test_fade_back_steps_over_frames_darker_than_12(self, luma, expected):
        rows = [bright(90), dark(96, 0, luma), dark(100, 1)]
        assert rule_j.fade_back(rows, 2, 80.0) == expected

    def test_fade_back_may_stop_on_the_floor_itself(self):
        rows = [dark(76, 0, 6.0), dark(80, 0, 6.0), dark(83, 1)]
        assert rule_j.fade_back(rows, 2, 80.0) == 80.0

    def test_a_credit_frame_exactly_20_s_before_the_coarse_start_is_in_the_window(self):
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [bright(79), *[dark(t, 1, 20.0) for t in range(80, 101)]]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 80.0

    def test_no_credit_frame_in_the_window_keeps_the_coarse_time(self):
        # The dark keyframe at 96 s tells this branch from the no-fine-rows one, which would fade back to it.
        rows = [bright(0), dark(96, 0, 5.0), dark(100, 1), dark(102, 1), dark(110, 1), dark(125, 1)]
        coarse = rule_j.coarse_start(rows)
        assert rule_j.refine_start(rows, coarse, [bright(95), bright(96)]) == 100.0

    def test_no_fine_rows_in_the_window_fades_back_on_the_keyframes(self):
        rows = [bright(0), dark(96, 0, 5.0), dark(100, 1), dark(102, 1), dark(116, 1)]
        coarse = rule_j.coarse_start(rows)
        assert rule_j.refine_start(rows, coarse, [bright(10)]) == 96.0


class TestEpilogueCards:
    """Spec §5.4's owner rule says epilogue text cards aren't credits; rule J can't tell them apart when they touch the
    roll. These cells pin what it does (checked against the prototype's ``detect``); Task 11's frame-check sheets look at
    every candidate shaped like this."""

    STORY = [bright(t) for t in range(0, 90, 2)]
    CARDS = [dark(t, 1) for t in range(90, 100, 2)]  # 10 s of white-on-black cards

    def test_cards_right_before_the_roll_become_the_start(self):
        rows = [*self.STORY, *self.CARDS, *[dark(t, 2) for t in range(100, 162, 2)]]
        fine = [*[bright(t) for t in range(80, 90)], *[dark(t, 1) for t in range(90, 100)], dark(100, 2)]
        assert rule_j.credits_start(rows, fine) == 90.0

    def test_cards_bridged_to_the_roll_by_black_become_the_start(self):
        rows = [
            *self.STORY,
            *self.CARDS,
            *[dark(t) for t in range(100, 130, 2)],
            *[dark(t, 2) for t in range(130, 190, 2)],
        ]
        fine = [*[dark(t) for t in range(110, 130)], dark(130, 2)]
        assert rule_j.credits_start(rows, fine) == 90.0

    def test_cards_split_from_the_roll_by_a_lit_scene_are_not_the_start(self):
        rows = [
            *self.STORY,
            *self.CARDS,
            *[bright(t) for t in range(100, 130, 2)],
            *[dark(t, 2) for t in range(130, 190, 2)],
        ]
        fine = [*[bright(t) for t in range(110, 130)], dark(130, 2)]
        assert rule_j.credits_start(rows, fine) == 130.0


class TestEnd:
    """Owner, Q3 (2026-09-16): the skip ends at the roll's last credit frame when more than 30 s of the file follows it."""

    ROWS = [bright(0), bright(60), dark(100, 1), dark(102, 1), dark(110, 1), dark(125, 1)]  # run 100–125 s

    def _coarse(self):
        return rule_j.coarse_start(self.ROWS)

    def test_a_roll_that_runs_to_the_end_of_the_file_is_open_ended(self):
        # 25 s after the last credit keyframe: a logo or black, not a scene. The fine rows aren't read.
        assert rule_j.credits_end(self.ROWS, self._coarse(), _UnreadRows(), 150.0) is None

    def test_the_last_credit_keyframe_decides_even_when_the_refined_end_is_earlier(self):
        # 154.5 − 125 = 29.5 s: open-ended, although the fine rows would refine the end to 124 s (30.5 s before the end).
        fine = [dark(124, 1), bright(125)]
        assert rule_j.credits_end(self.ROWS, self._coarse(), fine, 154.5) is None

    def test_a_scene_after_the_roll_is_kept_the_skip_ends_at_the_last_contiguous_credit_frame(self):
        fine = [
            dark(124, 1),
            dark(125, 1),
            dark(126, 1),
            dark(127, 2),
            bright(128),
            bright(129),
            bright(130),
            dark(131, 1),
            bright(132),
        ]
        assert rule_j.credits_end(self.ROWS, self._coarse(), fine, 200.0) == 127.0  # 131 is 4 s after 127

    @pytest.mark.parametrize(("after_s", "expected"), [(30.0, None), (30.1, 125.0)])
    def test_more_than_30_s_must_follow(self, after_s, expected):
        assert rule_j.credits_end(self.ROWS, self._coarse(), [], 125.0 + after_s) == expected

    def test_a_refined_end_within_30_s_of_the_end_is_open_ended(self):
        fine = [dark(t, 1) for t in range(124, 129)]  # the roll goes on to 128 s; 157 − 128 = 29 s
        assert rule_j.credits_end(self.ROWS, self._coarse(), fine, 157.0) is None

    def test_no_credit_frame_near_the_last_keyframe_keeps_the_keyframe(self):
        assert rule_j.refine_end(self.ROWS, self._coarse(), [bright(126), bright(127)]) == 125.0

    def test_the_walk_only_reads_the_window_around_the_last_keyframe(self):
        # 123 s is before the window (end − 1 s); 146 s is past it (end + 20 s).
        fine = [dark(123, 3), dark(124, 1), dark(146, 1)]
        assert rule_j.refine_end(self.ROWS, self._coarse(), fine) == 124.0

    def test_the_forward_walk_stops_20_s_after_the_last_credit_keyframe(self):
        fine = [dark(t, 1) for t in range(124, 160)]  # the 1 fps roll goes on to 159 s
        assert rule_j.refine_end(self.ROWS, self._coarse(), fine) == 145.0

    def test_the_walk_starts_inside_the_window_not_at_an_earlier_credit_frame(self):
        # From 123 s the walk would stop at once (126 s is 3 s later); the window starts at 124 s.
        assert rule_j.refine_end(self.ROWS, self._coarse(), [dark(123, 1), dark(126, 1)]) == 126.0


@lru_cache(maxsize=1)
def _fixture() -> dict:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def _rows(raw: list) -> list[tuple[float, int, float]]:
    return [(float(r[0]), int(r[1]), float(r[2])) for r in raw]


class TestEightyFiles:
    def test_reproduces_the_prototype_at_the_spec_20_s_refine_span(self):
        # 59 / 1 / 8 / 4; §5.4's table was measured at a 10 s refine span (late 9).
        tally = Counter()
        for item in _fixture()["items"]:
            start = rule_j.credits_start(_rows(item["key"]), _rows(item["fine"]))
            if start is None:
                tally["none"] += 1
                continue
            error = start - item["truth_s"]
            tally["within_10s"] += abs(error) <= 10
            if abs(error) > 30:
                tally["early" if error < 0 else "late"] += 1
        assert len(_fixture()["items"]) == 80
        assert (tally["within_10s"], tally["early"], tally["late"], tally["none"]) == (59, 1, 8, 4)

    def test_matches_the_prototype_item_for_item(self):
        for item in _fixture()["items"]:
            start = rule_j.credits_start(_rows(item["key"]), _rows(item["fine"]))
            expected = item["expected_error_s"]
            if expected is None:
                assert start is None, item["id"]
            else:
                assert start is not None and abs((start - item["truth_s"]) - expected) <= 0.0015, item["id"]

    def test_four_of_the_76_runs_end_more_than_30_s_before_the_end_of_the_file(self):
        # Measured while planning, on coarse ends (the fixture's 1 fps rows sit around the start, so no end refine here).
        kept = []
        for item in _fixture()["items"]:
            key = _rows(item["key"])
            coarse = rule_j.coarse_start(key)
            if coarse is not None and rule_j.keeps_a_scene_after(rule_j.coarse_end_s(key, coarse), item["duration_s"]):
                kept.append(item["id"])
        assert len(kept) == 4, kept

    def test_fixture_is_anonymised(self):
        raw = gzip.decompress(FIXTURE.read_bytes()).decode()
        assert "/" not in raw.replace("\\/", "")
        assert not re.search(r"\.(mkv|mp4|avi)|\{(tmdb|tvdb|imdb)-|\[[^\]]*p\]", raw)
        assert set(_fixture()) == {"about", "refine_before_s", "items"}
        for item in _fixture()["items"]:
            assert re.fullmatch(r"(movie|tv)-\d\d", item["id"])
            assert item["kind"] in {"movie", "tv"}
            assert set(item) == {"id", "kind", "duration_s", "truth_s", "key", "fine", "expected_error_s"}
