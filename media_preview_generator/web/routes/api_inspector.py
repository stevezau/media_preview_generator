"""Inspector API: the search rows' statuses, a show's episodes, one file's previews, and exact frames from the video.

The search itself is ``GET /api/media/search``; Intro & Credits data, saving, locking and re-detecting are the
``/api/markers/item*`` routes. These add only what the Inspector needs beyond them.
"""

from __future__ import annotations

import base64
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from flask import jsonify, request
from loguru import logger

from ..auth import api_token_required
from . import api
from ._helpers import MEDIA_ROOT, _safe_resolve_within, limiter
from .api_bif import _get_plex_config_folder, _validate_path_under_any_server
from .api_markers import _library_file, _library_roots, _registry, _without_secrets

MAX_STATUS_PATHS = 30
MAX_SHOW_FOLDERS = 10
DEFAULT_FRAME_COUNT = 7
# The status route's budget for all its rows: each row asks its servers at once, all of them together for at most
# previews.LOOKUP_TIMEOUT_S.
_STATUS_WAIT_S = 12.0
# Every server request these routes make stops after this long (a job can afford the configured 30 s; a page can't).
_SERVER_TIMEOUT_S = 8
_STATUS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="inspector-status")
# A file a server was asked about and knew: the frames route doesn't ask again for this long.
_KNOWN_TTL_S = 600.0
_KNOWN_MAX = 512
_known_paths: dict[str, float] = {}
_lookup_locks: dict[str, threading.Lock] = {}
_known_lock = threading.Lock()
# No real video is two days long; a start past this is a bad query, not a time.
_MAX_START_MS = 48 * 3600 * 1000
_ACTIVE_STATUSES = ("pending", "running")


def _json_paths(limit: int) -> tuple[list[str] | None, tuple[Any, int] | None]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify({"error": "The request body must be a JSON object with paths"}), 400)
    paths = data.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        return None, (jsonify({"error": "paths must be a non-empty list of paths"}), 400)
    if len(paths) > limit:
        return None, (jsonify({"error": f"At most {limit} paths at a time"}), 400)
    return list(dict.fromkeys(p.strip() for p in paths)), None


def _preview_state(rows: list[dict]) -> dict:
    ready = [r for r in rows if r.get("exists")]
    if ready:
        frames = next((r.get("frame_count") for r in ready if r.get("frame_count")), None)
        return {"state": "ready", "frames": frames, "servers": [r["server_name"] for r in ready]}
    if any(r.get("error") for r in rows):
        return {"state": "unknown", "frames": None, "servers": []}
    return {"state": "missing", "frames": None, "servers": []}


def _status_of(path: str, registry: Any, store: Any, plex_config_folder: str) -> dict:
    from ...inspector.previews import file_previews
    from ...inspector.statuses import file_kind, markers_state, quality_from_name
    from ...markers.ownership import owning_servers

    safe = _library_file(path, registry)
    if safe is None:
        return {"in_library": False}
    owners = owning_servers(safe, registry)
    rows = file_previews(safe, owners, plex_config_folder=plex_config_folder, with_facts=False)
    return {
        "in_library": True,
        "exists": True,
        "kind": file_kind(safe),
        "quality": quality_from_name(safe),
        "servers": [cfg.name for cfg, _server, _matches in owners],
        "preview": _preview_state(rows),
        "markers": markers_state(store, safe),
    }


@api.route("/inspector/status", methods=["POST"])
@api_token_required
@limiter.limit("120 per minute")
def inspector_status():
    """Statuses for the Inspector's search rows.

    Body: ``{"paths": [local file paths]}`` (at most ``MAX_STATUS_PATHS``).

    Returns:
        200 with ``{"items": {path: {...}}}``: a path that isn't a file inside a server library gets ``in_library``
        false; every other one ``kind`` (``movie``/``episode``), ``quality`` (from its name, e.g. "2160p Dolby
        Vision"), ``servers`` (names of the servers whose library holds it), ``preview`` (``state``: ``ready``,
        ``missing`` or ``unknown`` when a server couldn't be asked; ``frames`` for a BIF) and ``markers``
        (``state`` and ``label``, from markers.db). A file whose status took too long gets ``error``. 400 for a bad
        body.
    """
    from ...markers.store import get_marker_store

    paths, refused = _json_paths(MAX_STATUS_PATHS)
    if refused is not None:
        return refused
    registry = _registry(timeout_s=_SERVER_TIMEOUT_S)
    store = get_marker_store()
    plex_config_folder = _get_plex_config_folder()
    futures = {p: _STATUS_POOL.submit(_status_of, p, registry, store, plex_config_folder) for p in paths}
    items: dict[str, dict] = {}
    deadline = time.monotonic() + _STATUS_WAIT_S
    for path, future in futures.items():
        try:
            items[path] = future.result(timeout=max(0.1, deadline - time.monotonic()))
        except FutureTimeoutError:
            # A row not started yet never starts: the answer has gone, and the next search mustn't queue behind it.
            future.cancel()
            items[path] = {"in_library": True, "error": "Took too long to check"}
        except Exception as exc:
            logger.warning("Inspector status for a search row failed: {}", type(exc).__name__)
            items[path] = {"in_library": True, "error": "Couldn't check this file"}
    return jsonify({"items": _without_secrets(items, registry)})


@api.route("/inspector/show", methods=["POST"])
@api_token_required
@limiter.limit("60 per minute")
def inspector_show():
    """A show's seasons and episodes, read from its folders, each episode's Intro & Credits state from markers.db.

    Body: ``{"paths": [the show's local folders]}`` (a search row's ``paths``; at most ``MAX_SHOW_FOLDERS``).

    Returns:
        200 with ``{"seasons": inspector.statuses.show_seasons}``; 400 when no path is a folder inside a server
        library.
    """
    from ...inspector.statuses import show_seasons
    from ...markers.store import get_marker_store

    paths, refused = _json_paths(MAX_SHOW_FOLDERS)
    if refused is not None:
        return refused
    roots = _library_roots(_registry())
    folders = []
    for raw in paths:
        safe = _validate_path_under_any_server(raw, roots)
        if safe and os.path.isdir(safe) and _safe_resolve_within(safe, MEDIA_ROOT) is not None:
            folders.append(safe)
    if not folders:
        return jsonify({"error": "None of these is a folder inside a server library"}), 400
    return jsonify({"seasons": show_seasons(folders, get_marker_store())})


def _normalized(path: object) -> str | None:
    """An absolute, normalised path without ``..`` inside the media root; None for anything else."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return None
    normalized = os.path.normpath(path.strip())
    if not os.path.isabs(normalized) or ".." in normalized.split(os.sep):
        return None
    if _safe_resolve_within(normalized, MEDIA_ROOT) is None:
        return None
    return normalized


def _under_any(path: str, roots: list[str]) -> bool:
    for root in roots:
        root_n = os.path.normpath(root)
        if root_n and (path == root_n or path.startswith(root_n.rstrip(os.sep) + os.sep)):
            return True
    return False


def _worker_files(job: Any) -> list[str]:
    files = [getattr(job.progress, "current_file", "") or ""]
    for worker in getattr(job.progress, "workers", None) or []:
        files.append(
            worker.get("current_file", "") if isinstance(worker, dict) else getattr(worker, "current_file", "")
        )
    return [f for f in files if f]


def active_job_for(path: str) -> dict | None:
    """The queued or running job that works on ``path``: one started for it, or one whose worker has it now.

    Args:
        path: The file's local path.

    Returns:
        ``id``, ``kind``, ``status``, ``name`` (the job's title) and ``percent``, running jobs first; None when no job
        has it.
    """
    from ..jobs import get_job_manager

    found = []
    for job in get_job_manager().get_all_jobs():
        status = getattr(job.status, "value", job.status)
        if status not in _ACTIVE_STATUSES:
            continue
        cfg = job.config or {}
        listed = path in (cfg.get("file_paths") or []) or path in (cfg.get("webhook_paths") or [])
        if listed or path in _worker_files(job):
            found.append((status != "running", job))
    if not found:
        return None
    _pending, job = sorted(found, key=lambda pair: pair[0])[0]
    return {
        "id": job.id,
        "kind": job.kind,
        "status": getattr(job.status, "value", job.status),
        "name": job.library_name,
        "percent": round(float(job.progress.percent or 0.0), 1),
    }


def _versions(path: str, previews: list[dict]) -> list[dict]:
    from ...inspector.statuses import quality_from_name

    files: list[str] = []
    for row in previews:
        for version in row.get("versions") or []:
            if version not in files:
                files.append(version)
    if len(files) < 2:
        return []
    return [
        {"path": f, "label": quality_from_name(f) or os.path.basename(f), "current": f == path} for f in sorted(files)
    ]


@api.route("/inspector/file", methods=["GET"])
@api_token_required
def inspector_file():
    """One file's facts for the Inspector: where each server keeps its preview and what that preview holds.

    Query: ``path`` (the file's local path, as the search rows and deep links give it).

    Returns:
        200 with ``canonical_path``, ``exists`` (false: gone from disk; null for a path neither in a library nor in
        markers.db, which isn't looked at), ``in_library`` (inside a server library),
        ``known`` (markers.db has it), ``title``, ``kind``, ``quality``; and for a file that is there and in a
        library: ``duration_ms`` (markers.db's, else the preview's), ``previews`` (one row per owning server,
        ``inspector.previews.server_preview``), ``preview`` (the one the frames come from, with ``interval_ms``
        checked against the length, or null), ``versions`` (a server item's other versions on disk, when it has
        several) and ``job`` (the queued or running job that has this file, or null). 400 when the path isn't an
        absolute path inside the media folder.
    """
    from ...inspector.previews import chosen_preview, file_previews, interval_for
    from ...inspector.statuses import file_kind, file_title, quality_from_name
    from ...markers.ownership import owning_servers
    from ...markers.store import get_marker_store

    path = _normalized(request.args.get("path"))
    if path is None:
        return jsonify({"error": "Give the file's full path inside the media folder"}), 400
    registry = _registry(timeout_s=_SERVER_TIMEOUT_S)
    store = get_marker_store()
    rec = store.get_file(path)
    in_library = _under_any(path, _library_roots(registry))
    payload: dict[str, Any] = {
        "canonical_path": path,
        # Only said of a file the app has a reason to know: anything else would answer "is there a file here?" for
        # any path.
        "exists": os.path.isfile(path) if in_library or rec is not None else None,
        "in_library": in_library,
        "known": rec is not None,
        "title": file_title(path),
        "kind": "movie" if rec is not None and rec.is_movie else file_kind(path),
        "quality": quality_from_name(path),
        "duration_ms": rec.duration_ms if rec is not None else None,
        "previews": [],
        "preview": None,
        "versions": [],
        "job": None,
    }
    if not payload["exists"] or not payload["in_library"]:
        return jsonify(payload)
    try:
        payload["job"] = active_job_for(path)
    except Exception as exc:
        logger.debug("Inspector: couldn't list the jobs for a file: {}", type(exc).__name__)
    owners = owning_servers(path, registry)
    previews = file_previews(path, owners, plex_config_folder=_get_plex_config_folder())
    payload["previews"] = previews
    payload["versions"] = _versions(path, previews)
    chosen = chosen_preview(previews)
    if chosen is not None:
        chosen = dict(chosen)
        chosen["interval_ms"] = interval_for(chosen, payload["duration_ms"])
        if not payload["duration_ms"] and chosen["interval_ms"]:
            payload["duration_ms"] = chosen["frame_count"] * chosen["interval_ms"]
        payload["preview"] = chosen
    return jsonify(_without_secrets(payload, registry))


def _knows(path: str, registry: Any) -> bool:
    """Whether the app already knows ``path``: markers.db has it, or a server holding it lists it (remembered a while)."""
    from ...markers.ownership import owning_servers
    from ...markers.store import get_marker_store

    if get_marker_store().get_file(path) is not None:
        return True
    with _known_lock:
        lookup_lock = _lookup_locks.setdefault(path, threading.Lock())
    # The page asks for several strips of one file at once: one of them asks the servers, the rest wait for its answer.
    with lookup_lock:
        try:
            with _known_lock:
                seen = _known_paths.get(path)
            if seen is not None and time.monotonic() - seen < _KNOWN_TTL_S:
                return True
            for _cfg, server, matches in owning_servers(path, registry):
                try:
                    item_id = server.resolve_remote_path_to_item_id(path, library_ids=[m.library_id for m in matches])
                except Exception as exc:
                    logger.debug("Inspector frames: item lookup failed: {}", type(exc).__name__)
                    continue
                if item_id:
                    with _known_lock:
                        if len(_known_paths) >= _KNOWN_MAX:
                            _known_paths.pop(next(iter(_known_paths)))
                        _known_paths[path] = time.monotonic()
                    return True
            return False
        finally:
            with _known_lock:
                _lookup_locks.pop(path, None)


def _tonemap_setting() -> str:
    """The previews' tone-map curve (Settings), so HDR frames here look like the preview's."""
    from ..settings_manager import get_settings_manager

    value = str(get_settings_manager().get("tonemap_algorithm") or "hable").strip().lower()
    # Only a plain filter option name goes into the ffmpeg filter chain.
    return value if value.isalpha() else "hable"


def _int_arg(name: str, default: int) -> int | None:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return None


@api.route("/inspector/frames", methods=["GET"])
@api_token_required
@limiter.limit("120 per minute")
def inspector_frames():
    """Exact frames, one a second, read straight from the video (Adjust and Review; works without a preview).

    Query: ``path`` (a file inside a server library that markers.db has or a server lists), ``start_ms`` (the first
    frame, default 0), ``count`` (1-14, default 7), ``width`` (160, 240, 320 or 480; default 320).

    Returns:
        200 with ``{"path", "start_ms", "step_ms", "frames": [{"t_ms", "src"}]}`` (``src`` a JPEG data URI; fewer
        frames when the video ends first); 400 for a bad query or a path that isn't a video inside a server library;
        404 for a file no server lists and markers.db doesn't have; 502 when ffmpeg can't read the frames; 503 when
        other frame reads keep every slot busy.
    """
    from ...config import _resolve_ffmpeg_path
    from ...inspector.frames import MAX_FRAMES, WIDTHS, FramesBusyError, FramesError, exact_frames
    from ...plex_client import VIDEO_EXTENSIONS

    registry = _registry(timeout_s=_SERVER_TIMEOUT_S)
    safe = _library_file(request.args.get("path"), registry)
    if safe is None or os.path.splitext(safe)[1].lower() not in VIDEO_EXTENSIONS:
        return jsonify({"error": "Path is not a video inside any server library"}), 400
    start_ms = _int_arg("start_ms", 0)
    count = _int_arg("count", DEFAULT_FRAME_COUNT)
    width = _int_arg("width", 320)
    if start_ms is None or count is None or width is None:
        return jsonify({"error": "start_ms, count and width must be whole numbers"}), 400
    if not 0 <= start_ms <= _MAX_START_MS or not 1 <= count <= MAX_FRAMES or width not in WIDTHS:
        return jsonify(
            {"error": f"start_ms must be 0 to {_MAX_START_MS}, count 1-{MAX_FRAMES}, width one of {WIDTHS}"}
        ), 400
    if not _knows(safe, registry):
        return jsonify({"error": "This file isn't one the app knows"}), 404
    try:
        frames = exact_frames(
            safe,
            start_ms=start_ms,
            count=count,
            width=width,
            ffmpeg=_resolve_ffmpeg_path(),
            tonemap=_tonemap_setting(),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FramesBusyError:
        return jsonify({"error": "Busy reading other frames; try again in a moment"}), 503
    except FramesError as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify(
        {
            "path": safe,
            "start_ms": start_ms,
            "step_ms": 1000,
            "frames": [
                {"t_ms": t_ms, "src": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}
                for t_ms, jpeg in frames
            ],
        }
    )
