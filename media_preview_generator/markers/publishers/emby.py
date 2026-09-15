"""Emby publisher via the Media Preview Bridge for Emby plugin (spec §3.3, §6.3).

Emby has no marker write API. The plugin stores what we POST and writes it into the item's chapter rows as IntroStart /
IntroEnd / CreditsStart, next to the file's own chapters and any marker rows it didn't write, and writes it back after a
refresh deletes it. Emby keeps no credits end: a Skip Credits button always skips to the end of the file, past any scene
after the credits. Those credits are sent anyway (owner decision 2026-09-14) and the server row says so. Every POST is
confirmed by reading the item's chapters back.

Emby keeps each version of a video as its own item with its own chapters, and its player shows the chapters of the
version playing (lab, 2026-09-15). Each version is published on its own, like a Jellyfin item, once the item the job
found is confirmed to be this file's version.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

import requests
from loguru import logger

from ...servers.ownership import apply_path_mappings
from ..decide import EOF_CLAMP_MS
from ..models import Marker, MarkerType
from .base import (
    Capability,
    CapabilityReport,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
    Shown,
    compare_shown,
)

if TYPE_CHECKING:
    from ...servers.base import ServerConfig
    from ...servers.emby import EmbyServer
    from ..settings import ServerMarkersSettings

TICKS_PER_MS = 10_000
MARKERS_FEATURE = "markers"
CREDITS_BEFORE_END_NOTE = "Emby skips to the end of the file"
# Emby's chapter MarkerType values for each type we write (intro start and end come as a pair).
_ROW_TYPES: dict[MarkerType, tuple[str, ...]] = {
    MarkerType.INTRO: ("IntroStart", "IntroEnd"),
    MarkerType.CREDITS: ("CreditsStart",),
}


def credits_note(markers: Iterable[Marker], duration_ms: int | None) -> str:
    """Row and Inspector wording for credits that end more than 2 s before the file does.

    Args:
        markers: Markers Emby shows (or will show) as ours.
        duration_ms: File duration; None when unknown (then nothing can be said).

    Returns:
        ``CREDITS_BEFORE_END_NOTE``, or "" when every credits marker runs to the end.
    """
    if duration_ms is None:
        return ""
    early = any(m.type is MarkerType.CREDITS and m.end_ms < duration_ms - EOF_CLAMP_MS for m in markers)
    return CREDITS_BEFORE_END_NOTE if early else ""


def _times(markers: Iterable[Marker]) -> list[tuple[MarkerType, int, int]]:
    return [(m.type, m.start_ms, m.end_ms) for m in markers]


def _file_size(path: str) -> int | None:
    try:
        return os.stat(path).st_size
    except OSError:
        return None


def _has_rows(rows: list[dict[str, Any]], mtype: MarkerType) -> bool:
    return any(row["marker_type"] in _ROW_TYPES[mtype] for row in rows)


def _served(rows: list[dict[str, Any]], ours: Iterable[Marker]) -> dict[MarkerType, list[tuple[int, int]]]:
    """Emby's chapter markers in ``compare_shown`` form.

    An intro is exactly one IntroStart and one IntroEnd. Each CreditsStart gets the end of our credits, since Emby keeps
    no end: credits are compared by their start.
    """
    starts: dict[str, list[int]] = {}
    for row in rows:
        starts.setdefault(row["marker_type"], []).append(row["start_ms"])
    served: dict[MarkerType, list[tuple[int, int]]] = {}
    if len(starts.get("IntroStart", [])) == 1 and len(starts.get("IntroEnd", [])) == 1:
        served[MarkerType.INTRO] = [(starts["IntroStart"][0], starts["IntroEnd"][0])]
    credits_end = next((m.end_ms for m in ours if m.type is MarkerType.CREDITS), None)
    served[MarkerType.CREDITS] = [(start, credits_end) for start in starts.get("CreditsStart", [])]
    return served


def _shows(rows: list[dict[str, Any]], markers: list[Marker], mtype: MarkerType) -> bool:
    """Whether the item's rows of ``mtype`` are exactly ``markers`` of that type (credits by start)."""
    mine = [m for m in markers if m.type is mtype]
    return bool(mine) and compare_shown(mine, _served(rows, mine), others_alongside=False) is Shown.OURS


def _stale_ours(rows: list[dict[str, Any]], wanted: list[Marker], prior: list[Marker]) -> frozenset[MarkerType]:
    """The wanted types whose rows are still what this app left before (``prior``) instead of ``wanted``."""
    return frozenset(
        mtype for mtype in {m.type for m in wanted} if not _shows(rows, wanted, mtype) and _shows(rows, prior, mtype)
    )


def _kept_types(
    rows: list[dict[str, Any]], wanted: list[Marker], keep: bool, prior: list[Marker] = ()
) -> frozenset[MarkerType] | None:
    """The wanted types Emby shows its own rows of instead of ours, as the plugin leaves them without ``ReplaceOwn``.

    Rows that are what this app left before (``prior``) are ours, never Emby's.

    Returns:
        Those types (empty when every type shows ours); None when a type shows neither ours nor, under "Keep Emby's",
        rows of Emby's own.
    """
    kept = set()
    for mtype in dict.fromkeys(m.type for m in wanted):
        if _shows(rows, wanted, mtype):
            continue
        if not keep or not _has_rows(rows, mtype) or _shows(rows, list(prior), mtype):
            return None
        kept.add(mtype)
    return frozenset(kept)


def _stored_markers(state: dict[str, Any]) -> list[Marker]:
    """The markers the plugin stores for an item (a credits marker gets its start as end: Emby keeps none)."""
    stored = []
    if state["intro_start_ticks"] is not None and state["intro_end_ticks"] is not None:
        stored.append(
            Marker(
                MarkerType.INTRO,
                state["intro_start_ticks"] // TICKS_PER_MS,
                state["intro_end_ticks"] // TICKS_PER_MS,
                (),
            )
        )
    if state["credits_start_ticks"] is not None:
        start = state["credits_start_ticks"] // TICKS_PER_MS
        stored.append(Marker(MarkerType.CREDITS, start, start, ()))
    return stored


def _post_body(wanted: list[Marker], file_size: int | None) -> dict[str, int | None]:
    intro = next((m for m in wanted if m.type is MarkerType.INTRO), None)
    credits = next((m for m in wanted if m.type is MarkerType.CREDITS), None)
    return {
        "intro_start_ticks": intro.start_ms * TICKS_PER_MS if intro else None,
        "intro_end_ticks": intro.end_ms * TICKS_PER_MS if intro else None,
        "credits_start_ticks": credits.start_ms * TICKS_PER_MS if credits else None,
        "file_size": file_size,
    }


class EmbyMarkerPublisher(MarkerPublisher):
    """Pushes markers to the Emby Bridge plugin, which keeps them in Emby's chapter rows."""

    supported_types = frozenset(_ROW_TYPES)
    name = "emby_bridge"
    # The plugin stores before its chapters are read back, so a PublishError can follow a POST that changed its store.
    atomic_writes = False
    # Under "Keep Emby's" the plugin still stores ours for a kept type and shows them once Emby's rows are gone.
    kept_types_hold_ours = True

    def __init__(
        self,
        server: EmbyServer,
        config: ServerConfig,
        settings: ServerMarkersSettings,
        *,
        settings_provider: Callable[[], ServerMarkersSettings] | None = None,
    ) -> None:
        """Create the publisher.

        Args:
            server: Live ``EmbyServer`` client.
            config: That server's ``ServerConfig``.
            settings: That server's ``ServerMarkersSettings``.
            settings_provider: The saved settings right now, read before each write for "When Emby has its own
                markers" (the pipeline compares the saved setting too); None uses ``settings``.
        """
        self._server = server
        self._config = config
        self._settings = settings
        self._settings_provider = settings_provider

    def _keeps_emby(self) -> bool:
        settings = self._settings_provider() if self._settings_provider is not None else self._settings
        return settings.on_emby_redetect == "keep_emby"

    def projection_note(self, markers: Iterable[Marker], *, duration_ms: int | None) -> str:
        """Say when Emby's credits skip goes past the end of our credits (see ``credits_note``)."""
        return credits_note(markers, duration_ms)

    def capability(self) -> CapabilityReport:
        """Check the switch, the plugin (with the markers feature) and administrator access."""
        if not self._settings.enabled:
            return CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server")
        info = self._server.get_bridge_info()
        if info is None:
            return CapabilityReport(Capability.UNREACHABLE, "Can't reach this Emby server")
        if not info.get("installed"):
            return CapabilityReport(
                Capability.NEEDS_PLUGIN,
                "Install the Media Preview Bridge for Emby plugin",
                {"catalog_listed": self._server.bridge_catalog_listed()},
            )
        version = info.get("version")
        if MARKERS_FEATURE not in (info.get("features") or []):
            return CapabilityReport(
                Capability.PLUGIN_OUTDATED,
                f"Update Media Preview Bridge for Emby (installed {version or 'unknown'}) to get markers support",
                {"plugin_version": version},
            )
        access = self._server.get_bridge_markers_access()
        if access == "unauthorized":
            return CapabilityReport(Capability.MISCONFIGURED, "Emby rejected this server's credentials; reconnect it")
        if access == "forbidden":
            return CapabilityReport(
                Capability.MISCONFIGURED,
                "Emby refused the Media Preview Bridge markers endpoint; this server's API key or user needs "
                "administrator rights",
            )
        if access != "ok":
            return CapabilityReport(
                Capability.UNREACHABLE, "Can't reach the Media Preview Bridge markers endpoint on this Emby server"
            )
        return CapabilityReport(
            Capability.READY,
            "Media Preview Bridge for Emby plugin",
            {"plugin_version": version, "can_show": ["intro", "credits"]},
        )

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
        """Replace the plugin's markers for the item and confirm Emby's chapters show them.

        The item must be this file's own version: Emby lists an item's other versions with it, each as its own item.
        The POST carries this file's size, which the plugin compares with the item's file. "Use ours" sends
        ``ReplaceOwn``, so ours replace Emby's own rows of a type. "Keep Emby's" doesn't: the plugin leaves a type that
        has Emby's rows alone (it still stores ours and shows them once Emby's rows are gone), and that type is reported
        in ``last_kept_types``. An empty set deletes the plugin's markers when something of ours may be there
        (``previous`` None or not empty, or a kept type). Nothing is sent when ``previous`` already is this set, the
        plugin holds exactly it for this very file, and Emby's chapters already show what a POST would leave there.

        ``own_previous`` is ignored: every Emby version is an item with its own chapters.

        Returns:
            The markers that are ours on the item now: ``project(markers)`` without kept types, or ``[]``.

        Raises:
            ItemNotFoundError: Emby doesn't know the item (yet), or it has several versions and none of them is this
                file.
            PublishError: The item is another version than this file, not written, or stored but not shown (then
                removed again, best effort).
        """
        self.last_write_changed = False
        keep = self._keeps_emby()
        kept_before = frozenset(kept_types) if keep else frozenset()
        self.last_kept_types = kept_before
        wanted = self.project(markers)
        prior = self.project(previous) if previous is not None else []
        try:
            if not wanted:
                self.last_kept_types = frozenset()
                if previous is None or previous or kept_types:
                    self._check(self._server.delete_emby_markers(item_id), item_id)
                    self.last_write_changed = True
                return []
            item = self._server.get_chapters_and_versions(item_id)
            if item is None:
                raise PublishError("Couldn't read this Emby item; markers not written")
            rows, versions = item
            self._confirm_version(item_id, versions, canonical_path)
            body = _post_body(wanted, _file_size(canonical_path))
            ours_before = [m for m in wanted if m.type not in kept_before]
            if previous is not None and _times(prior) == _times(ours_before):
                kept = self._kept_if_nothing_to_send(item_id, rows, wanted, body, keep)
                if kept is not None:
                    self.last_kept_types = kept
                    return [m for m in wanted if m.type not in kept]
        except requests.RequestException as exc:
            raise PublishError(
                f"Can't reach Emby ({type(exc).__name__}); markers not written", state=Capability.UNREACHABLE
            ) from exc
        answer = self._post(item_id, body, replace_own=not keep)
        # Only a POST that put rows of ours on the item, or took earlier ones off, changed what Emby shows.
        self.last_write_changed = (answer.get("Stored") or 0) > 0 or bool(previous)
        rows = self._chapters_after_post(item_id)
        stale = _stale_ours(rows, wanted, prior) if keep else frozenset()
        if stale:
            # The plugin no longer knows those rows are ours (its store was lost), so without ReplaceOwn it kept them as
            # Emby's. ReplaceOwn is per POST: replace just those types, then store the whole set again.
            logger.info("Emby {}: item {} still shows markers this app wrote earlier; replacing them",
                        self._config.name, item_id)  # fmt: skip
            self._post(item_id, _post_body([m for m in wanted if m.type in stale], body["file_size"]), replace_own=True)
            self._post(item_id, body, replace_own=False)
            self.last_write_changed = True
            rows = self._chapters_after_post(item_id)
        kept = _kept_types(rows, wanted, keep, prior)
        if kept is None:
            self._remove_unconfirmed(item_id)
            raise PublishError("Emby stored the markers but its chapters don't show them; check the Emby log")
        newly_kept = kept - kept_before
        if newly_kept:
            logger.info(
                "Emby {}: item {} shows Emby's own {} instead of ours; keeping them (Keep Emby's)",
                self._config.name,
                item_id,
                " and ".join(t.value for t in MarkerType if t in newly_kept),
            )
        self.last_kept_types = kept
        ours = [m for m in wanted if m.type not in kept]
        logger.info("Emby {}: item {} now shows {} marker(s) of ours", self._config.name, item_id, len(ours))
        return ours

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
        """Read the item's chapter markers.

        Args:
            item_id: Emby item id.
            ours: What this app last left on the item.
            kept_types: Types kept as Emby's own; one with no rows left, or whose rows are the markers the plugin
                stores for it (ours, written back after a refresh deleted Emby's), is MISSING: the job records it again.
            item_files: Ignored: each version is published on its own (see ``write``).

        Returns:
            How Emby's markers compare with ``ours`` (credits by their start); GONE when Emby has no such item; None
            when they couldn't be read.
        """
        rows = self._server.get_chapter_markers(item_id)
        if rows is None:
            # One more request only when the read failed: a deleted item is drift to fix, not a read to warn about.
            return Shown.GONE if self.item_missing(item_id) is True else None
        if any(not _has_rows(rows, mtype) for mtype in kept_types):
            return Shown.MISSING
        if kept_types:
            # A refresh that deleted Emby's own rows lets the plugin write back ours for a kept type (it stores them).
            state = self._server.get_emby_marker_state(item_id)
            if state is None:
                return None
            if any(_shows(rows, _stored_markers(state), mtype) for mtype in kept_types):
                return Shown.MISSING
        return compare_shown(self.project(ours), _served(rows, ours), others_alongside=False)

    def _post(self, item_id: str, body: dict[str, int | None], *, replace_own: bool) -> dict[str, Any]:
        """POST ``body``; raises on an error answer, and (after removing it again) on a Stale one."""
        try:
            answer = self._check(self._server.put_emby_markers(item_id, **body, replace_own=replace_own), item_id)
        except requests.RequestException as exc:
            raise PublishError(
                f"Can't reach Emby ({type(exc).__name__}); markers not written", state=Capability.UNREACHABLE
            ) from exc
        if answer.get("Stale"):
            self._remove_unconfirmed(item_id)
            raise PublishError(
                "Emby sees a different file size for this item than the analysed file; markers not shown"
            )
        return answer

    def _chapters_after_post(self, item_id: str) -> list[dict[str, Any]]:
        rows = self._server.get_chapter_markers(item_id)
        if rows is None:
            self._remove_unconfirmed(item_id)
            raise PublishError("Stored on Emby but couldn't confirm it is shown", state=Capability.UNREACHABLE)
        return rows

    def _kept_if_nothing_to_send(
        self, item_id: str, rows: list[dict[str, Any]], wanted: list[Marker], body: dict[str, int | None], keep: bool
    ) -> frozenset[MarkerType] | None:
        """The kept types when a POST of ``body`` would change nothing; None when it would (or that can't be told).

        Nothing changes when the plugin stores exactly ``body`` for the file on disk now and every type of the item's
        chapter ``rows`` already shows ours or, under "Keep Emby's", Emby's own rows.
        """
        if body["file_size"] is None:
            return None
        state = self._server.get_emby_marker_state(item_id)
        if state is None or state["stale"] or any(state[key] != value for key, value in body.items()):
            return None
        return _kept_types(rows, wanted, keep)

    def _local_candidates(self, server_path: str) -> list[str]:
        return apply_path_mappings(server_path, list(self._config.path_mappings or [])) or [server_path]

    def _confirm_version(self, item_id: str, versions: list[tuple[str, str | None]], canonical_path: str) -> None:
        """Make sure ``item_id`` is this file's own version of the Emby item.

        An item with one file is the item the job found for this path, even when the server's path mappings don't map
        its path back to it.

        Raises:
            PublishError: Emby lists this file as another version's item.
            ItemNotFoundError: The item has several versions and none of them is this file.
        """
        this_file = [version for path, version in versions if canonical_path in self._local_candidates(path)]
        if item_id in this_file:
            return
        elsewhere = next((version for version in this_file if version), None)
        if elsewhere is not None:
            raise PublishError(
                f"This file is Emby item {elsewhere}, another version of item {item_id}; markers not written"
            )
        if len(versions) > 1:
            raise ItemNotFoundError(
                "This Emby item has no version matching this file (yet); check the server's path mappings"
            )

    def _remove_unconfirmed(self, item_id: str) -> None:
        try:
            resp = self._server.delete_emby_markers(item_id)
        except requests.RequestException as exc:
            logger.warning(
                "Emby {}: couldn't remove unconfirmed markers for item {} ({})",
                self._config.name,
                item_id,
                type(exc).__name__,
            )
            return
        if resp.status_code != 200:
            logger.warning(
                "Emby {}: couldn't remove unconfirmed markers for item {} (HTTP {})",
                self._config.name,
                item_id,
                resp.status_code,
            )

    @staticmethod
    def _check(resp: requests.Response, item_id: str) -> dict[str, Any]:
        """The plugin's ``MarkersResponse``; raises for anything but a 200 that stored or removed the markers."""
        try:
            body: Any = resp.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else None
        error = body.get("Error") if body is not None else None
        if resp.status_code == 200:
            if body is None:
                raise PublishError("Media Preview Bridge for Emby returned an unreadable answer")
            if body.get("Found") is False:
                raise ItemNotFoundError(f"Emby doesn't know item {item_id} (yet)")
            if error:
                raise PublishError(f"Media Preview Bridge for Emby refused the markers: {error}")
            return body
        if resp.status_code == 404:
            raise PublishError(
                "Media Preview Bridge for Emby isn't installed or has no markers endpoint",
                state=Capability.NEEDS_PLUGIN,
            )
        if resp.status_code == 401:
            raise PublishError("Emby rejected this server's credentials (HTTP 401); reconnect it")
        if resp.status_code == 403:
            raise PublishError(
                "Emby refused the markers write (HTTP 403); the server's API key or user needs administrator rights"
            )
        if resp.status_code in (502, 503, 504):
            raise PublishError(
                f"Emby is unavailable (HTTP {resp.status_code}); markers not written", state=Capability.UNREACHABLE
            )
        detail = f": {error}" if isinstance(error, str) and error else ""
        raise PublishError(f"Media Preview Bridge for Emby returned HTTP {resp.status_code}{detail}")
