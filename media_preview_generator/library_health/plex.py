"""Plex counters: previews, loudness and intro/credits per library.

Reads Plex's own database read-only (one connection, four queries) so a 100k-file library takes seconds. Without
database access (Plex marker agent, or no config folder) it falls back to Plex's API, which only knows previews.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, cast

from loguru import logger

from ..config.paths import path_to_canonical_local
from ..loudness.plex_db import has_analysis
from ..markers.publishers.base import PublishError
from ..markers.publishers.plex_db import LocalPlexDb, PlexMarkerPublisher
from ..markers.publishers.plex_remote import plex_database
from ..markers.settings import load_server
from ..output.plex_bundle import PlexBundleAdapter
from ..servers.base import MediaServer, ServerConfig
from .models import Cell, CellState, CheckCancelled, Feature, LibraryResult, ServerResult, TodoFile
from .scope import LibraryScope, excluded, library_scopes

ACCESS_DIRECT = "Reads Plex's database directly"
ACCESS_API_ONLY = "No access to Plex's database"
NEEDS_DATABASE = "Needs access to Plex's database"

_DB_WAIT_S = 30.0
_BIF_THREADS = 8
_BIF_CHUNK = 2000
_MOVIE = 1
_EPISODE = 4
_SD_FLAG = '"mi:indexes":"sd"'
_SD_FLAG_URL_FORM = "mi%3Aindexes=sd"

_PARTS_SQL = """
SELECT mi.library_section_id, mi.id, mi.metadata_type, mp.id, mp.file, mp.hash, mp.extra_data,
       mi.title, parent.title AS season, grand.title AS show, mi."index", parent."index"
FROM metadata_items mi
JOIN media_items mdi ON mdi.metadata_item_id = mi.id AND mdi.deleted_at IS NULL
JOIN media_parts mp ON mp.media_item_id = mdi.id AND mp.deleted_at IS NULL
LEFT JOIN metadata_items parent ON parent.id = mi.parent_id
LEFT JOIN metadata_items grand ON grand.id = parent.parent_id
WHERE mi.library_section_id IN ({ids}) AND mi.metadata_type IN (1, 4) AND mi.deleted_at IS NULL
  AND COALESCE(mdi.proxy_type, 0) = 0
"""
_AUDIO_COUNT_SQL = """
SELECT ms.media_part_id, COUNT(*) FROM media_streams ms JOIN media_parts mp ON mp.id = ms.media_part_id
JOIN media_items mdi ON mdi.id = mp.media_item_id JOIN metadata_items mi ON mi.id = mdi.metadata_item_id
WHERE ms.stream_type_id = 2 AND ms."index" IS NOT NULL AND mi.library_section_id IN ({ids}) GROUP BY ms.media_part_id
"""
# Same stream filter as the loudness job (indexed audio only), so the counts agree with what it will analyse.
# Only streams that may carry loudness (plain or URL-encoded 'ln:'): keeps memory small
# (thousands of rows, not hundreds of thousands).
_LOUDNESS_STREAMS_SQL = """
SELECT ms.media_part_id, ms.extra_data FROM media_streams ms JOIN media_parts mp ON mp.id = ms.media_part_id
JOIN media_items mdi ON mdi.id = mp.media_item_id JOIN metadata_items mi ON mi.id = mdi.metadata_item_id
WHERE ms.stream_type_id = 2 AND ms."index" IS NOT NULL
  AND (ms.extra_data LIKE '%ln:%' OR ms.extra_data LIKE '%ln\\%3A%' ESCAPE '\\')
  AND mi.library_section_id IN ({ids})
"""
_MARKERS_SQL = """
SELECT tg.metadata_item_id, tg.text FROM taggings tg JOIN tags t ON t.id = tg.tag_id
WHERE t.tag_type = 12 AND tg.text IN ('intro', 'credits')
"""


@dataclass
class _Part:
    """One media part as read from Plex's database."""

    library_id: str
    item_id: int
    part_id: int
    path: str
    title: str
    bundle_hash: str
    extra_data: str
    bif_ok: bool = False


@dataclass
class _PlexRows:
    """Everything the database queries returned, so the connection can close before any file is touched."""

    parts: list[tuple[Any, ...]]
    audio_counts: dict[int, int]
    loudness_streams: dict[int, list[str | None]]
    markers: dict[int, set[str]] = field(default_factory=dict)


def _open_db(cfg: ServerConfig) -> LocalPlexDb | None:
    """The local Plex database for this server, or None when this app can't open it directly."""
    db = plex_database(
        load_server(cfg.markers, "plex"),
        path_provider=lambda: PlexMarkerPublisher.db_path_for(cfg),
        label=cfg.name or "",
    )
    if isinstance(db, LocalPlexDb) and (cfg.output or {}).get("plex_config_folder"):
        return db
    return None


def count_plex(
    cfg: ServerConfig,
    server: MediaServer,
    *,
    nothing_found: dict[str, set[Feature]],
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> ServerResult:
    """Count previews, loudness and intro/credits for every library of a Plex server.

    Args:
        cfg: The server's saved configuration.
        server: The live server client (used only when Plex's database can't be read).
        nothing_found: Canonical path -> features whose marker search ended with nothing (from markers.db).
        cancel_check: Returns True when the user cancelled the check.
        progress: Called as ``progress(step, done, total)``.

    Returns:
        The server's per-library cells and the files still to do.

    Raises:
        CheckCancelled: ``cancel_check`` returned True.
    """
    scopes = library_scopes(cfg)
    db = _open_db(cfg)
    if db is not None:
        libraries, todo = _count_from_db(
            cfg, db, scopes, nothing_found=nothing_found, cancel_check=cancel_check, progress=progress
        )
        access = ACCESS_DIRECT
    else:
        libraries, todo = _count_from_api(cfg, server, scopes, cancel_check=cancel_check, progress=progress)
        access = ACCESS_API_ONLY
    return ServerResult(
        server_id=cfg.id, name=cfg.name, type=cfg.type.value, libraries=libraries, todo=todo, access=access
    )


def _fixed_cells(scope: LibraryScope, counted: Callable[[], Cell]) -> dict[Feature, Cell]:
    """Copies of the scope's fixed cells, with ``counted()`` for every feature to be counted."""
    return {feature: replace(fixed) if fixed is not None else counted() for feature, fixed in scope.features.items()}


def _error_library(scope: LibraryScope, message: str) -> LibraryResult:
    cells = _fixed_cells(scope, lambda: Cell(CellState.ERROR, reason=message[:200]))
    return LibraryResult(scope.library_id, scope.name, scope.kind, 0, cells)


def _episode_title(show: str | None, season_index: int | None, index: int | None, title: str | None) -> str:
    return f"{show} – S{season_index or 0:02d}E{index or 0:02d} – {title}"


def _preview_flag_shown(extra_data: str) -> bool:
    """Whether Plex's own flag says the item has previews (``mi:indexes`` is ``sd``)."""
    if _SD_FLAG in extra_data:
        return True
    try:
        data = json.loads(extra_data)
    except ValueError:
        return _SD_FLAG_URL_FORM in extra_data
    return isinstance(data, dict) and data.get("mi:indexes") == "sd"


def _bif_exists(plex_config_folder: str, bundle_hash: str) -> bool:
    try:
        return os.stat(PlexBundleAdapter.bundle_bif_path(plex_config_folder, bundle_hash)).st_size > 0
    except OSError:
        return False


def _section_ids(scopes: list[LibraryScope]) -> list[int]:
    """Plex section ids of the libraries with anything to count."""
    ids = []
    for scope in scopes:
        if any(cell is None for cell in scope.features.values()) and scope.library_id.isdigit():
            ids.append(int(scope.library_id))
    return ids


def _read_rows(db: LocalPlexDb, section_ids: list[int], *, want_markers: bool) -> _PlexRows:
    marks = ",".join("?" * len(section_ids))
    with db._database(read_only=True, deadline=time.monotonic() + _DB_WAIT_S) as conn:
        parts = conn.execute(_PARTS_SQL.format(ids=marks), section_ids).fetchall()
        audio_counts = dict(conn.execute(_AUDIO_COUNT_SQL.format(ids=marks), section_ids).fetchall())
        loudness_streams: dict[int, list[str | None]] = defaultdict(list)
        for part_id, extra in conn.execute(_LOUDNESS_STREAMS_SQL.format(ids=marks), section_ids):
            loudness_streams[part_id].append(extra)
        rows = _PlexRows(parts, audio_counts, loudness_streams)
        if want_markers:
            for item_id, text in conn.execute(_MARKERS_SQL):
                rows.markers.setdefault(item_id, set()).add(text)
    return rows


def _loudness_done(part_id: int, rows: _PlexRows) -> bool:
    """Every audio stream of the part carries a complete result of this app's analysis."""
    passing = 0
    for extra in rows.loudness_streams.get(part_id, ()):
        try:
            if has_analysis(extra):
                passing += 1
        except PublishError:
            continue
    return passing == rows.audio_counts[part_id]


def _build_parts(cfg: ServerConfig, rows: _PlexRows, scopes: list[LibraryScope]) -> dict[str, list[_Part]]:
    is_excluded = excluded(cfg)
    known = {scope.library_id for scope in scopes}
    by_library: dict[str, list[_Part]] = defaultdict(list)
    for (
        section,
        item_id,
        kind,
        part_id,
        file,
        bundle_hash,
        extra,
        title,
        _season,
        show,
        index,
        season_index,
    ) in rows.parts:
        library_id = str(section)
        if library_id not in known:
            continue
        path = path_to_canonical_local(file or "", cfg.path_mappings)
        if is_excluded(path):
            continue
        full_title = _episode_title(show, season_index, index, title) if kind == _EPISODE else title or ""
        by_library[library_id].append(
            _Part(library_id, item_id, part_id, path, full_title, bundle_hash or "", extra or "")
        )
    return by_library


def _stat_bifs(
    folder: str,
    wanted: list[_Part],
    *,
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> None:
    """Set ``bif_ok`` on each part; the BIFs are on local disk so a small thread pool is enough."""
    total = len(wanted)
    with ThreadPoolExecutor(_BIF_THREADS) as pool:
        for start in range(0, total, _BIF_CHUNK):
            if cancel_check():
                raise CheckCancelled()
            progress("Checking previews", start, total)
            chunk = wanted[start : start + _BIF_CHUNK]
            for part, ok in zip(chunk, pool.map(lambda p: _bif_exists(folder, p.bundle_hash), chunk), strict=True):
                part.bif_ok = ok
    progress("Checking previews", total, total)


def _tally_library(
    scope: LibraryScope,
    parts: list[_Part],
    rows: _PlexRows,
    nothing_found: dict[str, set[Feature]],
) -> tuple[LibraryResult, list[TodoFile]]:
    cells = _fixed_cells(scope, lambda: Cell(CellState.COUNTED))
    counted = {feature for feature, fixed in scope.features.items() if fixed is None}
    todo: list[TodoFile] = []

    def add_todo(feature: Feature, part: _Part, *, not_showing: bool = False) -> None:
        todo.append(TodoFile(feature, part.path, part.title, str(part.item_id), not_showing, scope.library_id))

    for part in parts:
        if Feature.PREVIEWS in counted:
            cell = cells[Feature.PREVIEWS]
            cell.total += 1
            if part.bif_ok:
                cell.done += 1
                if not _preview_flag_shown(part.extra_data):
                    cell.not_showing += 1
                    add_todo(Feature.PREVIEWS, part, not_showing=True)
            else:
                add_todo(Feature.PREVIEWS, part)
        if Feature.LOUDNESS in counted and rows.audio_counts.get(part.part_id):
            cell = cells[Feature.LOUDNESS]
            cell.total += 1
            if _loudness_done(part.part_id, rows):
                cell.done += 1
            else:
                add_todo(Feature.LOUDNESS, part)
        for feature in (Feature.INTRO, Feature.CREDITS):
            if feature not in counted:
                continue
            cell = cells[feature]
            cell.total += 1
            if feature.value in rows.markers.get(part.item_id, ()):
                cell.done += 1
            elif feature in nothing_found.get(part.path, ()):
                cell.nothing_found += 1
            else:
                add_todo(feature, part)
    return LibraryResult(scope.library_id, scope.name, scope.kind, len(parts), cells), todo


def _count_from_db(
    cfg: ServerConfig,
    db: LocalPlexDb,
    scopes: list[LibraryScope],
    *,
    nothing_found: dict[str, set[Feature]],
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> tuple[list[LibraryResult], list[TodoFile]]:
    if cancel_check():
        raise CheckCancelled()
    section_ids = _section_ids(scopes)
    if not section_ids:
        return [
            LibraryResult(s.library_id, s.name, s.kind, 0, _fixed_cells(s, lambda: Cell(CellState.COUNTED)))
            for s in scopes
        ], []
    want_markers = any(
        scope.features.get(feature) is None for scope in scopes for feature in (Feature.INTRO, Feature.CREDITS)
    )
    progress("Reading Plex's database", 0, 0)
    try:
        rows = _read_rows(db, section_ids, want_markers=want_markers)
    except (PublishError, sqlite3.Error) as exc:
        logger.warning("Library health: could not read Plex's database on {}: {}", cfg.name, exc)
        return [_error_library(scope, str(exc)) for scope in scopes], []

    by_library = _build_parts(cfg, rows, scopes)
    folder = str(cfg.output["plex_config_folder"])
    wanted = [
        part
        for scope in scopes
        if scope.features.get(Feature.PREVIEWS) is None
        for part in by_library.get(scope.library_id, ())
        if part.bundle_hash
    ]
    _stat_bifs(folder, wanted, cancel_check=cancel_check, progress=progress)

    libraries: list[LibraryResult] = []
    todo: list[TodoFile] = []
    for scope in scopes:
        try:
            result, library_todo = _tally_library(scope, by_library.get(scope.library_id, []), rows, nothing_found)
        except Exception as exc:  # noqa: BLE001 - one library failing must not hide the others
            logger.warning("Library health: could not count {} on {}: {}", scope.name, cfg.name, exc)
            libraries.append(_error_library(scope, str(exc)))
            continue
        libraries.append(result)
        todo.extend(library_todo)
    return libraries, todo


def _count_from_api(
    cfg: ServerConfig,
    server: MediaServer,
    scopes: list[LibraryScope],
    *,
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> tuple[list[LibraryResult], list[TodoFile]]:
    is_excluded = excluded(cfg)
    libraries: list[LibraryResult] = []
    todo: list[TodoFile] = []
    for number, scope in enumerate(scopes):
        if cancel_check():
            raise CheckCancelled()
        progress("Checking previews", number, len(scopes))
        cells = _fixed_cells(scope, lambda: Cell(CellState.UNAVAILABLE, reason=NEEDS_DATABASE))
        total = 0
        if scope.features.get(Feature.PREVIEWS) is None:
            cell = Cell(CellState.COUNTED)
            library_todo: list[TodoFile] = []
            try:
                root = (
                    cast(Any, server)
                    ._connect()
                    .query(
                        f"/library/sections/{scope.library_id}/all?type={_MOVIE if scope.is_movie_library else _EPISODE}"
                    )
                )
                for video in root.findall("Video"):
                    title = (
                        _episode_title(
                            video.get("grandparentTitle"),
                            _int_or_none(video.get("parentIndex")),
                            _int_or_none(video.get("index")),
                            video.get("title"),
                        )
                        if video.get("type") == "episode"
                        else video.get("title") or ""
                    )
                    for part in video.findall("Media/Part"):
                        path = path_to_canonical_local(part.get("file") or "", cfg.path_mappings)
                        if is_excluded(path):
                            continue
                        cell.total += 1
                        if part.get("indexes") == "sd":
                            cell.done += 1
                        else:
                            library_todo.append(
                                TodoFile(
                                    Feature.PREVIEWS, path, title, video.get("ratingKey") or "", False, scope.library_id
                                )
                            )
                total = cell.total
                cells[Feature.PREVIEWS] = cell
                todo.extend(library_todo)
            except Exception as exc:  # noqa: BLE001 - one library failing must not hide the others
                logger.warning("Library health: could not count {} on {}: {}", scope.name, cfg.name, exc)
                cells[Feature.PREVIEWS] = Cell(CellState.ERROR, reason=str(exc)[:200])
        libraries.append(LibraryResult(scope.library_id, scope.name, scope.kind, total, cells))
    return libraries, todo


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
