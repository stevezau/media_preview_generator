"""Whether the app is shutting down, for code that has to tell a stop from a failure.

When the container stops, the app gets SIGTERM and so does every FFmpeg it started (s6 signals every process). FFmpeg
then exits 255 with "Exiting normally, received signal 15.", which reads as a crash unless the app knows it is going
down.
"""

import atexit
import signal
import threading
from collections.abc import Callable
from types import FrameType

from loguru import logger

_shutting_down = threading.Event()
_installed = False
_install_lock = threading.Lock()


def mark_shutting_down() -> None:
    """Record that the app is shutting down."""
    _shutting_down.set()


def is_shutting_down() -> bool:
    """Whether the app is shutting down.

    Returns:
        True once SIGTERM/SIGINT arrived (``install_signal_handlers``) or the interpreter began to exit.
    """
    return _shutting_down.is_set()


def wait_for_shutdown(timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for the app to start shutting down.

    Args:
        timeout: Longest wait, in seconds.

    Returns:
        True when it is shutting down.
    """
    return _shutting_down.wait(timeout)


def _chained(previous: object) -> Callable[[int, FrameType | None], None]:
    def handler(signum: int, frame: FrameType | None) -> None:
        mark_shutting_down()
        if callable(previous):
            previous(signum, frame)
        elif previous == signal.SIG_DFL:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    return handler


def install_signal_handlers() -> None:
    """Mark the shutdown when SIGTERM or SIGINT arrives, then run the handler that was there before.

    Gunicorn's worker installs its own handlers before it loads the app, so they keep running after this one (for a
    handler left at the default, the signal is raised again with the default action). SIGTERM keeps restarting
    interrupted system calls, as gunicorn sets it up. Only the main thread can install handlers; a second call does
    nothing.
    """
    global _installed
    if threading.current_thread() is not threading.main_thread():
        logger.debug("Shutdown signal handlers not installed: not on the main thread")
        return
    with _install_lock:
        if _installed:
            return
        _installed = True
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, _chained(signal.getsignal(signum)))
    signal.siginterrupt(signal.SIGTERM, False)
    atexit.register(mark_shutting_down)


def reset_for_tests() -> None:
    """Forget a recorded shutdown (tests)."""
    _shutting_down.clear()
