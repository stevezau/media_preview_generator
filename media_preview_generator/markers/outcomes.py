"""Per-file outcomes (job counters, Files panel) and per-server row statuses for Intro & Credits jobs."""

from __future__ import annotations

from enum import Enum


class FileOutcome(str, Enum):
    """What happened to one file."""

    PUBLISHED = "markers_published"
    UP_TO_DATE = "markers_up_to_date"
    WAITING = "markers_waiting"
    NEEDS_REVIEW = "markers_needs_review"
    SKIPPED = "markers_skipped"
    NO_MARKERS = "markers_none"
    NO_OWNERS = "markers_no_owners"
    FILE_NOT_FOUND = "skipped_file_not_found"
    FAILED = "failed"


OUTCOME_KEYS: tuple[str, ...] = tuple(o.value for o in FileOutcome)


class ServerStatus(str, Enum):
    """What happened on one server for one file."""

    WRITTEN = "markers_written"
    UP_TO_DATE = "markers_up_to_date"
    NEEDS_REVIEW = "markers_needs_review"
    SKIPPED = "markers_skipped"
    WAITING = "markers_waiting"
    NONE = "markers_none"
    FAILED = "failed"


def file_outcome(statuses: set[str], *, needs_review: bool) -> FileOutcome:
    """Fold one file's per-server statuses into its job outcome.

    Precedence, first match wins: any server written → published; any server failed → failed (so a broken write is
    never hidden behind another server's up-to-date row); any up to date → up to date; any waiting (the server hasn't
    indexed the file yet, or the item's other versions don't agree yet) → waiting; sources disagree → needs review;
    any server with nothing to publish (or no rows) → no markers; every server skipped (no publisher, plugin missing)
    → skipped.

    Args:
        statuses: ``ServerStatus`` values of the file's rows.
        needs_review: Whether any enabled marker type is waiting for agreement.

    Returns:
        The file outcome counted on the job.
    """
    for status, outcome in (
        (ServerStatus.WRITTEN, FileOutcome.PUBLISHED),
        (ServerStatus.FAILED, FileOutcome.FAILED),
        (ServerStatus.UP_TO_DATE, FileOutcome.UP_TO_DATE),
        (ServerStatus.WAITING, FileOutcome.WAITING),
    ):
        if status.value in statuses:
            return outcome
    if needs_review:
        return FileOutcome.NEEDS_REVIEW
    if ServerStatus.NONE.value in statuses or not statuses:
        return FileOutcome.NO_MARKERS
    return FileOutcome.SKIPPED


# ``reason_code`` of a waiting row whose server hasn't indexed the file yet (no item id, or the item isn't there): the
# job retries those files later. Other rows carry no code.
NOT_IN_LIBRARY = "not_in_library"


# publish_state.status persisted per (file, server) for the rows that record an attempt.
STATE_BY_STATUS: dict[ServerStatus, str] = {
    ServerStatus.WRITTEN: "written",
    ServerStatus.SKIPPED: "skipped",
    ServerStatus.WAITING: "waiting",
    ServerStatus.FAILED: "failed",
}
