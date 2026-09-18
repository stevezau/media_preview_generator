"""Markers already on a server: agreement evidence (spec §5.5 item 7: never a sole source, never the checked edge, may
shorten a skip), or what clients currently see (``include_ours=True``, for the Inspector)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from ...servers.base import ServerType
from ..models import Candidate, MarkerType, Source
from ..publishers.jellyfin import bridge_key, core_key, segment_times

if TYPE_CHECKING:
    from ...servers.base import ServerConfig

# Bump when reading a server changes what a stored answer would hold, so servers are read again (spec §6.2).
# 2: an importer-plugin row names every importer plugin, not just the first (its copy's database is read from them).
READER_VERSION = 2
# Plex markers are one set per item: another version of the item further apart than this is another cut. Emby and
# Jellyfin keep each version's markers on its own item (spec §3.3), so their readers need no such check.
SAME_CUT_MS = 2_000
_PLEX_TYPES = {"intro": MarkerType.INTRO, "credits": MarkerType.CREDITS}
# Plugins that write IntroDB / TheIntroDB / SkipDB / AniSkip answers as the server's own segments, by the database they
# import ("intro db" covers TheIntroDB too). Intro Skipper is left out on purpose: it fingerprints the server's own
# files (local detection), it doesn't copy a crowd database.
_IMPORTER_DATABASES = {
    "introdb": re.compile(r"intro[ _-]?db", re.IGNORECASE),
    "skipdb": re.compile(r"skip[ _-]?db", re.IGNORECASE),
    "aniskip": re.compile(r"ani[ _-]?skip", re.IGNORECASE),
}


def importer_plugin(plugin_names: Iterable[str]) -> str | None:
    """The installed plugins that import a crowd skip database, so the server's markers are that data again.

    Args:
        plugin_names: Names of the server's installed plugins.

    Returns:
        Their names joined with ", " in the server's order, or None when there is none.
    """
    names = [name for name in plugin_names if any(p.search(name) for p in _IMPORTER_DATABASES.values())]
    return ", ".join(names) or None


def importer_database(text: str) -> str:
    """Which crowd database importer plugin names (or the :func:`imported_detail` naming them) point to.

    Args:
        text: Plugin names, or an imported evidence row's detail.

    Returns:
        "introdb", "skipdb" or "aniskip"; "" when none matches or several do (a server with importers of two
        databases: its markers could be a copy of either).
    """
    found = [database for database, pattern in _IMPORTER_DATABASES.items() if pattern.search(text)]
    return found[0] if len(found) == 1 else ""


def imported_detail(plugin_names: str) -> str:
    """Why a server's markers don't count as a second opinion (shown with its evidence in the Inspector)."""
    return f"Markers on this server look imported from {plugin_names}; not a second opinion for that database"


def _candidate(mtype: MarkerType, start_ms: int, end_ms: int | None, origin: str) -> Candidate:
    return Candidate(mtype, start_ms, end_ms, Source.SERVER_MARKERS, origin=origin)


def _one_cut(durations: list[int | None] | None, duration_ms: int) -> bool:
    """Whether an item's one marker set can describe this file: a single version, or every version is this cut."""
    if durations is None:
        return False
    if len(durations) <= 1:
        return True
    return all(d is not None and abs(d - duration_ms) <= SAME_CUT_MS for d in durations)


def _from_plex(
    server: Any, item_id: str, origin: str, include_ours: bool, duration_ms: int | None
) -> list[Candidate] | None:
    rows = server.get_markers(item_id)
    if rows is None:
        return None
    out = []
    for row in rows:
        mtype = _PLEX_TYPES.get(row["type"])
        if mtype is None:
            continue
        runs_to_end = mtype is MarkerType.CREDITS and row.get("final")
        out.append(_candidate(mtype, row["start_ms"], None if runs_to_end else row["end_ms"], origin))
    if out and duration_ms is not None and not _one_cut(server.get_part_durations(item_id), duration_ms):
        return None
    return out


def _from_jellyfin(
    server: Any, item_id: str, origin: str, include_ours: bool, duration_ms: int | None
) -> list[Candidate] | None:
    rows = server.get_media_segments(item_id)
    if rows is None:
        return None
    served = [(core_key(row), converted) for row in rows if (converted := segment_times(*core_key(row))) is not None]
    if not served:
        return []
    ours: set[tuple[object, object, object]] = set()
    if not include_ours:
        stored = server.get_bridge_markers(item_id)
        if stored is None:
            # Our own published segments would otherwise count as a second opinion agreeing with ourselves.
            return None
        ours = {bridge_key(s) for s in stored}
    return [_candidate(*converted, origin) for key, converted in served if key not in ours]


def _from_emby(
    server: Any, item_id: str, origin: str, include_ours: bool, duration_ms: int | None
) -> list[Candidate] | None:
    rows = server.get_chapter_markers(item_id)
    if rows is None:
        return None
    starts: dict[str, list[int]] = {}
    for row in rows:
        starts.setdefault(row["marker_type"], []).append(row["start_ms"])
    intro_start, intro_end, credits_start = (starts.get(k, []) for k in ("IntroStart", "IntroEnd", "CreditsStart"))
    out = []
    # Emby keeps one intro and one credits start per item; anything else can't be paired without guessing. Each version
    # is its own item with its own chapter rows (spec §3.3), so these describe this item's own cut: no version check.
    if len(intro_start) == 1 and len(intro_end) == 1:
        out.append(_candidate(MarkerType.INTRO, intro_start[0], intro_end[0], origin))
    if len(credits_start) == 1:
        out.append(_candidate(MarkerType.CREDITS, credits_start[0], None, origin))
    return out


def read_server_markers(
    server: Any,
    config: ServerConfig,
    item_id: str,
    *,
    include_ours: bool = False,
    duration_ms: int | None = None,
) -> list[Candidate] | None:
    """Read one server's current markers for an item.

    Plex and Emby can't tell our markers from their own, so callers must not read a Plex/Emby item that any file has
    been published to (as evidence). Jellyfin's are told apart through the Bridge plugin's store.

    Args:
        server: Live client for ``config``.
        config: The server's ``ServerConfig`` (type and id).
        item_id: The server's item id.
        include_ours: False (evidence): leave out segments our Jellyfin plugin serves. True: everything clients see,
            ours included. Plex and Emby return the same either way.
        duration_ms: This file's duration, when the markers are read as evidence for it. Plex serves one marker set
            per item, so an item whose versions aren't all this cut (within 2 s), or whose versions can't be read,
            gives None. Emby and Jellyfin markers belong to one version's own item and are never checked.

    Returns:
        Candidates with ``origin`` = server id (credits that run to the end have ``end_ms=None``); ``[]`` when the
        server has none; None when they couldn't be read or may describe another cut.
    """
    readers = {ServerType.PLEX: _from_plex, ServerType.JELLYFIN: _from_jellyfin, ServerType.EMBY: _from_emby}
    reader = readers.get(config.type)
    return reader(server, item_id, config.id, include_ours, duration_ms) if reader else None
