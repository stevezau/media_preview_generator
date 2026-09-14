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


# ``reason_code`` of the waiting rows the job retries later; other rows carry no code.
# The server hasn't indexed the file yet (no item id, or the item isn't there).
NOT_IN_LIBRARY = "not_in_library"
# Plex answered its database checks but not the Plex Pass check (restarting, an HTTP blip).
PLEX_PASS_UNKNOWN = "plex_pass_unknown"
RETRY_REASON_CODES = frozenset({NOT_IN_LIBRARY, PLEX_PASS_UNKNOWN})


# Row message when Plex's own detection replaced ours and the server is set to "Keep Plex's".
KEPT_PLEX_MARKERS = "Plex's own markers are kept (Keep Plex's)"
# Row key on a written or up-to-date row of a replaced file: servers often rescan a replaced file after the job, so
# the job checks it again later (``job_runner._queue_verify``).
VERIFY_LATER = "verify_later"


# publish_state.status persisted per (file, server) for the rows that record an attempt.
STATE_BY_STATUS: dict[ServerStatus, str] = {
    ServerStatus.WRITTEN: "written",
    ServerStatus.SKIPPED: "skipped",
    ServerStatus.WAITING: "waiting",
    ServerStatus.FAILED: "failed",
}
