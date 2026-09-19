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
Version 2 (spec §13 items 13 and 14, ``evidence/eval/phase3-harness.md``) adds two things the prototype never had:
the anchor never steps over a gap longer than the 24 s join (:func:`coarse_start`), and the end steps back over scene
text the join glued on after the roll (:func:`end_keyframe_s`). The 80 files move to 64 / 1 / 7 / 4.
``coarse_end_s`` reads the run's latest credit frame in presentation order for the same reason, so Q3's end means the
end of the roll rather than whichever of its keyframes ffmpeg emitted last, and ``fade_back`` never steps onto a later
row. Everywhere else rows stay in ffmpeg's output order, the anchor's distance included: the measured keyframe rows
aren't always increasing, and sorting them changes an answer (4 of the 80 coarse starts, as measured).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Row = tuple[float, int, float]

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
    """The last credit run's start after the anchor step.

    Attributes:
        index: The start's row in the keyframe rows.
        end_index: The run's last credit row in ffmpeg's output order, which is where the run stops, not when: the
            run's latest time is :func:`coarse_end_s`.
        pts_s: The start's time.
    """

    index: int
    end_index: int
    pts_s: float


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


def coarse_start(rows: Sequence[Row], params: RuleParams = RULE_J) -> Coarse | None:
    """The last credit run's anchored start, or None when the rows hold no run.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        params: Rule thresholds.

    Returns:
        The coarse start, or None.
    """
    runs = credit_runs(rows, params)
    if not runs:
        return None
    first, last = runs[-1]
    # Start only where two credit samples sit next to each other: the 24 s join lets one story-scene text frame glue
    # itself onto the roll (Undisputed: a lone scene-text frame 24 s before it). Only ever the one frame, though --
    # see ANCHOR_MAX_STEPS. The slice below is [first:last+1], not last+2: the run's own credit frames are what the
    # yardstick measures, so a row outside the run is out of scope whatever it is -- including the rare case where
    # it's itself a credit frame (a trailing run under run_s gets dropped by credit_runs, so runs[-1] can leave one
    # sitting right past last when decode order is non-monotonic).
    spacing = _run_spacing(rows, first, last, params)
    steps = 0
    while first < last and steps < ANCHOR_MAX_STEPS:
        following = next(k for k in range(first + 1, last + 1) if is_credit(rows[k], params))
        gap = rows[following][0] - rows[first][0]
        # A frame further ahead than the 24 s join reaches wasn't glued on by it: only the dark bridge joins across
        # that, so the frames between are all dark and the frame is the roll's own first card on black (WILL: one
        # card, then 65 s of dark keyframes whose small text reads as no boxes before the next detected card; the step
        # put the start there, 72 s late).
        if gap <= ANCHOR_SPACING_FACTOR * spacing or gap > params.gap_s:
            break
        first = following
        steps += 1
    return Coarse(index=first, end_index=last, pts_s=rows[first][0])


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
        The credits start in seconds, or None when the keyframe rows hold no credit run.
    """
    coarse = coarse_start(rows, params)
    return None if coarse is None else refine_start(rows, coarse, fine_rows, before_s=before_s, params=params)


def coarse_end_s(rows: Sequence[Row], coarse: Coarse, params: RuleParams = RULE_J) -> float:
    """The chosen run's last credit keyframe, in presentation order.

    The rows are in ffmpeg's output order, which isn't always increasing, so the run's last *row* is not always its
    latest: on four of the 80 files it sits 10-21 s before the run's latest credit keyframe. Reading the row instead
    is the same decode-order-for-presentation-order mistake :func:`_run_spacing` fixes for the anchor, one layer over,
    and it moved movie-11 across Q3's 30 s line -- the roll really ends 20 s before the file does, so the skip runs to
    the end, yet the end window was decoded and walked anyway.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        coarse: The coarse start (``index`` and ``end_index`` bound the run).
        params: Rule thresholds, deciding which of the run's rows are credit frames.

    Returns:
        The time of the run's latest credit frame. Both ends of the slice are credit frames under the ``params``
        ``coarse`` was found with, so there is always one -- unless a mismatched ``params`` is passed here, in which
        case this degrades to the run's last row rather than raising on an empty ``max()``.
    """
    return max(
        (row[0] for row in rows[coarse.index : coarse.end_index + 1] if is_credit(row, params)),
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
    run's credit spacing, and separated from it by a lit keyframe with no text at all (the scene has started).

    It moves an end, never makes one: whether an end is kept at all is still Q3's test on :func:`coarse_end_s`, so a
    lit closing card within 30 s of the end of the file (a logo with a line of text) never turns into a stop.

    Args:
        rows: Keyframe rows of the tail, in decode order.
        coarse: The coarse start (``index`` and ``end_index`` bound the run).
        params: Rule thresholds.

    Returns:
        The keyframe's time.
    """
    return _end_keyframes(rows, coarse, params)[0]


def _end_keyframes(rows: Sequence[Row], coarse: Coarse, params: RuleParams) -> tuple[float, float | None]:
    """:func:`end_keyframe_s`, and when it stepped back over glued-on scene text, the scene's first keyframe (the
    first lit one with no text after the roll's last card; else None)."""
    run = rows[coarse.index : coarse.end_index + 1]
    credit = sorted((row for row in run if is_credit(row, params)), key=lambda row: row[0])
    if len(credit) < 3:
        return coarse_end_s(rows, coarse, params), None
    before, last = credit[-2], credit[-1]
    scene = [row[0] for row in run if before[0] < row[0] < last[0] and row[1] == 0 and row[2] >= params.dark]
    glued = (
        last[2] >= params.dark
        and bool(scene)
        and last[0] - before[0] > ANCHOR_SPACING_FACTOR * _typical_spacing(credit)
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
    reach = t + after_s if scene_s is None else min(t + after_s, scene_s)
    window = [j for j, row in enumerate(fine_rows) if t - REFINE_END_BEFORE_S <= row[0] <= reach]
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

    Whether an end is kept is judged on the run's latest credit keyframe (:func:`coarse_end_s`); the end itself is
    refined from :func:`end_keyframe_s`, which may step back over scene text glued onto the roll.

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start.
        fine_rows: 1 fps rows around :func:`end_keyframe_s` (read only when an end can be kept).
        duration_s: The file's duration.
        params: Rule thresholds.

    Returns:
        The end in seconds, or None: the skip runs to the end of the file.
    """
    if not keeps_a_scene_after(coarse_end_s(rows, coarse, params), duration_s):
        return None
    end = refine_end(rows, coarse, fine_rows, params=params)
    return end if keeps_a_scene_after(end, duration_s) else None
