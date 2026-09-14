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
from functools import lru_cache

import numpy as np
from loguru import logger

from ..locks import KeyedLocks
from ..store import FileRecord, MarkerStore, StoredFingerprint

WINDOW = "intro"
ALGORITHM = 1
MAX_WINDOW_S = 900.0
WINDOW_FRACTION = 0.35
FFMPEG_THREADS = 2
MAX_PARALLEL = 2
JELLYFIN_FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
_POLL_S = 0.5
# ffmpeg's wording when a file has no audio stream to fingerprint: a stored empty answer, not a retryable failure.
_NO_AUDIO_HINTS = ("does not contain any stream", "matches no streams", "Output file is empty")
_PARALLEL = threading.BoundedSemaphore(MAX_PARALLEL)
_FILE_LOCKS = KeyedLocks()


class FingerprintError(Exception):
    """ffmpeg couldn't fingerprint the file. Nothing is stored, so the next run tries again."""


def window_s(duration_ms: int) -> float:
    """Seconds fingerprinted from the start: 35% of the file, at most 900 s (spec §5.3)."""
    return min(MAX_WINDOW_S, WINDOW_FRACTION * duration_ms / 1000.0)


@lru_cache(maxsize=8)
def has_chromaprint(ffmpeg: str) -> bool:
    """Whether an ffmpeg binary has the chromaprint muxer (asked once per binary per process)."""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-muxers"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and any(line.split()[1:2] == ["chromaprint"] for line in proc.stdout.splitlines())


def chromaprint_ffmpeg(configured: str | None) -> str | None:
    """The first ffmpeg with chromaprint: the configured one, jellyfin-ffmpeg, then ``ffmpeg`` on PATH."""
    for candidate in dict.fromkeys(c for c in (configured, JELLYFIN_FFMPEG, shutil.which("ffmpeg")) if c):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK) and has_chromaprint(candidate):
            return candidate
    return None


def chromaprint_status(configured: str | None) -> tuple[str | None, str]:
    """The ffmpeg season audio uses and a sentence for Settings.

    Returns:
        ``(path, "")`` when found; ``(None, reason)`` otherwise.
    """
    found = chromaprint_ffmpeg(configured)
    if found:
        return found, ""
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


def ensure_fingerprint(
    store: MarkerStore, rec: FileRecord, *, ffmpeg: str, cancel_check: Callable[[], bool] | None = None
) -> np.ndarray | None:
    """A file's fingerprint from the cache, computing and storing it when missing.

    Two callers asking for one file share one ffmpeg run; at most ``MAX_PARALLEL`` run app-wide.

    Args:
        store: The markers store.
        rec: The file's record; its identity must match the file on disk (callers check).
        ffmpeg: An ffmpeg with chromaprint.
        cancel_check: True once the job is cancelled.

    Returns:
        The points, or None when the file's row changed identity while ffmpeg ran (nothing stored).

    Raises:
        FingerprintError: The file has no known duration, or ffmpeg failed.
    """
    with _FILE_LOCKS.hold(rec.id):
        stored = store.get_fingerprint(rec.id, WINDOW)
        if stored is not None:
            return points_of(stored)
        if not rec.duration_ms:
            raise FingerprintError(f"No known duration for {os.path.basename(rec.canonical_path)}")
        while not _PARALLEL.acquire(timeout=_POLL_S):
            if cancel_check and cancel_check():
                raise FingerprintError(f"Fingerprinting {os.path.basename(rec.canonical_path)} cancelled")
        try:
            points = compute_fingerprint(rec.canonical_path, rec.duration_ms, ffmpeg=ffmpeg, cancel_check=cancel_check)
        finally:
            _PARALLEL.release()
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
