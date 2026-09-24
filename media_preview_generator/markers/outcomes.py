"""Per-file outcomes (job counters, Files panel) and per-server row statuses for Intro & Credits jobs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any

from .decide import DecisionStatus
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


def file_outcome(statuses: set[str], *, needs_review: bool, waiting_to_retry: bool = False) -> FileOutcome:
    """Fold one file's per-server statuses into its job outcome.

    The file shows what still needs something, most urgent first, so a finished server never hides an unfinished one.
    First match wins:

    1. Any server failed → failed: a broken write or check, even when another server took the markers.
    2. A server waiting with a retry queued (it hasn't indexed the file yet, Plex Pass is unconfirmed, or another
       version of the file's Plex item is on disk but not checked yet) → waiting, even when a marker type needs review:
       the job runs the file again.
    3. Any server written → published (or waiting while another server waits for the item's other versions): the job
       changed what a server shows. A type still in review is named in the file's summary.
    4. An enabled marker type is in Needs review (the sources disagree, or the only answer can't decide alone) → needs
       review: nothing was written, and only the user settles it, even when the decided types are up to date.
    5. Any server waiting (the item's other versions were checked and don't agree) → waiting, even when another server
       is up to date.
    6. Any server up to date → up to date.
    7. Any server with nothing to publish, or no rows → no markers.
    8. Every server skipped (no publisher, plugin missing, turned off) → skipped.

    Retry and verify jobs are queued from the per-server rows, not from this outcome.

    Args:
        statuses: ``ServerStatus`` values of the file's rows.
        needs_review: Whether any enabled marker type is waiting for agreement.
        waiting_to_retry: Whether a waiting row has a reason the job retries (``RETRY_REASON_CODES``).

    Returns:
        The file outcome counted on the job.
    """
    if ServerStatus.FAILED.value in statuses:
        return FileOutcome.FAILED
    if waiting_to_retry and ServerStatus.WAITING.value in statuses:
        return FileOutcome.WAITING
    if needs_review and ServerStatus.WRITTEN.value not in statuses:
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
# Plex shows one marker set per item, and another version of this file's item is on disk but hasn't been checked yet.
# Versions that were checked and disagree carry no code: trying again changes nothing until one of them changes.
VERSIONS_UNCHECKED = "versions_unchecked"
RETRY_REASON_CODES = frozenset({NOT_IN_LIBRARY, PLEX_PASS_UNKNOWN, VERSIONS_UNCHECKED})
# ``reason_code`` of a failed row the job retries: the write gave up waiting for Plex's database (another program, or
# another task of this app, held it past the wait). The row stays failed, and so does the file once the retries run out.
PLEX_DB_BUSY = "plex_db_busy"


# Start of a waiting row's message: Plex shows a type only once every version of the item is decided and agrees on
# it (``MarkerStore.files_waiting_for_other_versions`` finds these rows by it).
VERSIONS_WAITING = "Waiting for this item's other versions to agree on"

# Skipped-file message for trailers and other extras (``external_ids.is_extra``).
EXTRAS_NOT_CHECKED = "Extras aren't checked for markers"


def review_message(decisions: Mapping[MarkerType, Any], types: Iterable[MarkerType]) -> str:
    """Row wording for a file with marker types in Needs review: why each one is there, in type order.

    Args:
        decisions: The file's decisions by type, fresh (``TypeDecision``) or stored (``DecisionRow``); both carry
            ``status`` and ``reason``.
        types: The types that count (the enabled ones, or every stored one).

    Returns:
        Each reason as a sentence, e.g. ``Only IntroDB has the intro; an online answer needs a check against the
        file``; "" when none of ``types`` is in review.
    """
    wanted = set(types)
    reasons = [
        decision.reason or "Needs review"
        for mtype in MarkerType
        if mtype in wanted
        and (decision := decisions.get(mtype)) is not None
        and decision.status is DecisionStatus.NEEDS_REVIEW
    ]
    return ". ".join(dict.fromkeys(reason[0].upper() + reason[1:] for reason in reasons))


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
        not_decided: Types left undecided while every server keeps its own and shows one (``kept_own_reason``);
            named whether or not they are in ``wanted``.

    Returns:
        "keeping Plex's credits" (or "intro and credits"); "" when none of ``wanted`` is kept and nothing was left
        undecided that way.
    """
    kept, wanted_types, undecided = set(kept_types), {m.type for m in wanted}, set(not_decided)
    names = [t.value for t in MarkerType if (t in kept and t in wanted_types) or t in undecided]
    return f"keeping {vendor}'s {' and '.join(names)}" if names else ""


def kept_own_reason(vendors: Iterable[str]) -> str:
    """Decision reason for a type left undecided while every server its markers go to keeps its own and shows one.

    Args:
        vendors: The brands of those servers as users know them (``Plex``, ``Emby``), repeats allowed.

    Returns:
        "kept Plex's own marker", or "kept Plex's and Emby's own markers".
    """
    names = list(dict.fromkeys(vendors))
    owners = " and ".join(f"{name}'s" for name in names)
    return f"kept {owners} own marker{'s' if len(names) > 1 else ''}"


def is_kept_own(status: DecisionStatus, reason: str | None) -> bool:
    """Whether a stored decision is the kept status (``kept_own_reason``), not detection turned off.

    Args:
        status: The decision's status.
        reason: Its reason.

    Returns:
        True for a type a run left to every server's own marker.
    """
    text = reason or ""
    return (
        status is DecisionStatus.DISABLED and text.startswith("kept ") and text.endswith(("own marker", "own markers"))
    )


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
