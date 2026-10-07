"""Runner-side transactional handoff from a drained tracker to a durable job."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from loguru import logger

from .checkpoints import delete_checkpoint, write_checkpoint
from .group_runtime import capacity_wait_message, refresh_worker_groups


class JobParked(Exception):
    """Nonterminal control flow: teardown this pass, then wait before admitting it again."""


def _cleanup_checkpoint(manager, job_id: str, reference: str) -> None:
    """Cleanup never changes the outcome of an already committed transition."""
    try:
        delete_checkpoint(manager.config_dir, job_id, reference)
    except (OSError, ValueError):
        logger.warning("Could not remove an unused checkpoint of job {}; its active continuation is unchanged", job_id)


def park_if_unavailable(dispatcher, tracker, manager, job_id: str, kind: str, bookkeeping: Callable[[], dict]) -> None:
    """Drain, commit and detach if current policy provides no capacity for this job.

    A failed checkpoint keeps the tracker registered and admission held. A cancel
    is authoritative and never becomes a parked job.
    """
    if not isinstance(getattr(dispatcher.worker_pool, "_policies", None), dict):
        return
    refresh_worker_groups(dispatcher.worker_pool)
    capacity = dispatcher.worker_pool.capacity_for(kind)
    # Once drain begins finish it even if hours reopen. Re-registering from the
    # committed checkpoint is the only way to clear that completion fence.
    if capacity["open"] and not getattr(tracker, "park_requested", False):
        return
    dispatcher.request_park(job_id)
    snapshot = dispatcher.park_snapshot(job_id)
    if snapshot is None or manager.is_cancellation_requested(job_id):
        return
    job = manager.get_job(job_id)
    if job is None or job.status == "cancelled":
        dispatcher.cancel_job(job_id)
        return
    state = snapshot["state"]
    completed = state["successful"] + state["failed"]
    total = state["total_items"]
    manager.update_progress(
        job_id,
        processed_items=completed,
        total_items=total,
        percent=completed / total * 100 if total else 0,
        current_item=capacity_wait_message(capacity),
    )
    manager.set_job_outcome(job_id, state["outcome_counts"])
    manager.set_job_cpu_fallback_files(job_id, state["cpu_fallback_files"])
    manager.set_publishers(job_id, list(state["publishers_aggregate"].values()))
    previous = (job.config or {}).get("parked_checkpoint")
    reference = None
    try:
        reference = write_checkpoint(manager.config_dir, job_id, snapshot, bookkeeping=bookkeeping())
        if not manager.park_job(job_id, reference, capacity_wait_message(capacity), capacity.get("next_opening")):
            _cleanup_checkpoint(manager, job_id, reference)
            return
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        logger.warning("Could not safely park job {} ({}); keeping its admitted work", job_id, type(exc).__name__)
        if reference is not None:
            _cleanup_checkpoint(manager, job_id, reference)
        return
    if manager.is_cancellation_requested(job_id):
        manager.cancel_job(job_id)
        dispatcher.cancel_job(job_id)
        return
    if not dispatcher.detach_parked(job_id):
        # Cancellation may win after the durable commit. It remains terminal;
        # never reinterpret it as a failed continuation or restart its work.
        latest = manager.get_job(job_id)
        if latest is None or latest.status == "cancelled" or manager.is_cancellation_requested(job_id):
            return
        raise RuntimeError("Drained job could not detach after its checkpoint was committed")
    if previous and previous != reference:
        _cleanup_checkpoint(manager, job_id, previous)
    raise JobParked()
