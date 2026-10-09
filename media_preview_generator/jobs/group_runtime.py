"""Bridge saved worker groups to the shared pool without stale per-job policy."""

from __future__ import annotations

import threading
import time
from datetime import datetime

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


def current_groups(config=None) -> list[dict] | None:
    """Prefer persisted live application policy to a job's stale config snapshot."""
    return current_group_policy(config)[0]


def capacity_for_groups(groups: list[dict], selected_gpus: list, kind: str, *, now: datetime | None = None) -> dict:
    """Distinguish policy, hardware and hours from ordinary busy worker slots."""
    from ..worker_groups import group_is_available, member_policies, next_group_opening, supports_job

    devices = {device for _, device, _ in selected_gpus}
    compatible = [g for g in member_policies(groups) if g["enabled"] and supports_job(g, kind)]
    detected = [g for g in compatible if g["resource"] == "cpu" or g.get("device") in devices]
    opened = [g for g in detected if group_is_available(g, now=now)]
    future = [next_group_opening(g, now=now) for g in detected]
    future = [value for value in future if value is not None]
    reason = "ready" if opened else "off_hours" if detected else "hardware" if compatible else "configuration"
    return {
        "configured": sum(g["count"] for g in compatible),
        "open": sum(g["count"] for g in opened),
        "available": 0,
        "busy": 0,
        "reason": reason,
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

    selected = _build_selected_gpus(settings) if any(p["resource"] == "gpu" for p in member_policies(groups)) else []
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
    once per second. Returns False for a legacy, ungrouped harness.
    """
    now = time.monotonic()
    if not force and config is None and selected_gpus is None and pool._policies is None:
        return False
    if not force and selected_gpus is None and now - getattr(pool, "_group_refresh_at", -10.0) < 1:
        return pool._policies is not None
    groups, revision = current_group_policy(config)
    if groups is None:
        return False
    pool.reconcile_groups(groups, selected_gpus, revision=revision)
    pool._group_refresh_at = now
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


def capacity_wait_message(capacity: dict) -> str:
    """Explain resource waiting separately from the start-up gate."""
    reason = capacity.get("reason")
    if reason == "hardware":
        return "Waiting for configured hardware — check the worker group's device"
    if reason == "off_hours":
        opening = capacity.get("next_opening")
        return f"Waiting for worker availability until {opening}" if opening else "Waiting for worker availability"
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
