"""Publisher contract (spec §6.3)."""

from __future__ import annotations

import contextlib
import threading
from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger

from ..models import Marker, MarkerType

if TYPE_CHECKING:
    from ..decide import FileLimits

# Versions of one item that decided a type within this of each other show one marker set (spec §6.3).
VERSION_AGREEMENT_MS = 2_000


class Capability(str, Enum):
    """Whether a server can receive markers right now, and if not, why."""

    READY = "ready"
    DISABLED = "disabled"
    NEEDS_CONFIRMATION = "needs_confirmation"
    NEEDS_PLUGIN = "needs_plugin"
    PLUGIN_OUTDATED = "plugin_outdated"
    NEEDS_PASS = "needs_pass"
    NEEDS_LOCAL_DB = "needs_local_db"
    # A Plex server whose database is on another machine and whose Plex marker agent can't be used right now: it
    # doesn't answer, it refuses this app's key, or it speaks another version of the protocol. The database itself
    # is fine — ``details["agent"]`` says which of the three it is (``plex_remote``).
    AGENT_UNAVAILABLE = "agent_unavailable"
    NEEDS_PLEX_DETECTION_ONCE = "needs_plex_detection_once"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    UNREACHABLE = "unreachable"
    MISCONFIGURED = "misconfigured"


@dataclass(frozen=True)
class CapabilityReport:
    """Capability plus a user-facing message and details for the Edit dialog status block."""

    state: Capability
    message: str
    details: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        """Whether writes may be attempted."""
        return self.state is Capability.READY


class Shown(str, Enum):
    """What a server item shows of the markers this app last left there (``MarkerPublisher.shows``)."""

    OURS = "ours"
    MISSING = "missing"  # some of ours are gone and nothing else of that type took their place
    REPLACED = "replaced"  # the server shows another marker of one of our types instead (its own detection)
    # The item's versions aren't the ones our last write agreed on (Plex: one marker set for every version, and a
    # version added since hasn't been decided the same way).
    VERSIONS_CHANGED = "versions_changed"
    GONE = "gone"  # the server no longer has the item (deleted, or its file moved to a new item)


def compare_shown(
    ours: Iterable[Marker], served: Mapping[MarkerType, list[tuple[int, int]]], *, others_alongside: bool
) -> Shown:
    """Classify what a server shows for the types of ``ours``.

    Args:
        ours: What this app last left on the item.
        served: Per type, ``(start_ms, end_ms)`` of every marker the server shows, in the form ``write`` returns.
        others_alongside: Whether another provider's marker of a type is shown next to ours without replacing them
            (Jellyfin serves every provider's segments); on Plex any other row of the type is one ours gave way to.

    Returns:
        OURS when every type shows ours; REPLACED when another marker of a type stands where ours was missing (or, on
        Plex, next to ours); otherwise MISSING.
    """
    mine_by_type: dict[MarkerType, Counter] = {}
    for marker in ours:
        mine_by_type.setdefault(marker.type, Counter())[(marker.start_ms, marker.end_ms)] += 1
    missing = False
    for mtype, mine in mine_by_type.items():
        shown = Counter(served.get(mtype, []))
        lacking, extra = mine - shown, shown - mine
        if not lacking and (others_alongside or not extra):
            continue
        if extra:
            return Shown.REPLACED
        missing = True
    return Shown.MISSING if missing else Shown.OURS


def versions_agree(mine: list[Marker], theirs: list[Marker]) -> bool:
    """Whether two marker lists of one type match pairwise (in start order) within ``VERSION_AGREEMENT_MS``."""
    if len(mine) != len(theirs):
        return False
    pairs = zip(sorted(mine, key=lambda m: m.start_ms), sorted(theirs, key=lambda m: m.start_ms), strict=True)
    return all(
        abs(a.start_ms - b.start_ms) <= VERSION_AGREEMENT_MS and abs(a.end_ms - b.end_ms) <= VERSION_AGREEMENT_MS
        for a, b in pairs
    )


def same_times(mine: list[Marker], theirs: list[Marker]) -> bool:
    """Whether two marker lists of one type hold exactly the same times (in start order).

    :func:`versions_agree` asks whether two versions mean the same answer; this asks whether a list *is* another
    list. It is what decides whether what the item already shows belongs to a version the user locked.
    """
    if len(mine) != len(theirs):
        return False
    key = lambda m: (m.start_ms, m.end_ms)  # noqa: E731 -- a two-line sort key, not worth a def
    return all(
        a.start_ms == b.start_ms and a.end_ms == b.end_ms
        for a, b in zip(sorted(mine, key=key), sorted(theirs, key=key), strict=True)
    )


def agreed_across_versions(
    markers: Iterable[Marker],
    others: Iterable[Mapping[MarkerType, Marker] | None],
    prior: Iterable[Marker],
    types: Iterable[MarkerType],
) -> list[Marker]:
    """The markers one Plex item shows for all its versions: the types every version decided alike.

    A type is kept only when the calling file has it and every other version is decided, has that type and agrees
    within ``VERSION_AGREEMENT_MS``. The times are the calling file's, unless what this app already left on the item
    (``prior``) agrees with every version too: then that stays, so versions whose times differ slightly don't rewrite
    each other's markers on every run. **A locked type is the exception** -- the user's own times win however close
    they are to what the item shows, because the whole difference an editor nudge makes is smaller than
    ``VERSION_AGREEMENT_MS``, so keeping ``prior`` would silently discard the edit. That exception stops once
    ``prior`` *is* a locked version's own times: the item can then show only one of two deliberate user choices that
    agree inside the window this app calls one answer, so the first to land stays and the versions settle rather than
    rewriting each other on every run forever. Whichever times win, each returned marker carries the calling file's
    ``locked`` flag for its type — only that file's, never a sibling version's: a lock belongs to the file the user edited, and a
    run of an unlocked version that lets the server keep its own markers again is undone by the locked version's next
    write (that write bumps the item's version, so the unlocked one's publish basis no longer matches either).

    Args:
        markers: The calling file's decided markers.
        others: Each other version's decided markers by type; None for a version never decided.
        prior: What this app last left on the item.
        types: The types the server shows.

    Returns:
        The agreed markers (unsorted).
    """
    markers, others, prior = list(markers), list(others), list(prior)
    agreed: list[Marker] = []
    for mtype in types:
        mine = [m for m in markers if m.type is mtype]
        theirs = [None if d is None else [m for m in d.values() if m.type is mtype] for d in others]
        if not mine or any(t is None or not versions_agree(mine, t) for t in theirs):
            continue
        kept = [m for m in prior if m.type is mtype]
        locked = any(m.locked for m in mine)
        # Every entry of ``theirs`` is a decided version that agrees with ``mine``; the guard above skipped this type
        # otherwise.
        locked_versions = [mine] + [t for t in theirs if any(m.locked for m in t)]
        # Not ``versions_agree``: the question is whether the item is already showing times a user locked, not
        # whether it is showing something close enough. Within the tolerance but belonging to nobody -- the detector's
        # times from before the locks, or an edit a later one superseded -- is exactly where a lock has to win.
        kept_is_a_locked_versions = any(same_times(kept, v) for v in locked_versions)
        agrees = kept and versions_agree(kept, mine) and all(versions_agree(kept, t) for t in theirs)
        if agrees and (not locked or kept_is_a_locked_versions):
            # The times stay what the item already shows, but the calling file's lock rides along: whether the type is
            # the user's own decides whether the server may keep its own markers of it (spec §5.5 rule 1), and what the
            # item record happens to carry from an earlier run must not answer that.
            agreed.extend(replace(m, locked=locked) for m in kept)
        else:
            # A locked type takes the shortcut above only when the item is already showing a locked version's own
            # times. The shortcut exists so two versions of one item don't rewrite each other every run over a
            # difference inside ``VERSION_AGREEMENT_MS``, and a nudge of the editor's arrow keys is smaller than
            # that: against times nobody locked, keeping ``prior`` would hand the user back what they just changed,
            # report the row ``unchanged``, and never reach ``set_publish_basis`` -- the same silent no-op on every
            # later run, which is the bug this branch was split for.
            #
            # Where the item *is* showing a locked version's times, keeping them is right even against another
            # locked version: both are the user's own deliberate choices, the item can show only one of them, and
            # they are inside the window this app already calls one answer. Letting each version write its own
            # instead makes the runs alternate forever, every one of them a real Plex write that bumps the item
            # version and so invalidates the other version's publish basis.
            #
            # The residual, stated at its real size: which locked version wins is whichever landed first, so any
            # other locked version's edit within ``VERSION_AGREEMENT_MS`` of it -- a first lock as much as a
            # re-edit -- is dropped. It is dropped *quietly*: the publisher returns ``prior`` unchanged, the pipeline
            # advances the publish basis and reports the row up to date, and nothing retries. A deterministic
            # winner over the locked set (lowest start, ties by path) would converge the same way and remove the
            # dependence on run order; deferred, see the phase-4 progress log.
            #
            # Also not fixed here: the guard above compares ``mine`` with each sibling but never sibling with
            # sibling, so versions that agree only through a middle one (0, 2500, 1200 ms) still make each run
            # remove or rewrite the type. That predates locks; the claim that versions "settle" holds for versions
            # that agree pairwise.
            agreed.extend(mine)
    return agreed


# One item for ``MarkerPublisher.shows_many``: item id, what this app last left there, the types kept as the server's
# own, and the item's version files recorded at the last write (None: not recorded).
ReadBackItem = tuple[str, list[Marker], frozenset[MarkerType], tuple[str, ...] | None]
# Failed reads in a row after which ``MarkerPublisher.shows_many`` stops reading a server for the call.
UNREADABLE_IN_A_ROW = 20


def stopped_unreadable(items: list[ReadBackItem], answers: dict[str, Shown | None]) -> bool:
    """Whether a ``shows_many`` call stopped early because the server's reads kept failing (not for a cancel).

    Args:
        items: The items asked about.
        answers: What the call returned, in the order the items were read.

    Returns:
        True when items were left unread right after ``UNREADABLE_IN_A_ROW`` failed reads.
    """
    if len(answers) >= len({item_id for item_id, *_ in items}) or len(answers) < UNREADABLE_IN_A_ROW:
        return False
    return all(shown is None for shown in list(answers.values())[-UNREADABLE_IN_A_ROW:])


class PublishError(Exception):
    """A publisher call failed; ``state`` says which capability problem caused it (if any).

    From ``write``: with ``atomic_writes`` nothing changed on the server; otherwise it may hold a partial write.
    """

    def __init__(self, message: str, *, state: Capability | None = None) -> None:
        super().__init__(message)
        self.state = state


class ItemNotFoundError(PublishError):
    """The server doesn't know the item (yet)."""


class DatabaseBusyError(PublishError):
    """The server's database stayed locked past the write's wait, by another program or another task of this app.

    Nothing was written. Such a lock is usually let go within minutes, so a job tries the file again a few minutes
    later (``outcomes.PLEX_DB_BUSY``) instead of leaving it failed until Check servers' next day. Its message ends its
    first clause with ``NEXT_RUN``, which a job that does retry the file words as ``RETRY_SOON``.
    """


# What a busy database's message promises when nothing retries the file sooner (the Inspector's publish, a check), and
# what a job that queues the file's retry says instead.
NEXT_RUN = "trying again on the next run"
RETRY_SOON = "this job tries again in a few minutes"

_waits = threading.local()


@contextlib.contextmanager
def cancellable_waits(cancel_check: Callable[[], bool] | None) -> Iterator[None]:
    """Let a publisher's lock waits on this thread stop early once ``cancel_check`` says the job was cancelled.

    Plex's publisher waits minutes for a busy database, in short slices (``plex_db.WAIT_SLICE_S``) that ask
    ``wait_cancelled`` in between; a cancelled job's threads are then free again within a slice.

    Args:
        cancel_check: True once the job is cancelled; None: waits run to their deadline.
    """
    before = getattr(_waits, "cancel_check", None)
    _waits.cancel_check = cancel_check
    try:
        yield
    finally:
        _waits.cancel_check = before


def wait_cancelled() -> bool:
    """Whether the job this thread's lock wait is for was cancelled (``cancellable_waits``). Never raises."""
    check = getattr(_waits, "cancel_check", None)
    try:
        return bool(check and check())
    except Exception:
        return False


class MarkerPublisher(ABC):
    """Writes markers to one server and reads back what it shows of them (``shows``).

    A server's own markers, read as evidence for a decision, come through ``sources.server_markers`` instead.
    """

    supported_types: frozenset[MarkerType] = frozenset()
    name: str = ""
    # True when a PublishError guarantees nothing changed on the server (one transaction). A caller may then keep
    # its record of what is ours; otherwise what is ours after a failed write is unknown.
    atomic_writes: bool = False
    # Set by every successful ``write``: whether it changed what the server shows. A publisher is built for one file's
    # publish to one server, so the flag always belongs to the caller's last write.
    last_write_changed: bool = True
    # Set by every successful ``write``: the types whose markers on the item are the server's own and stay there
    # untouched ("Keep Plex's", "Keep Emby's"); the caller records them and passes them back as ``kept_types``. Empty
    # elsewhere.
    last_kept_types: frozenset[MarkerType] = frozenset()
    # Set by every successful ``write``: the types whose markers on the item were the server's own and were replaced
    # anyway because the user locked them, although the server is set to keep its own (spec §5.5 rule 1). The caller
    # says so in the server's row; empty whenever nothing of the server's own was taken off it.
    last_replaced_own_types: frozenset[MarkerType] = frozenset()
    # Set by every successful ``write``: the types whose server's own markers were made for an earlier file at the
    # item's path and were replaced by ours although the server keeps its own (Plex). The caller says so in the row.
    last_replaced_stale_types: frozenset[MarkerType] = frozenset()
    # Set by every ``write`` that read the item: the item's version files that write computed the marker set for. The
    # caller records them and passes them back to ``shows``. None where items have no shared versions (Jellyfin, Emby).
    last_item_files: tuple[str, ...] | None = None
    # Set by every ``write``: whether another version of the item is on disk but not decided yet, so what the item
    # doesn't show waits for that version's first run, not for versions that disagree (the job tries the file again
    # later). False where items have no shared versions (Jellyfin, Emby).
    last_unchecked_versions: bool = False

    @abstractmethod
    def capability(self) -> CapabilityReport:
        """Check whether this server can receive markers."""

    @abstractmethod
    def shows(
        self,
        item_id: str,
        ours: list[Marker],
        *,
        kept_types: frozenset[MarkerType] = frozenset(),
        item_files: tuple[str, ...] | None = None,
    ) -> Shown | None:
        """Read what the server item shows for the types of ``ours`` (what this app last left there).

        Read-only and cheap: asked before a file is reported up to date, so a server that re-detected or rescanned
        the item gets our markers again.

        Args:
            item_id: Server item id.
            ours: What this app last left on the item.
            kept_types: Types whose server markers were kept last time (``last_kept_types``); one the server no
                longer shows at all is reported MISSING, so ours can go back.
            item_files: The item's version files recorded at the last write (``last_item_files``); a server with one
                marker set per item reports ``VERSIONS_CHANGED`` when its versions differ now. None: not recorded, not
                compared.

        Returns:
            How the server's markers compare with ``ours``; GONE when the server says it has no such item; None when
            they couldn't be read (a timeout, an error answer).
        """

    def item_missing(self, item_id: str) -> bool | None:
        """Whether the server confirms it has no item with this id (deleted, or its file moved to a new item).

        Args:
            item_id: The server's item id.

        Returns:
            True only when the server answers that the item doesn't exist; False when it has it; None when that
            couldn't be asked (this default: a publisher that can't tell).
        """
        return None

    def live_files(self, item_id: str) -> tuple[str, ...]:
        """The local paths of the files the server item held when ``shows_many`` last read it.

        Check servers runs them too when the item drifted: a version replaced by a file this app never ran would
        otherwise keep the markers decided for the old file.

        Args:
            item_id: The server's item id.

        Returns:
            Each file's local candidates through the server's path mappings, sorted; empty when not read or where a
            server item holds one file (this default: Jellyfin and Emby keep each version as its own item).
        """
        return ()

    def shows_many(
        self, items: list[ReadBackItem], *, cancel_check: Callable[[], bool] | None = None
    ) -> dict[str, Shown | None]:
        """``shows`` for many items (Check servers). A server with a cheaper bulk read overrides it.

        Stops after ``UNREADABLE_IN_A_ROW`` failed reads in a row: a server that went down would otherwise cost every
        item left its request timeouts (``stopped_unreadable`` tells the caller).

        Args:
            items: ``(item_id, ours, kept_types, item_files)`` per item.
            cancel_check: True once the job is cancelled; items not read by then are left out.

        Returns:
            What ``shows`` answers, per item id read; None for an item whose read failed.
        """
        out: dict[str, Shown | None] = {}
        failed_in_a_row = 0
        for item_id, ours, kept_types, item_files in items:
            if failed_in_a_row >= UNREADABLE_IN_A_ROW or (cancel_check and cancel_check()):
                break
            try:
                out[item_id] = self.shows(item_id, ours, kept_types=kept_types, item_files=item_files)
            except Exception as exc:
                # Counted as unreadable by the caller: a transport error mustn't stop the other items' reads.
                logger.debug("Couldn't read back item {}: {}", item_id, type(exc).__name__)
                out[item_id] = None
            failed_in_a_row = failed_in_a_row + 1 if out[item_id] is None else 0
        return out

    # Set by ``types_not_made_for_file``: its None meant this setup can never tell (a Plex marker agent older than the
    # answer: its item read works but carries no ``stale_types``), not a passing problem such as a busy database. The
    # agent reports only its version and protocols, and its version didn't change with that field, so only a read can
    # tell; the pipeline stops asking such a server for the rest of the job.
    stale_types_unanswerable: bool = False

    def types_not_made_for_file(self, item_id: str) -> frozenset[MarkerType] | None:
        """The types whose own markers the server shows for this item were made for an earlier file at its path.

        Plex keeps an item's markers when its file is replaced, so they can describe the old file. Only Plex's
        database can tell (``plex_db``); every other server answers None.

        Args:
            item_id: Server item id.

        Returns:
            Those types (empty when none are); None when this server can't tell.
        """
        return None

    @abstractmethod
    def write(
        self,
        item_id: str,
        markers: list[Marker],
        *,
        previous: list[Marker] | None,
        duration_ms: int | None,
        canonical_path: str,
        own_previous: list[Marker] | None = None,
        kept_types: frozenset[MarkerType] = frozenset(),
        limits: FileLimits | None = None,
    ) -> list[Marker]:
        """Make the server item show the calling file's decided ``markers``, as far as the server allows.

        Args:
            item_id: Server item id.
            markers: The calling file's decided markers.
            previous: What this app last left on THAT SERVER ITEM, whichever file published it. Markers of ours
                that are no longer wanted are removed only where the server still shows exactly these. None when
                unknown (a failed write on a non-atomic publisher): each publisher then removes only what it can
                prove is ours.
            duration_ms: File duration, for "runs to the end" semantics; None when unknown.
            canonical_path: Local path of the calling file.
            own_previous: What the calling file last published when that was to a different item (the server merged
                or split items since). Publishers whose files carry their own copy of the markers (Plex's part
                ``extra_data``) remove that copy where it still serves exactly these; others ignore it.
            kept_types: The types the last write kept as the server's own (recorded ``last_kept_types``). Servers that
                can't tell our markers from their own (Plex) never touch those while the server is set to keep them;
                others ignore it.
            limits: The calling file's limits (``decide.FileLimits``). Plex never keeps its own markers of a type when
                every one of them can't be right for the file (``decide.unusable_server_marker``); others ignore it.

        Returns:
            The markers that are ours on the server item after the call, sorted by start (empty when none are).
            A server with one marker set per item (Plex) can return fewer types than ``markers``: those its other
            versions haven't decided the same way, or kept as its own. ``last_write_changed`` says whether the call
            changed the server; ``last_kept_types`` which types are the server's own and left alone.

        Raises:
            PublishError: The write failed. With ``atomic_writes`` nothing changed; otherwise the server may hold a
                partial write. ``state`` is set when the UI can explain the problem. Any other exception is a bug.
        """

    def project(self, markers: Iterable[Marker]) -> list[Marker]:
        """Keep supported types, ordered by start."""
        return sorted((m for m in markers if m.type in self.supported_types), key=lambda m: (m.start_ms, m.type.value))

    def projection_note(self, markers: Iterable[Marker], *, duration_ms: int | None) -> str:
        """How this server shows some of the markers it is sent differently from how they were decided.

        Args:
            markers: The markers this server shows as ours.
            duration_ms: File duration; None when unknown.

        Returns:
            Row and Inspector wording (Emby: credits that end before the file does); "" when every marker shows as
            decided.
        """
        return ""
