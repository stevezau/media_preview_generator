"""One persisted worker policy for settings, quick scaling and setup."""

from __future__ import annotations

from datetime import datetime

from flask import jsonify, request
from loguru import logger

from ...job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_LOUDNESS, JOB_KIND_PREVIEWS
from ...worker_groups import (
    MAX_CPU_WORKERS,
    MAX_GPU_WORKERS,
    MAX_MEMBERS,
    application_timezone,
    configured_group_totals,
    future_capacity,
    group_is_available,
    legacy_group_view,
    member_policies,
    next_group_opening,
)
from ..auth import setup_or_auth_required
from ..settings_manager import WorkerGroupsConflict, get_settings_manager
from . import api


def future_job_capacity(settings, kind: str) -> int:
    """Count configured compatible capacity with some time outside global quiet hours."""
    return future_capacity(settings.worker_groups, settings.get("quiet_hours"), kind)


def reconcile_group_settings(settings) -> str | None:
    """Apply the saved policy and wake waiters; never manufacture a processing pool."""
    from ...jobs.group_runtime import refresh_worker_groups, wake_group_runtime
    from ._helpers import _ensure_gpu_cache
    from .api_jobs import _get_shared_worker_pool
    from .job_runner import _build_selected_gpus

    error = None
    try:
        pool = _get_shared_worker_pool()
        if pool is not None:
            detected = _ensure_gpu_cache()
            with settings.locked():
                refresh_worker_groups(pool, selected_gpus=_build_selected_gpus(settings, detected=detected), force=True)
        from .api_settings import _resize_text_detection_cpu_helpers

        _resize_text_detection_cpu_helpers()
    except Exception:
        logger.exception("Worker groups saved, but live reconciliation failed")
        error = "Worker groups were saved, but the live worker refresh failed. Check the application log."
    finally:
        wake_group_runtime()
    return error


def live_member_rows(pool) -> dict[tuple[str, str], dict]:
    """Index the pool's per-member snapshots by ``(group_id, member_id)``; empty when no pool is running."""
    if pool is None or not hasattr(pool, "member_snapshots"):
        return {}
    return {(row["group_id"], row["member_id"]): row for row in pool.member_snapshots()}


def _member_state(enabled: bool, detected: bool, opened: bool) -> str:
    if not enabled:
        return "disabled"
    if not detected:
        return "hardware_unavailable"
    return "active" if opened else "off_hours"


def _member_row(policy: dict, status: dict | None, devices: set, paused: bool) -> dict:
    """Capacity row for one saved member, preferring the pool's live numbers over the saved-policy fallback."""
    detected = policy["resource"] == "cpu" or policy["device"] in devices
    opened = group_is_available(policy)
    next_opening = next_group_opening(policy) if detected and not opened else None
    fallback_target = policy["count"] if policy["enabled"] and detected and opened else 0
    status = status or {}
    return {
        "id": policy["member_id"],
        "resource": policy["resource"],
        "device": policy["device"],
        "desired": policy["count"] if policy["enabled"] else 0,
        "target": status.get("target", fallback_target),
        "available": 0 if paused else status.get("available", fallback_target),
        "busy": max(0, status.get("running", 0) - status.get("finishing", 0)),
        "finishing": status.get("finishing", 0),
        "state": status.get("state") or _member_state(policy["enabled"], detected, opened),
        "next_available_at": status.get("next_opening") or (next_opening.isoformat() if next_opening else None),
    }


def _draining_member_row(row: dict) -> dict:
    return {
        "id": row["member_id"],
        "resource": row.get("resource"),
        "device": row.get("device"),
        "desired": 0,
        "target": row.get("target", 0),
        "available": 0,
        "busy": 0,
        "finishing": max(row.get("finishing", 0), row.get("running", 0)),
        "state": "draining",
        "next_available_at": None,
    }


def _group_row(group_id: str, name: str, enabled: bool, members: list[dict]) -> dict:
    """Aggregate member rows: counts are sums; the state follows the design's precedence."""
    states = [member["state"] for member in members]
    finishing = sum(member["finishing"] for member in members)
    target = sum(member["target"] for member in members)
    if not enabled:
        state = "disabled"
    elif finishing and not target:
        state = "draining"
    elif "active" in states:
        state = "active"
    elif states and all(value == "hardware_unavailable" for value in states):
        state = "hardware_unavailable"
    else:
        state = "off_hours"
    openings = sorted(member["next_available_at"] for member in members if member["next_available_at"])
    return {
        "id": group_id,
        "name": name,
        "desired": sum(member["desired"] for member in members),
        "target": target,
        "available": sum(member["available"] for member in members),
        "busy": sum(member["busy"] for member in members),
        "finishing": finishing,
        "state": state,
        "next_available_at": openings[0] if openings and state == "off_hours" else None,
        "members": members,
    }


def worker_group_payload(settings=None) -> dict:
    """Read current policy and activity without exposing private configuration."""
    from ._helpers import _ensure_gpu_cache
    from .api_jobs import _get_shared_worker_pool

    settings = settings or get_settings_manager()
    with settings.locked():
        groups = settings.worker_groups
        revision = settings.worker_groups_revision
    hardware = [
        {key: entry.get(key) for key in ("device", "name", "type", "status")}
        for entry in _ensure_gpu_cache()
        if isinstance(entry, dict)
    ]
    devices = {entry["device"] for entry in hardware if entry.get("status") != "failed"}
    live = live_member_rows(_get_shared_worker_pool())
    paused = settings.processing_paused
    rows = []
    warnings = []
    for group in groups:
        member_rows = []
        for policy in member_policies([group]):
            key = (policy["group_id"], policy["member_id"])
            row = _member_row(policy, live.pop(key, None), devices, paused)
            member_rows.append(row)
            if group["enabled"] and row["state"] == "hardware_unavailable":
                warnings.append(
                    {
                        "code": "hardware_unavailable",
                        "group_id": group["id"],
                        "member_id": policy["member_id"],
                        "message": f"{group['name']}: GPU {policy['device']} not detected. "
                        "Its jobs wait; other devices keep working.",
                    }
                )
        for key in [key for key in live if key[0] == group["id"]]:
            if live[key].get("finishing") or live[key].get("running"):
                member_rows.append(_draining_member_row(live[key]))
            del live[key]
        rows.append(_group_row(group["id"], group["name"], group["enabled"], member_rows))
    removed: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for (group_id, _member_id), row in live.items():
        if row.get("finishing") or row.get("running"):
            removed.setdefault(group_id, []).append(_draining_member_row(row))
            names[group_id] = row.get("name") or group_id
    for group_id, member_rows in removed.items():
        rows.append(_group_row(group_id, names[group_id], False, member_rows))
        rows[-1]["state"] = "draining"
    enabled_kinds = {JOB_KIND_PREVIEWS}
    for server in settings.get("media_servers", []) or []:
        if not isinstance(server, dict) or not server.get("enabled", True):
            continue
        if (server.get("loudness") or {}).get("enabled"):
            enabled_kinds.add(JOB_KIND_LOUDNESS)
        if (server.get("markers") or {}).get("enabled"):
            enabled_kinds.add(JOB_KIND_INTRO_CREDITS)
    labels = {
        JOB_KIND_PREVIEWS: "Video previews",
        JOB_KIND_INTRO_CREDITS: "Intro & Credits",
        JOB_KIND_LOUDNESS: "Plex loudness",
    }
    for kind in sorted(enabled_kinds):
        if not future_job_capacity(settings, kind):
            warnings.append(
                {
                    "code": "no_eligible_workers",
                    "job_type": kind,
                    "message": f"{labels[kind]} has no compatible worker hours outside global quiet hours. Add or enable a group and check its hours.",
                }
            )
    gpu_peak, cpu_peak = configured_group_totals(groups)
    timezone = application_timezone()
    timezone_name = str(timezone)
    timezone_label = timezone_name
    if timezone_name == "Local time":
        offset = datetime.now(timezone).strftime("%z")
        timezone_label = f"Local time (UTC{offset[:3]}:{offset[3:]})"
    members_by_resource = [member for row in rows for member in row["members"]]
    return {
        "groups": [{**group, **legacy_group_view(group)} for group in groups],
        "revision": revision,
        "timezone": timezone_name,
        "timezone_label": timezone_label,
        "limits": {"cpu": MAX_CPU_WORKERS, "gpu": MAX_GPU_WORKERS, "members": MAX_MEMBERS},
        "hardware": hardware,
        "capacity": {
            "groups": rows,
            "current": {
                resource: sum(member["target"] for member in members_by_resource if member["resource"] == resource)
                for resource in ("cpu", "gpu")
            },
            "peak": {"cpu": cpu_peak, "gpu": gpu_peak},
        },
        "warnings": warnings,
        "processing_paused": paused,
        "pause_reasons": settings.processing_pause_reasons,
    }


@api.route("/worker-groups", methods=["GET"])
@setup_or_auth_required
def get_worker_groups():
    """Return worker configuration and the current reasons for waiting."""
    return jsonify(worker_group_payload())


@api.route("/worker-groups", methods=["PUT"])
@setup_or_auth_required
def save_worker_groups():
    """Commit a staged policy, refusing to overwrite concurrent quick scaling."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("revision")) is not int or "groups" not in data:
        return jsonify({"error": "Provide groups and the revision you loaded"}), 400
    settings = get_settings_manager()
    try:
        settings.update_worker_groups(data["groups"], expected_revision=data["revision"])
    except WorkerGroupsConflict as exc:
        return jsonify({"error": str(exc), "revision": settings.worker_groups_revision}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_scaled_response(settings))


class ScaleRefused(Exception):
    """A quick-scale request that cannot be applied; carries the HTTP status and any extra response fields."""

    def __init__(self, message: str, status: int, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.extra = extra


def _scale_saved_member(group_id: str, member_id: str, delta: int) -> None:
    """Change one member's count by ``delta`` under the same lock and validation as a full edit.

    Raises:
        ScaleRefused: Unknown group or member (404), disabled group (409) or a count outside 1-32 (400).
        ValueError: The aggregate policy was refused (for example the weekly peak).
    """
    settings = get_settings_manager()
    with settings.locked():
        groups = settings.worker_groups
        group = next((entry for entry in groups if entry["id"] == group_id), None)
        if group is None:
            raise ScaleRefused("Worker group no longer exists", 404)
        member = next((entry for entry in group["members"] if entry["id"] == member_id), None)
        if member is None:
            raise ScaleRefused("That device is no longer in this group", 404)
        if not group["enabled"]:
            raise ScaleRefused("Enable the group first", 409)
        count = member["count"] + delta
        limit = MAX_CPU_WORKERS if member["resource"] == "cpu" else MAX_GPU_WORKERS
        if not 1 <= count <= limit:
            raise ScaleRefused(f"A device needs 1\u2013{limit} workers. Remove it in Settings to use zero.", 400)
        member["count"] = count
        settings.update_worker_groups(groups)


def scale_saved_group(
    group_id: str, *, delta: int | None = None, enabled: bool | None = None, member_id: str | None = None
) -> None:
    """Change one saved group under the same lock and validation as a full edit.

    A ``delta`` on a single-member group keeps the old meaning (at zero the group is disabled). On a
    multi-member group it needs ``member_id`` and then follows the strict member rules.

    Raises:
        KeyError: The group does not exist.
        ScaleRefused: A multi-member group without ``member_id`` (409) or a refused member change.
        ValueError: The aggregate policy was refused.
    """
    settings = get_settings_manager()
    with settings.locked():
        groups = settings.worker_groups
        group = next((entry for entry in groups if entry["id"] == group_id), None)
        if group is None:
            raise KeyError(group_id)
        if enabled is not None:
            group["enabled"] = enabled
        elif delta is not None:
            if len(group["members"]) > 1 or member_id is not None:
                if member_id is None:
                    raise ScaleRefused(
                        "Choose a device to scale",
                        409,
                        members=[member["id"] for member in group["members"]],
                    )
                _scale_saved_member(group_id, member_id, delta)
                return
            member = group["members"][0]
            target = (member["count"] if group["enabled"] else 0) + delta
            if target <= 0:
                group["enabled"] = False
            else:
                member["count"] = target
                group["enabled"] = True
        settings.update_worker_groups(groups)


def _scaled_response(settings) -> dict:
    error = reconcile_group_settings(settings)
    result = worker_group_payload(settings)
    result["success"] = True
    if error:
        result["warning"] = error
    return result


@api.route("/worker-groups/<group_id>/scale", methods=["POST"])
@setup_or_auth_required
def scale_worker_group(group_id: str):
    """Apply a quick count/enable change without a stale whole-policy payload."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Provide a scaling action"}), 400
    if set(data) == {"delta"} and type(data["delta"]) is int and data["delta"] in (-1, 1):
        changes = {"delta": data["delta"]}
    elif set(data) == {"enabled"} and isinstance(data["enabled"], bool):
        changes = {"enabled": data["enabled"]}
    else:
        return jsonify({"error": "Provide delta +1/-1 or enabled true/false"}), 400
    try:
        scale_saved_group(group_id, **changes)
    except KeyError:
        return jsonify({"error": "Worker group no longer exists"}), 404
    except ScaleRefused as exc:
        return jsonify({"error": str(exc), **exc.extra}), exc.status
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_scaled_response(get_settings_manager()))


@api.route("/worker-groups/<group_id>/members/<member_id>/scale", methods=["POST"])
@setup_or_auth_required
def scale_worker_group_member(group_id: str, member_id: str):
    """Add or remove one worker on one device of a group."""
    data = request.get_json(silent=True)
    if not (
        isinstance(data, dict) and set(data) == {"delta"} and type(data["delta"]) is int and data["delta"] in (-1, 1)
    ):
        return jsonify({"error": "Provide delta +1 or -1"}), 400
    try:
        _scale_saved_member(group_id, member_id, data["delta"])
    except ScaleRefused as exc:
        return jsonify({"error": str(exc), **exc.extra}), exc.status
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(_scaled_response(get_settings_manager()))
