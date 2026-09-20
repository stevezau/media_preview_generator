"""Rule "J": where the credit roll starts, and where it ends when a scene follows it, from the text boxes and brightness
of a file's ending (spec §5.4; the end is the owner's Q3 ruling, 2026-09-16).

A port of the measured prototype (``evidence/credits/eval_rules3.py`` ``detect`` with rule J's parameters, keyframe
mode, refined over the 20 s before the coarse answer): same inputs, same order, same comparisons, bar the anchor, whose
yardstick and walk were both wrong. ``_run_spacing`` measures the gap between the credit run's own credit frames in
presentation order, where the prototype took the median gap of every row of the whole decoded tail in decode order; and
``ANCHOR_MAX_STEPS`` holds the walk to the one glued-on frame its comment always claimed it stepped over. Together they
fix a start that collapsed onto the end of the roll and move the 80 files at the spec's 20 s refine span from the
prototype's 59 / 1 / 8 / 4 to 63 / 1 / 8 / 4, every changed file closer to the truth than the prototype had it
(§5.4's table was measured at 10 s, late 9; ``tests/fixtures/markers/credits_rule_j_80.json.gz``,
whose stored errors stay the prototype's -- ``PORT_DIVERGENCES`` in ``tests/markers/credits/test_rule_j.py`` names
the items that differ).
Version 2 (spec §13 items 13 and 14, ``evidence/eval/phase3-harness.md``) adds three things the prototype never had:
the anchor never steps over a gap longer than the 24 s join (:func:`coarse_start`), the end steps back over scene text
the join glued on after the roll (:func:`end_keyframe_s`), and text on screen all through the tail is not a roll
(:func:`text_all_through`, with a roll the tail opens on judged together with the keyframes before the tail,
:func:`opens_on_the_run`). The 80 files move to 64 / 1 / 7 / 4.
Version 3 (spec §13 items 14 and 15) is the first rule to read *where* a frame's text is. Two mechanisms read it:

* **Text that never moves is not credits.** A box position the detector keeps finding right across the story is a
  channel or score bug, a ticker or a burnt-in timecode, and inside the chosen run it doesn't count as text at all
  (:func:`overlay_boxes`, :func:`without_overlays`, ``coarse_start(rows, without=...)``).
* **A roll the 24 s join split is put back together from where its text sits.** A roll's cards, names and crawl keep
  to one band across the frame, so an earlier run in that band whose text never stops is the same roll
  (:func:`same_roll`), and a keyframe before the start in that band, at the run's own cadence, is more of it
  (:func:`reach_back`). Both only ever move a start earlier, and the end is still measured from the run alone
  (:func:`_run_rows`).

**The order of the two decides what they do to each other.** The overlays are found first, on the rows as they were
decoded. :func:`credit_runs` goes on reading those rows, and with it :func:`opens_on_the_run`, so the overlay step
can never unmask an earlier run; and :func:`text_all_through` counts each row's own text there too, so it never sees
a keyframe the overlay emptied as blank. Everything else about the guard is downstream of the start: **both of its
tests are measured from one the overlay step may have moved**, so that step can carry a run past the 30 s floor *and*
move the share the guard counts -- the window's edge is that start -- in either direction. A file version 2 refused
can gain an answer either way (:func:`text_all_through`; pinned by
``test_rule_j.TestOverlayBoxes.test_a_start_the_bug_pushed_later_can_carry_a_run_past_the_30_s_floor`` and
``...test_a_start_the_bug_pushed_later_can_drop_the_guards_share``).
Everything that reads *a frame's own text* -- which of the run's
frames are credit frames and the spacing the anchor measures, :func:`same_roll`'s bands and its share of texted
keyframes between two runs, :func:`reach_back`'s band test and cadence, and the ends -- reads the rows with the
overlays dropped. That second half is what keeps the band from walking a start over story keyframes whose only box is
the channel bug, which is what it does on broadcast recordings when it reads them raw.
**The overlay step is not monotone in either direction**, on its own and not only in the pair. Inside the chosen run,
dropping boxes thins the run's credit frames, which grows the spacing the anchor measures, which can stop the anchor
stepping over a frame the 24 s join glued on -- so a start can land *earlier* than version 2 put it, not only later
(:func:`_anchored`; pinned by ``test_rule_j.TestOverlayBoxes.test_thinning_a_run_can_stop_the_anchor_stepping``,
which reproduces with the band steps stubbed out). **How much earlier is not bounded by the 24 s join, and is not
bounded at all.** The join bounds the anchor, which takes at most ``ANCHOR_MAX_STEPS`` steps; :func:`reach_back` has
no step limit, and both of the things it reads move under the overlay step. Its cadence is capped by the run as
decoded so that thinning can no longer widen a step (:func:`reach_back`), but which frames it steps onto is
:func:`in_band` on the thinned boxes, and dropping a corner bug can move a story keyframe's median middle *into* the
roll's band. A synthetic tail of such frames answers the file's first row, the whole tail earlier than the band
steps alone (``test_rule_j.TestReachBack.test_dropping_a_bug_can_put_a_story_keyframe_in_the_band``). A start the
overlay step moves later can carry a run past the guard's 30 s floor, so a file version 2 refused can gain an
answer; that direction is unbounded too. No file of the sets, the lab or the 51 broadcast recordings does either,
and over all 285 set rows on both decode paths no answer of the pair is earlier than the band steps alone put it.
That measurement is the whole of the evidence: neither direction is bounded by an argument.
``coarse_end_s`` reads the run's latest credit frame in presentation order for the same reason, so Q3's end means the
end of the roll rather than whichever of its keyframes ffmpeg emitted last, and ``fade_back`` never steps onto a later
row. Everywhere else rows stay in ffmpeg's output order, the anchor's distance included: the measured keyframe rows
aren't always increasing, and sorting them changes an answer (4 of the 80 coarse starts, as measured).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from statistics import median

# One text box's place in its frame: left, top, right, bottom as **inclusive** pixel indices of the frame's own
# 320x180 -- 0 to 319 across and 0 to 179 down, so a box across the whole frame reads 0..319 and is 320 wide
# (``right - left + 1``). ``textdet.postprocess`` has already rounded every corner and clipped it to those ranges, so
# the bounds are exact.
Box = tuple[int, int, int, int]
# A frame: its time in seconds from the start of the file, how many text boxes it holds, its mean luma, and where those
# boxes are. Only version 3's :func:`overlay_boxes`, :func:`same_roll` and :func:`reach_back` read the fourth field;
# every other comparison, sort and index here is on the first three, as it was before positions arrived. A row that
# carries only three is read as a frame whose text could be anywhere, so it has no overlay and is never taken for a
# roll's own (:func:`boxes_of`).
Row = tuple[float, int, float, tuple[Box, ...]]

FADE_LUMA = 12.0
FADE_STEP_S = 4.0
REFINE_BEFORE_S = 20.0
REFINE_AFTER_S = 1.0
REFINE_GAP_S = 2.5
ANCHOR_SPACING_FACTOR = 1.5
# The anchor is there to step over *one* credit frame the 24 s join glued onto the roll from a story scene, so it takes
# one step. Unbounded, it eats the roll instead: on movie-02 it walked seven frames and 22.6 s past the real start.
ANCHOR_MAX_STEPS = 1
# Owner, Q3: the skip ends at the roll's last credit frame only when more than this much of the file follows it;
# otherwise it runs to the end of the file (no end), so a closing logo or a few seconds of black never become a stop.
KEEP_AFTER_CREDITS_S = 30.0
REFINE_END_BEFORE_S = 1.0
REFINE_END_AFTER_S = 20.0
# A roll follows story. Text on screen all through the tail makes credit runs wherever its frames read enough boxes: a
# dark frame with any, or a lit one that reads 3 now and then (the lab's burnt-in timecode). So there is no answer
# unless the rows hold at least this much before the run (84 s or more on every file of the harness's sets) ...
STORY_BEFORE_RUN_S = 30.0
# ... and fewer than this share of those keyframes carry any text. The harness's 245 files reach 0.54 (a stand-up
# special); the lab's Synth Audio test pattern with its running timecode, 1.00. The cost: a channel logo or ticker
# boxed on this share of the story loses a real roll's answer too. On 51 broadcast recordings with channel logos (final
# review) that was one answer (Live Rescue S02E01 on the GPU decode, 0.895), against five wrong ones this share and
# the 30 s above took away over both decode paths.
TEXT_ALL_THROUGH_SHARE = 0.8
# When the run is under STORY_BEFORE_RUN_S into the tail and only dark rows (luma under 30) come before it there, the
# roll may have begun before the tail (the lab's Heeramandi episodes: 462 s rolls against a 450 s tail), so the
# keyframes of this much before the tail are read too, and the run is judged on both when it continues into them
# (:func:`opens_on_the_run`, :func:`joined_before`). With 30 s of rows still wanted before the run, a roll that began up
# to 90 s before the tail is answered.
READ_BEFORE_TAIL_S = 120.0
# A channel bug, a score bug, a ticker or a burnt-in timecode is text in the *same place* all through the story, while
# a roll's text is only there for the roll. So a box position the detector keeps finding right across the story doesn't
# count as text at all (:func:`overlay_boxes`). The five numbers below were chosen on the 80, the 205 and the lab's
# synthetic files, never on the broadcast recordings they are for (spec §13 item 15,
# ``evidence/eval/broadcast-tv.md``): the only set answer any of them moves is one 205 movie's, and it moves 22 s
# closer to its truth at every keyframe share from 0.02 to 0.15 -- 0.20 loses it, 0.01 starts moving another.
# Boxes of one overlay are rarely pixel-identical (the detector's quadrilateral breathes with the picture behind it),
# so they are gathered by overlap rather than equality.
OVERLAY_IOU = 0.5
# First to last sighting, as a share of the story's own span.
OVERLAY_SPAN_SHARE = 0.8
# How often it has to be seen, as a multiple of the story's row count (sightings are boxes, so a group can be seen
# more often than there are rows). A logo over busy footage is only boxed now and then -- at 320 px most of the 51
# broadcast recordings' logos are boxed on well under half their keyframes -- so this is far below the share of
# keyframes carrying *any* text that makes :func:`text_all_through` refuse an answer.
OVERLAY_KEYFRAME_SHARE = 0.05
# ... but never fewer than this many sightings, so a coarsely keyed tail (a 450 s tail can hold under 60 keyframes)
# can't call two far-apart boxes an overlay. It is also the floor on how many rows a story needs before
# :func:`overlay_boxes` measures its span at all, so it can't be swept to 0 or 1.
OVERLAY_LEAST = 4
# A box is the overlay's when this much of it lies inside. Containment, not overlap: a credit line that happens to
# cross the bug keeps its own box, and only text the bug swallows is dropped.
OVERLAY_CONTAINMENT = 0.6
# Version 3: a roll's text keeps to one band across the frame. Its cards, names and crawl sit at about the same place
# frame after frame, where a scene's signs, captions and lower thirds wander, so a frame counts as one roll's own when
# the middle of its boxes is within this much of the middle of the roll's (:func:`in_band`). A tenth of the frame's
# 320 px: the widest band that adds no early answer. 16 and 24 px are as safe and reach one file less (205 Medium
# useful 99 against 100); 40 px and more -- and no band test at all -- put one more 205 answer over 10 s before its
# chapter (`evidence/eval/phase3-harness.md`, "Rule J version 3", "The two numbers, swept").
BAND_TOLERANCE_PX = 32.0
# ... and its text never stops: two runs are one roll only when at least this share of the keyframes between them
# carry a box, lit or dark. Half, and 0.4 gives the same rows; at 0.6 a measured roll is lost again and at 0.3 two
# early answers come back.
ROLL_TEXT_SHARE = 0.5


@dataclass(frozen=True)
class RuleParams:
    """Rule J's thresholds.

    Attributes:
        dense: Text boxes a bright frame needs to count as a credit frame (signage in a lit scene gave 3+ boxes for
            minutes: Checkin' It Twice).
        min_boxes: Text boxes a dark frame needs (cards on black often show only 1–2 boxes at 320 px).
        dark: Mean luma below which a frame is dark.
        gap_s: Credit frames join a run across gaps up to this long.
        run_s: The shortest run kept.
    """

    dense: int = 3
    min_boxes: int = 1
    dark: float = 30.0
    gap_s: float = 24.0
    run_s: float = 15.0


RULE_J = RuleParams()


@dataclass(frozen=True)
class Coarse:
    """The last credit run's start after the anchor step and the reach back over the rest of its roll.

    Every index here is into **the rows the rule read** -- the decoded rows with the overlays' boxes dropped, where
    there were any (``coarse_start(rows, without=...)``). The two lists are the same rows in the same order, so an
    index means the same frame in both; what differs is how much text that frame holds, which is what decides where
    the run stops and which of its frames are credit frames. Everything that reads a ``Coarse`` back --
    :func:`coarse_end_s`, :func:`end_keyframe_s`, :func:`refine_end`, :func:`credits_end`, :func:`_run_rows` -- has to
    be handed the same rows :func:`coarse_start` read, and answers silently differently if it isn't
    (``test_detector.TestFindCredits.test_the_end_window_is_read_and_walked_without_the_bug``).

    Attributes:
        index: The start's row in those rows.
        end_index: The run's last credit row in ffmpeg's output order, which is where the run stops, not when: the
            run's latest time is :func:`coarse_end_s`. With an overlay it is the run's last row that is still a credit
            frame once the overlay's boxes are gone, which can be earlier than the run's own last row.
        pts_s: The start's time.
        run_index: The chosen run's own anchored start, when :func:`same_roll` or :func:`reach_back` moved the start
            back before it; None when they didn't and ``index`` is that row itself. The end reads this one
            (:func:`_run_rows`), so reaching a start back can never move an end (Q3).
    """

    index: int
    end_index: int
    pts_s: float
    run_index: int | None = None


def boxes_of(row: Row) -> tuple[Box, ...]:
    """Where a frame's text is, or nothing when the row doesn't say.

    Every row the app decodes carries its boxes (``frames.decode_rows``), and the harness refuses a stored decode that
    doesn't. The rows that don't are the 80-file fixture's (``tests/fixtures/markers/credits_rule_j_80.json.gz``: the
    prototype recorded how many boxes a frame held and never where they were) and a few raw tuples in the tests -- the
    tests' own ``dark()`` and ``bright()`` rows do carry boxes, centred, so version 3's steps fire on them. A row
    without them is read as a frame whose text could be anywhere: it has no overlay (:func:`overlay_boxes`) and is
    never taken for a roll's own (:func:`same_roll`, :func:`reach_back`), so its answer is version 2's.
    """
    return row[3] if len(row) > 3 else ()


def _iou(a: Box, b: Box) -> float:
    """How much two boxes overlap, over the area they cover together.

    Bounds are inclusive, so a side is ``b - a + 1``.
    """
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    if right < left or bottom < top:
        return 0.0
    both = (right - left + 1) * (bottom - top + 1)
    return both / ((a[2] - a[0] + 1) * (a[3] - a[1] + 1) + (b[2] - b[0] + 1) * (b[3] - b[1] + 1) - both)


def _inside(box: Box, holder: Box) -> float:
    """The share of ``box`` that lies inside ``holder``."""
    left, top = max(box[0], holder[0]), max(box[1], holder[1])
    right, bottom = min(box[2], holder[2]), min(box[3], holder[3])
    if right < left or bottom < top:
        return 0.0
    return ((right - left + 1) * (bottom - top + 1)) / ((box[2] - box[0] + 1) * (box[3] - box[1] + 1))


def overlay_boxes(rows: Sequence[Row], params: RuleParams = RULE_J) -> tuple[Box, ...]:
    """The places where text sits on screen right across the story: a channel or score bug, a ticker, a burnt-in
    timecode.

    The story is the rows before the run rule J would otherwise pick, and only the story's boxes are gathered. That is
    what keeps a roll long enough to fill the tail out of the gather while it is **one run** -- the lab's Heeramandi
    episodes, a 462 s roll against a 450 s tail, put the same card in the same place for most of the rows and are not
    read as their own overlay.

    **It is not a roll the 24 s join split.** That roll's opening block and its names over footage sit in front of the
    last run, so they are story to this step, and text held at one place across them is gathered exactly as a channel
    bug would be. :func:`same_roll` then reads the keyframes between the blocks as blank and refuses the merge the
    band step exists for, so the answer usually comes from the later block, where version 2 had it, the gain
    forfeited. It is the one place version 3's two halves work against each other, and it needs a roll that fills
    most of its own tail: no file of the sets, the lab or the 51 broadcast recordings has the shape, and every answer
    the band steps move on them, the pair moves to the same second. The answer is **not** guaranteed to be no
    earlier than version 2's: this step's own non-monotonicity applies here as everywhere -- thinning the chosen run
    can stop the anchor stepping over a glued-on frame, and dropping a bug's box can put a story keyframe in the
    roll's band for a walk that has no step limit (see the module docstring and :func:`reach_back`) -- so this shape
    too is bounded by neither version 2 nor the 24 s join, but by the tail. Three ways of reading round it were
    measured and each cost a measured broadcast answer -- bounding the story by the merged roll, dropping the kept
    runs' own rows, and reading the share on the rows as decoded (``evidence/eval/phase3-harness.md``, "Tried and not
    taken (this round)"). Pinned by
    ``test_rule_j.TestOverlayBoxes.test_a_split_roll_can_be_gathered_as_its_own_overlay``.

    Boxes are gathered by overlap (``OVERLAY_IOU`` against the first box of each group, which is what the group is
    named by). A group is an overlay when its first and last sighting are at least ``OVERLAY_SPAN_SHARE`` of the story
    apart, and it was seen at least ``OVERLAY_LEAST`` times and at least ``OVERLAY_KEYFRAME_SHARE`` times the story's
    row count. Sightings are boxes, not frames: two boxes of one frame that both land on the group count twice, which
    is a word-for-word thing only where a bug's two lines sit on top of each other.

    This is not :func:`text_all_through` one box at a time. That step asks how many keyframes carry *any* text and
    refuses an answer for the whole file; this one asks where the text is, and takes away only the boxes that never
    move, leaving the rest of the frame to be read as usual.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        params: Rule thresholds, deciding which run the story ends at.

    Returns:
        One box per overlay (the group's first), in the order the groups were opened. Empty when the rows hold no run
        at all -- dropping boxes only ever takes credit frames away, so a file without a run can't gain one -- or when
        the story is too short to tell an overlay from a shot.
    """

    runs = credit_runs(rows, params)
    if not runs:
        return ()
    run_from = min(row[0] for row in rows[runs[-1][0] : runs[-1][1] + 1])
    story = [row for row in rows if row[0] < run_from]
    if len(story) < OVERLAY_LEAST:
        return ()
    times = [row[0] for row in story]
    story_s = max(times) - min(times)
    if story_s <= 0:
        return ()
    groups: list[list] = []  # [box, first seen, last seen, sightings]
    for row in story:
        pts = row[0]
        for box in boxes_of(row):
            best, best_iou = None, 0.0
            for group in groups:
                overlap = _iou(box, group[0])
                if overlap >= OVERLAY_IOU and overlap > best_iou:
                    best, best_iou = group, overlap
            if best is None:
                groups.append([box, pts, pts, 1])
            else:
                best[1], best[2], best[3] = min(best[1], pts), max(best[2], pts), best[3] + 1
    least = max(OVERLAY_LEAST, OVERLAY_KEYFRAME_SHARE * len(story))
    return tuple(
        box
        for box, first_s, last_s, seen in groups
        if last_s - first_s >= OVERLAY_SPAN_SHARE * story_s and seen >= least
    )


def without_overlays(rows: Sequence[Row], overlays: Sequence[Box]) -> list[Row]:
    """The rows with every box an overlay swallows dropped, and each row's count recounted.

    Args:
        rows: Any rows of the file (the tail's keyframes, a 1 fps window).
        overlays: :func:`overlay_boxes` of that file's tail. Empty leaves the rows as they are.

    Returns:
        Rows of the same length and order, so an index into one indexes the other.
    """
    if not overlays:
        return list(rows)
    out = []
    for row in rows:
        if len(row) <= 3:
            # A row that never recorded its boxes keeps the count it was measured with, rather than losing it to an
            # overlay found on rows that did (:func:`boxes_of`).
            out.append(row)
            continue
        kept = tuple(
            box for box in row[3] if not any(_inside(box, overlay) >= OVERLAY_CONTAINMENT for overlay in overlays)
        )
        out.append((row[0], len(kept), row[2], kept))
    return out


def is_credit(row: Row, params: RuleParams = RULE_J) -> bool:
    """Whether a frame shows credits: a dark frame with text, or a bright frame with a lot of it."""
    if row[2] < params.dark:
        return row[1] >= params.min_boxes
    return row[1] >= params.dense


def credit_runs(rows: Sequence[Row], params: RuleParams = RULE_J) -> list[tuple[int, int]]:
    """Runs of credit frames (first and last row index) at least ``run_s`` long, in row order."""
    runs: list[list[int]] = []
    last_bright = -1.0
    for i, row in enumerate(rows):
        if is_credit(row, params):
            joined = bool(runs) and row[0] - rows[runs[-1][1]][0] <= params.gap_s
            # Dark frames without text never break a run (Summit of the Gods: 24 s of dark keyframes split the roll).
            if runs and last_bright < rows[runs[-1][1]][0]:
                joined = True
            if joined:
                runs[-1][1] = i
            else:
                runs.append([i, i])
        elif row[2] >= params.dark:
            last_bright = row[0]
    return [(first, last) for first, last in runs if rows[last][0] - rows[first][0] >= params.run_s]


def _typical_spacing(rows: Sequence[Row]) -> float:
    gaps = sorted(rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1))
    # The prototype indexes len(rows) // 2 into len(rows) - 1 gaps; with two rows that index doesn't exist.
    return gaps[min(len(rows) // 2, len(gaps) - 1)]


def _run_spacing(rows: Sequence[Row], first: int, last: int, params: RuleParams) -> float:
    """The typical gap between one credit run's credit frames, measured in presentation order.

    The anchor asks how far away the next *credit frame* is, so its yardstick has to measure the same thing. Three
    things have to line up for that, and the prototype had none of them:

    * The population is the run, not the whole decoded tail. A tail median lets a densely keyed body set the limit: a
      1 s keyframe action climax before a 2 s GOP roll puts ``1.5 x spacing`` under the roll's own gap, no pair of
      credit rows ever looks adjacent, and the start walks all the way to the end of the run.
    * Only the run's credit frames count. A run also carries the keyframes the 24 s join and the dark bridge swept up
      with it, and those are often the majority -- tv-22's run holds 16 rows of which 6 are credit frames, so the
      keyframe cadence reads 1.919 s where the credit cadence is 6.256 s. Measuring every row is the same
      yardstick-against-comparison mismatch one layer down.
    * Presentation order. The measured rows aren't always increasing, so consecutive differences go negative and the
      median stops being a gap at all: on the six of the 80 files whose runs come out of decode order it read two to
      three times the real interval (movie-28: 20.020 s for a 10.010 s roll).

    Args:
        rows: Keyframe rows of the tail, in decode order.
        first: The run's first row index.
        last: The run's last row index.
        params: Rule thresholds, deciding which of the run's rows are credit frames.

    Returns:
        The typical gap in seconds. A run always holds at least two credit frames: both its first and its last row are
        credit frames, and it spans ``run_s`` or more.
    """
    credit = [row for row in rows[first : last + 1] if is_credit(row, params)]
    return _typical_spacing(sorted(credit, key=lambda row: row[0]))


def _credit_bounds(rows: Sequence[Row], first: int, last: int, params: RuleParams) -> tuple[int, int] | None:
    """One run's own credit frames, as the rows the anchor reads have them, or None when fewer than two are left.

    :func:`credit_runs` can't return a run of one frame, and both ends of one it does return are credit frames. That
    stops holding once the overlays are dropped: a run the bug accounted for all but one of isn't a roll either, and
    taking it would make a start that is also its own end. With no overlay to drop this is the run itself.
    """
    credit = [i for i in range(first, last + 1) if is_credit(rows[i], params)]
    return (credit[0], credit[-1]) if len(credit) >= 2 else None


def _anchored(rows: Sequence[Row], first: int, last: int, params: RuleParams) -> int:
    """One run's anchored start row, read from ``rows``: the rule's rows with the overlays' boxes dropped.

    Start only where two credit samples sit next to each other: the 24 s join lets one story-scene text frame glue
    itself onto the roll (Undisputed: a lone scene-text frame 24 s before it). Only ever the one frame, though --
    see ANCHOR_MAX_STEPS. The slice ``_run_spacing`` measures is [first:last+1], not last+2: the run's own credit
    frames are what the yardstick measures, so a row outside the run is out of scope whatever it is -- including the
    rare case where it's itself a credit frame (a trailing run under ``run_s`` gets dropped by :func:`credit_runs`, so
    ``runs[-1]`` can leave one sitting right past ``last`` when decode order is non-monotonic).
    """
    spacing = _run_spacing(rows, first, last, params)
    steps = 0
    while first < last and steps < ANCHOR_MAX_STEPS:
        following = next(k for k in range(first + 1, last + 1) if is_credit(rows[k], params))
        gap = rows[following][0] - rows[first][0]
        # A frame further ahead of the next credit frame than the 24 s join reaches wasn't glued on by that join: only
        # the dark bridge joins across more, so every keyframe between them is dark. It is kept as the start, lit or
        # dark itself -- a roll's first card is usually followed by black or by dark cards too small to read (WILL:
        # one card, then 65 s of such keyframes; stepping off it put the start 72 s late). On the harness's sets this
        # keeps the first frame on 9 files, 3 of them lit (credits over footage, an end-title card), and each of the 9
        # starts on the roll or inside it by frame check. The cost: a lit scene-text frame followed by more than 24 s of
        # dark keyframes before the roll is kept as the start too, that far early (spec §13 item 14).
        if gap <= ANCHOR_SPACING_FACTOR * spacing or gap > params.gap_s:
            break
        first = following
        steps += 1
    return first


def band_of(rows: Sequence[Row], first: int, last: int, params: RuleParams = RULE_J) -> float | None:
    """Where one run's text sits across the frame: the median horizontal middle of its credit frames' boxes.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        first: The run's first row index.
        last: The run's last row index.
        params: Rule thresholds, deciding which of the run's rows are credit frames.

    Returns:
        The middle in pixels of the frame's own 320 across, or None when the run's credit frames hold no box (every
        one of them is a dark frame the detector found no text on -- ``min_boxes`` counts boxes, and a run can be
        joined out of frames that have none).
    """
    centres = [
        (box[0] + box[2] + 1) / 2 for row in rows[first : last + 1] if is_credit(row, params) for box in boxes_of(row)
    ]
    return median(centres) if centres else None


def in_band(row: Row, band: float) -> bool:
    """Whether a frame's text sits in a roll's band (:func:`band_of`), so it can be that roll's own.

    ``BAND_TOLERANCE_PX`` is read in the body rather than bound as a default, so that a sweep which monkeypatches the
    constant reaches this test as well as :func:`same_roll`'s.

    Args:
        row: The frame.
        band: The roll's band.

    Returns:
        Whether the middle of this frame's boxes is within ``BAND_TOLERANCE_PX`` of ``band``. A frame with no box
        never is.
    """
    boxes = boxes_of(row)
    if not boxes:
        return False
    return abs(median((box[0] + box[2] + 1) / 2 for box in boxes) - band) <= BAND_TOLERANCE_PX


def same_roll(rows: Sequence[Row], runs: Sequence[tuple[int, int]], params: RuleParams = RULE_J) -> int:
    """How far back the last run's own roll reaches: the index in ``runs`` of its earliest run (spec §13 item 14).

    Rule J keeps the **last** run, so a roll the 24 s join splits -- its opening block names over bright footage, a
    long gap in the middle -- is answered from a later block, 80.7 s late against Plex at the median on the 205-movie
    set. Two runs are one roll when both of these hold, and neither is a distance:

    * **The same band.** Both runs' text sits at the same place across the frame (:func:`band_of` within
      ``BAND_TOLERANCE_PX``). A roll keeps its layout from card to card; a scene's signs, captions and lower thirds
      don't share one with it except by chance.
    * **The text never stops.** At least ``ROLL_TEXT_SHARE`` of the keyframes between the two runs carry a box, and
      there is at least one such keyframe. A roll whose opening is names over footage reads 1-2 boxes on almost every
      keyframe -- under the 3 a lit frame needs to be a credit frame, which is why the join broke -- while story
      between two blocks of text is mostly blank.

    ``runs`` is found on the rows as they were decoded; ``rows`` here is what the rule reads, which after
    :func:`without_overlays` is those rows without the boxes that never move. Both tests read it: a channel bug sits
    at one place on every frame it is on, so read as decoded it carries its own band across every run of a broadcast
    tail and makes "the text never stops" true of any two of them. An earlier run the overlay leaves fewer than two
    credit frames of is no more a roll than the last one would be (:func:`_credit_bounds`), so the merge stops there.

    The cost of reading the suppressed rows is the one place version 3's two halves work against each other: a roll
    that fills most of its tail and that the 24 s join split has its own opening block and names over footage in front
    of the last run, which is where :func:`overlay_boxes` looks for its story, so the roll's text can be gathered as
    an overlay and the keyframes between the blocks then read as blank -- and this merge is refused. See
    :func:`overlay_boxes`; no file of the sets, the lab or the 51 broadcast recordings has that shape, and every way
    of reading round it that was measured cost a broadcast answer
    (``evidence/eval/phase3-harness.md``, "Tried and not taken (this round)").

    Args:
        rows: Keyframe rows of the tail, in decode order, with the overlays' boxes already dropped.
        runs: :func:`credit_runs`' runs of the rows as decoded, at least one.
        params: Rule thresholds.

    Returns:
        The index in ``runs`` to take the start from: ``len(runs) - 1`` (the last run alone) unless an earlier run
        joined it.
    """
    band = band_of(rows, runs[-1][0], runs[-1][1], params)
    if band is None:
        return len(runs) - 1
    keep = len(runs) - 1
    while keep > 0:
        bounds = _credit_bounds(rows, *runs[keep - 1], params)
        if bounds is None:
            break
        first, last = bounds
        earlier = band_of(rows, first, last, params)
        if earlier is None or abs(earlier - band) > BAND_TOLERANCE_PX:
            break
        ends = max(rows[i][0] for i in range(first, last + 1) if is_credit(rows[i], params))
        starts = min(rows[i][0] for i in range(runs[keep][0], runs[keep][1] + 1) if is_credit(rows[i], params))
        # No keyframe between the two runs is no evidence that the text carried across, so it is no merge -- and a
        # pair whose times run the wrong way round (decode order can leave a later run at a lower index) has none by
        # construction, which is how that is refused too.
        between = [row for row in rows if ends < row[0] < starts]
        if not between or sum(1 for row in between if row[1] >= 1) < ROLL_TEXT_SHARE * len(between):
            break
        keep -= 1
    return keep


def reach_back(
    rows: Sequence[Row],
    index: int,
    first: int,
    last: int,
    params: RuleParams = RULE_J,
    *,
    run: range | None = None,
    raw: Sequence[Row] | None = None,
) -> int:
    """Step the start back over earlier keyframes that are the roll's own (spec §13 item 14).

    The roll's opening names over footage read 1-2 boxes on a lit frame, so they are not credit frames and the run
    starts after them. A keyframe before the start is taken for more of the same roll when its text sits in the run's
    band (:func:`in_band`) and it is no further from the frame the walk is on than the run's own cadence reaches:
    ``ANCHOR_SPACING_FACTOR`` times the spacing of the run's credit frames, and never more than the 24 s join. The
    cadence is what keeps the walk out of story: a roll puts text on its keyframes at a steady rate, while a scene's
    signage is sporadic, and the first gap wider than the roll's own stops the walk.

    ``first`` and ``last`` are the run the walk **starts from**, which after :func:`same_roll` merged an earlier run
    into the roll is that earlier run, not the last one. Measuring against the last run instead would hand a block of
    16 s cards' cadence to a walk starting in a block of 2 s cards and let it run 400 s into story.

    The band is the only thing the walk steps onto -- a credit frame out of the band is not the roll's. Taking any
    credit frame as well, whatever its band, lets the walk cross in-band text onto an earlier run :func:`same_roll`
    has just refused on that band, which makes the merge's own test moot
    (``test_rule_j.TestReachBack.test_it_stops_at_a_credit_frame_outside_the_band``). It moves no answer on either
    set, the lab or the 51 broadcast recordings, on either decode path, and it can only ever take candidates away --
    starts move later or stay -- so the narrower rule ships.

    The dark bridge is deliberately not honoured here: :func:`credit_runs` has already joined everything it reaches
    across dark frames, so a stretch the run stopped at holds a lit frame. Letting the walk cross it as well takes the
    start into dark scenes: two more of the 205 land over 10 s before their chapter and both publish at High
    (17 wrong against 15; ``evidence/eval/phase3-harness.md``, "Tried and not taken (this round)").

    Nor does the walk step onto a row of ``run`` itself. The anchor has already ruled on those: where it stepped over a
    glued-on frame, stepping back onto it would undo that ruling, and on Marvel's Daredevil S03 -- where decode order
    put the anchor's next credit row two frames along -- the walk undid it twice and took the start 10 s earlier, into
    a published answer more than 10 s before the chapter.

    The rows are what the rule reads -- the decoded rows without the overlays' boxes. Read raw, a keyframe whose only
    box is a channel bug is in the roll's band whenever the bug is, and the walk crosses the whole story on it: that
    is what cost two right answers on the broadcast recordings when the two halves of version 3 were measured apart
    (``evidence/eval/broadcast-tv.md``).

    **The cadence is capped by the run as it was decoded** (``raw``). Dropping an overlay's boxes takes credit frames
    out of the run, which *grows* the spacing between the ones that are left, which would grow this limit -- and
    unlike the anchor, which takes at most ``ANCHOR_MAX_STEPS`` steps, this walk has no step limit, so a wider limit
    is a longer walk, not one longer step. Measured on a synthetic tail whose band is identical raw and thinned and
    whose cadence alone moves, an uncapped walk answered 297.5 s before the band steps alone put it
    (``test_rule_j.TestReachBack.test_the_walk_never_outreaches_the_runs_cadence_as_decoded``). Taking the smaller of
    the two spacings means the overlay step can never lend the walk a step the run did not have before its boxes were
    dropped. It costs nothing measured (2026-09-21, ``evidence/eval/phase3-harness.md``).

    **The cap bounds the step; nothing bounds the walk.** The other half of what ``rows`` is reaches the walk through
    :func:`in_band`, which reads the median middle of a frame's *remaining* boxes: a story keyframe carrying a corner
    bug and one box in the roll's band reads a middle between the two and is out of the band as decoded, and reads
    the band box alone once the bug is dropped. The walk then crosses it at the roll's own cadence like any other,
    however many such frames there are. A synthetic tail of them answers the first row of the file, the whole tail
    earlier than the band steps alone
    (``test_rule_j.TestReachBack.test_dropping_a_bug_can_put_a_story_keyframe_in_the_band``). That flip happens on
    real rows -- 36 keyframes over the 17 of the 51 broadcast recordings that have an overlay -- and no answer of the
    sets, the lab or the 51 moves because of it. That is measurement, not a guarantee.

    Args:
        rows: Keyframe rows of the tail, in decode order, with the overlays' boxes already dropped.
        index: The anchored start to walk back from.
        first: The first credit row of the run ``index`` came from -- its band and cadence are the yardstick.
        last: That run's last credit row.
        params: Rule thresholds.
        run: The row indices of that run, which the walk steps over rather than onto; None to walk over every earlier
            row.
        raw: The same rows as they were decoded, indexed alike, which caps the cadence. :func:`coarse_start` always
            passes them, so the rule never runs uncapped; None is for callers holding the decoded rows already, and
            leaves the cadence as ``rows`` measures it.

    Returns:
        The row the start walks back to, or ``index`` when nothing before it is the roll's.
    """
    band = band_of(rows, first, last, params)
    if band is None:
        return index
    spacing = _run_spacing(rows, first, last, params)
    if raw is not None:
        # ``first`` and ``last`` are credit frames of ``rows``, and dropping boxes only ever takes text away, so they
        # are credit frames of ``raw`` too: ``_run_spacing`` always has its two frames here.
        spacing = min(spacing, _run_spacing(raw, first, last, params))
    limit = min(params.gap_s, ANCHOR_SPACING_FACTOR * spacing)
    inside = frozenset(run or ())
    # Presentation order, not ffmpeg's: the walk asks what comes *before* this frame, and the measured keyframe rows
    # aren't always increasing. Read in decode order a swapped pair puts a later frame before the start, and the walk
    # steps onto it -- the start then sits inside the roll, which is the mistake ``fade_back`` and ``coarse_end_s``
    # each had to be fixed for.
    order = sorted(range(len(rows)), key=lambda i: rows[i][0])
    at = order.index(index)
    while at > 0:
        before = at - 1
        while before >= 0 and (order[before] in inside or not in_band(rows[order[before]], band)):
            before -= 1
        if before < 0 or rows[order[at]][0] - rows[order[before]][0] > limit:
            break
        at = before
    return order[at]


def coarse_start(
    rows: Sequence[Row], params: RuleParams = RULE_J, *, without: Sequence[Row] | None = None
) -> Coarse | None:
    """The last credit run's start, reached back over the rest of its own roll, or None when the rows hold no run.

    ``without`` is the same rows with the overlays' boxes dropped (:func:`without_overlays`). The runs are still found
    on ``rows``, so which run is the last one, and how long it has to be, are exactly what they were before overlays
    existed. Everything that reads a frame's own text then reads ``without``: which of a run's frames are credit
    frames, the spacing the anchor measures, :func:`same_roll`'s bands and the share of texted keyframes between two
    runs, and :func:`reach_back`'s band and cadence. A run whose every credit frame was the overlay's -- a channel bug
    over a night scene -- is not a roll and gives None; a roll the bug merely sat on top of keeps its answer, and one
    the bug started early keeps only its own first card. Passing nothing reads the rows as they are, which is what the
    rule's own tests do.

    The two steps are in that order for a reason. Read raw, :func:`reach_back` walks back over story keyframes whose
    only box is the bug -- on broadcast recordings that is most of them -- and the band's proof goes with it.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        params: Rule thresholds.
        without: The same rows, same order, with the overlays' boxes dropped.

    Returns:
        The coarse start, or None.

    Raises:
        ValueError: ``without`` isn't the same rows -- the two are indexed together, and a shorter or reordered list
            would read another frame's boxes.
    """
    if without is not None and [row[0] for row in without] != [row[0] for row in rows]:
        raise ValueError(f"`without` must be the same rows in the same order: {len(without)} against {len(rows)}")
    runs = credit_runs(rows, params)
    if not runs:
        return None
    read = rows if without is None else without
    chosen = _credit_bounds(read, *runs[-1], params)
    if chosen is None:
        return None
    anchored = _anchored(read, *chosen, params)
    keep = same_roll(read, runs, params)
    walk_run, walk_from, start = runs[-1], chosen, anchored
    if keep != len(runs) - 1:
        # :func:`same_roll` only merges a run whose band it could read, so these bounds are never None; falling back
        # to the last run rather than raising keeps a mismatched ``params`` from turning into an exception here.
        bounds = _credit_bounds(read, *runs[keep], params)
        if bounds is not None:
            walk_run, walk_from = runs[keep], bounds
            start = _anchored(read, *walk_from, params)
    # The rows the walk may not step *onto* are the whole run it starts from as it was decoded, overlay rows included:
    # the anchor has already ruled on them, and a row the overlay emptied is no more the walk's to take. ``rows``
    # goes in as well, and only to cap the walk's cadence: without it, thinning the run could lend the walk reach the
    # run never had (:func:`reach_back`). With no overlay ``read`` is ``rows``, so the cap is a no-op.
    start = reach_back(read, start, *walk_from, params, run=range(walk_run[0], walk_run[1] + 1), raw=rows)
    return Coarse(
        index=start,
        end_index=chosen[1],
        pts_s=read[start][0],
        run_index=None if start == anchored else anchored,
    )


def text_all_through(rows: Sequence[Row], coarse: Coarse) -> bool:
    """Whether the text a run was found in is on screen all through the tail rather than a roll after story.

    Rule J counts boxes; it can't tell a timecode, a channel logo, a ticker or subtitles from a card, and text that
    never leaves the screen makes credit runs wherever a frame reads enough boxes (the lab's Synth Audio episodes, a
    test pattern with a running timecode: answers 6 to 245 s into five-minute files, one published). So a run is only a
    roll when the tail before its start holds at least ``STORY_BEFORE_RUN_S`` of footage and fewer than
    ``TEXT_ALL_THROUGH_SHARE`` of those keyframes carry any text box at all, lit or dark.

    It can't tell that text from a logo or ticker on screen over real story either: such a file loses its answer when
    text detection boxes the logo on that share of the keyframes (no file of the harness's sets comes near; one of 51
    broadcast recordings did, ``evidence/eval/phase3-harness.md``). And it
    doesn't catch text that comes and goes: subtitles on a dark scene after text-free story are credit frames to rule
    J, and a run of them joined to the roll still starts early (``test_rule_j.TestTextAllThrough``).

    Args:
        rows: Keyframe rows of the tail, in decode order.
        coarse: The coarse start (the run's anchored start, reached back over the rest of the roll).

    Returns:
        True when there should be no answer.
    """
    if coarse.pts_s - min(row[0] for row in rows) < STORY_BEFORE_RUN_S:
        return True
    before = [row for row in rows if row[0] < coarse.pts_s]
    return sum(1 for row in before if row[1] >= 1) >= TEXT_ALL_THROUGH_SHARE * len(before)


def opens_on_the_run(rows: Sequence[Row], coarse: Coarse, params: RuleParams = RULE_J) -> bool:
    """Whether a run too close to the first row for :func:`text_all_through` may have begun before the rows do.

    True when the run starts less than ``STORY_BEFORE_RUN_S`` after the first row and every row before its first credit
    frame is dark (luma under ``params.dark``, as the dark bridge counts it: a roll's own dark ground reads 18 on the
    lab's Heeramandi episodes). The detector then reads ``READ_BEFORE_TAIL_S`` more, and keeps it only when the run
    continues into it (:func:`joined_before`). A run after a lit frame is not cut off by the tail -- story came first --
    so nothing more is read and it stays without an answer (a story caption 28 s into a tail, followed by 400 s of
    story, is one such run).

    Args:
        rows: Keyframe rows of the tail, in decode order.
        coarse: The coarse start (the run's anchored start, reached back over the rest of the roll).
        params: Rule thresholds.

    Returns:
        Whether the rows before the tail are worth reading.

    Raises:
        ValueError: ``rows`` holds no run, so there is none to ask about. This step finds the runs again, so it is
            handed the rows as they were decoded, exactly as :func:`coarse_start` was -- the rows with an overlay's
            boxes dropped can hold no run at all while ``coarse`` is still an answer (a bug over a night scene with
            two cards in it).
    """
    if coarse.pts_s - min(row[0] for row in rows) >= STORY_BEFORE_RUN_S:
        return False
    runs = credit_runs(rows, params)
    if not runs:
        raise ValueError("these rows hold no run: ask the rows the coarse start was found on")
    return all(row[2] < params.dark for row in rows[: runs[-1][0]])


def joined_before(
    before: Sequence[Row], rows: Sequence[Row], params: RuleParams = RULE_J, *, overlays: Sequence[Box] = ()
) -> list[Row] | None:
    """The rows read before the tail put ahead of the tail's own, when the tail's run continues into them.

    Rows of ``before`` at or after the tail's first row are the tail's own and are dropped. The join is kept only when
    its coarse start (:func:`coarse_start`) lies before the tail's first row: the roll really began before the tail.
    Version 3's reach back can carry that start across the tail's edge itself, onto in-band text in the window read
    before it, which is another way the same test is met.
    A run that stays inside the tail (dark story before a caption run, then lit story in the rows before) is judged on
    the tail alone, as before, and has no answer. So is a roll whose one card before the tail the anchor steps over
    (more than 1.5 x the roll's spacing from the next card, and within 24 s of it): no answer, where the same rows read
    as one longer tail would answer on the next card. The run's first row instead of its anchored start would keep that
    roll, but also a lone dark subtitle frame before the tail, followed by dark rows and a caption run on a night scene
    in it, which is story. The run must still start 30 s after the first row read (:func:`text_all_through`), so a roll
    that began more than ``READ_BEFORE_TAIL_S`` − ``STORY_BEFORE_RUN_S`` (90 s) before the tail has no answer either.

    Args:
        before: Keyframe rows of the window before the tail, in decode order.
        rows: Keyframe rows of the tail, in decode order.
        params: Rule thresholds.
        overlays: The tail's own overlays (:func:`overlay_boxes`), which the joined rows are read without. They are
            not gathered again from the joined rows: a roll that began before the tail is exactly the shape that must
            not be read as its own overlay. Empty reads the joined rows as they are.

    Returns:
        The joined rows as decoded, or None when the tail is to be judged alone.
    """
    tail_first_s = min(row[0] for row in rows)
    joined = [*(row for row in before if row[0] < tail_first_s), *rows]
    coarse = coarse_start(joined, params, without=without_overlays(joined, overlays) if overlays else None)
    return joined if coarse is not None and coarse.pts_s < tail_first_s else None


def fade_back(rows: Sequence[Row], index: int, floor_s: float) -> float:
    """Step back from ``rows[index]`` over the fade to black: frames darker than 12, at most 4 s apart, not before
    ``floor_s``.

    Only ever back in time: on the keyframe rows (the fallback when no 1 fps row lies in the refine window) a swapped
    pair can put a later row before the start in ffmpeg's output order, and stepping onto it would start the skip
    inside the roll. The 1 fps rows are always in order, so this changes nothing there.

    Returns:
        The time of the earliest frame reached.
    """
    i = index
    while (
        i > 0
        and floor_s <= rows[i - 1][0] <= rows[i][0]
        and rows[i - 1][2] < FADE_LUMA
        and rows[i][0] - rows[i - 1][0] <= FADE_STEP_S
    ):
        i -= 1
    return rows[i][0]


def refine_start(
    rows: Sequence[Row],
    coarse: Coarse,
    fine_rows: Sequence[Row],
    *,
    before_s: float = REFINE_BEFORE_S,
    params: RuleParams = RULE_J,
) -> float:
    """Refine the coarse start with the 1 fps rows decoded just before it.

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start.
        fine_rows: 1 fps rows (any window; only ``[coarse − before_s, coarse + 1 s]`` is read).
        before_s: How far before the coarse start the refinement may reach.
        params: Rule thresholds.

    Returns:
        The refined start in seconds.
    """
    t = coarse.pts_s
    floor_s = t - before_s
    window = [j for j, row in enumerate(fine_rows) if floor_s <= row[0] <= t + REFINE_AFTER_S]
    if not window:
        return fade_back(rows, coarse.index, -1.0)
    credit = [j for j in window if is_credit(fine_rows[j], params)]
    if not credit:
        return t
    j = credit[-1]
    while True:
        earlier = [k for k in credit if k < j and fine_rows[j][0] - fine_rows[k][0] <= REFINE_GAP_S]
        if not earlier:
            break
        j = earlier[0]
    return fade_back(fine_rows, j, floor_s)


def credits_start(
    rows: Sequence[Row],
    fine_rows: Sequence[Row],
    *,
    before_s: float = REFINE_BEFORE_S,
    params: RuleParams = RULE_J,
) -> float | None:
    """Coarse start then refinement on rows already decoded (the 80-file fixture's builder and its tests; the detector,
    and the harness through it, decode the fine rows only after they know the coarse start).

    Returns:
        The credits start in seconds, or None when the keyframe rows hold no credit run, when the overlay leaves the
        chosen run fewer than two credit frames (:func:`coarse_start`), or when its text is on screen all through the
        tail (:func:`text_all_through`).
    """
    overlays = overlay_boxes(rows, params)
    rule_rows = without_overlays(rows, overlays)
    coarse = coarse_start(rows, params, without=rule_rows)
    # The guard counts the rows as they were decoded, exactly as the detector does: see :func:`overlay_boxes`.
    if coarse is None or text_all_through(rows, coarse):
        return None
    return refine_start(rule_rows, coarse, without_overlays(fine_rows, overlays), before_s=before_s, params=params)


def _run_rows(rows: Sequence[Row], coarse: Coarse) -> Sequence[Row]:
    """The chosen run's own rows, from its anchored start to where it stops.

    Not from ``coarse.index``: version 3's :func:`same_roll` and :func:`reach_back` may have moved the start back
    before the run, and the end is decided from the run alone, exactly as version 2 decided it.
    """
    return rows[(coarse.index if coarse.run_index is None else coarse.run_index) : coarse.end_index + 1]


def coarse_end_s(rows: Sequence[Row], coarse: Coarse, params: RuleParams = RULE_J) -> float:
    """The chosen run's last credit keyframe, in presentation order.

    The rows are in ffmpeg's output order, which isn't always increasing, so the run's last *row* is not always its
    latest: on four of the 80 files it sits 10-21 s before the run's latest credit keyframe. Reading the row instead
    is the same decode-order-for-presentation-order mistake :func:`_run_spacing` fixes for the anchor, one layer over,
    and it moved movie-11 across Q3's 30 s line -- the roll really ends 20 s before the file does, so the skip runs to
    the end, yet the end window was decoded and walked anyway.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        coarse: The coarse start (:func:`_run_rows` bounds the run; ``index`` may be earlier).
        params: Rule thresholds, deciding which of the run's rows are credit frames.

    Returns:
        The time of the run's latest credit frame. Both ends of the slice are credit frames under the ``params``
        ``coarse`` was found with, so there is always one -- unless a mismatched ``params`` is passed here, in which
        case this degrades to the run's last row rather than raising on an empty ``max()``.
    """
    return max(
        (row[0] for row in _run_rows(rows, coarse) if is_credit(row, params)),
        default=rows[coarse.end_index][0],
    )


def end_keyframe_s(rows: Sequence[Row], coarse: Coarse, params: RuleParams = RULE_J) -> float:
    """The keyframe the end's refinement starts from: the run's latest credit keyframe, or the one before it when that
    keyframe is scene text glued onto the roll.

    The start's anchor, mirrored (spec §13 item 13). The 24 s join lets a lit text frame in the scene after the roll
    join the run, and the end then lands in that scene: Rick and Morty S01E04's roll ends on a card at 1183.0 s, and
    swscale's frame of the scene at 1198.4 s reads 3 boxes, so the CPU decode's skip ran 11.5 s into the scene. The last
    credit keyframe is taken for glued on -- and the end steps back one credit keyframe, never more -- only when it's
    all of: a lit frame (a dark card is the roll's own), further from the credit keyframe before it than 1.5 x the
    spacing of the run's other credit keyframes, and separated from it by a lit keyframe with no text at all (the scene
    has started). The spacing leaves the last keyframe out: its own gap would be the median of a run of three or four
    credit keyframes, which is never more than 1.5 x itself.

    It moves an end, never makes one: :func:`credits_end` decides whether there is an end from the latest credit
    keyframe exactly as before, so a lit closing card with text (a logo) never turns into a stop.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        coarse: The coarse start (:func:`_run_rows` bounds the run; ``index`` may be earlier).
        params: Rule thresholds.

    Returns:
        The keyframe's time.
    """
    return _end_keyframes(rows, coarse, params)[0]


def _end_keyframes(rows: Sequence[Row], coarse: Coarse, params: RuleParams) -> tuple[float, float | None]:
    """:func:`end_keyframe_s`, and when it stepped back over glued-on scene text, the scene's first keyframe (the
    first lit one with no text after the roll's last card; else None)."""
    run = _run_rows(rows, coarse)
    credit = sorted((row for row in run if is_credit(row, params)), key=lambda row: row[0])
    if len(credit) < 3:
        return coarse_end_s(rows, coarse, params), None
    before, last = credit[-2], credit[-1]
    scene = [row[0] for row in run if before[0] < row[0] < last[0] and row[1] == 0 and row[2] >= params.dark]
    glued = (
        last[2] >= params.dark
        and bool(scene)
        and last[0] - before[0] > ANCHOR_SPACING_FACTOR * _typical_spacing(credit[:-1])
    )
    return (before[0], min(scene)) if glued else (last[0], None)


def keeps_a_scene_after(end_s: float, duration_s: float) -> bool:
    """Whether more than 30 s of the file follows an end (Q3), so the skip stops there."""
    return duration_s - end_s > KEEP_AFTER_CREDITS_S


def refine_end(
    rows: Sequence[Row],
    coarse: Coarse,
    fine_rows: Sequence[Row],
    *,
    after_s: float = REFINE_END_AFTER_S,
    params: RuleParams = RULE_J,
) -> float:
    """Refine the end keyframe (:func:`end_keyframe_s`) with 1 fps rows, the start's walk in the other direction.

    From the first credit frame in ``[end − 1 s, end + after_s]``, walk forward over credit frames at most 2.5 s apart;
    the end is the last one reached (no fade step: the skip stops on the roll's last frame, never inside the scene).
    ``end`` is :func:`end_keyframe_s`; when that stepped back over scene text glued onto the roll, the window stops at
    the scene's first keyframe, so the walk can't reach the scene's own text however long it stays on screen.

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start (``end_index`` is where the run stops, not when -- see :func:`coarse_end_s`).
        fine_rows: 1 fps rows (any window; only ``[end − 1 s, end + after_s]`` is read).
        after_s: How far past the end keyframe the refinement may reach (never past the scene's first keyframe).
        params: Rule thresholds.

    Returns:
        The refined end in seconds (the keyframe's time when the window holds no credit frame).
    """
    t, scene_s = _end_keyframes(rows, coarse, params)
    return _walk_forward(fine_rows, t, t + after_s if scene_s is None else min(t + after_s, scene_s), params)


def _walk_forward(fine_rows: Sequence[Row], t: float, reach_s: float, params: RuleParams) -> float:
    """From the first credit frame in ``[t − 1 s, reach_s]``, the last credit frame reached over gaps of 2.5 s at most
    (``t`` when the window holds none)."""
    window = [j for j, row in enumerate(fine_rows) if t - REFINE_END_BEFORE_S <= row[0] <= reach_s]
    credit = [j for j in window if is_credit(fine_rows[j], params)]
    if not credit:
        return t
    j = credit[0]
    while True:
        later = [k for k in credit if k > j and fine_rows[k][0] - fine_rows[j][0] <= REFINE_GAP_S]
        if not later:
            break
        j = later[-1]
    return fine_rows[j][0]


def credits_end(
    rows: Sequence[Row],
    coarse: Coarse,
    fine_rows: Sequence[Row],
    duration_s: float,
    *,
    params: RuleParams = RULE_J,
) -> float | None:
    """Where the credits skip ends (Q3): the refined last credit frame when more than 30 s of the file follows it.

    Whether there is an end is decided exactly as rule J version 1 did, from the run's latest credit keyframe: more than
    30 s must follow it, and more than 30 s must follow the last credit frame the 1 fps walk reaches from it. Only then
    does :func:`refine_end` say where the end is, which may step back over scene text glued onto the roll. The step
    back can only move an end earlier, so the end it gives always has more than 30 s after it too.

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start.
        fine_rows: 1 fps rows over ``[end_keyframe_s − 1 s, coarse_end_s + 20 s]`` (read only when an end can be kept;
            the two keyframes are the same one unless the end steps back).
        duration_s: The file's duration.
        params: Rule thresholds.

    Returns:
        The end in seconds, or None: the skip runs to the end of the file.
    """
    latest = coarse_end_s(rows, coarse, params)
    if not keeps_a_scene_after(latest, duration_s):
        return None
    if not keeps_a_scene_after(_walk_forward(fine_rows, latest, latest + REFINE_END_AFTER_S, params), duration_s):
        return None
    return refine_end(rows, coarse, fine_rows, params=params)
