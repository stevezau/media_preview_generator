"""Worker classes for processing media items using threading.

Provides Worker and WorkerPool classes that use threading instead of
multiprocessing for better simplicity and performance with FFmpeg tasks.
"""

import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from typing import Any, Optional, cast

from loguru import logger

from ..config import Config
from ..job_kinds import ItemOutcome, normalize_outcome, outcome_value
from ..markers.credits.textdet_helper import CREDITS_KEPT_ON_CPU_PREFIX, CREDITS_ON_CPU_PREFIX
from ..nodes.executor import FileNotVisibleError, NodeRequeue, NodeUnavailableError, Placement, placed_on, requeue_for
from ..processing.generator import (
    FALLBACK_CODEC,
    FALLBACK_IO_ERROR,
    FALLBACK_STALL,
    GPU_LACKS_FORMAT,
    CancellationError,
    CodecNotSupportedError,
    ProcessingResult,
    _notify_file_result,
    failure_scope,
    gpu_fallback_advice,
)
from ..processing.types import ProcessableItem
from ..utils import format_display_title, redact_secrets, redacted_traceback
from .gpu_fallback import get_gpu_fallback_tracker
from .node_members import (
    EMPTY_NODE_HARDWARE,
    NODE_GPU_FFMPEG_THREADS,
    NodeHardware,
    NodeMemberHardware,
    checked_ready_since_drop,
)
from .worker_naming import friendly_device_label

# Map thread_id -> job_id. Each job's log handler filters on its own id, so concurrent jobs
# don't pick up each other's worker logs.
_job_thread_to_job_id: dict[int, str] = {}
_job_thread_ids_lock = threading.Lock()
# Loguru ``extra`` key: a record bound with it stays out of a job's own log (the app log still has it) — a traceback,
# say, that belongs in the log file but would bury the job's Logs panel. Honoured by both kinds' job logs.
JOB_LOG_SKIP = "job_log_skip"


def register_job_thread(job_id: str = "") -> None:
    """Register the current thread as belonging to ``job_id``.

    ``job_id`` defaults to "" for back-compat with callers that pre-date the
    multi-job dispatcher; in that mode the thread is registered with no
    owner and ``is_job_thread_for`` will treat it as belonging to no job
    (so a stale registration cannot leak into a real job's log handler).
    """
    with _job_thread_ids_lock:
        _job_thread_to_job_id[cast(int, threading.current_thread().ident)] = job_id


def unregister_job_thread() -> None:
    """Remove only the current thread from job tracking.

    Use this in job ``finally`` blocks so that concurrently-running retry
    jobs keep their own threads registered for log capture.
    """
    with _job_thread_ids_lock:
        _job_thread_to_job_id.pop(cast(int, threading.current_thread().ident), None)


@contextmanager
def job_thread(job_id: str | None) -> Iterator[None]:
    """Register the current thread to ``job_id`` for the block (:func:`register_job_thread`), then unregister it.

    A later thread reusing a finished thread's ident (a webhook timer, a decode reader) mustn't log into the job.

    Args:
        job_id: The job the thread works for.
    """
    register_job_thread(job_id or "")
    try:
        yield
    finally:
        unregister_job_thread()


@contextmanager
def logging_for_job(job_id: str) -> Iterator[None]:
    """Route the current thread's log lines to ``job_id``'s log for the block, then back to wherever they went.

    For a line the dispatcher's own thread writes about one job's file (a file given back by its node).

    Args:
        job_id: The job.
    """
    ident = cast(int, threading.current_thread().ident)
    with _job_thread_ids_lock:
        previous = _job_thread_to_job_id.get(ident)
        _job_thread_to_job_id[ident] = job_id
    try:
        yield
    finally:
        with _job_thread_ids_lock:
            if previous is None:
                _job_thread_to_job_id.pop(ident, None)
            else:
                _job_thread_to_job_id[ident] = previous


def is_job_thread_for(thread_id: int, job_id: str) -> bool:
    """Check whether ``thread_id`` belongs to the specific ``job_id``.

    Returns False when the thread isn't registered at all OR is registered
    to a different job — the caller (a per-job log handler) should NEVER
    capture another concurrent job's worker logs.
    """
    if not job_id:
        return False
    with _job_thread_ids_lock:
        return _job_thread_to_job_id.get(thread_id) == job_id


def resolve_per_item_pin(config, item, registry) -> str | frozenset[str] | None:
    """Decide which server (or set of servers) a dispatch should publish to.

    Shared by the processing path (:meth:`Worker._process_item`) and the
    dispatcher's checking stage so both agree on who publishes — a divergence
    would let the cheap check skip an item whose pinned server is actually
    stale. Precedence:

    1. Caller-supplied ``config.server_id_filter`` always wins (explicit
       "publish to this server only" pin from the job config).
    2. No caller pin + non-Plex originator → scope to that originator's id
       (``item.server_id`` carries the vendor that fired the webhook).
    3. No caller pin + Plex originator → fan out (return None).

    A job that picked libraries on several servers (``config.library_ids_by_server``) has no single pin; its picked
    servers are the allow-list for every item that isn't scoped to its own non-Plex originator (rule 2 still holds),
    so a file shared with an unpicked server isn't published there.
    """
    from ..servers.base import ServerType as _ST

    _config_pin = getattr(config, "server_id_filter", None) or None
    if _config_pin:
        return _config_pin
    picked = getattr(config, "library_ids_by_server", None)
    allow_list = frozenset(picked) if isinstance(picked, dict) and picked else None
    if getattr(item, "server_id", None):
        origin_cfg = registry.get_config(item.server_id) if registry else None
        origin_is_plex = bool(origin_cfg and origin_cfg.type is _ST.PLEX)
        return item.server_id if not origin_is_plex else allow_list
    return allow_list


def resolve_per_item_regenerate(config: Config, item: ProcessableItem) -> bool:
    """Decide whether a dispatch regenerates this item's previews.

    Shared by the processing path and the dispatcher's checking stage, like
    :func:`resolve_per_item_pin`. A Regenerate job regenerates every file,
    except the ones its retry chain already regenerated
    (``config.regenerated_paths``): those only wait on a server indexing them
    or on Plex chapters, so each retry checks them like a normal run instead of
    re-running FFmpeg on them.

    Args:
        config: The job's Config.
        item: The :class:`ProcessableItem` being dispatched.

    Returns:
        True when the item's previews and chapters must be made again.
    """
    if not getattr(config, "regenerate_thumbnails", False):
        return False
    return item.canonical_path not in (getattr(config, "regenerated_paths", None) or ())


def _extracted_frames(ms_result) -> bool:
    """Whether FFmpeg decoded this file for this run: a publisher row that took frames (``frame_source`` "extracted"
    on a status that describes a frame outcome), as opposed to frames from the cache or an output already there.

    Args:
        ms_result: The :class:`MultiServerResult` of the run.

    Returns:
        True when at least one publisher row records extracted frames.
    """
    # Lazy: the orchestrator imports this module at load time.
    from .orchestrator import _FRAME_PROVENANCE_STATUSES

    return any(
        getattr(p.status, "value", str(p.status)) in _FRAME_PROVENANCE_STATUSES
        and (p.frame_source or "extracted") == "extracted"
        for p in getattr(ms_result, "publishers", None) or []
    )


class Worker:
    """Represents a worker thread for processing media items."""

    def __init__(
        self,
        worker_id: int,
        worker_type: str,
        gpu: str | None = None,
        gpu_device: str | None = None,
        gpu_index: int | None = None,
        gpu_name: str | None = None,
        ffmpeg_threads: int | None = None,
        node_id: str | None = None,
        node_name: str | None = None,
    ):
        """Initialize a worker.

        Args:
            worker_id: Unique identifier for this worker.
            worker_type: 'GPU' or 'CPU'.
            gpu: GPU type for acceleration (e.g. 'nvidia', 'amd').
            gpu_device: GPU device path (e.g. '/dev/dri/renderD128').
            gpu_index: Index of the assigned GPU in the pool.
            gpu_name: Human-readable GPU name for display.
            ffmpeg_threads: Per-GPU FFmpeg thread cap (overrides
                config.ffmpeg_threads when set).
            node_id: The node this worker's heavy steps run on (its ``gpu_device`` is the node's device); None runs
                them in this app.
            node_name: The node's name, for job log lines.

        """
        self.worker_id = worker_id
        self.worker_type = worker_type
        self.gpu = gpu
        self.gpu_device = gpu_device
        self.gpu_index = gpu_index
        self.gpu_name = gpu_name
        self.ffmpeg_threads = ffmpeg_threads
        self.display_name = f"{worker_type} {worker_id}"
        self.node_id = node_id
        self.node_name = node_name or node_id or ""
        # Ownership outlives edits: a busy slot never moves to a different group member.
        self.group_id: str | None = None
        self.member_id: str | None = None
        self.policy_id: str | None = None
        self.group_name: str | None = None
        self.group_resource: str = "cpu" if worker_type == "CPU" else f"gpu:{gpu_device}"
        if node_id is not None:
            self.group_resource = f"node:{node_id}:{self.group_resource}"
        self.allowed_job_types: frozenset[str] | None = None

        # Task state
        self.is_busy = False
        self.current_thread: threading.Thread | None = None
        self.current_task = None
        # The item last assigned, kept after the task ends so the dispatcher can queue it again.
        self.current_item = None
        # Set when the last task gave its file back without an outcome (its node dropped, or can't see the file);
        # the dispatcher queues it again or fails it, and the worker's counters say nothing about it.
        self.last_task_requeue: NodeRequeue | None = None
        # Set when the last task's GPU couldn't run its file and another GPU can (the GPU's error): the dispatcher
        # queues it again for a worker on another GPU, and the worker's counters say nothing about it.
        self.last_task_handed_off: CodecNotSupportedError | None = None
        # The GPUs (``group_resource`` -> name) that couldn't run the current file before, this GPU's own error when it
        # was one of them, and the pool's check for a worker on any other GPU that runs the file's kind; all set per
        # task by the dispatcher.
        self.gpus_refused: dict[str, str] = {}
        self.gpu_error: CodecNotSupportedError | None = None
        self.other_gpu_runs: Callable[[Collection[str]], bool] | None = None
        # When the last task's thread was seen finished (time.monotonic()); 0: never.
        self.task_ended_at = 0.0

        # Progress tracking
        self.progress_percent = 0
        self.speed = "0.0x"
        self.current_duration = 0.0
        self.total_duration = 0.0
        self.remaining_time = 0.0  # Remaining time calculated from FFmpeg data
        self.task_title = ""
        self.display_title = ""
        self.media_title = ""
        # Display-only flag, "episode" or "video", derived from the title;
        # drives how the worker card truncates long titles. NOT a real
        # semantic media type — vendor processors derive that from the path.
        self.title_kind = ""
        self.media_file = ""  # Actual file path being processed
        self.library_name = ""
        self.title_max_width = 20
        self.ffmpeg_started = False  # Track if FFmpeg has started outputting progress
        self.start_logged = False  # "Started processing" once per file, though a step-by-step kind restarts progress
        self.task_start_time: float = 0  # Track when task started
        # Free-form sub-phase string emitted by the multi-server
        # processor (e.g. "Resolving item id on EmbyTest…"). Surfaced
        # to the UI so the user sees what the worker is actually doing
        # before FFmpeg starts reporting.
        self.current_phase: str = ""
        self.chapter_progress: dict | None = None

        # FFmpeg data fields for display
        self.frame = 0
        self.fps = 0
        self.q = 0
        self.size = 0
        self.time_str = "00:00:00.00"
        self.bitrate = 0

        # Track verbose logging
        self.last_verbose_log_time = 0

        # Job tracking for multi-job dispatch
        self.current_job_id: str | None = None

        # Pre-task baselines for per-task success/failure detection
        self._pre_task_completed = 0
        self._pre_task_failed = 0
        self._pre_task_outcome_counts: dict = {}

        # Statistics
        self.completed = 0
        self.failed = 0
        self.outcome_counts = {r.value: 0 for r in ProcessingResult}
        # Most recent task's publisher list (one entry per (server,
        # adapter) the file fanned out to). Captured each task; consumed by
        # the dispatcher's _merge_worker_outcome on completion. Reset every
        # task so a stale value from the previous item never bleeds into
        # the next job's publisher chip set.
        self.last_publishers: list[dict] = []
        # Aggregate MultiServerResult message (set on FAILED /
        # NO_FRAMES when no publisher even ran). Used as a fallback
        # reason in record_file_result so the Files panel never shows
        # an empty Details cell on a failed file.
        self.last_ms_message: str = ""

        # Set per task by assign_task: a non-preview kind's process function and its valid outcome keys.
        # None keeps the previews process_canonical_path flow.
        self.process_fn: Callable[..., ItemOutcome] | None = None
        self.outcome_keys: tuple[str, ...] = ()
        # A non-preview kind's own "picked up" line (``KindHandlers.pickup_fn``); None logs the generic one.
        self.pickup_fn: Callable[[Any, str], None] | None = None

        # In-place GPU→CPU fallback state (set during a retry, cleared on next
        # task assignment). Surfaced to the UI so users see why the switch
        # happened.
        self.fallback_active = False
        self.fallback_reason: str | None = None
        # The fallback that counts towards this GPU's "GPU keeps failing" streak (``_blame_gpu_for``), or None.
        self.gpu_blame_reason: str | None = None
        # True once a step of this file went to the CPU for a format the GPU lacks: its GPU didn't decode that part.
        self.gpu_lacked_format = False
        # True once the CPU decodes this file for a GPU worker (its CPU rerun, or a step that fell back to the CPU).
        # Credit text read on the CPU sets fallback_active only: the GPU still decodes the frames it reads.
        self.decoding_on_cpu = False
        # True once the last finished file needed the CPU fallback (cancelled files stay False); the dispatcher
        # folds it into the job's "N files ran on the CPU because the GPU failed" tally.
        self.last_task_cpu_fallback = False
        # The thumbnail interval when the last finished file was fully decoded because its snapshot frames were too
        # far apart (else None); the dispatcher folds it into the job's slow-path tally.
        self.last_task_slow_path_interval: int | None = None

        # Per-worker removal flag set by reconcile_gpu_workers for busy
        # workers that should be retired after completing their current task.
        # Unlike the type-level _pending_removals counter, this is
        # device-aware and avoids retiring workers from the wrong GPU.
        self._pending_removal = False

        # Optional event signalled when _process_item finishes, allowing the
        # dispatch loop to wake immediately instead of polling on a timer.
        # Set by WorkerPool when the pool has a _worker_done_event.
        self._done_event: threading.Event | None = None

    @property
    def node_device(self) -> str:
        """``lab-gpu/GPU0`` for a worker on a node; empty for this app's own workers."""
        if self.node_id is None:
            return ""
        device = "CPU" if self.worker_type != "GPU" else f"GPU{'' if self.gpu_index is None else self.gpu_index}"
        return f"{self.node_name}/{device}"

    @property
    def log_tag(self) -> str:
        """How this worker's own progress lines start: ``[GPU 0]:`` / ``[CPU]:`` here; on a node ``lab-gpu/GPU0 ·``,
        the prefix the node's own lines carry in the job log. A GPU worker reads as the CPU once the CPU decodes its
        file (its CPU rerun, or a step that fell back to the CPU); credit text read on the CPU doesn't change it, as
        the GPU still decodes those frames."""
        on_gpu = self.worker_type == "GPU" and not self.decoding_on_cpu
        if self.node_id is None:
            return f"[GPU {self.gpu_index}]:" if on_gpu else "[CPU]:"
        return f"{self.node_device} ·" if on_gpu else f"{self.node_name}/CPU ·"

    @property
    def placement(self) -> Placement | None:
        """Where this worker's heavy steps run: its node, or None for this app."""
        if self.node_id is None:
            return None
        return Placement(self.node_id, self.node_name)

    def is_available(self) -> bool:
        """Check if this worker is available for a new task."""
        return not self.is_busy and not self._pending_removal

    def _format_gpu_name_for_display(self) -> str:
        """Format GPU name for consistent display width."""
        if not self.gpu_name:
            return f"GPU {self.gpu_index}"

        # If already short enough, pad to 10 characters
        if len(self.gpu_name) <= 10:
            return self.gpu_name.ljust(10)[:10]

        # Dictionary of GPU name patterns and their shortened forms
        # Pattern matching rules: (pattern, replacement or extraction function)
        patterns = [
            (r".*TITAN.*RTX.*", lambda m: "TITAN RTX"),  # TITAN RTX -> "TITAN RTX"
            (
                r".*RTX\s*(\d+).*",
                lambda m: f"RTX{m.group(1)}"[:8],
            ),  # Extract RTX number
            (
                r".*GTX\s*(\d+).*",
                lambda m: f"GTX{m.group(1)}"[:8],
            ),  # Extract GTX number
            (
                r".*GeForce\s+([A-Z0-9\s]+).*",
                lambda m: m.group(1).strip()[:8],
            ),  # Extract GeForce model
            (r".*TITAN.*", lambda m: "TITAN"),  # TITAN (without RTX)
            (r".*Intel.*", lambda m: "Intel"),  # Intel GPUs
            (r".*AMD.*", lambda m: "AMD"),  # AMD GPUs
        ]

        # Try each pattern in order
        for pattern, replacement in patterns:
            match = re.search(pattern, self.gpu_name)
            if match:
                result = replacement(match) if callable(replacement) else replacement
                return result.ljust(10)[:10]

        # Fallback: truncate to 8 characters
        return self.gpu_name[:8].ljust(10)[:10]

    def assign_task(
        self,
        item,
        config: Config,
        registry,
        progress_callback=None,
        title_max_width: int = 20,
        job_id: str | None = None,
        library_name: str = "",
        cancel_check=None,
        pause_check=None,
        process_fn: Callable[..., ItemOutcome] | None = None,
        outcome_keys: tuple[str, ...] | None = None,
        pickup_fn: Callable[[Any, str], None] | None = None,
        gpus_refused: dict[str, str] | None = None,
        gpu_error: CodecNotSupportedError | None = None,
        other_gpu_runs: Callable[[Collection[str]], bool] | None = None,
    ) -> None:
        """Assign a :class:`ProcessableItem` to this worker.

        Worker UI fields (media_title, media_file, library_name) are
        populated from the ProcessableItem so the dashboard's per-worker
        panel shows what each worker is doing.

        Args:
            item: ProcessableItem describing the canonical_path + per-server
                ids the unified ``process_canonical_path`` pipeline needs.
            config: Configuration object passed through to FFmpeg helpers.
            registry: Live :class:`ServerRegistry` — publishers fan out via
                this.
            progress_callback: Optional callback for progress updates.
            title_max_width: Maximum width for title display.
            job_id: Optional job identifier for multi-job dispatch routing.
            library_name: Library name the item belongs to.
            cancel_check: Optional callable returning True when job is
                cancelled.
            pause_check: Optional callable returning True while the job is
                paused.
            process_fn: Non-preview kind's per-item function; None runs the
                previews ``process_canonical_path`` flow.
            outcome_keys: Valid outcome keys for ``process_fn``'s kind.
            pickup_fn: ``process_fn``'s kind's own "picked up" line
                (``KindHandlers.pickup_fn``); None logs the generic one.
            gpus_refused: The GPUs (``group_resource`` -> name) that couldn't
                run this file before.
            gpu_error: This GPU's error when it couldn't run this file before:
                the file goes straight to the CPU rerun (or another GPU).
            other_gpu_runs: Whether a worker on a GPU outside the given
                ``group_resource`` set runs this file's kind; None never hands
                the file to another GPU.
        """
        # Pre-claimed workers (is_busy=True but no current_task yet) are
        # acceptable — _find_available_worker(claim=True) atomically reserves
        # the worker before this call to close the race between dispatcher
        # and per-job consumer threads. Only raise if the worker is mid-task.
        if self.is_busy and self.current_task is not None:
            raise RuntimeError(f"{self.display_name} is already busy")

        self._pre_task_completed = self.completed
        self._pre_task_failed = self.failed
        self._pre_task_outcome_counts = dict(self.outcome_counts)
        # Reset last-task publisher list AND the aggregate message so a
        # previous task's fan-out / failure reason doesn't bleed into
        # this one's chip set or Details cell.
        self.last_publishers = []
        self.last_ms_message = ""
        self.current_phase = ""
        self.chapter_progress = None
        self.last_task_requeue = None
        self.last_task_handed_off = None
        self.gpus_refused = dict(gpus_refused or {})
        self.gpu_error = gpu_error
        self.other_gpu_runs = other_gpu_runs

        self.is_busy = True
        self.current_task = item.canonical_path
        self.current_item = item
        self.current_job_id = job_id
        self.media_title = item.title or item.canonical_path
        # Display-only heuristic: " - S" in the title means episode-style
        # truncation (preserve trailing S01E01); everything else gets
        # movie-style truncation. Has no bearing on processing logic.
        self.title_kind = "episode" if " - S" in self.media_title else "video"
        self.media_file = item.canonical_path
        self.library_name = library_name
        self.title_max_width = title_max_width
        self.display_title = format_display_title(self.media_title, self.title_kind, title_max_width)
        if self.worker_type == "GPU":
            gpu_display = self._format_gpu_name_for_display()
            self.task_title = f"[{gpu_display}]: {self.display_title}"
        else:
            self.task_title = f"[CPU      ]: {self.display_title}"
        self.progress_percent = 0
        self.speed = "0.0x"
        self.current_duration = 0.0
        self.total_duration = 0.0
        self.remaining_time = 0.0
        self.ffmpeg_started = False
        self.start_logged = False
        self.task_start_time = time.time()
        self.fallback_active = False
        self.fallback_reason = None
        self.gpu_blame_reason = None
        self.gpu_lacked_format = False
        self.decoding_on_cpu = False
        self.last_task_cpu_fallback = False
        self.last_task_slow_path_interval = None
        self.cancel_check = cancel_check
        self.pause_check = pause_check
        self.process_fn = process_fn
        self.outcome_keys = tuple(outcome_keys or ())
        self.pickup_fn = pickup_fn

        self.frame = 0
        self.fps = 0
        self.q = 0
        self.size = 0
        self.time_str = "00:00:00.00"
        self.bitrate = 0

        self.last_verbose_log_time = 0

        self.current_thread = threading.Thread(
            target=self._process_item,
            args=(item, config, registry, progress_callback),
            daemon=True,
        )
        self.current_thread.start()

    def _process_item(
        self,
        item,
        config: Config,
        registry,
        progress_callback=None,
    ) -> None:
        """Background-thread target for :meth:`assign_task`.

        Dispatches into ``process_canonical_path`` (the per-vendor publisher
        funnel) and translates its :class:`MultiServerResult` into the
        :class:`ProcessingResult` counters the WorkerPool accounting consumes.
        Handles GPU→CPU fallback on :class:`CodecNotSupportedError`: the file
        goes to another GPU first when one can take it
        (:meth:`_hand_off_to_another_gpu`), else this worker reruns it on its
        CPU in place. A task assigned with ``process_fn`` (a non-preview kind) runs
        :meth:`_process_custom_item` instead.
        """
        if self.process_fn is not None:
            try:
                self._process_custom_item(item, progress_callback)
            finally:
                # Like the dispatcher's check threads: a later thread reusing this ident mustn't log into this job.
                unregister_job_thread()
                if self._done_event is not None:
                    self._done_event.set()
            return

        from ..processing.multi_server import process_canonical_path

        # Lazy: the orchestrator imports this module at load time.
        from .orchestrator import _outcome_for_multi_server_status

        # Register THIS worker thread under THIS job's id so the per-job
        # log handler captures only its own messages — not a sibling job
        # running concurrently.
        with job_thread(self.current_job_id), failure_scope(self.current_job_id), placed_on(self.placement):
            display_name = self.media_file or self.media_title or item.canonical_path

            ctx_logger = logger.bind(
                worker_id=self.worker_id,
                worker_type=self.worker_type,
                gpu_index=self.gpu_index,
                media_title=self.media_title,
                canonical_path=item.canonical_path,
            )
            # "Picked up" rather than "started" — this fires when the worker
            # thread RECEIVES the assignment, before any ownership check, cache
            # check, or FFmpeg invocation. The actual encoding-start log lives
            # at multi_server.py:980 ("FFmpeg start: server=... gpu=..."); this
            # one only signals the worker is now responsible for the item.
            ctx_logger.info("{} picked up: {}", self.display_name, display_name)

            def _phase_cb(text: str) -> None:
                # Single-writer (worker thread) → no lock needed.
                self.current_phase = text or ""

            def _chapter_progress_cb(snapshot: dict | None) -> None:
                # Replace snapshots instead of mutating one while a status reader serializes it.
                self.chapter_progress = dict(snapshot) if snapshot is not None else None

            # Pin precedence (config pin wins → non-Plex originator scopes to
            # itself → Plex originator fans out) lives in resolve_per_item_pin,
            # the single source both this generation path and the dispatcher's
            # checking stage share so they agree on who publishes. A Plex-pinned
            # webhook fanning out would silently write Emby/Jellyfin previews too
            # — see resolve_per_item_pin.
            per_item_pin: str | frozenset[str] | None = resolve_per_item_pin(config, item, registry)

            # ``webhook_source`` lives on Config when this dispatch came
            # from a webhook (Sonarr/Radarr/Plex/…). Use it as the gate
            # for ``display_name``: webhook jobs have ``library_name``
            # set to the cleaned title (e.g. ``"Deadliest Catch S22E01"``);
            # full library scans set it to the section name (e.g.
            # ``"Movies"``) which would mislead the chain row.
            _webhook_source = getattr(config, "webhook_source", None) or None
            _retry_display_name = self.library_name or None if _webhook_source else None

            def _run_once(gpu, gpu_device):
                return process_canonical_path(
                    canonical_path=item.canonical_path,
                    registry=registry,
                    config=config,
                    item_id_by_server=item.item_id_by_server or None,
                    gpu=gpu,
                    gpu_device_path=gpu_device,
                    # This GPU's own thread cap; Config.ffmpeg_threads is the max across GPUs.
                    # The CPU fallback (gpu=None) runs uncapped, like a CPU worker.
                    ffmpeg_threads_override=self.ffmpeg_threads if gpu else None,
                    progress_callback=progress_callback,
                    cancel_check=self.cancel_check,
                    pause_check=self.pause_check,
                    server_id_filter=per_item_pin,
                    regenerate=resolve_per_item_regenerate(config, item),
                    phase_callback=_phase_cb,
                    chapter_progress_callback=_chapter_progress_cb,
                    # Radarr/Sonarr deletedFiles[] from upgrade webhooks.
                    # The job-wide list is forwarded as-is; the cleanup
                    # pass inside process_canonical_path looks at each
                    # path's parent folder so unrelated entries simply
                    # find no matching artifacts and become no-ops.
                    deleted_paths=getattr(config, "webhook_deleted_paths", None) or None,
                    display_name=_retry_display_name,
                    source=_webhook_source,
                    # Identify this dispatch as the "Original" in any
                    # retry chain that gets spawned from a publisher
                    # returning PUBLISHED_PENDING_REGISTRATION. The
                    # chain's modal-dropdown surfaces this UUID as
                    # the first entry ("Original dispatch") so the
                    # user sees the full lifecycle (initial FFmpeg
                    # + Plex/Emby publish, then each retry firing)
                    # from one row.
                    originating_job_id=self.current_job_id,
                )

            # Persist the per-file outcome via the job-runner-installed
            # callback so the Jobs UI can show a per-file row (Files panel
            # under each job). Caller-side cap protects 100k-item scans
            # from bloating the JSONL — see web/jobs.record_file_result.
            # Passes the per-publisher attribution so each file row
            # can show which server(s) it landed on. ``last_ms_message``
            # captures the aggregate MultiServerResult.message — used as
            # a fallback reason when there are no publishers (FFmpeg
            # failed before any publish ran, so reason='' would otherwise
            # show as a blank Details column).
            self.last_ms_message = ""

            def _persist(outcome: ProcessingResult, reason: str = "") -> None:
                effective_reason = reason or self.last_ms_message or ""
                # Defensive: _notify_file_result writes a JSONL row + emits
                # SocketIO. A disk-full or emit-state hiccup must not bubble
                # out of the per-item try/except (which is scoped to FFmpeg
                # failures) and drop the worker out of the dispatch loop.
                # Swallow with a WARNING so the operator sees the persist
                # failure even though this file's row is lost.
                try:
                    _notify_file_result(
                        item.canonical_path,
                        outcome,
                        effective_reason,
                        self.display_name,
                        servers=list(self.last_publishers or []),
                    )
                except Exception as persist_exc:
                    logger.warning(
                        "Failed to persist {} result for {}: {}: {}",
                        outcome.value if hasattr(outcome, "value") else outcome,
                        item.canonical_path,
                        type(persist_exc).__name__,
                        persist_exc,
                    )

            # Capture this task's publisher fan-out for the dispatcher to
            # merge into JobTracker.publishers_by_server. Each entry
            # is a flat dict — not the dataclass — so the dispatcher
            # doesn't need to import processing.multi_server. server_type
            # is looked up from the registry by id so the Files-panel
            # pills get the right colour palette (PublisherResult itself
            # doesn't carry the vendor type).
            def _capture_publishers(ms_result_obj) -> None:
                # Aggregate-level message (set by process_canonical_path on
                # FAILED / NO_FRAMES when no publisher even ran). Surfaced
                # via _persist when no caller-supplied reason exists.
                self.last_ms_message = (ms_result_obj.message or "").strip()
                rows = []
                for p in ms_result_obj.publishers or []:
                    server_type = ""
                    try:
                        cfg = registry.get_config(p.server_id)
                        if cfg is not None:
                            server_type = (
                                cfg.type.value if hasattr(cfg.type, "value") else str(cfg.type or "")
                            ).lower()
                    except Exception:
                        pass
                    rows.append(
                        {
                            "server_id": p.server_id,
                            "server_name": p.server_name,
                            "server_type": server_type,
                            "adapter_name": p.adapter_name,
                            "status": getattr(p.status, "value", str(p.status)),
                            "message": p.message or "",
                            "frame_source": p.frame_source or "extracted",
                            # canonical_path lets the Job-Details Publishers
                            # block group rows by file (each file gets its
                            # own section). Without it the modal renders
                            # every server entry under "(unknown path)".
                            "canonical_path": item.canonical_path,
                            # output_paths feeds the BIF-viewer deep-link
                            # in the Files panel — when present, the
                            # inspector links straight to the BIF file
                            # rather than running a Plex title search
                            # that can mis-resolve episodes (e.g. "S04E17"
                            # mishandled by the title-stripping regex).
                            "output_paths": [str(op) for op in (p.output_paths or [])],
                            "artifacts": getattr(p, "artifacts", {}),
                        }
                    )
                self.last_publishers = rows
                # Frames from the cache or an output already on disk never count, whatever an earlier run did.
                self.last_task_slow_path_interval = (
                    getattr(ms_result_obj, "slow_path_interval", None) if _extracted_frames(ms_result_obj) else None
                )

            cancelled = False
            gpu_decoded = False
            try:
                self._skip_gpu_that_failed_it()
                ms_result = _run_once(self.gpu, self.gpu_device)
                gpu_decoded = self.gpu is not None and _extracted_frames(ms_result)
                _capture_publishers(ms_result)
                result = _outcome_for_multi_server_status(ms_result.status)
                self.outcome_counts[result.value] += 1
                if result == ProcessingResult.FAILED:
                    self.failed += 1
                else:
                    self.completed += 1
                _persist(result)
            except CancellationError:
                ctx_logger.info("{} cancelled while processing {}", self.display_name, display_name)
                cancelled = True
                self.outcome_counts["failed"] += 1
                self.failed += 1
                _persist(ProcessingResult.FAILED, "cancelled by user")
            except CodecNotSupportedError as e:
                if self.worker_type == "GPU":
                    # The GPU path couldn't finish this item (unsupported
                    # codec on HW decoder, HW-accel runtime failure,
                    # signal-killed FFmpeg): it goes to another GPU, or
                    # this worker reruns it on its CPU and surfaces the
                    # reason to the UI + logs.
                    reason = str(e) or "GPU processing failed"
                    if self.cancel_check and self.cancel_check():
                        ctx_logger.info("{} cancelled before CPU fallback for {}", self.display_name, display_name)
                        cancelled = True
                        self.outcome_counts["failed"] += 1
                        self.failed += 1
                        _persist(ProcessingResult.FAILED, "cancelled before CPU fallback")
                    elif not self._hand_off_to_another_gpu(e, reason, display_name):
                        self.fallback_active = True
                        self.decoding_on_cpu = True
                        self.fallback_reason = reason
                        self._blame_gpu_for(reason)
                        ctx_logger.warning(
                            "{} couldn't process {} on the GPU and is retrying on CPU. Reason: {}. {}{}",
                            self.display_name,
                            display_name,
                            reason,
                            gpu_fallback_advice(getattr(e, "kind", FALLBACK_CODEC)),
                            self._no_other_gpu_note(),
                        )
                        try:
                            ms_result = _run_once(None, None)
                            _capture_publishers(ms_result)
                            result = _outcome_for_multi_server_status(ms_result.status)
                            self.outcome_counts[result.value] += 1
                            if result == ProcessingResult.FAILED:
                                self.failed += 1
                            else:
                                self.completed += 1
                            ctx_logger.info(
                                "{} completed CPU fallback for {} ({})",
                                self.display_name,
                                display_name,
                                result.value,
                            )
                            _persist(result, f"CPU fallback after GPU error: {reason}")
                        except CancellationError:
                            ctx_logger.info("{} cancelled during CPU fallback for {}", self.display_name, display_name)
                            cancelled = True
                            self.outcome_counts["failed"] += 1
                            self.failed += 1
                            _persist(ProcessingResult.FAILED, "cancelled during CPU fallback")
                        except (NodeUnavailableError, FileNotVisibleError) as node_exc:
                            self._give_back(node_exc, display_name)
                        except Exception as fallback_exc:
                            ctx_logger.exception(
                                "{} also couldn't process {} on CPU after the GPU attempt failed. "
                                "Marking this file as failed; other items keep processing. "
                                "The full traceback is included above.",
                                self.display_name,
                                display_name,
                            )
                            self.outcome_counts["failed"] += 1
                            self.failed += 1
                            _persist(ProcessingResult.FAILED, f"CPU fallback failed: {fallback_exc}")
                else:
                    # CPU worker received codec error — unexpected, treat as failure.
                    ctx_logger.error(
                        "CPU worker {} couldn't decode {}: {}. The file may be corrupt or use a codec "
                        "FFmpeg doesn't support. Marking this file as failed; other items keep "
                        "processing.",
                        self.display_name,
                        display_name,
                        e,
                    )
                    self.outcome_counts["failed"] += 1
                    self.failed += 1
                    _persist(ProcessingResult.FAILED, f"CPU codec error: {e}")
            except (NodeUnavailableError, FileNotVisibleError) as exc:
                self._give_back(exc, display_name)
            except Exception as exc:
                ctx_logger.exception(
                    "{} failed to generate a preview for {}. "
                    "Marking this file as failed; other items keep processing. "
                    "The full traceback is included above.",
                    self.display_name,
                    display_name,
                )
                self.outcome_counts["failed"] += 1
                self.failed += 1
                _persist(ProcessingResult.FAILED, str(exc) or type(exc).__name__)
            finally:
                # Counters are final by here; the dispatcher reads them once this thread has exited.
                if self._done_event is not None:
                    self._done_event.set()
                # A file a node gave back says nothing about the GPU, as a cancelled one doesn't. One handed to another
                # GPU counts against this GPU like a CPU fallback (``_hand_off_to_another_gpu`` blamed it).
                self._record_gpu_file(
                    gpu_decoded=gpu_decoded, cancelled=cancelled or self.last_task_requeue is not None
                )

    def _give_back(self, exc: NodeUnavailableError | FileNotVisibleError, display_name: str) -> None:
        """Record that this task ends without an outcome: the dispatcher queues the file again or fails it.

        Args:
            exc: Why the node couldn't run it.
            display_name: The file, for the job log.
        """
        self.last_task_requeue = requeue_for(exc)
        logger.warning("{} hands {} back to the queue: {}", self.display_name, display_name, exc)

    @property
    def gpu_label(self) -> str:
        """This worker's GPU as people know it ("Quadro M1200"), with its node's name in front on a node."""
        label = friendly_device_label({"name": self.gpu_name or ""}, self.gpu_device, self.gpu)
        return f"{self.node_name} {label}" if self.node_id is not None else label

    def _hand_off_to_another_gpu(self, exc: CodecNotSupportedError, reason: str, display_name: str) -> bool:
        """Give the file back for a worker on another GPU, when this GPU couldn't run it and another runs its kind.

        A disk that stalled or failed a read isn't the GPU's doing, so another GPU wouldn't help: the CPU rerun takes
        it as before. Once every GPU that runs the kind has failed the file, the last one reruns it on its CPU.

        Args:
            exc: Why the GPU run stopped.
            reason: The reason as the job log shows it.
            display_name: The file, for the job log.

        Returns:
            True when the file was given back; the caller then records no outcome for it.
        """
        if self.other_gpu_runs is None or getattr(exc, "kind", FALLBACK_CODEC) in (FALLBACK_IO_ERROR, FALLBACK_STALL):
            return False
        if not self.other_gpu_runs({*self.gpus_refused, self.group_resource}):
            return False
        self._blame_gpu_for(reason)
        # A fresh error without the traceback: the file can wait a long time, and the stack's frames would wait with it.
        self.last_task_handed_off = CodecNotSupportedError(str(exc), kind=getattr(exc, "kind", FALLBACK_CODEC))
        logger.warning(
            "{} couldn't process {} on the GPU and hands it to another GPU. Reason: {}",
            self.display_name,
            display_name,
            reason,
        )
        return True

    def _skip_gpu_that_failed_it(self) -> None:
        """Raise this GPU's earlier error again when it already couldn't run the file, rather than run it again.

        The file came back here because the GPU it waited for went away (removed, outside its hours, or on a node that
        can't take it); the error's handling then sends it to another GPU or to this worker's CPU.

        Raises:
            CodecNotSupportedError: This GPU couldn't run the file before.
        """
        if self.gpu_error is not None:
            raise CodecNotSupportedError(str(self.gpu_error), kind=getattr(self.gpu_error, "kind", FALLBACK_CODEC))

    def _no_other_gpu_note(self) -> str:
        """The CPU rerun's note naming the other GPUs that couldn't run the file either, or ``""`` when none tried."""
        others = [name for resource, name in self.gpus_refused.items() if resource != self.group_resource]
        if not others:
            return ""
        return f" No other GPU left to try ({' and '.join(others)} couldn't either)."

    def _blame_gpu_for(self, reason: str) -> None:
        """Count this file's fallback against its GPU, unless the GPU only lacks the video's format or keeps credits
        on the CPU by design: neither is a fault, and "GPU keeps failing" would send the user to a driver that's fine.

        Args:
            reason: Why the file, or a step of it, ran on the CPU.
        """
        if GPU_LACKS_FORMAT in reason:
            self.gpu_lacked_format = True
        elif not reason.startswith(CREDITS_KEPT_ON_CPU_PREFIX):
            self.gpu_blame_reason = reason

    def _record_gpu_file(self, *, gpu_decoded: bool, cancelled: bool) -> None:
        """Count a finished file for this GPU's fallback streak and the job's CPU-fallback tally.

        A file whose fallback blames the GPU (``_blame_gpu_for``) counts as fallen back; one the GPU itself decoded
        counts as ok and ends a streak; any other (frames from the cache, output already there, failed outright,
        cancelled, or sent to the CPU for a format the GPU lacks) says nothing about the GPU and is not recorded. CPU
        workers never record. Called after the file's counters and done event: nothing here may stop the worker, so a
        tracker failure is logged and dropped.

        Args:
            gpu_decoded: The GPU decoded this file (not the CPU rerun).
            cancelled: The file was cancelled before it finished.
        """
        if self.worker_type != "GPU" or cancelled:
            return
        self.last_task_cpu_fallback = self.fallback_active
        if self.gpu_error is not None:
            return  # the GPU didn't run the file this time; its first try already counted
        fell_back = self.gpu_blame_reason is not None
        if not (fell_back or gpu_decoded):
            return
        device = self.gpu_device or self.gpu or "GPU"
        label = self.gpu_label
        if self.node_id is not None:
            # Two nodes can have the same device path; each node's GPU keeps its own streak.
            device = f"node:{self.node_id}:{device}"
        try:
            get_gpu_fallback_tracker().record(
                device,
                label,
                fell_back=fell_back,
                reason=self.gpu_blame_reason,
            )
        except Exception as exc:
            logger.debug("{}: the GPU fallback tracker failed: {}: {}", self.display_name, type(exc).__name__, exc)

    def _log_pickup(self, item, display_name: str) -> None:
        """Log that this worker started a non-preview item: the kind's own line, or the generic one.

        Args:
            item: The :class:`ProcessableItem` being started.
            display_name: What the generic line names the item by.
        """
        if self.pickup_fn is not None:
            try:
                self.pickup_fn(item, self.display_name)
                return
            except Exception as exc:  # a line describing the item mustn't stop the item
                logger.debug("{}: the kind's pickup line failed: {}", self.display_name, type(exc).__name__)
        logger.info("{} picked up: {}", self.display_name, display_name)

    def _process_custom_item(self, item, progress_callback) -> None:
        """Run a non-preview kind's ``process_fn`` with the same GPU→CPU fallback previews use.

        Args:
            item: The :class:`ProcessableItem` to process.
            progress_callback: Worker progress callback forwarded to ``process_fn``.
        """
        register_job_thread(self.current_job_id or "")
        display_name = self.media_file or self.media_title or item.canonical_path
        with failure_scope(self.current_job_id), placed_on(self.placement):
            self._log_pickup(item, display_name)

            cpu_rerun = False

            def _phase_cb(text: str) -> None:
                # A new phase is a new step with its own FFmpeg run (or none), so the row starts it over: the progress
                # callback shows its percent, speed and ETA again once that run reports. Plain writes, not the pool's
                # ``_progress_lock`` (a worker has no handle on it): each is one attribute store. A reporter of the
                # previous step is normally done by now (its reader is joined, bounded); one that outlives that join
                # could write once over the reset, and the next report from the new step corrects it.
                self.current_phase = text or ""
                self.ffmpeg_started = False
                self.progress_percent = 0
                self.speed = "0.0x"
                self.remaining_time = 0.0
                # A step's own CPU decode ends with the step; a whole-item CPU rerun stays on the CPU throughout.
                if not cpu_rerun:
                    self.decoding_on_cpu = False

            def _fallback_cb(reason: str) -> None:
                # A step that fell back to the CPU inside the kind's own run (the end-picture decode) shows on the row
                # as a whole-item CPU rerun does. Credit text read on the CPU shows there too, but the GPU still
                # decodes its frames, so the decode lines keep the GPU's prefix.
                self.fallback_active = True
                self.decoding_on_cpu = self.decoding_on_cpu or not reason.startswith(CREDITS_ON_CPU_PREFIX)
                self.fallback_reason = redact_secrets(reason) or "GPU processing failed"
                self._blame_gpu_for(self.fallback_reason)

            def _run(gpu, gpu_device):
                return self.process_fn(
                    item,
                    gpu=gpu,
                    gpu_device_path=gpu_device,
                    progress_callback=progress_callback,
                    phase_callback=_phase_cb,
                    cancel_check=self.cancel_check,
                    pause_check=self.pause_check,
                    # The GPU's own gpu_config value, for the kind's GPU work on this GPU (its decodes); CPU work, the
                    # CPU rerun's and chromaprint's included, runs at FFmpeg's own count, as previews' CPU work does.
                    ffmpeg_threads=self.ffmpeg_threads if gpu is not None else None,
                    fallback_callback=_fallback_cb,
                    # True on a GPU worker's CPU rerun too: CPU work it does is its own, not a CPU worker's.
                    gpu_worker=self.worker_type == "GPU",
                    worker_name=self.display_name,
                )

            # Exception text reaches the file's row (served by the jobs API) and the log, and can carry a server URL
            # with its token: masked, and the traceback kept out of the job's own log.
            def _failed(message: str, exc: Exception) -> ItemOutcome:
                text = redact_secrets(message)
                logger.error("{} failed on {}; other items keep processing: {}", self.display_name, display_name, text)
                logger.bind(**{JOB_LOG_SKIP: True}).error(
                    "Traceback of {} on {}:\n{}", self.display_name, display_name, redacted_traceback(exc)
                )
                return ItemOutcome("failed", text)

            cancelled = False
            try:
                self._skip_gpu_that_failed_it()
                outcome = _run(self.gpu, self.gpu_device)
            except CancellationError:
                cancelled = True
                outcome = ItemOutcome("failed", "cancelled by user")
            except CodecNotSupportedError as exc:
                cancelled = bool(self.cancel_check and self.cancel_check())
                reason = redact_secrets(str(exc)) or "GPU processing failed"
                if (
                    self.worker_type == "GPU"
                    and not cancelled
                    and self._hand_off_to_another_gpu(exc, reason, display_name)
                ):
                    self._record_gpu_file(gpu_decoded=False, cancelled=False)
                    return
                if self.worker_type == "GPU" and not cancelled:
                    self.fallback_active = True
                    self.decoding_on_cpu = True
                    cpu_rerun = True
                    self.fallback_reason = reason
                    self._blame_gpu_for(self.fallback_reason)
                    note = self._no_other_gpu_note()
                    logger.warning(
                        "{} couldn't process {} on the GPU and is retrying on CPU. Reason: {}{}",
                        self.display_name,
                        display_name,
                        self.fallback_reason,
                        f".{note}" if note else "",
                    )
                    try:
                        outcome = _run(None, None)
                    except CancellationError:
                        cancelled = True
                        outcome = ItemOutcome("failed", "cancelled during CPU fallback")
                    except (NodeUnavailableError, FileNotVisibleError) as node_exc:
                        self._give_back(node_exc, display_name)
                        return
                    except Exception as fallback_exc:
                        outcome = _failed(f"CPU fallback failed: {fallback_exc}", fallback_exc)
                else:
                    outcome = ItemOutcome("failed", redact_secrets(f"codec error: {exc}"))
            except (NodeUnavailableError, FileNotVisibleError) as exc:
                self._give_back(exc, display_name)
                return
            except Exception as exc:
                outcome = _failed(str(exc) or type(exc).__name__, exc)

            valid = normalize_outcome(outcome, self.outcome_keys)
            if valid is not outcome:
                logger.warning(
                    "{} got {} for {}; counting it as failed.", self.display_name, valid.message, display_name
                )
            outcome = valid
            # Counters first: nothing below may skip them, or the job's outcome breakdown drops this item.
            self.outcome_counts[outcome.outcome_key] = self.outcome_counts.get(outcome.outcome_key, 0) + 1
            if outcome.failed:
                self.failed += 1
            else:
                self.completed += 1
            # A kind doesn't say what the GPU decoded: a file it finished on this GPU counts as the GPU's, unless the
            # CPU reran it or decoded a step of it for a format the GPU lacks.
            gpu_decoded = not (outcome.failed or cpu_rerun or self.gpu_lacked_format)
            self._record_gpu_file(gpu_decoded=gpu_decoded, cancelled=cancelled)
            try:
                raw_rows = list(outcome.publisher_rows or [])
            except Exception:
                logger.warning("{} got unusable publisher rows for {}", self.display_name, display_name)
                raw_rows = []
            # The dispatcher folds these into the job aggregate; a non-dict row would make that fold raise.
            rows = [row for row in raw_rows if isinstance(row, dict)]
            if len(rows) != len(raw_rows):
                logger.warning(
                    "{} dropped {} non-dict publisher row(s) for {}",
                    self.display_name,
                    len(raw_rows) - len(rows),
                    display_name,
                )
            self.last_publishers = rows
            self.last_ms_message = outcome.message or ""
            try:
                _notify_file_result(
                    item.canonical_path,
                    cast(ProcessingResult, outcome_value(outcome.outcome_key)),
                    outcome.message,
                    self.display_name,
                    servers=rows,
                )
            except Exception as persist_exc:
                logger.warning("Failed to persist result for {}: {}", item.canonical_path, persist_exc)

    def check_completion(self) -> bool:
        """Check if this worker has completed its current task.

        Returns:
            bool: True if task completed, False if still running

        """
        if not self.is_busy:
            return False  # Worker is available, not completing

        if self.current_thread and not self.current_thread.is_alive():
            # Thread finished, mark as completed
            self.task_ended_at = time.monotonic()  # before is_busy: a poll then never sees idle with the old time
            self.is_busy = False
            self.current_task = None
            self.current_phase = ""
            self.chapter_progress = None
            return True

        return False

    def last_task_succeeded(self) -> bool:
        """Check whether the most recently completed task succeeded.

        Compares current completed/failed counters against the pre-task
        baselines captured when ``assign_task`` was called.
        """
        completed_delta = self.completed - self._pre_task_completed
        failed_delta = self.failed - self._pre_task_failed
        return completed_delta == 1 and failed_delta == 0

    def last_task_outcome_delta(self) -> dict:
        """Return the outcome count changes for the most recent task.

        Compares current outcome_counts against the pre-task snapshot
        captured when ``assign_task`` was called.

        Returns:
            Dict mapping outcome keys to their delta (usually 0 or 1).

        """
        return {
            key: self.outcome_counts.get(key, 0) - self._pre_task_outcome_counts.get(key, 0)
            for key in self.outcome_counts
        }

    def get_progress_data(self) -> dict:
        """Get current progress data for main thread."""
        return {
            "progress_percent": self.progress_percent,
            "speed": self.speed,
            "task_title": self.task_title,
            "is_busy": self.is_busy,
            "current_duration": self.current_duration,
            "total_duration": self.total_duration,
            "remaining_time": self.remaining_time,
            "worker_id": self.worker_id,
            "group_id": self.group_id,
            "member_id": self.member_id,
            "group_name": self.group_name,
            "group_resource": self.group_resource,
            "retiring": self._pending_removal,
            "worker_type": self.worker_type,
            "library_name": self.library_name,
            "frame": self.frame,
            "fps": self.fps,
            "q": self.q,
            "size": self.size,
            "time_str": self.time_str,
            "bitrate": self.bitrate,
            "chapter_progress": self.chapter_progress if self.is_busy else None,
        }

    def shutdown(self) -> None:
        """Shutdown the worker gracefully."""
        if self.current_thread and self.current_thread.is_alive():
            # Wait for current task to complete (with timeout)
            self.current_thread.join(timeout=60)
            if self.current_thread.is_alive():
                logger.warning(
                    "{} is still busy after waiting 60 seconds for it to stop. "
                    "Continuing shutdown anyway — its FFmpeg process may keep running for a few more seconds "
                    "before the OS reaps it. If you see leftover ffmpeg processes after restart, kill them manually.",
                    self.display_name,
                )


# A node that just dropped a file gets no new file until a registry check begun after the drop comes back ready. This
# cap only ends a hold the registry never answers (a node that stays down loses its workers once it reads offline).
SUSPECT_NODE_S = 300.0


def _group_display_name(group_name: str, worker: Worker) -> str:
    """A group worker's name in the job log: ``Off-hours · 2`` here, ``Lab nodes · lab-gpu/GPU0`` on a node.

    A node worker is named by its node and device, the prefix its own lines carry; this app's running number would
    match nothing the user sees for it.
    """
    return f"{group_name} · {worker.node_device or worker.worker_id + 1}"


class WorkerPool:
    """Manages a pool of workers for processing media items."""

    # Set by group_runtime / dispatcher to force the next refresh; read with a getattr default.
    _group_refresh_at: float

    def __init__(
        self,
        gpu_workers: int,
        cpu_workers: int,
        selected_gpus: list[tuple[str, str, dict]],
    ):
        """Initialize worker pool.

        Args:
            gpu_workers: Number of GPU workers to create
            cpu_workers: Number of CPU workers to create
            selected_gpus: List of (gpu_type, gpu_device, gpu_info) tuples for GPU workers

        """
        self.workers: list[Worker] = []
        self._workers_lock = threading.RLock()
        self._progress_lock = threading.Lock()  # Thread-safe progress updates
        self.selected_gpus = selected_gpus
        self._next_gpu_assignment_index = 0
        self._next_worker_id = 0
        self._next_type_index: dict[str, int] = defaultdict(lambda: 1)
        # Deferred scale-down requests by worker type; busy workers are retired
        # when they finish their current task.
        self._pending_removals: defaultdict[str, int] = defaultdict(int)
        # Optional event set by worker threads on task completion to wake
        # the dispatch loop immediately (set by JobDispatcher).
        self._worker_done_event: threading.Event | None = None
        self._policies: dict[str, dict] | None = None
        self._policy_targets: dict[str, int] = {}
        self._group_revision: int | None = None
        self._resource_targets: dict[str, int] = {}
        # The node registry as read at the last reconcile; capacity and snapshots use it until the next one.
        self._node_hardware: NodeHardware = EMPTY_NODE_HARDWARE
        self._node_revision: int | None = None
        # Node id -> the monotonic time its hold ends at the latest.
        self._suspect_nodes: dict[str, float] = {}
        self.add_workers("GPU", gpu_workers)
        self.add_workers("CPU", cpu_workers)
        # The "Created fresh worker pool" line comes from whoever made it, once its worker groups are applied:
        # members on nodes only get their workers then.

    def has_busy_workers(self) -> bool:
        """Check if any workers are currently busy."""
        with self._workers_lock:
            return any(worker.is_busy for worker in self.workers)

    def _snapshot_workers(self) -> list["Worker"]:
        """Return a stable snapshot of workers for safe iteration."""
        with self._workers_lock:
            return list(self.workers)

    def size_summary(self) -> str:
        """The pool's size for the job log: ``"3 worker(s): 1 GPU + 0 CPU here, 1 GPU + 1 CPU on nodes"``.

        The part about nodes is there only when a worker runs on one.
        """
        workers = self._snapshot_workers()

        def count(worker_type: str, on_node: bool) -> int:
            return sum(1 for w in workers if w.worker_type == worker_type and (w.node_id is not None) == on_node)

        text = f"{len(workers)} worker(s): {count('GPU', False)} GPU + {count('CPU', False)} CPU"
        node_gpu, node_cpu = count("GPU", True), count("CPU", True)
        if node_gpu or node_cpu:
            text += f" here, {node_gpu} GPU + {node_cpu} CPU on nodes"
        return text

    def log_created(self) -> None:
        """Log that this pool was just made, with its size once its worker groups are applied."""
        logger.info(
            "Created fresh worker pool — {} (subsequent jobs reuse this pool, you'll see 'reusing shared worker pool' "
            "instead)",
            self.size_summary(),
        )

    def reconcile_groups(
        self, groups: list[dict], selected_gpus: list | None, *, now=None, revision: int | None = None
    ) -> dict:
        """Apply saved member targets, retaining exact busy slots until they finish.

        Each group member is one policy owning its own slots. Closed and removed
        members contribute no target. Their in-flight tasks still consume the
        resource's assignment budget until reaped. Creating idle replacement
        slots therefore cannot oversubscribe a handover. A member on a node gets
        its target from the node registry's capacity for that node.
        """
        from copy import deepcopy

        from ..worker_groups import group_is_available, group_resource_key, member_policies
        from .node_members import member_detected, read_node_hardware

        by_id = {p["id"]: p for p in deepcopy(member_policies(groups))}
        # Read before taking the pool lock: the registry has its own lock and the dispatch loop must not wait on it.
        nodes = read_node_hardware(by_id.values())
        added = removed = 0
        with self._workers_lock:
            if revision is not None and self._group_revision is not None and revision < self._group_revision:
                return {"added": 0, "removed": 0, "retiring": sum(w._pending_removal for w in self.workers)}
            if revision is not None:
                self._group_revision = revision
            # Periodic time reevaluation must use hardware from the latest
            # locked refresh, never overwrite a concurrent detection update.
            selected_gpus = list(self.selected_gpus if selected_gpus is None else selected_gpus)
            devices = {device: (gpu, device, info) for gpu, device, info in selected_gpus}
            targets = {
                pid: p["count"] if group_is_available(p, now=now) and member_detected(p, devices, nodes) else 0
                for pid, p in by_id.items()
            }
            resources: dict[str, int] = {}
            for pid, policy in by_id.items():
                key = group_resource_key(policy)
                resources[key] = resources.get(key, 0) + targets[pid]
            self._policies = by_id
            self._policy_targets = targets
            self._resource_targets = resources
            self._node_hardware = nodes
            self.selected_gpus = list(selected_gpus)
            self._pending_removals.clear()
            # Legacy/unowned slots and changed resources may finish, never
            # become another member's slot while holding an assigned task.
            for worker in list(self.workers):
                current_policy = by_id.get(worker.policy_id)
                if current_policy is None or worker.group_resource != group_resource_key(current_policy):
                    if worker.is_busy:
                        worker._pending_removal = True
                    else:
                        self.workers.remove(worker)
                        removed += 1
            for pid, policy in by_id.items():
                resource = group_resource_key(policy)
                owned = [w for w in self.workers if w.policy_id == pid and w.group_resource == resource]
                # Prefer existing non-retiring slots, then revive compatible
                # draining slots before allocating any replacements.
                owned.sort(key=lambda w: (w._pending_removal, not w.is_busy))
                desired = targets[pid]
                hardware = nodes.member(policy) if policy.get("node") else None
                for index, worker in enumerate(owned):
                    if hardware is not None and hardware.available:
                        # A rename or GPU reorder on the node shows in the job log prefix of workers already running.
                        worker.node_name = hardware.node_name
                        if worker.worker_type == "GPU" and hardware.gpu:
                            worker.gpu_index = hardware.gpu_index
                            worker.gpu_name = hardware.gpu.get("name") or policy["device"]
                    worker.group_name = policy["name"]
                    worker.allowed_job_types = frozenset(policy["job_types"])
                    worker.display_name = _group_display_name(policy["name"], worker)
                    if index < desired:
                        worker._pending_removal = False
                        if worker.worker_type == "GPU":
                            worker.ffmpeg_threads = (
                                NODE_GPU_FFMPEG_THREADS
                                if policy.get("node")
                                else devices[policy["device"]][2].get("ffmpeg_threads")
                            )
                    elif worker.is_busy:
                        worker._pending_removal = True
                    else:
                        self.workers.remove(worker)
                        removed += 1
                for _ in range(max(0, desired - len(owned))):
                    if policy.get("node"):
                        worker = self._create_node_worker(policy, hardware or nodes.member(policy))
                    else:
                        if policy["resource"] == "gpu":
                            self._next_gpu_assignment_index = next(
                                i for i, (_, device, _) in enumerate(self.selected_gpus) if device == policy["device"]
                            )
                        worker = self._create_worker("CPU" if policy["resource"] == "cpu" else "GPU")
                    worker.group_id = policy["group_id"]
                    worker.member_id = policy["member_id"]
                    worker.policy_id = pid
                    worker.group_name = policy["name"]
                    worker.group_resource = resource
                    worker.allowed_job_types = frozenset(policy["job_types"])
                    worker.display_name = _group_display_name(policy["name"], worker)
                    self.workers.append(worker)
                    added += 1
            retiring = sum(w._pending_removal for w in self.workers)
        if self._worker_done_event is not None:
            self._worker_done_event.set()
        return {"added": added, "removed": removed, "retiring": retiring}

    def _worker_supports_kind(self, worker: "Worker", kind: str, *, budget: bool = True) -> bool:
        """Check intrinsic capability, member policy and shared draining budget.

        Caller holds ``_workers_lock`` through assignment reservation.

        Args:
            worker: The worker.
            kind: The job kind.
            budget: Also require a free slot in its resource's shared budget; False asks only whether it runs
                ``kind`` now, busy or not.
        """
        if kind == "loudness" and worker.worker_type != "CPU":
            return False
        if worker.allowed_job_types is not None and kind not in worker.allowed_job_types:
            return False
        if self._policies is None:
            return True
        if not worker.policy_id or self._policy_targets.get(worker.policy_id, 0) <= 0:
            return False
        from ..worker_groups import group_is_available

        if not group_is_available(self._policies[worker.policy_id]):
            return False
        if not budget:
            return True
        busy = sum(w.is_busy and w.group_resource == worker.group_resource for w in self.workers)
        return busy < self._resource_targets.get(worker.group_resource, 0)

    def capacity_for(self, kind: str) -> dict:
        """Describe configured and available capacity without treating busy as disabled."""
        from .group_runtime import capacity_for_groups

        with self._workers_lock:
            if self._policies is None:
                compatible = [w for w in self.workers if kind != "loudness" or w.worker_type == "CPU"]
                opened = len([w for w in compatible if not w._pending_removal])
                return {
                    "configured": opened,
                    "open": opened,
                    "available": sum(w.is_available() for w in compatible),
                    "busy": sum(w.is_busy for w in compatible),
                    "reason": "ready" if opened else "configuration",
                    "next_opening": None,
                }
            result = capacity_for_groups(
                list(self._policies.values()), self.selected_gpus, kind, nodes=self._node_hardware
            )
            result["available"] = 0
            for resource, target in self._resource_targets.items():
                resource_workers = [w for w in self.workers if w.group_resource == resource]
                free_budget = max(0, target - sum(w.is_busy for w in resource_workers))
                idle = sum(w.is_available() and self._worker_supports_kind(w, kind) for w in resource_workers)
                result["available"] += min(free_budget, idle)
            result["busy"] = sum(
                w.is_busy
                and (kind != "loudness" or w.worker_type == "CPU")
                and (w.allowed_job_types is None or kind in w.allowed_job_types)
                for w in self.workers
            )
            return result

    def can_run_outside(self, kind: str, exclude_nodes: Collection[str]) -> bool:
        """Whether the configured workers can still run ``kind`` somewhere other than ``exclude_nodes``.

        True while nothing at all is configured for the kind (the item waits, as any item does); False when every
        configured slot for it that can come back is on an excluded node. Busy slots, members outside their hours and
        members on a node that is offline right now count: they come back, so the item waits for them instead of
        failing. Members whose node needs someone to act (``NodeHardware.member_can_come_back``: turned off, removed,
        needing an update, refusing the key, an access problem, no worker role, or not listing the member's GPU) and
        this app's members on a GPU it hasn't detected don't count: an item waiting for them would never finish.

        Args:
            kind: The job kind.
            exclude_nodes: Node ids that can't run the item.
        """
        from ..worker_groups import supports_job
        from .node_members import member_detected

        with self._workers_lock:
            if self._policies is None:
                nodes = [
                    getattr(w, "node_id", None)
                    for w in self.workers
                    if not w._pending_removal
                    and (kind != "loudness" or w.worker_type == "CPU")
                    and (w.allowed_job_types is None or kind in w.allowed_job_types)
                ]
            else:
                devices = {device for _gpu, device, _info in self.selected_gpus}
                configured = [
                    policy
                    for policy in self._policies.values()
                    if policy.get("enabled", True) and policy.get("count", 0) > 0 and supports_job(policy, kind)
                ]
                if not configured:
                    return True
                nodes = [
                    policy.get("node")
                    for policy in configured
                    if (
                        self._node_hardware.member_can_come_back(policy)
                        if policy.get("node")
                        else member_detected(policy, devices, self._node_hardware)
                    )
                ]
                return any(node is None or node not in exclude_nodes for node in nodes)
            return not nodes or any(node is None or node not in exclude_nodes for node in nodes)

    def runs_kind_outside(self, kind: str, exclude_nodes: Collection[str]) -> bool:
        """Whether a worker outside ``exclude_nodes`` runs ``kind`` now, busy or not.

        Narrower than :meth:`can_run_outside`: a member outside its hours, or on a node that is offline, has no worker
        that runs now, so a file waiting for one of those could wait hours.

        Args:
            kind: The job kind.
            exclude_nodes: Node ids left out. This app's own workers are never left out.
        """
        with self._workers_lock:
            return any(
                not worker._pending_removal
                and getattr(worker, "node_id", None) not in exclude_nodes
                and self._worker_supports_kind(worker, kind, budget=False)
                for worker in self.workers
            )

    def runs_kind_on_other_gpu(
        self,
        kind: str,
        exclude_gpus: Collection[str],
        exclude_nodes: Collection[str] = (),
        frozen_jobs: Collection[str] = (),
    ) -> bool:
        """Whether a worker on a GPU outside ``exclude_gpus`` and ``exclude_nodes`` runs ``kind`` now, busy or not.

        Like :meth:`runs_kind_outside`: a member outside its hours has no worker that runs now, so a file waiting for
        one could wait hours; it goes to the CPU instead. So does one whose only such worker is busy on a frozen job.

        Args:
            kind: The job kind.
            exclude_gpus: GPUs (``group_resource``) that couldn't run the file.
            exclude_nodes: Nodes the file can't go to (``dispatcher._nodes_off``): a GPU there can't take it.
            frozen_jobs: Jobs whose running files are frozen until their schedule's window opens again.
        """
        with self._workers_lock:
            return any(
                worker.worker_type == "GPU"
                and not worker._pending_removal
                and worker.group_resource not in exclude_gpus
                and getattr(worker, "node_id", None) not in exclude_nodes
                and not (worker.is_busy and worker.current_job_id in frozen_jobs)
                and self._worker_supports_kind(worker, kind, budget=False)
                for worker in self.workers
            )

    def stuck_node_members(self, kind: str, exclude_nodes: Collection[str]) -> list[str]:
        """``"<node> (<reason>)"`` for each node that has members for ``kind`` it won't give work until someone acts.

        What :meth:`can_run_outside` leaves out on nodes outside ``exclude_nodes``, so a file failed for want of
        anywhere else to run can say why those didn't count.

        Args:
            kind: The job kind.
            exclude_nodes: Node ids that can't run the item.
        """
        from ..worker_groups import supports_job

        with self._workers_lock:
            hardware = self._node_hardware
            stuck = [
                f"{hardware.node_name(policy['node'])} ({hardware.member(policy).reason})"
                for policy in (self._policies or {}).values()
                if policy.get("node")
                and policy["node"] not in exclude_nodes
                and policy.get("enabled", True)
                and policy.get("count", 0) > 0
                and supports_job(policy, kind)
                and not hardware.member_can_come_back(policy)
            ]
        return list(dict.fromkeys(stuck))

    def capacity_outside(self, kind: str, exclude_nodes: Collection[str]) -> dict | None:
        """The configured capacity for ``kind`` off ``exclude_nodes``, judged like a job's (``capacity_for_groups``).

        Worked out from the last reconcile's policies and node snapshot after the pool lock is released.

        Args:
            kind: The job kind.
            exclude_nodes: Node ids that can't run the item.

        Returns:
            ``capacity_for_groups``' dict (``open`` 0 with the ``reason`` when no member left is open now), or None for
            a pool without worker groups.
        """
        from .group_runtime import capacity_for_groups

        with self._workers_lock:
            if self._policies is None:
                return None
            policies = [
                policy
                for policy in self._policies.values()
                if policy.get("node") not in exclude_nodes and policy.get("count", 0) > 0
            ]
            selected_gpus = list(self.selected_gpus)
            nodes = self._node_hardware
        return capacity_for_groups(policies, selected_gpus, kind, nodes=nodes)

    def has_capacity_for(self, kind: str) -> bool:
        """Whether configured slots can run this kind now, even when all are busy."""
        from ..worker_groups import group_is_available, supports_job

        with self._workers_lock:
            if self._policies is None:
                return True  # Legacy callers retain their independent checking stage.
            return any(
                self._policy_targets.get(pid, 0) > 0 and supports_job(policy, kind) and group_is_available(policy)
                for pid, policy in self._policies.items()
            )

    def member_snapshots(self) -> list[dict]:
        """Return saved targets and live slots per group member, including removed members still draining.

        Each row is the member policy (``id`` = ``"<group_id>:<member_id>"``, ``group_id``, ``member_id``, ``name``,
        ``enabled``, ``availability``, ``resource``, ``device``, ``count``, ``job_types``) plus ``target``,
        ``running``, ``available``, ``finishing``, ``state`` (active, disabled, hardware_unavailable,
        node_unavailable, off_hours, draining), ``reason`` (why a node member has zero workers, else ``""``),
        ``node_name`` (``None`` for this app) and ``next_opening``. Draining rows have ``count`` 0 and ``enabled``
        False.
        """
        from ..worker_groups import group_resource_key, next_group_opening
        from .node_members import MEMBER_STATE_NODE_UNAVAILABLE, worker_node_id

        with self._workers_lock:
            policies = dict(self._policies or {})
            for worker in self.workers:
                if worker.policy_id and worker.policy_id not in policies:
                    policies[worker.policy_id] = {
                        "id": worker.policy_id,
                        "group_id": worker.group_id,
                        "member_id": worker.member_id,
                        "name": worker.group_name,
                        "enabled": False,
                        "resource": "cpu" if worker.worker_type == "CPU" else "gpu",
                        "device": worker.gpu_device,
                        "count": 0,
                        "job_types": list(worker.allowed_job_types or ()),
                        "availability": {"mode": "always", "windows": []},
                        "node": worker_node_id(worker),
                    }
            rows = []
            devices = {device for _, device, _ in self.selected_gpus}
            nodes = self._node_hardware
            for pid, policy in policies.items():
                workers = [w for w in self.workers if w.policy_id == pid]
                target = self._policy_targets.get(pid, 0)
                finishing = sum(w.is_busy and w._pending_removal for w in workers)
                node = policy.get("node")
                hardware = nodes.member(policy) if node else None
                state = "active"
                if not policy["enabled"]:
                    state = "disabled"
                elif hardware is not None and not hardware.available:
                    state = MEMBER_STATE_NODE_UNAVAILABLE
                elif not node and policy["resource"] == "gpu" and policy.get("device") not in devices:
                    state = "hardware_unavailable"
                elif not target:
                    state = "off_hours"
                if finishing and not target:
                    state = "draining"
                opening = next_group_opening(policy)
                resource = group_resource_key(policy)
                free_budget = max(
                    0,
                    self._resource_targets.get(resource, 0)
                    - sum(w.is_busy and w.group_resource == resource for w in self.workers),
                )
                rows.append(
                    {
                        **policy,
                        "target": target,
                        "running": sum(w.is_busy for w in workers),
                        "available": min(
                            free_budget,
                            sum(
                                w.is_available() and any(self._worker_supports_kind(w, k) for k in policy["job_types"])
                                for w in workers
                            ),
                        ),
                        "finishing": finishing,
                        "state": state,
                        "reason": hardware.reason if hardware is not None else "",
                        "node_name": hardware.node_name if hardware is not None else None,
                        "next_opening": opening.isoformat() if opening is not None else None,
                    }
                )
            return rows

    def _create_node_worker(self, policy: dict, hardware: NodeMemberHardware) -> "Worker":
        """Create a slot for a member on a node, typed and named from the node's own GPU entry.

        Caller holds ``_workers_lock`` and has checked ``hardware.available``.
        """
        worker_id = self._next_worker_id
        self._next_worker_id += 1
        if policy["resource"] == "gpu":
            gpu = hardware.gpu or {}
            worker = Worker(
                worker_id,
                "GPU",
                node_id=policy["node"],
                node_name=hardware.node_name,
                gpu=gpu.get("type"),
                gpu_device=policy["device"],
                gpu_index=hardware.gpu_index,
                gpu_name=gpu.get("name") or policy["device"],
                ffmpeg_threads=NODE_GPU_FFMPEG_THREADS,
            )
        else:
            worker = Worker(worker_id, "CPU", node_id=policy["node"], node_name=hardware.node_name)
        worker._done_event = self._worker_done_event
        return worker

    def _create_worker(self, worker_type: str) -> "Worker":
        """Create a worker with a unique ID and display name, wired to wake the dispatch loop when it finishes."""
        worker_id = self._next_worker_id
        self._next_worker_id += 1
        normalized_type = worker_type.upper()
        type_idx = self._next_type_index[normalized_type]
        self._next_type_index[normalized_type] = type_idx + 1

        if normalized_type == "GPU":
            if not self.selected_gpus:
                raise ValueError("Cannot create GPU worker: no GPUs available")
            gpu_index = self._next_gpu_assignment_index % len(self.selected_gpus)
            self._next_gpu_assignment_index += 1
            gpu_type, gpu_device, gpu_info = self.selected_gpus[gpu_index]
            gpu_name = gpu_info.get("name", f"{gpu_type} GPU")
            per_gpu_ffmpeg = gpu_info.get("ffmpeg_threads")
            worker = Worker(
                worker_id,
                "GPU",
                gpu_type,
                gpu_device,
                gpu_index,
                gpu_name,
                ffmpeg_threads=per_gpu_ffmpeg,
            )
            worker.display_name = f"GPU Worker {type_idx} ({gpu_name})"
            worker._done_event = self._worker_done_event
            return worker

        if normalized_type == "CPU":
            worker = Worker(worker_id, "CPU")
            worker.display_name = f"CPU Worker {type_idx}"
            worker._done_event = self._worker_done_event
            return worker
        raise ValueError(f"Unsupported worker type: {worker_type}")

    def add_workers(self, worker_type: str, count: int) -> int:
        """Add workers of a specific type and return added count."""
        if self._policies is not None:
            raise ValueError("Update a worker group's saved count instead of the shared pool")
        if count <= 0:
            return 0

        added = 0
        with self._workers_lock:
            for _ in range(count):
                self.workers.append(self._create_worker(worker_type))
                added += 1
        if added > 0:
            logger.info("Added {} {} worker(s)", added, worker_type.upper())
        return added

    def remove_workers(self, worker_type: str, count: int) -> dict:
        """Remove workers of a type.

        Idle workers are removed immediately. Busy workers are scheduled for
        deferred removal and retired when they become idle.

        Returns:
            {"removed": int, "scheduled": int, "unavailable": int}

        """
        if self._policies is not None:
            raise ValueError("Update a worker group's saved count instead of the shared pool")
        if count <= 0:
            return {"removed": 0, "scheduled": 0, "unavailable": 0}

        normalized_type = worker_type.upper()
        if normalized_type not in {"GPU", "CPU"}:
            raise ValueError(f"Unsupported worker type: {worker_type}")

        removed = 0
        scheduled = 0
        unavailable = 0
        with self._workers_lock:
            matches = [w for w in self.workers if w.worker_type == normalized_type]
            unavailable = max(0, count - len(matches))
            idle_matches = [w for w in matches if not w.is_busy]
            busy_matches = [w for w in matches if w.is_busy]

            for worker in idle_matches:
                if removed >= count:
                    break
                self.workers.remove(worker)
                removed += 1

            remaining = count - removed
            if remaining > 0 and busy_matches:
                scheduled = min(remaining, len(busy_matches))
                self._pending_removals[normalized_type] += scheduled

        if removed > 0:
            logger.info("Removed {} idle {} worker(s)", removed, normalized_type)
        if scheduled > 0:
            logger.info("Scheduled {} busy {} worker(s) for removal when idle", scheduled, normalized_type)
        return {"removed": removed, "scheduled": scheduled, "unavailable": unavailable}

    def _retire_idle_worker_if_scheduled(self, worker: "Worker") -> bool:
        """Retire an idle worker if deferred removal was requested.

        Checks both the per-worker ``_pending_removal`` flag (set by
        reconcile_gpu_workers for device-aware deferral) and the type-level
        ``_pending_removals`` counter (set by remove_workers).
        """
        if worker.is_busy:
            return False

        with self._workers_lock:
            if worker not in self.workers:
                return False

            # Per-worker flag takes priority (device-aware reconciliation)
            if worker._pending_removal:
                self.workers.remove(worker)
                logger.info("Retired {} after deferred reconciliation removal", worker.display_name)
                return True

            pending = int(self._pending_removals.get(worker.worker_type, 0))
            if pending <= 0:
                return False
            self.workers.remove(worker)
            self._pending_removals[worker.worker_type] = pending - 1

        logger.info("Retired {} after deferred removal request", worker.display_name)
        return True

    def _apply_deferred_removals(self) -> int:
        """Remove currently idle workers that are pending deferred retirement."""
        retired = 0
        # Work on a snapshot to avoid mutating the list while iterating.
        for worker in self._snapshot_workers():
            if self._retire_idle_worker_if_scheduled(worker):
                retired += 1
        return retired

    def reconcile_gpu_workers(self, new_selected_gpus: list[tuple[str, str, dict]]) -> dict:
        """Reconcile live GPU workers against a new GPU configuration.

        Compares current GPU workers (by device path) against
        *new_selected_gpus* and adds/removes workers so the pool matches
        the desired state.

        Idle workers are removed from the pool immediately.  Busy workers
        are flagged with ``_pending_removal`` so the dispatcher can still
        track their in-flight task completions; they are retired
        automatically once they become idle.  This avoids the type-level
        ``_pending_removals`` counter which cannot distinguish between
        devices in a multi-GPU setup.

        Flagged workers count as gone: raising a device's count first
        clears their flags (they stay) and only then adds new workers, and
        lowering it again flags only workers that aren't flagged yet.

        GPU removals still waiting in the type-level counter (busy workers
        removed through ``remove_workers``, i.e. the workers API) are
        cancelled, so the pool settles at the counts passed here: an API
        GPU change lasts only until the next reconcile.

        Args:
            new_selected_gpus: List of (gpu_type, gpu_device, gpu_info) tuples
                for GPUs that should be active.  Each gpu_info dict must
                contain a ``workers`` key with the desired worker count.

        Returns:
            Summary dict with keys ``added``, ``removed``, and ``deferred``.

        """
        if self._policies is not None:
            from .group_runtime import current_group_policy

            groups, revision = current_group_policy()
            summary = self.reconcile_groups(
                list(self._policies.values()) if groups is None else groups, new_selected_gpus
            )
            return {**summary, "deferred": summary["retiring"]}
        new_by_device: dict[str, tuple] = {
            device: (gpu_type, device, info) for gpu_type, device, info in new_selected_gpus
        }

        added = 0
        removed = 0
        deferred = 0
        cancelled = 0

        with self._workers_lock:
            api_removals_cancelled = self._pending_removals["GPU"]
            self._pending_removals["GPU"] = 0

            current_by_device: dict[str, list] = defaultdict(list)
            for w in self.workers:
                if w.worker_type == "GPU" and w.gpu_device:
                    current_by_device[w.gpu_device].append(w)

            # Remove workers for devices no longer enabled
            for device, workers in current_by_device.items():
                if device not in new_by_device:
                    for w in workers:
                        if w.is_busy:
                            w._pending_removal = True
                            deferred += 1
                        else:
                            self.workers.remove(w)
                            removed += 1

            # Adjust worker counts for devices still enabled
            for device, gpu_tuple in new_by_device.items():
                _, _, info = gpu_tuple
                desired = info.get("workers", 1)
                current_workers = current_by_device.get(device, [])
                staying = [w for w in current_workers if not w._pending_removal]
                retiring = [w for w in current_workers if w._pending_removal]
                current_count = len(staying)

                if current_count > desired:
                    excess = current_count - desired
                    idle_first = sorted(staying, key=lambda w: w.is_busy)
                    for w in idle_first[:excess]:
                        if w.is_busy:
                            w._pending_removal = True
                            deferred += 1
                        else:
                            self.workers.remove(w)
                            removed += 1
                elif current_count < desired:
                    kept = retiring[: desired - current_count]
                    for w in kept:
                        w._pending_removal = False
                    cancelled += len(kept)
                    deficit = desired - current_count - len(kept)

                    # Put the device's new entry in selected_gpus, in its place, so
                    # _create_worker builds from it (its ffmpeg_threads included);
                    # the list is replaced at the end of this block.
                    at = next((i for i, (_, d, _) in enumerate(self.selected_gpus) if d == device), None)
                    self.selected_gpus = list(self.selected_gpus)
                    if at is None:
                        self.selected_gpus.append(gpu_tuple)
                    else:
                        self.selected_gpus[at] = gpu_tuple

                    gpu_idx = next(
                        (i for i, (_, d, _) in enumerate(self.selected_gpus) if d == device),
                        0,
                    )
                    for _ in range(deficit):
                        self._next_gpu_assignment_index = gpu_idx
                        self.workers.append(self._create_worker("GPU"))
                        added += 1

                # A saved thread change reaches the workers already on the GPU from their next file (previews and markers
                # alike), as a count change does; new workers got it from selected_gpus above. An entry without the key
                # (the settings always give one) leaves their value as it was.
                if "ffmpeg_threads" in info:
                    for w in self.workers:
                        if w.worker_type == "GPU" and w.gpu_device == device:
                            w.ffmpeg_threads = info["ffmpeg_threads"]

            self.selected_gpus = list(new_selected_gpus)
            self._next_gpu_assignment_index = 0

        if api_removals_cancelled:
            logger.info(
                "Cancelled {} pending GPU worker removal(s) from the workers API; the saved GPU counts apply",
                api_removals_cancelled,
            )
        if cancelled:
            logger.info("Kept {} busy GPU worker(s) that were due to retire", cancelled)
        if removed or added or deferred:
            logger.info("GPU reconciliation: added={}, removed={}, deferred={}", added, removed, deferred)
        return {"added": added, "removed": removed, "deferred": deferred}

    def reconcile_cpu_workers(self, desired: int) -> dict:
        """Reconcile live CPU workers against the saved CPU worker count.

        Busy workers already due to retire count as gone, so raising the
        count first cancels those pending removals and only then adds new
        workers. Lowering it removes idle workers at once; busy ones retire
        after their current task, via the type-level ``_pending_removals``
        counter (CPU workers have no device to tell apart).

        Args:
            desired: Number of CPU workers the pool should settle at.

        Returns:
            Summary dict with keys ``added``, ``removed``, ``deferred`` (busy
            workers this call scheduled to retire) and ``retiring`` (every busy
            CPU worker still due to retire, including earlier calls' ones).

        """
        if self._policies is not None:
            from .group_runtime import current_group_policy

            groups, revision = current_group_policy()
            summary = self.reconcile_groups(
                list(self._policies.values()) if groups is None else groups, self.selected_gpus
            )
            return {**summary, "deferred": summary["retiring"]}
        desired = max(0, desired)
        added = 0
        removed = 0
        deferred = 0
        cancelled = 0

        with self._workers_lock:
            cpu_count = sum(1 for w in self.workers if w.worker_type == "CPU")
            pending = self._pending_removals["CPU"]
            effective = cpu_count - pending

            if desired > effective:
                cancelled = min(pending, desired - effective)
                self._pending_removals["CPU"] = pending - cancelled
                added = self.add_workers("CPU", desired - effective - cancelled)
            elif desired < effective:
                result = self.remove_workers("CPU", effective - desired)
                removed = result["removed"]
                deferred = result["scheduled"]
            retiring = self._pending_removals["CPU"]

        if cancelled:
            logger.info("Kept {} busy CPU worker(s) that were due to retire", cancelled)
        return {"added": added, "removed": removed, "deferred": deferred, "retiring": retiring}

    def mark_node_suspect(self, node_id: str) -> None:
        """Hold new files back from a node that just dropped one, until a check begun after the drop finds it ready
        (spec §6).

        Without this the file it gave back goes straight to another worker on the same dead node (GPU workers are
        picked first), and so does every other queued file, each one failing there within milliseconds.

        Args:
            node_id: The node.
        """
        with self._workers_lock:
            self._suspect_nodes[node_id] = time.monotonic() + SUSPECT_NODE_S

    def refresh_suspect_nodes(self) -> None:
        """Clear each suspect node a registry check begun since its drop found ready, or whose hold ran out.

        Call without the pool lock held; the dispatch loop calls it once per assignment pass.
        """
        with self._workers_lock:
            if not self._suspect_nodes:
                return
            suspects = dict(self._suspect_nodes)
        now = time.monotonic()
        cleared = []
        for node_id, until in suspects.items():
            if now >= until or checked_ready_since_drop(node_id):
                cleared.append(node_id)
        with self._workers_lock:
            for node_id in cleared:
                # A drop marked meanwhile starts a new hold.
                if self._suspect_nodes.get(node_id) == suspects[node_id]:
                    del self._suspect_nodes[node_id]

    def _is_suspect(self, node_id: str | None, now: float) -> bool:
        """Whether ``node_id``'s new files are held back. Caller holds ``_workers_lock``."""
        if node_id is None:
            return False
        until = self._suspect_nodes.get(node_id)
        return until is not None and now < until

    def _find_available_worker(
        self,
        cpu_only: bool = False,
        *,
        claim: bool = False,
        kind: str = "previews",
        exclude_nodes: Collection[str] = (),
        exclude_gpus: Collection[str] = (),
    ) -> Optional["Worker"]:
        """Find an available worker.

        Args:
            cpu_only: If True, only look for CPU workers
            claim: If True, atomically mark the returned worker as busy
                under the pool lock so a concurrent caller (the dispatcher
                thread vs. the worker-pool's own consumer loop) can't
                find-and-assign the same worker. The caller is responsible
                for either calling ``worker.assign_task(...)`` or unsetting
                ``worker.is_busy = False`` if it backs out.
            kind: The job kind the worker must run.
            exclude_nodes: Node ids whose workers are skipped (nodes that couldn't see the item's file). This app's
                own workers have no node and are never skipped. Workers on a node that just dropped a file
                (:meth:`mark_node_suspect`) are skipped too.
            exclude_gpus: GPUs (``group_resource``) that couldn't run the item. When given, only a worker on another
                GPU is found: the CPU waits until no other GPU runs the kind (:meth:`runs_kind_on_other_gpu`).

        Returns:
            An available worker matching criteria, GPU workers before CPU workers, or None
        """
        now = time.monotonic()
        with self._workers_lock:
            # Group order isn't a priority (an upgrade from v4 lists the CPU group first), so an idle GPU always wins.
            for worker in sorted(self.workers, key=lambda w: w.worker_type != "GPU"):
                if cpu_only and worker.worker_type != "CPU":
                    continue
                node_id = getattr(worker, "node_id", None)
                if node_id is not None and (node_id in exclude_nodes or self._is_suspect(node_id, now)):
                    continue
                if exclude_gpus and (worker.worker_type != "GPU" or worker.group_resource in exclude_gpus):
                    continue
                if worker.is_available() and self._worker_supports_kind(worker, kind):
                    if claim:
                        worker.is_busy = True
                    return worker
            return None

    def _update_worker_progress(
        self,
        worker,
        progress_percent,
        current_duration,
        total_duration,
        speed=None,
        remaining_time=None,
        frame=0,
        fps=0,
        q=0,
        size=0,
        time_str="00:00:00.00",
        bitrate=0,
        media_file=None,
    ):
        """Update worker progress data from callback."""
        # Use thread-safe updates to prevent race conditions
        with self._progress_lock:
            worker.progress_percent = progress_percent
            worker.current_duration = current_duration
            worker.total_duration = total_duration
            if speed:
                worker.speed = speed
            if remaining_time is not None:
                worker.remaining_time = remaining_time

            # Store media file path if provided
            if media_file:
                worker.media_file = media_file

            # Store FFmpeg data for display
            worker.frame = frame
            worker.fps = fps
            worker.q = q
            worker.size = size
            worker.time_str = time_str
            worker.bitrate = bitrate

            # Log when FFmpeg actually starts processing (only once)
            if not worker.start_logged:
                worker.start_logged = True
                display_path = worker.media_file if worker.media_file else worker.media_title
                logger.info("{} Started processing {}", worker.log_tag, display_path)

            # Mark that FFmpeg has started outputting progress
            worker.ffmpeg_started = True

            # Emit periodic progress logs every 5 seconds.
            # Read worker.speed (the last-known-good value, kept across
            # FFmpeg "speed=N/A" lines) rather than the callback's
            # ``speed`` parameter so the throttled log doesn't print
            # "0.0x" the moment FFmpeg emits an unmeasurable line.
            current_time = time.time()
            if current_time - worker.last_verbose_log_time >= 5.0:
                worker.last_verbose_log_time = current_time
                logger.info(
                    "{} {} - {}% (speed={})",
                    worker.log_tag,
                    worker.media_title,
                    round(progress_percent, 1),
                    worker.speed,
                )

    def shutdown(self) -> None:
        """Shutdown all workers gracefully."""
        logger.info("Shutting down worker pool...")
        for worker in self._snapshot_workers():
            worker.shutdown()
        logger.info("Worker pool shutdown complete")
