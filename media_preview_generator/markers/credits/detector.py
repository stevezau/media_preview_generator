"""The credit text detector (spec §5.4): keyframes of the tail → rule J → one frame a second just before its start (and
around its end when a scene follows the roll, Q3) → one credits candidate from ``credits_text``. The pipeline registers
it as a local detector that runs on a worker whenever it decodes (:func:`credits_text_needs_worker`).

The rows the result carries are the ones that were decoded. Rule J version 3 reads the chosen run without the text
that sits in one place right across the story (``rule_j.overlay_boxes``, spec §13 item 15), and the 1 fps refine and
end rows the same way -- which is also what keeps its band steps (``rule_j.same_roll``, ``rule_j.reach_back``, §13
item 14) from walking a start back over story keyframes whose only box is a channel bug.
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
from loguru import logger

from ..decide import credits_limits_ms, earliest_credits_start_ms
from ..models import Candidate, FileIdentity, MarkerType, Source
from . import decode_check, frames, rule_j
from .textdet_helper import TextDetShuttingDownError, TextDetUnavailableError, get_textdet_pool

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
# Reading on before the tail step by step while the run still starts too close to the first row read (2026-09-23) is
# not a version: no answer that was found moves. A "nothing found" stored before it is asked again once instead, and
# only where a step after the first can be read (:func:`credits_text_due`).
# 4: every decode path scales frames the same way -- the whole decoded frame, downloaded from the GPU when it was
# decoded there, to 320x180 by the nearest pixel (``frames._scale_filter``) -- where each vendor's own scaler blurred
# text a few pixels tall differently, so credits found on NVIDIA were lost on Intel and the CPU; and a tail with no
# answer at 320x180 is read again at 640x360 (``RETRY_SCALE``). Any stored answer, found or not, can move.
# 5: the 640x360 reading of the rest of a file after an answer that ends in a scene reads a keyframe whole when it
# holds text only that frame boxes (a roll the 320x180 reading half boxed), and a roll read at 640x360 starts on dense
# text or the text it runs into without a break (``rule_j.start_on_dense_text``: small print on story before it is not
# its start). Answers read at 640x360 can move; one found at 320x180 that runs to the end of the file can't.
CREDITS_TEXT_VERSION = 5
# A stored answer's version is CREDITS_TEXT_VERSION for Automatic (what it has always been, so nothing is decoded again
# on upgrade) and CREDITS_TEXT_VERSION + window seconds * this for a window the user chose. The smallest window
# (300 s) gives 300,005, so a chosen window's version never equals Automatic's, and another window's answer is asked
# again.
_WINDOW_VERSION_STEP = 1000
# Stored with every answer as what it was based on (``detector_runs``). An answer without it was read when the look-back
# stopped after one step (:func:`credits_text_due`).
LOOK_BACK_BASIS = "steps back to the earliest kept start"
# The first step before the tail keeps the one decode's time limit it always had, counted from its start, and every
# later step gets what is left of it, so a reading's worst case stays what it was (a file with no answer at 320x180
# has two readings, :func:`find_credits`). Every later step that would start past
# it, or runs past it, is a timeout like any decode's (T-R7): the file is asked again the next day.
LOOK_BACK_TIMEOUT_S = frames.DECODE_TIMEOUT_S
# A tail whose 320x180 frames give no answer is read once more at this many times the size, 640x360. Small credit
# cards can box nothing at 320x180 (Accused (2020): "PRODUCER / DIRECTOR", "COLORIST" at 4-11 boxes a frame at 640x360,
# none at 320x180), and the roll is then no run at all, or story captions become the last run and are refused. So
# does the rest of the file after a 320x180 answer that ends in a scene (a roll invisible at 320x180 may follow it);
# an answer that runs to the end of the file is never read again and stays exactly as it was.
RETRY_SCALE = 2
# ... where a box more than this many pixels tall (in 320x180 pixels) is not the small text that reading is for, and
# is dropped unless the 320x180 reading boxed it too (a keyframe's logo or title, text either way): small text makes
# boxes 4-11 px tall there (2,054 of the 2,084 boxes only the larger frame found on 18 real rolls; 20 more are 12-15
# px), while at 640x360 the model also boxes dark footage -- blobs up to the frame's height -- which on a dark frame
# (one box makes a credit frame) join into runs. Measured on the files the larger reading answered: 11 px loses a real
# roll (Lisa Ann Walter: It Was an Accident), 19 px lets a making-of's blobs back in as a run 336 s before its credits
# (Frankenstein: The Anatomy Lesson), 15 px has neither (small-text-retry.md).
SMALL_TEXT_MAX_HEIGHT_PX = 15
# ... and whose roll starts on a frame this dense (``rule_j.start_on_dense_text``): a dark credit frame, or a lit one
# with this many boxes, twice the 3 a lit credit frame needs. Swept at 4, 5, 6 and 8 on the 80, the 205, Accused and
# I Survived a Serial Killer: 4 and 5 leave S01E14 starting on court footage whose small print boxes four and five a
# frame, 68 s early; 6 and 8 give every file the same verdict, but 8 starts Animal (2023) 241 s later.
DENSE_BOXES = rule_j.RULE_J.dense * RETRY_SCALE
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
        scale: The frames the answer was read at, in multiples of 320x180: 1, or ``RETRY_SCALE`` when the larger
            reading answered (the 320x180 one had no answer, or its answer ended in a scene and the rest of the file
            held a roll: ``key_rows`` before that end are then the 320x180 reading's). The boxes are in 320x180 pixels
            either way. At ``RETRY_SCALE`` every row decoded at that size is as decoded less the boxes too tall for
            small text (:func:`_small_text`), and ``key_rows`` still holds the text the 320x180 reading had boxed.
        run_rows: At ``RETRY_SCALE``, the keyframe rows rule J found the runs on: ``key_rows`` without the text the
            320x180 reading had already boxed (:func:`_not_seen`; after a 320x180 answer's end, a keyframe that also
            holds text only the larger frame boxes keeps all of its text), which can't be gathered again from
            ``key_rows`` alone; a frame's own text is these without ``overlays``. Empty at 1, where the runs are
            ``key_rows``'.
    """

    start_s: float | None
    end_s: float | None
    key_rows: tuple[rule_j.Row, ...]
    fine_rows: tuple[rule_j.Row, ...]
    end_rows: tuple[rule_j.Row, ...]
    overlays: tuple[rule_j.Box, ...] = ()
    scale: int = 1
    run_rows: tuple[rule_j.Row, ...] = ()


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
    earliest_start_s: float | None = None,
    check_gpu_decode: bool = False,
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
    the same keyframe pass, and rule J runs on both when the run continues into them (``rule_j.joined_before``). The
    run has then crossed the tail's edge, so while it still starts under 30 s after the first row read
    (``rule_j.too_little_story``) the step before is read and put in front too (``rule_j.rows_before``), whether it is
    more of the roll or the story the start needs, until the start has that story. The first step reads what it always
    has; the later ones never read further back than 30 s before ``earliest_start_s`` and are never shorter than 30 s
    (:func:`_next_step_start`), and all of them share one decode's time limit (``LOOK_BACK_TIMEOUT_S``): a later step
    that runs past it is a timeout, as any decode's is. A run still too close to the first row when the steps stop has
    no answer, as one that began too early always had. A step whose window holds no keyframe isn't empty: ffmpeg gives
    the first keyframe after the window (exit 0, on the GPU and the CPU alike), which is the first row already read,
    and the join drops it, so the step adds nothing (``evidence/credits/empty-window-decode.md``).

    All of that reads 320x180 frames. When it gives no answer and decoded any frame at all, it is done once more at
    ``RETRY_SCALE`` times the size, 640x360 -- the tail, its steps and its refine windows, through the same keyframe
    pass and start time, with the boxes brought back to 320x180 pixels (``frames.run_decode``) -- because credit cards
    too small to box at 320x180 leave the roll no run, or leave story captions as the last run to be refused. The
    larger reading finds its runs, and reads a frame's own text, without what the 320x180 one boxed
    (:func:`_not_seen`): that text was read and held no roll, and the larger reading is for the text too small to box
    there, so its roll has to be made of that text. Read again, an epilogue card the smaller frame boxed is glued onto
    the roll only the larger frame shows, and a lower third whose words box apart at 640x360 makes a run over lit
    story. The larger reading's answer is kept when it has one, the 320x180 reading otherwise.

    An answer at 320x180 that ends in a scene -- more than 30 s of the file follows its end (``rule_j.credits_end``) --
    has the rest of the file, from that end, read again at 640x360 the same way, the keyframes before it being the
    320x180 reading's (all of their text seen). The roll whose names only the larger frame boxes is then the last run,
    after the scene: at 320x180 verdict and epilogue cards, or a card mid-episode, were the last run and answered 20 s
    to 6 min early (Accused (2020): 26 of 47 answers). After the end, a keyframe holding any text only the larger
    frame boxes is read whole: a roll the 320x180 reading half boxed there -- its cards boxed as blocks, a frame short
    of a 15 s run -- is made of that text too (I Survived a Serial Killer S01E04: without it, the roll kept too few boxes
    for a run and story captions stayed the answer, 92 s early), while a card it boxed whole (an epilogue card on
    black) is still seen text. The larger reading's roll starts on dense text (``rule_j.start_on_dense_text``, in
    either case). The larger reading's answer is kept only when it starts at or
    after that end (its keyframes before it are all seen text, but its start's 1 fps walk can reach back past it); an
    answer that runs to the end of the file has nowhere after it for a roll to be, and is not read again.

    A GPU failure, a cancel or the app stopping in either reading is the file's, and so is a timeout of the larger
    reading of a tail without an answer (it waits a day, as a 320x180 timeout does). A larger reading that times out
    after an answer, can't be decoded or can't have its text detected keeps the 320x180 answer or "nothing found"
    (raised, both readings would run and fail again every day). A file takes up to two readings' time: each has the
    decode limit, and the steps before the tail, it always had.

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
        earliest_start_s: The earliest credits start the decision keeps for this file
            (``decide.earliest_credits_start_ms``): the steps after the first stop 30 s before it, the story a start
            there needs. None reads the first step only.
        check_gpu_decode: Run the process's decode check on ``gpu`` first (``decode_check.check_device``: once per
            device, logged only). The decodes run on ``gpu`` either way; False, for the harness, runs no check.

    Returns:
        The start, the end, and the rows they came from, with the scale they were read at.

    Raises:
        frames.GpuDecodeError: The GPU decode failed or gave no frames. A keyframe pass gives none only when no
            keyframe follows its start anywhere in the file (a VP9 tail whose container flags none): a window without
            one gives the first keyframe after it instead.
        frames.DecodeTimeoutError: A decode, or the start time or video packet probe before them, ran past its time
            limit.
        frames.FrameDecodeError: ffprobe couldn't read the start time, either probe wasn't started (earlier ffprobes
            being stuck), or ffmpeg couldn't decode the frames.
        frames.DecodeCancelledError: The job was cancelled before or during a decode, or during the GPU decode check.
        TextDetUnavailableError: Text detection couldn't answer.
    """
    show = phase or (lambda _text: None)
    show(READING_PHASE)
    start_time_s = frames.container_start_s(path, ffmpeg, cancel_check=cancel_check)
    thinning = frames.keyframe_thinning(path, ffmpeg, cancel_check=cancel_check)
    if check_gpu_decode:
        decode_check.check_device(gpu, gpu_device_path, ffmpeg=ffmpeg, cancel_check=cancel_check)
    decode = {"ffmpeg": ffmpeg, "gpu": gpu, "gpu_device_path": gpu_device_path, "detect_boxes": detect_boxes,
              "cancel_check": cancel_check, "start_time_s": start_time_s,
              "download_format": thinning.download_format}  # fmt: skip
    tail_start = frames.tail_start_s(
        duration_ms, tail_s=frames.tail_length_s(is_episode=is_episode) if tail_s is None else tail_s
    )
    read = functools.partial(_read_credits, path, duration_ms=duration_ms, tail_start=tail_start, decode=decode,
                             thinning=thinning, earliest_start_s=earliest_start_s, show=show)  # fmt: skip
    seen: list[rule_j.Row] = []
    found = read(scale=1, seen=(), decoded=seen)
    if not found.key_rows or (found.start_s is not None and found.end_s is None):
        return found
    after = {}
    if found.end_s is not None:
        after = {"from_s": found.end_s, "rows_before": [row for row in found.key_rows if row[0] < found.end_s]}
    try:
        larger = read(scale=RETRY_SCALE, seen=seen, decoded=None, **after)
    except (frames.GpuDecodeError, TextDetShuttingDownError):
        # The worker's CPU rerun reads both sizes again; the app stopping mid-read (worker threads are daemons, so the
        # pool closes under them) is no answer, as it is at 320x180, not a "nothing found" kept for good.
        raise
    except (frames.FrameDecodeError, TextDetUnavailableError) as exc:
        if found.start_s is not None:
            # An answer read after its end keeps that answer: it was found, and a larger reading that can't finish
            # there is no reason to lose it or to read the file again every day.
            logger.warning("Kept the 320x180 answer for the credits of {}: reading the rest of its file at 640x360 "
                           "failed: {}", os.path.basename(path), exc)  # fmt: skip
            return found
        if isinstance(exc, frames.DecodeTimeoutError):
            # A tail with no answer times out like one at 320x180 (T-R7), the look-back's shared deadline included: a
            # mount that stalled between the readings, or a larger reading slow on this host, is no answer, and the
            # file is asked again in a day rather than kept as "nothing found" for good.
            raise
        # The 320x180 reading of the same tail has just completed, so the larger reading failed on its own account (a
        # decode that can't be read, a text detection helper that died); raised, both readings would run and fail
        # again every day. The trade-off: a CPU helper that dies of something passing (the OOM killer) stores
        # "nothing found", and the file is next read when it changes or on a forced re-detect.
        logger.warning('Stored "nothing found" for the credits of {}: reading its tail at 640x360 failed: {}',
                       os.path.basename(path), exc)  # fmt: skip
        return found
    if larger.start_s is None or (found.end_s is not None and larger.start_s < found.end_s):
        # Its start walked back (1 fps rows) into the answer's own stretch: the scene the answer ended on is not where
        # the larger reading's run begins, so it is no roll after that scene (OMG 2 (2023): 90 s later still, and late).
        return found
    return larger


def _read_credits(
    path: str,
    *,
    duration_ms: int,
    tail_start: float,
    decode: dict[str, Any],
    thinning: frames.KeyframeThinning,
    earliest_start_s: float | None,
    show: Callable[[str], None],
    scale: int,
    seen: Sequence[rule_j.Row],
    decoded: list[rule_j.Row] | None,
    from_s: float | None = None,
    rows_before: Sequence[rule_j.Row] = (),
) -> CreditsTextResult:
    """One reading of the ending at ``scale`` times 320x180: the tail and the steps before it, rule J, and the refine
    windows of its answer, every decode at that scale (:func:`find_credits` has the rest). With ``from_s``, only the
    keyframes from there to the end are read, after ``rows_before`` (another reading's keyframes before it).

    In the larger reading every row -- keyframes and 1 fps alike -- is read without the boxes taller than small text
    makes that the 320x180 reading didn't box in the same keyframe (:func:`_small_text`), right as it is decoded: they
    are not text. ``seen`` is every keyframe row the 320x180 reading decoded, when this is the larger one. The text it
    boxed is left out wherever rule J finds the runs or reads a frame's own text (:func:`_not_seen`), the join before
    the tail included -- except, after ``from_s``, on a keyframe that also holds text only the larger frame boxes,
    which is read whole; the overlays and ``rule_j.text_all_through`` read the rows as decoded, as they do at 320x180.
    The larger reading's roll starts on dense text (``rule_j.start_on_dense_text``). ``decoded``, when given, collects
    every keyframe row this reading decodes, joined or not.
    """
    decode = {**decode, "scale": scale}
    cancel_check = decode["cancel_check"]
    seen_boxes = {row[0]: rule_j.boxes_of(row) for row in seen}
    # After a 320x180 answer's end, a keyframe holding text only the larger frame boxes is read whole: a roll that
    # reading half boxed (as blocks, a frame short of a 15 s run) is that text too (I Survived a Serial Killer S01E04:
    # left out, the roll kept too few boxes for a run and story captions stayed the answer, 92 s early). A keyframe
    # whose text it boxed all of -- an epilogue card on black after the answer's end -- still has none, or the 24 s
    # join glues the card onto the roll (Accused (2020) S04E05, S07E02: 25.5 and 17.5 s early read whole). A tail
    # without an answer keeps the rule it was measured with: read whole there too, no wrong answer is fixed and five
    # move, one from no answer to 20 s before its chapter (small-text-retry.md).
    read_whole = from_s is not None

    def decode_rows(*, keyframes_only: bool, **window: Any) -> list[rule_j.Row]:
        rows = frames.decode_rows(path, **decode, keyframes_only=keyframes_only, **window)
        if scale == 1:
            return rows
        # 1 fps frames aren't matched to the 320x180 keyframes: one landing on a keyframe's time would keep a tall box
        # its neighbours drop.
        return _small_text(rows, seen_boxes if keyframes_only else {})

    def keyframes(start_s: float, length_s: float | None, timeout_s: float | None = None) -> list[rule_j.Row]:
        limit = {} if timeout_s is None else {"timeout_s": timeout_s}
        rows = decode_rows(
            start_s=start_s, length_s=length_s, keyframes_only=True, fps=None, keep_every=thinning.keep_every,
            drop_non_key=thinning.drop_non_key, **limit,
        )  # fmt: skip
        if decoded is not None:
            decoded.extend(rows)
        return rows

    if from_s is None:
        key_rows = keyframes(tail_start, None)
    else:
        # A stride keeps one packet in so many counted from the seek, so read from the answer's end it would keep
        # others than the 320x180 reading did, and none of their text would be matched to it (_not_seen, _small_text):
        # a strided pass seeks where the tail's did and drops what comes before the end. A keyframe pass decodes the
        # same keyframes from any seek.
        seek_s = tail_start if thinning.keep_every else from_s
        key_rows = [*rows_before, *(row for row in keyframes(seek_s, None) if row[0] >= from_s)]
    # Text that never moves off one spot across the story is a channel or score bug, a ticker or a timecode, not
    # credits, so everything that reads a frame's own text reads the rows without it: which of the run's frames are
    # credit frames, the anchor's spacing, and the band steps' own bands and cadences (spec §13 items 14 and 15).
    # Which run is the last one is still read from the rows with their overlays, and so is the share
    # text_all_through counts: that step is what catches a file whose overlay this one doesn't find, and counting the
    # overlay out would take its answer away. The larger reading finds its runs as well as a frame's own text without
    # the text the 320x180 reading boxed (new_text): that text held no roll there, and counted again, the words of a
    # lower third that box apart at 640x360 make a run over lit story (A Season to Remember (2024), decoded on the CPU:
    # 347 s early, published at Medium). The overlays and text_all_through still read that text: gathered from rows
    # without it, a bug boxed at both sizes would have no sightings and stay in the 1 fps rows.
    overlays = rule_j.overlay_boxes(key_rows)

    def new_text(rows: Sequence[rule_j.Row]) -> list[rule_j.Row]:
        return _not_seen(rows, seen_boxes, read_whole=read_whole)

    def own_text(rows: Sequence[rule_j.Row]) -> list[rule_j.Row]:
        return rule_j.without_overlays(new_text(rows), overlays)

    rule_rows = own_text(key_rows)
    coarse = rule_j.coarse_start(new_text(key_rows), without=rule_rows)
    # Everything that reads the *runs* rather than one run's frames reads the rows the runs were found on:
    # opens_on_the_run and joined_before both find the runs again, and they have to find the run coarse came from. The tail's overlays carry over to the
    # joined rows and are not gathered again from them: a roll that began before the tail is exactly the shape that
    # must not be read as its own overlay. A real bug excludes itself from the tail's own overlays on this branch --
    # opens_on_the_run only says yes when every row before the run *in decode order* is dark, and a dark row a bug is
    # boxed on is a credit frame, which dark frames then merge into the run. What is left is a shape nothing measured
    # has: four or more keyframes emitted after the whole chosen run yet timestamped before its earliest frame, since
    # overlay_boxes takes its story by time while that check reads decode order (the reordering measured on the 80 is
    # 10-21 s, and this branch's story is under 30 s all told).
    if coarse is not None and tail_start > 0 and rule_j.opens_on_the_run(new_text(key_rows), coarse):
        deadline = time.monotonic() + LOOK_BACK_TIMEOUT_S
        # The first step reads what it always has, even where the whole of it lies before the earliest start the
        # decision keeps (a movie on Automatic): narrowed to that bound, or to 30 s before it, it turns stored answers
        # into none -- ones the decision refuses anyway, but answers that were found (spec §14 2026-09-23).
        before_start = max(0.0, tail_start - rule_j.READ_BEFORE_TAIL_S)
        before_rows = keyframes(before_start, tail_start - before_start)
        joins = rule_j.joined_before(new_text(before_rows), new_text(key_rows), overlays=overlays) is not None
        joined = rule_j.rows_before(before_rows, key_rows) if joins else None
        while joined is not None:
            key_rows = joined
            rule_rows = own_text(key_rows)
            coarse = rule_j.coarse_start(new_text(key_rows), without=rule_rows)
            # Stopping with too little story before the run leaves it to text_all_through, which answers nothing.
            if coarse is None or not rule_j.too_little_story(key_rows, coarse):
                break
            read_from, before_start = before_start, _next_step_start(before_start, earliest_start_s)
            if before_start is None:
                break
            before_rows = _step_rows(keyframes, before_start, read_from, deadline, cancel_check, os.path.basename(path))
            # The run crossed the tail's edge at the first step, so a later step is kept whatever it holds: more of the
            # roll moves the start back, and story is what the start needs before it.
            joined = rule_j.rows_before(before_rows, key_rows)
    run_rows = tuple(new_text(key_rows)) if scale > 1 else ()
    if coarse is not None and scale > 1:
        shown = rule_j.without_overlays(key_rows, overlays)
        coarse = rule_j.start_on_dense_text(rule_rows, coarse, run_rows, shown, dense_boxes=DENSE_BOXES)
    if coarse is None or rule_j.text_all_through(key_rows, coarse):
        return CreditsTextResult(None, None, tuple(key_rows), (), (), overlays, scale, run_rows)
    show(REFINING_PHASE)
    # max() can't bind while text_all_through holds an answered run 30 s past its first row; it keeps -ss non-negative.
    fine_start = max(0.0, coarse.pts_s - rule_j.REFINE_BEFORE_S)
    fine_length = coarse.pts_s + rule_j.REFINE_AFTER_S - fine_start
    fine_rows = decode_rows(start_s=fine_start, length_s=fine_length, keyframes_only=False, fps=1)
    start_s = rule_j.refine_start(rule_rows, coarse, rule_j.without_overlays(fine_rows, overlays))
    duration_s = duration_ms / 1000.0
    last_keyframe = rule_j.coarse_end_s(rule_rows, coarse)
    if not rule_j.keeps_a_scene_after(last_keyframe, duration_s):
        return CreditsTextResult(start_s, None, tuple(key_rows), tuple(fine_rows), (), overlays, scale, run_rows)
    show(REFINING_END_PHASE)
    # From where the end's walk starts to where the latest credit keyframe's walk may reach: credits_end decides
    # whether there is an end from the latest one, then where it is from end_keyframe_s (the same keyframe unless the
    # end steps back over scene text; then at most 24 s earlier, the join's reach).
    end_start = max(0.0, rule_j.end_keyframe_s(rule_rows, coarse) - rule_j.REFINE_END_BEFORE_S)  # as fine_start
    end_length = last_keyframe + rule_j.REFINE_END_AFTER_S - end_start
    end_rows = decode_rows(start_s=end_start, length_s=end_length, keyframes_only=False, fps=1)
    end_s = rule_j.credits_end(rule_rows, coarse, rule_j.without_overlays(end_rows, overlays), duration_s)
    return CreditsTextResult(
        start_s, end_s, tuple(key_rows), tuple(fine_rows), tuple(end_rows), overlays, scale, run_rows
    )


def _small_text(rows: Sequence[rule_j.Row], seen_boxes: dict[float, tuple[rule_j.Box, ...]]) -> list[rule_j.Row]:
    """The larger reading's rows without the boxes taller than ``SMALL_TEXT_MAX_HEIGHT_PX`` that the 320x180 reading
    didn't box, each recounted.

    That reading is for text too small to box at 320x180, and such text makes boxes at most that tall. A taller box the
    640x360 frame gives is either text the 320x180 reading boxes too -- kept: a logo or a title boxed at every size is
    text, and the overlays and ``rule_j.text_all_through`` have to see it, as they do at 320x180 -- or no text at all:
    the model boxes dark footage at 640x360. A tall box is the 320x180 reading's when it lies
    ``rule_j.OVERLAY_CONTAINMENT`` or more inside one of that frame's boxes there, the test :func:`_not_seen` uses. A
    row that never recorded its boxes keeps its count (``rule_j.boxes_of``).

    Args:
        rows: Rows decoded at ``RETRY_SCALE``, boxes in 320x180 pixels.
        seen_boxes: Each 320x180 keyframe row's boxes by its time; empty for rows no 320x180 keyframe matches.

    Returns:
        Rows of the same length, order and times.
    """
    out = []
    for row in rows:
        if len(row) <= 3:
            out.append(row)
            continue
        tall = tuple(box for box in row[3] if box[3] - box[1] + 1 > SMALL_TEXT_MAX_HEIGHT_PX)
        unseen = set(rule_j.without_overlays([(row[0], len(tall), row[2], tall)], seen_boxes.get(row[0], ()))[0][3])
        kept = tuple(box for box in row[3] if box not in unseen)
        out.append((row[0], len(kept), row[2], kept))
    return out


def _not_seen(
    rows: Sequence[rule_j.Row], seen_boxes: dict[float, tuple[rule_j.Box, ...]], *, read_whole: bool = False
) -> list[rule_j.Row]:
    """The larger reading's keyframe rows without the text the 320x180 reading already boxed in the same frame.

    That text was read at 320x180 and rule J found no roll in it; the larger reading is for text too small to box
    there, so it finds its runs and reads a frame's own text without it. Read again, a card the smaller frame already
    showed -- an epilogue card on black, story captions, burnt-in subtitles -- is a credit frame the 24 s join glues
    onto the roll only the larger frame shows (Accused (2020): 4 of the 14 files with no answer at 320x180 started
    13-34 s early on such cards). A box goes when it lies ``rule_j.OVERLAY_CONTAINMENT`` or more inside one of that
    frame's 320x180 boxes, the same test an overlay's boxes are dropped by, so a frame keeps whatever text only the
    larger frame shows. Frames are matched by their time: both readings decode the same keyframes. A row the smaller
    reading never decoded (a step before the tail it didn't take) keeps all its boxes.

    With ``read_whole`` (the rest of a file after a 320x180 answer's end, :func:`find_credits`), a frame that keeps any
    box is kept whole instead: it shows text only the larger frame boxes, so the 320x180 reading saw only part of it,
    and a roll it half boxed there is made of both. A frame whose every box it boxed still has none.

    Args:
        rows: The larger reading's keyframe rows, boxes in 320x180 pixels.
        seen_boxes: Each 320x180 keyframe row's boxes by its time.
        read_whole: Keep a frame whole when it holds any box the 320x180 reading didn't box.

    Returns:
        Rows of the same length, order and times, each recounted.
    """
    if not seen_boxes:
        return list(rows)
    out = []
    for row in rows:
        unseen = rule_j.without_overlays([row], seen_boxes.get(row[0], ()))[0]
        out.append(row if read_whole and unseen[1] > 0 else unseen)
    return out


def _next_step_start(read_from_s: float, earliest_start_s: float | None) -> float | None:
    """Where the step after the first that ends at ``read_from_s`` starts, or None when no such step is read.

    A step is ``rule_j.READ_BEFORE_TAIL_S`` long and reads no further back than the ``rule_j.STORY_BEFORE_RUN_S`` of
    story a start at ``earliest_start_s`` needs before it: any start further back is one the decision refuses. Nor is
    it ever shorter than that 30 s: a remainder under it is read with the step before it (up to 150 s long), and one
    left over after the first step is not read. What that saves is a decode of its own -- an ffmpeg start, a seek and
    a hardware decoder brought up -- for a sliver that holds a keyframe or two at most, and often none: a keyframe pass
    over a window without one gives the first keyframe after it (exit 0, on the GPU and the CPU alike), which is the
    first row already read, so :func:`rule_j.rows_before` drops it (``evidence/credits/empty-window-decode.md``).
    None (no bound given) reads no step after the first.
    """
    if earliest_start_s is None:
        return None
    floor_s = earliest_start_s - rule_j.STORY_BEFORE_RUN_S
    if read_from_s - floor_s < rule_j.STORY_BEFORE_RUN_S:
        return None
    start_s = read_from_s - rule_j.READ_BEFORE_TAIL_S
    return start_s if start_s - floor_s >= rule_j.STORY_BEFORE_RUN_S else floor_s


def _step_rows(
    keyframes: Callable[[float, float, float], list[rule_j.Row]],
    start_s: float,
    end_s: float,
    deadline: float,
    cancel_check: Callable[[], bool] | None,
    name: str,
) -> list[rule_j.Row]:
    """One later step's keyframes, decoded within what is left of the look-back's time.

    Raises:
        frames.DecodeCancelledError: The job was cancelled. Asked first, so a cancel that lands once the time has also
            run out is recorded as a cancel, not as a timeout that keeps the file back for a day.
        frames.DecodeTimeoutError: The look-back's time ran out before or during the decode. It is a timeout like any
            decode's, so a stalled mount leaves the file for a day (T-R7) rather than storing "nothing found" --
            in either reading of a tail without an answer; after an answer, :func:`find_credits` keeps that answer.
    """
    if cancel_check and cancel_check():
        raise frames.DecodeCancelledError(f"cancelled before decoding {name}")
    remaining_s = deadline - time.monotonic()
    if remaining_s <= 0:
        raise frames.DecodeTimeoutError(
            f"reading before the tail of the credits of {name} ran past {LOOK_BACK_TIMEOUT_S:g} s before {end_s:.0f} s"
        )
    return keyframes(start_s, end_s - start_s, remaining_s)


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


def credits_text_failed_here(rec: FileRecord, ctx: PipelineContext) -> bool:
    """Whether reading this file's credit text failed as the file is now: a decode error (``detector_failures``,
    dropped when the file's identity changes) or a timeout (``credits_text_timeouts``, keyed by the identity).

    A credits chapter that rule 3 holds for credit text then decides as it did before rule 3 (spec §5.5): the file
    would otherwise wait in Needs review for an answer that may never come. Text detection being unavailable says
    nothing about the file and isn't recorded.

    Args:
        rec: The file.
        ctx: The job's context.

    Returns:
        True when a failure is recorded for this identity.
    """
    if ctx.store.get_detector_failure(rec.id, Source.CREDITS_TEXT) is not None:
        return True
    return ctx.store.credits_text_timed_out_at(FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)) is not None


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

    The file is decoded on the worker's GPU, whatever its codec; the first decode per device per process runs the
    decode check (``decode_check``), which only logs. Text detection runs on the worker's GPU as its own self-test
    decides.

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
            day, cancelled, text detection failed); nothing is stored. A failure of the 640x360 reading alone that
            doesn't raise (:func:`find_credits`: not on the GPU, a cancel, the app stopping, or a timeout of a tail
            without an answer) is not one: that stores the 320x180 answer or "nothing found".
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
            earliest_start_s=_earliest_start_s(rec, ctx),
            check_gpu_decode=True,
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
    except frames.ReadStalledError as exc:
        raise DetectorUnavailableError(str(exc)) from exc
    except frames.FrameDecodeError as exc:
        # Kept for this file as it is (a new identity drops it): rule 3 stops waiting for an answer it may never get.
        ctx.store.set_detector_failure(rec.id, Source.CREDITS_TEXT, str(exc) or type(exc).__name__)
        raise DetectorUnavailableError(str(exc)) from exc
    except TextDetUnavailableError as exc:
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


def _earliest_start_s(rec: FileRecord, ctx: PipelineContext) -> float:
    """The earliest credits start the decision keeps for a file, from the same numbers the pipeline decides it with."""
    window_ms, movie_cap_ms = credits_limits_ms(
        is_episode=rec.season_key is not None,
        tv_window_s=ctx.settings.credits_tv_s,
        movie_window_s=ctx.settings.credits_movie_s,
    )
    earliest_ms = earliest_credits_start_ms(
        rec.duration_ms, is_movie=rec.is_movie, credits_window_ms=window_ms, movie_credits_max_from_end_ms=movie_cap_ms
    )
    return earliest_ms / 1000.0


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
    stopped after one step, on a file where a step after the first can be read now.

    Nothing else can differ. A found start had story before it within the one step, so the steps after it are never
    read and it is the same answer; and where no step of 30 s or more fits between the first step and 30 s before the
    earliest start the decision keeps (:func:`_next_step_start`) -- every movie on Automatic, whose first step is all
    before its 900 s cap -- nothing more is read now. A file of unknown kind (no season, not a movie) reads the movie
    tail but has no cap, so it can be asked. Asked once: the answer it stores is based on :data:`LOOK_BACK_BASIS`.

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
    tail_start = frames.tail_start_s(rec.duration_ms, tail_s=_tail_s(rec, ctx))
    first_step_s = max(0.0, tail_start - rule_j.READ_BEFORE_TAIL_S)
    return tail_start > 0 and _next_step_start(first_step_s, _earliest_start_s(rec, ctx)) is not None


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
        failed_here=credits_text_failed_here,
    )
