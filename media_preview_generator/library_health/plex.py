"""Plex counters: previews, chapter thumbnails, loudness and intro/credits per library.

Reads Plex's own database read-only (one connection, a handful of queries) so a 100k-file library takes seconds. Without
database access (Plex marker agent, or no config folder) it falls back to Plex's API, which only knows previews.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, cast

from loguru import logger

from ..config.paths import path_to_canonical_local
from ..loudness import analyze
from ..loudness.plex_db import has_analysis
from ..loudness.undecodable import ffmpeg_identity, skipped_count_by_part_hash
from ..markers.fs import filesystem_type, is_network_filesystem
from ..markers.publishers.base import PublishError
from ..markers.publishers.plex_db import LocalPlexDb, PlexMarkerPublisher
from ..markers.publishers.plex_remote import RemotePlexDb, plex_database
from ..markers.settings import load_server
from ..output.plex_bundle import PlexBundleAdapter
from ..output.plex_hash import get_source_fingerprint
from ..servers.base import MediaServer, ServerConfig
from .markers_db import path_key
from .models import Cell, CellState, CheckCancelled, Feature, LibraryResult, ServerResult, TodoFile
from .plex_rows import query_rows
from .scope import LibraryScope, excluded, library_scopes

ACCESS_DIRECT = "Reads Plex's database directly"
ACCESS_NODE = "Reads Plex's database through the Plex database node"
ACCESS_API_ONLY = "No access to Plex's database"
NEEDS_DATABASE = "Needs access to Plex's database"

_DB_WAIT_S = 30.0
# The Plex helper build that first runs these counts on Plex's machine; named in the "update the helper" message.
LIBRARY_HEALTH_AGENT_VERSION = "1.2.0"
_BIF_THREADS = 8
_BIF_CHUNK = 2000
_MOVIE = 1
_EPISODE = 4
_SD_FLAG = '"mi:indexes":"sd"'
_SD_FLAG_URL_FORM = "mi%3Aindexes=sd"


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
    media_id: int = 0
    bif_ok: bool = False


@dataclass
class _Chapters:
    """One item's chapters: how many, and (index, start ms) of each one Plex has no thumbnail for."""

    count: int
    missing: list[tuple[int, int]]


@dataclass
class _PlexRows:
    """Everything the database queries returned, so the connection can close before any file is touched."""

    parts: list[tuple[Any, ...]]
    audio_counts: dict[int, int]
    loudness_streams: dict[int, list[str | None]]
    undecodable: dict[str, int] = field(default_factory=dict)
    markers: dict[int, set[str]] = field(default_factory=dict)
    chapters: dict[int, _Chapters] = field(default_factory=dict)
    durations: dict[int, int] = field(default_factory=dict)


def _open_db(cfg: ServerConfig) -> LocalPlexDb | RemotePlexDb | None:
    """This server's Plex database: the local file, or its Plex database node (the Plex helper) when one is set up.

    None without a Plex config folder: the previews and chapter counts read Plex's bundles there, whichever side runs
    the database queries.
    """
    db = plex_database(
        load_server(cfg.markers, "plex"),
        path_provider=lambda: PlexMarkerPublisher.db_path_for(cfg),
        label=cfg.name or "",
    )
    if (cfg.output or {}).get("plex_config_folder"):
        return db
    return None


def _node_refusal(db: RemotePlexDb) -> str:
    """Why the node can't run the counts ("update the helper", "can't reach it"), or "" when it can."""
    try:
        db.require_library_health(needs=LIBRARY_HEALTH_AGENT_VERSION)
    except PublishError as exc:
        return str(exc)
    return ""


def _network_share(cfg: ServerConfig) -> str | None:
    """The filesystem type when Plex's database is on a network share, else None.

    SQLite can't read a database in WAL mode over a network share: on Docker Desktop's 9p mount of a Windows folder it
    can't map the ``-shm`` file and fails with "disk I/O error", and over NFS or SMB it could read an inconsistent view.
    """
    path = PlexMarkerPublisher.db_path_for(cfg)
    if not path:
        return None
    fs_type = filesystem_type(os.path.realpath(path))
    return fs_type if is_network_filesystem(fs_type) else None


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
    # A node checks its own disk; this app's view of the database path only matters when it opens the file itself.
    share = _network_share(cfg) if isinstance(db, LocalPlexDb) else None
    refused = _node_refusal(db) if isinstance(db, RemotePlexDb) else ""
    if db is not None and share is None and not refused:
        libraries, todo = _count_from_db(
            cfg, db, scopes, nothing_found=nothing_found, cancel_check=cancel_check, progress=progress
        )
        access = ACCESS_NODE if isinstance(db, RemotePlexDb) else ACCESS_DIRECT
    else:
        unavailable, access = NEEDS_DATABASE, ACCESS_API_ONLY
        if refused:
            logger.warning("Library health: the Plex database node for {} can't count: {}", cfg.name, refused)
            unavailable = refused[:200]
        elif share:
            logger.warning(
                "Library health: Plex's database on {} is on a network share ({}); counting previews through Plex's API",
                cfg.name,
                share,
            )
            unavailable = f"Can't read Plex's database over a network share ({share})"
            access = f"Plex's database is on a network share ({share}), so only previews are counted"
        libraries, todo = _count_from_api(
            cfg, server, scopes, unavailable=unavailable, cancel_check=cancel_check, progress=progress
        )
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


def _parse_missing(text: str | None) -> list[tuple[int, int]]:
    missing = []
    for entry in (text or "").split(","):
        index, _, start = entry.partition(":")
        try:
            missing.append((int(index), int(start)))
        except ValueError:
            continue
    return missing


def _rows_from(raw: Mapping[str, Iterable[Sequence[Any]]], undecodable: dict[str, int]) -> _PlexRows:
    """The queries' rows (``plex_rows.query_rows``' cursors here, or a Plex database node's lists) as lookups.

    Raises:
        ValueError: A node sent rows in another shape.
    """
    try:
        loudness_streams: dict[int, list[str | None]] = defaultdict(list)
        for part_id, extra in raw["loudness_streams"]:
            loudness_streams[part_id].append(extra)
        rows = _PlexRows(
            [tuple(row) for row in raw["parts"]],
            {part_id: count for part_id, count in raw["audio_counts"]},
            loudness_streams,
            undecodable,
        )
        for item_id, text in raw["markers"]:
            rows.markers.setdefault(item_id, set()).add(text)
        for item_id, count, missing in raw["chapters"]:
            rows.chapters[item_id] = _Chapters(count, _parse_missing(missing))
        rows.durations = {item_id: duration for item_id, duration in raw["durations"] if duration}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Library Health rows in an unexpected shape: {exc}") from exc
    return rows


def _read_rows(
    db: LocalPlexDb | RemotePlexDb,
    section_ids: list[int],
    *,
    want_markers: bool,
    want_chapters: bool,
    machine_identifier: str = "",
) -> _PlexRows:
    # In the image this is always the bundled analyser the loudness job uses; outside it a custom ffmpeg_path makes
    # Health conservative (skips recorded by another ffmpeg don't count), never optimistic.
    ffmpeg = ffmpeg_identity(analyze.resolve_ffmpeg("ffmpeg"))
    deadline = time.monotonic() + _DB_WAIT_S
    if isinstance(db, RemotePlexDb):
        raw = db.library_health_rows(
            section_ids,
            want_markers=want_markers,
            want_chapters=want_chapters,
            deadline=deadline,
            needs=LIBRARY_HEALTH_AGENT_VERSION,
            machine_identifier=machine_identifier,
        )
        return _rows_from(raw, skipped_count_by_part_hash(ffmpeg))
    with db._database(read_only=True, deadline=deadline) as conn:
        queries = query_rows(conn, section_ids, want_markers=want_markers, want_chapters=want_chapters)
        return _rows_from(queries, skipped_count_by_part_hash(ffmpeg))


def _loudness_done(part_id: int, part_hash: str, rows: _PlexRows) -> bool:
    """Every audio stream of the part carries a complete result of this app's analysis or is known undecodable."""
    passing = 0
    for extra in rows.loudness_streams.get(part_id, ()):
        try:
            if has_analysis(extra):
                passing += 1
        except PublishError:
            continue
    # >=: a stream recorded undecodable could later gain loudness data.
    return passing + rows.undecodable.get(part_hash, 0) >= rows.audio_counts[part_id]


def _chapter_manifest_state(folder: str, part: _Part, missing: set[int]) -> str:
    """Refine a to-do from this app's chapter manifest (``todo``, ``done`` or ``cant_do``).

    A manifest only counts while it still describes the file on disk. It records files the chapter step gave up on
    (unsupported, or a failure it won't retry until the file changes) and chapters that start after the video really
    ends, which Plex's duration can miss.
    """
    if not part.bundle_hash:
        return "todo"
    manifest_path = (
        PlexBundleAdapter.bundle_bif_path(folder, part.bundle_hash).parent.parent
        / "Chapters"
        / "chapter-thumbnails.mpg.json"
    )
    try:
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict) or manifest.get("source") != list(get_source_fingerprint(part.path)):
            return "todo"
    except (OSError, ValueError):
        return "todo"
    if any(isinstance(manifest.get(key), str) and manifest[key] for key in ("unsupported", "failed")):
        return "cant_do"
    images = manifest.get("images")
    if isinstance(images, dict) and all(
        isinstance(images.get(str(index)), dict) and images[str(index)].get("past_end") is True for index in missing
    ):
        return "done"
    return "todo"


def _chapter_state(folder: str, part: _Part, rows: _PlexRows, parts_per_media: Counter[int]) -> str:
    """Whether a part's chapter thumbnails are ``none`` (no chapters), ``done``, ``cant_do`` or ``todo``."""
    chapters = rows.chapters.get(part.item_id)
    if chapters is None:
        return "none"
    if parts_per_media[part.media_id] > 1:
        return "cant_do"  # the chapter step skips a version split into several files
    duration = rows.durations.get(part.item_id)
    needed = {index for index, start in chapters.missing if duration is None or start < duration}
    if not needed:
        # Every chapter starting after the video ends leaves no frame to show: the chapter step fails those.
        return "cant_do" if len(chapters.missing) >= chapters.count else "done"
    return _chapter_manifest_state(folder, part, needed)


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
        media_id,
    ) in rows.parts:
        library_id = str(section)
        if library_id not in known:
            continue
        path = path_to_canonical_local(file or "", cfg.path_mappings)
        if is_excluded(path):
            continue
        full_title = _episode_title(show, season_index, index, title) if kind == _EPISODE else title or ""
        by_library[library_id].append(
            _Part(library_id, item_id, part_id, path, full_title, bundle_hash or "", extra or "", media_id)
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
    *,
    folder: str = "",
    parts_per_media: Counter[int] | None = None,
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
        if Feature.CHAPTERS in counted:
            cell = cells[Feature.CHAPTERS]
            cell.total += 1
            chapter_state = _chapter_state(folder, part, rows, parts_per_media or Counter())
            if chapter_state == "done":
                cell.done += 1
            elif chapter_state == "none":
                cell.nothing_found += 1
            elif chapter_state == "cant_do":
                cell.cant_do += 1
            else:
                add_todo(Feature.CHAPTERS, part)
        if Feature.LOUDNESS in counted and rows.audio_counts.get(part.part_id):
            cell = cells[Feature.LOUDNESS]
            cell.total += 1
            if _loudness_done(part.part_id, part.bundle_hash, rows):
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
            elif feature in nothing_found.get(path_key(part.path), ()):
                cell.nothing_found += 1
            else:
                add_todo(feature, part)
    return LibraryResult(scope.library_id, scope.name, scope.kind, len(parts), cells), todo


def _count_from_db(
    cfg: ServerConfig,
    db: LocalPlexDb | RemotePlexDb,
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
    want_chapters = any(scope.features.get(Feature.CHAPTERS) is None for scope in scopes)
    progress("Reading Plex's database", 0, 0)
    try:
        rows = _read_rows(
            db,
            section_ids,
            want_markers=want_markers,
            want_chapters=want_chapters,
            machine_identifier=str(getattr(cfg, "server_identity", None) or ""),
        )
    except (PublishError, sqlite3.Error, ValueError) as exc:
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
    parts_per_media = Counter(row[-1] for row in rows.parts)

    libraries: list[LibraryResult] = []
    todo: list[TodoFile] = []
    for scope in scopes:
        try:
            result, library_todo = _tally_library(
                scope,
                by_library.get(scope.library_id, []),
                rows,
                nothing_found,
                folder=folder,
                parts_per_media=parts_per_media,
            )
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
    unavailable: str,
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
        cells = _fixed_cells(scope, lambda: Cell(CellState.UNAVAILABLE, reason=unavailable))
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
