"""Multi-job dispatcher for concurrent job processing.

Provides a persistent dispatch loop and shared worker pool so multiple jobs
can run simultaneously.  Items are dispatched using priority-aware drain-first
scheduling: workers focus on the highest-priority active job and only spill
over to the next job when the current job's queue is empty.
"""

import threading
import time
from collections import deque
from collections.abc import Callable
from functools import partial
from typing import Any

from loguru import logger

from ..config import Config
from ..processing.generator import ProcessingResult
from ..web.jobs import PRIORITY_NORMAL
from .worker import Worker, WorkerPool

_submission_counter_lock = threading.Lock()
_submission_counter = 0


def _next_submission_order() -> int:
    """Return a monotonically increasing submission sequence number."""
    global _submission_counter
    with _submission_counter_lock:
        _submission_counter += 1
        return _submission_counter


class JobTracker:
    """Tracks progress for items belonging to a single job.

    Each job submitted to the dispatcher gets its own tracker that holds
    the item queue, completion counters, callbacks, and a done event.

    Args:
        job_id: Unique job identifier.
        items: List of :class:`ProcessableItem` instances.
        config: Config object for this job's processing.
        registry: Live :class:`ServerRegistry` — publishers fan out via this.
        title_max_width: Max display width for titles.
        library_name: Library name for log prefixes.
        callbacks: Dict of per-job callback functions.
        priority: Dispatch priority (1=high, 2=normal, 3=low).
    """

    def __init__(
        self,
        job_id: str,
        items: list,
        config: Config,
        registry,
        title_max_width: int = 20,
        library_name: str = "",
        callbacks: dict[str, Any] | None = None,
        priority: int = PRIORITY_NORMAL,
    ):
        """Initialize tracker for a single job."""
        self.job_id = job_id
        self.priority = priority
        self.submission_order = _next_submission_order()
        self.config = config
        self.registry = registry
        self.title_max_width = title_max_width
        self.library_name = library_name
        self.library_prefix = f"[{library_name}] " if library_name else ""

        # Two-stage queues (issue #243 unification). Every submitted item
        # first lands in ``check_queue``, where the dispatcher's checking
        # workers run the cheap ``process_canonical_path(check_only=True)``
        # pass. Items already fresh (or otherwise terminal) are recorded
        # straight away via ``record_check_result`` and never touch a
        # GPU/CPU processing worker. Items that genuinely need FFmpeg are
        # appended to ``item_queue``, which the processing workers drain
        # exactly as before. ``total_items`` counts every submitted item;
        # each one is recorded exactly once (check-skip OR processing).
        self.check_queue: deque = deque(items)
        self.item_queue: deque = deque()
        self.total_items = len(items)
        self.successful = 0
        self.failed = 0
        self.cancelled = False
        self.outcome_counts: dict[str, int] = {r.value: 0 for r in ProcessingResult}
        # D12 — per-server aggregate (one entry per server_id) so the Job
        # views render a fixed-size summary regardless of file count.
        # Per-file × per-server detail lives in the Files-panel JSONL via
        # record_file_result; duplicating it on the Job row caused the
        # Active Jobs and History sections to grow unbounded on big runs.
        self.publishers_aggregate: dict[str, dict] = {}
        self.done_event = threading.Event()
        # Flips True the first time one of this job's items is assigned to a
        # generation worker (set in JobDispatcher._assign_tasks). Drives the
        # "Checking existing previews…" → "completed" progress label so a
        # scan that's still sweeping doesn't read as if it's generating.
        self.generation_started = False
        # Guards every mutation of successful / failed / outcome_counts /
        # publishers_aggregate. Pre-#243 these were only ever touched by the
        # single dispatch-loop thread; the checking stage now folds outcomes
        # from up to ``scan_workers`` concurrent daemon check threads AND the
        # dispatch loop concurrently folds finished FFmpeg items — non-atomic
        # ``+=`` would lose increments, so ``completed`` could never reach
        # ``total_items`` and ``done_event`` would never fire (hung job).
        self._counts_lock = threading.Lock()

        cbs = callbacks or {}
        self.progress_callback: Callable | None = cbs.get("progress_callback")
        self.worker_callback: Callable | None = cbs.get("worker_callback")
        self.on_item_complete: Callable | None = cbs.get("on_item_complete")
        self.cancel_check: Callable | None = cbs.get("cancel_check")
        self.pause_check: Callable | None = cbs.get("pause_check")
        # Wired by JobDispatcher.submit_items so record_completion can
        # include the same in-flight fraction the periodic emitter uses.
        # Without this, two competing emit paths produced different
        # percent values for the same instant: the completion path saw
        # only ``completed/total`` while the 3s periodic path included
        # ``(completed + in_flight)/total`` — the bar visibly bounced
        # between e.g. 12% (completion path) and 30% (periodic path).
        self.in_progress_fraction_getter: Callable[[], float] | None = None

        # Throttle timestamps for callbacks
        self._last_progress_update = 0.0
        self._last_worker_update = 0.0
        # Set when all items are done; used by _cleanup_done_trackers
        self._done_at: float | None = None

    @property
    def completed(self) -> int:
        """Total items finished (success + failure)."""
        return self.successful + self.failed

    def is_paused(self) -> bool:
        """Check if this job's dispatch is paused."""
        if self.pause_check:
            return self.pause_check()
        return False

    def is_cancelled(self) -> bool:
        """Check if this job has been cancelled."""
        if self.cancel_check:
            return self.cancel_check()
        return False

    def record_completion(
        self,
        success: bool,
        worker_display_name: str = "",
        title: str = "",
    ) -> None:
        """Record a completed item and fire per-job callbacks.

        Args:
            success: Whether the item succeeded.
            worker_display_name: Display name of the worker for logging.
            title: Media title for logging.

        """
        # Increment under the lock and capture the post-increment completion
        # state atomically (the checking stage calls this from many threads).
        with self._counts_lock:
            if success:
                self.successful += 1
            else:
                self.failed += 1
            is_done = (self.successful + self.failed) >= self.total_items

        # A raising callback must never prevent done_event.set() below — that
        # would strand the job (hang). It also runs in the shared dispatch loop
        # thread (via _check_completions), so an unguarded raise there kills
        # the loop for every job. Swallow + log; completion always proceeds.
        try:
            if self.on_item_complete:
                self.on_item_complete(worker_display_name, title, success)

            if self.progress_callback:
                now = time.time()
                is_final = is_done
                if is_final or now - self._last_progress_update >= 0.5:
                    fraction = 0.0
                    if self.in_progress_fraction_getter is not None:
                        try:
                            fraction = self.in_progress_fraction_getter()
                        except Exception:
                            fraction = 0.0
                    effective = self.completed + fraction
                    percent = (effective / self.total_items * 100) if self.total_items > 0 else 0
                    # Until the first item claims a generation worker, the job
                    # is still sweeping the library for existing previews —
                    # surface that rather than "completed", which reads as
                    # generation. Flips to the normal label the moment
                    # generation begins (set in JobDispatcher._assign_tasks).
                    # Mirrors the label the multi-server scan path showed
                    # before the engines merged.
                    if self.generation_started:
                        msg = f"{self.library_prefix}{self.completed}/{self.total_items} completed"
                    else:
                        msg = f"{self.library_prefix}Checking existing previews… {self.completed}/{self.total_items}"
                    self.progress_callback(
                        self.completed,
                        self.total_items,
                        msg,
                        percent_override=percent,
                    )
                    self._last_progress_update = now
        except Exception as exc:
            logger.debug("Job {} completion callback raised (ignored): {}", self.job_id, exc)

        if is_done:
            self.done_event.set()

    def cancel(self) -> None:
        """Mark this job cancelled and drain remaining items.

        Drains BOTH the not-yet-checked queue and the checked-but-not-yet-
        generated queue so the failed tally covers every still-pending item.
        """
        self.cancelled = True
        with self._counts_lock:
            remaining = len(self.check_queue) + len(self.item_queue)
            self.check_queue.clear()
            self.item_queue.clear()
            if remaining:
                self.failed += remaining
        self.done_event.set()

    def record_check_result(self, item, result) -> None:
        """Record an item decided by the checking stage (no processing worker).

        ``result`` is the :class:`MultiServerResult` from a
        ``check_only=True`` call whose status is terminal (SKIPPED,
        PUBLISHED pending-registration, NO_OWNERS, SKIPPED_FILE_NOT_FOUND) —
        i.e. it did NOT need FFmpeg. Folds the same outcome counts +
        per-server publisher rows + Files-panel row + completion that
        ``_merge_worker_outcome`` + ``record_completion`` produce for the
        processing path (it does NOT call them — it writes the equivalent
        data inline), so accounting is identical regardless of which stage
        finished the item. Must run inside ``failure_scope(job_id)`` (the
        caller, ``_run_check``, provides it) so the Files-panel row routes to
        this job. No-op if the job is already done (e.g. cancelled mid-check).
        """
        if self.done_event.is_set():
            return

        # Lazy imports avoid a circular import at module load
        # (orchestrator imports the dispatcher singleton lazily too).
        from ..processing.generator import _notify_file_result
        from .orchestrator import (
            _outcome_for_multi_server_status,
            _publisher_rows_from_result,
            fold_publisher_rows_into_aggregate,
        )

        outcome = _outcome_for_multi_server_status(result.status)
        # Same coarse success rule the processing path uses
        # (Worker._process_item): only FAILED counts as a failure; SKIPPED /
        # NO_MEDIA_PARTS / SKIPPED_FILE_NOT_FOUND / pending all count as done.
        success = outcome is not ProcessingResult.FAILED

        # Publisher rows are best-effort UI attribution — never let a malformed
        # result block the count/completion below.
        try:
            rows = _publisher_rows_from_result(result, getattr(result, "canonical_path", "") or item.canonical_path)
        except Exception as exc:
            logger.debug("Could not build publisher rows for {}: {}", item.canonical_path, exc)
            rows = []
        # Counter + aggregate mutations under the lock (concurrent check threads).
        # The aggregate fold is guarded *inside* the lock so a raise there can't
        # propagate to _run_check's router AFTER the outcome counter has already
        # been bumped — that double-counts the item (re-queue → worker counts it
        # again), the exact "X processed / Y in outcome" divergence shape.
        with self._counts_lock:
            if outcome.value in self.outcome_counts:
                self.outcome_counts[outcome.value] += 1
            publishers_snapshot = None
            if rows:
                try:
                    fold_publisher_rows_into_aggregate(self.publishers_aggregate, rows)
                    publishers_snapshot = list(self.publishers_aggregate.values())
                except Exception as exc:
                    logger.debug("Could not fold publisher rows for {}: {}", item.canonical_path, exc)
        if publishers_snapshot is not None:
            try:
                from ..web.jobs import get_job_manager

                get_job_manager().set_publishers(self.job_id, publishers_snapshot)
            except Exception as exc:
                logger.debug("Could not set publisher aggregate for job {}: {}", self.job_id, exc)

        try:
            _notify_file_result(
                getattr(result, "canonical_path", "") or item.canonical_path,
                outcome,
                (getattr(result, "message", "") or "").strip(),
                "Library scan",
                servers=rows,
            )
        except Exception as exc:
            logger.debug("Could not notify checked file result for {}: {}", item.canonical_path, exc)

        title = getattr(item, "title", "") or item.canonical_path
        self.record_completion(success, "Library scan", title)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until all items are processed.

        Returns:
            True if completed, False if timed out.

        """
        return self.done_event.wait(timeout)

    def get_result(self) -> dict:
        """Return a result dict compatible with WorkerPool.process_items_headless."""
        return {
            "completed": self.successful,
            "failed": self.failed,
            "total": self.total_items,
            "cancelled": self.cancelled,
            "outcome": dict(self.outcome_counts),
        }


class JobDispatcher:
    """Coordinates item dispatch across multiple concurrent jobs.

    Owns a persistent WorkerPool and runs a background dispatch loop.
    Uses priority-aware drain-first scheduling: workers focus on the
    highest-priority active job and spill over to the next only when idle.

    Args:
        worker_pool: The shared WorkerPool instance.

    """

    def __init__(self, worker_pool: WorkerPool):
        """Initialize dispatcher with shared worker pool."""
        self.worker_pool = worker_pool
        self._trackers: dict[str, JobTracker] = {}
        self._trackers_lock = threading.RLock()
        self._dispatch_thread: threading.Thread | None = None
        self._has_work = threading.Event()
        self._shutdown = False
        # Signalled by worker threads on completion so the dispatch loop
        # wakes immediately instead of sleeping through the full cycle.
        self._worker_done = threading.Event()
        worker_pool._worker_done_event = self._worker_done
        # Backfill existing workers that were created before this event existed
        for w in worker_pool._snapshot_workers():
            w._done_event = self._worker_done
        # Snapshot of {worker_id: is_busy} from the previous emit pass —
        # used by _emit_worker_updates to detect state changes and
        # bypass the 1Hz throttle when needed (so sub-second tasks
        # don't flicker invisibly).
        self._last_worker_busy_snapshot: dict[int, bool] = {}

        # Checking stage (issue #243): a bounded pool of lightweight
        # "checking workers" that run process_canonical_path(check_only=True)
        # on submitted items BEFORE they can claim a (capped) GPU/CPU
        # processing worker. Decouples the fast "does a fresh preview exist?"
        # sweep from the heavy FFmpeg cap. Items already fresh are recorded
        # straight away; only items needing FFmpeg reach the processing pool.
        #
        # Driven by the dispatch loop (single priority-aware picker) which
        # spawns one short-lived **daemon** thread per check, capped at
        # ``_max_checks`` in flight. Daemon matters: a check that blocks on a
        # hung mount inside process_canonical_path (the same os.path.isfile
        # the processing worker hits) must NOT keep the process alive — the
        # legacy worker threads were daemon for exactly this reason. A
        # non-daemon pool (e.g. ThreadPoolExecutor) would leave a stuck check
        # blocking interpreter/xdist-worker shutdown ("not properly
        # terminated"). On-demand spawn means a 1-item webhook uses 1 thread,
        # not ``_max_checks``; threads exit when their check returns, so
        # nothing accumulates.
        self._max_checks = 0
        self._checks_in_flight = 0
        self._checks_in_flight_lock = threading.Lock()
        self._check_pool_started = False

    def submit_items(
        self,
        job_id: str,
        items: list,
        config: Config,
        registry,
        title_max_width: int = 20,
        library_name: str = "",
        callbacks: dict[str, Any] | None = None,
        priority: int = PRIORITY_NORMAL,
    ) -> JobTracker:
        """Submit items for a job to the shared dispatch queue.

        Args:
            job_id: Unique job identifier.
            items: List of :class:`ProcessableItem` instances.
            config: Configuration for processing these items.
            registry: Live :class:`ServerRegistry` — publishers fan out via this.
            title_max_width: Max title display width.
            library_name: Library name for log prefixes.
            callbacks: Dict with keys: progress_callback, worker_callback,
                on_item_complete, cancel_check, pause_check.
            priority: Dispatch priority (1=high, 2=normal, 3=low).

        Returns:
            JobTracker that callers can wait() on for completion.
        """
        tracker = JobTracker(
            job_id=job_id,
            items=items,
            config=config,
            registry=registry,
            title_max_width=title_max_width,
            library_name=library_name,
            callbacks=callbacks,
            priority=priority,
        )
        # Wire the in-flight fraction getter so JobTracker.record_completion
        # emits the same percent the dispatcher's periodic _emit_progress_updates
        # uses (completed + in_progress_fraction). Without this, the bar
        # bounced between the two paths' divergent values.
        tracker.in_progress_fraction_getter = lambda jid=job_id: self._get_in_progress_fraction(jid)
        with self._trackers_lock:
            self._trackers[job_id] = tracker
        logger.info(
            "Dispatcher: submitted {} items for job {} ({})", len(items), job_id[:8], library_name or "no library"
        )
        self._ensure_check_pool_running(config)
        self._has_work.set()
        self._ensure_dispatch_running()
        return tracker

    def _resolve_scan_workers(self, config: Config) -> int:
        """Resolve the checking-pool size from config.

        ``scan_workers`` 0 = Auto → ``max(32, processing workers)``, mirroring
        the orchestrator's resolution so the default lands at the same value
        regardless of which path created the dispatcher. An explicit value is
        floored at 1.
        """
        try:
            cfg = max(0, int(getattr(config, "scan_workers", 0) or 0))
        except (TypeError, ValueError):
            cfg = 0
        generators = max(1, len(self.worker_pool._snapshot_workers()))
        return cfg if cfg > 0 else max(32, generators)

    def _ensure_check_pool_running(self, config: Config) -> None:
        """Set the checking in-flight cap once, sized from ``scan_workers``."""
        if self._check_pool_started:
            return
        self._max_checks = self._resolve_scan_workers(config)
        self._check_pool_started = True
        logger.info("Dispatcher: checking enabled ({} max in-flight)", self._max_checks)

    def cancel_job(self, job_id: str) -> None:
        """Cancel a job's remaining items in the dispatch queue."""
        with self._trackers_lock:
            tracker = self._trackers.get(job_id)
        if tracker:
            tracker.cancel()
            logger.info("Dispatcher: cancelled job {}", job_id[:8])

    def shutdown(self) -> None:
        """Stop the dispatch loop + checking executor and shut down the pool."""
        self._shutdown = True
        self._has_work.set()  # Wake the dispatch loop so it can exit
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=30)
        # Checking threads are daemon + short-lived — no executor to drain.
        self.worker_pool.shutdown()

    # ------------------------------------------------------------------
    # Internal dispatch machinery
    # ------------------------------------------------------------------

    def _ensure_dispatch_running(self) -> None:
        """Start the background dispatch thread if not already running."""
        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="job-dispatcher")
        self._dispatch_thread.start()

    def _dispatch_loop(self) -> None:
        """Persistent loop: check completions, assign tasks, sleep adaptively.

        Uses ``_worker_done`` to wake immediately when a worker thread
        finishes, which is critical for fast-completing tasks like
        BIF-exists skips that would otherwise sit idle for the full
        sleep duration (~5 ms) per item.
        """
        logger.info("Dispatcher: dispatch loop started")
        last_progress_log = time.time()

        while not self._shutdown:
            # Wait until there's work to do (with periodic wake for housekeeping)
            self._has_work.wait(timeout=1.0)

            if self._shutdown:
                break

            # The whole iteration is guarded: this is the SHARED loop for every
            # job, so an unhandled exception in any step (a raising callback,
            # malformed worker state, a transient error) must not kill it —
            # that would hang every active and future job. Log and continue;
            # the next tick retries. Per-item failures are already isolated in
            # the worker/check threads, so this only catches engine-level slips.
            try:
                # 1. Handle cancelled jobs
                self._handle_cancellations()

                # 2. Check worker completions and route to trackers
                self._check_completions()

                # 2b. Feed the checking stage: sweep unchecked items (bounded,
                #     priority-aware). Already-fresh items are recorded here and
                #     never reach a processing worker; items needing FFmpeg land
                #     in the per-job item_queue that step 3 drains.
                self._submit_checks()

                # 3. Assign items to available workers BEFORE emitting
                #    updates so the first status emission reflects the newly
                #    busy worker instead of stale "idle" data.
                self._assign_tasks()

                # 4. Emit periodic worker status updates for all active jobs
                self._emit_worker_updates()

                # 5. Emit periodic progress updates for active jobs
                self._emit_progress_updates()

                # 6. Periodic progress logging
                now = time.time()
                if now - last_progress_log >= 5.0:
                    self._log_progress()
                    last_progress_log = now

                # 7. Clean up completed trackers and check if loop can idle
                self._cleanup_done_trackers()
                if self._all_idle():
                    self._has_work.clear()
            except Exception:
                logger.exception("Dispatcher: dispatch loop iteration failed; continuing")
                time.sleep(0.05)

            # Event-based sleep: wake immediately when any worker
            # completes instead of burning a fixed 5 ms.  Falls back
            # to a longer idle sleep when no workers are active.
            if self.worker_pool.has_busy_workers():
                self._worker_done.wait(timeout=0.005)
                self._worker_done.clear()
            else:
                time.sleep(0.01)

        logger.info("Dispatcher: dispatch loop exited")

    def _handle_cancellations(self) -> None:
        """Cancel trackers whose cancel_check returns True."""
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        for tracker in active:
            if tracker.is_cancelled() and not tracker.cancelled:
                tracker.cancel()
                logger.info(
                    "Dispatcher: job {} cancelled ({}/{} done)",
                    tracker.job_id[:8],
                    tracker.completed,
                    tracker.total_items,
                )

    def _check_completions(self) -> int:
        """Check all workers for completed tasks and update the owning tracker.

        Returns:
            Number of workers that completed since last check.
        """
        reaped = 0
        for worker in self.worker_pool._snapshot_workers():
            if not worker.check_completion():
                continue

            reaped += 1
            title = worker.media_title or "(unknown)"
            job_id = worker.current_job_id

            with self._trackers_lock:
                tracker = self._trackers.get(job_id) if job_id else None

            if tracker and not tracker.done_event.is_set():
                success = worker.last_task_succeeded()
                # Merge worker outcome counts into tracker
                self._merge_worker_outcome(worker, tracker)
                tracker.record_completion(success, worker.display_name, title)
            else:
                # No tracker for this item — just log
                success = worker.last_task_succeeded()
                outcome = "success" if success else "failed"
                logger.debug(
                    "Dispatcher: {} completed {} ({}) — no active tracker for job_id={}",
                    worker.display_name,
                    title,
                    outcome,
                    job_id,
                )

            # Retire deferred-removal workers
            self.worker_pool._retire_idle_worker_if_scheduled(worker)
        return reaped

    def _merge_worker_outcome(self, worker: Worker, tracker: JobTracker) -> None:
        """Merge the latest outcome delta from a worker into the tracker.

        Uses the pre-task baseline snapshot on the worker to compute which
        outcome counters changed during the most recent task.
        """
        delta = worker.last_task_outcome_delta()
        # Under the tracker lock: the checking stage now folds outcome_counts
        # from concurrent check threads, so this dispatch-loop fold must not
        # race them (non-atomic ``+=`` would lose increments → hung job).
        with tracker._counts_lock:
            for key, count in delta.items():
                if count > 0 and key in tracker.outcome_counts:
                    tracker.outcome_counts[key] += count
        # D12 — fold this task's per-server publisher rows into the
        # tracker's per-server aggregate (server_id → status counts) and
        # mirror that fixed-size summary onto the Job. The earlier D7
        # design appended one publisher row per (file × server), which
        # made job.publishers grow O(files × servers) — a 500-file
        # library run blew up the Active Jobs and History sections to
        # hundreds of rows. The per-file × per-server detail still lives
        # in the Files-panel JSONL (record_file_result `servers` field).
        # Shared helper so the full-scan ThreadPoolExecutor path
        # (orchestrator._dispatch_processable_items) cannot drift from
        # this aggregate shape — the original 1ecf099 fix patched only
        # this method and the full-scan path kept appending per-file.
        if worker.last_publishers:
            from .orchestrator import fold_publisher_rows_into_aggregate

            with tracker._counts_lock:
                fold_publisher_rows_into_aggregate(tracker.publishers_aggregate, worker.last_publishers)
                publishers_snapshot = list(tracker.publishers_aggregate.values())
            try:
                from ..web.jobs import get_job_manager

                get_job_manager().set_publishers(
                    tracker.job_id,
                    publishers_snapshot,
                )
            except Exception as exc:
                logger.debug(
                    "Could not set publisher aggregate for job {}: {}",
                    tracker.job_id,
                    exc,
                )

    def _assign_tasks(self) -> None:
        """Assign items from active jobs to available workers."""
        self.worker_pool._apply_deferred_removals()

        while True:
            # Atomic claim closes the race vs. _process_items_loop
            # (the worker pool's own consumer) — both used to find the
            # same idle worker and the loser tripped "already busy".
            worker = self.worker_pool._find_available_worker(claim=True)
            if not worker:
                break

            # Pull next item (highest priority, then oldest submission).
            picked = self._get_next_item()
            if not picked:
                # Nothing to do — release the pre-claim.
                worker.is_busy = False
                break

            job_id, item, library_name = picked
            with self._trackers_lock:
                tracker = self._trackers.get(job_id)
            if not tracker:
                # Tracker disappeared between pick and lookup — release the
                # pre-claim and try again with the next available worker.
                worker.is_busy = False
                continue

            # First item of this job to reach a generation worker — flip the
            # progress label off "Checking existing previews…".
            tracker.generation_started = True

            progress_callback = partial(self.worker_pool._update_worker_progress, worker)
            worker.assign_task(
                item,
                tracker.config,
                tracker.registry,
                progress_callback=progress_callback,
                title_max_width=tracker.title_max_width,
                job_id=job_id,
                library_name=library_name,
                cancel_check=tracker.cancel_check,
                pause_check=tracker.pause_check,
            )
            logger.info(
                "Dispatch: assigned canonical item {!r} (job {}) to {}",
                item.canonical_path,
                job_id[:8],
                worker.display_name,
            )

    def _get_next_check_item(self):
        """Pick the next item to CHECK, priority-aware, skipping paused jobs.

        Mirrors :meth:`_get_next_item` but drains ``check_queue`` (the
        pre-FFmpeg checking stage) instead of ``item_queue`` (processing).

        Returns ``(tracker, item)`` or ``None``.
        """
        with self._trackers_lock:
            eligible = [
                t
                for t in self._trackers.values()
                if not t.done_event.is_set() and not t.is_paused() and not t.is_cancelled() and t.check_queue
            ]
            eligible.sort(key=lambda t: (t.priority, t.submission_order))
            for tracker in eligible:
                item = tracker.check_queue.popleft()
                return (tracker, item)
        return None

    def _submit_checks(self) -> None:
        """Spawn checking tasks as bounded, short-lived daemon threads.

        Called from the dispatch loop. Picks the highest-priority unchecked
        item and runs it on a fresh daemon thread, up to ``_max_checks`` in
        flight. Priority-aware (single picker), bounded (in-flight cap),
        on-demand (one thread per concurrent check; none persist). Daemon so a
        check stuck on a hung mount can never block process shutdown.
        """
        if not self._check_pool_started:
            return
        while True:
            with self._checks_in_flight_lock:
                if self._checks_in_flight >= self._max_checks:
                    return
            picked = self._get_next_check_item()
            if picked is None:
                return
            tracker, item = picked
            with self._checks_in_flight_lock:
                self._checks_in_flight += 1
            threading.Thread(
                target=self._run_check_and_release,
                args=(tracker, item),
                name="job-checker",
                daemon=True,
            ).start()

    def _run_check_and_release(self, tracker: JobTracker, item) -> None:
        """Run one check then release its in-flight slot + wake the loop."""
        from .worker import unregister_job_thread

        try:
            self._run_check(tracker, item)
        finally:
            # Mirror the processing worker's per-thread cleanup so this
            # short-lived check thread's ident doesn't linger in the per-job
            # log-routing map after it exits.
            unregister_job_thread()
            self._on_check_done()

    def _on_check_done(self) -> None:
        with self._checks_in_flight_lock:
            self._checks_in_flight -= 1
        # Wake the dispatch loop so it can submit more checks, assign any
        # newly-queued processing items, and refresh progress/idle state.
        self._has_work.set()
        self._worker_done.set()

    def _run_check(self, tracker: JobTracker, item) -> None:
        """Run one ``check_only`` pass and route the outcome.

        Terminal results (already fresh, no-owners, source-missing,
        pending-registration) are recorded straight onto the tracker — no
        GPU/CPU processing worker is claimed. Items needing FFmpeg are
        appended to the tracker's processing queue. ``check_only=True`` is
        contractually guaranteed never to run FFmpeg, so generation only
        happens later under the capped processing workers.
        """
        # Re-check cancel/done right before any work so a job cancelled
        # between pick and run does no I/O.
        if tracker.is_cancelled() or tracker.done_event.is_set():
            return

        from ..processing.generator import failure_scope
        from ..processing.multi_server import MultiServerStatus, process_canonical_path
        from .worker import register_job_thread, resolve_per_item_pin

        # Register under the job so the per-job log captures the check's
        # Dispatch / Owners-resolved lines (mirrors the processing worker).
        register_job_thread(tracker.job_id)
        per_item_pin = resolve_per_item_pin(tracker.config, item, tracker.registry)
        # ``failure_scope`` (NOT register_job_thread) is what routes
        # ``_notify_file_result`` rows + ``record_failure`` to this job — the
        # processing worker wraps process_canonical_path the same way
        # (worker.py). Without it, a checked item's Files-panel row + any
        # failure land in the anonymous "" bucket and are dropped.
        with failure_scope(tracker.job_id):
            try:
                result = process_canonical_path(
                    canonical_path=item.canonical_path,
                    registry=tracker.registry,
                    config=tracker.config,
                    item_id_by_server=getattr(item, "item_id_by_server", None) or None,
                    bundle_metadata_by_server=getattr(item, "bundle_metadata_by_server", None) or None,
                    gpu=None,
                    gpu_device_path=None,
                    progress_callback=None,
                    cancel_check=tracker.cancel_check,
                    server_id_filter=per_item_pin,
                    regenerate=bool(getattr(tracker.config, "regenerate_thumbnails", False)),
                    check_only=True,
                )

                if result.status is MultiServerStatus.NEEDS_GENERATION:
                    # Guard: never re-queue into a job cancel() already drained.
                    if not tracker.is_cancelled() and not tracker.done_event.is_set():
                        tracker.item_queue.append(item)
                elif not tracker.done_event.is_set():
                    tracker.record_check_result(item, result)
            except Exception as exc:
                # ANYTHING the check stage raises — the cheap probe itself, an
                # unexpected/malformed result (e.g. no .status), or recording —
                # routes the item to a processing worker so the full path's
                # error handling / CPU fallback / retries apply (the same
                # conservative fallthrough the orchestrator uses). Crucially
                # this also means the item always *completes*: stranding it
                # here (the routing used to sit outside this guard) left
                # tracker.wait() blocking forever. Guarded so a job cancelled
                # mid-check doesn't re-populate a queue cancel() already drained.
                logger.debug(
                    "Dispatcher: check raised for {!r} ({}: {}); routing to a processing worker.",
                    item.canonical_path,
                    type(exc).__name__,
                    exc,
                )
                if not tracker.is_cancelled() and not tracker.done_event.is_set():
                    tracker.item_queue.append(item)
                return

    def update_job_priority(self, job_id: str, priority: int) -> None:
        """Update the dispatch priority of a running job's tracker.

        Args:
            job_id: Job identifier.
            priority: New priority (1=high, 2=normal, 3=low).
        """
        with self._trackers_lock:
            tracker = self._trackers.get(job_id)
            if tracker:
                tracker.priority = priority

    def _get_next_item(self) -> tuple[str, Any, str] | None:
        """Get the next item using priority-aware drain-first scheduling.

        Picks from the highest-priority active job first (lowest number).
        Within the same priority, earlier submissions are preferred.

        Returns:
            ``(job_id, item, library_name)`` or ``None``. ``item`` is a
            :class:`ProcessableItem`; ``library_name`` always blank for now
            (the canonical-path flow doesn't carry a per-item library tag at
            dispatch time).
        """
        with self._trackers_lock:
            eligible = [
                t
                for t in self._trackers.values()
                if not t.done_event.is_set() and not t.is_paused() and not t.is_cancelled() and t.item_queue
            ]
            eligible.sort(key=lambda t: (t.priority, t.submission_order))
            for tracker in eligible:
                item = tracker.item_queue.popleft()
                return (tracker.job_id, item, "")
        return None

    def _emit_worker_updates(self) -> None:
        """Emit worker status updates for all active trackers.

        Normally 1Hz throttled to keep SocketIO traffic light, BUT when
        any worker has changed state (busy↔idle) since the last emit,
        we override the throttle and emit immediately. Without the
        override, sub-second tasks (skip-cached BIF exists, frame-cache
        hits) flickered "processing → idle" inside a single 1Hz window
        and the user saw NO worker activity at all — the user-flagged
        symptom: "I see progress sometimes but not for this job."
        """
        now = time.time()
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        # Build a snapshot of (worker_id → is_busy) so we can detect
        # state changes regardless of which worker is doing what for
        # which tracker. Cheap — same lock as the worker_pool snapshot.
        current_busy = {w.worker_id: bool(w.is_busy) for w in self.worker_pool._snapshot_workers()}
        state_changed = current_busy != self._last_worker_busy_snapshot
        for tracker in active:
            throttle_ok = now - tracker._last_worker_update >= 1.0
            if tracker.worker_callback and (throttle_ok or state_changed):
                worker_statuses = self._build_worker_statuses()
                tracker.worker_callback(worker_statuses)
                tracker._last_worker_update = now
        # Save the new state for the next loop iteration's diff.
        self._last_worker_busy_snapshot = current_busy

    def _emit_progress_updates(self) -> None:
        """Emit periodic progress updates for active trackers.

        Factors in per-file progress from busy workers so the job-level
        percentage reflects in-progress work instead of staying at 0%
        until the first file completes.
        """
        now = time.time()
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        for tracker in active:
            if tracker.progress_callback and now - tracker._last_progress_update >= 3.0:
                # Include fractional progress from workers actively
                # processing items for this job.
                in_progress_fraction = self._get_in_progress_fraction(tracker.job_id)
                effective = tracker.completed + in_progress_fraction
                percent = (effective / tracker.total_items * 100) if tracker.total_items > 0 else 0
                tracker.progress_callback(
                    tracker.completed,
                    tracker.total_items,
                    f"{tracker.library_prefix}{tracker.completed}/{tracker.total_items} completed",
                    percent_override=percent,
                )
                tracker._last_progress_update = now

    def _get_in_progress_fraction(self, job_id: str) -> float:
        """Sum fractional progress of workers busy on a specific job.

        Each busy worker contributes its per-file progress as a fraction
        of one item (e.g. a worker at 60% contributes 0.6).

        Args:
            job_id: Job identifier to match against workers.

        Returns:
            Sum of fractional item progress across busy workers.
        """
        fraction = 0.0
        for worker in self.worker_pool._snapshot_workers():
            with self.worker_pool._progress_lock:
                is_busy = worker.is_busy
                wjob = worker.current_job_id
                pct = worker.progress_percent
            if is_busy and wjob == job_id and pct > 0:
                fraction += pct / 100.0
        return fraction

    def _build_worker_statuses(self) -> list[dict]:
        """Build the worker status list for the worker_callback."""
        all_workers = self.worker_pool._snapshot_workers()

        type_counters: dict[str, int] = {}
        worker_type_index: dict[int, int] = {}
        for w in all_workers:
            type_counters[w.worker_type] = type_counters.get(w.worker_type, 0) + 1
            worker_type_index[w.worker_id] = type_counters[w.worker_type]

        statuses = []
        for worker in all_workers:
            with self.worker_pool._progress_lock:
                progress_data = worker.get_progress_data()
                is_busy = worker.is_busy

            idx = worker_type_index[worker.worker_id]
            # Shared label helper — keeps this dispatcher's rows visually
            # identical to the multi-server dispatcher's rows and to the
            # synthesised idle entries returned by /api/jobs/workers when
            # no job is active.
            from .worker_naming import cpu_worker_label, friendly_device_label, gpu_worker_label

            if worker.worker_type == "GPU":
                device_label = friendly_device_label(
                    {"name": worker.gpu_name or ""},
                    worker.gpu_device,
                    worker.worker_type,
                )
                display_name = gpu_worker_label(idx, device_label)
            else:
                display_name = cpu_worker_label(idx)

            statuses.append(
                {
                    "worker_id": worker.worker_id,
                    "worker_type": worker.worker_type,
                    "worker_name": display_name,
                    "status": "processing" if is_busy else "idle",
                    "current_title": worker.media_title if is_busy else "",
                    "library_name": worker.library_name if is_busy else "",
                    "progress_percent": (progress_data["progress_percent"] if is_busy else 0),
                    "speed": progress_data["speed"] if is_busy else "0.0x",
                    "remaining_time": (progress_data["remaining_time"] if is_busy else 0.0),
                    "fallback_active": bool(getattr(worker, "fallback_active", False)),
                    "fallback_reason": getattr(worker, "fallback_reason", None),
                    # ffmpeg_started + current_phase drive the UI's pre-FFmpeg
                    # branch. When the dispatcher dropped these (the legacy
                    # process_items_headless path emitted them, this one did
                    # not) every dispatcher-driven job got stuck rendering
                    # "Working…" and hid the speed/ETA chips for the entire
                    # run — user-reported "I never see ffmpeg %/speed".
                    "ffmpeg_started": bool(getattr(worker, "ffmpeg_started", False)) if is_busy else False,
                    "current_phase": (getattr(worker, "current_phase", "") or "") if is_busy else "",
                }
            )
        return statuses

    def _cleanup_done_trackers(self) -> None:
        """Remove trackers that have been done for a while to free memory.

        Keeps done trackers for 60 seconds so callers can still read
        results via ``get_result()`` before they are garbage-collected.
        """
        with self._trackers_lock:
            done_ids = [jid for jid, t in self._trackers.items() if t.done_event.is_set()]
            for jid in done_ids:
                still_referenced = any(
                    w.current_job_id == jid and w.is_busy for w in self.worker_pool._snapshot_workers()
                )
                if not still_referenced:
                    tracker = self._trackers[jid]
                    if tracker._done_at is None:
                        tracker._done_at = time.time()
                    elif time.time() - tracker._done_at > 60:
                        del self._trackers[jid]

    def _all_idle(self) -> bool:
        """Check if there is no work left (no items, no busy workers)."""
        if self.worker_pool.has_busy_workers():
            return False
        with self._checks_in_flight_lock:
            if self._checks_in_flight > 0:
                return False
        with self._trackers_lock:
            for tracker in self._trackers.values():
                if not tracker.done_event.is_set():
                    return False
        return True

    def _log_progress(self) -> None:
        """Log aggregate progress across all active jobs."""
        with self._trackers_lock:
            active = [t for t in self._trackers.values() if not t.done_event.is_set()]
        if not active:
            return
        for tracker in active:
            pct = int(tracker.completed / tracker.total_items * 100) if tracker.total_items > 0 else 0
            logger.info(
                "Dispatcher progress: job {} {}/{} ({}%)",
                tracker.job_id[:8],
                tracker.completed,
                tracker.total_items,
                pct,
            )


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_dispatcher: JobDispatcher | None = None
_dispatcher_lock = threading.Lock()


def get_dispatcher(worker_pool: WorkerPool | None = None) -> JobDispatcher | None:
    """Get or create the global JobDispatcher singleton.

    Args:
        worker_pool: Required on first call to create the dispatcher.
            Subsequent calls ignore this argument.

    Returns:
        The global JobDispatcher, or None if no pool has been provided yet.

    """
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            if worker_pool is None:
                return None
            _dispatcher = JobDispatcher(worker_pool)
            logger.info("Created global JobDispatcher")
        return _dispatcher


def reset_dispatcher() -> None:
    """Reset the global dispatcher (for testing)."""
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is not None:
            _dispatcher.shutdown()
        _dispatcher = None
