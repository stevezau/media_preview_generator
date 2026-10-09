"""Progress of one FFmpeg step of an Intro & Credits file, shown on the worker's row as that step's percent, speed and
ETA, and the reader of ffmpeg's ``-progress`` output both it and loudness analysis use."""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from loguru import logger

# Media seconds per wall second are not trusted before this much wall time has passed: the first reading is noise.
_MIN_ELAPSED_S = 0.5

WorkerProgress = Callable[[float, float, float, "str | None", "float | None"], None]
# ``make(total_s, clock)``: a step's StepProgress once the caller knows the window and the clock that leaves paused
# time out; None when nothing is to be reported.
StepFactory = Callable[[float, Callable[[], float]], "StepProgress | None"]


class StepProgress:
    """Turns "this many seconds of the step's window are done" into the worker row's progress callback.

    One per FFmpeg run. The bar is the step's own percent, so the next step starts it over.
    """

    def __init__(
        self, callback: WorkerProgress, *, total_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """
        Args:
            callback: The worker's progress callback ``(percent, done_s, total_s, speed, remaining_s)``.
            total_s: Seconds of media the step covers.
            clock: Wall clock for the computed speed; a paused job's ``Freeze.clock`` keeps paused time out of it.
        """
        self._callback = callback
        self._total_s = total_s
        self._clock = clock
        self._started = clock()
        self._warned = False

    @classmethod
    def maybe(
        cls, callback: WorkerProgress | None, total_s: float | None, *, clock: Callable[[], float] = time.monotonic
    ) -> StepProgress | None:
        """A ``StepProgress``, or None when there is no callback or the step's length is unknown, so callers pass
        ``progress=StepProgress.maybe(...)`` on without a branch."""
        if callback is None or total_s is None or not math.isfinite(total_s) or total_s <= 0:
            return None
        return cls(callback, total_s=total_s, clock=clock)

    def update(self, done_s: float) -> None:
        """Report ``done_s`` seconds done.

        The speed is always this step's own, from its clock: ffmpeg's ``speed=`` counts the time a pause stopped it.

        A failing callback is logged once and swallowed: progress must never fail a file.
        """
        done_s = min(max(done_s, 0.0), self._total_s)
        elapsed = self._clock() - self._started
        speed = done_s / elapsed if elapsed >= _MIN_ELAPSED_S and done_s > 0 else None
        remaining = (self._total_s - done_s) / speed if speed else None
        try:
            self._callback(
                done_s / self._total_s * 100.0,
                done_s,
                self._total_s,
                f"{speed:.1f}x" if speed else None,
                remaining,
            )
        except Exception as exc:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "Intro & Credits progress callback failed (further failures this step not logged): {}", exc
                )


def _parse_progress_seconds(value: str) -> float | None:
    """ffmpeg's ``out_time_us``/``out_time_ms`` (both microseconds) as seconds; None for ``N/A`` or junk."""
    try:
        seconds = int(value) / 1_000_000
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _parse_progress_speed(value: str) -> float | None:
    """ffmpeg's ``speed=1.4x`` as 1.4; None for ``N/A`` or junk."""
    try:
        speed = float(value.strip().removesuffix("x"))
    except ValueError:
        return None
    return speed if math.isfinite(speed) and speed > 0 else None


def read_progress(stream, on_progress: Callable[[float, float | None], None] | None) -> None:
    """Drain ffmpeg's ``-progress`` output to its end, reporting each block (``progress=`` line) once.

    Reading to the end matters even with no callback: an undrained pipe would block ffmpeg.

    Args:
        stream: ffmpeg's stdout (binary).
        on_progress: Called with (seconds of audio processed, speed as a multiple of real time or None).
    """
    seconds: float | None = None
    speed: float | None = None
    warned = False
    for raw in stream:
        key, _, value = raw.decode("utf-8", errors="replace").strip().partition("=")
        if key in ("out_time_us", "out_time_ms"):
            seconds = _parse_progress_seconds(value)
        elif key == "speed":
            speed = _parse_progress_speed(value)
        elif key == "progress" and on_progress is not None and seconds is not None:
            try:
                on_progress(seconds, speed)
            except Exception as exc:
                if not warned:
                    warned = True
                    logger.warning("ffmpeg progress callback failed (further failures this run not logged): {}", exc)
