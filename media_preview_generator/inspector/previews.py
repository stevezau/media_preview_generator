"""Where each server keeps a file's preview, and what that preview holds.

Plex keeps a BIF in its config folder under the file's bundle hash (one lookup of the item and one of its parts), Emby
a BIF next to the video, Jellyfin tile sheets next to the video or in its own config folder. Every server is looked up
on a thread of its own and all of them together are waited for at most ``LOOKUP_TIMEOUT_S``, so a server that doesn't
answer only marks its own row, and isn't asked again for ``REST_S``.
"""

from __future__ import annotations

import os
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from ..bif_reader import BIF_MAGIC, read_bif_metadata
from ..config import resolve_frame_interval
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import OwnershipMatch, apply_path_mappings

# The longest a page waits for one server's preview lookup (Plex: the item, then its parts).
LOOKUP_TIMEOUT_S = 8.0
# Lookups in flight at once across every page. A server that doesn't answer holds a thread until its own request
# timeout; the page has stopped waiting long before.
_LOOKUPS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="inspector-preview")
# A server that didn't answer in time isn't asked again for this long (its rows say it couldn't be reached), so one
# hung server can't fill _LOOKUPS and make healthy servers' lookups queue past their own deadline.
REST_S = 30.0
_resting_since: dict[str, float] = {}
_rest_lock = threading.Lock()
# A header interval this far from the file's length divided by its frames is not the file's: the length wins.
_INTERVAL_TOLERANCE = 0.25
_SHEET_DIR_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)x(\d+)\s*$")

NOT_LISTED_NOTE = "{name} doesn't list this file yet"
NOT_ANALYSED_NOTE = "Plex hasn't analysed this file yet, so there is no place for its preview"
UNREACHABLE_NOTE = "Couldn't reach {name}"
NO_CONFIG_FOLDER_NOTE = "This server's Plex config folder isn't set"
NO_JELLYFIN_FOLDER_NOTE = "This server's Jellyfin config folder isn't set"


def bif_frame_count(path: str) -> int | None:
    """A BIF's frame count from its 16-byte header alone (the search rows' "Ready · N frames").

    Args:
        path: The BIF's path.

    Returns:
        The count, or None when the file can't be read or isn't a BIF.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return None
    if len(head) < 16 or head[:8] != BIF_MAGIC:
        return None
    return struct.unpack("<I", head[12:16])[0]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def bif_facts(path: str) -> dict:
    """What a BIF holds, for the Inspector's preview facts.

    Args:
        path: The BIF's path (already known to exist).

    Returns:
        ``frame_count``, ``interval_ms`` (0 when the file can't tell), ``file_size`` and ``created_at`` (ISO, the
        file's modification time); ``error`` instead when it can't be read.
    """
    try:
        meta = read_bif_metadata(path)
    except (OSError, ValueError, struct.error) as exc:
        logger.debug("Inspector: couldn't read the BIF at {}: {}", path, type(exc).__name__)
        return {"error": "This preview file couldn't be read"}
    return {
        "frame_count": meta.frame_count,
        "interval_ms": meta.frame_interval_ms,
        "file_size": meta.file_size,
        "created_at": _iso(meta.created_at),
    }


def trickplay_sheet_info(sheet_dir: str) -> dict:
    """What a Jellyfin trickplay sheet folder holds, measured from its files (Jellyfin writes no manifest).

    Tile geometry comes from the folder's name (Jellyfin's ``"<width> - <tileW>x<tileH>"``), the frame count from
    the sheets: every sheet but the last is full, and the last is scanned for its first empty (all-black) tile.

    Args:
        sheet_dir: The sheet folder (already validated by the caller).

    Returns:
        ``tile_width``, ``tile_height``, ``thumb_width``, ``thumb_height``, ``thumbnail_count``,
        ``frames_per_sheet``, ``sheet_count``, ``sheets_dir`` and ``sheets`` (``index``, ``path``, ``exists``,
        ``size_bytes``).

    Raises:
        ValueError: The folder's name, tiles or sheets can't be read; the message says which.
    """
    from PIL import Image

    dir_name = os.path.basename(sheet_dir.rstrip("/"))
    match = _SHEET_DIR_RE.match(dir_name)
    if not match:
        raise ValueError(f"Sheet directory name doesn't match '<width> - <tileW>x<tileH>': {dir_name!r}")
    tile_w = int(match.group(2))
    tile_h = int(match.group(3))
    if tile_w < 1 or tile_h < 1:
        raise ValueError(f"Invalid tile geometry {tile_w}x{tile_h}")
    frames_per_sheet = tile_w * tile_h

    sheet_files = sorted(
        (f for f in os.listdir(sheet_dir) if f.endswith(".jpg") and f.split(".")[0].isdigit()),
        key=lambda f: int(f.split(".")[0]),
    )
    sheet_count = len(sheet_files)
    if sheet_count == 0:
        raise ValueError("No tile sheets found in directory")

    last_sheet_path = os.path.join(sheet_dir, sheet_files[-1])
    try:
        with Image.open(last_sheet_path) as img:
            sheet_pixel_w, sheet_pixel_h = img.size
    except Exception as exc:
        raise ValueError(f"Could not measure last sheet: {exc}") from exc

    thumb_w = sheet_pixel_w // tile_w
    thumb_h = sheet_pixel_h // tile_h
    # The last sheet's filled tiles: the first all-black tile from the end is an empty slot of our packing.
    last_sheet_tiles = frames_per_sheet
    try:
        with Image.open(last_sheet_path) as img:
            for i in range(frames_per_sheet - 1, -1, -1):
                row, col = divmod(i, tile_w)
                tile = img.crop((col * thumb_w, row * thumb_h, (col + 1) * thumb_w, (row + 1) * thumb_h))
                if tile.getbbox() is not None:
                    last_sheet_tiles = i + 1
                    break
            else:
                last_sheet_tiles = 0
    except Exception:
        # A sheet that can't be scanned counts as full.
        pass

    sheets = []
    for n in range(sheet_count):
        sheet_path = os.path.join(sheet_dir, f"{n}.jpg")
        exists = os.path.isfile(sheet_path)
        sheets.append(
            {
                "index": n,
                "path": sheet_path,
                "exists": exists,
                "size_bytes": os.path.getsize(sheet_path) if exists else 0,
            }
        )
    return {
        "tile_width": tile_w,
        "tile_height": tile_h,
        "thumb_width": thumb_w,
        "thumb_height": thumb_h,
        "thumbnail_count": (sheet_count - 1) * frames_per_sheet + last_sheet_tiles,
        "frames_per_sheet": frames_per_sheet,
        "sheet_count": sheet_count,
        "sheets_dir": sheet_dir,
        "sheets": sheets,
    }


def _trickplay_facts(sheet_dir: str, interval_s: int) -> dict:
    try:
        info = trickplay_sheet_info(sheet_dir)
    except (OSError, ValueError) as exc:
        logger.debug("Inspector: couldn't read the trickplay sheets at {}: {}", sheet_dir, type(exc).__name__)
        return {"error": "This preview's tile sheets couldn't be read"}
    sheets = info["sheets"]
    mtimes = [os.path.getmtime(s["path"]) for s in sheets if s["exists"]]
    return {
        "frame_count": info["thumbnail_count"],
        # Jellyfin keeps the interval in its database, not on disk: the server's setting is what the sheets were
        # made at, and interval_for() corrects it from the file's length when the two disagree.
        "interval_ms": interval_s * 1000,
        "file_size": sum(s["size_bytes"] for s in sheets),
        "created_at": _iso(max(mtimes)) if mtimes else None,
        "tile_width": info["tile_width"],
        "tile_height": info["tile_height"],
        "sheets_dir": info["sheets_dir"],
    }


def interval_for(preview: dict, duration_ms: int | None) -> int:
    """The time between a preview's frames, checked against the file's length.

    A BIF's header can leave the interval out, and a trickplay folder never states it, so when the file's length is
    known and the stated interval is 0 or more than ``_INTERVAL_TOLERANCE`` away from length / frames, the length
    wins.

    Args:
        preview: A preview row with ``frame_count`` and ``interval_ms``.
        duration_ms: The file's length, when known.

    Returns:
        Milliseconds between frames; 0 when neither the preview nor the length can tell.
    """
    stated = int(preview.get("interval_ms") or 0)
    frames = int(preview.get("frame_count") or 0)
    if not duration_ms or frames <= 0:
        return stated
    derived = int(round(duration_ms / frames))
    if stated <= 0 or abs(stated - derived) > derived * _INTERVAL_TOLERANCE:
        return derived
    return stated


def _row(cfg: ServerConfig, kind: str) -> dict:
    return {
        "server_id": cfg.id,
        "server_name": cfg.name,
        "server_type": cfg.type.value,
        "kind": kind,
        "path": "",
        "exists": False,
        "note": "",
        "error": "",
        "versions": [],
    }


def _plex_preview(cfg: ServerConfig, server: Any, path: str, matches: list[OwnershipMatch], fallback: str) -> dict:
    from ..output.plex_bundle import PlexBundleAdapter

    row = _row(cfg, "bif")
    config_folder = str((cfg.output or {}).get("plex_config_folder") or fallback or "")
    if not config_folder:
        row["note"] = NO_CONFIG_FOLDER_NOTE
        return row
    item_id = server.resolve_remote_path_to_item_id(path, library_ids=[m.library_id for m in matches])
    if not item_id:
        row["note"] = NOT_LISTED_NOTE.format(name=cfg.name or "Plex")
        return row
    row["item_id"] = str(item_id)
    parts = [(h, remote) for h, remote in server.get_bundle_metadata(item_id) or [] if remote]
    mappings = list(cfg.path_mappings or [])
    # Every part of the item: the versions Plex keeps together under one item (one marker set for all of them).
    for _hash, remote in parts:
        for local in apply_path_mappings(remote, mappings) or [remote]:
            if local not in row["versions"] and os.path.isfile(local):
                row["versions"].append(local)
                break
    try:
        bundle_hash = PlexBundleAdapter._select_hash_for_path(parts, path, str(item_id))
    except Exception:
        row["note"] = NOT_ANALYSED_NOTE
        return row
    row["path"] = str(PlexBundleAdapter.bundle_bif_path(config_folder, bundle_hash))
    return row


def _emby_preview(cfg: ServerConfig, path: str) -> dict:
    from ..output.emby_sidecar import EmbyBifAdapter

    output = cfg.output or {}
    row = _row(cfg, "bif")
    width = int(output.get("width") or 320)
    row["path"] = str(EmbyBifAdapter.sidecar_path(path, width=width, frame_interval=resolve_frame_interval(output)))
    return row


def _jellyfin_preview(cfg: ServerConfig, server: Any, path: str, matches: list[OwnershipMatch]) -> dict:
    from ..output.jellyfin_trickplay import JellyfinTrickplayAdapter

    output = cfg.output or {}
    row = _row(cfg, "trickplay")
    width = int(output.get("width") or 320)
    if bool(output.get("save_with_media", True)):
        row["path"] = str(JellyfinTrickplayAdapter.sheet_dir(path, width=width))
        return row
    folder = str(output.get("jellyfin_config_folder") or "")
    if not folder:
        row["note"] = NO_JELLYFIN_FOLDER_NOTE
        return row
    item_id = server.resolve_remote_path_to_item_id(path, library_ids=[m.library_id for m in matches])
    if not item_id:
        row["note"] = NOT_LISTED_NOTE.format(name=cfg.name or "Jellyfin")
        return row
    adapter = JellyfinTrickplayAdapter(width=width, save_with_media=False, jellyfin_config_folder=folder)
    row["path"] = str(adapter.offmedia_sheet_dir(str(item_id), width=width))
    return row


def server_preview(
    cfg: ServerConfig,
    server: Any,
    canonical_path: str,
    matches: list[OwnershipMatch],
    *,
    plex_config_folder: str = "",
    with_facts: bool = True,
) -> dict:
    """Where one server keeps a file's preview, whether it is there, and (``with_facts``) what it holds.

    Args:
        cfg: The server's config.
        server: Its live client.
        canonical_path: The file's local path.
        matches: The server's libraries holding the file.
        plex_config_folder: The Plex config folder to use when the server's own isn't set.
        with_facts: Read the preview (frames, interval, size, date); off for the search rows, which only need
            whether it is there and a BIF's frame count.

    Returns:
        ``server_id``, ``server_name``, ``server_type``, ``kind`` (``bif`` or ``trickplay``), ``path``, ``exists``,
        ``note`` (why there's no path), ``error``, ``versions`` (Plex: every version's local file that is on disk),
        ``item_id`` (Plex, when found), plus the facts: ``frame_count``, ``interval_ms``, ``file_size``,
        ``created_at`` and, for trickplay, ``tile_width``, ``tile_height`` and ``sheets_dir``.
    """
    if cfg.type is ServerType.PLEX:
        row = _plex_preview(cfg, server, canonical_path, matches, plex_config_folder)
    elif cfg.type is ServerType.EMBY:
        row = _emby_preview(cfg, canonical_path)
    elif cfg.type is ServerType.JELLYFIN:
        row = _jellyfin_preview(cfg, server, canonical_path, matches)
    else:
        return _row(cfg, "unknown")
    if not row["path"]:
        return row
    if row["kind"] == "bif":
        row["exists"] = os.path.isfile(row["path"])
        if row["exists"]:
            row.update(bif_facts(row["path"]) if with_facts else {"frame_count": bif_frame_count(row["path"])})
    else:
        row["exists"] = os.path.isdir(row["path"]) and any(n.endswith(".jpg") for n in os.listdir(row["path"]))
        if row["exists"] and with_facts:
            row.update(_trickplay_facts(row["path"], resolve_frame_interval(cfg.output or {})))
    return row


def _unreachable(cfg: ServerConfig, kind: str) -> dict:
    row = _row(cfg, kind)
    row["error"] = UNREACHABLE_NOTE.format(name=cfg.name or cfg.type.value.capitalize())
    return row


def file_previews(
    canonical_path: str,
    owners: list[tuple[ServerConfig, Any, list[OwnershipMatch]]],
    *,
    plex_config_folder: str = "",
    with_facts: bool = True,
    timeout_s: float = LOOKUP_TIMEOUT_S,
) -> list[dict]:
    """:func:`server_preview` for every owning server at once, in the owners' order.

    Args:
        canonical_path: The file's local path.
        owners: ``owning_servers(canonical_path, registry)``.
        plex_config_folder: As for :func:`server_preview`.
        with_facts: As for :func:`server_preview`.
        timeout_s: The longest to wait for all of them together.

    Returns:
        One row per owner; a server that raised or didn't answer in time gets a row with ``error`` set.
    """
    kinds = {ServerType.PLEX: "bif", ServerType.EMBY: "bif", ServerType.JELLYFIN: "trickplay"}
    futures: list[tuple[ServerConfig, Any]] = []
    for cfg, server, matches in owners:
        if _resting(cfg.id):
            # It just didn't answer: asking again would only hold another lookup thread for the same wait.
            futures.append((cfg, None))
            continue
        futures.append(
            (
                cfg,
                _LOOKUPS.submit(
                    server_preview,
                    cfg,
                    server,
                    canonical_path,
                    matches,
                    plex_config_folder=plex_config_folder,
                    with_facts=with_facts,
                ),
            )
        )
    rows = []
    # One deadline for all of them: each server runs on its own thread already, so N slow servers cost timeout_s, not
    # N times it.
    deadline = time.monotonic() + timeout_s
    for cfg, future in futures:
        if future is None:
            rows.append(_unreachable(cfg, kinds.get(cfg.type, "unknown")))
            continue
        try:
            rows.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
            _answered(cfg.id)
        except FutureTimeoutError:
            future.cancel()
            _rest(cfg.id)
            logger.debug("Inspector: {} didn't say in time where this file's preview is", cfg.name)
            rows.append(_unreachable(cfg, kinds.get(cfg.type, "unknown")))
        except Exception as exc:
            logger.debug("Inspector: preview lookup on {} failed: {}", cfg.name, type(exc).__name__)
            rows.append(_unreachable(cfg, kinds.get(cfg.type, "unknown")))
    return rows


def _resting(server_id: str) -> bool:
    with _rest_lock:
        since = _resting_since.get(server_id)
    return since is not None and time.monotonic() - since < REST_S


def _rest(server_id: str) -> None:
    with _rest_lock:
        _resting_since[server_id] = time.monotonic()


def _answered(server_id: str) -> None:
    with _rest_lock:
        _resting_since.pop(server_id, None)


def chosen_preview(rows: list[dict]) -> dict | None:
    """The preview the Inspector draws its frames from: the first BIF that is there, else the first trickplay.

    Args:
        rows: :func:`file_previews`.

    Returns:
        The row, or None when no server has a readable preview.
    """
    usable = [r for r in rows if r.get("exists") and not r.get("error") and r.get("frame_count")]
    for kind in ("bif", "trickplay"):
        for row in usable:
            if row["kind"] == kind:
                return row
    return None
