"""Markers already on a server: agreement evidence (spec §5.5 item 7: never a sole source, never the checked edge, may
shorten a skip), or what clients currently see (``include_ours=True``, for the Inspector)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from loguru import logger

from ...servers.base import ServerType
from ...servers.emby import NOTHING_STORED
from ..models import Candidate, MarkerType, Source
from ..publishers.emby import MARKERS_FEATURE, OwnVersion, own_version, without_plugin_rows
from ..publishers.jellyfin import bridge_key, core_key, segment_times

if TYPE_CHECKING:
    from ...servers.base import ServerConfig

# Bump when reading a server changes what a stored answer would hold, so servers are read again (spec §6.2).
# 2: an importer-plugin row names every importer plugin, not just the first (its copy's database is read from them).
# 3: an Emby item that isn't the file's own version gives no evidence (its markers describe another cut).
# 4: Emby's markers leave out the chapter rows our Bridge plugin wrote (its store says which), as Jellyfin's do.
# 5: a Plex item whose library hides a marker type (its own intro or credits setting off) gives no answer; its empty
#    answer was stored as "none there". Plex markers made for an earlier file at the path are stored flagged (the
#    pipeline asks Plex's database), so answers stored before are read again to be flagged; one from a Plex server that
#    shows our markers now can't be read again, so it stops counting instead (``pipeline._drop_older_reader_answer``).
READER_VERSION = 5
# The first reader version whose Plex answers were checked for markers made for an earlier file. An older answer from a
# Plex server that shows our markers now can't be read again to be checked, so it stops counting
# (``pipeline._drop_older_reader_answer``); a checked one counts as it did when it was read, whatever READER_VERSION is
# now. Raise it with READER_VERSION only when what a Plex answer holds changes.
PLEX_CHECKED_SINCE = 5
# Plex markers are one set per item: another version of the item further apart than this is another cut. Emby and
# Jellyfin keep each version's markers on its own item (spec §3.3); Emby's reader checks instead that the item is this
# file's own version.
SAME_CUT_MS = 2_000
_PLEX_TYPES = {"intro": MarkerType.INTRO, "credits": MarkerType.CREDITS}
_EMBY_MARKER_ROWS = frozenset({"IntroStart", "IntroEnd", "CreditsStart"})
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
    server: Any,
    config: ServerConfig,
    item_id: str,
    include_ours: bool,
    duration_ms: int | None,
    canonical_path: str | None,
) -> list[Candidate] | None:
    origin = config.id
    # A library with its own intro or credits setting off serves no marker of that type, so evidence is unknown there.
    # The list can't say "unknown" for one type alone: a partial answer would be stored as final and never read again.
    rows = server.get_markers(item_id, unknown_if_hidden=not include_ours)
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
    server: Any,
    config: ServerConfig,
    item_id: str,
    include_ours: bool,
    duration_ms: int | None,
    canonical_path: str | None,
) -> list[Candidate] | None:
    origin = config.id
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


def _nothing_stored_without_the_bridge(server: Any, config: ServerConfig) -> dict[str, Any] | None:
    """An empty marker store when the server has no Bridge plugin that could have stored markers of ours.

    The store read answers more than a 404 when the plugin isn't there: an Emby behind a proxy, or one that rejects an
    unrouted path, can answer 401, 502 or an HTML page, and every one of those would otherwise take the whole server's
    markers out of the evidence. The Ping route settles it, kept per server for a few minutes so a server whose
    markers route is broken doesn't get one ping per item.

    Args:
        server: Live Emby client.
        config: That server's ``ServerConfig`` (for the log line).

    Returns:
        A store with nothing in it when the Bridge (with its markers feature) isn't installed; None when it is, or
        when the server couldn't be asked.
    """
    info = server.get_bridge_info_cached()
    if info is None or (info.get("installed") and MARKERS_FEATURE in (info.get("features") or [])):
        logger.debug("Emby {}: the Bridge markers store couldn't be read; its markers are no evidence", config.name)
        return None
    return dict(NOTHING_STORED)


def _from_emby(
    server: Any,
    config: ServerConfig,
    item_id: str,
    include_ours: bool,
    duration_ms: int | None,
    canonical_path: str | None,
) -> list[Candidate] | None:
    origin = config.id
    if canonical_path is None:
        rows = server.get_chapter_markers(item_id)
    else:
        # Emby's path lookup can fall back to a search that returns another version's item, whose markers describe
        # another cut: the same check the publisher makes before it writes.
        answer = server.get_chapters_and_versions(item_id)
        if answer is None:
            return None
        rows, versions = answer
        state, elsewhere = own_version(item_id, versions, canonical_path, list(config.path_mappings or []))
        if state is not OwnVersion.YES:
            logger.debug(
                "{} item {} isn't this file's own version ({}{}); its markers aren't evidence for {}",
                config.name,
                item_id,
                state.value,
                f": this file is item {elsewhere}" if elsewhere else "",
                canonical_path,
            )
            return None
    if rows is None:
        return None
    if not include_ours and any(row["marker_type"] in _EMBY_MARKER_ROWS for row in rows):
        # The Bridge plugin's own store says which rows are ours, whatever markers.db knows (a lost or fresh config).
        state = server.get_emby_marker_state(item_id, missing_route_is_empty=True)
        if state is None:
            state = _nothing_stored_without_the_bridge(server, config)
        if state is None:
            # Our own published markers would otherwise count as a second opinion agreeing with ourselves.
            return None
        rows = without_plugin_rows(rows, state)
    starts: dict[str, list[int]] = {}
    for row in rows:
        starts.setdefault(row["marker_type"], []).append(row["start_ms"])
    intro_start, intro_end, credits_start = (starts.get(k, []) for k in ("IntroStart", "IntroEnd", "CreditsStart"))
    out = []
    # Emby keeps one intro and one credits start per item; anything else can't be paired without guessing. Each version
    # is its own item with its own chapter rows (spec §3.3), so no duration check.
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
    canonical_path: str | None = None,
) -> list[Candidate] | None:
    """Read one server's current markers for an item.

    Plex can't tell our markers from its own, so callers must not read a Plex item that any file has been published to
    (as evidence); after a lost or reset markers.db, markers we wrote there earlier read as Plex's. Jellyfin's and
    Emby's are told apart through their Bridge plugin's store, which lives on the server.

    Args:
        server: Live client for ``config``.
        config: The server's ``ServerConfig`` (type and id).
        item_id: The server's item id.
        include_ours: False (evidence): leave out segments our Jellyfin plugin serves and chapter rows our Emby plugin
            wrote (None when its store can't be read on an Emby that has the plugin), and give None for a Plex item
            whose library hides a marker type (its own intro or credits setting off: Plex then serves none of that
            type, ours included). True: everything clients see, ours included.
        duration_ms: This file's duration, when the markers are read as evidence for it. Plex serves one marker set
            per item, so an item whose versions aren't all this cut (within 2 s), or whose versions can't be read,
            gives None. Emby and Jellyfin markers belong to one version's own item: no duration check.
        canonical_path: This file's local path, when the markers are read as evidence for it. An Emby item that isn't
            this file's own version (Emby's path lookup can fall back to another version's item) gives None.

    Returns:
        Candidates with ``origin`` = server id (credits that run to the end have ``end_ms=None``); ``[]`` when the
        server has none; None when they couldn't be read or may describe another cut.
    """
    readers = {ServerType.PLEX: _from_plex, ServerType.JELLYFIN: _from_jellyfin, ServerType.EMBY: _from_emby}
    reader = readers.get(config.type)
    return reader(server, config, item_id, include_ours, duration_ms, canonical_path) if reader else None
