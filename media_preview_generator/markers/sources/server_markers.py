"""Markers already on a server: agreement evidence (spec §5.5 item 7, never a sole source, never the times), or what
clients currently see (``include_ours=True``, for the Inspector)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...servers.base import ServerType
from ..models import Candidate, MarkerType, Source
from ..publishers.jellyfin import bridge_key, core_key, segment_times

if TYPE_CHECKING:
    from ...servers.base import ServerConfig

_PLEX_TYPES = {"intro": MarkerType.INTRO, "credits": MarkerType.CREDITS}


def _candidate(mtype: MarkerType, start_ms: int, end_ms: int | None, origin: str) -> Candidate:
    return Candidate(mtype, start_ms, end_ms, Source.SERVER_MARKERS, origin=origin)


def _from_plex(server: Any, item_id: str, origin: str, include_ours: bool) -> list[Candidate] | None:
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
    return out


def _from_jellyfin(server: Any, item_id: str, origin: str, include_ours: bool) -> list[Candidate] | None:
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


def _from_emby(server: Any, item_id: str, origin: str, include_ours: bool) -> list[Candidate] | None:
    rows = server.get_chapter_markers(item_id)
    if rows is None:
        return None
    starts: dict[str, list[int]] = {}
    for row in rows:
        starts.setdefault(row["marker_type"], []).append(row["start_ms"])
    intro_start, intro_end, credits_start = (starts.get(k, []) for k in ("IntroStart", "IntroEnd", "CreditsStart"))
    out = []
    # Emby keeps one intro and one credits start per item; anything else can't be paired without guessing.
    if len(intro_start) == 1 and len(intro_end) == 1:
        out.append(_candidate(MarkerType.INTRO, intro_start[0], intro_end[0], origin))
    if len(credits_start) == 1:
        out.append(_candidate(MarkerType.CREDITS, credits_start[0], None, origin))
    return out


def read_server_markers(
    server: Any, config: ServerConfig, item_id: str, *, include_ours: bool = False
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

    Returns:
        Candidates with ``origin`` = server id (credits that run to the end have ``end_ms=None``); ``[]`` when the
        server has none; None when they couldn't be read.
    """
    readers = {ServerType.PLEX: _from_plex, ServerType.JELLYFIN: _from_jellyfin, ServerType.EMBY: _from_emby}
    reader = readers.get(config.type)
    return reader(server, item_id, config.id, include_ours) if reader else None
