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
    from ..pipeline import LocalDetectorSpec, PipelineContext
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
CREDITS_TEXT_VERSION = 3
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
            the 120 s before the tail, ``key_rows`` is the joined rows, and a roll that began before the tail is
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
    the same keyframe pass, and rule J runs on both when the run continues into them (``rule_j.joined_before``). That
    window can give no keyframe at all; on the GPU no frames is a GPU failure, and the worker's CPU rerun then gives
    the same no answer.

    Args:
        path: The media file (read only).
        duration_ms: Its duration.
        is_episode: Read the last 450 s instead of 900 s (T-R4).
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

    def keyframes(start_s: float, length_s: float | None) -> list[rule_j.Row]:
        return frames.decode_rows(
            path, start_s=start_s, length_s=length_s, keyframes_only=True, fps=None, keep_every=thinning.keep_every,
            drop_non_key=thinning.drop_non_key, **decode,
        )  # fmt: skip

    tail_start = frames.tail_start_s(duration_ms, is_episode=is_episode)
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
        before_start = max(0.0, tail_start - rule_j.READ_BEFORE_TAIL_S)
        before_rows = keyframes(before_start, tail_start - before_start)
        joined = rule_j.joined_before(before_rows, key_rows, overlays=overlays)
        if joined is not None:
            key_rows = joined
            rule_rows = rule_j.without_overlays(key_rows, overlays)
            coarse = rule_j.coarse_start(key_rows, without=rule_rows)
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
) -> list[Candidate]:
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
        One candidate (its end None when the roll runs to the end of the file, Q3), or [] when the tail holds no credit
        roll ("nothing found").

    Raises:
        CodecNotSupportedError: The GPU decode failed; the worker reruns the file on the CPU.
        DetectorUnavailableError: No answer this time (duration unknown, decode failed, timed out now or in the last
            day, cancelled, text detection failed); nothing is stored.
    """
    from ...processing.generator import CodecNotSupportedError
    from ..pipeline import DetectorUnavailableError

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
        return []
    end_ms = None if result.end_s is None else int(round(result.end_s * 1000))
    return [Candidate(MarkerType.CREDITS, int(round(result.start_s * 1000)), end_ms, Source.CREDITS_TEXT)]


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
        needs_worker=credits_text_needs_worker,
    )
