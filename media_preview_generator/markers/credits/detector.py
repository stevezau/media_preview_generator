"""The credit text detector (spec §5.4): keyframes of the tail → rule J → one frame a second just before its start (and
around its end when a scene follows the roll, Q3) → one credits candidate from ``credits_text``. The pipeline registers
it as a local detector that runs on a worker whenever it decodes (:func:`credits_text_needs_worker`).

The rows the result carries are the ones that were decoded. Rule J version 3 reads the chosen run without the text
that sits in one place right across the story (``rule_j.overlay_boxes``, spec §13 item 15), and the 1 fps refine and
end rows the same way -- which is also what keeps its band steps (``rule_j.same_roll``, ``rule_j.reach_back``, §13
item 14) from walking a start back over story keyframes whose only box is a channel bug.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from ..models import Candidate, FileIdentity, MarkerType, Source
from . import frames, rule_j
from .textdet_helper import TextDetUnavailableError, get_textdet_pool

if TYPE_CHECKING:
    from ..pipeline import DetectorAnswer, LocalDetectorSpec, PipelineContext
    from ..store import FileRecord

# Stored with every answer. Bump it when rule J, the tail lengths, the frame format or the model change: stored answers
# of another version are asked again, even for decided types (spec §14 2026-09-14 "Local detectors").
# 2: the anchor never steps over a gap the 24 s join can't bridge, the end steps back over scene text glued onto the
# roll, text on screen all through the tail gives no answer, and a roll the tail opens on is read from 120 s before the
# tail (spec §13 items 13 and 14, phase3-harness.md "Rule J version 2").
# 3: rule J reads where a frame's text is. Text that sits in one place right across the story -- a channel or score
# bug, a ticker, a burnt-in timecode -- doesn't count as text inside the credit run; and a roll the 24 s join split is
# put back together from the band its text keeps to, an earlier run in that band whose text never stops being the same
# roll and a keyframe before the start in it, at the run's own cadence, being more of it. Starts move both ways under
# the first and earlier only under the second, and no end moved on either set or decode path (spec §13 items 14
# and 15, phase3-harness.md and
# broadcast-tv.md "Rule J version 3"). Stored answers of version 2 are asked again because these starts differ.
# Reading on before the tail step by step while the roll still fills what was read (2026-09-23) is not a version: no
# answer that was found moves. A "nothing found" stored before it is asked again once instead, and only where the steps
# now read further than the one step did (:func:`credits_text_due`).
CREDITS_TEXT_VERSION = 3
# A stored answer's version is CREDITS_TEXT_VERSION for Automatic (what it has always been, so nothing is decoded again
# on upgrade) and CREDITS_TEXT_VERSION + window seconds * this for a window the user chose. The smallest window
# (300 s) gives 300,003, so a chosen window's version never equals Automatic's, and another window's answer is asked
# again.
_WINDOW_VERSION_STEP = 1000
# Stored with every answer as what it was based on (``detector_runs``). An answer without it was read when the look-back
# stopped after one step (:func:`credits_text_due`).
LOOK_BACK_BASIS = "steps back to the middle"
# Every step read before the tail shares the one decode's time limit the single step had, so a file's worst case stays
# what it was: a step that would start past it, or runs past it, ends the look-back with no answer.
LOOK_BACK_TIMEOUT_S = frames.DECODE_TIMEOUT_S
READING_PHASE = "Reading the credits…"
REFINING_PHASE = "Refining the credits start…"
REFINING_END_PHASE = "Finding where the credits end…"
# A file whose decode timed out isn't decoded again for this long unless it changes or the run is forced (I1).
TIMEOUT_RETRY = timedelta(days=1)
_GIVES_UP = "credits_text_gives_up"


@dataclass(frozen=True)
class CreditsTextResult:
    """What one file's ending gave.

    Attributes:
        start_s: The credits start, or None when the tail holds no credit run or its text is on screen all through the
            tail (``rule_j.text_all_through``).
        end_s: Where the skip ends (Q3), or None: it runs to the end of the file.
        key_rows: The tail's keyframe rows, each ``(pts, box count, luma, boxes)`` (the harness keeps them).
        fine_rows: The 1 fps rows before the coarse start (empty without an answer).
        end_rows: The 1 fps rows from 1 s before ``rule_j.end_keyframe_s`` to 20 s past the run's latest credit keyframe
            (empty unless more than 30 s follows that keyframe).
        overlays: The text that never moved (``rule_j.overlay_boxes``), gathered from the **tail's** rows. Anything
            reading ``key_rows`` back has to be handed these rather than gather them again: on the branch that reads
            the steps before the tail, ``key_rows`` is the joined rows, and a roll that began before the tail is
            exactly the shape that must not be read as its own overlay.
    """

    start_s: float | None
    end_s: float | None
    key_rows: tuple[rule_j.Row, ...]
    fine_rows: tuple[rule_j.Row, ...]
    end_rows: tuple[rule_j.Row, ...]
    overlays: tuple[rule_j.Box, ...] = ()


def find_credits(
    path: str,
    *,
    duration_ms: int,
    is_episode: bool,
    tail_s: float | None = None,
    ffmpeg: str,
    detect_boxes: Callable[[np.ndarray], list[tuple[rule_j.Box, ...]]],
    gpu: str | None,
    gpu_device_path: str | None,
    cancel_check: Callable[[], bool] | None = None,
    phase: Callable[[str], None] | None = None,
) -> CreditsTextResult:
    """Decode the tail, find the roll, refine its start and, when a scene follows it, its end.

    The app and the harness run exactly this. The container's start time is probed here, once, and handed to every
    decode: the pipeline's own probe is None on the re-run path, and a stale or defaulted start time would put a
    recording's answers tens of thousands of seconds out (Task 6).

    An intra-only stream (every frame a keyframe: ProRes, DNxHD, MJPEG, an all-I H.264) has its keyframe pass thinned
    before the decoder to one frame per ``frames.INTRA_ONLY_SPACING_S``, the keyframe spacing rule J was measured at;
    decoded and read for text in full it would run past the decode timeout and never answer (the tail is still read
    from disk in full). A VP9 stream's decoder ignores ``-skip_frame``, so its keyframe pass drops the packets not
    flagged as keyframes before the decoder instead (``frames.keyframe_thinning``). Nothing else is thinned: other
    files' keyframe passes and every file's 1 fps refine decodes are decoded in full.

    A roll can begin before the tail does. When the run starts under 30 s into the tail and nothing lit comes before it
    (``rule_j.opens_on_the_run``), the keyframes of the ``rule_j.READ_BEFORE_TAIL_S`` before the tail are read through
    the same keyframe pass, and rule J runs on both when the run continues into them (``rule_j.joined_before``). While
    the joined rows still open on the run, the step before them is read and joined the same way, until the roll's
    start has story before it. The first step reads what it always has; the later ones never read before the middle of
    the file (the earliest a credits start is ever kept, ``decide``), nor a step too short to hold the 30 s of story
    rule J wants before a run, and all of them share one decode's time limit (``LOOK_BACK_TIMEOUT_S``). A roll still
    filling the rows when either stops has no answer, as one that began too early always had. A step can give no
    keyframe at all; on the GPU no frames is a GPU failure, and the worker's CPU rerun then gives the same no answer.

    Args:
        path: The media file (read only).
        duration_ms: Its duration.
        is_episode: Picks the default tail when ``tail_s`` is None: the last 450 s instead of 900 s (T-R4).
        tail_s: The length of the tail to read, overriding both (the user's window in Settings); None = by kind.
        ffmpeg: ffmpeg binary.
        detect_boxes: Text boxes per chunk of luma planes.
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.
        cancel_check: True once the job is cancelled.
        phase: Shows the step on the worker row.

    Returns:
        The start, the end, and the rows they came from.

    Raises:
        frames.GpuDecodeError: The GPU decode failed or gave no frames.
        frames.DecodeTimeoutError: A decode, or the start time or video packet probe before them, ran past its time
            limit.
        frames.FrameDecodeError: ffprobe couldn't read the start time, either probe wasn't started (earlier ffprobes
            being stuck), or ffmpeg couldn't decode the frames.
        frames.DecodeCancelledError: The job was cancelled before or during a decode.
        TextDetUnavailableError: Text detection couldn't answer.
    """
    show = phase or (lambda _text: None)
    show(READING_PHASE)
    start_time_s = frames.container_start_s(path, ffmpeg, cancel_check=cancel_check)
    thinning = frames.keyframe_thinning(path, ffmpeg, cancel_check=cancel_check)
    decode = {"ffmpeg": ffmpeg, "gpu": gpu, "gpu_device_path": gpu_device_path, "detect_boxes": detect_boxes,
              "cancel_check": cancel_check, "start_time_s": start_time_s}  # fmt: skip

    def keyframes(start_s: float, length_s: float | None, timeout_s: float | None = None) -> list[rule_j.Row]:
        limit = {} if timeout_s is None else {"timeout_s": timeout_s}
        return frames.decode_rows(
            path, start_s=start_s, length_s=length_s, keyframes_only=True, fps=None, keep_every=thinning.keep_every,
            drop_non_key=thinning.drop_non_key, **decode, **limit,
        )  # fmt: skip

    tail_start = frames.tail_start_s(
        duration_ms, tail_s=frames.tail_length_s(is_episode=is_episode) if tail_s is None else tail_s
    )
    key_rows = keyframes(tail_start, None)
    # Text that never moves off one spot across the story is a channel or score bug, a ticker or a timecode, not
    # credits, so everything that reads a frame's own text reads the rows without it: which of the run's frames are
    # credit frames, the anchor's spacing, and the band steps' own bands and cadences (spec §13 items 14 and 15).
    # Which run is the last one is still read from the rows as they were decoded, and so is the share
    # text_all_through counts: that step is what catches a file whose overlay this one doesn't find, and counting the
    # overlay out would take its answer away.
    overlays = rule_j.overlay_boxes(key_rows)
    rule_rows = rule_j.without_overlays(key_rows, overlays)
    coarse = rule_j.coarse_start(key_rows, without=rule_rows)
    # Everything that reads the *runs* rather than one run's frames reads key_rows: opens_on_the_run and joined_before
    # both find the runs again, and they have to find the run coarse came from. The tail's overlays carry over to the
    # joined rows and are not gathered again from them: a roll that began before the tail is exactly the shape that
    # must not be read as its own overlay. A real bug excludes itself from the tail's own overlays on this branch --
    # opens_on_the_run only says yes when every row before the run *in decode order* is dark, and a dark row a bug is
    # boxed on is a credit frame, which dark frames then merge into the run. What is left is a shape nothing measured
    # has: four or more keyframes emitted after the whole chosen run yet timestamped before its earliest frame, since
    # overlay_boxes takes its story by time while that check reads decode order (the reordering measured on the 80 is
    # 10-21 s, and this branch's story is under 30 s all told).
    if coarse is not None and tail_start > 0 and rule_j.opens_on_the_run(key_rows, coarse):
        deadline = time.monotonic() + LOOK_BACK_TIMEOUT_S
        before_start = max(0.0, tail_start - rule_j.READ_BEFORE_TAIL_S)
        before_rows = keyframes(before_start, tail_start - before_start)
        while (joined := rule_j.joined_before(before_rows, key_rows, overlays=overlays)) is not None:
            key_rows = joined
            rule_rows = rule_j.without_overlays(key_rows, overlays)
            coarse = rule_j.coarse_start(key_rows, without=rule_rows)
            # Rows that no longer open on the run are judged as they always were. The other two stops leave rows that
            # still do, the run under 30 s after their first row, which text_all_through answers with nothing.
            if not rule_j.opens_on_the_run(key_rows, coarse) or not _can_step_back(before_start, duration_ms):
                break
            read_from = before_start
            before_start = max(duration_ms / 2000.0, read_from - rule_j.READ_BEFORE_TAIL_S)
            before_rows = _step_rows(keyframes, before_start, read_from, deadline)
            if before_rows is None:
                logger.info("Out of time reading back for the start of the credits of {}", os.path.basename(path))
                break
    if coarse is None or rule_j.text_all_through(key_rows, coarse):
        return CreditsTextResult(None, None, tuple(key_rows), (), (), overlays)
    show(REFINING_PHASE)
    # max() can't bind while text_all_through holds an answered run 30 s past its first row; it keeps -ss non-negative.
    fine_start = max(0.0, coarse.pts_s - rule_j.REFINE_BEFORE_S)
    fine_length = coarse.pts_s + rule_j.REFINE_AFTER_S - fine_start
    fine_rows = frames.decode_rows(
        path, start_s=fine_start, length_s=fine_length, keyframes_only=False, fps=1, **decode
    )
    start_s = rule_j.refine_start(rule_rows, coarse, rule_j.without_overlays(fine_rows, overlays))
    duration_s = duration_ms / 1000.0
    last_keyframe = rule_j.coarse_end_s(rule_rows, coarse)
    if not rule_j.keeps_a_scene_after(last_keyframe, duration_s):
        return CreditsTextResult(start_s, None, tuple(key_rows), tuple(fine_rows), (), overlays)
    show(REFINING_END_PHASE)
    # From where the end's walk starts to where the latest credit keyframe's walk may reach: credits_end decides
    # whether there is an end from the latest one, then where it is from end_keyframe_s (the same keyframe unless the
    # end steps back over scene text; then at most 24 s earlier, the join's reach).
    end_start = max(0.0, rule_j.end_keyframe_s(rule_rows, coarse) - rule_j.REFINE_END_BEFORE_S)  # as fine_start
    end_length = last_keyframe + rule_j.REFINE_END_AFTER_S - end_start
    end_rows = frames.decode_rows(path, start_s=end_start, length_s=end_length, keyframes_only=False, fps=1, **decode)
    end_s = rule_j.credits_end(rule_rows, coarse, rule_j.without_overlays(end_rows, overlays), duration_s)
    return CreditsTextResult(start_s, end_s, tuple(key_rows), tuple(fine_rows), tuple(end_rows), overlays)


def _can_step_back(read_from_s: float, duration_ms: int) -> bool:
    """Whether another step can be read before ``read_from_s``: one that starts no earlier than the middle of the file
    and is long enough to hold the ``rule_j.STORY_BEFORE_RUN_S`` of story before a run that an answer needs."""
    return read_from_s - duration_ms / 2000.0 >= rule_j.STORY_BEFORE_RUN_S


def _step_rows(
    keyframes: Callable[[float, float, float], list[rule_j.Row]], start_s: float, end_s: float, deadline: float
) -> list[rule_j.Row] | None:
    """One later step's keyframes, decoded within what is left of the look-back's time, or None when that ran out
    before or during the decode."""
    remaining_s = deadline - time.monotonic()
    if remaining_s <= 0:
        return None
    try:
        return keyframes(start_s, end_s - start_s, remaining_s)
    except frames.DecodeTimeoutError:
        return None


def _timed_out_lately(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether this exact file timed out decoding its credit text less than ``TIMEOUT_RETRY`` ago (never on a forced
    run)."""
    if ctx.force:
        return False
    failed_at = ctx.store.credits_text_timed_out_at(FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns))
    return failed_at is not None and ctx.now() - failed_at < TIMEOUT_RETRY


def _gives_up(rec: FileRecord, ctx: PipelineContext) -> str | None:
    """Why the detector won't decode this file now, or None.

    Read once per run of the file (``ctx.run_memo``), so the check stage's hand-off and the detector itself can't
    disagree: the day after a timeout could otherwise end between the two reads and put a full decode on the checking
    thread.
    """
    memo = ctx.run_memo(rec.canonical_path)
    if _GIVES_UP not in memo:
        if not rec.duration_ms:
            memo[_GIVES_UP] = "the file's duration is unknown"
        elif _timed_out_lately(rec, ctx):
            memo[_GIVES_UP] = "reading the file timed out less than a day ago; a forced re-detect tries now"
        else:
            memo[_GIVES_UP] = None
    return memo[_GIVES_UP]


def credits_text_needs_worker(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether reading a file's credit text needs a worker: only when it is going to be decoded.

    A file with no known duration, or one that timed out lately, makes :func:`detect_credits_text` give up at once, so
    that happens on the checking thread instead of holding a worker (one by default) every run for the day.

    Args:
        rec: The file.
        ctx: The job's context.

    Returns:
        True when the detector will decode the file.
    """
    return _gives_up(rec, ctx) is None


def detect_credits_text(
    rec: FileRecord,
    *,
    ctx: PipelineContext,
    gpu: str | None = None,
    gpu_device_path: str | None = None,
    phase_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
) -> DetectorAnswer:
    """The local detector: a credits candidate from the file's on-screen credit roll.

    A paused job doesn't block the worker here (T-R9): one file's decode is bounded, and the job pauses between files.

    Args:
        rec: The file (its identity matches the disk: the pipeline just checked).
        ctx: The job's context (``config.ffmpeg_path``, ``store``, ``now``, ``force``).
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.
        phase_callback: Worker row step text.
        cancel_check: True once the job is cancelled.
        pause_check: Unused.

    Returns:
        One candidate (its end None when the roll runs to the end of the file, Q3), or none when the tail holds no
        credit roll ("nothing found"); either way based on :data:`LOOK_BACK_BASIS`.

    Raises:
        CodecNotSupportedError: The GPU decode failed; the worker reruns the file on the CPU.
        DetectorUnavailableError: No answer this time (duration unknown, decode failed, timed out now or in the last
            day, cancelled, text detection failed); nothing is stored.
    """
    from ...processing.generator import CodecNotSupportedError
    from ..pipeline import DetectorAnswer, DetectorUnavailableError

    reason = _gives_up(rec, ctx)
    if reason is not None:
        raise DetectorUnavailableError(reason)
    # The process's one pool (spec §6.4 item 7): never closed here, since closing it ends it for every later file.
    pool = get_textdet_pool()
    try:
        result = find_credits(
            rec.canonical_path,
            duration_ms=rec.duration_ms,
            is_episode=rec.season_key is not None,
            tail_s=_tail_s(rec, ctx),
            ffmpeg=getattr(ctx.config, "ffmpeg_path", None) or "ffmpeg",
            detect_boxes=lambda planes: pool.detect_boxes(planes, gpu=gpu, gpu_device_path=gpu_device_path),
            gpu=gpu,
            gpu_device_path=gpu_device_path,
            cancel_check=cancel_check,
            phase=phase_callback,
        )
    except frames.GpuDecodeError as exc:
        raise CodecNotSupportedError(str(exc)) from exc
    except frames.DecodeCancelledError as exc:
        raise DetectorUnavailableError("cancelled") from exc
    except frames.DecodeTimeoutError as exc:
        now = ctx.now()
        identity = FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)
        ctx.store.record_credits_text_timeout(identity, now, forget_before=now - TIMEOUT_RETRY)
        raise DetectorUnavailableError(str(exc)) from exc
    except (frames.FrameDecodeError, TextDetUnavailableError) as exc:
        raise DetectorUnavailableError(str(exc)) from exc
    if result.start_s is None:
        logger.debug("No credit roll in the end of {}", os.path.basename(rec.canonical_path))
        return DetectorAnswer((), LOOK_BACK_BASIS)
    end_ms = None if result.end_s is None else int(round(result.end_s * 1000))
    found = Candidate(MarkerType.CREDITS, int(round(result.start_s * 1000)), end_ms, Source.CREDITS_TEXT)
    return DetectorAnswer((found,), LOOK_BACK_BASIS)


def _tail_s(rec: FileRecord, ctx: PipelineContext) -> float:
    """The tail length for a file: the user's window for its kind, else the default. A file whose path names no
    episode (``season_key`` None) is a movie, as it is everywhere else."""
    return frames.tail_length_s(
        is_episode=rec.season_key is not None, tv_s=ctx.settings.credits_tv_s, movie_s=ctx.settings.credits_movie_s
    )


def credits_answer_version(rec: FileRecord, ctx: PipelineContext) -> int:
    """The version a file's credit text answer is stored under, which depends on the window it was read from.

    Args:
        rec: The file.
        ctx: The job's context.

    Returns:
        :data:`CREDITS_TEXT_VERSION` when the user has no window for the file's kind, else that with the window added.
    """
    chosen = ctx.settings.credits_tv_s if rec.season_key is not None else ctx.settings.credits_movie_s
    return CREDITS_TEXT_VERSION if chosen is None else CREDITS_TEXT_VERSION + chosen * _WINDOW_VERSION_STEP


def credits_text_due(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether a stored answer of this version is asked again anyway: a "nothing found" read when the look-back
    stopped after one step, on a file where it now reads further.

    Nothing else can differ. A found start had story before it within the one step, so the steps after it are never
    read and it is the same answer; and a file whose one step already reached within 30 s of its middle reads nothing
    more now. Asked once: the answer it stores is based on :data:`LOOK_BACK_BASIS`.

    Args:
        rec: The file.
        ctx: The job's context.

    Returns:
        True when the detector should read the file again.
    """
    if not rec.duration_ms or ctx.store.get_detector_run(rec.id, Source.CREDITS_TEXT) == LOOK_BACK_BASIS:
        return False
    stored = [row for row in ctx.store.evidence_rows(rec.id) if row.source is Source.CREDITS_TEXT]
    if not stored or any(row.type is not None for row in stored):
        return False
    first_step_s = max(0.0, frames.tail_start_s(rec.duration_ms, tail_s=_tail_s(rec, ctx)) - rule_j.READ_BEFORE_TAIL_S)
    return _can_step_back(first_step_s, rec.duration_ms)


def credits_text_spec() -> LocalDetectorSpec:
    """The detector as the pipeline registers it: credits only, on a worker whenever it decodes, answers kept per file
    identity.

    Returns:
        Its spec.
    """
    from ..pipeline import LocalDetectorSpec

    return LocalDetectorSpec(
        source=Source.CREDITS_TEXT,
        types=frozenset({MarkerType.CREDITS}),
        detect=detect_credits_text,
        version=CREDITS_TEXT_VERSION,
        version_of=credits_answer_version,
        due=credits_text_due,
        needs_worker=credits_text_needs_worker,
    )
