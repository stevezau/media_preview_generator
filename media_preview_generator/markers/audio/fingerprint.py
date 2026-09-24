"""Chromaprint fingerprints of an episode's opening (spec §5.3), cached per file identity in markers.db.

CPU only (there is no GPU chromaprint), ``-threads 2`` and at most two at a time across the whole app (spec §5.6).
Only jellyfin-ffmpeg carries the chromaprint muxer in the image; the arm64 image has none, so season audio is then
unavailable rather than failing every episode.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from enum import Enum
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger

from ..fs import gone_from_disk
from ..locks import KeyedLocks
from ..missing import sweep_missing_files
from ..probe import kill_and_collect, stuck_processes
from ..store import SEASON_PAIR_WINDOW, FileRecord, FingerprintCheck, MarkerStore, StoredFingerprint

if TYPE_CHECKING:
    from ...servers.base import ServerConfig

# One name for the window, defined beside the season-pair queries that read it (the store can't import this module).
WINDOW = SEASON_PAIR_WINDOW
ALGORITHM = 1
# A retimed fingerprint's audio is resampled to this rate and then declared to run at this rate times the retime factor,
# so it plays that much faster or slower, pitch with it: the exact inverse of a PAL speed-up (``markers.speed``). A
# pitch-keeping stretch (atempo) matched none of Bones season 5's 85 cross-speed pairs; this matched all 85.
RETIME_BASE_RATE = 48_000
# Retime factors outside this range are no PAL speed-up (a bug, not a file): ffmpeg is never started with one.
MIN_RETIME, MAX_RETIME = 0.5, 2.0
MAX_WINDOW_S = 900.0
WINDOW_FRACTION = 0.35
FFMPEG_THREADS = 2
MAX_PARALLEL = 2
# Fingerprinted files one cache sweep looks for on disk (about 28 KiB of markers.db each once gone): a 100k-episode
# library is gone through every 50 sweeps, at a few seconds of file stats each on a network mount.
MAX_SWEEP_CHECKS = 2_000
# Sweeps run on their own thread, one at a time and at most one start an hour. One still running after 10 minutes is
# waiting on a file system: a hard-mounted network share that stalls blocks os.stat without an error.
SWEEP_MIN_GAP_S = 3600.0
SWEEP_STUCK_S = 600.0
# A sweep checks no further file once this long has passed; the next one goes on from there.
SWEEP_BUDGET_S = 60.0
# How long an ffmpeg that didn't list its muxers (timed out, couldn't start, exited with an error) stays unknown before
# it is asked again.
CHROMAPRINT_RETRY_S = 600.0
JELLYFIN_FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
# How long a killed ffmpeg may take to let go of its output before it is left to a reaper thread.
KILL_WAIT_S = 5.0
_POLL_S = 0.5
# ffmpeg's wording when a file has no audio stream to fingerprint: a stored empty answer, not a retryable failure.
_NO_AUDIO_HINTS = ("does not contain any stream", "matches no streams", "Output file is empty")
_PARALLEL = threading.BoundedSemaphore(MAX_PARALLEL)
_FILE_LOCKS = KeyedLocks()
_monotonic = time.monotonic
# Per ffmpeg binary: what its muxer list said, and when to ask again (None: never).
_chromaprint_answers: dict[str, tuple[ChromaprintState, float | None]] = {}
_MUXER_LOCKS = KeyedLocks()
_SWEEP_LOCK = threading.Lock()
_sweep_started_at: float | None = None
_stuck_warned_at: float | None = None
# Killed ffmpegs that still held their output are counted under this reaper name (probe.stuck_processes). They no
# longer hold a fingerprint slot, so without the count every freed slot would start another one on the same mount.
REAPER = "fingerprint-reaper"


class ChromaprintState(str, Enum):
    """What checking the ffmpeg binaries for the chromaprint muxer found."""

    AVAILABLE = "available"
    # Every ffmpeg found listed its muxers, none with chromaprint (or there is no ffmpeg): kept for the process.
    ABSENT = "absent"
    # An ffmpeg didn't list its muxers (timed out, couldn't start, exited with an error): asked again later.
    UNKNOWN = "unknown"


class FingerprintError(Exception):
    """ffmpeg couldn't fingerprint the file. No fingerprint is stored; season audio records the failure, so other
    episodes' season steps skip the file for a day while it is unchanged, and only its own run and a forced re-detect
    try it again sooner."""


class FingerprintStalledError(FingerprintError):
    """``MAX_PARALLEL`` earlier fingerprint ffmpegs are still stuck reading their files (a stalled mount), so no new one
    is started. Not the file's fault: nothing is recorded against it, and nothing else should be."""


class FingerprintSkippedError(Exception):
    """The caller's ``skip`` said not to run ffmpeg on the file (season audio: it failed on it lately)."""


def window_s(duration_ms: int) -> float:
    """Seconds fingerprinted from the start: 35% of the file, at most 900 s (spec §5.3)."""
    return min(MAX_WINDOW_S, WINDOW_FRACTION * duration_ms / 1000.0)


def muxer_state(ffmpeg: str) -> ChromaprintState:
    """Whether an ffmpeg binary has the chromaprint muxer, as far as its muxer list tells.

    The answer of an ffmpeg that listed its muxers is kept for the process. One that didn't (timed out, couldn't start,
    exited with an error) is UNKNOWN for ``CHROMAPRINT_RETRY_S`` and is then asked again.
    """
    cached = _cached_muxer_state(ffmpeg)
    if cached is not None:
        return cached
    # Jobs starting together share one check per binary (up to 20 s each) instead of each running its own.
    with _MUXER_LOCKS.hold(ffmpeg):
        cached = _cached_muxer_state(ffmpeg)
        if cached is not None:
            return cached
        try:
            proc = subprocess.run([ffmpeg, "-hide_banner", "-muxers"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is None or proc.returncode != 0:
            _chromaprint_answers[ffmpeg] = (ChromaprintState.UNKNOWN, _monotonic() + CHROMAPRINT_RETRY_S)
            return ChromaprintState.UNKNOWN
        found = any(line.split()[1:2] == ["chromaprint"] for line in proc.stdout.splitlines())
        state = ChromaprintState.AVAILABLE if found else ChromaprintState.ABSENT
        _chromaprint_answers[ffmpeg] = (state, None)
        return state


def _cached_muxer_state(ffmpeg: str) -> ChromaprintState | None:
    cached = _chromaprint_answers.get(ffmpeg)
    if cached is not None and (cached[1] is None or _monotonic() < cached[1]):
        return cached[0]
    return None


def has_chromaprint(ffmpeg: str) -> bool:
    """Whether an ffmpeg binary is known to have the chromaprint muxer (:func:`muxer_state`)."""
    return muxer_state(ffmpeg) is ChromaprintState.AVAILABLE


def forget_chromaprint_answers() -> None:
    """Forget every ffmpeg's cached muxer answer (tests)."""
    _chromaprint_answers.clear()


def _ffmpeg_candidates(configured: str | None) -> list[str]:
    return [
        candidate
        for candidate in dict.fromkeys(c for c in (configured, JELLYFIN_FFMPEG, shutil.which("ffmpeg")) if c)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK)
    ]


def chromaprint_ffmpeg(configured: str | None) -> str | None:
    """The first ffmpeg with chromaprint: the configured one, jellyfin-ffmpeg, then ``ffmpeg`` on PATH."""
    for candidate in _ffmpeg_candidates(configured):
        if has_chromaprint(candidate):
            return candidate
    return None


def chromaprint_state(configured: str | None) -> ChromaprintState:
    """AVAILABLE when an ffmpeg :func:`chromaprint_ffmpeg` looks at has chromaprint, else UNKNOWN when one of them
    didn't list its muxers, else ABSENT."""
    unknown = False
    for candidate in _ffmpeg_candidates(configured):
        state = muxer_state(candidate)
        if state is ChromaprintState.AVAILABLE:
            return state
        unknown = unknown or state is ChromaprintState.UNKNOWN
    return ChromaprintState.UNKNOWN if unknown else ChromaprintState.ABSENT


def chromaprint_status(configured: str | None) -> tuple[str | None, str]:
    """The ffmpeg season audio uses and a sentence for Settings.

    Returns:
        ``(path, "")`` when found; ``(None, reason)`` otherwise.
    """
    found = chromaprint_ffmpeg(configured)
    if found:
        return found, ""
    if chromaprint_state(configured) is ChromaprintState.UNKNOWN:
        return None, "ffmpeg didn't answer the check for the chromaprint muxer; it is checked again in 10 minutes"
    return None, "Needs an ffmpeg with the chromaprint muxer (jellyfin-ffmpeg in the amd64 image); none was found"


def fingerprint_window(retime: float | None) -> str:
    """The ``fingerprints`` window a fingerprint is cached under: ``WINDOW`` for the file's own audio, and one per
    retime factor for its audio retimed to another speed (a file has one own speed, so the factor names the group's)."""
    return WINDOW if retime is None else f"{WINDOW}@{retime:.6f}"


def fingerprint_command(ffmpeg: str, path: str, length_s: float, retime: float | None = None) -> list[str]:
    """The spec §5.3 command: raw algorithm-1 chromaprint of the first ``length_s`` seconds, stereo.

    Args:
        ffmpeg: An ffmpeg with chromaprint.
        path: Media file.
        length_s: Seconds of the file fingerprinted from its start.
        retime: The file's own seconds per second of its season group's speed (``speed.retime_factor``), None for its
            own speed. The audio then plays at the group's speed, so a point ``i`` of the fingerprint is ``i * POINT_S
            * retime`` seconds into the file.

    Returns:
        The command.

    Raises:
        FingerprintError: ``retime`` isn't a speed change between half and double (the two it is made for are 4.3 %).
    """
    if retime is not None and not (math.isfinite(retime) and MIN_RETIME < retime < MAX_RETIME):
        raise FingerprintError(f"Not fingerprinting {os.path.basename(path)} with retime {retime}")
    speed = (
        [] if retime is None else ["-af", f"aresample={RETIME_BASE_RATE},asetrate={round(RETIME_BASE_RATE * retime)}"]
    )
    return [
        ffmpeg, "-nostdin", "-v", "error", "-threads", str(FFMPEG_THREADS),
        "-ss", "0", "-t", f"{length_s:.3f}", "-i", path,
        "-vn", "-sn", "-dn", "-ac", "2", *speed,
        "-f", "chromaprint", "-algorithm", str(ALGORITHM), "-fp_format", "raw", "-",
    ]  # fmt: skip


def compute_fingerprint(
    path: str,
    duration_ms: int,
    *,
    ffmpeg: str,
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = 300.0,
    retime: float | None = None,
) -> np.ndarray:
    """Run ffmpeg and return the fingerprint points.

    Args:
        path: Media file (read only).
        duration_ms: File duration, for the window.
        ffmpeg: An ffmpeg with chromaprint.
        cancel_check: True once the job is cancelled; ffmpeg is killed.
        timeout_s: Hard limit (a hung network mount must not hold a worker): the call returns within it plus
            ``KILL_WAIT_S`` and one poll.
        retime: Fingerprint the audio retimed to another speed (:func:`fingerprint_command`); None for its own.

    Returns:
        uint32 points (little-endian); empty for a file without an audio stream.

    Raises:
        FingerprintError: ffmpeg failed, timed out or the job was cancelled.
    """
    name = os.path.basename(path)
    command = fingerprint_command(ffmpeg, path, window_s(duration_ms), retime)
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            out, err = proc.communicate(timeout=_POLL_S)
            break
        except subprocess.TimeoutExpired:
            cancelled = bool(cancel_check and cancel_check())
            if cancelled or time.monotonic() > deadline:
                _stop(proc, name)
                why = "cancelled" if cancelled else f"timed out after {timeout_s:.0f} s"
                raise FingerprintError(f"Fingerprinting {name} {why}") from None
    if proc.returncode != 0:
        text = (err or b"").decode("utf-8", errors="replace")
        if any(hint in text for hint in _NO_AUDIO_HINTS):
            return np.zeros(0, dtype="<u4")
        raise FingerprintError(f"ffmpeg exited {proc.returncode} fingerprinting {name}: {text.strip()[-200:]}")
    usable = len(out) - len(out) % 4
    return np.frombuffer(out[:usable], dtype="<u4").copy()


def _stop(proc: subprocess.Popen, name: str) -> None:
    """Kill ffmpeg and collect it, waiting at most ``KILL_WAIT_S``.

    A read stuck on a stalled network mount leaves ffmpeg unkillable until the read returns, holding its pipes. Waiting
    for that would keep the worker, one of the two fingerprint slots and the file's lock for as long as the mount
    stalls, so such a process goes to a daemon reaper instead, and counts as stalled until the reaper collects it.
    """
    kill_and_collect(proc, what=f"ffmpeg fingerprinting {name}", reaper_name=REAPER, wait_s=KILL_WAIT_S)


def stalled_ffmpegs() -> int:
    """How many killed fingerprint ffmpegs are still stuck holding their output."""
    return stuck_processes(REAPER)


def _raise_if_stalled(name: str) -> None:
    stalled = stalled_ffmpegs()
    if stalled >= MAX_PARALLEL:
        raise FingerprintStalledError(
            f"Not fingerprinting {name}: {stalled} earlier fingerprint ffmpegs are still stuck reading their files"
        )


def points_of(stored: StoredFingerprint) -> np.ndarray:
    """The points of a cached fingerprint."""
    return np.frombuffer(stored.points, dtype="<u4").copy()


def cached_fingerprint(store: MarkerStore, rec: FileRecord, retime: float | None = None) -> StoredFingerprint | None:
    """A file's cached fingerprint, when it was made the way this build makes it (algorithm and window length).

    Args:
        store: The markers store.
        rec: The file's record.
        retime: The retime factor of the fingerprint wanted (:func:`fingerprint_command`), None for the file's own.

    Returns:
        The fingerprint, or None (never made, or made another way: it is computed again).
    """
    if not rec.duration_ms:
        return None
    return store.get_fingerprint(
        rec.id, fingerprint_window(retime), algorithm=ALGORITHM, length_s=window_s(rec.duration_ms)
    )


def has_cached_fingerprint(store: MarkerStore, rec: FileRecord, retime: float | None = None) -> bool:
    """Whether :func:`cached_fingerprint` finds one, without reading its points."""
    return bool(rec.duration_ms) and store.has_fingerprint(
        rec.id, fingerprint_window(retime), algorithm=ALGORITHM, length_s=window_s(rec.duration_ms)
    )


def ensure_fingerprint(
    store: MarkerStore,
    rec: FileRecord,
    *,
    ffmpeg: str,
    cancel_check: Callable[[], bool] | None = None,
    skip: Callable[[], bool] | None = None,
    on_failure: Callable[[], None] | None = None,
    retime: float | None = None,
) -> np.ndarray | None:
    """A file's fingerprint from the cache, computing and storing it when missing.

    Two callers asking for one file share one ffmpeg run; at most ``MAX_PARALLEL`` run app-wide.

    Args:
        store: The markers store.
        rec: The file's record; its identity must match the file on disk (callers check).
        ffmpeg: An ffmpeg with chromaprint.
        cancel_check: True once the job is cancelled.
        skip: Asked once the file's lock is held and nothing is cached: True means don't run ffmpeg. Callers that
            waited on the lock while another caller's ffmpeg failed see that failure here.
        on_failure: Called when ffmpeg fails (not when the job was cancelled, nor when earlier ffmpegs are stalled),
            before the lock is released, so a ``skip`` of a caller waiting on the lock sees what it records.
        retime: The file's audio retimed to its season group's speed (:func:`fingerprint_command`), cached apart from
            its own; None for its own.

    Returns:
        The points, or None when the file's row changed identity while ffmpeg ran (nothing stored).

    Raises:
        FingerprintStalledError: ``MAX_PARALLEL`` earlier ffmpegs are still stuck reading their files; none is started.
        FingerprintError: The file has no known duration, or ffmpeg failed.
        FingerprintSkippedError: ``skip`` said not to run ffmpeg.
    """
    name = os.path.basename(rec.canonical_path)
    with _FILE_LOCKS.hold(rec.id):
        if not rec.duration_ms:
            raise FingerprintError(f"No known duration for {name}")
        stored = cached_fingerprint(store, rec, retime)
        if stored is not None:
            return points_of(stored)
        if skip is not None and skip():
            raise FingerprintSkippedError(f"Not fingerprinting {name} again yet")
        _raise_if_stalled(name)  # rather than wait for a slot only to find the mount still stalled
        while not _PARALLEL.acquire(timeout=_POLL_S):
            if cancel_check and cancel_check():
                raise FingerprintError(f"Fingerprinting {name} cancelled")
        try:
            try:
                # A stalled ffmpeg is counted just before it gives back its slot, and a caller waiting for that slot
                # takes it at once: only a check here sees it.
                _raise_if_stalled(name)
                points = compute_fingerprint(
                    rec.canonical_path, rec.duration_ms, ffmpeg=ffmpeg, cancel_check=cancel_check, retime=retime
                )
            finally:
                _PARALLEL.release()
        except FingerprintStalledError:
            raise
        except FingerprintError:
            if on_failure is not None and not (cancel_check and cancel_check()):
                on_failure()
            raise
        saved = store.set_fingerprint(
            rec.id,
            size=rec.size,
            mtime_ns=rec.mtime_ns,
            window=fingerprint_window(retime),
            start_s=0.0,
            length_s=window_s(rec.duration_ms),
            algorithm=ALGORITHM,
            points=points.tobytes(),
        )
        if not saved:
            logger.debug("{} changed while it was fingerprinted; not cached", os.path.basename(rec.canonical_path))
            return None
        return points


def sweep_fingerprint_cache(
    store: MarkerStore, *, limit: int = MAX_SWEEP_CHECKS, budget_s: float = SWEEP_BUDGET_S
) -> int:
    """Drop cached fingerprints of files gone from disk (a quality upgrade renames a file; a show is deleted).

    Checks the next ``limit`` fingerprinted files, those checked longest ago first, until ``budget_s`` has passed (a slow
    network mount); the next sweep goes on from the first file left unchecked. A file whose folder is missing is kept.
    Its ``files`` row stays either way: the missing-file sweep that runs before this one on the same thread
    (``missing.sweep_missing_files``) marks it missing once the file's disk roots show it gone.

    Args:
        store: The markers store.
        limit: Most files to check.
        budget_s: Time after which no further file is checked.

    Returns:
        How many files' fingerprints were dropped.
    """
    deadline = _monotonic() + budget_s
    folders: dict[str, bool] = {}
    gone: list[FingerprintCheck] = []
    checked_up_to = None
    for check in store.fingerprint_checks(limit):
        if _monotonic() >= deadline:
            break
        # A stalled hard-mounted share blocks these stats, which is why start_fingerprint_sweep runs this on its own
        # thread.
        if gone_from_disk([check.canonical_path], folders):
            gone.append(check)
        checked_up_to = check.file_id
    if checked_up_to is None:
        return 0
    return store.finish_fingerprint_checks(checked_up_to, gone)


def start_fingerprint_sweep(store: MarkerStore, configs: Sequence[ServerConfig] | None = None) -> bool:
    """Start :func:`sweep_fingerprint_cache` on a background thread, after marking the files missing from disk
    (``missing.sweep_missing_files``) when the servers' configs are given; the caller never waits for it.

    Nothing starts while a sweep is running, or within ``SWEEP_MIN_GAP_S`` of the last start. A sweep still running
    after ``SWEEP_STUCK_S`` is logged as a warning when another is skipped for it, at most once per ``SWEEP_STUCK_S``.
    Never raises.

    Args:
        store: The markers store.
        configs: The servers' configs, for the disk roots the missing-file sweep checks first; None skips it.

    Returns:
        True when a sweep was started.
    """
    global _sweep_started_at, _stuck_warned_at
    now = _monotonic()
    if not _SWEEP_LOCK.acquire(blocking=False):
        started = _sweep_started_at
        stuck = started is not None and now - started > SWEEP_STUCK_S
        if stuck and (_stuck_warned_at is None or now - _stuck_warned_at >= SWEEP_STUCK_S):
            _stuck_warned_at = now
            logger.warning(
                "Skipped clearing old audio fingerprints: the fingerprint cleanup is still waiting on the file system "
                "(started {} min ago)",
                int((now - started) // 60),
            )
        return False
    if _sweep_started_at is not None and now - _sweep_started_at < SWEEP_MIN_GAP_S:
        _SWEEP_LOCK.release()
        return False
    _sweep_started_at = now
    try:
        threading.Thread(
            target=_sweep_in_background, args=(store, configs), name="fingerprint-sweep", daemon=True
        ).start()
    except Exception as exc:
        _SWEEP_LOCK.release()
        logger.warning("Couldn't start clearing old audio fingerprints: {}", exc)
        return False
    return True


def _sweep_in_background(store: MarkerStore, configs: Sequence[ServerConfig] | None) -> None:
    try:
        if configs is not None:
            try:
                sweep_missing_files(store, configs)  # logs how many it marked
            except Exception as exc:
                logger.warning("Couldn't check markers.db for files missing from disk: {}", exc)
        dropped = sweep_fingerprint_cache(store)
        if dropped:
            logger.info("Cleared the cached audio fingerprints of {} file(s) no longer on disk", dropped)
    except Exception as exc:
        logger.warning("Couldn't clear old audio fingerprints from markers.db: {}", exc)
    finally:
        _SWEEP_LOCK.release()
