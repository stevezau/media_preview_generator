"""Emby and Jellyfin counters: previews and intro/credits per library.

Both servers list items over HTTP. Jellyfin says in the item itself whether it has previews (``Trickplay``) and
answers a separate segment request per item for markers. Emby carries markers in the item's chapters and keeps its
previews as ``.bif`` files next to the video, so those are looked for on disk, one folder listing at a time.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, cast

from loguru import logger

from ..config import resolve_frame_interval
from ..config.paths import path_to_canonical_local
from ..output.emby_sidecar import EmbyBifAdapter
from ..servers._embyish import (
    _LIST_ITEMS_MAX_ATTEMPTS,
    _LIST_ITEMS_PAGE_SIZE,
    _LIST_ITEMS_RETRY_BASE_WAIT_S,
    _LIST_ITEMS_TIMEOUT_S,
)
from ..servers.base import MediaServer, ServerConfig, ServerType
from .models import Cell, CellState, CheckCancelled, Feature, LibraryResult, ServerResult, TodoFile
from .scope import LibraryScope, excluded, library_scopes

ACCESS_API = "Reads the server's library over its web API"

_SEGMENT_THREADS = 4
_CANCEL_EVERY = 50
_SEGMENT_FAILURE_LIMIT = 0.10
_DEFAULT_WIDTH = 320
_EMBY_FIELDS = "Path,Chapters"
_JELLYFIN_FIELDS = "Path,Trickplay,MediaSources"
_BACKOFF_SLICE_S = 0.5
_SEGMENT_FEATURES = {"Intro": Feature.INTRO, "Outro": Feature.CREDITS}
_CHAPTER_FEATURES = {"IntroStart": Feature.INTRO, "CreditsStart": Feature.CREDITS}


@dataclass
class _Item:
    """One video as listed by the server."""

    item_id: str
    path: str
    title: str
    raw: dict[str, Any]
    markers: set[Feature] | None = None  # None: the server did not answer
    previews_ok: bool = False


def count_embyish(
    cfg: ServerConfig,
    server: MediaServer,
    *,
    nothing_found: dict[str, set[Feature]],
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> ServerResult:
    """Count previews and intro/credits for every library of an Emby or Jellyfin server.

    Args:
        cfg: The server's saved configuration.
        server: The live server client.
        nothing_found: Canonical path -> features whose marker search ended with nothing (from markers.db).
        cancel_check: Returns True when the user cancelled the check.
        progress: Called as ``progress(step, done, total)``.

    Returns:
        The server's per-library cells and the files still to do.

    Raises:
        CheckCancelled: ``cancel_check`` returned True.
    """
    libraries: list[LibraryResult] = []
    todo: list[TodoFile] = []
    for scope in library_scopes(cfg):
        if cancel_check():
            raise CheckCancelled()
        if all(cell is not None for cell in scope.features.values()):
            libraries.append(LibraryResult(scope.library_id, scope.name, scope.kind, 0, _fixed_cells(scope, None)))
            continue
        try:
            result, library_todo = _count_library(
                cfg, server, scope, nothing_found=nothing_found, cancel_check=cancel_check, progress=progress
            )
        except CheckCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one library failing must not hide the others
            logger.warning("Library health: could not count {} on {}: {}", scope.name, cfg.name, exc)
            libraries.append(LibraryResult(scope.library_id, scope.name, scope.kind, 0, _error_cells(scope, str(exc))))
            continue
        libraries.append(result)
        todo.extend(library_todo)
    return ServerResult(
        server_id=cfg.id, name=cfg.name, type=cfg.type.value, libraries=libraries, todo=todo, access=ACCESS_API
    )


def _fixed_cells(scope: LibraryScope, counted: Cell | None) -> dict[Feature, Cell]:
    """Copies of the scope's fixed cells; features to be counted get a copy of ``counted`` (or a plain COUNTED)."""
    return {
        feature: replace(fixed) if fixed is not None else replace(counted or Cell(CellState.COUNTED))
        for feature, fixed in scope.features.items()
    }


def _error_cells(scope: LibraryScope, message: str) -> dict[Feature, Cell]:
    return _fixed_cells(scope, Cell(CellState.ERROR, reason=message[:200]))


def _title(raw: dict[str, Any]) -> str:
    name = raw.get("Name") or ""
    series = raw.get("SeriesName")
    season = raw.get("ParentIndexNumber")
    number = raw.get("IndexNumber")
    if series and season is not None and number is not None:
        return f"{series} – S{season:02d}E{number:02d} – {name}"
    return name


def _wait(seconds: float, cancel_check: Callable[[], bool]) -> None:
    """Sleep ``seconds`` in short slices so a cancel is noticed within about half a second.

    Raises:
        CheckCancelled: ``cancel_check`` returned True.
    """
    remaining = seconds
    while remaining > 0:
        if cancel_check():
            raise CheckCancelled()
        step = min(_BACKOFF_SLICE_S, remaining)
        time.sleep(step)
        remaining -= step


def _fetch_page(
    request: Callable[..., Any], params: dict[str, Any], cancel_check: Callable[[], bool]
) -> dict[str, Any]:
    """One ``/Items`` page, with the app's usual long timeout and retries with backoff.

    Raises:
        CheckCancelled: ``cancel_check`` returned True between attempts or during the backoff.
        Exception: The last error once every attempt has failed.
    """
    for attempt in range(1, _LIST_ITEMS_MAX_ATTEMPTS + 1):
        if cancel_check():
            raise CheckCancelled()
        try:
            response = request("GET", "/Items", params=params, timeout=_LIST_ITEMS_TIMEOUT_S)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
        except Exception as exc:  # noqa: BLE001 - any failure is worth another try; the last one is re-raised
            if attempt >= _LIST_ITEMS_MAX_ATTEMPTS:
                raise
            wait = _LIST_ITEMS_RETRY_BASE_WAIT_S * (2 ** (attempt - 1))
            logger.info("Library health: /Items page failed ({}), retrying in {:.1f}s", type(exc).__name__, wait)
            _wait(wait, cancel_check)
    raise AssertionError("unreachable")


def _version_paths(raw: dict[str, Any], is_jellyfin: bool) -> list[tuple[str, str]]:
    """``(item id, path)`` per file of a listed row.

    Jellyfin merges versions into one row, and the scan fans out per ``MediaSources`` entry (#271), so the
    health check does too. Emby lists each version as its own row.
    """
    top = (str(raw.get("Id") or ""), str(raw.get("Path") or ""))
    if not is_jellyfin:
        return [top] if top[1] else []
    versions: dict[str, str] = {}
    for source in raw.get("MediaSources") or []:
        path = source.get("Path")
        if path and path not in versions:
            versions[path] = str(source.get("Id") or top[0])
    if not versions:
        return [top] if top[1] else []
    return [(item_id, path) for path, item_id in versions.items()]


def _has_trickplay(item: _Item) -> bool:
    """Whether Jellyfin has previews for this version: ``Trickplay`` is keyed by media source id."""
    keys = {str(key).replace("-", "").lower() for key in (item.raw.get("Trickplay") or {})}
    return item.item_id.replace("-", "").lower() in keys


def _list_items(
    cfg: ServerConfig, server: MediaServer, library_id: str, cancel_check: Callable[[], bool]
) -> list[_Item]:
    """Every movie/episode of a library, paged the same way as the app's own listing."""
    is_excluded = excluded(cfg)
    fields = _EMBY_FIELDS if cfg.type == ServerType.EMBY else _JELLYFIN_FIELDS
    request = cast(Any, server)._request
    items: list[_Item] = []
    start_index = 0
    while True:
        params = {
            "ParentId": library_id,
            "IncludeItemTypes": "Movie,Episode",
            "Recursive": "true",
            "Fields": fields,
            "Limit": _LIST_ITEMS_PAGE_SIZE,
            "StartIndex": start_index,
        }
        payload = _fetch_page(request, params, cancel_check)
        raw_items = payload.get("Items") or []
        for raw in raw_items:
            for item_id, source_path in _version_paths(raw, cfg.type == ServerType.JELLYFIN):
                path = path_to_canonical_local(source_path, cfg.path_mappings)
                if not is_excluded(path):
                    items.append(_Item(item_id, path, _title(raw), raw))
        start_index += len(raw_items)
        total = payload.get("TotalRecordCount")
        if len(raw_items) < _LIST_ITEMS_PAGE_SIZE or (total is not None and start_index >= total):
            return items


def _read_segments(
    server: MediaServer,
    items: list[_Item],
    *,
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> int:
    """Fill ``markers`` on each item from Jellyfin's segments; returns how many reads got no answer."""
    failures = 0
    total = len(items)
    reader = cast(Any, server).get_media_segments
    with ThreadPoolExecutor(_SEGMENT_THREADS) as pool:
        for start in range(0, total, _CANCEL_EVERY):
            if cancel_check():
                raise CheckCancelled()
            progress("Checking intro & credits", start, total)
            chunk = items[start : start + _CANCEL_EVERY]
            for item, segments in zip(chunk, pool.map(lambda i: reader(i.item_id), chunk), strict=True):
                if segments is None:
                    failures += 1
                    continue
                item.markers = {_SEGMENT_FEATURES[s["Type"]] for s in segments if s.get("Type") in _SEGMENT_FEATURES}
    progress("Checking intro & credits", total, total)
    return failures


def _read_chapters(items: list[_Item]) -> None:
    for item in items:
        chapters = item.raw.get("Chapters") or []
        item.markers = {
            _CHAPTER_FEATURES[c["MarkerType"]] for c in chapters if c.get("MarkerType") in _CHAPTER_FEATURES
        }


def _check_sidecars(
    cfg: ServerConfig,
    items: list[_Item],
    *,
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> None:
    """Set ``previews_ok`` by listing each media folder once; one thread, as network shares slow down with more."""
    width = int((cfg.output or {}).get("width") or _DEFAULT_WIDTH)
    interval = resolve_frame_interval(cfg.output)
    by_folder: dict[str, list[tuple[_Item, str]]] = defaultdict(list)
    for item in items:
        sidecar = EmbyBifAdapter.sidecar_path(item.path, width=width, frame_interval=interval)
        by_folder[str(sidecar.parent)].append((item, sidecar.name))
    total = len(by_folder)
    for number, (folder, entries) in enumerate(by_folder.items()):
        if cancel_check():
            raise CheckCancelled()
        progress("Checking previews", number, total)
        try:
            with os.scandir(folder) as listing:
                present = {entry.name: entry for entry in listing}
        except OSError as exc:
            logger.debug("Library health: could not list {}: {}", folder, exc)
            continue
        for item, name in entries:
            entry = present.get(name)
            if entry is None:
                continue
            try:
                item.previews_ok = entry.stat().st_size > 0
            except OSError:
                item.previews_ok = False
    progress("Checking previews", total, total)


def _count_library(
    cfg: ServerConfig,
    server: MediaServer,
    scope: LibraryScope,
    *,
    nothing_found: dict[str, set[Feature]],
    cancel_check: Callable[[], bool],
    progress: Callable[[str, int, int], None],
) -> tuple[LibraryResult, list[TodoFile]]:
    is_jellyfin = cfg.type == ServerType.JELLYFIN
    counted = {feature for feature, fixed in scope.features.items() if fixed is None}
    marker_features = [f for f in (Feature.INTRO, Feature.CREDITS) if f in counted]

    progress("Reading the library", 0, 0)
    items = _list_items(cfg, server, scope.library_id, cancel_check)
    cells = _fixed_cells(scope, None)
    todo: list[TodoFile] = []

    segment_failures = 0
    markers_unreadable = False
    if marker_features:
        if is_jellyfin:
            segment_failures = _read_segments(server, items, cancel_check=cancel_check, progress=progress)
            if items and segment_failures / len(items) > _SEGMENT_FAILURE_LIMIT:
                markers_unreadable = True
                reason = f"Jellyfin didn't answer for {segment_failures} files"
                for feature in marker_features:
                    cells[feature] = Cell(CellState.ERROR, reason=reason)
        else:
            _read_chapters(items)
    if Feature.PREVIEWS in counted and not is_jellyfin:
        _check_sidecars(cfg, items, cancel_check=cancel_check, progress=progress)

    def add_todo(feature: Feature, item: _Item) -> None:
        todo.append(TodoFile(feature, item.path, item.title, item.item_id, False, scope.library_id))

    for item in items:
        if Feature.PREVIEWS in counted:
            cell = cells[Feature.PREVIEWS]
            cell.total += 1
            ok = _has_trickplay(item) if is_jellyfin else item.previews_ok
            if ok:
                cell.done += 1
            else:
                add_todo(Feature.PREVIEWS, item)
        if markers_unreadable:
            continue
        for feature in marker_features:
            cell = cells[feature]
            cell.total += 1
            if item.markers is not None and feature in item.markers:
                cell.done += 1
            elif feature in nothing_found.get(item.path, ()):
                cell.nothing_found += 1
            else:
                add_todo(feature, item)
    return LibraryResult(scope.library_id, scope.name, scope.kind, len(items), cells), todo
