"""Plex loudness API."""

from __future__ import annotations

from flask import jsonify

from ..auth import api_token_required
from . import api
from .api_markers import job_title, parse_job_request


@api.route("/loudness/jobs", methods=["POST"])
@api_token_required
def create_loudness_job_route():
    """Start a Plex loudness job for chosen libraries, chosen files, or every library with loudness turned on.

    Body: ``libraries`` (``[{"server_id", "library_id"}]``) or ``file_paths`` (files or folders inside ``MEDIA_ROOT``)
    (neither means every library loudness goes to); ``priority`` (1-3 or high/normal/low, default low);
    ``library_name`` (job title).

    Returns:
        201 with the job, 400 for an invalid body, 503 when the config directory isn't writable.
    """
    from ...loudness.job import create_loudness_job

    parsed, error = parse_job_request()
    if error is not None:
        return error
    libraries, resolved_paths, priority, library_name, _data = parsed
    job = create_loudness_job(
        library_name=job_title("Plex loudness", library_name, libraries, resolved_paths),
        priority=priority,
        source="manual",
        libraries=libraries,
        file_paths=resolved_paths,
    )
    return jsonify(job.to_dict()), 201
