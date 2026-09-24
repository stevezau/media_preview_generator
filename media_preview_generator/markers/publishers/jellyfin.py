"""Jellyfin publisher via the Media Preview Bridge plugin (spec §3.2, §6.3).

Jellyfin has no API for writing media segments. The Bridge plugin stores what we POST and serves it through its own
segment provider, so markers survive scans, refreshes and restarts. Each POST replaces everything the plugin stored for
the item and carries the file's current size (the pipeline re-checks file identity before publishing): the plugin serves
nothing once the file on disk has a different size. Jellyfin answers 200 even when it serves nothing (provider switched
off for the library, provider error, stale size), so every POST is confirmed against core ``/MediaSegments``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import requests
from loguru import logger

from ..models import Marker, MarkerType
from .base import Capability, CapabilityReport, ItemNotFoundError, MarkerPublisher, PublishError, Shown, compare_shown

if TYPE_CHECKING:
    from ...servers.base import ServerConfig
    from ...servers.jellyfin import JellyfinServer
    from ..decide import FileLimits
    from ..settings import ServerMarkersSettings

TICKS_PER_MS = 10_000
MARKERS_FEATURE = "markers"
SEGMENT_TYPES: dict[MarkerType, str] = {
    MarkerType.INTRO: "Intro",
    MarkerType.CREDITS: "Outro",
    MarkerType.RECAP: "Recap",
    MarkerType.PREVIEW: "Preview",
}
_FROM_SEGMENT_TYPE = {name: mtype for mtype, name in SEGMENT_TYPES.items()}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def segment_times(type_name: object, start_ticks: object, end_ticks: object) -> tuple[MarkerType, int, int] | None:
    """Convert one Jellyfin segment to ``(type, start_ms, end_ms)``.

    Args:
        type_name: Jellyfin segment type (``Intro``, ``Outro``, ``Recap``, ``Preview``; others are ignored).
        start_ticks: Start in ticks.
        end_ticks: End in ticks.

    Returns:
        The converted segment, or None for a type we don't manage or non-integer ticks.
    """
    mtype = _FROM_SEGMENT_TYPE.get(type_name) if isinstance(type_name, str) else None
    if mtype is None or not _is_int(start_ticks) or not _is_int(end_ticks):
        return None
    return mtype, start_ticks // TICKS_PER_MS, end_ticks // TICKS_PER_MS


def bridge_key(segment: dict[str, Any]) -> tuple[object, object, object]:
    """``(type, startTicks, endTicks)`` of a segment as the Bridge plugin stores it."""
    return segment.get("type"), segment.get("startTicks"), segment.get("endTicks")


def core_key(row: dict[str, Any]) -> tuple[object, object, object]:
    """The same key for a core ``/MediaSegments`` row, which uses PascalCase names."""
    return row.get("Type"), row.get("StartTicks"), row.get("EndTicks")


def _times(markers: list[Marker]) -> list[tuple[MarkerType, int, int]]:
    return [(m.type, m.start_ms, m.end_ms) for m in markers]


def _file_size(path: str) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


class JellyfinMarkerPublisher(MarkerPublisher):
    """Pushes markers to the Bridge plugin, which serves them as media segments."""

    supported_types = frozenset(SEGMENT_TYPES)
    name = "jellyfin_bridge"
    # The plugin stores before Jellyfin serves, so a PublishError can follow a POST that already changed its store.
    atomic_writes = False

    def __init__(self, server: JellyfinServer, config: ServerConfig, settings: ServerMarkersSettings) -> None:
        """Create the publisher.

        Args:
            server: Live ``JellyfinServer`` client.
            config: That server's ``ServerConfig``.
            settings: That server's ``ServerMarkersSettings``.
        """
        self._server = server
        self._config = config
        self._settings = settings

    def capability(self) -> CapabilityReport:
        """Check the switch, the plugin (with the markers feature) and administrator access."""
        if not self._settings.enabled:
            return CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server")
        info = self._server.get_bridge_info()
        if info is None:
            return CapabilityReport(Capability.UNREACHABLE, "Can't reach this Jellyfin server")
        if not info.get("installed"):
            return CapabilityReport(Capability.NEEDS_PLUGIN, "Install the Media Preview Bridge plugin")
        version = info.get("version")
        if MARKERS_FEATURE not in (info.get("features") or []):
            return CapabilityReport(
                Capability.PLUGIN_OUTDATED,
                f"Update Media Preview Bridge (installed {version or 'unknown'}) to get markers support",
                {"plugin_version": version},
            )
        # No version check: lab P3 showed a 10.11 build on 12.0 doesn't load (Ping 404 -> NEEDS_PLUGIN above).
        access = self._server.get_bridge_markers_access()
        if access == "unauthorized":
            return CapabilityReport(
                Capability.MISCONFIGURED, "Jellyfin rejected this server's credentials; reconnect it"
            )
        if access == "forbidden":
            return CapabilityReport(
                Capability.MISCONFIGURED,
                "Jellyfin refused the Media Preview Bridge markers endpoint; this server's API key or user needs "
                "administrator rights",
            )
        if access != "ok":
            return CapabilityReport(
                Capability.UNREACHABLE, "Can't reach the Media Preview Bridge markers endpoint on this Jellyfin server"
            )
        return CapabilityReport(
            Capability.READY,
            "Media Preview Bridge plugin",
            {"plugin_version": version, "can_show": ["intro", "credits", "recap", "preview"]},
        )

    def _shown(self, item_id: str, segments: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        """The given Bridge-shaped segments that Jellyfin serves right now; None when that can't be read."""
        rows = self._server.get_media_segments(item_id)
        if rows is None:
            return None
        served = {core_key(row) for row in rows}
        return [seg for seg in segments if bridge_key(seg) in served]

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
        """Replace the plugin's markers for the item and confirm Jellyfin serves them.

        An empty set deletes them instead, when something was published before or that is unknown (``previous`` None).
        When ``previous`` already is this set, the plugin holds it for this very file (its stored size is the size on
        disk now) and Jellyfin still serves all of it, nothing is sent.

        Writes are not atomic (``atomic_writes`` False): a ``PublishError`` from the confirmation comes after the POST.
        The unconfirmed markers are then deleted (best effort); if that DELETE fails too, the plugin store keeps them
        until the next write for the item, so after a failure callers pass ``previous=None`` and write again.

        ``own_previous`` is ignored: Jellyfin item ids are per version, so no other file's markers share the item.
        ``kept_types`` and ``limits`` are ignored: Jellyfin serves every provider's segments side by side, so nothing
        is kept instead.

        Returns:
            The markers that are ours on this item now: ``project(markers)``, or ``[]`` after a delete or when there
            was nothing to send.

        Raises:
            ItemNotFoundError: Jellyfin doesn't know the item yet.
            PublishError: Not written, or stored but not shown and removed again. ``state`` is set only for problems
                of the whole server (unreachable, plugin missing); problems of this item or library leave it None.
        """
        self.last_write_changed = False
        wanted = self.project(markers)
        if wanted and previous is not None and _times(self.project(previous)) == _times(wanted):
            # A forced run, a restore check or a replaced file with the same markers. /MediaSegments is Jellyfin's
            # copy of the provider's last answer and still lists ours for a replaced file, while the plugin, holding
            # the old file's size, serves nothing once Jellyfin refreshes it: a POST stores the new size.
            if self._plugin_holds_this_file(item_id, canonical_path) and self.shows(item_id, wanted) is Shown.OURS:
                return wanted
        try:
            if not wanted:
                if previous is None or previous:
                    self._check(self._server.delete_bridge_markers(item_id), item_id)
                    self.last_write_changed = True
                return []
            segments = [
                {
                    "type": SEGMENT_TYPES[m.type],
                    "startTicks": m.start_ms * TICKS_PER_MS,
                    "endTicks": m.end_ms * TICKS_PER_MS,
                }
                for m in wanted
            ]
            resp = self._server.put_bridge_markers(item_id, segments, file_size=_file_size(canonical_path))
            self._check(resp, item_id)
        except requests.RequestException as exc:
            raise PublishError(
                f"Can't reach Jellyfin ({type(exc).__name__}); markers not written", state=Capability.UNREACHABLE
            ) from exc
        self.last_write_changed = True
        self._confirm_shown(item_id, segments)
        logger.info("Jellyfin {}: stored {} marker(s) for item {}", self._config.name, len(wanted), item_id)
        return wanted

    def _plugin_holds_this_file(self, item_id: str, canonical_path: str) -> bool:
        """Whether the plugin's markers for the item were stored for the file on disk now (same size, not stale)."""
        size = _file_size(canonical_path)
        state = self._server.get_bridge_marker_state(item_id)
        return size is not None and state is not None and not state["stale"] and state["fileSize"] == size

    def item_missing(self, item_id: str) -> bool | None:
        """Whether the server answers that it has no such item (``MediaServer.item_missing``: 404 or no match).

        Args:
            item_id: The server's item id.

        Returns:
            True when it doesn't exist, False when it does, None when the server couldn't be asked.
        """
        return self._server.item_missing(item_id)

    def shows(
        self,
        item_id: str,
        ours: list[Marker],
        *,
        kept_types: frozenset[MarkerType] = frozenset(),
        item_files: tuple[str, ...] | None = None,
    ) -> Shown | None:
        """Read core ``/MediaSegments`` for the item: what Jellyfin serves from every provider.

        Args:
            item_id: Jellyfin item id.
            ours: What this app last left on the item.
            kept_types: Ignored (see ``write``).
            item_files: Ignored (see ``write``).

        Returns:
            Whether Jellyfin still serves each of ``ours`` (another provider's segments may sit alongside); GONE when
            Jellyfin has no such item; None when the segments couldn't be read.
        """
        try:
            rows = self._server.get_media_segments(item_id, raise_no_answer=True)
        except (requests.ConnectionError, requests.Timeout) as exc:
            # No answer at all: asking whether the item exists would wait just as long (a hung Jellyfin, 20 items in
            # a row).
            logger.debug("Jellyfin {}: couldn't read item {} back: {}", self._config.name, item_id, type(exc).__name__)
            return None
        if rows is None:
            # Only after an error answer is Jellyfin asked whether the item exists (one or two more requests, see
            # JellyfinServer.item_missing): a deleted item is drift to fix, not a read to warn about.
            return Shown.GONE if self.item_missing(item_id) is True else None
        served: dict[MarkerType, list[tuple[int, int]]] = {}
        for row in rows:
            converted = segment_times(*core_key(row))
            if converted is not None:
                served.setdefault(converted[0], []).append(converted[1:])
        return compare_shown(self.project(ours), served, others_alongside=True)

    def _confirm_shown(self, item_id: str, segments: list[dict[str, Any]]) -> None:
        shown = self._shown(item_id, segments)
        if shown is not None and len(shown) == len(segments):
            return
        # The error is worked out first: it reads the plugin's stale flag, which the cleanup DELETE removes.
        error = self._unconfirmed_error(item_id, len(segments), shown)
        self._remove_unconfirmed(item_id)
        raise error

    def _unconfirmed_error(self, item_id: str, stored: int, shown: list[dict[str, Any]] | None) -> PublishError:
        if shown is None:
            return PublishError("Stored on Jellyfin but couldn't confirm it is shown", state=Capability.UNREACHABLE)
        if not shown:
            state = self._server.get_bridge_marker_state(item_id)
            if state is not None and state.get("stale"):
                return PublishError(
                    "Jellyfin sees a different file size for this item than the analysed file; markers stored but "
                    "not shown"
                )
            if state is not None:
                return PublishError(
                    'Jellyfin stored the markers but isn\'t showing them. Turn on the "Media Preview Bridge" media '
                    "segment provider for this library, then check the Jellyfin log"
                )
        return PublishError(
            f"Jellyfin stored {stored} markers but serves only {len(shown or [])}; check the Jellyfin log"
        )

    def _remove_unconfirmed(self, item_id: str) -> None:
        try:
            resp = self._server.delete_bridge_markers(item_id)
        except requests.RequestException as exc:
            logger.warning(
                "Jellyfin {}: couldn't remove unconfirmed markers for item {} ({})",
                self._config.name,
                item_id,
                type(exc).__name__,
            )
            return
        if resp.status_code not in (200, 204):
            logger.warning(
                "Jellyfin {}: couldn't remove unconfirmed markers for item {} (HTTP {})",
                self._config.name,
                item_id,
                resp.status_code,
            )

    @staticmethod
    def _check(resp: requests.Response, item_id: str) -> None:
        if resp.status_code in (200, 204):
            return
        try:
            body: Any = resp.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if resp.status_code == 404:
            if error == "item not found":
                raise ItemNotFoundError(f"Jellyfin doesn't know item {item_id} (yet)")
            raise PublishError(
                "Media Preview Bridge has no markers endpoint; update the plugin", state=Capability.NEEDS_PLUGIN
            )
        if resp.status_code == 401:
            raise PublishError("Jellyfin rejected this server's credentials (HTTP 401); reconnect it")
        if resp.status_code == 403:
            raise PublishError(
                "Jellyfin refused the markers write (HTTP 403); the server's API key or user needs administrator rights"
            )
        if resp.status_code in (502, 503, 504):
            raise PublishError(
                f"Jellyfin is unavailable (HTTP {resp.status_code}); markers not written", state=Capability.UNREACHABLE
            )
        detail = f": {error}" if error else ""
        raise PublishError(f"Media Preview Bridge returned HTTP {resp.status_code}{detail}")
