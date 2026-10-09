"""Priority-aware gate on job start-up.

Bounds how many jobs can be "starting up" (config load, server queries, building the file list) at once, so a
webhook burst does not hammer Plex/Emby/Jellyfin with dozens of simultaneous enumerations. A job holds a slot only
until its files are submitted to the dispatcher; from then on the dispatcher's priority order and the worker pool
decide how much runs at once.

The gate sits between ``job_runner._start_job_async``'s daemon-thread spawn and the work that enumerates files.
Jobs that can't acquire stay in ``JobStatus.PENDING`` with a visible
``current_item="Queued — waiting to start (X of 3 jobs starting up)"`` until a peer has submitted its files.

Design choices:

* **threading.Condition + priority heap** over BoundedSemaphore (FIFO, uninterruptible), over ThreadPoolExecutor
  (no priority, no waiter visibility), over external brokers (requires Redis/RabbitMQ — overkill for a single-container
  app).
* **Priority by ``job.priority``** (1=high, 2=normal, 3=low) — matches the dispatcher's priority semantics so a
  Sonarr-webhook job starts up ahead of a scheduled full-scan, then FIFO within a priority.
* **A kind with no open workers waits** — a job whose workers are all disabled or off-hours does not take a start-up
  slot it could not use. Running jobs are never preempted.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

#: How many jobs may be starting up at once. Fixed, not a setting: it only protects the media servers from a burst
#: of simultaneous enumerations, and a job gives its slot back as soon as its files are submitted.
STARTUP_SLOTS = 3


@dataclass
class _Request:
    token: object
    order: tuple[str, str]
    priority: int
    kind: str
    policy: Callable[[bool], tuple[int, bool] | None]
    owner: object | None = None
    revision: int = 0
    ready: bool = False
    preflight_complete: bool = False
    preflight_waiting: bool = False


def format_wait_message(active: int) -> str:
    """Render the ``current_item`` text shown while a job waits at the gate.

    Shared by every gate call site so the dashboard never shows two different phrasings for the same state.

    Args:
        active: Jobs currently holding a start-up slot.

    Returns:
        The message explaining the wait.
    """
    return f"Queued — waiting to start ({active} of {STARTUP_SLOTS} jobs starting up)"


def release_slot(slot: dict, gate: JobGate) -> None:
    """Give back the start-up slot a runner holds, once; a slot it no longer holds is left alone.

    Args:
        slot: The runner's ``{"held": bool}`` record of whether it holds a slot.
        gate: The gate the slot was taken from.
    """
    if not slot["held"]:
        return
    slot["held"] = False
    try:
        gate.release()
    except Exception as exc:
        logger.debug("Could not release job gate: {}", exc)


class JobGate:
    """Priority-aware gate on job start-up, ``STARTUP_SLOTS`` wide.

    ``acquire(priority, cancel_check, on_wait)`` blocks until the caller is admitted (returns True) or
    ``cancel_check()`` returns True (returns False, no slot consumed). ``release()`` admits the next waiter by
    priority, then FIFO within the same priority.
    """

    _POLL_SECONDS = 1.0  # How often a waiter re-checks cancel_check.

    def __init__(self, kind_capacity_provider: Callable[[str], int | None] | None = None) -> None:
        self._kind_capacity_provider = kind_capacity_provider
        self._kind_limits: dict[str, int | None] = {}
        self._waiter_kinds: dict[object, str | None] = {}
        self._capacity_errors: set[str] = set()
        self._cond = threading.Condition()
        self._active = 0
        self._heap: list[tuple[int, tuple[str, str], object]] = []
        self._seq = itertools.count()
        self._requests: dict[str, _Request] = {}
        self._refresh_lock = threading.Lock()
        self._refresh_at = 0.0
        self._refreshed_epoch = -1
        self._policy_epoch = 0

    def register_request(
        self,
        job_id: str,
        *,
        created_at: str,
        priority: int,
        kind: str,
        policy: Callable[[bool], tuple[int, bool] | None],
    ) -> None:
        """Reserve queue order, without reserving capacity or starting a worker."""
        with self._cond:
            if job_id in self._requests:
                return
            request = _Request(object(), (created_at, job_id), priority, kind, policy)
            self._requests[job_id] = request
            self._waiter_kinds[request.token] = kind
            heapq.heappush(self._heap, (priority, request.order, request.token))
            self._policy_epoch += 1
            self._cond.notify_all()

    def claim_request(self, job_id: str) -> object | None:
        """Give cleanup ownership to the runner that won its in-flight claim."""
        with self._cond:
            request = self._requests.get(job_id)
            if request is None or request.owner is not None:
                return None
            request.owner = object()
            return request.owner

    def finish_request(self, job_id: str, owner: object | None) -> None:
        """Remove only this runner's request; a duplicate start cannot remove it."""
        with self._cond:
            request = self._requests.get(job_id)
            if request is None or request.owner is not owner:
                return
            del self._requests[job_id]
            self._heap = [entry for entry in self._heap if entry[2] is not request.token]
            heapq.heapify(self._heap)
            self._waiter_kinds.pop(request.token, None)
            self._policy_epoch += 1
            self._cond.notify_all()

    def has_request(self, job_id: str) -> bool:
        with self._cond:
            return job_id in self._requests

    def complete_preflight(self, job_id: str) -> None:
        """The runner has honored its dependency/deadline, including operator overrides."""
        with self._cond:
            request = self._requests.get(job_id)
            if request is not None:
                request.preflight_complete = True
                request.preflight_waiting = False
                request.revision += 1
                self._policy_epoch += 1
            self._cond.notify_all()

    def defer_preflight(self, job_id: str) -> None:
        """An actual runner wait, including a locally extended deadline, is not ready."""
        with self._cond:
            request = self._requests.get(job_id)
            if request is not None:
                request.preflight_waiting = True
                request.ready = False
                request.revision += 1
                self._policy_epoch += 1
            self._cond.notify_all()

    def reprioritize(self, job_id: str, priority: int) -> None:
        """Wake an existing waiter without changing the priority of a held slot."""
        with self._cond:
            request = self._requests.get(job_id)
            if request is not None:
                # Take the new place in line now, not at the next refresh: a slot freed in between would otherwise go
                # to a waiter this job just overtook. Readiness doesn't depend on priority, so it stays as is.
                request.priority = priority
                request.revision += 1
                self._heap = [
                    (priority, order, token) if token is request.token else (entry_priority, order, token)
                    for entry_priority, order, token in self._heap
                ]
                heapq.heapify(self._heap)
                self._policy_epoch += 1
            self._cond.notify_all()

    def _refresh_requests(self) -> None:
        # All polling threads share one bounded snapshot. No manager/settings
        # callback runs under the gate condition or once per waiting thread.
        with self._refresh_lock:
            self._refresh_requests_once()

    def _refresh_requests_once(self) -> None:
        with self._cond:
            epoch = self._policy_epoch
            if time.monotonic() < self._refresh_at and self._refreshed_epoch == epoch:
                return
            queued = {entry[2] for entry in self._heap}
            snapshots = [(request, request.revision) for request in self._requests.values() if request.token in queued]
        limits = {kind: self._kind_limit(kind) for kind in {request.kind for request, _ in snapshots}}
        updates = []
        for request, revision in snapshots:
            try:
                policy = request.policy(request.preflight_complete)
                priority, ready = policy if policy is not None else (request.priority, False)
            except Exception:
                policy = (request.priority, False)
                priority, ready = request.priority, False
            updates.append((request, revision, priority, ready, policy is None))
        with self._cond:
            current = {request.token: request for request in self._requests.values()}
            self._kind_limits.update(limits)
            retired = set()
            for request, revision, priority, ready, terminal in updates:
                if current.get(request.token) is not request:
                    continue
                if terminal and request.owner is None and request.revision == revision:
                    retired.add(request.token)
                    self._waiter_kinds.pop(request.token, None)
                    continue
                if request.revision == revision:
                    request.priority = priority
                    request.ready = ready and not request.preflight_waiting
            self._heap = [
                (current[token].priority, order, token) if token in current else (priority, order, token)
                for priority, order, token in self._heap
                if token not in retired
            ]
            self._requests = {
                job_id: request for job_id, request in self._requests.items() if request.token not in retired
            }
            heapq.heapify(self._heap)
            self._refreshed_epoch = epoch
            self._refresh_at = time.monotonic() + 0.1

    def _kind_limit(self, kind: str | None) -> int | None:
        """Read live policy outside the gate lock; unavailable policy admits no work."""
        if kind is None or self._kind_capacity_provider is None:
            return None
        try:
            value = self._kind_capacity_provider(kind)
            limit = None if value is None else max(0, int(value))
        except Exception:
            if kind not in self._capacity_errors:
                logger.warning("Could not read {} worker capacity; keeping its jobs queued", kind)
                self._capacity_errors.add(kind)
            return 0
        self._capacity_errors.discard(kind)
        return limit

    def _kind_can_admit(self, kind: str | None) -> bool:
        if kind is None:
            return True
        limit = self._kind_limits.get(kind, 0)
        return limit is None or limit > 0

    def _next_eligible(self) -> object | None:
        if self._active >= STARTUP_SLOTS:
            return None
        readiness = {request.token: request.ready for request in self._requests.values()}
        eligible = [
            entry
            for entry in self._heap
            if readiness.get(entry[2], True) and self._kind_can_admit(self._waiter_kinds[entry[2]])
        ]
        return min(eligible, default=(0, 0, None))[2]

    def acquire(
        self,
        priority: int,
        cancel_check: Callable[[], bool],
        on_wait: Callable[[int], None] | None = None,
        *,
        kind: str | None = None,
        on_resource_wait: Callable[[], None] | None = None,
        request_id: str | None = None,
    ) -> bool:
        """Admit by priority, then FIFO, among jobs whose kind has open workers.

        Capacity reads and wait callbacks run outside the condition lock.
        Unknown legacy capacity is unlimited; zero or unreadable capacity waits.
        A cancelled wait removes its queued entry without consuming a slot.

        Args:
            priority: Dispatch priority (1=high, 2=normal, 3=low).
            cancel_check: True once the caller no longer wants a slot.
            on_wait: Called with the number of busy slots on each poll tick while the caller waits for a slot.
            kind: The job kind, for the open-workers check.
            on_resource_wait: Called instead of ``on_wait`` while the wait is for open workers.
            request_id: Job id of a request registered with :meth:`register_request`, which supplies the queue
                order and readiness policy.

        Returns:
            True once admitted; False if ``cancel_check`` fired first.
        """
        with self._cond:
            request = self._requests.get(request_id) if request_id is not None else None
            token = request.token if request is not None else object()
            order = request.order if request is not None else ("", f"{next(self._seq):020}")
            if not any(entry[2] is token for entry in self._heap):
                heapq.heappush(self._heap, (priority, order, token))
                self._policy_epoch += 1
            self._waiter_kinds[token] = kind
        try:
            while True:
                if request is not None and cancel_check():
                    return False
                self._refresh_requests()
                limit = self._kind_limit(kind) if request is None else None
                if request is not None and cancel_check():
                    return False
                with self._cond:
                    if kind is not None and request is None:
                        self._kind_limits[kind] = limit
                    if self._next_eligible() is token:
                        self._heap = [entry for entry in self._heap if entry[2] is not token]
                        heapq.heapify(self._heap)
                        self._active += 1
                        break
                    if cancel_check():
                        return False
                    resource_blocked = not self._kind_can_admit(kind)
                    active = self._active
                if resource_blocked and on_resource_wait is not None:
                    on_resource_wait()
                elif on_wait is not None:
                    on_wait(active)
                with self._cond:
                    # A release during a callback must not cost an extra poll.
                    if self._next_eligible() is token:
                        continue
                    self._cond.wait(timeout=self._POLL_SECONDS)
            return True
        finally:
            with self._cond:
                self._heap = [entry for entry in self._heap if entry[2] is not token]
                heapq.heapify(self._heap)
                self._waiter_kinds.pop(token, None)
                self._cond.notify_all()

    def release(self) -> None:
        """Release a slot and wake every waiter so the priority-heap head can admit itself.

        ``notify_all`` (rather than ``notify``) is required because the heap head may have cancelled between our
        notify and its wake — we can't pick "the right waiter" ourselves; each waiter has to re-evaluate its own
        eligibility.
        """
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify_all()

    def snapshot(self) -> tuple[int, int, int]:
        """Return ``(active_count, waiting_count, slots)`` for observability.

        All three fields are captured under the same lock so a reader never sees a torn read.
        """
        with self._cond:
            return (self._active, len(self._heap), STARTUP_SLOTS)


_gate: JobGate | None = None
_gate_lock = threading.Lock()


def get_job_gate() -> JobGate:
    """Return the process-wide ``JobGate`` singleton.

    Lazy-initialised so tests that never spin up the web app don't pay for gate construction.
    """
    global _gate
    with _gate_lock:
        if _gate is None:
            from ..jobs.group_runtime import admission_capacity

            _gate = JobGate(kind_capacity_provider=admission_capacity)
        return _gate


def reset_job_gate() -> None:
    """Drop the singleton. Tests only."""
    global _gate
    with _gate_lock:
        _gate = None
