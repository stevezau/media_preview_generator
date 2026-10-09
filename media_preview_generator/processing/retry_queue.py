"""Backoff schedule for the job-level retry path.

All retry-eligible outcomes (PENDING_REGISTRATION, NOT_INDEXED, NOT_IN_LIBRARY,
unresolved, not-found-on-disk) go through the per-Job retry pattern in
``web/routes/job_runner.py`` (``_spawn_retry_job``): one retry Job per attempt,
scoped to the still-pending paths only, with gate admission, log isolation, and
parent chain-state mutation via ``upsert_retry_chain_job``.

This module holds:

* :data:`BACKOFF_SCHEDULE` — the (60s, 2m, 5m, 15m, 1h) cadence consumed
  by ``_spawn_retry_job``. Public so any caller that wants to display the
  "next retry in Xs" countdown shares the canonical timing.
* :data:`PENDING_PUBLISHER_STATUSES` — the per-publisher status values
  that flag older results and non-chapter outputs for retry.
* :func:`publisher_needs_retry` and :func:`publisher_retry_count` — shared
  retry eligibility for file rows and server aggregates, including chapter
  failures whose retry flag is independent of their displayed status.
"""

from __future__ import annotations

from typing import Any

#: Backoff schedule in seconds for each attempt (1-indexed:
#: ``BACKOFF_SCHEDULE[0]`` is the delay before attempt #2). Five entries;
#: the retry count setting decides how many are used (a count past five
#: repeats the last). At the default count and delay the retries span
#: ~83 minutes, deliberately past typical Plex full-scan duration on a
#: small library; a count of 3 stops after 8.
#:
#: First attempt is 60s — Jellyfin's ``LibraryMonitor`` has a hard-coded
#: ~45s file-event settle delay before processing the refresh, so anything
#: under 45s is a guaranteed miss. Starting at 60s gives Jellyfin a real
#: chance to have indexed the file on attempt 1 instead of wasting it.
#: Subsequent gaps (2m / 5m / 15m / 1h) cover Plex's typical scan latency
#: window without turning into a runaway loop.
BACKOFF_SCHEDULE: tuple[int, ...] = (60, 120, 300, 900, 3600)

#: Retries when no count is stored: the whole schedule. Only the default; a stored count is kept as it is.
DEFAULT_RETRY_COUNT = len(BACKOFF_SCHEDULE)


def scaled_backoff_delay(attempt: int, retry_delay_sec: int) -> int:
    """Return the wait in seconds before retry ``attempt``.

    The user's "Initial retry delay" setting (default 30) scales every step of
    :data:`BACKOFF_SCHEDULE` by ``retry_delay_sec / 30``, floored at half speed.
    Attempts past the end of the schedule reuse its last step.

    Args:
        attempt: 1-indexed retry attempt number.
        retry_delay_sec: The ``webhook_retry_delay`` setting in seconds.

    Returns:
        The delay in whole seconds, never less than 1.
    """
    scale = max(0.5, retry_delay_sec / 30.0)
    step = BACKOFF_SCHEDULE[min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)]
    return max(1, int(step * scale))


#: Per-publisher status values (as ``.value`` strings of
#: :class:`PublisherStatus`) that flag a file as "still needs another
#: attempt because the destination server isn't ready yet."
#:
#: The shared eligibility helpers apply this legacy policy to:
#:
#: * ``web/routes/job_runner.py`` — the retry-decision scan that walks
#:   the per-file JSONL after each dispatch to decide whether to spawn
#:   another retry Job.
#: * ``web/routes/api_jobs.py`` — the ``_pending_servers`` helper that
#:   computes the ``pending_servers`` field on each ``/attempts`` entry
#:   so the modal can render per-pill vendor chips.
#:
#: Chapter artifacts with explicit retry flags override these status defaults.
PENDING_PUBLISHER_STATUSES: frozenset[str] = frozenset(
    {
        "published_pending_registration",
        "published_pending_chapters",
        "skipped_not_indexed",
        "skipped_not_in_library",
    }
)


CHAPTER_PUBLISHER_STATUSES = frozenset({"published_pending_chapters", "published_chapters_failed"})


def publisher_needs_retry(row: dict) -> bool:
    """Read retry eligibility separately from a chapter's displayed result.

    Older saved chapter results have no retry flag, so retain their existing policy.
    """
    if not isinstance(row, dict):
        return False
    status = row.get("status")
    if status in CHAPTER_PUBLISHER_STATUSES:
        artifacts = row.get("artifacts")
        chapter = artifacts.get("chapters") if isinstance(artifacts, dict) else None
        if isinstance(chapter, dict) and type(chapter.get("retryable")) is bool:
            return chapter["retryable"]
    return status in PENDING_PUBLISHER_STATUSES


def publisher_retry_count(publisher: dict) -> int:
    """Count retryable server items without treating every chapter failure as transient."""
    counts = publisher.get("counts") or {}
    eligible = publisher.get("retryable_counts") or {}
    if not isinstance(counts, dict) or not isinstance(eligible, dict):
        return 0
    total = 0
    for status in PENDING_PUBLISHER_STATUSES | CHAPTER_PUBLISHER_STATUSES:
        count = eligible.get(status, counts.get(status, 0) if status in PENDING_PUBLISHER_STATUSES else 0)
        if type(count) is int and count > 0:
            total += count
    return total


def _clamped_int(settings: Any, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(settings.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def retry_policy(settings: Any) -> tuple[int, int]:
    """The global retry policy: how many retries, and the base delay that scales :data:`BACKOFF_SCHEDULE`.

    The keys are named for webhooks (where the settings used to live) but apply to every job type; the bounds match
    Settings → Processing → Job Execution. Webhook preview jobs and Intro & Credits retries both read it here so the
    clamps can't drift apart.

    Args:
        settings: The settings manager (anything with ``get(key, default)``).

    Returns:
        ``(webhook_retry_count, webhook_retry_delay)``: count clamped to 0-10 (default
        :data:`DEFAULT_RETRY_COUNT`), delay to 10-300 seconds (default 30); a value that isn't a number falls back
        to its default.
    """
    return (
        _clamped_int(settings, "webhook_retry_count", DEFAULT_RETRY_COUNT, 0, 10),
        _clamped_int(settings, "webhook_retry_delay", 30, 10, 300),
    )
