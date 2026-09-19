"""Centralised logging configuration.

Architecture
------------
:func:`setup_logging` is called **once** at startup (``create_app``) and
again on hot-reload when the user changes the log level in Settings.
Each invocation creates up to three loguru handlers — all at the same
configured level:

1. **Console** (stderr) — what ``docker logs`` shows.
2. **File** (``app.log``, JSONL) — persistent history read by
   ``GET /api/logs/history``.
3. **SocketIO broadcaster** — pushes live messages to the ``/logs``
   namespace so the web log viewer can stream in real time.

Only the handler IDs created by :func:`setup_logging` are tracked; per-job
log sinks added externally are left untouched during hot-reloads.

Standard-library ``logging`` (Flask, werkzeug, APScheduler, python-socketio,
…) is routed into loguru too (``_StdlibToLoguru``), so its lines are masked
and written by the same handlers, at the levels that reached the console
before; a library that keeps its records to itself (urllib3's and requests'
NullHandler) stays quiet as before.

Set ``LOG_FORMAT=json`` (env var) or pass ``log_format="json"`` to emit
structured JSON on stderr for log-aggregation pipelines (ELK / Loki / etc.).
"""

import inspect
import json as _json
import logging  # stdlib logging only to hand libraries' records to loguru; app code must use loguru
import os
import sys
import threading
from typing import Optional

from loguru import logger
from rich.console import Console

from .utils import redact_secrets, redacted_traceback

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

# Handler IDs managed by setup_logging() — only these are removed on
# hot-reload so that per-job log sinks (added externally) are preserved.
_managed_handler_ids: list[int] = []
_initial_setup_done: bool = False
_handler_lock = threading.Lock()

# Levels the SocketIO broadcaster will emit (excludes TRACE / SUCCESS).
_BROADCAST_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

# Numeric level values for filtering in the history API.
LEVEL_ORDER = {
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

_CONSOLE_FORMAT = "<green>{time:YYYY/MM/DD HH:mm:ss}</green> | {level.icon}  - <level>{message}</level>"

# ---------------------------------------------------------------------------
# Shared payload builder
# ---------------------------------------------------------------------------


def _compact_payload(record) -> dict:
    """Build the compact dict shared by the JSONL file handler and the
    SocketIO broadcaster.  Keys match what the log-viewer frontend expects.
    """
    return {
        "ts": record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "level": record["level"].name,
        "msg": record["message"],
        "mod": record["name"].rsplit(".", 1)[-1] if record["name"] else "",
        "func": record["function"] or "",
        "line": record["line"],
    }


# ---------------------------------------------------------------------------
# Loguru helpers
# ---------------------------------------------------------------------------


def _mask_secrets(record) -> None:
    """Loguru patcher: mask tokens in each record's message before any handler (or a job's log) sees it.

    A message built from exception text can carry a server URL with its token (``utils.redact_secrets``). A logged
    exception's own text isn't the message: each managed handler masks its traceback (``_write_stderr``,
    ``_jsonl_format``).
    """
    record["message"] = redact_secrets(record["message"])


def _write_stderr(message: str) -> None:
    """Console sink: the formatted line, traceback included, with secrets masked."""
    sys.stderr.write(redact_secrets(message))


def _jsonl_record_patcher(record) -> bool:
    """Loguru *filter* that pre-serialises a JSONL string, and a logged exception's masked traceback, into ``extra``.

    The file handler's format (``_jsonl_format``) writes them directly.

    Returns:
        True always — level gating is done by loguru's ``level=`` parameter.
    """
    record["extra"]["_jsonl"] = _json.dumps(_compact_payload(record), default=str)
    exception = record.get("exception")
    value = getattr(exception, "value", None)
    record["extra"]["_traceback"] = f"{redacted_traceback(value)}\n" if isinstance(value, BaseException) else ""
    return True


def _jsonl_format(record) -> str:
    """app.log's line format: the JSON line, then a logged exception's traceback with secrets masked.

    A callable format, because with a string one loguru appends its own ``{exception}``, whose text isn't masked.
    """
    return "{extra[_jsonl]}\n{extra[_traceback]}"


def _json_sink(message) -> None:
    """Loguru sink: one JSON object per record on *stderr*.

    Used when ``LOG_FORMAT=json``.  Includes worker-thread context
    (``worker_id``, ``gpu_index``, etc.) when present.
    """
    record = message.record
    payload = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "module": record["module"],
    }
    if record["exception"] is not None:
        payload["exception"] = redact_secrets(str(record["exception"]))
    extra = record.get("extra", {})
    for key in ("worker_id", "worker_type", "gpu_index", "media_title", "item_key"):
        if key in extra and extra[key] is not None:
            payload[key] = extra[key]
    sys.stderr.write(_json.dumps(payload, default=str) + "\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# Standard-library logging (Flask, werkzeug, APScheduler, python-socketio, …)
# ---------------------------------------------------------------------------


class _StdlibToLoguru(logging.Handler):
    """Root handler of the standard library's ``logging``: its records go through loguru, masked and written like the
    app's own lines.

    It stands in for the stdlib's last-resort handler, which printed WARNING and above of a record no handler took, so
    what reaches the logs is what reached them before: it sits at WARNING, and leaves a record to a handler of its own
    logger or a parent below the root (urllib3's and requests' NullHandler, which keep their records quiet).
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        # Set while this thread hands a record to loguru: a sink that logs through ``logging`` itself, on this thread,
        # would otherwise send the record round for ever.
        self._handing_over = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._handing_over, "active", False) or _built_by_loguru(record) or _handled_below_root(record):
            return
        self._handing_over.active = True
        try:
            try:
                level: str | int = logger.level(record.levelname).name
            except ValueError:
                level = record.levelno
            # Name the code that logged (loguru reads its module, function and line), not this handler or logging.
            frame, depth = inspect.currentframe(), 0
            while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
                frame = frame.f_back
                depth += 1
            logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
        except Exception:
            self.handleError(record)
        finally:
            self._handing_over.active = False


def _built_by_loguru(record: logging.LogRecord) -> bool:
    """Whether loguru made the record, for a ``logging.Handler`` added as a loguru sink (the tests' caplog bridge).

    It is a loguru line already: handing it to loguru again would write it twice, and on loguru's writer thread (an
    ``enqueue`` sink) the round would never end. Loguru's standard-handler sink builds each record with its own
    ``extra`` dict as the record's ``extra`` attribute.
    """
    return isinstance(record.__dict__.get("extra"), dict)


def _handled_below_root(record: logging.LogRecord) -> bool:
    """Whether the record's logger, or a parent it propagates to below the root, has a handler of its own."""
    current: logging.Logger | None = logging.getLogger(record.name)
    while current is not None and current is not logging.root:
        if current.handlers:
            return True
        current = current.parent if current.propagate else None
    return False


def _route_stdlib_logging() -> None:
    """Send the standard library's ``logging`` through loguru (``_StdlibToLoguru``); safe to call again.

    python-socketio, python-engineio and Flask give their loggers a console handler of their own when nothing handles
    their records yet (Flask on first use, the others when the app starts): each is taken off, so their lines are
    masked and written once, WARNING and above as the root handler takes them (and not below their own logger's
    level). A handler writing anywhere else (a file, a stream of its own) stays. A logger that doesn't propagate
    (gunicorn's) keeps its handlers and never reaches the root.
    """
    if not any(isinstance(handler, _StdlibToLoguru) for handler in logging.root.handlers):
        logging.root.addHandler(_StdlibToLoguru())
    for candidate in list(logging.root.manager.loggerDict.values()):
        if not isinstance(candidate, logging.Logger) or not candidate.propagate:
            continue
        for handler in list(candidate.handlers):
            # Flask's streams to the request's ``wsgi.errors``, which is sys.stderr outside a request.
            if type(handler) is logging.StreamHandler and handler.stream in (sys.stderr, sys.stdout):
                candidate.removeHandler(handler)


def get_app_log_path() -> str:
    """Return the absolute path to the structured ``app.log`` file."""
    log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
    return os.path.join(log_dir, "app.log")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def setup_logging(
    log_level: str = "INFO",
    console: Console | None = None,
    log_format: str | None = None,
    rotation: str = "10 MB",
    retention: int = 5,
) -> None:
    """Create (or replace) the managed loguru handlers.

    Called once at startup from ``create_app()`` and again whenever the
    user changes log settings via the web UI.

    Args:
        log_level: Minimum level for all three handlers.
        console: Rich Console for coordinated output (ignored when
            *log_format* is ``"json"``).
        log_format: ``"json"`` for structured JSON, ``"pretty"`` (default)
            for coloured output.  Falls back to ``LOG_FORMAT`` env var.
        rotation: Max file size before rotating (e.g. ``"10 MB"``).
        retention: Rotated files to keep (int) or duration (``"30 days"``).
    """
    if log_format is None:
        log_format = os.environ.get("LOG_FORMAT", "pretty").lower()

    global _managed_handler_ids, _initial_setup_done

    with _handler_lock:
        # On first call, strip loguru's default handler.
        # On subsequent calls, remove only *our* handlers so that per-job
        # log sinks added elsewhere are preserved.
        if not _initial_setup_done:
            logger.remove()
            _initial_setup_done = True
        else:
            for hid in _managed_handler_ids:
                try:
                    logger.remove(hid)
                except (ValueError, TypeError):
                    pass

        _managed_handler_ids = []
        logger.configure(patcher=_mask_secrets)

        # Every handler below sets diagnose=False: loguru's default prints each traceback frame's variable values
        # (a server's token among them) into docker logs, app.log and the live log viewer. The traceback stays.

        # --- 1. Console (stderr) handler ---
        if log_format == "json":
            hid = logger.add(
                _json_sink,
                level=log_level,
                format="{message}",
                enqueue=True,
                diagnose=False,
            )
        elif console:
            hid = logger.add(
                lambda msg: console.print(redact_secrets(msg), end=""),
                level=log_level,
                format=_CONSOLE_FORMAT,
                enqueue=True,
                diagnose=False,
            )
        else:
            hid = logger.add(
                _write_stderr,
                level=log_level,
                format=_CONSOLE_FORMAT,
                colorize=True,
                enqueue=True,
                diagnose=False,
            )
        _managed_handler_ids.append(hid)

        # --- 2. Persistent JSONL file (app.log) ---
        log_dir = os.path.join(os.environ.get("CONFIG_DIR", "/config"), "logs")
        try:
            os.makedirs(log_dir, exist_ok=True)
            hid = logger.add(
                os.path.join(log_dir, "app.log"),
                level=log_level,
                format=_jsonl_format,
                filter=_jsonl_record_patcher,
                rotation=rotation,
                retention=retention,
                compression="gz",
                enqueue=True,
                diagnose=False,
            )
            _managed_handler_ids.append(hid)
        except (PermissionError, OSError) as exc:
            logger.warning(
                "Could not create the persistent log file at {}: {}. "
                "Logging continues to the console and the live web log viewer, but log history won't survive restarts. "
                "Usually a permissions problem — make sure the user running this app can write to that folder "
                "(check PUID/PGID and the volume mount).",
                os.path.join(log_dir, "app.log"),
                exc,
            )

        # --- 3. SocketIO broadcaster (live log viewer) ---
        broadcaster = get_log_broadcaster()
        if broadcaster is not None:
            hid = logger.add(
                broadcaster.sink,
                level=log_level,
                format="{message}",
                enqueue=True,
                diagnose=False,
            )
            _managed_handler_ids.append(hid)

        # --- 4. Standard-library logging through the handlers above ---
        _route_stdlib_logging()


# ---------------------------------------------------------------------------
# SocketIO live-log broadcaster
# ---------------------------------------------------------------------------

_broadcaster: Optional["SocketIOLogBroadcaster"] = None


def get_log_broadcaster() -> Optional["SocketIOLogBroadcaster"]:
    """Return the registered broadcaster, or *None*."""
    return _broadcaster


def set_log_broadcaster(b: "SocketIOLogBroadcaster") -> None:
    """Register the broadcaster (called once during ``create_app``)."""
    global _broadcaster
    _broadcaster = b


class SocketIOLogBroadcaster:
    """Loguru sink that pushes log records to SocketIO ``/logs`` clients.

    Each connected client joins rooms named after log levels
    (``"DEBUG"``, ``"INFO"``, …).  The record is emitted to the room
    matching its level so clients only receive messages at or above
    their chosen threshold.
    """

    def __init__(self, socketio) -> None:
        self._socketio = socketio

    def sink(self, message) -> None:
        """Loguru sink callable — build payload and emit."""
        record = message.record
        level_name = record["level"].name
        if level_name not in _BROADCAST_LEVELS:
            return

        try:
            self._socketio.emit(
                "log_message",
                _compact_payload(record),
                namespace="/logs",
                room=level_name,
            )
        except Exception:
            pass
