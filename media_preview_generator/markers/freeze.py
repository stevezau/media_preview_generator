"""Freezing a worker's running markers subprocess while everything is paused, as previews freeze their FFmpeg.

Pause all, quiet hours and a job's schedule's stop time stop a running preview's FFmpeg where it is (SIGSTOP) and let it
go on from there on resume (SIGCONT), its stall clock frozen meanwhile (``processing.ffmpeg_runner``). The credits
decodes, the end-picture decodes and the audio fingerprints do the same through :class:`Freeze`, and their time limits
leave the paused time out. A pause of one Intro & Credits job by hand is not such a pause: it gives the job's slot back
and lets the running file finish (``job_runner``), so it never reaches this module.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable

from loguru import logger

POLL_S = 0.2


def _signal(proc: subprocess.Popen, sig: signal.Signals) -> bool:
    """Send ``sig`` to the process's own group (its ffmpeg and anything ffmpeg started), or to the process alone when
    it shares the app's group. Returns False when it has exited."""
    if proc.poll() is not None:
        return False
    with contextlib.suppress(ProcessLookupError, PermissionError):
        pgid = os.getpgid(proc.pid)
        if pgid == proc.pid and pgid != os.getpgrp():
            os.killpg(pgid, sig)
        else:
            proc.send_signal(sig)
        return True
    return False


class Freeze:
    """The pause that stops a running subprocess where it is, and the time it has held the caller.

    One per detector run: the time held adds up across its subprocesses, so a time limit shared by several decodes (the
    credits look-back's) leaves the pause out as each decode's own limit does (:meth:`clock`).
    """

    def __init__(self, paused: Callable[[], bool] | None = None, *, poll_s: float = POLL_S) -> None:
        """Set up the freeze.

        Args:
            paused: True while running work must stop where it is; None: never.
            poll_s: How often a held caller asks again.
        """
        self._paused = paused
        self._poll_s = poll_s
        self.held_s = 0.0

    @classmethod
    def of(cls, check: Callable[[], bool] | Freeze | None) -> Freeze:
        """``check`` itself when it is a :class:`Freeze` (its time held keeps adding up), else a new one around it."""
        return check if isinstance(check, Freeze) else cls(check)

    def __call__(self) -> bool:
        """Whether running work must stop now."""
        return bool(self._paused and self._paused())

    def clock(self) -> float:
        """Monotonic seconds less the time held: a deadline on this clock moves out by every pause."""
        return time.monotonic() - self.held_s

    def hold(
        self,
        proc: subprocess.Popen | None = None,
        *,
        cancel_check: Callable[[], bool] | None = None,
        name: str = "",
    ) -> float:
        """While paused, stop ``proc`` (SIGSTOP), wait for the resume or a cancel, then let it go on (SIGCONT).

        Returns at once when nothing is paused. With no process (before one starts, between files) it only waits, so no
        new subprocess starts during a pause. A cancel lets the process go on too, so the caller's kill reaches a
        running process.

        Args:
            proc: The running subprocess, or None.
            cancel_check: True once the job is cancelled.
            name: The file, for the log.

        Returns:
            Seconds held (0.0 when not paused).
        """
        if not self():
            return 0.0
        started = time.monotonic()
        stopped = proc is not None and _signal(proc, signal.SIGSTOP)
        if stopped:
            logger.info("Paused ffmpeg for {} (PID {}): processing is paused", name, proc.pid)
        try:
            while self() and not (cancel_check and cancel_check()):
                time.sleep(self._poll_s)
        finally:
            if stopped and _signal(proc, signal.SIGCONT):
                logger.info("Resumed ffmpeg for {} (PID {})", name, proc.pid)
            held = time.monotonic() - started
            self.held_s += held
        return held
