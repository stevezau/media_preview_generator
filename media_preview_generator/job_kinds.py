"""Job kinds and the per-item outcome contract for non-preview kinds.

Previews keep their original ``process_canonical_path`` flow. Other kinds (Intro & Credits) plug into the same
dispatcher and worker pool through :class:`KindHandlers`, so they share workers, priorities, pause and cancel.
This module imports nothing from the package so both ``web.jobs`` and ``jobs.*`` can use it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

JOB_KIND_PREVIEWS = "previews"
JOB_KIND_INTRO_CREDITS = "intro_credits"
JOB_KIND_LOUDNESS = "loudness"
JOB_KINDS: tuple[str, ...] = (JOB_KIND_PREVIEWS, JOB_KIND_INTRO_CREDITS, JOB_KIND_LOUDNESS)
# Kinds with their own runner and their own pause (a job pause of theirs is the user's, kept across Pause all and a
# restart); preview jobs pause through the global flag.
SELF_PAUSED_KINDS: frozenset[str] = frozenset({JOB_KIND_INTRO_CREDITS, JOB_KIND_LOUDNESS})


def parse_job_kind(value: object) -> str:
    """Return a known job kind, defaulting to previews.

    Args:
        value: Raw kind (from storage, an API payload, or a caller).

    Returns:
        ``value`` when it names a known kind, else :data:`JOB_KIND_PREVIEWS`.
    """
    return value if isinstance(value, str) and value in JOB_KINDS else JOB_KIND_PREVIEWS


def parse_kind_ranks(value: object) -> dict[str, int]:
    """Validate a ``job_kind_priority`` value; kinds it leaves out rank 1.

    Args:
        value: Raw setting (API payload or stored value); ``None`` means every kind ranks 1.

    Returns:
        Every job kind mapped to its rank.

    Raises:
        ValueError: ``value`` is not a mapping of known kinds to 1, 2 or 3.
    """
    ranks = dict.fromkeys(JOB_KINDS, 1)
    if value is None:
        return ranks
    if not isinstance(value, dict) or any(
        kind not in JOB_KINDS or type(rank) is not int or rank not in (1, 2, 3) for kind, rank in value.items()
    ):
        raise ValueError("job_kind_priority must map previews, intro_credits and loudness to 1, 2 or 3")
    ranks.update(value)
    return ranks


def safe_kind_ranks(value: object) -> dict[str, int]:
    """Like :func:`parse_kind_ranks`, but a hand-edited bad value ranks every kind 1 instead of stopping dispatch.

    Args:
        value: Stored ``job_kind_priority``.

    Returns:
        Every job kind mapped to its rank.
    """
    try:
        return parse_kind_ranks(value)
    except ValueError:
        return dict.fromkeys(JOB_KINDS, 1)


@dataclass
class ItemOutcome:
    """Result of one item for a non-preview job kind.

    Attributes:
        outcome_key: Per-file outcome counted on the job (must be in the kind's ``outcome_keys``).
        message: Human-readable detail for the Files panel.
        publisher_rows: Per-server rows in the shape ``fold_publisher_rows_into_aggregate`` expects.
    """

    outcome_key: str
    message: str = ""
    publisher_rows: list[dict] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Whether the item counts as a failure."""
        return self.outcome_key == "failed"


def outcome_value(key: str) -> object:
    """Wrap an outcome key for ``_notify_file_result``, which reads ``outcome.value``.

    Args:
        key: Outcome key to wrap.

    Returns:
        An object whose ``value`` attribute is ``key``.
    """
    return SimpleNamespace(value=key)


def normalize_outcome(value: object, outcome_keys: tuple[str, ...]) -> ItemOutcome:
    """Return ``value`` if it is a valid outcome for the kind, else a ``failed`` outcome.

    A handler bug must still count the item exactly once; an unknown key would otherwise be dropped by the
    counters and leave "X processed" and the outcome breakdown disagreeing.

    Args:
        value: Whatever a kind's ``check_fn`` / ``process_fn`` returned.
        outcome_keys: The kind's valid outcome keys.

    Returns:
        ``value`` itself when valid, otherwise a new ``failed`` :class:`ItemOutcome`.
    """
    if isinstance(value, ItemOutcome) and value.outcome_key in outcome_keys:
        return value
    shown = value.outcome_key if isinstance(value, ItemOutcome) else type(value).__name__
    return ItemOutcome("failed", f"internal error: invalid item outcome {shown!r}")


@dataclass(frozen=True)
class KindHandlers:
    """Per-kind item functions used by the dispatcher (check stage) and workers (process stage)."""

    check_fn: Callable[..., ItemOutcome | None]
    process_fn: Callable[..., ItemOutcome]
    outcome_keys: tuple[str, ...]
    check_label: str = "Checking…"
    check_worker_label: str = "Lookup"
    # Largest fraction of the dispatcher's checking threads this kind may hold at once (floor 1). A kind whose
    # check_fn can block (rate-limited online lookups) sets < 1 so it can't starve preview checks.
    check_share: float = 1.0
    # ``pickup_fn(item, worker_display_name)`` logs the kind's own line when a worker starts an item; None: the worker
    # logs its generic "picked up" line.
    pickup_fn: Callable[[Any, str], None] | None = None
