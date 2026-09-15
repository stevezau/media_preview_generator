"""Header-driven pacing for online sources (spec §4 "Limits").

One limiter per source, shared by every job and thread. ``acquire`` reserves the next free time slot under a lock, so
concurrent check threads never burst past the per-window limit, then waits outside the lock in short polls so a
cancelled job stops promptly. A 429, a closed rate window or an open circuit that arrives while a thread waits moves
its slot (or refuses it) instead of letting it fire into the block.

Daily budgets come from ``x-usagelimit-*`` headers; LOW priority (backfill) stops at a 20% reserve so webhook-triggered
jobs keep first call on the day's quota. Requests reserved but not yet answered can overshoot a fresh server count by
at most the number of in-flight requests (a few), which the reserve absorbs.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import Enum

from loguru import logger

PRIORITY_LOW = 3  # mirrors web.jobs.PRIORITY_LOW; the markers package never imports web
_MAX_BLOCK_S = 3600.0
_DEFAULT_429_S = 300.0
_MIN_RETRY_S = 1.0
_WAIT_POLL_S = 0.5
# Reset/Retry-After values this large are absolute Unix times, not delta-seconds: some APIs send X-RateLimit-Reset as
# epoch seconds, and Retry-After may be an HTTP-date (RFC 9110). 1e9 s of delta would be 31 years.
_EPOCH_THRESHOLD_S = 1_000_000_000
# The daily budget always ends at the UTC day roll (see ``_roll_day``), never a source's own reset header — shown to
# users so they know when a "daily limit reached" state will clear.
RESET_TIME_LABEL = "00:00 UTC"
DEFAULT_RESERVE_FRACTION = 0.2


def _utc_day() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class Acquire(str, Enum):
    """Outcome of asking for a request slot."""

    ALLOWED = "allowed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


def _number(value: str | None) -> float | None:
    """A finite number from a header value, or None."""
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _delay_s(value: str | None, wall_now: float) -> float | None:
    """Seconds from now for a delta-seconds, Unix-time or HTTP-date header value (None when unparseable)."""
    number = _number(value)
    if number is None:
        if not value:
            return None
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        number = when.timestamp()
    if number >= _EPOCH_THRESHOLD_S:
        number -= wall_now
    return max(0.0, number)


def low_priority_exhausted(
    *, limit: int | None, remaining: int | None, reserve_fraction: float = DEFAULT_RESERVE_FRACTION
) -> bool:
    """Whether a LOW-priority (backfill) lookup would be refused right now.

    Mirrors the check in :meth:`SourceLimiter.acquire`: true once nothing at all is left, or once only the share
    reserved for higher-priority jobs remains. Exposed standalone so the Settings API can answer this for a merged
    (live + stored) usage snapshot without a live limiter instance.

    Args:
        limit: The source's daily limit, or None when never learned.
        remaining: What the source last said remains today, or None when never learned.
        reserve_fraction: Share of the daily limit LOW priority may not spend.

    Returns:
        False whenever ``remaining`` is unknown (nothing to be exhausted yet).
    """
    if remaining is None:
        return False
    return remaining <= 0 or (limit is not None and remaining <= reserve_fraction * limit)


class SourceLimiter:
    """Paces one online source."""

    def __init__(
        self,
        source_id: str,
        *,
        min_interval_s: float,
        reserve_fraction: float = DEFAULT_RESERVE_FRACTION,
        failure_threshold: int = 5,
        circuit_open_s: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utc_day: Callable[[], str] = _utc_day,
        on_usage: Callable[[str, str, int, int | None, int | None], None] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a limiter.

        Args:
            source_id: Source id for logs/usage.
            min_interval_s: Minimum spacing between requests.
            reserve_fraction: Share of the daily budget LOW priority may not use.
            failure_threshold: Consecutive network/5xx failures that open the circuit.
            circuit_open_s: How long an open circuit blocks requests.
            clock: Monotonic clock (tests inject a fake).
            sleep: Sleep function (tests inject a fake).
            utc_day: Returns the current UTC day string.
            on_usage: Called after every ``record()`` (responses and network errors) with
                (source, day, used, limit, remaining).
            wall_clock: Unix time, only for headers that carry an absolute time.
        """
        self.source_id = source_id
        self.min_interval_s = min_interval_s
        # Public like min_interval_s: the Settings API mirrors low_priority_exhausted() on merged (live + stored)
        # values and must use this source's actual share, not the module default, if one is ever set per source.
        self.reserve_fraction = reserve_fraction
        self._failure_threshold = failure_threshold
        self._circuit_open_s = circuit_open_s
        self._clock = clock
        self._sleep = sleep
        self._utc_day = utc_day
        self._on_usage = on_usage
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._blocked_until = 0.0
        self._failures = 0
        self._day = utc_day()
        self._used = 0
        self._limit: int | None = None
        self._remaining: int | None = None
        # Bumped whenever _remaining is replaced (server count, day roll) so a cancelled reservation only refunds the
        # count it decremented, never a fresher one.
        self._remaining_gen = 0
        self._usage_seq = 0
        self._usage_lock = threading.Lock()
        self._published_seq = 0
        # Per thread: the (day, remaining_gen) of the last ALLOWED slot that no record() has answered yet.
        self._local = threading.local()

    def _replace_remaining(self, value: int | None) -> None:
        self._remaining = value
        self._remaining_gen += 1

    def _roll_day(self) -> None:
        """Start a new UTC day's count. Caller holds the lock.

        The day, not ``x-usagelimit-reset``, ends the budget: TheIntroDB sends that header as ``0`` while 173 of 500
        requests remain (measured 2026-09-14), so it can't mean "seconds until reset".
        """
        day = self._utc_day()
        if day != self._day:
            self._day = day
            self._used = 0
            self._replace_remaining(None)

    def _refund(self, day: str, remaining_gen: int) -> None:
        """Give back a reservation that never sent a request. Caller holds the lock."""
        if day == self._day and self._used > 0:
            self._used -= 1
        if remaining_gen == self._remaining_gen and self._remaining is not None:
            self._remaining += 1

    def acquire(
        self, *, priority: int, cancel_check: Callable[[], bool] | None = None, max_wait_s: float = 60.0
    ) -> Acquire:
        """Reserve the next request slot, sleeping until it arrives.

        Args:
            priority: Job priority (1 high .. 3 low); LOW may not spend the reserved share of the daily budget.
            cancel_check: Polled while waiting; True abandons the slot.
            max_wait_s: Longest acceptable wait; a later slot returns BLOCKED at once.

        Returns:
            ALLOWED when the caller may send one request now; otherwise why not.
        """
        self._local.ticket = None
        if cancel_check is not None and cancel_check():
            return Acquire.CANCELLED
        with self._lock:
            self._roll_day()
            now = self._clock()
            if self._remaining is not None:
                if self._remaining <= 0:
                    return Acquire.BUDGET_EXHAUSTED
                if (
                    priority >= PRIORITY_LOW
                    and self._limit is not None
                    and self._remaining <= self.reserve_fraction * self._limit
                ):
                    return Acquire.BUDGET_EXHAUSTED
            deadline = now + max_wait_s
            slot = max(now, self._next_slot, self._blocked_until)
            if slot > deadline:
                return Acquire.BLOCKED
            self._next_slot = slot + self.min_interval_s
            self._used += 1
            if self._remaining is not None:
                self._remaining -= 1
            day, remaining_gen = self._day, self._remaining_gen
        return self._wait_for(slot, deadline, day, remaining_gen, cancel_check)

    def _wait_for(
        self, slot: float, deadline: float, day: str, remaining_gen: int, cancel_check: Callable[[], bool] | None
    ) -> Acquire:
        while True:
            with self._lock:
                now = self._clock()
                if self._blocked_until > slot:
                    slot = max(now, self._next_slot, self._blocked_until)
                    if slot > deadline:
                        self._refund(day, remaining_gen)
                        return Acquire.BLOCKED
                    self._next_slot = slot + self.min_interval_s
                wait = slot - now
            if wait <= 0:
                self._local.ticket = (day, remaining_gen)
                return Acquire.ALLOWED
            if cancel_check is not None and cancel_check():
                with self._lock:
                    self._refund(day, remaining_gen)
                return Acquire.CANCELLED
            self._sleep(min(wait, _WAIT_POLL_S))

    def refund(self) -> None:
        """Give back this thread's last ALLOWED slot when the request was never sent (e.g. it could not be built).

        A no-op once ``record()`` answered that slot, after a later ``acquire`` in this thread, or when repeated.
        """
        ticket = getattr(self._local, "ticket", None)
        if ticket is None:
            return
        self._local.ticket = None
        with self._lock:
            self._refund(*ticket)

    def _block(self, now: float, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, now + min(seconds, _MAX_BLOCK_S))

    def record(self, status_code: int | None, headers: Mapping[str, str] | None) -> None:
        """Update pacing from a response.

        Args:
            status_code: HTTP status, or None for a network error/timeout.
            headers: Response headers (any case), or None.
        """
        self._local.ticket = None
        lowered = {str(k).lower(): str(v) for k, v in headers.items()} if headers else {}
        circuit_opened = False
        with self._lock:
            self._roll_day()
            now = self._clock()
            if status_code is None or status_code >= 500:
                self._failures += 1
                if self._failures >= self._failure_threshold:
                    circuit_opened = self._blocked_until <= now
                    self._blocked_until = max(self._blocked_until, now + self._circuit_open_s)
                    # Half-open: after the pause the next failure alone re-opens it; a success resets the run.
                    self._failures = self._failure_threshold - 1
            else:
                self._failures = 0
            if status_code is not None:
                self._apply_headers(status_code, lowered, now)
            self._usage_seq += 1
            seq = self._usage_seq
            snapshot = (self._day, self._used, self._limit, self._remaining)
        if circuit_opened:
            logger.warning(
                "{} circuit open after {} failures in a row; pausing lookups for {:.0f} s",
                self.source_id,
                self._failure_threshold,
                self._circuit_open_s,
            )
        self._publish_usage(seq, snapshot)

    def _apply_headers(self, status_code: int, lowered: Mapping[str, str], now: float) -> None:
        """Caller holds the lock."""
        wall_now = self._wall_clock()
        limit = _number(lowered.get("x-usagelimit-limit"))
        if limit is not None and limit > 0:
            self._limit = int(limit)
        remaining = _number(lowered.get("x-usagelimit-remaining"))
        if remaining is not None:
            self._replace_remaining(max(0, int(remaining)))
        rate_remaining = _number(lowered.get("x-ratelimit-remaining"))
        rate_reset = _delay_s(lowered.get("x-ratelimit-reset"), wall_now)
        if rate_remaining is not None and rate_remaining <= 0 and rate_reset:
            self._block(now, rate_reset)
        retry_after = _delay_s(lowered.get("retry-after"), wall_now)
        if status_code == 429:
            if retry_after is None:
                retry_after = rate_reset if rate_reset else _DEFAULT_429_S
            self._block(now, max(retry_after, _MIN_RETRY_S))
        elif status_code >= 500 and retry_after is not None:
            self._block(now, max(retry_after, _MIN_RETRY_S))

    def _publish_usage(self, seq: int, snapshot: tuple[str, int, int | None, int | None]) -> None:
        """Hand usage to ``on_usage`` outside the pacing lock, never letting an older snapshot overwrite a newer one."""
        if self._on_usage is None:
            return
        with self._usage_lock:
            if seq <= self._published_seq:
                return
            self._published_seq = seq
            try:
                self._on_usage(self.source_id, *snapshot)
            except Exception as exc:  # usage is display-only; a failed save must never break pacing
                logger.warning("Could not save {} usage: {}", self.source_id, exc)

    def seed_usage(self, *, day: str, used: int | None, limit: int | None, remaining: int | None) -> None:
        """Carry on from usage stored earlier today (after a restart), so the daily reserve and "used today" hold.

        Ignored for another UTC day, or once this limiter has handed out a request of its own.

        Args:
            day: The UTC day the usage was stored for.
            used: Requests counted that day.
            limit: The daily limit from the last response's headers.
            remaining: What the last response said remained.
        """
        with self._lock:
            self._roll_day()
            if day != self._day or self._used or self._usage_seq:
                return
            self._used = max(0, used or 0)
            if limit is not None and limit > 0:
                self._limit = int(limit)
            if remaining is not None:
                self._replace_remaining(max(0, int(remaining)))

    def usage(self) -> dict:
        """Snapshot for the Settings page.

        Returns:
            ``{"day", "used", "limit", "remaining", "blocked_until_s", "low_priority_exhausted", "resets_at"}``;
            limit/remaining are None until a response carried them. ``resets_at`` is always the day boundary in the
            user's words (``RESET_TIME_LABEL``): this source's own reset header can't be trusted (see ``_roll_day``).
        """
        with self._lock:
            self._roll_day()
            now = self._clock()
            return {
                "day": self._day,
                "used": self._used,
                "limit": self._limit,
                "remaining": self._remaining,
                "blocked_until_s": max(0.0, self._blocked_until - now),
                "low_priority_exhausted": low_priority_exhausted(
                    limit=self._limit, remaining=self._remaining, reserve_fraction=self.reserve_fraction
                ),
                "resets_at": RESET_TIME_LABEL,
            }


# Per-window floors only (TheIntroDB: 30 requests per 10 s); daily budgets always come from headers.
_MIN_INTERVALS = {"theintrodb": 10.0 / 30.0 + 0.01, "introdb": 0.5, "skipdb": 0.5}
_limiters: dict[str, SourceLimiter] = {}
_limiters_lock = threading.Lock()


def _persist_usage(source_id: str, day: str, used: int, limit: int | None, remaining: int | None) -> None:
    from ..store import get_marker_store

    get_marker_store().record_source_usage(source_id, day=day, used=used, limit=limit, remaining=remaining)


def _stored_usage(source_id: str, day: str) -> dict | None:
    from ..store import get_marker_store

    return get_marker_store().source_usage(source_id, day)


def get_limiter(source_id: str) -> SourceLimiter:
    """Process-wide limiter for a source.

    Args:
        source_id: ``theintrodb``, ``introdb`` or ``skipdb`` (others get a 1 s spacing).

    Returns:
        The shared limiter, created on first use from today's stored usage (so a restart keeps the day's count).
    """
    with _limiters_lock:
        if source_id not in _limiters:
            limiter = SourceLimiter(
                source_id, min_interval_s=_MIN_INTERVALS.get(source_id, 1.0), on_usage=_persist_usage
            )
            day = limiter.usage()["day"]
            try:
                stored = _stored_usage(source_id, day)
            except Exception as exc:  # usage is a courtesy to the source; an unreadable store must never block lookups
                logger.warning("Could not read {} usage: {}", source_id, type(exc).__name__)
                stored = None
            if stored:
                limiter.seed_usage(day=day, **stored)
            _limiters[source_id] = limiter
        return _limiters[source_id]


def reset_limiters() -> None:
    """Forget all limiters (tests)."""
    with _limiters_lock:
        _limiters.clear()
