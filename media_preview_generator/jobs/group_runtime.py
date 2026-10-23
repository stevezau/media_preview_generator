"""Bridge saved worker groups to the shared pool without stale per-job policy."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from .node_members import NodeHardware

_capacity_changed = threading.Event()


def current_group_policy(config=None) -> tuple[list[dict] | None, int | None]:
    """Read saved groups and their revision together; legacy callers return None."""
    from contextlib import nullcontext

    from ..web.settings_manager import peek_settings_manager

    settings = peek_settings_manager()
    if settings is not None:
        locked = getattr(settings, "locked", None)
        with locked() if callable(locked) else nullcontext():
            groups = settings.get("worker_groups")
            if isinstance(groups, list):
                revision = getattr(settings, "worker_groups_revision", 0)
                return groups, revision if type(revision) is int else 0
    groups = getattr(config, "worker_groups", None)
    return (groups, None) if isinstance(groups, list) else (None, None)


def current_kind_ranks() -> dict[str, int]:
    """The saved ``job_kind_priority``, read live each dispatch pass; every kind ranks 1 without a settings manager."""
    from ..job_kinds import safe_kind_ranks
    from ..web.settings_manager import peek_settings_manager

    settings = peek_settings_manager()
    return safe_kind_ranks(settings.get("job_kind_priority") if settings is not None else None)


def current_groups(config=None) -> list[dict] | None:
    """Prefer persisted live application policy to a job's stale config snapshot."""
    return current_group_policy(config)[0]


def capacity_for_groups(
    groups: list[dict],
    selected_gpus: list,
    kind: str,
    *,
    now: datetime | None = None,
    nodes: NodeHardware | None = None,
) -> dict:
    """Distinguish policy, hardware, nodes and hours from ordinary busy worker slots.

    Args:
        groups: Saved groups or member policies.
        selected_gpus: This app's usable GPUs as ``(type, device, info)``.
        kind: Job kind.
        now: Local clock override.
        nodes: The node registry snapshot to judge node members by; read fresh when omitted.

    Returns:
        ``configured``, ``open``, ``available``, ``busy``, ``reason`` (ready, off_hours, hardware, node_unavailable,
        configuration), ``detail`` (the node and its reason when every compatible member is on an unavailable node,
        else ``""``) and ``next_opening``.
    """
    from ..worker_groups import group_is_available, member_policies, next_group_opening, supports_job
    from .node_members import member_detected, read_node_hardware

    devices = {device for _, device, _ in selected_gpus}
    compatible = [g for g in member_policies(groups) if g["enabled"] and supports_job(g, kind)]
    if nodes is None:
        nodes = read_node_hardware(compatible)
    detected = [g for g in compatible if member_detected(g, devices, nodes)]
    opened = [g for g in detected if group_is_available(g, now=now)]
    openings = [next_group_opening(g, now=now) for g in detected]
    future = [value for value in openings if value is not None]
    reason = "ready" if opened else "off_hours" if detected else "hardware" if compatible else "configuration"
    detail = ""
    if reason == "hardware" and all(g.get("node") for g in compatible):
        reason = "node_unavailable"
        detail = "; ".join(
            dict.fromkeys(
                f"{hardware.node_name}: {hardware.reason}" for hardware in (nodes.member(g) for g in compatible)
            )
        )
        # Only jobs ask for capacity, so a job waits on these nodes alone: check them soon, not after a back-off.
        from ..nodes.registry import note_work_waiting

        try:
            note_work_waiting({g["node"] for g in compatible})
        except Exception as exc:  # noqa: BLE001 - only brings a check forward; the capacity answer stands
            logger.debug("Couldn't bring the next check of nodes forward: {}", exc)
    return {
        "configured": sum(g["count"] for g in compatible),
        "open": sum(g["count"] for g in opened),
        "available": 0,
        "busy": 0,
        "reason": reason,
        "detail": detail,
        "next_opening": min(future).isoformat() if future else None,
    }


def runtime_capacity(kind: str, *, now: datetime | None = None) -> dict:
    """Read capacity before admission/enumeration without constructing a worker pool."""
    from ..web.settings_manager import peek_settings_manager

    settings = peek_settings_manager()
    groups = current_groups()
    if groups is None:
        # Application startup migrates groups before accepting jobs. Old
        # embedded callers without a SettingsManager retain their prior path.
        return {"configured": 1, "open": 1, "available": 0, "busy": 0, "reason": "ready", "next_opening": None}
    from ..web.routes.job_runner import _build_selected_gpus
    from ..worker_groups import member_policies

    local_gpu = any(p["resource"] == "gpu" and not p.get("node") for p in member_policies(groups))
    selected = _build_selected_gpus(settings) if local_gpu else []
    return capacity_for_groups(groups, selected, kind, now=now)


def admission_capacity(kind: str) -> int | None:
    """Open compatible workers for the kind: none means its jobs wait at the start-up gate."""
    if current_groups() is None:
        return None
    return runtime_capacity(kind)["open"]


def mark_admission_wait(manager, job_id: str, kind: str) -> None:
    """Explain worker-capacity admission separately from the start-up slot limit."""
    label = {"loudness": "loudness", "intro_credits": "Intro & Credits", "previews": "preview"}.get(kind, kind)
    message = f"Queued — waiting for {label} worker capacity"
    manager.update_progress(job_id, current_item=message)
    manager.note_slot_wait(job_id)
    job = manager.get_job(job_id)
    parent_id = (job.config or {}).get("parent_job_id") if job else None
    if parent_id:
        manager.update_progress(parent_id, current_item=message)


def admission_options(manager, job_id: str, kind: str | None) -> dict:
    """Carry a runner's kind and visible resource wait into the start-up gate."""
    if kind is None:
        return {}
    options = {"kind": kind, "on_resource_wait": lambda: mark_admission_wait(manager, job_id, kind)}
    from ..web.job_gate import get_job_gate

    gate = get_job_gate()
    if gate.has_request(job_id):
        gate.complete_preflight(job_id)
        options["request_id"] = job_id
    return options


def refresh_worker_groups(pool, config=None, selected_gpus: list | None = None, *, force: bool = False) -> bool:
    """Reconcile current policy and time; periodic calls do no hardware discovery.

    Settings saves and initializers supply a fresh detected selection. The
    dispatch loop only reevaluates cached hardware and current policy, at most
    once per second, unless the node registry changed since the last refresh
    (a node going offline or coming back) while a member uses a node. Returns
    False for a legacy, ungrouped harness.
    """
    from .node_members import registry_revision

    now = time.monotonic()
    if not force and config is None and selected_gpus is None and pool._policies is None:
        return False
    uses_nodes = any(policy.get("node") for policy in (getattr(pool, "_policies", None) or {}).values())
    # Only read the registry when a member uses a node: its revision syncs from settings, and this runs on every
    # dispatch pass.
    node_revision = registry_revision() if uses_nodes else None
    nodes_changed = uses_nodes and node_revision != getattr(pool, "_node_revision", None)
    if (
        not force
        and selected_gpus is None
        and not nodes_changed
        and now - getattr(pool, "_group_refresh_at", -10.0) < 1
    ):
        return pool._policies is not None
    groups, revision = current_group_policy(config)
    if groups is None:
        return False
    pool.reconcile_groups(groups, selected_gpus, revision=revision)
    pool._group_refresh_at = now
    if node_revision is None:
        # Policies that just gained a node member: the revision the reconcile read the registry at.
        node_revision = getattr(getattr(pool, "_node_hardware", None), "revision", None)
    pool._node_revision = node_revision
    return True


def wake_group_runtime() -> None:
    """Wake the existing dispatcher after policy changes; never initialize one."""
    from .dispatcher import get_dispatcher

    _capacity_changed.set()
    dispatcher = get_dispatcher()
    if dispatcher is not None:
        dispatcher.worker_pool._group_refresh_at = -10.0
        dispatcher._has_work.set()
        dispatcher._worker_done.set()


def _opening_text(opening: object) -> str:
    """When a worker group opens, in the app's time zone: ``03:00`` within a day, ``Fri 03:00`` within a week.

    The message is written once, not redrawn, so "within a day" (not "today") keeps a bare time unambiguous.
    """
    from ..worker_groups import application_timezone, local_now

    try:
        when = datetime.fromisoformat(str(opening))
    except ValueError:
        return str(opening)
    zone = application_timezone()
    when = (when if when.tzinfo is not None else when.replace(tzinfo=zone)).astimezone(zone)
    ahead = when - local_now()
    if ahead < timedelta(days=1):
        return when.strftime("%H:%M")
    if ahead < timedelta(days=7):
        return when.strftime("%a %H:%M")
    return when.strftime("%Y-%m-%d %H:%M")


def capacity_wait_message(capacity: dict) -> str:
    """Explain resource waiting separately from the start-up gate."""
    reason = capacity.get("reason")
    if reason == "hardware":
        return "Waiting for configured hardware — check the worker group's device"
    if reason == "node_unavailable":
        detail = capacity.get("detail")
        return f"Waiting for a node — {detail}" if detail else "Waiting for a node to come back"
    if reason == "off_hours":
        opening = capacity.get("next_opening")
        if not opening:
            return "Waiting for worker availability"
        return f"Waiting for worker availability until {_opening_text(opening)}"
    return "Waiting for worker configuration — add or enable a compatible worker group"


def wait_for_capacity(manager, job_id: str, kind: str, cancel_check, pause_check=None) -> bool:
    """Wait before admission without creating workers, retry attempts or large queues."""
    last = None
    while not cancel_check():
        if pause_check is None or not pause_check():
            capacity = runtime_capacity(kind)
            if capacity["open"]:
                return True
            reason = capacity_wait_message(capacity)
            state = (reason, capacity.get("next_opening"))
            if state != last:
                manager.mark_resource_wait(job_id, reason, next_eligible=capacity.get("next_opening"))
                last = state
        _capacity_changed.wait(timeout=1.0)
        _capacity_changed.clear()
    return False
