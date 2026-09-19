"""Publisher contract (spec §6.3)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from loguru import logger

from ..models import Marker, MarkerType

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
    each other's markers on every run.

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
        if kept and versions_agree(kept, mine) and all(versions_agree(kept, t) for t in theirs):
            agreed.extend(kept)
        else:
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
    # Set by every ``write`` that read the item: the item's version files that write computed the marker set for. The
    # caller records them and passes them back to ``shows``. None where items have no shared versions (Jellyfin, Emby).
    last_item_files: tuple[str, ...] | None = None

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
