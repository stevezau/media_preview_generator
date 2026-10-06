"""Priority-aware concurrency gate for job activation.

Caps how many jobs can be "actively working" (config-loading, enumerating
paths, submitting items to the dispatcher) at once. The worker pool
below is sized for FFmpeg parallelism; this gate sits above it and
prevents N+1 jobs from stampeding config/API/registry init when a
webhook burst or auto-requeue fires many jobs at once.

The gate sits between ``job_runner._start_job_async``'s daemon-thread
spawn and the ``run_processing(...)`` call — every job thread acquires
a slot before ``run_processing`` and releases in the outer finally.
Jobs that can't acquire stay in ``JobStatus.PENDING`` with a visible
``current_item="Queued — waiting for active slot (X of Y busy)"`` until
a peer finishes.

Design choices:

* **threading.Condition + priority heap** over BoundedSemaphore (FIFO,
  uninterruptible, immutable cap), over ThreadPoolExecutor (no priority,
  no waiter visibility, immutable max_workers), over external brokers
  (requires Redis/RabbitMQ — overkill for a single-container app).
* **Priority by ``job.priority``** (1=high, 2=normal, 3=low) — matches
  the dispatcher's existing priority semantics so a Sonarr-webhook job
  can jump past a scheduled full-scan, while the cap still bounds the
  total concurrency.
* **One slot reserved for high priority** (issue #285) — a full-library
  scan runs for hours, so "first in the waiting line" is worthless to an
  incoming webhook once every slot is held by a long scan. Normal/low
  jobs are therefore admitted only while ``active_non_high < cap - 1``;
  the last slot stays free for priority-1 work. Note the ``non_high``:
  high-priority slots are counted separately, so a running webhook job
  does NOT cost a scan its slot — at cap=3, one high plus two normal is
  a legal steady state. Total in-flight never exceeds the user's cap
  (unlike exempting high priority from it, which would reinstate the
  very stampede this gate exists to prevent). The reservation is skipped
  at ``cap == 1``, where it would starve normal-priority jobs completely.
* **Cap read on every wake** via ``cap_provider`` callable — the user
  can change ``max_concurrent_jobs`` in Settings and the new value
  takes effect immediately, no restart.
"""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Callable

from loguru import logger

from .jobs import PRIORITY_HIGH, PRIORITY_NORMAL

#: Slots held back for priority-1 jobs when the cap allows it (see the
#: module docstring). Kept as a constant so the gate, the wait message,
#: and the tests all agree on the size of the reservation.
HIGH_PRIORITY_RESERVED_SLOTS = 1


def format_wait_message(active: int, cap: int, effective_cap: int) -> str:
    """Render the ``current_item`` text shown while a job waits at the gate.

    Shared by both gate call sites in ``job_runner`` so the dashboard
    never shows two different phrasings for the same state.

    Args:
        active: Jobs currently holding a slot.
        cap: The user's ``max_concurrent_jobs`` setting.
        effective_cap: The cap this particular waiter is subject to —
            lower than ``cap`` for anything below high priority.

    Returns:
        A message explaining the wait, naming the reservation only when
        that is what's actually holding this waiter back. When the gate
        is genuinely full (``active >= cap``) the reservation is not the
        reason and mentioning it would send the user to the wrong knob.
    """
    if active < cap and effective_cap < cap:
        return f"Queued — waiting for active slot ({active} of {cap} busy, {cap - effective_cap} reserved for high priority)"
    return f"Queued — waiting for active slot ({active} of {cap} busy)"


class JobGate:
    """Priority-aware concurrency gate with runtime-adjustable cap.

    ``acquire(priority, cancel_check, on_wait)`` blocks until the caller
    is admitted (returns True) or ``cancel_check()`` returns True
    (returns False, no slot consumed). ``release(priority)`` admits the
    next waiter by priority, then FIFO within the same priority.

    The cap is read from ``cap_provider`` on every wake, so setting
    changes take effect within one poll tick (1s) without a restart.
    Values outside ``[1, 10]`` are clamped; non-int values fall back
    to 3. Three layers of defense (this clamp, the settings POST
    validator, the settings GET response clamp) keep the runtime
    value in sync with the UI.
    """

    _CAP_MIN = 1
    _CAP_MAX = 10
    _CAP_DEFAULT = 3
    _POLL_SECONDS = 1.0  # How often a waiter re-checks cancel_check.

    def __init__(
        self, cap_provider: Callable[[], int], kind_capacity_provider: Callable[[str], int | None] | None = None
    ) -> None:
        self._cap_provider = cap_provider
        self._kind_capacity_provider = kind_capacity_provider
        self._kind_limits: dict[str, int | None] = {}
        self._kind_active: dict[tuple[str, int], int] = {}
        self._waiter_kinds: dict[object, str | None] = {}
        self._capacity_errors: set[str] = set()
        self._cond = threading.Condition()
        self._active = 0
        # Slots held by priority-1 jobs. Tracked separately so a running
        # high-priority job doesn't eat into the normal-priority budget:
        # at cap=3, one high + two normal jobs is a legal steady state.
        self._active_high = 0
        self._heap: list[tuple[int, int, object]] = []  # (priority, seq, token)
        self._seq = itertools.count()

    def _cap(self) -> int:
        try:
            return max(self._CAP_MIN, min(self._CAP_MAX, int(self._cap_provider())))
        except (TypeError, ValueError):
            return self._CAP_DEFAULT

    def effective_cap(self, priority: int, cap: int | None = None) -> int:
        """The admission ceiling a job of ``priority`` is subject to.

        High priority sees the full cap; everything else sees the cap
        minus the reservation, floored at 1 so a cap of 1 still admits
        normal work.

        Args:
            priority: Dispatch priority (1=high, 2=normal, 3=low).
            cap: Cap to derive from; read from the provider when omitted.

        Returns:
            The maximum concurrency available to that priority.
        """
        cap = self._cap() if cap is None else cap
        if priority <= PRIORITY_HIGH:
            return cap
        return max(1, cap - HIGH_PRIORITY_RESERVED_SLOTS)

    def _can_admit(self, priority: int, cap: int) -> bool:
        """Whether a waiter at ``priority`` may take a slot right now.

        Two ceilings apply, and both must hold: the hard total cap (so
        the reservation can never push concurrency above what the user
        asked for) and, for non-high priority, the count of *non-high*
        slots against the reduced budget.
        """
        if self._active >= cap:
            return False
        if priority <= PRIORITY_HIGH:
            return True
        return (self._active - self._active_high) < self.effective_cap(priority, cap)

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

    def _kind_can_admit(self, kind: str | None, priority: int) -> bool:
        if kind is None:
            return True
        limit = self._kind_limits.get(kind, 0)
        if limit is None:
            return True
        if priority > PRIORITY_HIGH:
            ordinary = sum(
                n
                for (held_kind, held_priority), n in self._kind_active.items()
                if held_kind == kind and held_priority > PRIORITY_HIGH
            )
            # Preserve ordinary room for another kind. A strictly higher
            # priority may escape this ceiling: at the default cap of three,
            # a normal webhook must still reach a low-priority scan's worker.
            ceiling = max(1, self.effective_cap(priority) - 1)
            higher_priority_escape = ordinary and not any(
                n and held_kind == kind and PRIORITY_HIGH < held_priority <= priority
                for (held_kind, held_priority), n in self._kind_active.items()
            )
            if ordinary >= ceiling and not higher_priority_escape:
                return False
        # Higher-priority work must still reach the dispatcher's item-level
        # priority queue while a long lower-priority scan holds admission.
        active = sum(
            n
            for (held_kind, held_priority), n in self._kind_active.items()
            if held_kind == kind and held_priority <= priority
        )
        return active < limit

    def _next_eligible(self, cap: int) -> object | None:
        eligible = (
            entry
            for entry in self._heap
            if self._can_admit(entry[0], cap) and self._kind_can_admit(self._waiter_kinds[entry[2]], entry[0])
        )
        return min(eligible, default=(0, 0, None))[2]

    def acquire(
        self,
        priority: int,
        cancel_check: Callable[[], bool],
        on_wait: Callable[[int, int, int], None] | None = None,
        *,
        kind: str | None = None,
        on_resource_wait: Callable[[], None] | None = None,
    ) -> bool:
        """Admit by priority/FIFO among jobs with compatible worker capacity.

        Kind limits count equal-or-higher-priority admissions. This bounds
        same-priority contention without preventing urgent work from reaching
        the shared dispatcher. Global limits and the HIGH reservation still
        apply. Release with the same captured priority and kind.

        Capacity reads and wait callbacks run outside the condition lock.
        Unknown legacy capacity is unlimited; zero or unreadable capacity waits.
        A cancelled wait removes its queued entry without consuming a slot.
        """
        token = object()
        with self._cond:
            heapq.heappush(self._heap, (priority, next(self._seq), token))
            self._waiter_kinds[token] = kind
        try:
            while True:
                limit = self._kind_limit(kind)
                with self._cond:
                    if kind is not None:
                        self._kind_limits[kind] = limit
                    cap = self._cap()
                    if self._next_eligible(cap) is token:
                        self._heap = [entry for entry in self._heap if entry[2] is not token]
                        heapq.heapify(self._heap)
                        self._active += 1
                        if priority <= PRIORITY_HIGH:
                            self._active_high += 1
                        if kind is not None:
                            key = (kind, priority)
                            self._kind_active[key] = self._kind_active.get(key, 0) + 1
                        return True
                    if cancel_check():
                        return False
                    resource_blocked = not self._kind_can_admit(kind, priority)
                    active, effective_cap = self._active, self.effective_cap(priority, cap)
                if resource_blocked and on_resource_wait is not None:
                    on_resource_wait()
                elif on_wait is not None:
                    on_wait(active, cap, effective_cap)
                with self._cond:
                    # A release during a callback must not cost an extra poll.
                    if self._next_eligible(self._cap()) is token:
                        continue
                    self._cond.wait(timeout=self._POLL_SECONDS)
        finally:
            with self._cond:
                self._heap = [entry for entry in self._heap if entry[2] is not token]
                heapq.heapify(self._heap)
                self._waiter_kinds.pop(token, None)
                self._cond.notify_all()

    def release(self, priority: int = PRIORITY_NORMAL, *, kind: str | None = None) -> None:
        """Release a slot and wake every waiter so the priority-heap
        head can admit itself.

        Args:
            priority: The priority the slot was ACQUIRED at. Callers
                must pass the value they captured before ``acquire``,
                not a live read of ``job.priority`` — the user can
                re-prioritise a running job from the UI, and settling
                up with the changed value would corrupt the high-slot
                counter.

        ``notify_all`` (rather than ``notify``) is required because the
        heap head may have cancelled between our notify and its wake —
        we can't pick "the right waiter" ourselves; each waiter has to
        re-evaluate its own eligibility.
        """
        with self._cond:
            self._active = max(0, self._active - 1)
            if kind is not None:
                key = (kind, priority)
                self._kind_active[key] = max(0, self._kind_active.get(key, 0) - 1)
            if priority <= PRIORITY_HIGH:
                self._active_high = max(0, self._active_high - 1)
            self._cond.notify_all()

    def snapshot(self) -> tuple[int, int, int]:
        """Return ``(active_count, waiting_count, cap)`` for observability.

        All three fields are captured under the same lock so the UI
        never sees a torn read where ``active + waiting`` briefly
        exceeds the total job count.
        """
        with self._cond:
            return (self._active, len(self._heap), self._cap())


_gate: JobGate | None = None
_gate_lock = threading.Lock()


def get_job_gate() -> JobGate:
    """Return the process-wide ``JobGate`` singleton.

    Lazy-initialised so tests that never spin up the web app don't
    pay for gate construction. The cap is pulled from
    ``settings_manager`` via a closure so every ``_cap()`` call reads
    the user's current setting — no reload, no restart.
    """
    global _gate
    with _gate_lock:
        if _gate is None:
            from ..jobs.group_runtime import admission_capacity
            from .settings_manager import get_settings_manager

            _gate = JobGate(
                lambda: get_settings_manager().get("max_concurrent_jobs", 3), kind_capacity_provider=admission_capacity
            )
        return _gate


def reset_job_gate() -> None:
    """Drop the singleton. Tests only.

    Required because the gate holds a settings-manager closure; a test
    that swaps the settings manager singleton needs a fresh gate so
    cap reads see the new manager.
    """
    global _gate
    with _gate_lock:
        _gate = None
