"""Publisher contract (spec §6.3)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from ..models import Marker, MarkerType


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
    """Writes and reads markers on one server."""

    supported_types: frozenset[MarkerType] = frozenset()
    name: str = ""
    # True when a PublishError guarantees nothing changed on the server (one transaction). A caller may then keep
    # its record of what is ours; otherwise what is ours after a failed write is unknown.
    atomic_writes: bool = False

    @abstractmethod
    def capability(self) -> CapabilityReport:
        """Check whether this server can receive markers."""

    @abstractmethod
    def read(self, item_id: str) -> list[Marker]:
        """Markers currently on the server for an item, as clients see them (supported types only)."""

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

        Returns:
            The markers that are ours on the server item after the call, sorted by start (empty when none are).
            A server with one marker set per item (Plex) can return fewer types than ``markers``: those its other
            versions haven't decided the same way.

        Raises:
            PublishError: The write failed. With ``atomic_writes`` nothing changed; otherwise the server may hold a
                partial write. ``state`` is set when the UI can explain the problem. Any other exception is a bug.
        """

    def project(self, markers: Iterable[Marker]) -> list[Marker]:
        """Keep supported types, ordered by start."""
        return sorted((m for m in markers if m.type in self.supported_types), key=lambda m: (m.start_ms, m.type.value))
