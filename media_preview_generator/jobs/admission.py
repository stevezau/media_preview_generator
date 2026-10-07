"""Stable entry ordering for runnable jobs, without reserving worker slots."""

from __future__ import annotations

from datetime import UTC, datetime

from ..job_kinds import JOB_KIND_PREVIEWS


def preceding_job_ready(preceding) -> bool:
    """A follow-up may run after the parent's first pass, including a retry wait."""
    from ..web.jobs import JobStatus

    return (
        preceding is None
        or preceding.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
        or (preceding.status is JobStatus.PENDING and bool(preceding.progress.retry_eta))
    )


def _future(raw: object) -> bool:
    try:
        due = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return False
    if due is None:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=UTC)
    return due > datetime.now(UTC)


def _policy(manager, job_id: str, preflight_complete: bool) -> tuple[int, bool] | None:
    from ..web.jobs import JobStatus
    from ..web.settings_manager import get_settings_manager

    job = manager.get_job(job_id)
    if job is None or job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
        return None
    priority = job.priority
    cfg = job.config or {}
    if (
        cfg.get("is_retry_chain")
        or job.paused
        or manager.is_pause_requested(job_id)
        or manager.is_cancellation_requested(job_id)
        or get_settings_manager().processing_paused
    ):
        return priority, False
    if preflight_complete:
        return priority, True
    dependencies = [cfg.get("follows_job_id"), *(cfg.get("follows_job_ids") or [])]
    if any(parent and not preceding_job_ready(manager.get_job(parent)) for parent in dependencies):
        return priority, False
    if not cfg.get("force_fire_now"):
        deadline = cfg.get("retry_not_before")
        if job.kind == JOB_KIND_PREVIEWS and cfg.get("is_retry"):
            deadline = cfg.get("scheduled_at")
            if not deadline and not cfg.get("parked_checkpoint"):
                return priority, False
        if _future(deadline) or _future(job.progress.retry_eta) or _future(cfg.get("webhook_fire_at")):
            return priority, False
    return priority, True


def prepare_admissions(manager, jobs) -> None:
    """Register the whole recovered batch before any of its threads can race."""
    from ..web.job_gate import get_job_gate
    from ..web.jobs import JobStatus

    gate = get_job_gate()
    for job in jobs:
        if job.status not in (JobStatus.PENDING, JobStatus.RUNNING) or (job.config or {}).get("is_retry_chain"):
            continue
        gate.register_request(
            job.id,
            created_at=job.created_at or "",
            priority=job.priority,
            kind=job.kind,
            policy=lambda completed, job_id=job.id: _policy(manager, job_id, completed),
        )


def claim_admission(manager, job_id: str, *, job=None) -> object | None:
    """Called only by an async runner that already owns the in-flight claim."""
    from ..web.job_gate import get_job_gate

    if job is None:
        job = manager.get_job(job_id)
    if job is not None:
        prepare_admissions(manager, [job])
    return get_job_gate().claim_request(job_id)


def finish_admission(job_id: str, owner: object | None) -> None:
    from ..web.job_gate import get_job_gate

    get_job_gate().finish_request(job_id, owner)
