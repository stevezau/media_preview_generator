"""Create Intro & Credits jobs from the API, webhook batches and schedules."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

from loguru import logger

from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..servers.base import ServerConfig
from ..servers.ownership import webhook_path_candidates
from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict
from ..web.jobs import PRIORITY_HIGH, PRIORITY_NORMAL, Job, get_job_manager
from ..web.settings_manager import get_settings_manager
from .job_runner import start_intro_credits_job_async
from .ownership import marker_matches
from .settings import load_server

# Serialises "is this file already queued?" with the job creation, so two webhooks for one import queue one job.
_follow_up_lock = threading.Lock()
# Same for Inspector re-detect, so a double-click queues one job.
_redetect_lock = threading.Lock()
_REDETECT_SOURCE = "inspector"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def markers_enabled_anywhere() -> bool:
    """Whether any enabled server has Intro & Credits turned on (Plex also needs the DB-write confirmation).

    Returns:
        True when at least one enabled server has Intro & Credits on.
    """
    for entry in get_settings_manager().get("media_servers") or []:
        if isinstance(entry, dict) and entry.get("enabled", True):
            if load_server(entry.get("markers"), str(entry.get("type") or "").lower()).enabled:
                return True
    return False


def _server_configs() -> list[ServerConfig]:
    configs = []
    for entry in get_settings_manager().get("media_servers") or []:
        if not isinstance(entry, dict):
            continue
        try:
            configs.append(server_config_from_dict(entry))
        except UnsupportedServerTypeError:
            continue
    return configs


def _local_candidates(path: str, configs: list[ServerConfig]) -> set[str]:
    # The candidates orchestrator._resolve_webhook_path_to_canonical tries, without its on-disk check.
    return set(webhook_path_candidates(path, configs))


def marker_owned_paths(paths: list[str]) -> list[str]:
    """Paths held by a library that Intro & Credits goes to on an enabled server.

    Runs on webhook threads, so it reads settings only: no disk or network access.

    Args:
        paths: File paths as the webhook sender reported them.

    Returns:
        The owned paths, in input order.
    """
    configs = _server_configs()
    return [
        path
        for path in paths
        if any(marker_matches(candidate, configs) for candidate in _local_candidates(path, configs))
    ]


def create_intro_credits_job(
    *,
    library_name: str,
    priority: int,
    source: str,
    libraries: list[dict] | None = None,
    file_paths: list[str] | None = None,
    follows_job_id: str | None = None,
    parent_schedule_id: str = "",
    force: bool = False,
    item_id_hints: dict[str, dict[str, str]] | None = None,
    retry_attempt: int = 0,
    retry_delay_s: int = 0,
    verify: bool = False,
) -> Job:
    """Create and start an Intro & Credits job.

    Args:
        library_name: Job title shown in the queue.
        priority: 1 high, 2 normal, 3 low.
        source: What created it (``manual``, ``schedule``, a webhook source).
        libraries: ``[{"server_id", "library_id"}]`` to enumerate; empty with no ``file_paths`` = every library
            that Intro & Credits goes to.
        file_paths: Explicit files or folders instead of libraries.
        follows_job_id: Preview job this job waits for before taking a slot.
        parent_schedule_id: Schedule that created it.
        force: Re-detect files already done.
        item_id_hints: ``{path: {server_id: item_id}}`` from vendor webhooks.
        retry_attempt: For a retry of files a server hadn't indexed yet or that weren't on disk yet: which retry this
            is (1-based).
        retry_delay_s: For a retry or a verify job: seconds to wait before it takes a slot.
        verify: A later check of files published after they were replaced (``job_runner._queue_verify``).

    Returns:
        The created job.
    """
    config = {
        "kind": JOB_KIND_INTRO_CREDITS,
        "source": source,
        "libraries": list(libraries or []),
        "file_paths": list(file_paths or []),
        "follows_job_id": follows_job_id,
        "force": bool(force),
        "webhook_item_id_hints": dict(item_id_hints or {}),
    }
    if verify:
        config["verify"] = True
    if retry_attempt:
        config["retry_attempt"] = int(retry_attempt)
    if retry_attempt or verify:
        # The due time (not just the delay) is stored so a job revived after a restart doesn't wait again in full.
        config["retry_delay"] = int(retry_delay_s)
        config["retry_not_before"] = (_utcnow() + timedelta(seconds=int(retry_delay_s))).isoformat()
    jm = get_job_manager()
    job = jm.create_job(
        library_name=library_name,
        config=config,
        priority=priority,
        kind=JOB_KIND_INTRO_CREDITS,
        parent_schedule_id=parent_schedule_id,
    )
    start_intro_credits_job_async(job.id)
    logger.info("Created Intro & Credits job {} ({}, source={})", job.id[:8], library_name, source)
    return job


def _queued_in_waiting_follow_ups(jm, configs: list[ServerConfig]) -> set[str]:
    # A follow-up that has never started reads its files when it runs, so a file already listed there is covered.
    # A revived one (PENDING again, started_at kept) may already have published the file's old version.
    queued: set[str] = set()
    for job in jm.get_pending_jobs():
        cfg = job.config or {}
        if job.kind != JOB_KIND_INTRO_CREDITS or job.started_at is not None:
            continue
        if not cfg.get("follows_job_id") or cfg.get("force"):
            continue
        for path in cfg.get("file_paths") or []:
            queued |= _local_candidates(str(path), configs)
    return queued


def submit_webhook_follow_up(
    *,
    preview_job_id: str,
    paths: list[str],
    source: str,
    item_id_hints: dict[str, dict[str, str]] | None = None,
) -> str | None:
    """Queue the Intro & Credits job that follows a webhook preview job (spec §6.4 item 9).

    Only files a server with Intro & Credits on holds are queued, and not files a waiting follow-up already lists.
    The job runs at NORMAL, or at the preview job's priority when that is lower; its runner waits for the preview job to
    finish.

    Args:
        preview_job_id: The preview job just started for the batch.
        paths: The batch's file paths.
        source: Webhook source (``sonarr``, ``plex``…).
        item_id_hints: ``{path: {server_id: item_id}}`` from vendor webhooks.

    Returns:
        The new job's id, or None when nothing needed queueing.
    """
    if not paths or not markers_enabled_anywhere():
        return None
    owned = marker_owned_paths(list(paths))
    if not owned:
        logger.debug("No server with Intro & Credits on holds the files of webhook job {}", preview_job_id)
        return None
    configs = _server_configs()
    jm = get_job_manager()
    with _follow_up_lock:
        queued = _queued_in_waiting_follow_ups(jm, configs)
        fresh = [p for p in owned if not (_local_candidates(p, configs) & queued)]
        if not fresh:
            logger.info("Intro & Credits for webhook job {}: its files are already queued", preview_job_id)
            return None
        preview = jm.get_job(preview_job_id)
        if len(fresh) == len(paths) and preview is not None and preview.library_name:
            name = f"Intro & Credits · {preview.library_name}"
        elif len(fresh) == 1:
            name = f"Intro & Credits · {os.path.basename(fresh[0])}"
        else:
            name = f"Intro & Credits · {len(fresh)} files"
        hints = {p: h for p, h in (item_id_hints or {}).items() if p in fresh}
        job = create_intro_credits_job(
            library_name=name,
            # Never ahead of the preview job it follows: with incoming jobs set to Low, previews still drain first.
            priority=max(PRIORITY_NORMAL, preview.priority) if preview is not None else PRIORITY_NORMAL,
            source=source,
            file_paths=fresh,
            follows_job_id=preview_job_id,
            item_id_hints=hints or None,
        )
    return job.id


def submit_redetect(path: str) -> str:
    """Queue an Inspector re-detect for one file: a forced single-file job at HIGH that asks every source again.

    While this file's previous re-detect is still queued or running, that job is returned instead: a second forced
    job would spend the online sources' budget twice and hold a HIGH slot that incoming webhook previews need.

    Args:
        path: The file's local path, already validated by the caller.

    Returns:
        The id of the new or the reused job.
    """
    jm = get_job_manager()
    with _redetect_lock:
        for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
            cfg = job.config or {}
            if (
                job.kind == JOB_KIND_INTRO_CREDITS
                and cfg.get("source") == _REDETECT_SOURCE
                and cfg.get("force")
                and list(cfg.get("file_paths") or []) == [path]
            ):
                logger.info("Re-detect for {} is already queued as job {}", os.path.basename(path), job.id[:8])
                return job.id
        job = create_intro_credits_job(
            library_name=f"Intro & Credits: {os.path.basename(path)}",
            priority=PRIORITY_HIGH,
            source=_REDETECT_SOURCE,
            file_paths=[path],
            force=True,
        )
    return job.id
