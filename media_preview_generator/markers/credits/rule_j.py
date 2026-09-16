"""Rule "J": where the credit roll starts, and where it ends when a scene follows it, from the text boxes and brightness
of a file's ending (spec §5.4; the end is the owner's Q3 ruling, 2026-09-16).

A port of the measured prototype (``evidence/credits/eval_rules3.py`` ``detect`` with rule J's parameters, keyframe
mode, refined over the 20 s before the coarse answer). Same inputs, same order, same comparisons, so it reproduces the
prototype at the spec's 20 s refine span on the 80 files (59 / 1 / 8 / 4; §5.4's table was measured at 10 s, late 9;
``tests/fixtures/markers/credits_rule_j_80.json.gz``). Rows stay in ffmpeg's output order: the measured keyframe rows
aren't always increasing, and sorting them changes an answer.
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
        end_index: The run's last credit row.
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
    # itself onto the roll (Undisputed: a lone scene-text frame 24 s before it).
    spacing = _typical_spacing(rows)
    while first < last:
        following = next(k for k in range(first + 1, last + 1) if is_credit(rows[k], params))
        if rows[following][0] - rows[first][0] <= ANCHOR_SPACING_FACTOR * spacing:
            break
        first = following
    return Coarse(index=first, end_index=last, pts_s=rows[first][0])


def fade_back(rows: Sequence[Row], index: int, floor_s: float) -> float:
    """Step back from ``rows[index]`` over the fade to black: frames darker than 12, at most 4 s apart, not before
    ``floor_s``.

    Returns:
        The time of the earliest frame reached.
    """
    i = index
    while (
        i > 0
        and rows[i - 1][0] >= floor_s
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
    """Coarse start then refinement on rows already decoded (the harness and the fixture test; the detector decodes the
    fine rows only after it knows the coarse start).

    Returns:
        The credits start in seconds, or None when the keyframe rows hold no credit run.
    """
    coarse = coarse_start(rows, params)
    return None if coarse is None else refine_start(rows, coarse, fine_rows, before_s=before_s, params=params)


def coarse_end_s(rows: Sequence[Row], coarse: Coarse) -> float:
    """The chosen run's last credit keyframe."""
    return rows[coarse.end_index][0]


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
    """Refine the run's last credit keyframe with 1 fps rows, the start's walk in the other direction.

    From the first credit frame in ``[end − 1 s, end + after_s]``, walk forward over credit frames at most 2.5 s apart;
    the end is the last one reached (no fade step: the skip stops on the roll's last frame, never inside the scene).

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start (its ``end_index`` is the run's last credit keyframe).
        fine_rows: 1 fps rows (any window; only ``[end − 1 s, end + after_s]`` is read).
        after_s: How far past the last credit keyframe the refinement may reach.
        params: Rule thresholds.

    Returns:
        The refined end in seconds (the keyframe's time when the window holds no credit frame).
    """
    t = coarse_end_s(rows, coarse)
    window = [j for j, row in enumerate(fine_rows) if t - REFINE_END_BEFORE_S <= row[0] <= t + after_s]
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

    Args:
        rows: The keyframe rows the coarse start came from.
        coarse: The coarse start.
        fine_rows: 1 fps rows around the run's last credit keyframe (read only when an end can be kept).
        duration_s: The file's duration.
        params: Rule thresholds.

    Returns:
        The end in seconds, or None: the skip runs to the end of the file.
    """
    if not keeps_a_scene_after(coarse_end_s(rows, coarse), duration_s):
        return None
    end = refine_end(rows, coarse, fine_rows, params=params)
    return end if keeps_a_scene_after(end, duration_s) else None
