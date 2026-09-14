"""Intro & Credits API."""

from __future__ import annotations

from flask import jsonify, request

from ..auth import api_token_required
from ..jobs import PRIORITY_FROM_LABEL, PRIORITY_LABELS, PRIORITY_LOW
from . import api
from ._helpers import MEDIA_ROOT, _param_to_bool, _safe_resolve_within
from .api_jobs import _config_unwritable_response


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
