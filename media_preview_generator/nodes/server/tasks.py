"""The node's task table: each task is one op run on one file, in its own thread, polled by the app.

A task lives only in this process's memory (the node is served by one process for that reason). The app polls it about once a
second; one nobody has polled for ``unpolled_expiry_s`` is cancelled and its temp dir removed, so an app that
restarted mid-task never leaves FFmpeg running here. A finished task is kept ``finished_keep_s`` after its last poll
so the app can still fetch the artifact, unless the app deletes it first.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from loguru import logger

from ...utils import FFMPEG_LOG_PREFIX, node_temp_root
from ..protocol import (
    CLIENT_TASK_ID_FIELD,
    CLIENT_TASK_ID_PATTERN,
    KIND_CANCELLED,
    KIND_FAILED,
    KIND_FILE_NOT_VISIBLE,
    KIND_GPU_FAILED,
    KIND_TEMP_FOLDER,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_FILE_NOT_VISIBLE,
    STATUS_RUNNING,
    TASK_FINISHED_KEEP_S,
    TASK_UNPOLLED_EXPIRY_S,
    OpError,
    OpFn,
    PathMapping,
)
from . import access, nodelog

TASK_LOG_LINES = 2000
TASK_DIR_PREFIX = "mpg-task-"
REAP_INTERVAL_S = 5.0
# A DELETE for a task id the node doesn't know yet can overtake its start (the app gave up waiting for the
# start's answer): the id is refused for a start this long after, and this many such ids are remembered.
DELETED_ID_KEEP_S = 300.0
DELETED_IDS_MAX = 1024
# How long a stopping node keeps answering, at most, until the app has asked after each task it cancelled: told it
# was a restart, the app sends the file back without counting a drop. The app polls every second. In the image s6
# kills the node 3 s after a stop's SIGTERM (S6_KILL_GRACETIME), cutting this and the server's SHUTDOWN_WAIT_S short
# when a task is slow to stop; a longer grace would slow every stop by as much, as s6 waits all of it.
STOP_TELL_WAIT_S = 3.0
_STOP_TELL_POLL_S = 0.05
_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})

# The app's own "cancelled" exceptions, by module: an op that stops because cancel_check() fired raises one of these
# from deep inside the app's code. Looked up in sys.modules rather than imported, so the table never loads FFmpeg,
# numpy or ONNX code itself (an op that raised one has already loaded its module).
_CANCELLATION_CLASSES = (
    ("media_preview_generator.processing.generator", "CancellationError"),
    ("media_preview_generator.loudness.analyze", "LoudnessCancelled"),
    ("media_preview_generator.markers.credits.frames", "DecodeCancelledError"),
    ("media_preview_generator.markers.credits.textdet_helper", "TextDetCancelledError"),
)


class TaskSpecError(ValueError):
    """The task spec is malformed (answered 400, kind ``request``)."""


class TaskDeletedError(Exception):
    """The app deleted this task id before its start arrived (answered 409, kind ``task_deleted``): nothing runs."""


class TaskStartError(Exception):
    """The node can't take the task (its temp folder is full or can't be written): answered 400, kind ``failed``.
    Nothing ran, so the app gives the file back to the queue and holds the node until its next check."""


class NodeStoppingError(Exception):
    """The node is stopping (answered 503, kind ``node_stopping``): nothing ran, and the app sends the file elsewhere.
    A task started now would outlive the cancel that stopping already sent, and die with the process. A task the
    stop cancelled is answered the same way: the file did nothing wrong."""


# What a write into the temp folder fails with when the folder itself is the problem. Ops write only there (the media
# is opened read-only), so one of these from an op is the temp folder's.
_TEMP_FULL_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})
_TEMP_WRITE_ERRNOS = _TEMP_FULL_ERRNOS | {errno.EROFS}
# The node's user may not write there (PUID/PGID). Reading a video it may not read fails the same way, so one of these
# is the temp folder's only when it names a file in it and the folder refuses a new folder too.
_TEMP_PERMISSION_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
# After a task filled the temp folder, free space that comes back by less than this isn't room made: a new folder's
# block, or other files on a shared disk, come and go by that much.
TEMP_ROOM_MADE_BYTES = 1 << 20


def default_temp_root() -> str:
    """``NODE_TEMP_DIR``, or ``<system temp>/mpg-node``: where FFmpeg's logs go too (``utils.scratch_dir``)."""
    return node_temp_root()


def temp_folder_problem(temp_root: str, exc: OSError) -> str:
    """Why the node can't use its temp folder, from the error a write there raised.

    Args:
        temp_root: The node's temp folder.
        exc: The error.

    Returns:
        A sentence naming the folder and the cause.
    """
    why = exc.strerror or str(exc) or type(exc).__name__
    if exc.errno in _TEMP_FULL_ERRNOS:
        return f"The node's temp folder {temp_root} is full ({why})"
    return f"The node can't write to its temp folder {temp_root} ({why})"


def _free_bytes(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def probe_temp_folder(temp_root: str) -> str | None:
    """Make and remove a folder in the temp root, as each task's start does.

    Args:
        temp_root: The node's temp folder (made first when it is missing, as a start does).

    Returns:
        None when that works, else why it doesn't (:func:`temp_folder_problem`).
    """
    try:
        os.makedirs(temp_root, exist_ok=True)
        probe = tempfile.mkdtemp(prefix=f"{TASK_DIR_PREFIX}probe-", dir=temp_root)
    except OSError as exc:
        return temp_folder_problem(temp_root, exc)
    try:
        os.rmdir(probe)
    except OSError:
        pass  # It has the task prefix, so the next start's leftover sweep removes it.
    return None


def _inside(path: str, folder: str) -> bool:
    real, root = os.path.realpath(path), os.path.realpath(folder)
    return os.path.commonpath([real, root]) == root


def blames_temp_folder(temp_root: str, code: int | None, written: str | None) -> bool:
    """Whether a failed write is the temp folder's problem, so the node hands the file back instead of failing it.

    Shared with the FFmpeg runner, which words its log line by it (``processing/ffmpeg_runner.py``).

    Args:
        temp_root: The node's temp folder.
        code: The write's errno.
        written: The path the write named, when it named one.

    Returns:
        True when the folder filled up, went read-only, or can't be written by the node's user.
    """
    if code not in _TEMP_WRITE_ERRNOS | _TEMP_PERMISSION_ERRNOS:
        return False
    blamed = code in _TEMP_WRITE_ERRNOS if written is None else _inside(written, temp_root)
    # The node's user made the task's folder, so a refusal there is a security policy on one file or FFmpeg's
    # catch-all error (-1) read as EPERM, unless the folder refuses a new folder now too.
    if blamed and code in _TEMP_PERMISSION_ERRNOS:
        blamed = probe_temp_folder(temp_root) is not None
    return blamed


def _is_cancellation(exc: BaseException) -> bool:
    for module_name, class_name in _CANCELLATION_CLASSES:
        module = sys.modules.get(module_name)
        cls = getattr(module, class_name, None) if module is not None else None
        if isinstance(cls, type) and isinstance(exc, cls):
            return True
    return False


def _json_value(value: object) -> object:
    """``value`` as JSON can carry it: lists and dicts kept (the app reads ``args`` and ``fallbacks`` as lists),
    non-finite floats as None, anything else as its text."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list | tuple):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    return str(value)


def optional_int(spec: dict, field: str) -> int | None:
    """``spec[field]`` as a non-negative int, or None when absent.

    Raises:
        TaskSpecError: Present but not a whole number.
    """
    value = spec.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TaskSpecError(f"{field} must be a whole number or null")
    return value


def parse_mappings(raw: object) -> list[PathMapping]:
    """The spec's ``mapping`` rows.

    Raises:
        TaskSpecError: Not a list of ``{"app_prefix", "node_prefix"}`` objects.
    """
    if not isinstance(raw, list):
        raise TaskSpecError("mapping must be a list")
    try:
        return [PathMapping.from_json(row) for row in raw]
    except ValueError as exc:
        raise TaskSpecError(str(exc)) from exc


def _client_task_id(spec: dict) -> str | None:
    value = spec.get(CLIENT_TASK_ID_FIELD)
    if value is None:
        return None
    # It names the task in URLs and its temp dir, so only plain id characters.
    if not isinstance(value, str) or not re.fullmatch(CLIENT_TASK_ID_PATTERN, value):
        raise TaskSpecError(f"{CLIENT_TASK_ID_FIELD} must be 16-64 letters, digits, '-' or '_'")
    return value


def _parse_gpu(raw: object) -> tuple[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or not isinstance(raw.get("device"), str):
        raise TaskSpecError('gpu must be {"type", "device"} or null')
    return raw["type"], raw["device"]


class NodeTask:
    """One task: the :class:`~media_preview_generator.nodes.protocol.TaskContext` its op receives, and its state."""

    def __init__(self, task_id: str, spec: dict, mappings: list[PathMapping], gpu: tuple[str, str] | None,
                 temp_dir: str) -> None:  # fmt: skip
        self.id = task_id
        self.op: str = spec["op"]
        self.app_path: str = spec["path"]
        self.size = optional_int(spec, "size")
        self.mtime_ns = optional_int(spec, "mtime_ns")
        self.params: dict = spec.get("params") or {}
        self.mappings = mappings
        self.gpu = gpu
        self.temp_dir = temp_dir
        # The temp folder's free space before this task's folder was made: what turned out too little if it fills up.
        self.temp_free_at_start: int | None = None
        self.status = STATUS_RUNNING
        self.result: dict | None = None
        self.error: dict | None = None
        self.started = time.monotonic()
        self.finished: float | None = None
        self.last_contact = self.started
        self.discarded = False
        # Cancelled by the node's own stop (TaskTable.close), and whether the app has asked after it since.
        self.stopped_by_node = False
        self.told_stopping = False
        self._progress: dict[str, Any] = {"percent": 0.0}
        self._log: deque[dict[str, Any]] = deque(maxlen=TASK_LOG_LINES)
        self._next_seq = 0
        self._artifact: str | None = None
        self._cancel = threading.Event()
        self._paused = threading.Event()
        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None

    # TaskContext ------------------------------------------------------------------------------------------------

    def cancel_check(self) -> bool:
        return self._cancel.is_set()

    def pause_check(self) -> bool:
        # A cancelled task never reads as paused: an op waiting out a pause would otherwise never see the cancel.
        return self._paused.is_set() and not self._cancel.is_set()

    def progress(self, percent: float, **extra: object) -> None:
        try:
            value = float(percent)
        except (TypeError, ValueError):
            value = 0.0
        update = {str(k): _json_value(v) for k, v in extra.items()}
        with self._lock:
            self._progress = {**self._progress, **update, "percent": value if math.isfinite(value) else 0.0}

    def log(self, level: str, message: str) -> None:
        name = str(level).upper()
        name = name if name in _LEVELS else "INFO"
        self._add_line(name, str(message))
        logger.bind(**{nodelog.TASK_LOGGED_EXTRA: True}).log(name, "[task {}] {}", self.id[:8], message)

    def set_artifact(self, path: str) -> None:
        real = os.path.realpath(path)
        if os.path.commonpath([real, os.path.realpath(self.temp_dir)]) != os.path.realpath(self.temp_dir):
            raise ValueError("an artifact must be inside the task's temp dir")
        with self._lock:
            self._artifact = real

    @contextmanager
    def capture_thread(self) -> Iterator[None]:
        """Route loguru lines from the calling thread into this task's log while inside.

        The op's own thread is captured already; an op that starts threads of its own wraps their bodies in this.
        """
        with nodelog.capture_thread(self._add_line):
            yield

    # Table side -------------------------------------------------------------------------------------------------

    def _add_line(self, level: str, message: str, per_file: bool = False) -> None:
        with self._lock:
            line: dict[str, Any] = {"seq": self._next_seq, "level": level, "message": message}
            if per_file:
                line["per_file"] = True
            self._log.append(line)
            self._next_seq += 1

    def request_cancel(self) -> None:
        self._cancel.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    @property
    def artifact(self) -> str | None:
        with self._lock:
            return self._artifact

    @property
    def is_finished(self) -> bool:
        return self.finished is not None

    def finish(self, status: str, *, result: dict | None = None, error: dict | None = None) -> None:
        with self._lock:
            self.status, self.result, self.error = status, result, error
            self.finished = time.monotonic()

    def to_json(self, log_from: int = 0) -> dict[str, Any]:
        """The protocol's TASK_STATE, with log lines whose ``seq`` is ``log_from`` or later."""
        with self._lock:
            lines = [dict(line) for line in self._log if line["seq"] >= log_from]
            end = self.finished if self.finished is not None else time.monotonic()
            return {
                "id": self.id,
                "op": self.op,
                "status": self.status,
                "progress": dict(self._progress),
                "log": lines,
                "next_log": self._next_seq,
                "result": self.result,
                "error": self.error,
                "seconds": round(end - self.started, 2),
            }


class TaskTable:
    """Starts, tracks and expires tasks.

    Args:
        ops: Op name -> function.
        temp_root: Each task's temp dir is made under it; None on a node without the worker role (it runs no ops).
        unpolled_expiry_s: A running task nobody has contacted for this long is cancelled and dropped.
        finished_keep_s: A finished task is dropped this long after its last contact.
        reap_interval_s: How often the reaper looks.
        on_gpu_failed: Called when a task ends with error kind ``gpu_failed`` (GPU detection re-checks).
    """

    def __init__(
        self,
        ops: Mapping[str, OpFn],
        *,
        temp_root: str | None,
        unpolled_expiry_s: float = TASK_UNPOLLED_EXPIRY_S,
        finished_keep_s: float = TASK_FINISHED_KEEP_S,
        reap_interval_s: float = REAP_INTERVAL_S,
        on_gpu_failed: Callable[[], None] | None = None,
    ) -> None:
        self._ops = dict(ops)
        self.temp_root = temp_root
        self._unpolled_expiry_s = unpolled_expiry_s
        self._finished_keep_s = finished_keep_s
        self._on_gpu_failed = on_gpu_failed
        self._tasks: dict[str, NodeTask] = {}
        # When a task last filled the temp folder, the free space that task started with, and whether another task's
        # files were there too (:meth:`filled_problem` says until when it reads full); the app sends no files meanwhile.
        self._filled: tuple[float, int | None, bool] | None = None
        # Task id -> when a DELETE found no such task (oldest first), so a start arriving after it never runs.
        self._deleted_unknown: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        if temp_root is not None:
            os.makedirs(temp_root, exist_ok=True)
            self._remove_leftovers()
        self._reaper = threading.Thread(
            target=self._reap_loop, args=(reap_interval_s,), name="node-task-reaper", daemon=True
        )
        self._reaper.start()

    @property
    def ops(self) -> list[str]:
        return sorted(self._ops)

    def _remove_leftovers(self) -> None:
        # Tasks live in memory only, so a task dir found at start belongs to a process that died mid-task, and so does
        # an FFmpeg log: a preview's FFmpeg writes it here (utils.scratch_dir), outside the task's own dir.
        root = self.temp_root
        if root is None:
            return
        try:
            names = os.listdir(root)
        except OSError:
            return
        for name in names:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            if name.startswith(TASK_DIR_PREFIX) and os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif name.startswith(FFMPEG_LOG_PREFIX) and name.endswith(".log") and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError as exc:
                    logger.debug("Couldn't remove {} left by an earlier run: {}", name, exc)

    def start(self, spec: object) -> str:
        """Validate a TASK_SPEC and start its op in a thread.

        Args:
            spec: The request body.

        A spec carrying ``client_task_id`` gets that as its id; a repeated start with the same id answers the task
        already there (running or finished) and starts nothing, and a start whose id was deleted before it arrived
        starts nothing either.

        Returns:
            The task id (the op is still running).

        Raises:
            TaskSpecError: The spec is malformed or names an op this node doesn't have.
            TaskDeletedError: The app deleted this id in the last ``DELETED_ID_KEEP_S``.
            TaskStartError: The task's temp folder can't be made.
            NodeStoppingError: :meth:`close` has begun.
        """
        if not isinstance(spec, dict):
            raise TaskSpecError("the task spec must be an object")
        op = spec.get("op")
        if not isinstance(op, str) or op not in self._ops:
            raise TaskSpecError(f"this node has no op {op!r}" if isinstance(op, str) else "op must be a string")
        if not isinstance(spec.get("path"), str) or not spec["path"]:
            raise TaskSpecError("path must be the app's path")
        if spec.get("params") is not None and not isinstance(spec["params"], dict):
            raise TaskSpecError("params must be an object")
        optional_int(spec, "size")
        optional_int(spec, "mtime_ns")
        mappings = parse_mappings(spec.get("mapping"))
        gpu = _parse_gpu(spec.get("gpu"))
        task_id = _client_task_id(spec) or uuid.uuid4().hex
        with self._lock:
            self._refuse_stopping()
            self._refuse_deleted(task_id)
            if task_id in self._tasks:
                return task_id
        temp_dir, free_at_start = self._make_temp_dir(task_id)
        task = NodeTask(task_id, spec, mappings, gpu, temp_dir)
        task.temp_free_at_start = free_at_start
        with self._lock:
            try:
                # Checked again under the lock close() takes to collect the tasks: a task registered here is either
                # in its list (and cancelled) or refused.
                self._refuse_stopping()
                self._refuse_deleted(task_id)  # its DELETE may have come while the temp dir was made
            except (NodeStoppingError, TaskDeletedError):
                shutil.rmtree(temp_dir, ignore_errors=True)
                raise
            if task_id in self._tasks:  # The same start, twice at once: the first one runs.
                shutil.rmtree(temp_dir, ignore_errors=True)
                return task_id
            self._tasks[task_id] = task
            # Daemon: a hung op must not keep the process alive; close() waits for the rest so their FFmpeg is killed.
            # Started under the lock, so close() never finds a thread it can't join yet.
            task.thread = threading.Thread(target=self._run, args=(task,), name=f"node-task-{task_id[:8]}", daemon=True)
            task.thread.start()
        return task_id

    def _make_temp_dir(self, task_id: str) -> tuple[str, int | None]:
        """The task's own temp dir, the temp root made again first (a host's tmp cleaner may have removed it).

        Returns:
            The dir, and the temp folder's free space just before it was made (None when that can't be read).

        Raises:
            TaskStartError: The folder can't be made (a full disk, no permission), or the node has no temp folder.
        """
        if self.temp_root is None:
            raise TaskStartError("This node has no temp folder: it runs no tasks")
        try:
            os.makedirs(self.temp_root, exist_ok=True)
            free = _free_bytes(self.temp_root)
            return tempfile.mkdtemp(prefix=f"{TASK_DIR_PREFIX}{task_id[:8]}-", dir=self.temp_root), free
        except OSError as exc:
            message = temp_folder_problem(self.temp_root, exc)
            logger.warning("Task {} not started: {}", task_id[:8], message)
            raise TaskStartError(message) from exc

    def _temp_folder_failure(self, exc: BaseException) -> tuple[str, bool] | None:
        """Why an op stopped, when the temp folder filled up, went read-only or can't be written by the node's user, and
        whether it filled up; else None."""
        seen: BaseException | None = exc
        while seen is not None and self.temp_root is not None:
            if isinstance(seen, OSError):
                written = seen.filename if isinstance(seen.filename, str) else None
                if blames_temp_folder(self.temp_root, seen.errno, written):
                    message = temp_folder_problem(self.temp_root, seen)
                    filled = seen.errno in _TEMP_FULL_ERRNOS
                    if filled:
                        # The preview op holds its JPEGs and their tar there at once.
                        message += "; a preview needs about twice its frames' size there"
                    return message, filled
            seen = seen.__cause__
        return None

    def filled_problem(self) -> str | None:
        """The temp folder's problem after a task filled it up, while it lasts; None when no task did.

        Once the task's folder is removed the folder has room again, and a fresh check of it finds nothing wrong, so
        without this the app would send the next big file straight back to fill it. A task that filled the folder by
        itself keeps it reading full until there is more room than that task started with. One that filled it beside
        other tasks' files only shows the folder is too small for them all at once: it reads full until no task runs
        and that room is back (until no task runs, when the room can't be read). A task started after the fill that
        finishes clears it too.

        Returns:
            A sentence naming the folder and the free space that turned out too little, or None.
        """
        with self._lock:
            filled = self._filled
        if filled is None or self.temp_root is None:
            return None
        _started, free_then, shared = filled
        free_now = _free_bytes(self.temp_root)
        if free_then is None or free_now is None:
            cleared = self.busy() == 0
        elif shared:
            cleared = self.busy() == 0 and free_now >= free_then - TEMP_ROOM_MADE_BYTES
        else:
            cleared = free_now > free_then + TEMP_ROOM_MADE_BYTES
        if cleared:
            with self._lock:
                if self._filled == filled:
                    self._filled = None
            return None
        room = f" ({free_then / (1 << 20):,.0f} MB free)" if free_then is not None else ""
        return f"The node's temp folder {self.temp_root} filled up while working on a file{room}"

    def _run(self, task: NodeTask) -> None:
        try:
            with task.capture_thread():
                self._run_op(task)
        except BaseException as exc:
            logger.opt(exception=exc).error("Task {} ({}) crashed the node's task runner", task.id[:8], task.op)
            raise
        finally:
            if not task.is_finished:
                task.finish(STATUS_FAILED, error={"kind": KIND_FAILED, "message": "The node's task runner failed"})
            with self._lock:
                discarded = task.discarded
            if discarded:
                shutil.rmtree(task.temp_dir, ignore_errors=True)
            if task.error and task.error.get("kind") == KIND_GPU_FAILED and self._on_gpu_failed is not None:
                try:
                    self._on_gpu_failed()
                except Exception:  # noqa: BLE001 - a re-check failing must not take the task thread's report with it
                    logger.opt(exception=True).warning("Couldn't schedule a GPU re-check after task {}", task.id[:8])

    def _run_op(self, task: NodeTask) -> None:
        quiet = logger.bind(**{nodelog.TASK_LOGGED_EXTRA: True})
        try:
            node_path = access.resolve(task.app_path, task.size, task.mtime_ns, task.mappings)
        except access.NotVisible as exc:
            quiet.info("Task {} ({}): file not visible: {}", task.id[:8], task.op, exc.message)
            task.finish(
                STATUS_FILE_NOT_VISIBLE,
                error={"kind": KIND_FILE_NOT_VISIBLE, "reason": exc.reason, "message": exc.message},
            )
            return
        quiet.info("Task {} ({}) started on {}", task.id[:8], task.op, node_path)
        try:
            result = self._ops[task.op](task, node_path, task.params)
        except OpError as exc:
            if exc.kind == KIND_CANCELLED:
                task.finish(STATUS_CANCELLED, error=exc.to_json())
            else:
                quiet.info("Task {} ({}) ended: {}: {}", task.id[:8], task.op, exc.kind, exc.message)
                task.finish(STATUS_FAILED, error=exc.to_json())
            return
        except Exception as exc:  # noqa: BLE001 - every failure must end as a task status the app can read
            if task.cancel_check() or _is_cancellation(exc):
                task.finish(STATUS_CANCELLED, error={"kind": KIND_CANCELLED, "message": "Cancelled"})
                return
            temp_failure = self._temp_folder_failure(exc)
            if temp_failure is not None:
                temp_problem, filled = temp_failure
                if filled:
                    with self._lock:
                        # A finished task's files (waiting for the app's DELETE) were there when this one started.
                        shared = any(other is not task and not other.is_finished for other in self._tasks.values())
                        self._filled = (time.monotonic(), task.temp_free_at_start, shared)
                # The node's condition, not a bug: no traceback.
                quiet.warning("Task {} ({}) failed: {}", task.id[:8], task.op, temp_problem)
                task.finish(STATUS_FAILED, error={"kind": KIND_TEMP_FOLDER, "message": temp_problem})
                return
            quiet.opt(exception=exc).error("Task {} ({}) failed", task.id[:8], task.op)
            task.finish(STATUS_FAILED, error={"kind": KIND_FAILED, "message": f"{type(exc).__name__}: {exc}"})
            return
        if task.cancel_check():
            task.finish(STATUS_CANCELLED, error={"kind": KIND_CANCELLED, "message": "Cancelled"})
            return
        try:
            if not isinstance(result, dict):
                raise TypeError(f"the op returned {type(result).__name__}, not a dict")
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            quiet.error("Task {} ({}) returned a result that isn't JSON: {}", task.id[:8], task.op, exc)
            task.finish(STATUS_FAILED, error={"kind": KIND_FAILED, "message": f"{type(exc).__name__}: {exc}"})
            return
        quiet.info("Task {} ({}) done", task.id[:8], task.op)
        with self._lock:
            # A task started after the folder filled up fitted there: it has room for files again.
            if self._filled is not None and task.started > self._filled[0]:
                self._filled = None
        task.finish(STATUS_DONE, result=result)

    def _touch(self, task_id: str) -> NodeTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                task.last_contact = time.monotonic()
            return task

    def _answerable(self, task_id: str) -> NodeTask | None:
        """The task, counting as contact; the node's stop cancelled it when this raises.

        Raises:
            NodeStoppingError: The node's own stop cancelled the task (the app is told so, once is enough).
        """
        task = self._touch(task_id)
        if task is not None and task.stopped_by_node:
            with self._lock:
                task.told_stopping = True
            raise NodeStoppingError("This node is restarting: it stopped the task")
        return task

    def poll(self, task_id: str, log_from: int = 0) -> dict[str, Any] | None:
        """The task's state (None: unknown), counting as contact.

        Raises:
            NodeStoppingError: The node's own stop cancelled the task.
        """
        task = self._answerable(task_id)
        return task.to_json(log_from) if task is not None else None

    def cancel(self, task_id: str) -> bool:
        """Ask the op to stop. False: unknown task."""
        task = self._touch(task_id)
        if task is None:
            return False
        task.request_cancel()
        return True

    def set_paused(self, task_id: str, paused: bool) -> bool:
        """Pause or resume the task (its op's ``pause_check()``). False: unknown task."""
        task = self._touch(task_id)
        if task is None:
            return False
        task.set_paused(paused)
        return True

    def get(self, task_id: str) -> NodeTask | None:
        """The task (None: unknown), counting as contact.

        Raises:
            NodeStoppingError: The node's own stop cancelled the task (its artifact is gone with it).
        """
        return self._answerable(task_id)

    @property
    def stopping(self) -> bool:
        """Whether :meth:`close` has begun: the node takes no new task."""
        return self._stop.is_set()

    def _refuse_stopping(self) -> None:
        if self._stop.is_set():
            raise NodeStoppingError("This node is stopping")

    def _refuse_deleted(self, task_id: str) -> None:
        # Under the lock. Ids past their time go first; the dict is in deletion order.
        now = time.monotonic()
        while self._deleted_unknown:
            oldest, deleted_at = next(iter(self._deleted_unknown.items()))
            if now - deleted_at < DELETED_ID_KEEP_S:
                break
            del self._deleted_unknown[oldest]
        if task_id in self._deleted_unknown:
            raise TaskDeletedError("The app deleted this task before its start arrived; nothing was started")

    def delete(self, task_id: str) -> bool:
        """Drop the task now: cancel it if it runs, free its temp dir. False: unknown task, whose id is then refused
        for a start for ``DELETED_ID_KEEP_S`` (the DELETE overtook it)."""
        with self._lock:
            task = self._tasks.pop(task_id, None)
            if task is None:
                self._deleted_unknown.pop(task_id, None)
                self._deleted_unknown[task_id] = time.monotonic()
                while len(self._deleted_unknown) > DELETED_IDS_MAX:
                    self._deleted_unknown.popitem(last=False)
        if task is None:
            return False
        self._discard(task)
        return True

    def __contains__(self, task_id: object) -> bool:
        """Whether the table knows the task, without counting as contact."""
        with self._lock:
            return task_id in self._tasks

    def busy(self) -> int:
        """How many tasks are running."""
        with self._lock:
            return sum(1 for task in self._tasks.values() if not task.is_finished)

    def _discard(self, task: NodeTask) -> None:
        # A running op may still be writing into its temp dir; its own thread removes the dir once it returns.
        with self._lock:
            task.discarded = True
            finished = task.is_finished
        task.request_cancel()
        if finished:
            shutil.rmtree(task.temp_dir, ignore_errors=True)

    def reap(self) -> None:
        """One pass: drop running tasks nobody polls and finished tasks past their keep time."""
        now = time.monotonic()
        expired: list[tuple[NodeTask, str]] = []
        with self._lock:
            for task_id, task in list(self._tasks.items()):
                if task.is_finished:
                    if now - max(task.last_contact, task.finished or 0.0) >= self._finished_keep_s:
                        expired.append((self._tasks.pop(task_id), "kept long enough after its last poll"))
                elif now - task.last_contact >= self._unpolled_expiry_s:
                    expired.append((self._tasks.pop(task_id), "nobody polled it, so it was cancelled"))
        for task, why in expired:
            logger.info("Task {} ({}) removed: {}", task.id[:8], task.op, why)
            self._discard(task)

    def _reap_loop(self, interval_s: float) -> None:
        while not self._stop.wait(interval_s):
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - the reaper must outlive one bad pass
                logger.opt(exception=True).warning("The node's task reaper failed one pass")

    def close(self, wait_s: float = 0.0) -> None:
        """Stop the reaper and stop every task: running ones are cancelled, which stops their FFmpeg.

        The tasks stay in the table, answered as stopped by the node (``NodeStoppingError``) rather than unknown: an
        unknown task reads to the app as a node that dropped the file, and two of those fail it.

        Args:
            wait_s: How long to wait, in all, for the cancelled ops to return (a node shutting down waits, so no FFmpeg
                outlives it and no task dir is left behind; tests don't).
        """
        self._stop.set()
        with self._lock:
            tasks = list(self._tasks.values())
            for task in tasks:
                task.stopped_by_node = True
        for task in tasks:
            self._discard(task)
        deadline = time.monotonic() + wait_s
        for task in tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if task.thread is not None and task.thread is not threading.current_thread():
                task.thread.join(remaining)
        running = [task for task in tasks if task.thread is not None and task.thread.is_alive()]
        if wait_s > 0 and running:
            logger.warning(
                "{} task(s) still running after {:.0f} s of stopping: {}",
                len(running),
                wait_s,
                ", ".join(f"{task.id[:8]} ({task.op})" for task in running),
            )

    def wait_until_told(self, timeout_s: float = STOP_TELL_WAIT_S) -> None:
        """After :meth:`close`: wait until the app has asked after every task the stop cancelled (or deleted it).

        A node that exits first is asked again only once it is back, which then doesn't know the task: the app counts
        that as the node dropping the file.

        Args:
            timeout_s: The most this waits.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if not any(task.stopped_by_node and not task.told_stopping for task in self._tasks.values()):
                    return
            time.sleep(_STOP_TELL_POLL_S)
