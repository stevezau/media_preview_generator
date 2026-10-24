"""Runs one heavy step on a node for a remote worker: start, poll, log lines, cancel, pause, errors (spec §6, §10.3).

A remote worker is an ordinary worker whose thread carries a :class:`Placement` (:func:`placed_on`). Each of the five
call sites that run a heavy step asks :func:`current_placement` and either calls the local function or hands the same
arguments to ``nodes.remote_ops``, which runs the op here with :func:`run_on_node` and turns its answer back into what
the local function returns.

The placement is a ``ContextVar`` set by the worker thread around its file. Every call site runs on that thread (no
pipeline on these paths hands a call site to a pool or a helper thread), so nothing has to copy the context.
"""

from __future__ import annotations

import io
import os
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, NamedTuple

from loguru import logger

from ..logging_config import per_file_logger
from ..processing.generator import FALLBACK_CODEC, FALLBACK_HWACCEL, CodecNotSupportedError
from ..shutdown import is_shutting_down, request_shutdown
from . import protocol
from .client import (
    MAX_ARTIFACT_BYTES,
    UNREACHABLE_TIMEOUT,
    NodeAuthError,
    NodeCantStartError,
    NodeClient,
    NodeError,
    NodeRateLimitedError,
    NodeRequestError,
    NodeStoppingError,
    NodeTaskDeletedError,
    NodeTimeoutError,
    NodeUnknownTaskError,
    NodeUnreachableError,
)
from .registry import NodeRegistry, get_registry

# How often a running task is polled; a cancel or a pause flip is forwarded within one slice of it.
POLL_INTERVAL_S = 1.0
_SLICE_S = 0.2
# A task's node counts as gone once its polls have failed for this long, and at least MAX_FAILED_POLLS of them in a
# row (spec §6). By time, not count alone: a refused connection fails at once, so three polls would be a 2 s blip.
NODE_GONE_AFTER_S = 12.0
MAX_FAILED_POLLS = 3
# A cancelled task the node still reports running after this long is given up on: the node's own unpolled-task expiry
# stops it.
CANCEL_WAIT_S = 30.0
# Polls the node turns away (429: too many wrong keys from this app's address) never reach the task, so the node drops
# it once none has for its unpolled-task expiry. Past that, with a margin for the node's own reaper, the file goes
# back in the queue.
RATE_LIMITED_GIVE_UP_S = protocol.TASK_UNPOLLED_EXPIRY_S + 12.0
# The read timeout of a task's DELETE, sent from a thread of its own so the file (its result, or its next try) never
# waits for it: a node that stops answering right after the result would hold the file for the full read timeout.
GONE_DELETE_TIMEOUT_S = 3.0
# A DELETE lost to a passing error is sent again after each of these waits, then once the node answers a check again:
# until one arrives the node keeps the task and its temp dir (a preview's frames and their tar) for 10 minutes.
DELETE_RETRY_DELAYS_S = (1.0, 4.0)
# How long a stopping app waits in all for the DELETEs of its tasks still running on nodes (cancel_running_tasks).
STOP_DELETE_BUDGET_S = 1.0
# A preview frame is a 320x240 JPEG (tens of KB; raw RGB would be 225 KB), and a 4-hour film at the 1 s interval
# is 14,400 of them: anything past these is not a preview's frames.
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_FRAMES = 100_000
_LEVELS = frozenset({"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"})

REQUEUE_NODE_UNAVAILABLE = "node_unavailable"
# The node never ran the task (it refused the connection or didn't answer the start): no drop counts against the file.
REQUEUE_NODE_NOT_STARTED = "node_not_started"
REQUEUE_NOT_VISIBLE = "not_visible"
# What the "Started" line logged when a node takes a task calls each op.
_OP_LABELS = {
    protocol.OP_PREVIEW_FRAMES: "preview frames",
    protocol.OP_LOUDNESS_ANALYZE: "loudness analysis",
    protocol.OP_INTRO_FINGERPRINT: "season audio fingerprint",
    protocol.OP_INTRO_END_PICTURE: "end-picture frames",
    protocol.OP_CREDITS_FIND: "credit text",
}


@dataclass(frozen=True)
class Placement:
    """Where a worker's heavy steps run: on this node."""

    node_id: str
    node_name: str


_placement: ContextVar[Placement | None] = ContextVar("node_placement", default=None)


def current_placement() -> Placement | None:
    """The current thread's placement; None runs heavy steps locally."""
    return _placement.get()


@contextmanager
def placed_on(placement: Placement | None) -> Iterator[None]:
    """Run the block's heavy steps on ``placement`` (None: locally).

    Args:
        placement: The node, or None.
    """
    token = _placement.set(placement)
    try:
        yield
    finally:
        _placement.reset(token)


# Set while a step reads a file other than the worker's own (a season sibling, an end-picture partner).
_other_file: ContextVar[bool] = ContextVar("node_other_file", default=False)


@contextmanager
def reading_other_file(other: bool = True) -> Iterator[None]:
    """Run the block's node steps on a file other than the worker's own (when ``other``).

    One the node can't see still raises :class:`FileNotVisibleError`, for the caller to leave out, but isn't reported
    to the registry: another worker may see it fine, and each episode of its season would count it towards pausing
    the node.

    Args:
        other: False runs the block as the worker's own file (for a caller that reads both).
    """
    token = _other_file.set(other)
    try:
        yield
    finally:
        _other_file.reset(token)


class NodeUnavailableError(Exception):
    """The node stopped answering mid-task, no longer knows the task, or couldn't take its start (no answer, or a
    temp folder it can't use). Not the file's failure: it is queued again.

    Attributes:
        node_id: The node.
        node_name: Its name.
        started: False when the node never ran the task (it didn't take the start), stopped it for its own temp
            folder or its own routine stop (a restart), or refused the key mid-task, so the drop doesn't count.
    """

    def __init__(self, node_id: str, node_name: str, message: str, *, started: bool = True) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.node_name = node_name
        self.started = started


class FileNotVisibleError(Exception):
    """The node can't see the file as the app sees it. Not the file's failure: another node (or this app) runs it.

    Attributes:
        node_id: The node.
        node_name: Its name.
        reason: ``protocol.NOT_VISIBLE_*``.
    """

    def __init__(self, node_id: str, node_name: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.node_name = node_name
        self.reason = reason


class NodeTaskError(RuntimeError):
    """The op ran on the node and failed. ``nodes.remote_ops`` raises the local exception it names instead.

    Attributes:
        kind: ``protocol.KIND_*``.
        message: The node's message, as the local exception would carry it.
        error_type: The class name of the exception the op raised on the node ("" when it named none).
        extra: The error's other fields.
    """

    def __init__(self, kind: str, message: str, *, error_type: str = "", extra: dict | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.error_type = error_type
        self.extra = dict(extra or {})


class NodeTaskCancelledError(NodeTaskError):
    """The task was cancelled (the job's cancel, forwarded)."""


class NodeRequeue(NamedTuple):
    """Why a remote worker gave its file back without an outcome; the dispatcher queues it again or fails it."""

    reason: str  # REQUEUE_NODE_UNAVAILABLE, REQUEUE_NODE_NOT_STARTED or REQUEUE_NOT_VISIBLE
    node_id: str
    node_name: str
    message: str


def requeue_for(exc: BaseException) -> NodeRequeue | None:
    """The requeue a worker records for ``exc``; None for any other exception."""
    if isinstance(exc, NodeUnavailableError):
        reason = REQUEUE_NODE_UNAVAILABLE if exc.started else REQUEUE_NODE_NOT_STARTED
        return NodeRequeue(reason, exc.node_id, exc.node_name, str(exc))
    if isinstance(exc, FileNotVisibleError):
        return NodeRequeue(REQUEUE_NOT_VISIBLE, exc.node_id, exc.node_name, str(exc))
    return None


class _BufferFile(io.RawIOBase):
    """A read-only file over a buffer that doesn't copy it (``io.BytesIO`` copies anything but ``bytes``)."""

    def __init__(self, data: bytes | bytearray | memoryview) -> None:
        super().__init__()
        self._view = memoryview(data).cast("B")
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        count = max(0, min(len(buffer), len(self._view) - self._pos))
        buffer[:count] = self._view[self._pos : self._pos + count]
        self._pos += count
        return count

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: len(self._view)}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def tell(self) -> int:
        return self._pos


def extract_artifact(data: bytes | bytearray | memoryview, dest: str) -> list[str]:
    """Unpack a preview's frames tar into ``dest``: plain files with bare names only, every entry checked first.

    Args:
        data: The tar.
        dest: An existing folder.

    Returns:
        The names unpacked, sorted.

    Raises:
        NodeTaskError: The tar is unreadable, holds anything but plain files with bare names, or is far bigger
            than any preview's frames.
    """
    try:
        with tarfile.open(fileobj=_BufferFile(data), mode="r:") as tar:
            members: list[tarfile.TarInfo] = []
            total = 0
            # A sparse member passes isfile() yet unpacks to any size it declares from a few bytes of tar.
            for member in tar:
                name = member.name
                if (
                    not member.isfile()
                    or member.issparse()
                    or not name
                    or name in (".", "..")
                    or "/" in name
                    or "\\" in name
                    or name != os.path.basename(name)
                ):
                    raise NodeTaskError(protocol.KIND_FAILED, f"The node's frames hold an unexpected entry {name!r}")
                total += member.size
                if member.size > MAX_FRAME_BYTES or total > MAX_ARTIFACT_BYTES or len(members) >= MAX_FRAMES:
                    raise NodeTaskError(protocol.KIND_FAILED, "The node's frames are far bigger than a preview's")
                members.append(member)
            tar.extractall(dest, members=members, filter="data")
    except tarfile.TarError as exc:
        raise NodeTaskError(protocol.KIND_FAILED, f"The node's frames couldn't be unpacked: {exc}") from exc
    return sorted(member.name for member in members)


def _device_label(registry: NodeRegistry, node_id: str, gpu: tuple[str, str] | None) -> str:
    """``GPU<n>`` by the device's place in the node's GPU list, or ``CPU``."""
    if gpu is None:
        return "CPU"
    try:
        view = registry.view(node_id)
    except Exception:  # a label must never stop the file
        view = None
    for index, entry in enumerate(view.gpus if view is not None else ()):
        if isinstance(entry, dict) and entry.get("device") == gpu[1]:
            return f"GPU{index}"
    return "GPU"


def placement_label(gpu: tuple[str, str] | None) -> str:
    """``<node>/<GPUn|CPU>`` for the current thread's node, as the job log prefixes that node's lines; "" locally.

    Args:
        gpu: ``(type, device)`` on the node, or None for its CPU.
    """
    placement = current_placement()
    if placement is None:
        return ""
    return f"{placement.node_name}/{_device_label(get_registry(), placement.node_id, gpu)}"


def _start_may_have_reached(exc: NodeError) -> bool:
    """Whether a start that failed with ``exc`` may have reached the node, which may then run the task.

    Not when the connection was never made (the client names why: refused, no such machine, TLS, a connect timeout)
    or the node answered that it took nothing (it refused the key or asked this address to wait, the task id was
    already deleted, its temp folder can't be used, or it is stopping). Anything else may have: no answer in time, a
    dropped connection, a 5xx from a gateway in front of the node.
    """
    if isinstance(
        exc, NodeAuthError | NodeRateLimitedError | NodeTaskDeletedError | NodeCantStartError | NodeStoppingError
    ):
        return False
    return isinstance(exc, NodeTimeoutError) or not (isinstance(exc, NodeUnreachableError) and exc.reason)


def _report(what: str, fn: Callable[..., None], *args: str) -> None:
    """Tell the registry about the node; its failure must not hide the file's own error."""
    try:
        fn(*args)
    except Exception as exc:
        logger.warning("Couldn't report {} to the node registry: {}: {}", what, type(exc).__name__, exc)


class _TaskRun:
    """One task on one node, driven from the worker's thread."""

    def __init__(
        self,
        registry: NodeRegistry,
        client: NodeClient,
        placement: Placement,
        *,
        gpu: tuple[str, str] | None,
        cancel_check: Callable[[], bool] | None,
        pause_check: Callable[[], bool] | None,
        progress_callback: Callable[[dict], None] | None,
        start_id: str = "",
        path_swap: tuple[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._client = client
        self._placement = placement
        self._gpu = gpu
        self._cancel_check = cancel_check
        self._pause_check = pause_check
        self._progress_callback = progress_callback
        self._prefix = f"{placement.node_name}/{_device_label(registry, placement.node_id, gpu)} · "
        self._task_id = ""
        # The id its start asks the node for, so a stopping app can free a task whose start is still on its way.
        self._start_id = start_id
        self._next_log = 0
        self._last_progress: dict | None = None
        self._progress_warned = False
        self._cancel_seen_at: float | None = None
        self._cancel_delivered = False
        self._paused_at: float | None = None
        self._forgotten = False
        # The node refused the key mid-task (recreated with a new one, or its NODE_KEY changed): nothing more is sent
        # to it, since each wrong key from this address brings the node closer to holding it back (429).
        self._key_refused = False
        # (node's path, app's path) of the file: the job log names it by this app's path throughout.
        self._path_swap = path_swap

    def _refused_key(self) -> Exception:
        """The file's end once the node refused the key: queued again with no drop counted (not the file's doing),
        and the node checked now, whose check then reads the refused key for its card."""
        self._key_refused = True
        return self._unavailable(f"{self._placement.node_name} refused the key", started=False)

    def _unavailable(self, message: str, *, started: bool = True) -> Exception:
        _report("an unavailable node", self._registry.report_unavailable, self._placement.node_id)
        if self._cancel_seen_at is not None:
            # The job was cancelled meanwhile: the file ends as cancelled, never queued again.
            return NodeTaskCancelledError(protocol.KIND_CANCELLED, message)
        if is_shutting_down():
            # The stop freed its task (cancel_running_tasks): queued again, it would start on another node, never
            # freed. Ended as cancelled, it isn't recorded and its job is revived on the next start.
            return NodeTaskCancelledError(protocol.KIND_CANCELLED, f"{message}; the app is stopping")
        return NodeUnavailableError(self._placement.node_id, self._placement.node_name, message, started=started)

    def start(self, spec: dict) -> None:
        name = self._placement.node_name
        try:
            self._task_id = self._client.start_task(spec)
        except NodeRequestError as exc:
            raise NodeTaskError(protocol.KIND_REQUEST, f"{name} refused the task: {exc}") from exc
        except NodeError as exc:
            # No answer, NodeCantStartError (its temp folder can't be used) or NodeStoppingError: the file goes back in
            # the queue and the node is checked now, whose /v1/info then names the folder, or says it is stopping, and
            # stops its work.
            if _start_may_have_reached(exc):
                # The node may have started it without the answer reaching us: stop it now rather than leave FFmpeg
                # running there beside the file's next try until the node's unpolled-task expiry.
                self._free_later(spec[protocol.CLIENT_TASK_ID_FIELD])
            raise self._unavailable(f"{name} couldn't start the task: {exc}", started=False) from exc
        # Live, as the node takes it: the step's own lines and progress follow under the same prefix.
        logger.info(
            "{}Started {}: {}", self._prefix, _OP_LABELS.get(spec["op"], spec["op"]), os.path.basename(spec["path"])
        )

    def _cancel_requested(self) -> bool:
        return bool(self._cancel_check and self._cancel_check())

    def _pause_wanted(self) -> bool:
        return bool(self._pause_check and self._pause_check())

    def _forward_controls(self) -> bool:
        """Send the job's cancel, and each pause flip, to the node; a failed send is tried again next slice.

        Returns:
            True when a cancel or a pause flip reached the node.
        """
        if self._cancel_seen_at is None and self._cancel_requested():
            self._cancel_seen_at = time.monotonic()
        if self._key_refused:
            return False
        if self._cancel_seen_at is not None:
            if self._cancel_delivered:
                return False
            try:
                self._client.cancel_task(self._task_id)
            except NodeAuthError:
                self._key_refused = True
                return False
            except NodeError as exc:
                logger.debug("{}cancel not delivered yet: {}", self._prefix, exc)
                return False
            self._cancel_delivered = True
            return True
        if self._pause_check is None:
            return False
        wanted = self._pause_wanted()
        if wanted == (self._paused_at is not None):
            return False
        try:
            if wanted:
                self._client.pause_task(self._task_id)
                self._paused_at = time.monotonic()
            else:
                self._client.resume_task(self._task_id)
                self._add_held(time.monotonic() - (self._paused_at or time.monotonic()))
                self._paused_at = None
        except NodeAuthError:
            self._key_refused = True
            return False
        except NodeError as exc:
            logger.debug("{}pause/resume not delivered yet: {}", self._prefix, exc)
            return False
        return True

    def _add_held(self, seconds: float) -> None:
        # A shared markers Freeze carries the time held, so deadlines the app keeps across steps move out by a pause
        # that happened on the node, as by one held here.
        held = getattr(self._pause_check, "held_s", None)
        if isinstance(held, float):
            self._pause_check.held_s = held + seconds  # type: ignore[union-attr]

    def _controls_changed(self) -> bool:
        if self._cancel_seen_at is not None:
            return not self._cancel_delivered
        if self._cancel_requested():
            return True
        return self._pause_check is not None and (self._pause_wanted() != (self._paused_at is not None))

    def _sleep(self) -> None:
        # A control that reached the node ends the wait so the next poll shows its effect; one that didn't is sent
        # again each slice without polling any faster.
        deadline = time.monotonic() + POLL_INTERVAL_S
        while (left := deadline - time.monotonic()) > 0:
            time.sleep(min(_SLICE_S, left))
            if self._controls_changed() and self._forward_controls():
                return

    def _relay(self, state: dict) -> None:
        for line in state.get("log") or []:
            if not isinstance(line, dict):
                continue
            level = str(line.get("level") or "INFO").upper()
            message = protocol.relayed_text(line.get("message"))
            if self._path_swap:
                message = message.replace(*self._path_swap)
            # A line the node tagged as one file's step-by-step story stays out of the app log here too.
            relay = per_file_logger if line.get("per_file") is True else logger
            relay.log(level if level in _LEVELS else "INFO", "{}{}", self._prefix, message)
        next_log = state.get("next_log")
        if isinstance(next_log, int):
            self._next_log = next_log
        progress = state.get("progress")
        if self._progress_callback is None or not isinstance(progress, dict) or progress == self._last_progress:
            return
        self._last_progress = dict(progress)
        try:
            self._progress_callback(progress)
        except Exception as exc:  # progress must never fail a file
            if not self._progress_warned:
                self._progress_warned = True
                logger.warning("{}progress update failed (later failures not logged): {}", self._prefix, exc)

    def wait(self) -> dict:
        """Poll until the task finishes; its last state."""
        name = self._placement.node_name
        failed_polls = 0
        failing_since = 0.0
        reached_at = time.monotonic()
        held_back = False
        while True:
            self._forward_controls()
            if self._key_refused:
                raise self._refused_key()
            asked_at = time.monotonic()
            try:
                state = self._client.poll_task(self._task_id, log_from=self._next_log)
            except NodeAuthError as exc:
                # Polled again, the refusals from this address soon reached the node's limit, and it held back the
                # app's own address (its check and the right key pasted next included) for a minute or more.
                raise self._refused_key() from exc
            except NodeUnknownTaskError as exc:
                self._forgotten = True
                if held_back and asked_at - reached_at > protocol.TASK_UNPOLLED_EXPIRY_S:
                    # No poll reached the task for the node's unpolled-task expiry (they were held back with 429s),
                    # so the node dropped it for that, not for going away: no drop counts against the file.
                    raise self._unavailable(
                        f"{name} kept asking this app to wait (too many wrong keys came from this app's address), "
                        "so it dropped the task",
                        started=False,
                    ) from exc
                raise self._unavailable(f"{name} no longer knows the task (it restarted?)") from exc
            except NodeStoppingError as exc:
                # A routine stop (a restart, an update) cancelled it: not the file's doing, so no drop is counted.
                raise self._unavailable(f"{name} is restarting", started=False) from exc
            except NodeRateLimitedError as exc:
                # The node answered: it holds back this app's address (wrong keys from something else there), not
                # the task, which keeps running until no poll has reached it for the node's unpolled-task expiry.
                logger.debug("{}poll held back: {}", self._prefix, exc)
                failed_polls = 0
                held_back = True
                if time.monotonic() - reached_at > RATE_LIMITED_GIVE_UP_S:
                    # Neither the node's doing nor the file's, so no drop counts against the file.
                    raise self._unavailable(
                        f"{name} kept asking this app to wait (too many wrong keys came from this app's address), "
                        "so it has dropped the task by now",
                        started=False,
                    ) from exc
            except NodeError as exc:
                if failed_polls == 0:
                    failing_since = asked_at
                failed_polls += 1
                if failed_polls >= MAX_FAILED_POLLS and time.monotonic() - failing_since >= NODE_GONE_AFTER_S:
                    raise self._unavailable(f"{name} stopped answering: {exc}") from exc
                self._sleep()
                continue
            else:
                failed_polls = 0
                held_back = False
                reached_at = asked_at
                self._relay(state)
                if state.get("status") in protocol.FINISHED_STATUSES:
                    return state
            if self._cancel_seen_at is not None and time.monotonic() - self._cancel_seen_at > CANCEL_WAIT_S:
                raise NodeTaskCancelledError(protocol.KIND_CANCELLED, f"{name} didn't stop the cancelled task")
            self._sleep()

    def finish(self, state: dict, app_path: str, artifact_to: str | None) -> dict:
        """The result of a finished task, or the error it ended with."""
        status = state.get("status")
        raw_error = state.get("error")
        error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
        kind = str(error.get("kind") or "")
        message = protocol.relayed_text(str(error.get("message") or kind or status or "failed"))
        if self._path_swap:
            # Shown in the job log and on the worker card, which name the file by this app's path, as _relay does.
            message = message.replace(*self._path_swap)
        node_id, name = self._placement.node_id, self._placement.node_name
        if status == protocol.STATUS_DONE:
            if artifact_to is not None:
                self._download(artifact_to)
            result = state.get("result")
            return result if isinstance(result, dict) else {}
        if status == protocol.STATUS_FILE_NOT_VISIBLE or kind == protocol.KIND_FILE_NOT_VISIBLE:
            reason = protocol.relayed_text(str(error.get("reason") or protocol.NOT_VISIBLE_MISSING))
            # Unmapped is not the node failing: the file belongs to other workers and must not pause this one.
            if not _other_file.get() and reason != protocol.NOT_VISIBLE_UNMAPPED:
                _report("a file the node can't see", self._registry.report_not_visible, node_id, app_path, reason)
            # The job log names the file by this app's path already; the node's message would add its own path.
            plain = protocol.not_visible_text(reason)
            raise FileNotVisibleError(node_id, name, reason, f"{name} can't see the file: {plain}")
        error_type = protocol.relayed_text(str(error.get("error_type") or ""))
        extra = {k: v for k, v in error.items() if k not in ("kind", "message", "error_type")}
        if status == protocol.STATUS_CANCELLED or kind == protocol.KIND_CANCELLED:
            raise NodeTaskCancelledError(protocol.KIND_CANCELLED, message, error_type=error_type, extra=extra)
        fallback_kind = protocol.relayed_text(str(error.get("fallback_kind") or ""))
        if kind == protocol.KIND_CODEC_NOT_SUPPORTED:
            raise CodecNotSupportedError(message, kind=fallback_kind or FALLBACK_CODEC)
        if kind == protocol.KIND_GPU_FAILED:
            device = self._gpu[1] if self._gpu else ""
            _report("a GPU error", self._registry.report_gpu_error, node_id, device)
            if not error_type:
                raise CodecNotSupportedError(message, kind=fallback_kind or FALLBACK_HWACCEL)
        if kind == protocol.KIND_TEMP_FOLDER:
            # The node's temp folder, not the file: as when it can't take a start, the file goes back in the queue
            # with no drop counted and the node is checked now.
            raise self._unavailable(f"{name} couldn't finish the task: {message}", started=False)
        raise NodeTaskError(kind or protocol.KIND_FAILED, message, error_type=error_type, extra=extra)

    def _download_cancelled(self) -> bool:
        return self._cancel_requested() or is_shutting_down()

    def _download(self, dest: str) -> None:
        """Fetch the finished task's frames into ``dest``.

        The node keeps a finished task for minutes, so a fetch lost to an error that came back at once (a 429, a
        dropped connection, a 5xx, a refused connection) is tried again, up to MAX_FAILED_POLLS fetches over
        NODE_GONE_AFTER_S, rather than throwing the finished work away. A fetch that waited out its whole timeout
        (``ARTIFACT_HEADERS_TIMEOUT_S`` with no answer, or ``ARTIFACT_TIMEOUT_S`` with no byte partway through it) isn't tried again (each try
        would wait as long again), nor is one the job's cancel or the app's stop is waiting on. A 429
        given up on is no drop (neither the node's doing nor the file's).
        """
        failed = 0
        failing_since = 0.0
        while True:
            try:
                data = self._client.fetch_artifact(self._task_id, cancel_check=self._download_cancelled)
                break
            except NodeAuthError as exc:
                raise self._refused_key() from exc
            except NodeError as exc:
                if failed == 0:
                    failing_since = time.monotonic()
                failed += 1
                if self._download_cancelled():
                    raise NodeTaskCancelledError(
                        protocol.KIND_CANCELLED, f"{self._placement.node_name} didn't send the frames: {exc}"
                    ) from exc
                passing = (
                    isinstance(exc, NodeUnreachableError)
                    and not isinstance(exc, NodeStoppingError)
                    and exc.reason != UNREACHABLE_TIMEOUT
                )
                if passing and (failed < MAX_FAILED_POLLS or time.monotonic() - failing_since < NODE_GONE_AFTER_S):
                    logger.debug("{}frames not fetched yet: {}", self._prefix, exc)
                    time.sleep(POLL_INTERVAL_S)
                    continue
                raise self._unavailable(
                    f"{self._placement.node_name} didn't send the frames: {exc}",
                    started=not isinstance(exc, NodeStoppingError | NodeRateLimitedError),
                ) from exc
        extract_artifact(data, dest)

    def delete(self) -> None:
        """Free the task on the node, from a short-lived thread.

        A DELETE lost to an error that may pass is sent again there (``DELETE_RETRY_DELAYS_S``). The file's result is
        published, or the file queued again, at once: a node that answers in time stops a task it still runs rather
        than at its unpolled-task expiry; one that doesn't stops it at that expiry.
        """
        if not self._task_id or self._forgotten or self._key_refused:
            return
        self._free_later(self._task_id)

    def _free_later(self, task_id: str) -> None:
        """Send the DELETE of ``task_id`` from a short-lived thread at once, and again after each
        ``DELETE_RETRY_DELAYS_S`` wait while nothing answers it; one still unanswered then is kept for the node's next
        answer to a check (:func:`send_pending_deletes`).

        Args:
            task_id: The node's task id (a start's client task id before the node answered with one).
        """
        threading.Thread(target=self._retry_delete, args=(task_id,), name="node-task-delete", daemon=True).start()

    def _retry_delete(self, task_id: str) -> None:
        for delay in (0.0, *DELETE_RETRY_DELAYS_S):
            time.sleep(delay)
            if not _send_delete(self._client, task_id, self._prefix):
                return
        _keep_pending(self._placement.node_id, task_id)


def _send_delete(client: NodeClient, task_id: str, prefix: str) -> bool:
    """Send one DELETE; True when no answer came from the node, so it may get through if sent again."""
    try:
        client.delete_task(task_id, read_timeout=GONE_DELETE_TIMEOUT_S)
    except NodeUnknownTaskError:
        pass  # The node doesn't have it: the start never reached it, or it already freed the task.
    except NodeUnreachableError as exc:
        logger.debug("{}couldn't free the task: {}", prefix, exc)
        return True
    except NodeError as exc:
        logger.debug("{}couldn't free the task: {}", prefix, exc)
    except Exception:  # noqa: BLE001 - nothing waits on this thread to see it fail
        logger.opt(exception=True).debug("{}freeing the task failed", prefix)
    return False


# DELETEs nothing answered after their retries (node id -> task ids, oldest first), sent again once a check hears the
# node (send_pending_deletes), so an outage longer than the retries doesn't leave the node a finished task's temp dir
# for 10 minutes. At most MAX_PENDING_DELETES per node, the oldest dropped first (the node's own expiry frees those).
# Under _pending_lock, which is taken alone.
MAX_PENDING_DELETES = 256
_pending_deletes: dict[str, dict[str, None]] = {}
_pending_lock = threading.Lock()


def _keep_pending(node_id: str, task_id: str) -> None:
    with _pending_lock:
        pending = _pending_deletes.setdefault(node_id, {})
        pending[task_id] = None
        while len(pending) > MAX_PENDING_DELETES:
            del pending[next(iter(pending))]


def send_pending_deletes(node_id: str, client: NodeClient) -> None:
    """Send the node's DELETEs nothing answered again, from a short-lived thread: a check just heard it answer.

    Args:
        node_id: The node.
        client: Its client now.
    """
    with _pending_lock:
        task_ids = list(_pending_deletes.pop(node_id, {}))
    if task_ids:
        threading.Thread(
            target=_resend_deletes, args=(node_id, client, task_ids), name="node-task-delete", daemon=True
        ).start()


def _resend_deletes(node_id: str, client: NodeClient, task_ids: list[str]) -> None:
    for task_id in task_ids:
        if _send_delete(client, task_id, f"{node_id} · "):
            _keep_pending(node_id, task_id)  # gone again: sent at its next answer


# The tasks running on nodes now, so a stopping app can free them (cancel_running_tasks).
_running: set[_TaskRun] = set()
_running_lock = threading.Lock()


def _free_for_stop(run: _TaskRun, read_timeout: float) -> None:
    try:
        run._client.delete_task(run._task_id or run._start_id, read_timeout=read_timeout)
    except NodeError as exc:
        logger.debug("{}couldn't free the task as the app stops: {}", run._prefix, exc)
    except Exception:  # noqa: BLE001 - nothing waits on this thread to see it fail
        logger.opt(exception=True).debug("{}freeing the task as the app stops failed", run._prefix)


def cancel_running_tasks(budget_s: float = STOP_DELETE_BUDGET_S) -> int:
    """Free every task this app runs on a node, for an app that is stopping.

    Without this a task keeps running on its node until the node's unpolled-task expiry, beside the tasks the job
    revived on the next start gives the node. The process is marked as stopping first, under the lock
    :func:`run_on_node` adds its tasks under: from then on no task starts on a node, and a file whose task was freed
    isn't queued again for another node (``_TaskRun._unavailable``). A task whose start is still on its way is freed
    by the id it asked for. Each DELETE runs on a daemon thread of its own, so a node that doesn't answer holds up none
    of the others; one still unanswered after ``budget_s`` is left to that expiry.

    Args:
        budget_s: The most this waits in all.

    Returns:
        How many tasks it asked nodes to free.
    """
    with _running_lock:
        request_shutdown()
        runs = list(_running)
    if not runs:
        return 0
    logger.info("The app is stopping: freeing {} task(s) running on nodes", len(runs))
    deadline = time.monotonic() + budget_s
    threads = [
        threading.Thread(target=_free_for_stop, args=(run, budget_s), name="node-task-stop", daemon=True)
        for run in runs
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return len(runs)


def run_on_node(
    op: str,
    app_path: str,
    params: dict,
    *,
    gpu: tuple[str, str] | None,
    cancel_check: Callable[[], bool] | None,
    pause_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    artifact_to: str | None = None,
) -> dict:
    """Run ``op`` on the current thread's node and return its result.

    The node's log lines go into this thread's log (so the job's log) at their level, prefixed
    ``<node>/<GPUn|CPU> · ``; the job's cancel and pause are forwarded to the task.

    Args:
        op: ``protocol.OP_*``.
        app_path: The file as the app sees it; the node maps it and checks it is the same file (size + mtime_ns).
        params: The op's plain parameters.
        gpu: ``(type, device)`` on the node, or None for its CPU.
        cancel_check: True once the job is cancelled.
        pause_check: True while the file's work must stop where it is.
        progress_callback: Called with the task's ``progress`` dict (``{"percent", ...}``) each time it changes;
            ``nodes.remote_ops`` turns it into the local function's own callback.
        artifact_to: A folder to unpack the task's artifact into (a preview's frames).

    Returns:
        The op's result.

    Raises:
        NodeUnavailableError: The node didn't start the task (``started`` False), stopped answering, or forgot it.
        FileNotVisibleError: The node can't see the file as the app sees it.
        CodecNotSupportedError: The node's GPU couldn't run the op (it goes to another GPU, or the worker reruns it on
            the node's CPU).
        NodeTaskCancelledError: The job was cancelled, or the app is stopping.
        NodeTaskError: The op failed on the node.
        OSError: The app can't stat ``app_path``.
        RuntimeError: No placement is set.
    """
    placement = current_placement()
    if placement is None:
        raise RuntimeError("run_on_node needs a placement (placed_on)")
    registry = get_registry()
    stat = os.stat(app_path)
    try:
        client = registry.client(placement.node_id)
        mappings = registry.mappings(placement.node_id)
    except KeyError as exc:
        raise NodeUnavailableError(
            placement.node_id, placement.node_name, f"{placement.node_name} is no longer configured", started=False
        ) from exc
    spec: dict[str, Any] = {
        protocol.CLIENT_TASK_ID_FIELD: uuid.uuid4().hex,
        "op": op,
        "path": app_path,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mapping": [m.to_json() for m in mappings],
        "gpu": None if gpu is None else {"type": gpu[0], "device": gpu[1]},
        "params": params,
    }
    node_path = protocol.map_app_path(app_path, mappings)
    if node_path is None:
        # A node's worker never takes a file its folder mappings don't cover: the file goes to another worker.
        reason = protocol.NOT_VISIBLE_UNMAPPED
        raise FileNotVisibleError(
            placement.node_id,
            placement.node_name,
            reason,
            f"{placement.node_name} can't see the file: {protocol.not_visible_text(reason)}",
        )
    run = _TaskRun(
        registry,
        client,
        placement,
        gpu=gpu,
        cancel_check=cancel_check,
        pause_check=pause_check,
        progress_callback=progress_callback,
        start_id=spec[protocol.CLIENT_TASK_ID_FIELD],
        path_swap=(node_path, app_path) if node_path and node_path != app_path else None,
    )
    with _running_lock:
        if is_shutting_down():
            raise NodeTaskCancelledError(
                protocol.KIND_CANCELLED, f"Not started on {placement.node_name}: the app is stopping"
            )
        _running.add(run)
    try:
        run.start(spec)
        return run.finish(run.wait(), app_path, artifact_to)
    finally:
        with _running_lock:
            _running.discard(run)
        run.delete()
