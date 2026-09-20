"""Rule J (spec §5.4): each step as a matrix of small row sets, then the 80-file regression fixture."""

from __future__ import annotations

import gzip
import itertools
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

from media_preview_generator.markers.credits import rule_j
from media_preview_generator.markers.credits.rule_j import Coarse

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "markers" / "credits_rule_j_80.json.gz"
SYNTH_FIXTURE = FIXTURE.with_name("credits_synth_lab.json.gz")


def cards(boxes: int) -> tuple[rule_j.Box, ...]:
    """Text boxes stacked down the frame, one per box, centred: where a row with this many boxes might hold them.

    Version 3 reads them in three places (:class:`TestWhereRuleJReadsPositions`, :class:`TestOverlayBoxes`). Centred
    is the roll's own band, so these rows carry text the band steps can reach for; and box *n* is in the same place
    on every row these make, which to :func:`rule_j.overlay_boxes` is a channel bug -- text right across the story in
    one spot. That is why a tail of them carrying text all through has no answer (:class:`TestTextAllThrough`'s
    subtitled rows), and why a fixture that wants two different bands must move its boxes (:func:`band_row`).
    """
    return tuple((40, 20 + 30 * n, 280, 44 + 30 * n) for n in range(boxes))


def dark(t: float, boxes: int = 0, luma: float = 10.0) -> rule_j.Row:
    return (t, boxes, luma, cards(boxes))


def bright(t: float, boxes: int = 0, luma: float = 120.0) -> rule_j.Row:
    return (t, boxes, luma, cards(boxes))


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
        # Version 3's reach back never steps back onto a row of the run, so the anchor's ruling stands here.
        assert coarse.run_index is None

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
        # credit_runs' dark bridge joins across more than 24 s, so every keyframe between is dark: the 24 s join didn't
        # glue the first frame on, and stepping over it put the start 72 s late.
        body = [bright(t) for t in range(0, 400, 2)]
        empties = [dark(400 + t) for t in range(2, int(gap_s), 2)]
        rows = [*body, dark(400, 1), *empties, *[dark(400 + gap_s + t, 2) for t in range(0, 60, 2)]]
        assert rule_j.credit_runs(rows) == [(200, len(rows) - 1)]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == start

    @pytest.mark.parametrize("first", [dark(400, 1), bright(400, 3)], ids=["dark-card", "lit-frame"])
    def test_a_first_frame_the_dark_bridge_joined_is_kept_lit_or_dark(self, first):
        # What the 24 s limit keys on is the gap, not the frame: a lit first frame is kept too. On the harness's sets
        # that is right (RocknRolla's lit end-title card, 3 boxes at luma 33.2, frame-checked), and it is the rule's
        # measured cost as well: a lit scene-text frame followed by more than 24 s of dark keyframes before the roll
        # becomes the start, that far early (spec §13 item 14). With the lit frame 24 s or nearer, one step as before.
        body = [bright(t) for t in range(0, 400, 2)]
        rows = [*body, first, *[dark(t) for t in range(402, 430, 2)], *[dark(t, 2) for t in range(430, 490, 2)]]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 400.0
        near = [*body, first, *[dark(t) for t in range(402, 424, 2)], *[dark(t, 2) for t in range(424, 490, 2)]]
        coarse = rule_j.coarse_start(near)
        assert coarse is not None and coarse.pts_s == 424.0

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
        # -38, tv-26) earlier, so it is a rule change for the harness gate, not a fix. Version 3's reach back walks in
        # presentation order but never onto a row of the run, so it leaves this alone.
        body = [bright(t) for t in range(0, 100, 2)]
        rows = [*body, dark(102, 1), dark(100, 1), *[dark(t, 1) for t in range(104, 132, 2)]]
        assert rule_j.coarse_start(rows) == Coarse(index=50, end_index=65, pts_s=102.0)


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

    def test_whether_there_is_an_end_is_decided_from_the_latest_credit_keyframe(self):
        # The final review's repro: after the roll (cards to 460 s), a lit empty keyframe, then a lit logo with text
        # at 476 s that stays on screen to 485 s, in a 511 s file. Walked from 476 s the end reaches 485 s, 26 s before
        # the end of the file, so version 1 had no end. Deciding it from the stepped-back keyframe instead would stop
        # the skip at 461 s, on the logo, 50 s before the end of the file.
        rows = [*self.ROLL, bright(466), bright(470), bright(476, 3)]
        coarse = rule_j.coarse_start(rows)
        assert rule_j.end_keyframe_s(rows, coarse) == 460.0
        fine = [dark(459, 5), dark(460, 5), dark(461, 5), *[bright(t) for t in range(462, 475)],
                *[bright(t, 4) for t in range(475, 486)], *[bright(t) for t in range(486, 497)]]  # fmt: skip
        assert rule_j.refine_end(rows, coarse, fine) == 461.0  # where an end would be
        assert rule_j.credits_end(rows, coarse, fine, 511.0) is None
        # With 5 s more of the file, 31 s follow the logo: version 1 kept an end at 485 s, version 2 moves it to 461 s.
        assert rule_j.credits_end(rows, coarse, fine, 516.0) == 461.0

    def test_the_spacing_leaves_the_glued_keyframe_out(self):
        # Three credit keyframes (100, 106, 116 s): all three gaps' median is the 10 s glued-on gap itself, which is
        # never more than 1.5 x itself, so the step back could never fire on a run of three or four. Without it the
        # spacing is 6 s, and 10 s is past 9 s.
        rows = [bright(0), bright(60), dark(100, 1), dark(106, 1), bright(110), bright(116, 3)]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and (coarse.pts_s, rule_j.coarse_end_s(rows, coarse)) == (100.0, 116.0)
        assert rule_j.end_keyframe_s(rows, coarse) == 106.0
        assert rule_j._typical_spacing([rows[2], rows[3], rows[5]]) == 10.0

    def test_it_moves_an_end_and_never_makes_one(self):
        # Under Siege's shape: the roll's last card 30.5 s before the end of the file, then a lit logo with a line of
        # text 7 s before it. Stepping back would leave more than 30 s and stop the skip on the logos; Q3 is judged on
        # the latest credit keyframe, so the skip still runs to the end of the file and nothing is decoded for an end.
        rows = [*self.ROLL, bright(470), dark(478), bright(482), bright(483, 4)]
        coarse = rule_j.coarse_start(rows)
        assert rule_j.end_keyframe_s(rows, coarse) == 460.0
        assert rule_j.credits_end(rows, coarse, _UnreadRows(), 490.5) is None


def _synth() -> dict:
    return {item["name"]: item for item in json.loads(gzip.decompress(SYNTH_FIXTURE.read_bytes()))["items"]}


def _synth_rows(raw: list) -> list[rule_j.Row]:
    """The lab fixture's rows whole: the app's own decode, box positions included."""
    return [(float(r[0]), int(r[1]), float(r[2]), tuple(tuple(int(v) for v in box) for box in r[3])) for r in raw]


class TestTextAllThrough:
    """Text on screen all through the tail is not a roll (the lab's Synth Audio episodes, final review): no answer
    unless at least 30 s of the tail precede the run and fewer than 80 % of those keyframes carry any text."""

    @pytest.mark.parametrize(
        ("name", "version_1_start_s"),
        [("Synth Audio (2022) - S01E02", 178.007), ("Synth Audio (2022) - S01E04", 106.007),
         ("Synth Audio (2022) - S02E01", 260.007)],
    )  # fmt: skip
    def test_a_test_pattern_with_a_running_timecode_gets_no_answer(self, name, version_1_start_s):
        # The app's own GPU decode of the lab file, one keyframe per 2 s: every keyframe carries the timecode, and the
        # few that read 3+ boxes make a run on these three episodes. Version 1 answered each; only the guard stops it.
        item = _synth()[name]
        key, fine = _rows(item["key"]), _rows(item["fine"])
        coarse = rule_j.coarse_start(key)
        assert coarse is not None and item["version_1_start_s"] == version_1_start_s
        assert min(later[0] - earlier[0] for earlier, later in itertools.pairwise(key)) >= 1.0  # keyframes only
        assert all(row[1] >= 1 for row in key)
        assert rule_j.refine_start(key, coarse, fine) == version_1_start_s  # what version 2 answers without the guard
        assert rule_j.text_all_through(key, coarse)
        assert rule_j.credits_start(key, fine) is None

    @pytest.mark.parametrize(
        "name", ["Synth Audio (2022) - S01E01", "Synth Audio (2022) - S01E03", "Synth Audio (2022) - S02E02"]
    )
    def test_the_other_timecode_episodes_have_no_run_on_their_keyframes(self, name):
        # Read from every frame (the VP9 decode before its non-key packets were dropped), these made runs too; on
        # their keyframes the 3-box frames are too few and too far apart to join into one.
        item = _synth()[name]
        key = _rows(item["key"])
        assert rule_j.coarse_start(key) is None and item["version_1_start_s"] is None
        assert all(row[1] >= 1 for row in key) and len(key) <= 300 / 2 + 2

    @pytest.mark.parametrize("name", ["Synth Credits (2024)", "Synth Credits Open (2025)"])
    def test_a_synthetic_roll_after_story_keeps_its_answer(self, name):
        item = _synth()[name]
        key, fine = _rows(item["key"]), _rows(item["fine"])
        assert not rule_j.text_all_through(key, rule_j.coarse_start(key))
        assert rule_j.credits_start(key, fine) == item["version_1_start_s"] == 541.0

    @staticmethod
    def _tail(story: list, roll_at: float) -> list:
        return [*story, *[dark(roll_at + 2 * i, 4) for i in range(30)]]

    @pytest.mark.parametrize(
        ("story_s", "answered"),
        [(29.9, False), (30.0, True)],  # the run must start 30 s or more into the tail
    )  # fmt: skip
    def test_a_run_needs_30_s_of_tail_before_it(self, story_s, answered):
        rows = self._tail([bright(1000 + 2 * i) for i in range(15)], 1000 + story_s)
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 1000 + story_s
        assert rule_j.text_all_through(rows, coarse) is not answered

    @pytest.mark.parametrize(
        ("keyframes", "texted", "answered"),
        [(10, 7, True), (10, 8, False), (10, 10, False), (100, 79, True), (100, 80, False)],  # any box counts
    )  # fmt: skip
    def test_the_text_share_before_the_run_decides(self, keyframes, texted, answered):
        # Lit frames with 1-2 boxes (a timecode, a bug) aren't credit frames, but they are text on screen. The run
        # answers below 80 % of the keyframes before it, not at 80 %.
        step = 100 / keyframes
        story = [bright(1000 + step * i, 1 if i < texted else 0) for i in range(keyframes)]
        rows = self._tail(story, 1100)
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 1100.0
        assert rule_j.text_all_through(rows, coarse) is not answered
        assert (rule_j.credits_start(rows, []) is None) is not answered

    SUBTITLE = [dark(t, 1 if t % 6 else 0, 15.0) for t in range(0, 450, 2)]  # a line on two keyframes in three

    @pytest.mark.parametrize(
        ("lead", "coarse_s", "start"),
        [
            ([], 2.0, None),                                               # from the tail's first keyframe: 30 s floor
            ([dark(t - 20, 0, 15.0) for t in range(0, 20, 2)], 2.0, None),  # 20 s of dark without text: floor
            ([bright(t - 100, 1) for t in range(0, 100, 2)], -100.0, None),  # 100 s of lit story, subtitled throughout
            ([bright(t - 100, 1 if t % 6 else 0) for t in range(0, 100, 2)], -98.0, None),  # subtitled on 2 in 3
        ],
    )  # fmt: skip
    def test_a_subtitled_dark_scene_is_no_answer(self, lead, coarse_s, start):
        # Rule J reads a dark frame with one box as a credit frame, so a subtitled dark scene is one long run. Lit
        # frames with one box aren't credit frames, but they are text on screen before it -- and read raw (which is
        # what ``coarse_start`` without ``without=`` does) version 3's reach back takes them, because a subtitle sits
        # in the same band as the cards it can't tell itself from. It walks the start to the first subtitled frame,
        # which leaves no rows before the run, so the 30 s floor answers None on the two leads with lit story
        # before the scene, whose coarse start version 2 put at 2.0: the early answer this shape gave is gone,
        # not moved. ``credits_start`` reaches the same None by
        # the other half of version 3 as well -- one subtitle line in one place right across the story is an overlay,
        # and the fourth lead has no run left at all without it. The 0.8 share itself is pinned by
        # ``test_the_text_share_before_the_run_decides``.
        rows = [*lead, *self.SUBTITLE]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == coarse_s
        assert rule_j.credits_start(rows, []) == start

    def test_subtitles_on_a_dark_scene_after_text_free_story_still_start_early(self):
        # Pinned as a known limit: after 200 s of lit story without text, the tail before the run is text-free, so the
        # subtitled dark scene reads as the roll's opening and the start is 250 s before a roll at 450 s.
        story = [bright(t - 200) for t in range(0, 200, 2)]
        rows = [*story, *self.SUBTITLE, *[dark(t, 4) for t in range(450, 510, 2)]]
        assert rule_j.credits_start(rows, []) == 2.0


class TestARollThatBeganBeforeTheTail:
    """A run less than 30 s into the tail may be a roll the tail cut into (the lab's Heeramandi episodes: 462 s rolls
    against a 450 s tail). The detector reads before the tail only when nothing lit comes before the run."""

    ROLL = [dark(1000 + 2 * i, 3) for i in range(100)]  # 1000-1198 s

    @pytest.mark.parametrize(
        ("before", "reads_on"),
        [
            ([], True),                                          # the tail opens on the roll
            ([dark(980 + 2 * i) for i in range(10)], True),       # 20 s of black first
            ([dark(980 + 2 * i) for i in range(9)] + [bright(998)], False),  # a lit frame 2 s before the run
            ([bright(980 + 2 * i) for i in range(10)], False),    # 20 s of story first
            ([dark(970 + 2 * i) for i in range(15)], False),      # 30 s of black: text_all_through judges it
        ],
    )  # fmt: skip
    def test_the_rows_before_the_tail_are_read_only_when_nothing_lit_precedes_a_run_under_30_s_in(
        self, before, reads_on
    ):
        rows = [*before, *self.ROLL]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 1000.0
        assert rule_j.opens_on_the_run(rows, coarse) is reads_on

    def test_a_story_caption_28_s_into_the_tail_stays_without_an_answer(self):
        # A real broadcast episode on the CPU decode (Mayday S12E10): lit story, a 25 s run of captions 28 s into the
        # tail, then 400 s of story; its real roll is 17 s long, too short to be a run. Story came first, so nothing
        # before the tail is read and the run stays too close to the tail's start to answer.
        rows = [bright(2250 + 2 * i) for i in range(14)] + [dark(2278 + 2 * i, 2) for i in range(13)]
        rows += [bright(2304 + 2 * i) for i in range(198)]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 2278.0
        assert not rule_j.opens_on_the_run(rows, coarse)
        assert rule_j.credits_start(rows, []) is None

    @staticmethod
    def _before(roll_from: float) -> list:
        # The 120 s window before a tail at 1000 s: story, then the roll's cards from roll_from on (one row the tail
        # shares, at 1000 s, which the join drops).
        story = [bright(880 + 2 * i) for i in range(int((roll_from - 880) / 2))]
        return [*story, *[dark(t, 3) for t in range(int(roll_from), 1001, 2)]]

    @pytest.mark.parametrize(
        ("roll_from", "start"),
        [
            (988.0, 988.0),  # 12 s before the tail (the Heeramandi shape): 108 s of story before it
            (910.0, 910.0),  # 90 s before: 30 s of story, just enough
            (900.0, None),  # 100 s before: 20 s of story, too little to tell it from text all through the tail
        ],
    )  # fmt: skip
    def test_the_roll_starts_where_it_began_before_the_tail_up_to_90_s_before(self, roll_from, start):
        joined = rule_j.joined_before(self._before(roll_from), self.ROLL)
        assert joined is not None and joined[-len(self.ROLL) :] == self.ROLL
        assert [row[0] for row in joined].count(1000.0) == 1
        coarse = rule_j.coarse_start(joined)
        assert coarse is not None and coarse.pts_s == roll_from
        assert rule_j.credits_start(joined, []) == start

    def test_a_roll_whose_one_card_before_the_tail_the_anchor_steps_over_gets_no_answer(self):
        # Pinned as a known cost: the roll's first card at 996 s, then its ground (luma 18) from the tail at 1000 s and
        # cards every 6 s from 1006 s. The anchor steps over the 996 s card (10 s to the next, over 1.5 x 6 s), so the
        # joined run's start is inside the tail and the join is dropped: no answer, where one longer tail answers
        # 1006 s. Keeping the join on the run's first row instead would also keep a lone dark subtitle frame before the
        # tail, and a night scene's captions after it, as a roll.
        before = [bright(880 + 2 * i) for i in range(58)] + [dark(996, 3), dark(998, 0, 18.4)]
        tail = [dark(1000, 0, 18.4), dark(1003, 0, 18.4)] + [dark(1006 + 6 * i, 3, 18.4) for i in range(30)]
        coarse = rule_j.coarse_start(tail)
        assert coarse is not None and coarse.pts_s == 1006.0 and rule_j.opens_on_the_run(tail, coarse)
        assert rule_j.joined_before(before, tail) is None
        assert rule_j.credits_start(tail, []) is None
        assert rule_j.credits_start([*before, *tail], []) == 1006.0

    @pytest.mark.parametrize(
        "before",
        [
            [bright(880 + 2 * i) for i in range(60)],  # lit story right up to the tail
            [dark(880 + 2 * i, 0, 22.0) for i in range(60)],  # a night scene without text
        ],
    )  # fmt: skip
    def test_a_run_that_stays_inside_the_tail_is_judged_on_the_tail_alone(self, before):
        # A caption run on a dark scene 10 s into the tail looks like a roll the tail opened on (nothing lit before it),
        # but the rows before the tail don't carry it on: it didn't begin before the tail, so the join is dropped and
        # the run stays too close to the tail's start to answer.
        tail = [dark(1000 + 2 * i, 0, 22.0) for i in range(5)] + [dark(1010 + 2 * i, 1, 22.0) for i in range(12)]
        tail += [bright(1034 + 2 * i) for i in range(200)]
        coarse = rule_j.coarse_start(tail)
        assert coarse is not None and coarse.pts_s == 1010.0 and rule_j.opens_on_the_run(tail, coarse)
        assert rule_j.joined_before(before, tail) is None
        assert rule_j.credits_start(tail, []) is None


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
    # The CPU decode's 1 fps rows from 1 s before the roll's last card (1182.0 s) to 1202 s. The app's end window runs
    # on to 20 s past the latest credit keyframe (1198.4 s); the rows past 1202 s only tell that an end exists.
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
    """A fixture's rows as rule J reads them: time, box count, luma.

    The 80-file fixture holds nothing else. It was built from the prototype's own measurements
    (``tools/markers_eval/credits_fixture.py``), which recorded how many boxes each frame had and never where they
    were, and re-measuring these files would replace the very rows the port is pinned against. Position work reads
    the harness's decode cache, whose rows carry the boxes, or the lab fixture below.
    """
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
        # These rows carry no positions, so version 3 never moves the start before the run and the indices
        # still bracket it.
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


class TestWhereRuleJReadsPositions:
    """Version 3 is the first rule to read where a frame's text is (spec §13 items 14 and 15), in three places:
    :func:`rule_j.overlay_boxes`, :func:`rule_j.same_roll` and :func:`rule_j.reach_back`.

    The band steps only ever move a **start** earlier. The overlay step is the one that changes what a frame holds --
    :func:`rule_j.without_overlays` recounts ``row[1]`` -- so which of the run's frames are credit frames, and with
    them the run's own bounds and the anchor's spacing, move under it; they are pinned in :class:`TestOverlayBoxes`.
    Everything else still reads the count and the luma alone, as it always did, and a row that carries no positions
    answers exactly what version 2 answered."""

    @pytest.mark.parametrize("boxes", [0, 1, 3])
    def test_a_frame_is_a_credit_frame_on_its_count_wherever_its_boxes_are(self, boxes):
        corner = ((0, 0, 8, 6),) * boxes  # a channel logo's place
        middle = ((120, 80, 200, 100),) * boxes  # a credit card's place
        for luma in (10.0, 120.0):
            assert rule_j.is_credit((1.0, boxes, luma, corner)) is rule_j.is_credit((1.0, boxes, luma, middle))
            assert rule_j.is_credit((1.0, boxes, luma, corner)) is rule_j.is_credit((1.0, boxes, luma))

    def test_rows_without_positions_answer_exactly_as_version_2(self):
        # The 80-file fixture's rows carry no boxes (the prototype never recorded them), so neither step can fire and
        # every one of its pinned answers above is version 2's. `run_index` is None on all 80: nothing reached back.
        for item in _fixture()["items"]:
            coarse = rule_j.coarse_start(_rows(item["key"]))
            assert coarse is None or coarse.run_index is None, item["id"]

    def test_a_row_that_does_not_say_where_its_text_is_is_never_the_rolls(self):
        assert rule_j.boxes_of((1.0, 2, 10.0)) == ()
        assert rule_j.in_band((1.0, 2, 10.0), 160.0) is False

    def test_the_band_steps_only_ever_move_a_start_earlier_and_never_an_end(self):
        # The lab fixture's rows are the app's own, so these are real boxes, not made-up ones.
        for item in _synth().values():
            key, fine = _synth_rows(item["key"]), _synth_rows(item["fine"])
            bare = [row[:3] for row in key]
            with_boxes, without = rule_j.coarse_start(key), rule_j.coarse_start(bare)
            assert (with_boxes is None) == (without is None), item["name"]
            if with_boxes is None:
                continue
            assert with_boxes.pts_s <= without.pts_s, item["name"]
            assert with_boxes.end_index == without.end_index, item["name"]
            assert rule_j.coarse_end_s(key, with_boxes) == rule_j.coarse_end_s(bare, without), item["name"]
            assert rule_j.end_keyframe_s(key, with_boxes) == rule_j.end_keyframe_s(bare, without), item["name"]
            assert rule_j.credits_end(key, with_boxes, fine, 100000.0) == rule_j.credits_end(
                bare, without, [row[:3] for row in fine], 100000.0
            ), item["name"]


def band_row(t: float, boxes: int, centre: int, luma: float = 120.0) -> rule_j.Row:
    """A lit frame whose boxes sit around ``centre`` across the frame."""
    return (t, boxes, luma, tuple((centre - 30, 20 + 30 * n, centre + 29, 44 + 30 * n) for n in range(boxes)))


class TestSameRoll:
    """An earlier run is the last run's own roll when its text sits in the same band and the text between them never
    stops (spec §13 item 14: a roll the 24 s join split, answered from a later block)."""

    OPENING = [band_row(t, 3, 160, 10.0) for t in range(400, 440, 2)]  # names on black, one block
    ROLL = [band_row(t, 3, 160, 10.0) for t in range(500, 560, 2)]  # the crawl, the last run
    # Lit frames with 1-2 boxes: text on screen, but not credit frames, which is why the 24 s join broke.
    NAMES_OVER_FOOTAGE = [band_row(t, 2, 160) for t in range(440, 500, 2)]

    def _rows(self, between):
        return [*[band_row(t, 0, 160) for t in range(0, 400, 2)], *self.OPENING, *between, *self.ROLL]

    def test_two_runs_in_one_band_with_text_all_the_way_between_are_one_roll(self):
        rows = self._rows(self.NAMES_OVER_FOOTAGE)
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        # The names between are 2 s apart, so the walk alone would reach 400 too: assert the merge itself, as the two
        # refusal tests below do. `TestTheTwoHalvesTogether` has the row where only the merge can get there.
        assert rule_j.same_roll(rows, runs, rule_j.RULE_J) == 0
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 400.0
        assert coarse.run_index is not None  # the end still belongs to the last run

    def test_an_earlier_run_in_another_band_is_left_alone(self):
        # The earlier block sits hard against the left edge: `same_roll` refuses it on the band. The end-to-end
        # assertion below is refused by the walk's *cadence*, not its band -- the last in-band row before the roll is
        # 62 s back, well past 1.5 x its 2 s. `TestReachBack.test_it_stops_at_a_credit_frame_outside_the_band` is the
        # row that pins the band half of the same refusal.
        rows = [
            *[band_row(t, 0, 160) for t in range(0, 400, 2)],
            *[band_row(t, 3, 40, 10.0) for t in range(400, 440, 2)],
            *[band_row(t, 2, 40) for t in range(440, 500, 2)],
            *self.ROLL,
        ]
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        assert rule_j.same_roll(rows, runs, rule_j.RULE_J) == 1
        assert rule_j.coarse_start(rows).pts_s == 500.0  # the last run's own first card, nothing reached back

    def test_an_earlier_run_is_left_alone_when_the_text_between_stops(self):
        # Text on one keyframe in three between the two runs: under "at least half".
        between = [band_row(t, 2 if t % 6 == 0 else 0, 160) for t in range(440, 500, 2)]
        rows = self._rows(between)
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        assert rule_j.same_roll(rows, runs, rule_j.RULE_J) == 1
        # The reach back takes the one boxed keyframe 2 s before the roll and stops: the next is 6 s further, past
        # 1.5 x the roll's own 2 s cadence.
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 498.0

    def test_nothing_between_the_two_runs_is_no_evidence_and_no_merge(self):
        # A lit frame at the earlier run's own last credit time ends the dark bridge (TestRuns) and leaves no keyframe
        # strictly between the two runs. "The text never stops" then has nothing to read, so the merge is refused
        # rather than made on the band alone.
        rows = [
            *[band_row(t, 0, 160) for t in range(0, 400, 2)],
            *[band_row(t, 3, 160, 10.0) for t in (400, 420, 438)],
            band_row(438, 0, 160),
            *[band_row(t, 3, 160, 10.0) for t in range(470, 510, 2)],
        ]
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        assert rule_j.same_roll(rows, runs, rule_j.RULE_J) == 1

    def test_two_runs_whose_times_run_the_wrong_way_round_are_never_merged(self):
        # ffmpeg's output order can leave a later run at a lower index, so `runs[-2]` isn't always earlier in time.
        # Such a pair has no keyframe between it by construction, which is what refuses it.
        rows = [
            *[band_row(t, 3, 160, 10.0) for t in (500, 530, 560)],
            *[band_row(t, 3, 160, 10.0) for t in (400, 420, 440)],
        ]
        assert rule_j.same_roll(rows, [(0, 2), (3, 5)], rule_j.RULE_J) == 1

    def test_the_walk_after_a_merge_keeps_the_cadence_of_the_block_it_starts_in(self):
        # The roll opens on 2 s cards, runs names over footage, and closes on 16 s cards. The walk starts in the
        # opening block, so its limit is that block's 1.5 x 2 s -- not the last run's 24 s, which would carry it over
        # the story's in-band text every 10 s and all the way to the first row.
        rows = [
            *[band_row(t, 2 if t % 10 == 0 else 0, 160) for t in range(0, 400, 2)],
            *[band_row(t, 3, 160, 10.0) for t in range(400, 442, 2)],
            *[band_row(t, 2, 160) for t in range(442, 500, 2)],
            *[band_row(t, 3, 160, 10.0) for t in range(500, 564, 16)],
        ]
        runs = rule_j.credit_runs(rows)
        assert rule_j.same_roll(rows, runs, rule_j.RULE_J) == 0
        assert rule_j._run_spacing(rows, *runs[0], rule_j.RULE_J) == 2.0
        assert rule_j._run_spacing(rows, *runs[-1], rule_j.RULE_J) == 16.0
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 400.0

    def test_the_end_still_reads_the_run_when_the_start_has_reached_back(self):
        # The roll opens on a block 20 s per card, runs names over footage for 160 s, and closes on a run 5 s per
        # card with scene text the join glued on 14 s after its last card. `end_keyframe_s` steps back over that text
        # only because the spacing it measures is the last run's own (1.5 x 5 s): read from the reached-back start it
        # would be the merged median, 20 s, and the glued frame would become the end.
        rows = [
            *[band_row(t, 0, 160) for t in range(0, 300, 2)],
            *[band_row(t, 3, 160, 10.0) for t in (300, 320, 340)],
            *[band_row(t, 2, 160) for t in range(342, 500, 2)],
            *[band_row(t, 3, 160, 10.0) for t in (500, 505, 510, 515, 520)],
            band_row(527, 0, 160),
            band_row(534, 3, 160),
        ]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 300.0 and coarse.run_index is not None
        assert rule_j.coarse_end_s(rows, coarse) == 534.0
        assert rule_j.end_keyframe_s(rows, coarse) == 520.0
        read_from_the_start = rule_j.Coarse(coarse.index, coarse.end_index, coarse.pts_s)
        assert rule_j.end_keyframe_s(rows, read_from_the_start) == 534.0

    def test_a_roll_of_dark_frames_with_no_boxes_at_all_has_no_band(self):
        # `min_boxes` counts boxes, but the dark bridge can join blank dark frames into a run; such a run has no band
        # and nothing is merged into it.
        rows = [
            *[band_row(t, 0, 160) for t in range(0, 400, 2)],
            *self.OPENING,
            *self.NAMES_OVER_FOOTAGE,
            *[band_row(500, 1, 160, 10.0), *[band_row(t, 0, 160, 10.0) for t in range(502, 560, 2)]],
        ]
        runs = rule_j.credit_runs(rows)
        assert rule_j.band_of(rows, runs[-1][0], runs[-1][1], rule_j.RULE_J) == 160.0
        bare = [row[:3] for row in rows]  # the same rows with nothing to say where their text is
        assert rule_j.band_of(bare, runs[-1][0], runs[-1][1], rule_j.RULE_J) is None
        assert rule_j.same_roll(bare, runs, rule_j.RULE_J) == len(runs) - 1


class TestTheTwoHalvesTogether:
    """The cells no single-mechanism class covers: the band steps read the rows the overlay step left, which is how
    the detector and :func:`rule_j.credits_start` always run them (spec §5.4 steps 4, 5 and 7).

    The matrix is ``without`` (None or set) x merge (fired or not) x reach back (fired or not). ``TestSameRoll`` and
    ``TestReachBack`` cover the ``without=None`` half, and ``TestOverlayBoxes`` the cells with one run and no merge.
    These are the rest. The bug here sits **in the cards' own band**, which is the case that matters: out of it the
    walk would never take it whichever rows it read.
    """

    BUG = (140, 150, 180, 166)  # a lower-third, centred like the cards
    STORY = [band_row(t, 0, 160) for t in range(0, 400, 2)]
    OPENING = [band_row(t, 3, 160, 10.0) for t in range(400, 440, 2)]  # names on black, one block
    NAMES_OVER_FOOTAGE = [band_row(t, 1, 160) for t in range(440, 500, 2)]  # lit, one box: not a credit frame
    # Text on every other keyframe: half of them, which `same_roll` merges across, but 4 s apart, which is further
    # than the walk's own cadence reaches (1.5 x the roll's 2 s). Only the merge can cross this.
    SPARSE_NAMES = [band_row(t, 1 if t % 4 == 0 else 0, 160) for t in range(440, 500, 2)]
    ROLL = [band_row(t, 3, 160, 10.0) for t in range(500, 560, 2)]

    @classmethod
    def _bug(cls, i: int) -> rule_j.Box:
        """The bug as the detector reads it: a pixel or two different every sighting."""
        left, top, right, bottom = cls.BUG
        return (left + i % 3, top - i % 2, right - i % 2, bottom + i % 3)

    @classmethod
    def _under_the_bug(cls, rows: list[rule_j.Row]) -> list[rule_j.Row]:
        return [(row[0], row[1] + 1, row[2], (*row[3], cls._bug(i))) for i, row in enumerate(rows)]

    def test_the_merge_and_the_walk_both_fire_through_the_bug(self):
        # A split roll under a lower-third bug, with the names over footage sparse enough that the walk can't cross
        # them: only `same_roll` reaches the opening block, so this really is the merge's own cell. Read as decoded
        # the bug is a box in the roll's band on every frame of the story, so the walk crosses the whole file anyway;
        # read without it the roll is put back together by the merge and the start is the opening block's first card.
        rows = self._under_the_bug([*self.STORY, *self.OPENING, *self.SPARSE_NAMES, *self.ROLL])
        overlays = rule_j.overlay_boxes(rows)
        assert len(overlays) == 1 and rule_j._iou(overlays[0], self.BUG) >= rule_j.OVERLAY_IOU
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        read = rule_j.without_overlays(rows, overlays)
        assert rule_j.same_roll(read, runs, rule_j.RULE_J) == 0  # the merge fires, and it is what reaches 400
        assert rule_j.coarse_start(rows).pts_s == 0.0  # what reading the rows as decoded would give
        coarse = rule_j.coarse_start(rows, without=read)
        assert coarse is not None and coarse.pts_s == 400.0
        assert coarse.run_index is not None  # the end still belongs to the last run
        # Without the merge the walk gets nowhere: the sparse names are 4 s apart, the roll's cadence reaches 3 s.
        assert rule_j.reach_back(read, runs[-1][0], *runs[-1], run=range(runs[-1][0], runs[-1][1] + 1)) == runs[-1][0]
        # `credits_start` still refuses the file: the guard counts the rows **as they were decoded**, and a bug on
        # every keyframe of the story is the share it is there to refuse (spec §5.4 step 8). The overlay step never
        # buys a file past it.
        assert rule_j.credits_start(rows, []) is None

    def test_the_walk_fires_alone_through_the_bug(self):
        # No earlier run to merge: names over bright footage run straight into the roll. The walk still has to read
        # the rows without the bug, or it crosses the story on it instead of stopping where the names begin.
        story = [band_row(t, 0, 160) for t in range(0, 440, 2)]
        rows = self._under_the_bug([*story, *self.NAMES_OVER_FOOTAGE, *self.ROLL])
        assert len(rule_j.credit_runs(rows)) == 1
        assert rule_j.coarse_start(rows).pts_s == 0.0
        coarse = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, rule_j.overlay_boxes(rows)))
        assert coarse is not None and coarse.pts_s == 440.0
        assert coarse.run_index is not None

    def test_a_file_with_no_overlay_answers_what_the_band_steps_alone_answer(self):
        # The cell that shows the overlay step costs nothing where there is no overlay: the same split roll with no
        # bug on it. `overlay_boxes` finds nothing, `without_overlays` hands the rows back, and both calls agree.
        rows = [*self.STORY, *self.OPENING, *self.NAMES_OVER_FOOTAGE, *self.ROLL]
        assert rule_j.overlay_boxes(rows) == ()
        assert rule_j.coarse_start(rows) == rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, ()))
        assert rule_j.coarse_start(rows).pts_s == 400.0


class TestReachBack:
    """A keyframe before the start is more of the same roll when its text is in the roll's band and it keeps the
    roll's own cadence."""

    ROLL = [band_row(t, 3, 160, 10.0) for t in range(500, 560, 2)]  # 2 s cadence, so the walk reaches 3 s

    def test_it_reads_presentation_order_not_ffmpegs(self):
        # Six of the 80 files emit the roll's keyframes in swapped pairs. Read in decode order the walk would compare
        # the wrong pair of times and stop at the first swap; read in presentation order it reaches the whole block.
        pairs = [row for t in range(470, 500, 4) for row in (band_row(t + 2, 2, 160), band_row(t, 2, 160))]
        rows = [*[band_row(t, 0, 160) for t in range(0, 470, 2)], *pairs, *self.ROLL]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 470.0

    def test_it_never_steps_onto_a_frame_the_start_already_passed(self):
        # A row inside the roll's own span emitted before the run. Walking in decode order would take it and start the
        # skip 12 s inside the roll -- the mistake `fade_back` and `coarse_end_s` were each fixed for.
        rows = [
            *[band_row(t, 0, 160) for t in range(0, 496, 2)],
            band_row(512, 2, 160),
            band_row(496, 2, 160),
            band_row(498, 2, 160),
            *self.ROLL,
        ]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 496.0

    def test_it_walks_over_lit_names_over_footage_in_the_band(self):
        rows = [*[band_row(t, 0, 160) for t in range(0, 470, 2)], *[band_row(t, 2, 160) for t in range(470, 500, 2)],
                *self.ROLL]  # fmt: skip
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 470.0

    def test_it_stops_at_text_outside_the_band(self):
        rows = [*[band_row(t, 0, 160) for t in range(0, 470, 2)], *[band_row(t, 2, 40) for t in range(470, 500, 2)],
                *self.ROLL]  # fmt: skip
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 500.0

    def test_it_stops_at_a_credit_frame_outside_the_band(self):
        # The band is the only thing the walk steps onto, and this is the row that makes that mean something. Names
        # over footage in the roll's band carry the walk back from 500 to 440; the next frame back is an earlier
        # *kept run*, at the roll's own 2 s cadence and well inside the walk's reach, but hard against the left edge
        # -- the run `same_roll` just refused on that band. Taking any credit frame, whatever its band, would step
        # onto it and walk to 400, making the merge's own test moot.
        earlier = [band_row(t, 3, 40, 10.0) for t in range(400, 440, 2)]
        names = [band_row(t, 2, 160) for t in range(440, 500, 2)]  # lit, two boxes: in band, not credit frames
        rows = [*[band_row(t, 0, 160) for t in range(0, 400, 2)], *earlier, *names, *self.ROLL]
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        assert rule_j.same_roll(rows, runs, rule_j.RULE_J) == 1  # the earlier run is refused on its band
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 440.0  # the names, not the out-of-band run behind them

    def test_it_stops_at_a_gap_wider_than_the_rolls_own_cadence(self):
        # Text in the band every 8 s: sporadic signage, not a roll's steady rate, and 8 s is past 1.5 x the roll's 2 s.
        rows = [*[band_row(t, 2 if t % 8 == 0 else 0, 160) for t in range(0, 500, 2)], *self.ROLL]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 500.0

    def test_it_does_not_cross_a_dark_stretch_the_run_stopped_at(self):
        # `credit_runs` has already joined everything the dark bridge reaches, so a stretch the run stopped at holds a
        # lit frame; letting the walk cross it as well took four of the 205's starts 20-70 s further early.
        rows = [
            *[band_row(t, 0, 160) for t in range(0, 440, 2)],
            *[band_row(t, 2, 160) for t in range(440, 460, 2)],  # names over footage, in the band
            *[band_row(t, 0, 160, 10.0) for t in range(460, 500, 2)],  # 40 s of black without text
            *self.ROLL,
        ]
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 500.0


def band(t: float, boxes: int, centre: int, luma: float = 120.0, jitter: int = 0) -> rule_j.Row:
    """A frame whose boxes sit around ``centre``, each one a pixel or two off the last (as the detector reads them)."""
    return (
        float(t),
        boxes,
        luma,
        tuple(
            (centre - 39 + jitter % 3, 20 + 30 * n + jitter % 2, centre + 38 - jitter % 2, 44 + 30 * n + jitter % 3)
            for n in range(boxes)
        ),
    )


class TestOverlayBoxes:
    """Text that sits in one place right across the story is a channel or score bug, a ticker or a burnt-in timecode,
    not credits (spec §13 item 15). Which run is the last one is still read from the rows as they were decoded; inside
    that run the boxes the overlay left decide which frames are credit frames (``coarse_start(..., without=...)``).

    The shape is the measured one: a channel logo the detector boxes now and then over lit story, and on every
    keyframe of a night scene where it stands out on black -- and a dark frame needs only one box, so that scene reads
    as a credit run (Live Rescue S03E01's night fire call under the A&E logo, 146 s early).
    """

    BUG = (4, 6, 46, 24)  # a channel logo, top left
    STORY = range(1000, 1300, 2)  # 150 lit keyframes
    NIGHT = range(1300, 1342, 2)  # 21 dark ones: a 40 s run
    ROLL = range(1400, 1460, 2)

    @classmethod
    def _bug(cls, i: int) -> rule_j.Box:
        """The bug as the detector reads it: its quadrilateral breathes a pixel or two with the picture behind it, so
        no two sightings are the same box and only ``OVERLAY_IOU`` gathers them (IoU here is 0.79 at worst)."""
        left, top, right, bottom = cls.BUG
        return (left + i % 3, top - i % 2, right - i % 2, bottom + i % 3)

    @classmethod
    def _tail(cls, every: int = 4, *, roll: bool = False, bug_until: float = 1e9) -> list[rule_j.Row]:
        rows = [
            (t, 1, 120.0, (cls._bug(i),)) if i % every == 0 and t <= bug_until else (t, 0, 120.0, ())
            for i, t in enumerate(cls.STORY)
        ]
        rows += [(t, 1, 12.0, (cls._bug(i),)) if t <= bug_until else (t, 0, 12.0, ()) for i, t in enumerate(cls.NIGHT)]
        if roll:
            rows += [(t, 0, 120.0, ()) for t in range(1342, 1400, 2)]
            rows += [
                (t, 3, 8.0, tuple((60, 30 + 12 * n + i, 260, 48 + 12 * n + i) for n in range(3)))
                for i, t in enumerate(cls.ROLL)
            ]
        return rows

    @staticmethod
    def _overlays(rows: list[rule_j.Row]) -> tuple[rule_j.Box, ...]:
        return rule_j.overlay_boxes(rows)

    def test_a_bug_over_a_night_scene_is_an_overlay_and_leaves_no_run(self):
        rows = self._tail()
        coarse = rule_j.coarse_start(rows)
        assert coarse is not None and coarse.pts_s == 1300.0  # what version 2 answers: 300 s of story skipped
        assert not rule_j.text_all_through(rows, coarse)  # 25 % of the keyframes before it carry text, under 80 %
        assert self._overlays(rows) == (self._bug(0),)
        assert rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, self._overlays(rows))) is None
        assert rule_j.credits_start(rows, []) is None

    def test_sightings_of_one_bug_are_gathered_by_overlap_not_equality(self):
        # Every sighting is a different box; at IoU 1.0 each would be its own group and none would be seen often
        # enough. Pins OVERLAY_IOU: the worst pair here overlaps 0.79.
        rows = self._tail()
        boxes = {row[3][0] for row in rows if row[3]}
        assert len(boxes) > 4 and len(self._overlays(rows)) == 1
        assert min(rule_j._iou(a, b) for a in boxes for b in boxes) >= rule_j.OVERLAY_IOU

    def test_a_real_roll_under_the_same_bug_still_answers_on_its_first_card(self):
        rows = self._tail(roll=True)
        overlays = self._overlays(rows)
        assert len(overlays) == 1
        coarse = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays))
        assert coarse is not None and coarse.pts_s == 1400.0
        assert rule_j.credits_start(rows, []) == 1400.0

    def test_thinning_a_run_can_stop_the_anchor_stepping(self, monkeypatch):
        # The overlay step is not monotone in either direction, and this is the direction the two halves' own
        # write-ups got wrong: dropping boxes thins the chosen run's credit frames, which grows the spacing the
        # anchor measures, which can stop the anchor stepping over a frame the 24 s join glued on. The start then
        # lands **earlier** than version 2's, bounded by that join, on an ordinary single run with no split roll.
        # A channel logo over story, a lit shop front 20 s before the roll, and a roll whose cards alternate with
        # frames the logo is the only text on: the run's raw cadence is 8 s so the anchor steps over the shop front,
        # and its cadence without the logo is 16 s so 1.5 x 16 covers the 20 s gap and it doesn't. Both publish.
        signs = ((150, 30, 250, 50), (150, 60, 250, 80), (150, 90, 250, 110))
        card = (60, 40, 260, 58)
        rows = [
            (float(t), 1, 120.0, (self._bug(i),)) if i % 4 == 0 else (float(t), 0, 120.0, ())
            for i, t in enumerate(range(0, 480, 2))
        ]
        rows.append((480.0, 4, 120.0, (self._bug(1), *signs)))  # the shop front
        rows += [(float(t), 0, 120.0, ()) for t in range(482, 500, 2)]
        carded, bug_only = {500, 516, 528, 532}, {510, 524}
        for i, t in enumerate(range(500, 700, 2)):
            if t in carded:
                rows.append((float(t), 2, 12.0, (self._bug(i), card)))
            elif t in bug_only:
                rows.append((float(t), 1, 12.0, (self._bug(i),)))
            else:
                rows.append((float(t), 0, 12.0, ()))
        bare = [row[:3] for row in rows]
        assert self._overlays(rows) == (self.BUG,)
        assert len(rule_j.credit_runs(rows)) == 1  # no split roll, no merge
        assert rule_j.coarse_start(bare).pts_s == 500.0  # version 2: the roll's first card
        assert rule_j.credits_start(bare, []) == 500.0
        assert rule_j.credits_start(rows, []) == 480.0  # version 3: the shop front, 20 s of story earlier
        # And it is the overlay step's own bound, not something the band steps bring: five places in the code and the
        # docs say "reproduces with the band steps stubbed out", so stub them and say it here.
        monkeypatch.setattr(rule_j, "same_roll", lambda rows, runs, params=rule_j.RULE_J: len(runs) - 1)
        monkeypatch.setattr(
            rule_j, "reach_back", lambda rows, index, first, last, params=rule_j.RULE_J, *, run=None: index
        )
        assert rule_j.credits_start(rows, []) == 480.0

    def test_a_split_roll_can_be_gathered_as_its_own_overlay(self):
        # The one place version 3's two halves work against each other, pinned so it can't be lost (spec §13 item 14,
        # `rule_j.overlay_boxes`). The roll fills most of its tail: opening cards, then names over bright footage,
        # then the closing crawl, all in one band. The join splits it, so the opening block and the names are "story"
        # to the overlay step, their text is held at one place across all of it, and it is gathered exactly as a
        # channel bug would be. `same_roll` then reads the keyframes between the blocks as blank and refuses the
        # merge the band step exists for, leaving the answer where version 2 had it (not *earlier* than version 2 --
        # see `test_thinning_a_run_can_stop_the_anchor_stepping` for the way this step can do that).
        rows = [band(t, 0, 160) for t in range(0, 40, 2)]
        rows += [band(t, 3, 160, 10.0, i) for i, t in enumerate(range(40, 80, 2))]
        rows += [band(t, 2, 160, 120.0, i) for i, t in enumerate(range(80, 500, 2))]
        rows += [band(t, 3, 160, 10.0, i) for i, t in enumerate(range(500, 700, 2))]
        runs = rule_j.credit_runs(rows)
        assert len(runs) == 2
        assert rule_j.same_roll(rows, runs, rule_j.RULE_J) == 0  # the band step alone puts the roll back together
        assert rule_j.coarse_start(rows).pts_s == 40.0  # ... and answers on its first card
        overlays = self._overlays(rows)
        assert len(overlays) == 2  # the roll's own two card lines, gathered as if they never moved
        assert rule_j.same_roll(rule_j.without_overlays(rows, overlays), runs, rule_j.RULE_J) == 1  # merge refused
        assert rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays)).pts_s == 500.0

    def test_the_band_steps_never_walk_the_start_back_over_the_bug(self):
        # Version 3's two halves, in order. The roll's cards and the bug are both text, so read as decoded the bug is
        # in the roll's band on every keyframe of the story and ``reach_back`` walks the start the whole way back to
        # the first one. The overlays are dropped first, so the walk has nothing to step onto and the start stays on
        # the roll's first card. Measured: read raw, this shape cost two right answers on the CPU decode of the 51
        # broadcast recordings (`evidence/eval/broadcast-tv.md`, "Rule J version 3").
        rows = [(t, 1, 120.0, (self._bug(i),)) for i, t in enumerate(range(1000, 1400, 2))]  # the bug over story
        rows += [
            (t, 3, 8.0, (self._bug(i), (2, 60, 44, 78), (6, 90, 48, 108)))  # cards in the bug's own band
            for i, t in enumerate(self.ROLL)
        ]
        raw = rule_j.coarse_start(rows)
        assert raw is not None and raw.pts_s == 1000  # the walk crossed the whole story on the bug
        overlays = self._overlays(rows)
        assert len(overlays) == 1
        coarse = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays))
        assert coarse is not None and coarse.pts_s == 1400.0
        assert coarse.run_index is None  # nothing reached back at all

    def test_the_run_is_still_the_one_the_rows_as_decoded_give(self):
        # The bug's night scene comes after the roll. Version 2 would answer on it; dropping the bug's boxes before
        # finding the runs would hand the answer to the roll instead. The run stays the last raw one, and it holds no
        # credit frame of its own, so there is no answer -- the story the bug sat on is never published.
        roll = [
            (t, 4, 8.0, (self._bug(i), *((60, 30 + 12 * n, 260, 48 + 12 * n) for n in range(3))))
            for i, t in enumerate(range(940, 1000, 2))
        ]
        rows = [*roll, *self._tail()]
        assert rule_j.coarse_start(rows).pts_s == 1300.0
        overlays = self._overlays(rows)
        assert len(overlays) == 1
        assert rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays)) is None

    def test_a_run_the_bug_leaves_one_credit_frame_of_is_not_a_roll(self):
        # One frame is not a run: ``credit_runs`` can't return one, and taking it would make a start that is also its
        # own end. Mayday S11E11's engine-animation telemetry under the National Geographic logo, on both paths.
        rows = [
            *[(t, 1, 120.0, (self._bug(i),)) for i, t in enumerate(self.STORY)],
            *[(t, 1, 12.0, (self._bug(i),)) for i, t in enumerate(range(1300, 1340, 2))],
            (1340, 2, 12.0, (self._bug(1), (60, 100, 260, 118))),
        ]
        # Read as decoded, every story frame's one box is the bug, in the run's own band and at its own cadence, so
        # the band steps walk the start right across the story to 1000 -- which is why the overlays are dropped first
        # (:class:`TestWhereRuleJReadsPositions`, and ``coarse_start``'s own docstring).
        assert rule_j.coarse_start(rows).pts_s == 1000
        overlays = self._overlays(rows)
        assert len(overlays) == 1
        assert rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays)) is None

    @pytest.mark.parametrize(("every", "found"), [(4, True), (20, True), (24, False)])
    def test_how_often_the_bug_has_to_be_boxed_over_the_story(self, every, found):
        # The night scene is the run, so only the lit story's own 150 rows count, against a share of 7.5 sightings:
        # every 20th keyframe is 8 of them and every 24th is 7. A logo boxed this thinly is still an overlay.
        assert bool(rule_j.overlay_boxes(self._tail(every))) is found

    def test_a_bug_that_leaves_the_screen_half_way_through_the_story_is_not_an_overlay(self):
        # The bug is boxed just as often, and the night scene is still a run -- a subtitle keeps it one once the bug
        # has gone -- so only its span decides. It covers 150 s of a 298 s story, under OVERLAY_SPAN_SHARE.
        subtitle = (60, 140, 260, 160)
        rows = self._tail(bug_until=1150)
        rows = [row if row[0] <= 1150 or row[0] < 1300 else (row[0], 1, 12.0, (subtitle,)) for row in rows]
        assert rule_j.credit_runs(rows)[-1:] == [(len(rows) - 21, len(rows) - 1)]
        assert rule_j.overlay_boxes(rows) == ()

    def test_a_promo_lower_third_is_not_an_overlay(self):
        # On for 40 s in the middle of the story: often enough, but nowhere near across it.
        promo = (40, 140, 280, 164)
        rows = [(t, 1, 120.0, (promo,)) if 1200 <= t < 1240 else (t, 0, 120.0, ()) for t in self.STORY]
        rows += [(t, 1, 12.0, (self._bug(i),)) for i, t in enumerate(self.NIGHT)]
        assert rule_j.overlay_boxes(rows) == ()

    def test_a_bug_seen_only_inside_the_run_is_the_runs_own_text(self):
        # A roll long enough to fill the tail puts its own cards in the same place for most of the rows (the lab's
        # Heeramandi episodes, a 462 s roll against a 450 s tail). Nothing of it is seen outside the run, so it stays.
        rows = [(1000 + 2 * i, 0, 120.0, ()) for i in range(25)]
        rows += [
            (1050 + 2 * i, 3, 8.0, ((60, 40, 260, 58), (60, 70, 260, 88), (60, 100, 260, 118))) for i in range(200)
        ]
        assert rule_j.overlay_boxes(rows) == ()
        assert rule_j.credits_start(rows, []) == 1050.0

    def test_the_refine_rows_are_read_without_the_bug_too(self):
        # The 1 fps rows before the roll are a dark scene the bug is the only text on. Read as they are, the walk back
        # takes every one of them and the start lands 20 s early; read without it, it stops on the roll's first card.
        rows = self._tail(roll=True)
        fine = [(float(t), 1, 12.0, (self._bug(t - 1380),)) for t in range(1380, 1400)]
        fine += [
            (float(t), 3, 8.0, ((60, 30, 260, 48), (60, 42, 260, 60), (60, 54, 260, 72))) for t in range(1400, 1402)
        ]
        overlays = self._overlays(rows)
        coarse = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays))
        assert rule_j.refine_start(rows, coarse, fine) == 1380.0  # what reading them as they are would give
        assert rule_j.credits_start(rows, fine) == 1400.0

    def test_a_start_the_bug_pushed_later_can_carry_a_run_past_the_30_s_floor(self):
        # One of the two ways version 3 can *gain* an answer (the other is the share, below). The guard counts each
        # row's own text as it was decoded, but both its tests are measured from the start, and dropping the bug's
        # boxes moves that start later. This row is the 30 s floor: version 2 refuses the file because the run opens
        # 28 s into the tail; version 3 starts on the cards 60 s in. No file of the sets, the lab or the 51
        # broadcast recordings has it.
        rows = [
            (1000.0 + 2 * i, 1, 120.0, (self._bug(i),)) if i % 4 == 0 else (1000.0 + 2 * i, 0, 120.0, ())
            for i in range(14)
        ]
        rows += [(1028.0 + 2 * i, 1, 12.0, (self._bug(i),)) for i in range(16)]  # the bug over a night scene
        rows += [(1060.0 + 2 * i, 2, 12.0, (self._bug(i), (60, 40, 260, 58))) for i in range(30)]  # the roll
        v2 = rule_j.coarse_start(rows)
        assert v2.pts_s == 1028.0 and rule_j.text_all_through(rows, v2)  # version 2: no answer
        overlays = self._overlays(rows)
        v3 = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays))
        assert v3.pts_s == 1060.0 and not rule_j.text_all_through(rows, v3)
        assert rule_j.credits_start(rows, []) == 1060.0

    def test_a_start_the_bug_pushed_later_can_drop_the_guards_share(self):
        # The guard's other test, the same way: its window is everything before the start, so moving the start moves
        # the window's edge and the share with it. Here the 30 s floor is clear for both versions and the *share*
        # alone decides. Version 2's run opens on the bug's night scene, so the 20 keyframes before it all carry the
        # bug and the share is 1.00 -- refused. Without the bug those frames aren't credit frames, the run opens on
        # the real cards 122 s in, and the 30 dark keyframes the dark bridge had swept into the run are now before
        # the start and blank: 0.51, under the 0.80, so the file gains an answer version 2 refused.
        card = (60, 40, 260, 58)
        rows = [(1000.0 + 2 * i, 1, 120.0, (self._bug(i),)) for i in range(20)]  # lit story under the bug
        rows += [(1040.0 + 2 * i, 1, 12.0, (self._bug(i),)) for i in range(11)]  # the bug over a night scene
        rows += [(1062.0 + 2 * i, 0, 12.0, ()) for i in range(30)]  # blank dark: the dark bridge sweeps these in
        rows += [(1122.0 + 2 * i, 2, 12.0, (self._bug(i), card)) for i in range(40)]  # the roll
        bare = [row[:3] for row in rows]
        v2 = rule_j.coarse_start(bare)
        assert v2.pts_s == 1040.0 and v2.pts_s - rows[0][0] >= rule_j.STORY_BEFORE_RUN_S  # the floor is clear
        assert rule_j.text_all_through(bare, v2) and rule_j.credits_start(bare, []) is None  # refused on the share
        v3 = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, self._overlays(rows)))
        assert v3.pts_s == 1122.0
        assert not rule_j.text_all_through(rows, v3)
        assert rule_j.credits_start(rows, []) == 1122.0

    def test_a_roll_the_bug_started_early_keeps_only_its_own_first_card(self):
        # A stand-up special's stage backdrop signage, boxed in one place through the act: the 24 s join glues the
        # last of it onto the roll, and dropping it moves the start onto the roll's first card (the 205's Gaurav
        # Gupta Market Down Hai, 32 s early -> 10 s).
        backdrop = (20, 30, 120, 50)
        rows = [(t, 2, 40.0, (backdrop, (150, 30, 250, 50))) for t in range(850, 1000, 2)]  # story: 2 boxes, not credit
        rows += [(t, 3, 40.0, (backdrop, (150, 30, 250, 50), (150, 60, 250, 80))) for t in range(1000, 1280, 2)]
        rows += [(t, 1, 40.0, (backdrop,)) for t in range(1280, 1300, 2)]
        rows += [(t, 3, 8.0, ((60, 40, 260, 58), (60, 70, 260, 88), (60, 100, 260, 118))) for t in range(1300, 1360, 2)]
        assert rule_j.coarse_start(rows).pts_s == 1000.0
        overlays = rule_j.overlay_boxes(rows)
        assert overlays == (backdrop, (150, 30, 250, 50))  # both signs are on screen through the act
        coarse = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, overlays))
        assert coarse is not None and coarse.pts_s == 1300.0

    def test_a_credit_line_crossing_the_bug_keeps_its_own_box(self):
        # Containment, not overlap: only text the bug swallows is dropped.
        across = (0, 6, 300, 24)
        rows = [*self._tail(), *[(1400 + 2 * i, 1, 8.0, (across,)) for i in range(20)]]
        kept = rule_j.without_overlays(rows, rule_j.overlay_boxes(rows))
        assert [row[1] for row in kept[-20:]] == [1] * 20

    @pytest.mark.parametrize(
        ("card", "dropped"),
        # A credit line at the bug's own height, reaching further and further out of it: wholly inside, then 0.61
        # inside, then 0.56. Either side of OVERLAY_CONTAINMENT, which no equality test would pin.
        [((30, 6, 48, 24), True), ((30, 6, 60, 24), True), ((30, 6, 63, 24), False)],
    )
    def test_how_far_inside_the_bug_a_box_has_to_lie(self, card, dropped):
        overlay = (0, 4, 48, 26)
        inside = rule_j._inside(card, overlay)
        assert (inside >= rule_j.OVERLAY_CONTAINMENT) is dropped, inside
        assert rule_j._iou(card, overlay) < rule_j.OVERLAY_IOU  # overlap alone would keep every one of them
        kept = rule_j.without_overlays([(1000.0, 1, 8.0, (card,))], (overlay,))
        assert kept[0][1] == (0 if dropped else 1)

    @pytest.mark.parametrize(("sightings", "found"), [(3, False), (4, True)])
    def test_a_short_story_needs_the_four_sighting_floor(self, sightings, found):
        # 20 story keyframes, so the 5 % share would let two far-apart boxes make an overlay. OVERLAY_LEAST is what
        # stops a coarsely keyed tail doing that.
        story = range(1000, 1040, 2)
        step = (len(story) - 1) // (sightings - 1)
        rows = [
            (t, 1, 120.0, (self._bug(i),)) if i % step == 0 and i <= step * (sightings - 1) else (t, 0, 120.0, ())
            for i, t in enumerate(story)
        ]
        rows += [(t, 1, 12.0, (self._bug(i),)) for i, t in enumerate(range(1040, 1082, 2))]
        assert sum(1 for row in rows[: len(story)] if row[1]) == sightings
        assert rule_j.OVERLAY_KEYFRAME_SHARE * len(story) < rule_j.OVERLAY_LEAST  # the floor is what decides here
        assert bool(rule_j.overlay_boxes(rows)) is found

    def test_rows_without_positions_have_no_overlay_and_keep_their_counts(self):
        # The 80-file fixture's rows were measured before positions were recorded; rule J answers them as it did.
        rows = [(1000 + 2 * i, 1, 12.0) for i in range(200)]
        assert rule_j.overlay_boxes(rows) == ()
        assert [row[1] for row in rule_j.without_overlays(rows, (self.BUG,))] == [1] * 200

    def test_a_row_list_that_isnt_the_same_rows_is_refused(self):
        rows = self._tail()
        with pytest.raises(ValueError, match="same rows"):
            rule_j.coarse_start(rows, without=rows[:-1])

    def test_a_story_with_no_time_in_it_is_not_looked_at(self):
        # Every story row at the same pts: the span test would read "0 s apart or more", which every group clears.
        rows = [(1000.0, 1, 120.0, (self._bug(i),)) for i in range(6)]
        rows += [(1000.0 + 2 * i, 1, 12.0, (self._bug(i),)) for i in range(1, 16)]
        assert rule_j.credit_runs(rows) and rule_j.overlay_boxes(rows) == ()

    def test_a_file_with_no_run_is_not_looked_at(self):
        rows = [(1000 + 2 * i, 1, 120.0, (self.BUG,)) for i in range(200)]  # lit: one box is never a credit frame
        assert rule_j.credit_runs(rows) == []
        assert rule_j.overlay_boxes(rows) == ()

    @pytest.mark.parametrize(
        "name", ["Synth Audio (2022) - S01E02", "Synth Audio (2022) - S01E04", "Synth Audio (2022) - S02E01"]
    )
    def test_the_labs_burnt_in_timecode_is_an_overlay(self, name):
        # The three episodes whose keyframes made a run: the guard refused them, and now there is no run to refuse.
        item = _synth()[name]
        key = _synth_rows(item["key"])
        overlays = rule_j.overlay_boxes(key)
        assert rule_j.coarse_start(key) is not None and overlays
        assert rule_j.coarse_start(key, without=rule_j.without_overlays(key, overlays)) is None

    @pytest.mark.parametrize("name", ["Synth Credits (2024)", "Synth Credits Open (2025)"])
    def test_the_labs_synthetic_roll_has_no_overlay(self, name):
        item = _synth()[name]
        key, fine = _synth_rows(item["key"]), _synth_rows(item["fine"])
        assert rule_j.overlay_boxes(key) == ()
        assert rule_j.credits_start(key, fine) == 541.0
