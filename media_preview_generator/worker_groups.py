"""Validated worker policy and weekly availability, independent of live workers.

A saved group is ``id``, ``name``, ``enabled``, ``availability`` and ``members``; each member is one device
(``resource``/``device``) with a worker ``count`` and its own ``job_types``. The runtime works on the flat
``member_policies`` view, where every member looks like a legacy single-device group plus ``group_id``/``member_id``.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from datetime import UTC, datetime, timedelta, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .job_kinds import JOB_KIND_LOUDNESS, JOB_KINDS

MAX_CPU_WORKERS = 64
MAX_GPU_WORKERS = 64
MAX_GROUPS = 64
MAX_MEMBERS = 8
LEGACY_MEMBER_ID = "m1"
WEEK_MINUTES = 7 * 24 * 60
_ALL_WEEK = (1 << WEEK_MINUTES) - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]\Z")


def application_timezone() -> tzinfo:
    """Return the application's local timezone with its daylight-saving rules."""
    return _timezone_for_name(os.environ.get("TZ", "").lstrip(":"))


@lru_cache(maxsize=8)
def _timezone_for_name(name: str) -> tzinfo:
    if name:
        try:
            return ZoneInfo(name)
        except (ValueError, ZoneInfoNotFoundError):
            pass
    try:
        # A container bind mount can replace a symlink target's contents without
        # changing its name. Read the mounted rules rather than infer an IANA name.
        with open("/etc/localtime", "rb") as stream:
            return ZoneInfo.from_file(stream, key="Local time")
    except (OSError, ValueError, ZoneInfoNotFoundError):
        return UTC


def local_now(now: datetime | None = None) -> datetime:
    """Use application time, or an explicitly supplied local clock for evaluation."""
    if now is None:
        return datetime.now(application_timezone())
    return now if now.tzinfo is not None else now.replace(tzinfo=application_timezone())


def group_resource_key(policy: dict) -> str:
    """Identify the shared capacity budget, separately from individual groups.

    Args:
        policy: A member policy (or a legacy flat group, or a group with exactly one member).

    Raises:
        ValueError: A group with several members has no single resource key.
    """
    if "members" in policy:
        policy = _single_policy(policy)
    return "cpu" if policy["resource"] == "cpu" else f"gpu:{policy['device']}"


def _single_policy(group: dict) -> dict:
    policies = member_policies([group])
    if len(policies) != 1:
        raise ValueError("A group with several devices has no single resource; use member_policies")
    return policies[0]


def supports_job(policy: dict, kind: str) -> bool:
    """Apply intrinsic hardware capability before the owner's job permissions.

    Args:
        policy: A member policy; a saved group with members is supported when any member supports the kind.
    """
    if "members" in policy:
        return any(supports_job(member, kind) for member in member_policies([policy]))
    return (
        kind in JOB_KINDS
        and kind in policy.get("job_types", [])
        and (kind != JOB_KIND_LOUDNESS or policy.get("resource") == "cpu")
    )


def member_policies(groups: list[dict]) -> list[dict]:
    """One flat policy per member, shaped like a v21 group plus its owners.

    Accepts validated groups; a legacy flat group (no ``members``) counts as one member ``m1`` so a stale
    snapshot or a hand-edited file cannot crash the runtime. Already-flat policies pass through unchanged.
    Pure: no settings, locks or I/O.

    Returns:
        Dicts with ``id`` (``"<group_id>:<member_id>"``), ``group_id``, ``member_id``, ``name`` (the group's),
        ``enabled``, ``availability`` (the group's), ``resource``, ``device``, ``count``, ``job_types``.
    """
    policies: list[dict] = []
    for group in groups:
        if "member_id" in group and "members" not in group:
            policies.append(dict(group))
            continue
        members = group.get("members")
        if members is None:
            members = [{**group, "id": LEGACY_MEMBER_ID}]
        for member in members:
            policies.append(
                {
                    "id": f"{group['id']}:{member['id']}",
                    "group_id": group["id"],
                    "member_id": member["id"],
                    "name": group.get("name", group["id"]),
                    "enabled": group.get("enabled", True),
                    "availability": group.get("availability", {"mode": "always", "windows": []}),
                    "resource": member["resource"],
                    "device": member.get("device"),
                    "count": member["count"],
                    "job_types": list(member.get("job_types", [])),
                }
            )
    return policies


def legacy_group_view(group: dict) -> dict:
    """Deprecated flat echo (resource, device, count, job_types) for a single-member group; ``{}`` otherwise."""
    members = group.get("members") or []
    if len(members) != 1:
        return {}
    member = members[0]
    return {
        "resource": member["resource"],
        "device": member.get("device"),
        "count": member["count"],
        "job_types": list(member.get("job_types", [])),
    }


def _minute(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def group_weekly_mask(group: dict) -> int:
    """Return eligible wall-clock minutes; overlapping windows count once."""
    if not group.get("enabled", True):
        return 0
    availability = group.get("availability", {"mode": "always"})
    if availability.get("mode") == "always":
        return _ALL_WEEK
    mask = 0
    for window in availability.get("windows", []):
        start, end = _minute(window["start"]), _minute(window["end"])
        duration = (end - start) % (24 * 60)
        for day in window["days"]:
            offset = day * 24 * 60 + start
            segment = ((1 << duration) - 1) << offset
            mask |= (segment & _ALL_WEEK) | (segment >> WEEK_MINUTES)
    return mask


def _clock_minute(now: datetime) -> int:
    return now.weekday() * 1440 + now.hour * 60 + now.minute


def group_is_available(group: dict, now: datetime | None = None) -> bool:
    """Whether this group can start new files at the supplied local wall time."""
    if not group.get("enabled", True):
        return False
    if group.get("availability", {}).get("mode", "always") == "always":
        return True
    return bool(group_weekly_mask(group) & (1 << _clock_minute(local_now(now))))


def next_mask_opening(mask: int, now: datetime | None = None) -> datetime | None:
    """Find real elapsed-time availability, including skipped/repeated DST hours."""
    if not mask:
        return None
    current = local_now(now)
    if mask & (1 << _clock_minute(current)):
        return current
    # Walk elapsed minutes, not local arithmetic: a skipped 02:30 must not become
    # an imaginary time, and both occurrences of a repeated hour are eligible.
    cursor = current.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    # A weekly window can disappear entirely in a spring-forward gap. Search
    # through the following week too, rather than reporting no future opening.
    for _ in range(15 * 1440):
        candidate = cursor.astimezone(current.tzinfo)
        if mask & (1 << _clock_minute(candidate)):
            return candidate
        cursor += timedelta(minutes=1)
    return None


def next_group_opening(group: dict, now: datetime | None = None) -> datetime | None:
    """Return the next eligible instant, or None for a disabled group."""
    return next_mask_opening(group_weekly_mask(group), now)


def _peak(policies: list[dict]) -> int:
    changes: dict[int, int] = {}
    for group in policies:
        mask = group_weekly_mask(group)
        while mask:
            start = (mask & -mask).bit_length() - 1
            shifted = mask >> start
            length = (shifted ^ (shifted + 1)).bit_length() - 1
            end = start + length
            changes[start] = changes.get(start, 0) + group["count"]
            changes[end] = changes.get(end, 0) - group["count"]
            mask &= ~(((1 << length) - 1) << start)
    current = peak = 0
    for point in sorted(changes):
        current += changes[point]
        peak = max(peak, current)
    return peak


def configured_group_totals(groups: list[dict]) -> tuple[int, int]:
    """Return peak (GPU, CPU) capacity, retaining the existing family limits."""
    policies = member_policies(groups)
    gpu, cpu = (_peak([p for p in policies if p["resource"] == resource]) for resource in ("gpu", "cpu"))
    return gpu, cpu


def _clean_member(raw: object, name: str, seen_ids: set[str], seen_devices: set[str], legacy: bool) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{name}: each device must be an object")
    member_id = LEGACY_MEMBER_ID if legacy else raw.get("id")
    if not isinstance(member_id, str) or not _ID.fullmatch(member_id) or member_id in seen_ids:
        raise ValueError(f"{name}: each device needs a unique valid ID")
    seen_ids.add(member_id)
    resource = raw.get("resource")
    if resource not in ("cpu", "gpu"):
        raise ValueError(f"{name}: choose CPU or GPU for each device")
    device = raw.get("device")
    if resource == "gpu" and (
        not isinstance(device, str) or not device.strip() or len(device) > 256 or device != device.strip()
    ):
        raise ValueError(f"{name}: choose a GPU device")
    if resource == "cpu" and device not in (None, ""):
        raise ValueError(f"{name}: a CPU member cannot select a GPU device")
    label = "CPU" if resource == "cpu" else device
    key = "cpu" if resource == "cpu" else f"gpu:{device}"
    if key in seen_devices:
        raise ValueError(f"{name}: {label} appears twice; use one row per device")
    seen_devices.add(key)
    count = raw.get("count")
    limit = MAX_CPU_WORKERS if resource == "cpu" else MAX_GPU_WORKERS
    if type(count) is not int or not 1 <= count <= limit:
        raise ValueError(f"{name} ({label}): worker count must be between 1 and {limit}; remove the device to use zero")
    kinds = raw.get("job_types")
    if not isinstance(kinds, list) or not kinds or any(k not in JOB_KINDS for k in kinds):
        raise ValueError(f"{name} ({label}): select at least one supported job type")
    if resource == "gpu" and JOB_KIND_LOUDNESS in kinds:
        raise ValueError(f"{name} ({label}): Plex loudness requires CPU workers")
    return {
        "id": member_id,
        "resource": resource,
        "device": device if resource == "gpu" else None,
        "count": count,
        "job_types": [kind for kind in JOB_KINDS if kind in kinds],
    }


def _reject_conflicting_flat_fields(raw: dict, members: list, name: str) -> None:
    """Refuse a flat resource/device/count/job_types that disagrees with the group's only member.

    The flat fields are a deprecated echo of a single member, so an equal echo is accepted (GET then PUT) while a
    different value would be silently dropped. Multi-member groups have no flat echo and ignore stray fields.
    """
    if len(members) != 1 or not isinstance(members[0], dict):
        return
    member = members[0]
    for key in ("resource", "device", "count", "job_types"):
        if key not in raw:
            continue
        flat, own = raw[key], member.get(key)
        if key == "device":
            flat, own = flat or None, own or None
        if flat != own:
            raise ValueError(f"{name}: edit members[0] instead of the group's top-level {key}")


def _clean_members(raw: dict, name: str) -> list[dict]:
    if "members" not in raw:
        # Legacy flat group: the group's own device fields are its single member.
        if "resource" not in raw:
            raise ValueError(f"{name}: add at least one device")
        return [_clean_member(raw, name, set(), set(), legacy=True)]
    members = raw["members"]
    if not isinstance(members, list) or not members:
        raise ValueError(f"{name}: add at least one device")
    _reject_conflicting_flat_fields(raw, members, name)
    if len(members) > MAX_MEMBERS:
        raise ValueError(f"{name}: use at most {MAX_MEMBERS} devices")
    seen_ids: set[str] = set()
    seen_devices: set[str] = set()
    return [_clean_member(member, name, seen_ids, seen_devices, legacy=False) for member in members]


def _clean_availability(raw: dict, name: str) -> dict:
    availability = raw.get("availability", {"mode": "always", "windows": []})
    if not isinstance(availability, dict) or availability.get("mode") not in ("always", "scheduled"):
        raise ValueError(f"{name}: availability must be always or scheduled")
    windows = availability.get("windows", [])
    if not isinstance(windows, list) or len(windows) > 32:
        raise ValueError(f"{name}: use at most 32 availability windows")
    if availability["mode"] == "scheduled" and not windows:
        raise ValueError(f"{name}: add an availability window")
    clean_windows = []
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError(f"{name}: each window must be an object")
        days = window.get("days")
        if not isinstance(days, list) or not days or any(type(d) is not int or not 0 <= d <= 6 for d in days):
            raise ValueError(f"{name}: select valid start days for each window")
        start, end = window.get("start"), window.get("end")
        if any(not isinstance(t, str) or not _TIME.fullmatch(t) for t in (start, end)) or start == end:
            raise ValueError(f"{name}: use different start and end times in HH:MM format")
        clean_windows.append({"days": sorted(set(days)), "start": start, "end": end})
    return {"mode": availability["mode"], "windows": clean_windows}


def validate_worker_groups(value: object) -> list[dict[str, Any]]:
    """Normalize worker groups, rejecting malformed or overcommitted policies.

    Accepts the member shape and the legacy flat shape (one device per group, converted to one member ``m1``).
    When ``members`` is present, top-level ``resource``/``device``/``count``/``job_types`` must match the only member
    or be absent.

    Args:
        value: Complete proposed group list; an empty list intentionally disables capacity.

    Returns:
        An independent normalized list in the member shape, safe to persist.

    Raises:
        ValueError: A field is invalid, or overlapping groups exceed capacity limits.
    """
    if not isinstance(value, list) or len(value) > MAX_GROUPS:
        raise ValueError(f"Worker groups must be a list of at most {MAX_GROUPS} groups")
    groups: list[dict] = []
    ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Each worker group must be an object")
        group_id = raw.get("id")
        if not isinstance(group_id, str) or not _ID.fullmatch(group_id) or group_id in ids:
            raise ValueError("Each worker group needs a unique valid ID")
        ids.add(group_id)
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Group names must contain between 1 and 80 characters")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{name}: enabled must be true or false")
        members = _clean_members(raw, name)
        groups.append(
            {
                "id": group_id,
                "name": name.strip(),
                "enabled": enabled,
                "availability": _clean_availability(raw, name),
                "members": members,
            }
        )
    gpu, cpu = configured_group_totals(groups)
    if cpu > MAX_CPU_WORKERS or gpu > MAX_GPU_WORKERS:
        raise ValueError(
            f"Overlapping groups exceed capacity: peak CPU {cpu}/{MAX_CPU_WORKERS}, GPU {gpu}/{MAX_GPU_WORKERS}. "
            "Reduce counts or use different hours."
        )
    return groups


def groups_from_legacy(settings: dict, detected: list[dict] | None = None) -> list[dict]:
    """Preserve explicitly configured allocations; zero CPU remains zero.

    Args:
        settings: Settings holding ``cpu_threads`` and ``gpu_config``.
        detected: Detected GPUs (dicts with ``device`` and ``name``). Each one missing from ``gpu_config`` gets an
            enabled 1-worker group, as the pre-groups release used it.

    Returns:
        The validated worker groups.
    """
    groups = []

    def append(group_id: str, name: str, resource: str, count: int, device: str | None, enabled: bool) -> None:
        groups.append(
            {
                "id": group_id,
                "name": name,
                "enabled": enabled and count > 0,
                "availability": {"mode": "always", "windows": []},
                "members": [
                    {
                        "id": LEGACY_MEMBER_ID,
                        "resource": resource,
                        "device": device,
                        "count": max(1, count),
                        "job_types": [kind for kind in JOB_KINDS if resource == "cpu" or kind != JOB_KIND_LOUDNESS],
                    }
                ],
            }
        )

    gpu_budget = MAX_GPU_WORKERS

    def append_gpu(device: str, name: str, workers: int, enabled: bool) -> None:
        # The old release allowed 16 workers per GPU with no total, so five busy GPUs would fail the 64 total cap
        # and stop the app from starting. Later GPUs get what is left; one with nothing left is kept but disabled.
        nonlocal gpu_budget
        count = max(1, min(workers, MAX_GPU_WORKERS))
        enabled = enabled and workers > 0
        if enabled:
            if gpu_budget <= 0:
                enabled = False
            else:
                count = min(count, gpu_budget)
                gpu_budget -= count
        group_id = "legacy-gpu-" + hashlib.sha256(device.encode()).hexdigest()[:16]
        append(group_id, name[:80], "gpu", count, device, enabled)

    cpu = settings.get("cpu_threads", 1)
    cpu = int(cpu) if cpu not in (None, "") else 1
    if cpu > 0:
        append("legacy-cpu", "CPU workers", "cpu", cpu, None, True)
    seen: set[str] = set()
    for entry in settings.get("gpu_config") or []:
        if not isinstance(entry, dict) or not entry.get("device"):
            continue
        device = str(entry["device"])
        if device in seen:
            continue
        seen.add(device)
        workers = entry.get("workers", 1)
        workers = workers if isinstance(workers, int) and not isinstance(workers, bool) else 1
        append_gpu(device, str(entry.get("name") or device), workers, bool(entry.get("enabled", True)))
    for gpu in detected or []:
        device = str(gpu.get("device") or "")
        if not device or device in seen:
            continue
        seen.add(device)
        append_gpu(device, str(gpu.get("name") or device), 1, True)
    return validate_worker_groups(groups)


def effective_worker_groups(settings: dict) -> list[dict]:
    """Use groups when present, including [], and legacy counts only when absent."""
    if "worker_groups" in settings:
        return validate_worker_groups(settings["worker_groups"])
    return groups_from_legacy(copy.deepcopy(settings))


def future_capacity(groups: list[dict], quiet_hours: dict | None, kind: str) -> int:
    """Count configured capacity with reachable hours outside the global pause rule."""
    from .quiet_hours import quiet_hours_weekly_mask

    blocked = quiet_hours_weekly_mask(quiet_hours)
    return sum(
        policy["count"]
        for policy in member_policies(groups)
        if supports_job(policy, kind) and group_weekly_mask(policy) & ~blocked
    )
