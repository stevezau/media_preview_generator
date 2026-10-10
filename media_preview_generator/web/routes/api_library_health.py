"""Library health API: the cached check results, starting/cancelling a check, and the to-do file lists."""

from __future__ import annotations

from flask import jsonify, request

from ...library_health.models import Feature, ServerResult
from ...library_health.runner import get_runner
from ...library_health.store import default_store
from ..auth import api_token_required
from ..scheduler import _LIBRARY_HEALTH_NIGHTLY, job_next_run
from . import api

# Above this many files the page runs a library job instead of a path job.
PATH_JOB_LIMIT = 1000
_TYPE_ORDER = {"plex": 0, "emby": 1, "jellyfin": 2}
_MAX_QUERY_LENGTH = 200
_MAX_LIMIT = 500


def _server_sort_key(server: ServerResult) -> tuple[int, str]:
    return _TYPE_ORDER.get(server.type, len(_TYPE_ORDER)), server.name.lower()


def _next_nightly_iso() -> str | None:
    next_run = job_next_run(_LIBRARY_HEALTH_NIGHTLY)
    return next_run.isoformat() if next_run else None


def _int_param(name: str, default: int) -> int | None:
    """An integer query param, or None when it isn't one."""
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return None


def _configured_server_count() -> int | None:
    """How many enabled media servers are saved; None when the setting can't be read."""
    from ..settings_manager import get_settings_manager

    try:
        raw_servers = get_settings_manager().get("media_servers") or []
        return sum(1 for server in raw_servers if server.get("enabled", True))
    except Exception:
        return None


@api.route("/library-health", methods=["GET"])
@api_token_required
def get_library_health():
    """Cached check results for every server, plus the running check and next nightly run."""
    runner = get_runner()
    servers = sorted(default_store().load(), key=_server_sort_key)
    payload = []
    for server in servers:
        entry = server.to_dict()
        entry["error"] = runner.server_error(server.server_id) or entry.get("error", "")
        payload.append(entry)
    stored_ids = {server.server_id for server in servers}
    for server_id, name, server_type, error in runner.failed_servers():
        if server_id not in stored_ids:
            payload.append({"server_id": server_id, "name": name, "type": server_type, "libraries": [], "error": error})
    progress = runner.progress()
    return jsonify(
        {
            "servers": payload,
            "servers_configured": _configured_server_count(),
            "running": progress.to_dict() if progress else None,
            "last_error": runner.last_error(),
            "next_nightly_at": _next_nightly_iso(),
            "path_job_limit": PATH_JOB_LIMIT,
        }
    )


@api.route("/library-health/check", methods=["POST"])
@api_token_required
def start_library_health_check():
    """Start a check of every server; started is false when one is already running."""
    started, progress = get_runner().start_check("manual")
    return jsonify({"started": started, "progress": progress.to_dict() if progress else None}), 202


@api.route("/library-health/cancel", methods=["POST"])
@api_token_required
def cancel_library_health_check():
    """Ask the running check to stop."""
    return jsonify({"cancelled": get_runner().cancel()})


@api.route("/library-health/files", methods=["GET"])
@api_token_required
def list_library_health_files():
    """One page of a library's to-do files for a feature.

    Query: ``server_id`` and ``library_id`` (required), ``feature``, ``q`` (max 200 chars), ``offset`` (>= 0),
    ``limit`` (default 100, clamped to 1-500), ``not_showing`` ("1" or "0").

    Returns:
        ``{"total", "files": [{"path", "title"}]}``; 400 for an invalid parameter.
    """
    server_id = request.args.get("server_id", "")
    library_id = request.args.get("library_id", "")
    if not server_id or not library_id:
        return jsonify({"error": "server_id and library_id are required"}), 400
    try:
        feature = Feature(request.args.get("feature", ""))
    except ValueError:
        return jsonify({"error": "unknown feature"}), 400
    query = request.args.get("q", "")
    if len(query) > _MAX_QUERY_LENGTH:
        return jsonify({"error": f"q must be at most {_MAX_QUERY_LENGTH} characters"}), 400
    offset = _int_param("offset", 0)
    limit = _int_param("limit", 100)
    if offset is None or offset < 0:
        return jsonify({"error": "offset must be a non-negative integer"}), 400
    if limit is None:
        return jsonify({"error": "limit must be an integer"}), 400
    limit = max(1, min(limit, _MAX_LIMIT))
    not_showing = request.args.get("not_showing", "0")
    if not_showing not in ("0", "1"):
        return jsonify({"error": "not_showing must be 0 or 1"}), 400

    total, files = default_store().list_todo(
        server_id, library_id, feature, q=query, offset=offset, limit=limit, not_showing=not_showing == "1"
    )
    return jsonify({"total": total, "files": [{"path": f.path, "title": f.title} for f in files]})


@api.route("/library-health/plex-reread", methods=["POST"])
@api_token_required
def start_plex_reread():
    """Ask Plex to re-read the items whose previews exist but aren't showing."""
    data = request.get_json(silent=True) or {}
    server_id = data.get("server_id")
    if not isinstance(server_id, str) or not server_id:
        return jsonify({"error": "server_id is required"}), 400
    started, result = get_runner().start_reread(server_id)
    if not started:
        return jsonify({"error": result.message}), result.status
    return jsonify({"started": True, "progress": result.to_dict()}), 202
