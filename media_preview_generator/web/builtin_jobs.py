"""Settings for the app's built-in scheduled jobs (the Library Health check and Check servers).

Stored under the top-level ``builtin_jobs`` key of ``settings.json``::

    {"library_health": {"enabled": true, "time": "03:00", "catch_up": true},
     "check_servers": {"enabled": true, "time": "04:30"}}

Every field falls back to its default when missing or invalid, so older settings files need no ``upgrade.py``
migration. Times are ``HH:MM`` in the same timezone the scheduler's other daily crons use.
"""

import re
from copy import deepcopy
from typing import Any

LIBRARY_HEALTH_ID = "library_health"
CHECK_SERVERS_ID = "check_servers"

DEFAULT_BUILTIN_JOBS: dict[str, dict[str, Any]] = {
    LIBRARY_HEALTH_ID: {"enabled": True, "time": "03:00", "catch_up": True},
    CHECK_SERVERS_ID: {"enabled": True, "time": "04:30"},
}

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_time(value: str) -> tuple[int, int]:
    """Split a strict ``HH:MM`` string into ``(hour, minute)``.

    Args:
        value: A 24-hour, zero-padded time such as ``"03:00"``.

    Returns:
        ``(hour, minute)``.

    Raises:
        ValueError: ``value`` is not ``HH:MM``.
    """
    match = _TIME_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("time must be HH:MM, for example 03:00")
    return int(match.group(1)), int(match.group(2))


def _valid_field(default: Any, value: Any) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    try:
        parse_time(value)
    except ValueError:
        return False
    return True


def read_builtin_jobs(raw: object) -> dict[str, dict[str, Any]]:
    """The saved ``builtin_jobs`` setting with a default for every missing or invalid field.

    Args:
        raw: The stored value; anything that is not the expected shape reads as all defaults.

    Returns:
        A new dict with every job and field present.
    """
    saved = raw if isinstance(raw, dict) else {}
    result = deepcopy(DEFAULT_BUILTIN_JOBS)
    for job_id, fields in result.items():
        saved_fields = saved.get(job_id)
        if not isinstance(saved_fields, dict):
            continue
        for name, default in fields.items():
            if name in saved_fields and _valid_field(default, saved_fields[name]):
                fields[name] = saved_fields[name]
    return result


def validate_builtin_update(job_id: str, body: object) -> dict[str, Any]:
    """Check a partial update for one built-in job.

    Args:
        job_id: ``library_health`` or ``check_servers``.
        body: The request body; only the fields to change.

    Returns:
        The validated fields, ready to merge over the saved ones.

    Raises:
        KeyError: ``job_id`` is not a built-in job.
        ValueError: ``body`` is not an object, names an unknown field, or holds a bad value.
    """
    if job_id not in DEFAULT_BUILTIN_JOBS:
        raise KeyError(job_id)
    if not isinstance(body, dict) or not body:
        raise ValueError("Send the fields to change as a JSON object")
    allowed = DEFAULT_BUILTIN_JOBS[job_id]
    for name, value in body.items():
        if name not in allowed:
            raise ValueError(f"Unknown field {name!r} for this job")
        if isinstance(allowed[name], bool):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        else:
            parse_time(value)
    return dict(body)


def load_builtin_jobs() -> dict[str, dict[str, Any]]:
    """The built-in job settings as saved in ``settings.json`` (defaults filled in)."""
    from .settings_manager import get_settings_manager

    return read_builtin_jobs(get_settings_manager().get("builtin_jobs"))


def save_builtin_job(job_id: str, changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Merge validated ``changes`` into one job's saved settings.

    Args:
        job_id: A built-in job id.
        changes: Fields already checked by :func:`validate_builtin_update`.

    Returns:
        All built-in job settings after the save.
    """
    from .settings_manager import get_settings_manager

    manager = get_settings_manager()
    with manager.locked():
        current = read_builtin_jobs(manager.get("builtin_jobs"))
        current[job_id].update(changes)
        manager.set("builtin_jobs", current)
    return current
