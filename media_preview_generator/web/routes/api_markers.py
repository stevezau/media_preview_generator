"""Intro & Credits API."""

from __future__ import annotations

import os
import re
from typing import Any

from flask import jsonify, request
from loguru import logger

from ..auth import api_token_required
from ..jobs import PRIORITY_FROM_LABEL, PRIORITY_LABELS, PRIORITY_LOW
from . import api
from ._helpers import MEDIA_ROOT, _param_to_bool, _safe_resolve_within
from .api_bif import _validate_path_under_any_server
from .api_jobs import _config_unwritable_response

_ONLINE_SOURCE_IDS = ("theintrodb", "introdb", "skipdb")
_AUTH_SECRET_KEYS = ("token", "api_key", "password", "access_token")
_MASK = "****"
# Item ids go into the media server's URL path: bare Plex rating keys, Jellyfin/Emby GUIDs (with or without dashes) or
# Emby's numeric ids. Anything else is refused before the server is asked.
_PLEX_ITEM_ID_RE = re.compile(r"[0-9]+")
_EMBYISH_ITEM_ID_RE = re.compile(r"[0-9a-fA-F-]{1,36}|\d+")


def _parse_job_priority(raw: object) -> int | None:
    """A priority from 1/2/3 or high/normal/low; None when it is anything else (bools included)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw in PRIORITY_LABELS else None
    if isinstance(raw, str):
        return PRIORITY_FROM_LABEL.get(raw.strip().lower())
    return None


@api.route("/markers/jobs", methods=["POST"])
@api_token_required
def create_marker_job():
    """Start an Intro & Credits job for chosen libraries, chosen files, or every library with markers turned on.

    Body: ``libraries`` (``[{"server_id", "library_id"}]``) or ``file_paths`` (files or folders inside
    ``MEDIA_ROOT``) — neither means every library Intro & Credits goes to; ``priority`` (1-3 or high/normal/low,
    default low); ``force`` (re-detect files already done); ``library_name`` (job title).

    Returns:
        201 with the job, 400 for an invalid body, 503 when the config directory isn't writable.
    """
    from ...markers.triggers import create_intro_credits_job

    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    data = request.get_json(silent=True)
    if data is None:
        # get_json answers None both for no body and for a body it can't read (no JSON content type, a trailing
        # comma); only an empty body means "every library".
        if request.content_length or request.get_data(cache=True):
            return jsonify({"error": "The request body must be JSON"}), 400
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "The request body must be a JSON object"}), 400
    libraries = data.get("libraries") or []
    file_paths = data.get("file_paths") or []
    if not isinstance(libraries, list) or not all(
        isinstance(x, dict) and str(x.get("server_id") or "").strip() and str(x.get("library_id") or "").strip()
        for x in libraries
    ):
        return jsonify({"error": "libraries must be a list of {server_id, library_id}"}), 400
    if not isinstance(file_paths, list) or not all(isinstance(p, str) and p.strip() for p in file_paths):
        return jsonify({"error": "file_paths must be a list of paths"}), 400
    if libraries and file_paths:
        return jsonify({"error": "Choose libraries or file_paths, not both"}), 400
    priority = _parse_job_priority(data["priority"]) if "priority" in data else PRIORITY_LOW
    if priority is None:
        return jsonify({"error": "priority must be 1, 2, 3, high, normal or low"}), 400
    library_name = data.get("library_name")
    if library_name is not None and not isinstance(library_name, str):
        return jsonify({"error": "library_name must be a string"}), 400
    resolved_paths = []
    for raw in file_paths:
        resolved = _safe_resolve_within(raw.strip(), MEDIA_ROOT)
        if resolved is None:
            return jsonify({"error": f"Path is outside allowed media root: {raw.strip()}"}), 400
        resolved_paths.append(resolved)

    if resolved_paths:
        count = len(resolved_paths)
        default_name = f"Intro & Credits: {count} file{'' if count == 1 else 's'}"
    elif libraries:
        count = len(libraries)
        default_name = f"Intro & Credits: {count} librar{'y' if count == 1 else 'ies'}"
    else:
        default_name = "Intro & Credits: all libraries"
    job = create_intro_credits_job(
        library_name=(library_name or "").strip() or default_name,
        priority=priority,
        source="manual",
        libraries=[{"server_id": str(x["server_id"]), "library_id": str(x["library_id"])} for x in libraries],
        file_paths=resolved_paths,
        force=_param_to_bool(data.get("force"), False),
    )
    return jsonify(job.to_dict()), 201


def _registry() -> Any:
    from ...servers import ServerRegistry
    from ..settings_manager import get_settings_manager

    return ServerRegistry.from_settings(list(get_settings_manager().get("media_servers") or []), legacy_config=None)


def _without_secrets(value: Any, registry: Any) -> Any:
    """``value`` with every configured server credential replaced by ``****`` (error text can echo a request)."""
    secrets = sorted(
        {
            str(cfg.auth.get(key))
            for cfg in registry.configs()
            for key in _AUTH_SECRET_KEYS
            if isinstance(cfg.auth, dict) and cfg.auth.get(key)
        },
        key=len,
        reverse=True,
    )

    def scrub(v: Any) -> Any:
        if isinstance(v, str):
            for secret in secrets:
                v = v.replace(secret, _MASK)
            return v
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items()}
        if isinstance(v, list | tuple):
            return [scrub(x) for x in v]
        return v

    return scrub(value) if secrets else value


def _library_roots(registry: Any) -> list[str]:
    """Local folders of every library on every enabled server, whatever the library's preview opt-in."""
    from ...servers.ownership import apply_path_mappings

    roots: list[str] = []
    for cfg in registry.configs():
        if not cfg.enabled:
            continue
        for lib in cfg.libraries:
            for remote in lib.remote_paths:
                if (remote or "").strip():
                    roots.extend(r for r in apply_path_mappings(remote, cfg.path_mappings or []) if (r or "").strip())
    return roots


def _library_file(path: object, registry: Any) -> str | None:
    """A file inside a server library (and the media root), normalised; None for anything else."""
    if not isinstance(path, str) or not path.strip():
        return None
    safe = _validate_path_under_any_server(path, _library_roots(registry))
    if safe is None or not os.path.isfile(safe) or _safe_resolve_within(safe, MEDIA_ROOT) is None:
        return None
    return safe


def _server_off_response(cfg: Any) -> tuple[Any, int]:
    message = f"{cfg.name or 'This server'} is disabled. Re-enable it on the Servers page first."
    return jsonify({"ok": False, "error": message}), 409


@api.route("/markers/servers/<server_id>/status", methods=["GET"])
@api_token_required
def marker_server_status(server_id: str):
    """Intro & Credits status for a server's Edit dialog.

    Returns:
        200 with ``markers.inspect.server_status_payload`` (capability checked as if Intro & Credits were on, state
        ``unknown`` when the check failed; a server turned off on the Servers page isn't contacted), 404 for an unknown
        server, 500 with a JSON error when the status can't be built.
    """
    from ...markers import inspect

    registry = _registry()
    cfg = registry.get_config(server_id)
    if cfg is None:
        return jsonify({"error": "server not found"}), 404
    try:
        payload = inspect.server_status_payload(registry.get(server_id), cfg)
    except Exception as exc:
        logger.warning("Intro & Credits status for {} failed: {}", cfg.name, type(exc).__name__)
        return jsonify({"error": "Couldn't check this server's Intro & Credits status"}), 500
    return jsonify(_without_secrets(payload, registry))


@api.route("/markers/sources/usage", methods=["GET"])
@api_token_required
def marker_source_usage():
    """Today's (UTC) lookups per online source; TheIntroDB's limit and remaining come from its response headers.

    Returns:
        200 with ``{source_id: {"day", "used", "limit", "remaining", "has_key", "low_priority_exhausted",
        "resets_at"}}``. ``low_priority_exhausted`` is true once a backfill (LOW priority) lookup would be refused
        right now; ``resets_at`` is always the daily budget's day boundary in the user's words, never the source's
        own (untrustworthy) reset header. The TheIntroDB key itself is never returned, only whether one is stored.
    """
    from ...markers.settings import get_global_settings
    from ...markers.sources.ratelimit import RESET_TIME_LABEL, get_limiter, low_priority_exhausted
    from ...markers.store import get_marker_store

    settings = get_global_settings()
    store = get_marker_store()
    out = {}
    for source_id in _ONLINE_SOURCE_IDS:
        limiter = get_limiter(source_id)
        live = limiter.usage()
        day = live["day"]
        # A limiter starts from today's stored row when it is created (after a restart); the stored row still counts
        # when that read-back failed.
        stored = store.source_usage(source_id, day) or {}
        source = settings.source(source_id)
        limit = live["limit"] if live.get("limit") is not None else stored.get("limit")
        remaining = live["remaining"] if live.get("remaining") is not None else stored.get("remaining")
        out[source_id] = {
            "day": day,
            "used": max(live.get("used") or 0, stored.get("used") or 0),
            "limit": limit,
            "remaining": remaining,
            "has_key": bool(source_id == "theintrodb" and source is not None and source.api_key),
            # This source's own share, not the module default: mirrors what its live limiter would actually do.
            "low_priority_exhausted": low_priority_exhausted(
                limit=limit, remaining=remaining, reserve_fraction=limiter.reserve_fraction
            ),
            "resets_at": RESET_TIME_LABEL,
        }
    return jsonify(out)


@api.route("/markers/item", methods=["GET"])
@api_token_required
def marker_item():
    """Inspector data for one file, by ``path`` or by ``server_id`` + ``item_id``.

    Returns:
        200 with ``markers.inspect.item_payload`` (a server whose state can't be read gets a degraded row); 400 when the
        path isn't a file inside a server library, the query is incomplete or ``item_id`` isn't shaped like that
        server's ids; 404 for an unknown server or an item with no file here; 409 when the server is off; 500 with a
        JSON error when the file's data can't be built.
    """
    from ...markers import inspect
    from ...markers.store import get_marker_store
    from ...servers.base import ServerType

    registry = _registry()
    path = request.args.get("path")
    if not path:
        server_id, item_id = request.args.get("server_id"), request.args.get("item_id")
        if not server_id or not item_id:
            return jsonify({"error": "Give path, or server_id and item_id"}), 400
        cfg = registry.get_config(server_id)
        server = registry.get(server_id)
        if cfg is None or server is None:
            return jsonify({"error": "server not found"}), 404
        item_id_re = _PLEX_ITEM_ID_RE if cfg.type is ServerType.PLEX else _EMBYISH_ITEM_ID_RE
        if not item_id_re.fullmatch(item_id):
            return jsonify({"error": "item_id isn't an item id this server uses"}), 400
        if not cfg.enabled:
            return _server_off_response(cfg)
        path = inspect.resolve_local_path(server, cfg, item_id)
        if not path:
            return jsonify({"error": "No file on this app's disk for that item"}), 404
    safe = _library_file(path, registry)
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    try:
        payload = inspect.item_payload(safe, registry=registry, store=get_marker_store())
    except Exception as exc:
        # One server's failure only degrades its row; this is the file-level part (e.g. markers.db unreadable).
        logger.warning("Inspector data for a file failed: {}", type(exc).__name__)
        return jsonify({"error": "Couldn't build the Intro & Credits data for this file"}), 500
    return jsonify(_without_secrets(payload, registry))


@api.route("/markers/item/redetect", methods=["POST"])
@api_token_required
def marker_item_redetect():
    """Run Intro & Credits again for one file, asking every source again.

    Body: ``{"path"}``.

    Returns:
        202 with ``{"job_id"}`` (a HIGH priority, forced, single-file job; this file's re-detect still queued or
        running when there is one); 400 when the path isn't a file inside a server library; 503 when the config
        directory isn't writable.
    """
    from ...markers.triggers import submit_redetect

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "The request body must be a JSON object with a path"}), 400
    safe = _library_file(data.get("path"), _registry())
    if safe is None:
        return jsonify({"error": "Path is not a file inside any server library"}), 400
    blocked = _config_unwritable_response()
    if blocked is not None:
        return blocked
    return jsonify({"job_id": submit_redetect(safe)}), 202
