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

    def test_a_lit_frame_at_the_same_time_as_the_runs_last_credit_frame_ends_the_dark_bridge(self):
        # Prototype parity: the bridge holds only while every lit frame is strictly earlier than the run's last credit
        # frame. A lit row sharing that timestamp (emitted after it) already counts as a scene, so the 30 s of dark
        # empties that follow can't carry the run over to 150 s.
        rows = [dark(100, 1), dark(120, 1), bright(120), dark(130), dark(140), dark(150, 1), dark(170, 1)]
        assert rule_j.credit_runs(rows) == [(0, 1), (5, 6)]

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


class TestAnchorSpacing:
    """The anchor's yardstick is the gap between the run's own credit frames, in presentation order.

    The prototype measured every row of the whole decoded tail in decode order, which is three mismatches against the
    distance the anchor actually compares: the body's cadence isn't the roll's, the keyframe cadence isn't the credit
    cadence, and a decode-order difference isn't a gap.
    """

    @staticmethod
    def _tail(body_step: int, body_end: int) -> list[tuple[float, int, float]]:
        """A lit body at ``body_step`` keyframes, a lone scene-text frame 6 s after it, then a 2 s GOP roll 6 s later."""
        body = [bright(t) for t in range(0, body_end, body_step)]
        roll = [dark(t, 2) for t in range(body_end + 12, body_end + 92, 2)]
        return [*body, dark(body_end + 6, 1), *roll]

    @pytest.mark.parametrize(
        ("body_step", "body_end", "tail_spacing", "whole_tail_answer"),
        [
            (1, 400, 1.0, 490.0),     # dense body: 1.5 x 1 s < the roll's 2 s, so every pair looked far apart
            (2, 400, 2.0, 412.0),     # same cadence either side: the whole-tail median was the right number too
            (10, 1200, 10.0, 1206.0), # sparse body: 1.5 x 10 s swallowed the 6 s glue gap, keeping the lone frame
        ],
    )  # fmt: skip
    def test_the_lone_frame_is_skipped_whatever_the_body_was_keyed_at(
        self, body_step, body_end, tail_spacing, whole_tail_answer
    ):
        rows = self._tail(body_step, body_end)
        first, last = rule_j.credit_runs(rows)[-1]
        assert rows[first][0] == body_end + 6.0  # the run starts on the lone frame in all three cells
        assert rule_j._typical_spacing(rows) == tail_spacing
        assert rule_j._run_spacing(rows, first, last, rule_j.RULE_J) == 2.0
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == body_end + 12.0
        # Two of the three cells only agree with the roll's own start by accident of how the body was keyed.
        assert (whole_tail_answer == coarse.pts_s) is (body_step == 2)

    def test_a_dense_body_before_a_slower_roll_does_not_collapse_the_start_to_its_end(self):
        # An action climax at 1 s keyframes, then a static roll on a 2 s GOP. The whole-tail median (1 s) put the
        # anchor limit at 1.5 s, under the roll's own 2 s gap: no pair of credit rows ever looked adjacent and the
        # start walked row by row to the run's last frame, 118 s late.
        body = [bright(t) for t in range(0, 421)]
        roll = [dark(t, 2) for t in range(421, 540, 2)]
        rows = [*body, *roll]
        assert rule_j.credit_runs(rows) == [(421, 480)]
        assert rule_j._typical_spacing(rows) == 1.0
        assert rule_j._run_spacing(rows, 421, 480, rule_j.RULE_J) == 2.0
        assert rule_j.coarse_start(rows) == Coarse(index=421, end_index=480, pts_s=421.0)

    def test_the_run_gap_is_measured_in_presentation_order_not_decode_order(self):
        # Six of the 80 files emit the roll's keyframes in swapped pairs. Consecutive differences then run -2 / +6 for
        # a 2 s roll, and the median of those is 6 s -- a 9 s limit that would keep a lone frame 8 s before the roll.
        body = [bright(t) for t in range(0, 494, 2)]
        pairs = [row for t in range(500, 564, 4) for row in (dark(t + 2, 2), dark(t, 2))]
        rows = [*body, dark(494, 1), *pairs]
        first, last = rule_j.credit_runs(rows)[-1]
        assert rows[first][0] == 494.0
        assert rule_j._typical_spacing(rows[first : last + 1]) == 6.0
        assert rule_j._run_spacing(rows, first, last, rule_j.RULE_J) == 2.0
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 502.0  # the first roll row emitted, not the lone frame

    def test_only_the_runs_credit_frames_set_the_gap_not_every_keyframe_in_it(self):
        # tv-31's shape: a 4 s credit cadence carried on 2 s keyframes, the empties between them bridged into the run
        # by the dark rule. Counting every row halves the yardstick to the keyframe cadence, and the anchor then walks
        # off the roll's real first frame because its neighbour is 4 s -- one credit frame -- away.
        body = [bright(t) for t in range(0, 400, 2)]
        roll = [dark(t, 2) if t % 4 == 0 else dark(t) for t in range(400, 482, 2)]
        rows = [*body, *roll]
        first, last = rule_j.credit_runs(rows)[-1]
        assert rows[first][0] == 400.0
        assert rule_j._typical_spacing(rows[first : last + 1]) == 2.0  # every row: the keyframe cadence
        assert rule_j._run_spacing(rows, first, last, rule_j.RULE_J) == 4.0  # credit frames only: the credit cadence
        assert rule_j.coarse_start(rows) == Coarse(index=first, end_index=last, pts_s=400.0)

    def test_the_walk_steps_over_one_glued_frame_and_no_more(self):
        # Two lone scene-text frames ahead of the roll, each far enough from the next credit frame to fail the test.
        # The anchor is documented to skip *one* glued frame; unbounded it kept walking into the roll itself.
        body = [bright(t) for t in range(0, 400, 2)]
        rows = [*body, dark(400, 1), dark(412, 1), *[dark(t, 2) for t in range(424, 490, 2)]]
        first, last = rule_j.credit_runs(rows)[-1]
        assert rows[first][0] == 400.0
        assert rule_j._run_spacing(rows, first, last, rule_j.RULE_J) == 2.0  # limit 3 s: neither 12 s gap passes it
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 412.0  # one step only; unbounded this reached 424.0

    @pytest.mark.parametrize(
        ("gap_s", "start"),
        [
            (3.0, 400.0),   # 1.5 x the 2 s credit spacing: the first card's neighbour, no step
            (24.0, 424.0),  # as far as the 24 s join reaches: possibly glued on by it, one step
            (24.1, 400.0),  # only the dark bridge joins that far: the roll's own first card, no step
        ],
    )  # fmt: skip
    def test_the_anchor_steps_only_over_a_gap_the_24_s_join_can_bridge(self, gap_s, start):
        # WILL: the roll's first card, then 65 s of dark empty keyframes before the next card text detection sees. Only
        # credit_runs' dark bridge joins across more than 24 s, so every frame between is dark and the lone frame is a
        # card on black, not scene text the join glued on; stepping over it put the start 72 s late.
        body = [bright(t) for t in range(0, 400, 2)]
        empties = [dark(400 + t) for t in range(2, int(gap_s), 2)]
        rows = [*body, dark(400, 1), *empties, *[dark(400 + gap_s + t, 2) for t in range(0, 60, 2)]]
        assert rule_j.credit_runs(rows) == [(200, len(rows) - 1)]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == start

    def test_a_scene_keyed_after_the_roll_cannot_reach_the_yardstick(self):
        # The Q3 shape: a post-credits scene 30 s past the roll's last credit frame. The slice stops at the run's
        # last row regardless of what that scene's own rows are (see test_a_credit_frame_can_sit_right_past_a_run,
        # dropped by run_s, for why that row is sometimes itself a credit frame).
        body = [bright(t) for t in range(0, 400, 4)]
        rows = [
            *body,
            dark(400, 1),
            *[dark(t, 2) for t in range(424, 472, 4)],
            *[bright(t) for t in range(502, 560, 4)],
        ]
        first, last = rule_j.credit_runs(rows)[-1]
        assert rows[last][0] == 468.0
        assert not rule_j.is_credit(rows[last + 1]) and rows[last + 1][0] == 502.0
        assert rule_j._run_spacing(rows, first, last, rule_j.RULE_J) == 4.0  # the roll's own cadence, not 30 s
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 424.0

    def test_a_run_of_two_rows_keeps_its_first_row(self):
        # Nothing to measure a cadence against: the gap between the pair is itself the typical gap, so it anchors.
        rows = [bright(0), bright(60), dark(100, 1), dark(116, 1)]
        assert rule_j._run_spacing(rows, 2, 3, rule_j.RULE_J) == 16.0
        assert rule_j.coarse_start(rows) == Coarse(index=2, end_index=3, pts_s=100.0)


class TestRefine:
    ROWS = [bright(0), bright(60), dark(100, 1), dark(102, 1), dark(110, 1), dark(125, 1)]

    def test_the_walk_back_stops_at_a_gap_longer_than_2_5_s(self):
        coarse = rule_j.coarse_start(self.ROWS)
        # Credit frames at luma 20 (dark, but not a fade): 101 → 100 → 98, then 95 is 3 s earlier.
        fine = [bright(85), dark(86, 0, 5.0), dark(87, 0, 8.0), dark(88, 1, 20.0), dark(90, 1, 20.0), dark(92, 2, 20.0),
                dark(95, 1, 20.0), dark(98, 1, 20.0), dark(100, 1, 20.0), dark(101, 1, 20.0)]  # fmt: skip
        assert rule_j.refine_start(self.ROWS, coarse, fine) == 98.0

    @pytest.mark.parametrize(("earlier_s", "expected"), [(97.5, 97.5), (97.4, 100.0)])
    def test_credit_frames_exactly_2_5_s_apart_are_contiguous(self, earlier_s, expected):
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [bright(95), dark(earlier_s, 1, 20.0), dark(100, 1, 20.0)]
        assert rule_j.refine_start(self.ROWS, coarse, fine) == expected

    def test_the_walk_jumps_to_the_first_listed_credit_frame_within_reach_as_the_prototype(self):
        # 1 fps rows come out in order, where jumping to the first or the last listed frame within 2.5 s ends in the same
        # place. Out of order they don't: the prototype's walk (``prev[0]``) goes from 100 s to 98 s, the first listed
        # of the two in reach; jumping to the last listed (101 s) would stop there, 3 s from 98 s.
        coarse = rule_j.coarse_start(self.ROWS)
        fine = [dark(98, 1, 20.0), dark(101, 1, 20.0), dark(100, 1, 20.0)]
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

    def test_the_fade_on_the_keyframes_never_steps_to_a_later_row(self):
        # Keyframe rows come in ffmpeg's output order. A swapped pair can put a dark row from 104 s right before the
        # 100 s coarse start; stepping "back" onto it would start the skip 4 s into the roll.
        rows = [bright(0), bright(60), dark(104, 0, 5.0), dark(100, 1), dark(102, 1), dark(110, 1), dark(125, 1)]
        coarse = rule_j.coarse_start(rows)
        assert coarse == Coarse(index=3, end_index=6, pts_s=100.0)
        assert rule_j.refine_start(rows, coarse, [bright(10)]) == 100.0

    def test_the_anchor_compares_rows_in_ffmpegs_output_order(self):
        # Pinned as measured (Q5): the anchor's distance is read in decode order, so a swapped pair at the run's
        # start reads as a negative gap and the first emitted credit row (102 s) is the start, not the earlier 100 s
        # one. Comparing in presentation order instead moves the coarse start of 4 of the 80 files (movie-11, -28,
        # -38, tv-26) earlier, so it is a rule change for the harness gate, not a fix.
        body = [bright(t) for t in range(0, 100, 2)]
        rows = [*body, dark(102, 1), dark(100, 1), *[dark(t, 1) for t in range(104, 132, 2)]]
        coarse = rule_j.coarse_start(rows)
        assert coarse == Coarse(index=50, end_index=65, pts_s=102.0)


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

    @pytest.mark.parametrize(("later_s", "expected"), [(127.5, 127.5), (127.6, 125.0)])
    def test_credit_frames_exactly_2_5_s_apart_carry_the_end_forward(self, later_s, expected):
        fine = [dark(125, 1), dark(later_s, 1), bright(129)]
        assert rule_j.refine_end(self.ROWS, self._coarse(), fine) == expected

    def test_the_forward_walk_jumps_to_the_last_listed_credit_frame_within_reach(self):
        # The start's walk mirrored: from 125 s both 124 s and 127 s are within 2.5 s, and the walk takes the last
        # listed (127 s). Taking the first listed (124 s) would stop there, 3 s short of 127 s.
        fine = [dark(125, 1), dark(124, 1), dark(127, 1)]
        assert rule_j.refine_end(self.ROWS, self._coarse(), fine) == 127.0

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

    SWAPPED = [*[bright(t) for t in range(0, 100, 2)], dark(100, 1), dark(120, 1), dark(116, 1)]

    def test_the_end_is_the_runs_latest_credit_frame_not_its_last_emitted_row(self):
        # Four of the 80 files emit the roll's last keyframes out of order, leaving the run's last ROW 10-21 s before
        # its latest credit frame. Reading the row put movie-11's end 10.4 s early -- enough to cross Q3's 30 s line
        # and decode an end window for a roll that really runs to the end of the file.
        coarse = rule_j.coarse_start(self.SWAPPED)
        assert coarse is not None and self.SWAPPED[coarse.end_index][0] == 116.0  # the last row emitted, 4 s early
        assert rule_j.coarse_end_s(self.SWAPPED, coarse) == 120.0
        # 148 − 120 = 28 s, so the skip runs to the end of the file and the fine rows are never read. The last emitted
        # row says 32 s, which keeps an end and decodes a window for it.
        assert rule_j.credits_end(self.SWAPPED, coarse, _UnreadRows(), 148.0) is None

    def test_the_end_walk_starts_from_the_runs_latest_credit_frame(self):
        # The window is [end − 1 s, end + 20 s] around 120 s, so the 116 s row is outside it; anchored on the last
        # emitted row (116 s) the walk would start there and stop at once, 4 s inside the roll.
        coarse = rule_j.coarse_start(self.SWAPPED)
        fine = [dark(116, 1), dark(119, 1), dark(120, 1)]
        assert rule_j.refine_end(self.SWAPPED, coarse, fine) == 120.0

    def test_a_row_swept_into_the_run_without_text_is_never_its_end(self):
        # The dark bridge and the 24 s join pull rows with no text into a run. The end is a credit frame, never one of
        # those, even when one of them carries the run's latest timestamp.
        rows = [*[bright(t) for t in range(0, 100, 2)], dark(100, 1), dark(130, 0), dark(118, 1)]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and rule_j.credit_runs(rows) == [(50, 52)]
        assert rule_j.coarse_end_s(rows, coarse) == 118.0

    def test_params_changes_which_of_the_runs_rows_count_as_its_end(self):
        # coarse is found once, under the caller's params; end functions take params separately (a future per-vendor
        # tuning could pass a different one at each step), so params has to be pinned on the end path too.
        rows = [*[bright(t) for t in range(0, 100, 2)], dark(100, 2), dark(110, 2), dark(120, 1)]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and rule_j.credit_runs(rows) == [(50, 52)]
        assert rule_j.coarse_end_s(rows, coarse) == 120.0
        # min_boxes=2 drops the 120 s row (1 box) but not the 100/110 s rows (2 boxes): the end moves to 110.
        assert rule_j.coarse_end_s(rows, coarse, params=rule_j.RuleParams(min_boxes=2)) == 110.0
        # No row in the slice qualifies at min_boxes=99: degrades to the run's last row instead of raising.
        assert rule_j.coarse_end_s(rows, coarse, params=rule_j.RuleParams(min_boxes=99)) == rows[coarse.end_index][0]


class TestSceneTextGluedOntoTheEnd:
    """Spec §13 item 13: the start's anchor, mirrored. A lit text frame in the scene after the roll, within 24 s of the
    roll's last card, joins the run; the end steps back over it (one keyframe) when it's lit, further than 1.5 x the
    run's credit spacing from the card before it, and separated from it by a lit keyframe with no text."""

    ROLL = [*[bright(t) for t in range(0, 400, 4)], *[dark(t, 5) for t in range(400, 464, 4)]]  # cards every 4 s
    DURATION_S = 600.0

    def _end(self, *after):
        rows = [*self.ROLL, *after]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 400.0
        return rule_j.end_keyframe_s(rows, coarse), rule_j.coarse_end_s(rows, coarse)

    @pytest.mark.parametrize(
        ("after", "end_keyframe"),
        [
            ((bright(466), bright(470), bright(476, 3)), 460.0),       # scene, then its text: stepped over
            ((bright(466), dark(476, 1)), 476.0),                      # a dark card after it: the roll's own
            ((dark(466), dark(470), bright(476, 3)), 476.0),           # only black between: no scene has started
            ((bright(466, 2), bright(470, 1), bright(476, 3)), 476.0), # every lit frame between has text
            ((bright(462), bright(466, 3)), 466.0),                    # 6 s: within 1.5 x the 4 s spacing
            ((bright(462), bright(466.1, 3)), 460.0),                  # 6.1 s: past it
        ],
    )  # fmt: skip
    def test_the_end_steps_back_only_over_lit_text_after_a_scene_frame(self, after, end_keyframe):
        assert self._end(*after) == (end_keyframe, after[-1][0])

    def test_it_steps_back_one_keyframe_never_more(self):
        after = (bright(466), bright(472, 3), bright(474), bright(484, 4))
        assert self._end(*after) == (472.0, 484.0)

    @pytest.mark.parametrize(("min_boxes", "end_keyframe"), [(3, 120.0), (99, 120.0)])
    def test_fewer_than_three_credit_frames_keep_the_last_row_and_never_raise(self, min_boxes, end_keyframe):
        # Two credit frames can't have their gap exceed 1.5 x their own spacing, so nothing is stepped over. The guard
        # matters when end functions get other params than the run was found with (as coarse_end_s allows): one credit
        # frame (min_boxes=3 keeps only the 3-box row) or none (99) in the slice must degrade, not index credit[-2].
        rows = [bright(0), bright(60), dark(100, 1), bright(110), bright(120, 3)]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and rule_j.end_keyframe_s(rows, coarse) == 120.0
        params = rule_j.RuleParams(min_boxes=min_boxes)
        assert rule_j.end_keyframe_s(rows, coarse, params) == end_keyframe
        assert rule_j.refine_end(rows, coarse, [], params=params) == end_keyframe

    def test_the_end_refine_starts_from_the_roll_and_stops_where_the_scene_starts(self):
        rows = [*self.ROLL, bright(466), bright(470), bright(476, 3)]
        coarse = rule_j.coarse_start(rows)
        # The scene's text is on screen from 468 s on: without the stop at 466 s (the scene's first keyframe) the
        # walk, which starts at the first credit frame in the window, would start there when the roll's own last
        # 1 fps frames are faded below the text line.
        fine = [dark(459, 0, 8.0), dark(460, 0, 6.0), dark(461), *[bright(t) for t in range(462, 468)],
                *[bright(t, 3) for t in range(468, 481)]]  # fmt: skip
        assert rule_j.refine_end(rows, coarse, fine) == 460.0
        roll_to_462 = [dark(459, 5), dark(460, 5), dark(461, 5), dark(462, 4), *fine[4:]]
        assert rule_j.credits_end(rows, coarse, roll_to_462, self.DURATION_S) == 462.0

    def test_it_moves_an_end_and_never_makes_one(self):
        # Under Siege's shape: the roll's last card 30.5 s before the end of the file, then a lit logo with a line of
        # text 7 s before it. Stepping back would leave more than 30 s and stop the skip on the logos; Q3 is judged on
        # the latest credit keyframe, so the skip still runs to the end of the file and nothing is decoded for an end.
        rows = [*self.ROLL, bright(470), dark(478), bright(482), bright(483, 4)]
        coarse = rule_j.coarse_start(rows)
        assert rule_j.end_keyframe_s(rows, coarse) == 460.0
        assert rule_j.credits_end(rows, coarse, _UnreadRows(), 490.5) is None


class TestAShortRollOverACardJustBrighterThanDark:
    """A real TV ending (Rick and Morty S01E04: keyframe rows from 1158 s on; a 1265.0 s file) whose roll runs
    1162.2-1186.5 s over a card at luma 30-35, then a 78.5 s scene. The rows and timestamps are right on both decode
    paths, only box counts on lit scene frames differ. Rule J version 2 fixes the CPU decode's end (spec §13 item 13);
    the GPU decode's missing answer is pinned as rule J reads it."""

    GPU = [(1158.657, 0, 50.7), (1162.203, 2, 30.5), (1170.294, 9, 33.5), (1173.047, 11, 34.1), (1179.470, 4, 32.2),
           (1183.015, 12, 34.2), (1186.519, 0, 153.5), (1188.145, 0, 130.8), (1189.647, 0, 130.6), (1191.649, 1, 158.3),
           (1195.611, 0, 146.0), (1198.406, 2, 145.2), (1200.157, 0, 134.4), (1206.122, 0, 83.1), (1216.549, 0, 142.0),
           (1226.976, 0, 141.9), (1236.652, 0, 168.5), (1242.491, 0, 172.6), (1243.742, 0, 139.4), (1247.705, 0, 156.4),
           (1256.046, 9, 130.4), (1257.548, 2, 42.2), (1259.049, 2, 210.3), (1261.051, 2, 163.9), (1264.054, 0, 29.7)]  # fmt: skip
    CPU = [(1158.657, 0, 50.9), (1162.203, 2, 30.7), (1170.294, 9, 33.8), (1173.047, 11, 34.4), (1179.470, 4, 32.5),
           (1183.015, 12, 34.5), (1186.519, 0, 153.5), (1188.145, 2, 130.8), (1189.647, 1, 130.6), (1191.649, 1, 158.5),
           (1195.611, 0, 146.1), (1198.406, 3, 145.6), (1200.157, 0, 134.5), (1206.122, 0, 83.3), (1216.549, 0, 142.1),
           (1226.976, 0, 142.1), (1236.652, 0, 168.6), (1242.491, 0, 172.7), (1243.742, 0, 139.6), (1247.705, 0, 156.5),
           (1256.046, 10, 130.7), (1257.548, 2, 42.4), (1259.049, 2, 210.5), (1261.051, 2, 164.3), (1264.054, 0, 30.0)]  # fmt: skip
    # The CPU decode's 1 fps rows of the end window the app now reads, 1 s before the roll's last card on (1182.0 s).
    CPU_END = [(1182.0, 5, 31.9), (1183.0, 12, 34.5), (1184.0, 14, 36.0), (1185.0, 2, 30.9), (1186.0, 3, 31.6),
               (1187.0, 0, 153.2), (1188.0, 2, 130.7), (1189.0, 3, 131.1), (1190.0, 1, 130.6), (1191.0, 1, 130.6),
               (1192.0, 1, 157.4), (1193.0, 1, 155.6), (1194.0, 1, 154.0), (1195.0, 0, 153.7), (1196.0, 0, 146.1),
               (1197.0, 0, 146.1), (1198.0, 3, 145.6), (1199.0, 2, 146.0), (1200.0, 0, 138.1), (1201.0, 0, 133.9),
               (1202.0, 0, 133.8)]  # fmt: skip
    DURATION_S = 1265.024

    def test_the_rolls_credit_keyframes_span_under_15_s_so_there_is_no_answer(self):
        # The first card (1162.2 s) is 2 boxes at luma 30.5, just over the dark line, where a frame needs 3. What is
        # left, 1170.3-1183.0 s, is 12.7 s of credit keyframes: under run_s, so no run and no answer. Counting 2 boxes
        # at luma 30-33 answers it and six more of the season within 1 s of their truth, but put a 2022 movie's start
        # on its epilogue cards (photos on a dark ground): measured and not taken (phase3-harness.md, version 2).
        assert not rule_j.is_credit(self.GPU[1])
        assert rule_j.credit_runs(self.GPU) == []

    def test_a_scene_text_keyframe_glued_onto_the_run_no_longer_carries_its_end_into_the_scene(self):
        # swscale's frame of the scene at 1198.4 s reads 3 boxes (scale_cuda's reads 2): a lit credit frame 15.4 s
        # after the roll, so the 24 s join takes it in and the run is long enough. Its end used to be that scene frame,
        # so the skip ran 11.5 s into the scene (1198.0 s, spec §13 item 13). The end now steps back over it to the
        # roll's last card and the refine walks the 1 fps rows to the roll's last frame, as the frames show it.
        coarse = rule_j.coarse_start(self.CPU)
        assert coarse is not None and coarse.pts_s == 1170.294
        assert rule_j.coarse_end_s(self.CPU, coarse) == 1198.406  # Q3's test: 66.6 s follows, an end is kept
        assert rule_j.end_keyframe_s(self.CPU, coarse) == 1183.015
        assert rule_j.credits_end(self.CPU, coarse, self.CPU_END, self.DURATION_S) == 1186.0


@lru_cache(maxsize=1)
def _fixture() -> dict:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def _rows(raw: list) -> list[tuple[float, int, float]]:
    return [(float(r[0]), int(r[1]), float(r[2])) for r in raw]


# Every file whose refined answer differs from the prototype's, as (prototype error, port error) against the
# frame-check truth: the anchor's yardstick, its one step, and its 24 s limit (rule J version 2). All nine move the
# same way -- closer to the truth -- and no other file moves. The fixture keeps
# the prototype's numbers as they were, so both sides are pinned here; ``tools/markers_eval/credits_fixture.py``
# repeats the id list for its rebuild guard and the two have to agree.
PORT_DIVERGENCES = {
    # id:        prototype, port,     why
    "movie-03": (71.717, 5.55),  # the step crossed 65 s of dark empties, past the 24 s join: the roll's first card kept
    "movie-12": (17.0, 9.0),  # the unbounded walk ran 26.2 s into the roll; one step reaches 8.9 s
    "movie-25": (334.292, 146.792),  # 10.417 s roll under a 5.292 s tail median: the start had collapsed onto the end
    "movie-29": (149.029, 90.095),  # 10.010 s roll read as 20.020 s in decode order, against a 2.419 s tail
    "movie-38": (130.088, 88.38),  # the step crossed 41.7 s of dark keyframes: still late, 41.7 s less so
    "tv-07": (15.742, 7.742),  # 17 rows in the run, 10 of them credit frames: 1.502 s keyframes, 2.502 s credits
    "tv-09": (21.125, 0.125),  # 3.670 s roll under a 2.336 s tail median: another collapse onto the run's last frame
    "tv-22": (19.866, 16.866),  # 16 rows, 6 credit frames: 1.919 s keyframes against a 6.256 s credit cadence
    "tv-31": (13.0, 4.0),  # 42 rows, 16 credit frames: 2.002 s keyframes against a 4.004 s credit cadence
}

# Files where the anchor still walks off the run's first row, and where it lands. The refine window absorbs most of
# these before they reach ``credits_start``, so the divergence table above cannot see them: movie-27 and tv-06 both
# moved in an earlier round of this fix with no visible effect on their final answer. Pinning the coarse time is what
# stops that drifting silently.
ANCHOR_WALKS_TO = {
    "movie-02": 1853.143,
    "movie-09": 1860.930,
    "movie-12": 1727.014,
    "movie-13": 1859.000,
    "movie-27": 1859.720,
    "movie-28": 1714.681,
    "movie-29": 1512.850,
    "movie-34": 1548.699,
    "tv-04": 1373.548,
    "tv-05": 1374.220,
    "tv-16": 1376.096,
    "tv-19": 1376.805,
    "tv-26": 1411.762,
    "tv-34": 1429.340,
}


class TestEightyFiles:
    def test_reproduces_the_prototype_at_the_spec_20_s_refine_span(self):
        # 64 / 1 / 7 / 4 against the prototype's 59 / 1 / 8 / 4. Five files enter the 10 s band: tv-09 (its start no
        # longer collapses onto the end of its run, 21.1 -> 0.1), movie-12, tv-07, tv-31 and movie-03 (WILL, 71.7 ->
        # 5.6: the anchor no longer steps past the 24 s join, CREDITS_TEXT_VERSION 2). movie-25, movie-29, movie-38 and
        # tv-22 improve without changing bucket, and nothing else moves at all. §5.4's table was measured at 10 s.
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
        assert (tally["within_10s"], tally["early"], tally["late"], tally["none"]) == (64, 1, 7, 4)

    def test_every_divergence_from_the_prototype_is_closer_to_the_truth(self):
        # The one property that separates this from tuning against a fixture: no file was traded away for another.
        for item_id, (prototype, port) in PORT_DIVERGENCES.items():
            assert abs(port) < abs(prototype), item_id

    def test_matches_the_prototype_item_for_item_bar_the_named_divergences(self):
        seen = set()
        for item in _fixture()["items"]:
            start = rule_j.credits_start(_rows(item["key"]), _rows(item["fine"]))
            expected = item["expected_error_s"]
            if item["id"] in PORT_DIVERGENCES:
                prototype, port = PORT_DIVERGENCES[item["id"]]
                assert expected == pytest.approx(prototype, abs=0.0015), item["id"]
                assert start is not None and (start - item["truth_s"]) == pytest.approx(port, abs=0.0015), item["id"]
                seen.add(item["id"])
            elif expected is None:
                assert start is None, item["id"]
            else:
                assert start is not None and abs((start - item["truth_s"]) - expected) <= 0.0015, item["id"]
        assert seen == set(PORT_DIVERGENCES)

    def test_the_anchor_walks_on_exactly_these_files_and_lands_here(self):
        walked = {}
        for item in _fixture()["items"]:
            key = _rows(item["key"])
            runs = rule_j.credit_runs(key)
            coarse = rule_j.coarse_start(key)
            if coarse is not None and coarse.index != runs[-1][0]:
                walked[item["id"]] = round(coarse.pts_s, 3)
        assert walked == ANCHOR_WALKS_TO

    def test_no_fixture_file_has_a_credit_frame_immediately_past_its_run(self):
        # Not a structural guarantee (see test_a_credit_frame_can_sit_right_past_a_run below) -- just what the 80
        # files happen to look like. 41 of them have a row past their run to check this on.
        checked = 0
        for item in _fixture()["items"]:
            key = _rows(item["key"])
            runs = rule_j.credit_runs(key)
            if not runs or runs[-1][1] + 1 >= len(key):
                continue
            checked += 1
            assert not rule_j.is_credit(key[runs[-1][1] + 1]), item["id"]
        assert checked == 41

    def test_a_credit_frame_can_sit_right_past_a_run(self):
        # A trailing raw run under run_s is dropped by credit_runs, so runs[-1] (the last KEPT run) can leave a
        # credit frame sitting right past it -- here, a stinger's title card 34 s after the roll, reachable only
        # because a swapped-order keyframe pair makes the run's own gap-join and dark-bridge rules both fail to
        # reach it. This is why the slice stays [first:last+1]: measuring the run's own frames is what matters,
        # not whatever sits just past it.
        body = [bright(t) for t in range(1000, 1100, 2)]
        rows = [
            *body,
            dark(1100, 1),  # a lone scene-text frame, gap-joined onto the roll over 20 s
            dark(1120, 1), dark(1122, 1), dark(1124, 1),  # the roll
            bright(1128),  # emitted out of order: a bright frame with a later pts than the run's last row
            dark(1126, 1),  # the run's last row (index 55)
            dark(1160, 1),  # 34 s past the roll's last frame: a credit frame immediately past the run
        ]  # fmt: skip
        first, last = rule_j.credit_runs(rows)[-1]
        assert (first, last) == (50, 55)
        assert rule_j.is_credit(rows[last + 1])
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 1120.0

    def test_no_start_lands_on_the_last_frame_of_its_own_run(self):
        # The shape of the bug the anchor's yardstick fixed: a coarse start walked all the way to ``end_index``.
        for item in _fixture()["items"]:
            key = _rows(item["key"])
            coarse = rule_j.coarse_start(key)
            assert coarse is None or coarse.index < coarse.end_index, item["id"]

    def test_three_of_the_76_runs_end_more_than_30_s_before_the_end_of_the_file(self):
        # Measured while planning on coarse ends (the fixture's 1 fps rows sit around the start, so no end refine
        # here). Four while the end was the run's last emitted row: movie-11's roll ends 20.4 s before the file does,
        # and only reading its last row (10.4 s earlier, its keyframes come out of order) put it over the 30 s line.
        kept = []
        for item in _fixture()["items"]:
            key = _rows(item["key"])
            coarse = rule_j.coarse_start(key)
            if coarse is not None and rule_j.keeps_a_scene_after(rule_j.coarse_end_s(key, coarse), item["duration_s"]):
                kept.append(item["id"])
        assert kept == ["movie-24", "movie-35", "tv-31"]

    def test_the_run_ends_later_than_its_last_emitted_row_on_four_files(self):
        # The shape the end fix addresses, on the measured rows: the run's last row in ffmpeg's output order sits
        # 10-21 s before the run's latest credit keyframe.
        moved = {}
        for item in _fixture()["items"]:
            key = _rows(item["key"])
            coarse = rule_j.coarse_start(key)
            if coarse is None:
                continue
            gap = rule_j.coarse_end_s(key, coarse) - key[coarse.end_index][0]
            if gap:
                moved[item["id"]] = round(gap, 3)
        assert moved == {"movie-11": 10.417, "movie-28": 10.010, "movie-38": 20.854, "tv-26": 10.427}

    def test_fixture_is_anonymised(self):
        raw = gzip.decompress(FIXTURE.read_bytes()).decode()
        assert "/" not in raw.replace("\\/", "")
        assert not re.search(r"\.(mkv|mp4|avi)|\{(tmdb|tvdb|imdb)-|\[[^\]]*p\]", raw)
        assert set(_fixture()) == {"about", "refine_before_s", "items"}
        for item in _fixture()["items"]:
            assert re.fullmatch(r"(movie|tv)-\d\d", item["id"])
            assert item["kind"] in {"movie", "tv"}
            assert set(item) == {"id", "kind", "duration_s", "truth_s", "key", "fine", "expected_error_s"}
