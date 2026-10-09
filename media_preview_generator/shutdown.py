"""Process-wide "the container is stopping" flag.

A stop signals FFmpeg children together with the app, so a file whose FFmpeg run died with it looks like a failed file.
Once this flag is set, work interrupted by the stop is neither recorded as a failure nor used to complete its job:
the job stays RUNNING in the store and ``JobManager.requeue_interrupted_jobs`` revives it on the next start.
"""

import atexit
import signal
import threading
from types import FrameType

from loguru import logger

# A plain bool: the signal handler sets it, and a handler must not take a lock.
_shutting_down = False


def request_shutdown() -> None:
    """Mark the process as stopping."""
    global _shutting_down
    _shutting_down = True


def is_shutting_down() -> bool:
    """Whether the process has begun stopping."""
    return _shutting_down


def install_shutdown_hooks() -> None:
    """Set the flag on SIGTERM / SIGINT (then run the handler already installed, e.g. gunicorn's) and at exit.

    Signal handlers can only be installed from the main thread; elsewhere only the exit hook is registered.
    """
    atexit.register(request_shutdown)
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous = signal.getsignal(sig)

        def handler(signum: int, frame: FrameType | None, previous=previous) -> None:
            request_shutdown()
            if callable(previous):
                previous(signum, frame)
            elif previous == signal.SIG_DFL:
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)

        try:
            signal.signal(sig, handler)
            # signal.signal() turns off syscall restarting; gunicorn keeps it on for SIGTERM.
            signal.siginterrupt(sig, False)
        except (ValueError, OSError) as exc:
            logger.debug("Could not install the shutdown handler for {}: {}", sig.name, exc)
