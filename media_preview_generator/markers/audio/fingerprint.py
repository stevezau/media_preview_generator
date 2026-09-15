"""Chromaprint fingerprints of an episode's opening (spec §5.3), cached per file identity in markers.db.

CPU only (there is no GPU chromaprint), ``-threads 2`` and at most two at a time across the whole app (spec §5.6).
Only jellyfin-ffmpeg carries the chromaprint muxer in the image; the arm64 image has none, so season audio is then
unavailable rather than failing every episode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from enum import Enum

import numpy as np
from loguru import logger

from ..locks import KeyedLocks
from ..store import FileRecord, FingerprintCheck, MarkerStore, StoredFingerprint

WINDOW = "intro"
ALGORITHM = 1
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


def fingerprint_command(ffmpeg: str, path: str, length_s: float) -> list[str]:
    """The spec §5.3 command: raw algorithm-1 chromaprint of the first ``length_s`` seconds, stereo."""
    return [
        ffmpeg, "-nostdin", "-v", "error", "-threads", str(FFMPEG_THREADS),
        "-ss", "0", "-t", f"{length_s:.3f}", "-i", path,
        "-vn", "-sn", "-dn", "-ac", "2", "-f", "chromaprint", "-algorithm", str(ALGORITHM), "-fp_format", "raw", "-",
    ]  # fmt: skip


def compute_fingerprint(
    path: str,
    duration_ms: int,
    *,
    ffmpeg: str,
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = 300.0,
) -> np.ndarray:
    """Run ffmpeg and return the fingerprint points.

    Args:
        path: Media file (read only).
        duration_ms: File duration, for the window.
        ffmpeg: An ffmpeg with chromaprint.
        cancel_check: True once the job is cancelled; ffmpeg is killed.
        timeout_s: Hard limit (a hung network mount must not hold a worker).

    Returns:
        uint32 points (little-endian); empty for a file without an audio stream.

    Raises:
        FingerprintError: ffmpeg failed, timed out or the job was cancelled.
    """
    name = os.path.basename(path)
    command = fingerprint_command(ffmpeg, path, window_s(duration_ms))
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            out, err = proc.communicate(timeout=_POLL_S)
            break
        except subprocess.TimeoutExpired:
            cancelled = bool(cancel_check and cancel_check())
            if cancelled or time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                why = "cancelled" if cancelled else f"timed out after {timeout_s:.0f} s"
                raise FingerprintError(f"Fingerprinting {name} {why}") from None
    if proc.returncode != 0:
        text = (err or b"").decode("utf-8", errors="replace")
        if any(hint in text for hint in _NO_AUDIO_HINTS):
            return np.zeros(0, dtype="<u4")
        raise FingerprintError(f"ffmpeg exited {proc.returncode} fingerprinting {name}: {text.strip()[-200:]}")
    usable = len(out) - len(out) % 4
    return np.frombuffer(out[:usable], dtype="<u4").copy()


def points_of(stored: StoredFingerprint) -> np.ndarray:
    """The points of a cached fingerprint."""
    return np.frombuffer(stored.points, dtype="<u4").copy()


def cached_fingerprint(store: MarkerStore, rec: FileRecord) -> StoredFingerprint | None:
    """A file's cached fingerprint, when it was made the way this build makes it (algorithm and window length).

    Args:
        store: The markers store.
        rec: The file's record.

    Returns:
        The fingerprint, or None (never made, or made another way: it is computed again).
    """
    if not rec.duration_ms:
        return None
    return store.get_fingerprint(rec.id, WINDOW, algorithm=ALGORITHM, length_s=window_s(rec.duration_ms))


def has_cached_fingerprint(store: MarkerStore, rec: FileRecord) -> bool:
    """Whether :func:`cached_fingerprint` finds one, without reading its points."""
    return bool(rec.duration_ms) and store.has_fingerprint(
        rec.id, WINDOW, algorithm=ALGORITHM, length_s=window_s(rec.duration_ms)
    )


def ensure_fingerprint(
    store: MarkerStore,
    rec: FileRecord,
    *,
    ffmpeg: str,
    cancel_check: Callable[[], bool] | None = None,
    skip: Callable[[], bool] | None = None,
    on_failure: Callable[[], None] | None = None,
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
        on_failure: Called when ffmpeg fails (not when the job was cancelled), before the lock is released, so a
            ``skip`` of a caller waiting on the lock sees what it records.

    Returns:
        The points, or None when the file's row changed identity while ffmpeg ran (nothing stored).

    Raises:
        FingerprintError: The file has no known duration, or ffmpeg failed.
        FingerprintSkippedError: ``skip`` said not to run ffmpeg.
    """
    with _FILE_LOCKS.hold(rec.id):
        if not rec.duration_ms:
            raise FingerprintError(f"No known duration for {os.path.basename(rec.canonical_path)}")
        stored = cached_fingerprint(store, rec)
        if stored is not None:
            return points_of(stored)
        if skip is not None and skip():
            raise FingerprintSkippedError(f"Not fingerprinting {os.path.basename(rec.canonical_path)} again yet")
        while not _PARALLEL.acquire(timeout=_POLL_S):
            if cancel_check and cancel_check():
                raise FingerprintError(f"Fingerprinting {os.path.basename(rec.canonical_path)} cancelled")
        try:
            try:
                points = compute_fingerprint(
                    rec.canonical_path, rec.duration_ms, ffmpeg=ffmpeg, cancel_check=cancel_check
                )
            finally:
                _PARALLEL.release()
        except FingerprintError:
            if on_failure is not None and not (cancel_check and cancel_check()):
                on_failure()
            raise
        saved = store.set_fingerprint(
            rec.id,
            size=rec.size,
            mtime_ns=rec.mtime_ns,
            window=WINDOW,
            start_s=0.0,
            length_s=window_s(rec.duration_ms),
            algorithm=ALGORITHM,
            points=points.tobytes(),
        )
        if not saved:
            logger.debug("{} changed while it was fingerprinted; not cached", os.path.basename(rec.canonical_path))
            return None
        return points


def _gone_from_disk(path: str, folders: dict[str, bool]) -> bool:
    """Whether a file is gone while its folder is still there (``folders`` caches each folder's answer).

    An unmounted library must never look gone: its folders are missing too. A read that fails with another error (a
    stale network file handle) doesn't count as gone either. A hard-mounted share that stalls makes these calls block
    instead of fail, which is why :func:`start_fingerprint_sweep` runs the sweep on its own thread.
    """
    folder = os.path.dirname(path)
    if folders.get(folder) is False:
        return False
    try:
        os.stat(path)
        return False
    except FileNotFoundError:
        pass
    except OSError:
        return False
    if folder not in folders:
        folders[folder] = os.path.isdir(folder)
    return folders[folder]


def sweep_fingerprint_cache(
    store: MarkerStore, *, limit: int = MAX_SWEEP_CHECKS, budget_s: float = SWEEP_BUDGET_S
) -> int:
    """Drop cached fingerprints of files gone from disk (a quality upgrade renames a file; a show is deleted).

    Checks the next ``limit`` fingerprinted files, those checked longest ago first, until ``budget_s`` has passed (a slow
    network mount); the next sweep goes on from the first file left unchecked. A file whose folder is missing is kept.
    Its ``files`` row stays either way.

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
        if _gone_from_disk(check.canonical_path, folders):
            gone.append(check)
        checked_up_to = check.file_id
    if checked_up_to is None:
        return 0
    return store.finish_fingerprint_checks(checked_up_to, gone)


def start_fingerprint_sweep(store: MarkerStore) -> bool:
    """Start :func:`sweep_fingerprint_cache` on a background thread; the caller never waits for it.

    Nothing starts while a sweep is running, or within ``SWEEP_MIN_GAP_S`` of the last start. A sweep still running
    after ``SWEEP_STUCK_S`` is logged as a warning when another is skipped for it, at most once per ``SWEEP_STUCK_S``.
    Never raises.

    Args:
        store: The markers store.

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
        threading.Thread(target=_sweep_in_background, args=(store,), name="fingerprint-sweep", daemon=True).start()
    except Exception as exc:
        _SWEEP_LOCK.release()
        logger.warning("Couldn't start clearing old audio fingerprints: {}", exc)
        return False
    return True


def _sweep_in_background(store: MarkerStore) -> None:
    try:
        dropped = sweep_fingerprint_cache(store)
        if dropped:
            logger.info("Cleared the cached audio fingerprints of {} file(s) no longer on disk", dropped)
    except Exception as exc:
        logger.warning("Couldn't clear old audio fingerprints from markers.db: {}", exc)
    finally:
        _SWEEP_LOCK.release()
