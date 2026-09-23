"""Per-file outcomes (job counters, Files panel) and per-server row statuses for Intro & Credits jobs."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from .models import Marker, MarkerType


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

    The file shows what still needs something, most urgent first, so a finished server never hides an unfinished one.
    First match wins:

    1. Any server failed → failed: a broken write or check, even when another server took the markers.
    2. The sources don't agree on an enabled marker type → needs review: only the user settles it, even when the
       agreed types were written or are up to date.
    3. Any server waiting (it hasn't indexed the file yet, Plex Pass is unconfirmed, or the item's other versions
       don't agree yet) → waiting, even when another server was written or is up to date.
    4. Any server written → published.
    5. Any server up to date → up to date.
    6. Any server with nothing to publish, or no rows → no markers.
    7. Every server skipped (no publisher, plugin missing, turned off) → skipped.

    Retry and verify jobs are queued from the per-server rows, not from this outcome.

    Args:
        statuses: ``ServerStatus`` values of the file's rows.
        needs_review: Whether any enabled marker type is waiting for agreement.

    Returns:
        The file outcome counted on the job.
    """
    if ServerStatus.FAILED.value in statuses:
        return FileOutcome.FAILED
    if needs_review:
        return FileOutcome.NEEDS_REVIEW
    for status, outcome in (
        (ServerStatus.WAITING, FileOutcome.WAITING),
        (ServerStatus.WRITTEN, FileOutcome.PUBLISHED),
        (ServerStatus.UP_TO_DATE, FileOutcome.UP_TO_DATE),
    ):
        if status.value in statuses:
            return outcome
    if ServerStatus.NONE.value in statuses or not statuses:
        return FileOutcome.NO_MARKERS
    return FileOutcome.SKIPPED


# ``reason_code`` of the waiting rows the job retries later; other rows carry no code.
# The server hasn't indexed the file yet (no item id, or the item isn't there).
NOT_IN_LIBRARY = "not_in_library"
# Plex answered its database checks but not the Plex Pass check (restarting, an HTTP blip).
PLEX_PASS_UNKNOWN = "plex_pass_unknown"
RETRY_REASON_CODES = frozenset({NOT_IN_LIBRARY, PLEX_PASS_UNKNOWN})


# Skipped-file message for trailers and other extras (``external_ids.is_extra``).
EXTRAS_NOT_CHECKED = "Extras aren't checked for markers"


def kept_note(
    kept_types: Iterable[MarkerType],
    wanted: Iterable[Marker],
    vendor: str,
    *,
    not_decided: Iterable[MarkerType] = (),
) -> str:
    """Row and Inspector wording for the types a server keeps as its own ("Keep Plex's", "Keep Emby's").

    Args:
        kept_types: The types kept as the server's own.
        wanted: The decided markers.
        vendor: The server's brand as users know it (``Plex``, ``Emby``).
        not_decided: Types left undecided because every server keeps its own and shows one, so the file wasn't read
            for them (``kept_own_reason``); named whether or not they are in ``wanted``.

    Returns:
        "keeping Plex's credits" (or "intro and credits"); "" when none of ``wanted`` is kept and nothing was left
        undecided that way.
    """
    kept, wanted_types, undecided = set(kept_types), {m.type for m in wanted}, set(not_decided)
    names = [t.value for t in MarkerType if (t in kept and t in wanted_types) or t in undecided]
    return f"keeping {vendor}'s {' and '.join(names)}" if names else ""


def kept_own_reason(vendors: Iterable[str]) -> str:
    """Decision reason for a type the file wasn't read for: every server its markers go to keeps its own and shows one.

    Args:
        vendors: The brands of those servers as users know them (``Plex``, ``Emby``), repeats allowed.

    Returns:
        "kept Plex's own marker", or "kept Plex's and Emby's own markers".
    """
    names = list(dict.fromkeys(vendors))
    owners = " and ".join(f"{name}'s" for name in names)
    return f"kept {owners} own marker{'s' if len(names) > 1 else ''}"


def with_kept_note(message: str, note: str) -> str:
    """``message; note``, or the note alone (capitalised) without a message."""
    if not note:
        return message
    return f"{message}; {note}" if message else note[0].upper() + note[1:]


def with_sentence(message: str, sentence: str) -> str:
    """``message. sentence`` — a whole sentence after a row message — or either one alone."""
    if not sentence:
        return message
    return f"{message}. {sentence}" if message else sentence


def replaced_own_note(replaced_types: Iterable[MarkerType], vendor: str) -> str:
    """Row wording for a locked marker that replaced the server's own although the server keeps its own.

    Approved copy, ``evidence/design/phase4/ui-copy.md`` §4 (owner decision, spec §14 2026-09-20). The Inspector's
    editor writes the same sentence itself (``web/static/js/markers_inspector.js`` ``resultLines``) because the save
    route hands it the replaced types, not this message — change both together.

    Args:
        replaced_types: The types whose own markers the server lost (``MarkerPublisher.last_replaced_own_types``).
        vendor: The server's brand as users know it (``Plex``, ``Emby``).

    Returns:
        The sentence; "" when the server's own markers were left alone.
    """
    if not frozenset(replaced_types):
        return ""
    return (
        f"Replaced {vendor}'s own marker. This server is set to keep {vendor}'s, but a marker you adjust always wins."
    )


def lock_overrides_note(replaced_types: Iterable[MarkerType], vendor: str) -> str:
    """Inspector wording **before** a save, for a locked type this server would otherwise keep its own markers of.

    Approved copy, ``evidence/design/phase4/ui-copy.md`` §4 (owner decision, spec §14 2026-09-20).

    Args:
        replaced_types: The types whose own markers the server is about to lose.
        vendor: The server's brand as users know it (``Plex``, ``Emby``).

    Returns:
        The sentence; "" when nothing of the server's own is in the way.
    """
    if not frozenset(replaced_types):
        return ""
    return f"This server is set to keep {vendor}'s own markers. Your locked marker replaces them anyway."


# Row key listing the types whose server's own markers a locked marker replaced although the server keeps its own
# (spec §5.5 rule 1); absent when nothing of the server's own was taken off it.
REPLACED_OWN = "replaced_own"


# Row key on a written or up-to-date row of a replaced file: servers often rescan a replaced file after the job, so
# the job checks it again later (``job_runner._queue_verify``).
VERIFY_LATER = "verify_later"
# Row key on an up-to-date row whose server couldn't be read back: the job warns how many files it couldn't check.
READ_BACK_FAILED = "read_back_failed"


# publish_state.status persisted per (file, server) for the rows that record an attempt.
STATE_BY_STATUS: dict[ServerStatus, str] = {
    ServerStatus.WRITTEN: "written",
    ServerStatus.SKIPPED: "skipped",
    ServerStatus.WAITING: "waiting",
    ServerStatus.FAILED: "failed",
}
