"""The node's own log, kept in memory: the last lines node-wide (``GET /v1/log``) and each task's lines.

Both are loguru sinks that run synchronously in the logging thread (no ``enqueue``): a task's line must be in its log
before the op's next statement, or a poll right after a step could miss it.
"""

from __future__ import annotations

import threading
import traceback
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record

NODE_LOG_LINES = 2000
# DEBUG lines from FFmpeg runs come by the hundred per file; neither log keeps them.
CAPTURE_MIN_LEVEL = 20

# ``logger.bind(**{SECRET_EXTRA: True})``: the line goes to the console only, never into a buffer someone can read
# back over the API (the one-time key notice). logging_config.CONSOLE_ONLY is the same name, so app.log and the live
# log viewer drop it too.
SECRET_EXTRA = "node_secret"
# ``logger.bind(**{TASK_LOGGED_EXTRA: True})``: the line is already in its task's log (``TaskContext.log``), so the
# thread capture must not add it a second time.
TASK_LOGGED_EXTRA = "node_task_logged"
# Set (``logger.contextualize``) on lines :func:`held_lines` holds back: no log keeps them, and the code that held them
# logs them again at the level it chooses. logging_config.HELD is the same name.
HELD_EXTRA = "node_held"
# A line that belongs to one file's step-by-step log (the app's own log keeps it only at DEBUG): the task's log carries
# it as ``per_file`` so the app relays it the same way. logging_config.PER_FILE_LOG is the same name, which the node
# server doesn't import (it needs the app's console dependencies).
PER_FILE_EXTRA = "per_file_log"

LineCallback = Callable[[str, str, bool], None]


def _keep(record: Record) -> bool:
    extra = record["extra"]
    return record["level"].no >= CAPTURE_MIN_LEVEL and not extra.get(SECRET_EXTRA) and not extra.get(HELD_EXTRA)


class NodeLogBuffer:
    """The last lines the node logged, oldest first."""

    def __init__(self, maxlen: int = NODE_LOG_LINES) -> None:
        self._lines: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def sink(self, message: Any) -> None:
        record = message.record
        text = record["message"]
        exc = record["exception"]
        if exc is not None and exc.type is not None:
            text += "\n" + "".join(traceback.format_exception(exc.type, exc.value, exc.traceback)).rstrip()
        when = record["time"]
        # ``time`` is the node's own wall clock (its offset included); ``ts`` (epoch seconds) is the same instant with
        # no zone to read, so the app shows it in its own time (protocol §3: added beside ``time``, not replacing it).
        line = {
            "time": when.isoformat(timespec="seconds"),
            "ts": round(when.timestamp(), 3),
            "level": record["level"].name,
            "message": text,
        }
        with self._lock:
            self._lines.append(line)

    def lines(self, count: int) -> list[dict[str, Any]]:
        """The last ``count`` lines, oldest first.

        Args:
            count: How many lines.

        Returns:
            Dicts with ``time`` (ISO 8601 with the node's offset), ``ts`` (epoch seconds), ``level`` and ``message``.
        """
        with self._lock:
            items = list(self._lines)
        return items[-count:] if count > 0 else []


_install_lock = threading.Lock()
_buffer_handler_id: int | None = None


def install() -> NodeLogBuffer:
    """Start a node-wide log buffer, replacing the one a previous ``install`` started in this process.

    Call after the process's logging is set up: the app's ``setup_logging`` removes every handler on its first call.

    Returns:
        The new buffer.
    """
    global _buffer_handler_id
    buffer = NodeLogBuffer()
    with _install_lock:
        if _buffer_handler_id is not None:
            try:
                logger.remove(_buffer_handler_id)
            except ValueError:
                pass
        _buffer_handler_id = logger.add(
            buffer.sink, level=0, format="{message}", filter=_keep, enqueue=False, diagnose=False, catch=True
        )
    return buffer


@contextmanager
def capture_thread(callback: LineCallback) -> Iterator[None]:
    """Send every loguru line the calling thread logs (INFO and up) to ``callback(level, message, per_file)`` while inside.

    Args:
        callback: Receives the level name, the message, and whether the line belongs to one file's step-by-step log
            (``PER_FILE_EXTRA``), which the app's own log keeps only at DEBUG.

    Yields:
        Nothing; capture stops on exit.
    """
    thread_id = threading.get_ident()

    def mine(record: Record) -> bool:
        return record["thread"].id == thread_id and _keep(record) and not record["extra"].get(TASK_LOGGED_EXTRA)

    handler_id = logger.add(
        lambda message: callback(
            message.record["level"].name,
            message.record["message"],
            bool(message.record["extra"].get(PER_FILE_EXTRA)),
        ),
        level=0,
        format="{message}",
        filter=mine,
        enqueue=False,
        diagnose=False,
        catch=True,
    )
    try:
        yield
    finally:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass


@contextmanager
def held_lines(into: list[tuple[str, str]]) -> Iterator[None]:
    """Hold back every line the calling thread logs while inside: no log gets it, ``into`` does.

    The caller logs them again afterwards, at their own level or a lower one (a periodic GPU re-check that found the
    same GPUs logs its lines at DEBUG).

    Args:
        into: Gets ``(level name, message)`` for each line, in order.
    """
    thread_id = threading.get_ident()

    def held(record: Record) -> bool:
        return record["thread"].id == thread_id and bool(record["extra"].get(HELD_EXTRA))

    handler_id = logger.add(
        lambda message: into.append((message.record["level"].name, message.record["message"])),
        level=0,
        format="{message}",
        filter=held,
        enqueue=False,
        diagnose=False,
        catch=True,
    )
    try:
        with logger.contextualize(**{HELD_EXTRA: True}):
            yield
    finally:
        try:
            logger.remove(handler_id)
        except ValueError:
            pass
