"""Intro & Credits job thread: gate → enumerate/resolve files → shared dispatcher with marker handlers → complete."""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone

from loguru import logger

from ..config import load_config
from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..jobs.dispatcher import get_or_create_dispatcher
from ..jobs.orchestrator import _build_multi_server_registry
from ..jobs.worker import is_job_thread_for, register_job_thread, unregister_job_thread
from ..processing.generator import clear_failures, failure_scope, set_file_result_callback
from ..processing.retry_queue import BACKOFF_SCHEDULE, retry_policy
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig
from ..web.job_gate import format_wait_message, get_job_gate
from ..web.jobs import JobStatus, WorkerStatus, get_job_manager
from ..web.routes.job_runner import _build_selected_gpus, _format_eta, _inflight_jobs, _inflight_lock
from ..web.settings_manager import get_settings_manager
from .outcomes import (
    NOT_IN_LIBRARY,
    PLEX_PASS_UNKNOWN,
    READ_BACK_FAILED,
    RETRY_REASON_CODES,
    VERIFY_LATER,
    FileOutcome,
    ServerStatus,
)
from .ownership import marker_libraries
from .pipeline import budget_exhausted_warnings, build_context, kind_handlers
from .settings import load_server

_POLL_S = 1.0
# Polls a PENDING preview job may go without a thread before its follow-up stops waiting for it. Covers the moment
# between two starts of the pending drain or the restart requeue; a job not revived after a restart never gets one.
_ORPHAN_GRACE_POLLS = 30
# Largest retry (or verify) job; a bigger backlog of unindexed files (a new library) waits for the next run instead.
MAX_RETRY_FILES = 500
# A replaced file is checked again this long after its publish at the least (servers rescan it after the job).
MIN_VERIFY_DELAY_S = 600
VERIFY_DELAY_FACTOR = 3
_FINISHED = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
# Job sources where the user chose the files (API/Start job dialog, Inspector re-detect, Season view Publish).
_USER_PICKED_SOURCES = frozenset({"manual", "inspector", "inspector_season"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def retry_reason(row: object) -> str | None:
    """Why a per-server row's file is worth trying again later, if it is.

    Args:
        row: One of the pipeline's per-server rows.

    Returns:
        The reason code of a waiting row the job retries (the server hasn't indexed the file yet, or Plex didn't answer
        its Plex Pass check); None for any other row.
    """
    if not isinstance(row, dict) or row.get("status") != ServerStatus.WAITING.value:
        return None
    code = row.get("reason_code")
    return code if code in RETRY_REASON_CODES else None


def retry_delay_s(attempt: int, retry_delay: int) -> int:
    """Delay before retry ``attempt`` (1-based): the preview retries' backoff, scaled by ``webhook_retry_delay``.

    Args:
        attempt: Which retry this is.
        retry_delay: The ``webhook_retry_delay`` setting (seconds, 30 = the schedule as is).

    Returns:
        Seconds to wait.
    """
    scale = max(0.5, retry_delay / 30.0)
    return max(1, int(BACKOFF_SCHEDULE[min(attempt - 1, len(BACKOFF_SCHEDULE) - 1)] * scale))


NOT_ON_DISK = "not_on_disk"
# Retry log wording per reason, in the order a combined line lists them.
_RETRY_WORDS = {
    NOT_ON_DISK: "not on disk",
    NOT_IN_LIBRARY: "not in a server's library",
    PLEX_PASS_UNKNOWN: "not checked on Plex",
}


def _retry_reason(waiting: dict[str, set[str]]) -> str:
    words = [text for reason, text in _RETRY_WORDS.items() if waiting.get(reason)]
    listed = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"
    return f"{listed} yet"


def _queue_retry(job, cfg: dict, waiting: dict[str, set[str]], sender_paths: dict[str, str]) -> None:
    """Create the delayed retry job for files that weren't on disk yet or that a server could take later.

    Up to ``webhook_retry_count`` retries, one job for every reason; a verify job's retries go on counting from the
    retries its chain used before it, and never queue another verify. The retry gets each file as its sender gave it
    (with that path's item id hints), like the preview retries: a file not on disk yet was given the first mapped
    disk's path, and only the sender's path is resolved again against every disk. Never raises: the job that found
    the files has already completed.

    Args:
        job: The job that found the files.
        cfg: Its config.
        waiting: Local paths per reason (``NOT_ON_DISK`` or a row's retry reason code).
        sender_paths: The path each local path was given as (``build_items``); a missing entry is retried as is.
    """
    jm = get_job_manager()
    paths = _sent_paths({path for files in waiting.values() for path in files}, sender_paths)
    reason = _retry_reason(waiting)
    try:
        attempt = int(cfg.get("retry_attempt") or cfg.get("chain_attempt") or 0) + 1
        count, delay_setting = retry_policy(get_settings_manager())
        if attempt > count:
            jm.add_log(
                job.id,
                f"WARNING - {len(paths)} file(s) still {reason.removesuffix(' yet')} after {count} retr"
                f"{'y' if count == 1 else 'ies'}; the next run for these files will try again",
            )
            return
        from .triggers import create_intro_credits_job

        if len(paths) > MAX_RETRY_FILES:
            jm.add_log(
                job.id,
                f"INFO - {len(paths) - MAX_RETRY_FILES} more files {reason} will be tried on the next run",
            )
            paths = paths[:MAX_RETRY_FILES]
        delay = retry_delay_s(attempt, delay_setting)
        base_name = (job.library_name or "Intro & Credits").removeprefix("Retry: ").removeprefix("Verify: ")
        retry = create_intro_credits_job(
            library_name=f"Retry: {base_name}",
            priority=job.priority,
            source=str(cfg.get("source") or "retry"),
            file_paths=paths,
            item_id_hints=_hints_for(cfg, paths) or None,
            retry_attempt=attempt,
            retry_delay_s=delay,
            verify_chain=bool(cfg.get("verify") or cfg.get("verify_chain")),
        )
        jm.add_log(
            job.id,
            f"INFO - {len(paths)} file(s) {reason}; retry {attempt} of {count} in {delay}s (job {retry.id[:8]})",
        )
    except Exception:
        logger.exception("Could not queue the retry for files job {} found waiting", job.id)


def _sent_paths(files: set[str], sender_paths: dict[str, str]) -> list[str]:
    """Local paths as their senders gave them, sorted: what a later job for these files is created with."""
    return sorted({sender_paths.get(path, path) for path in files})


def _hints_for(cfg: dict, paths: list[str]) -> dict[str, dict[str, str]]:
    sent_hints = cfg.get("webhook_item_id_hints") or {}
    return {path: sent_hints[path] for path in paths if sent_hints.get(path)}


def _queue_verify(job, cfg: dict, files: set[str], sender_paths: dict[str, str]) -> None:
    """Create the one delayed check of files this job published after they were replaced.

    Servers rescan a replaced file after the job and can drop or replace our markers then; the check reads them back
    and writes them again if so. It waits the first retry delay three times over (at least 10 minutes), follows the
    retry settings (none when retries are off) and cap, and never queues another check. Never raises.

    Args:
        job: The job that published the files.
        cfg: Its config.
        files: Local paths of the replaced files.
        sender_paths: The path each local path was given as (``build_items``).
    """
    jm = get_job_manager()
    try:
        count, delay_setting = retry_policy(get_settings_manager())
        if count < 1:
            return
        from .triggers import create_intro_credits_job

        sent = _sent_paths(files, sender_paths)
        paths = sent[:MAX_RETRY_FILES]
        if len(sent) > len(paths):
            jm.add_log(
                job.id,
                f"INFO - {len(sent) - len(paths)} more replaced file(s) aren't checked again later; the next run for "
                "them checks them",
            )
        delay = max(MIN_VERIFY_DELAY_S, retry_delay_s(1, delay_setting) * VERIFY_DELAY_FACTOR)
        base_name = (job.library_name or "Intro & Credits").removeprefix("Retry: ").removeprefix("Verify: ")
        check = create_intro_credits_job(
            library_name=f"Verify: {base_name}",
            priority=job.priority,
            source=str(cfg.get("source") or "verify"),
            file_paths=paths,
            item_id_hints=_hints_for(cfg, paths) or None,
            retry_delay_s=delay,
            verify=True,
            chain_attempt=int(cfg.get("retry_attempt") or 0),
        )
        jm.add_log(job.id, f"INFO - {len(paths)} replaced file(s) are checked again in {delay}s (job {check.id[:8]})")
    except Exception:
        logger.exception("Could not queue the later check of the replaced files job {} published", job.id)


def _wait_for_retry_time(job_id: str, cfg: dict, cancel_check: Callable[[], bool]) -> bool:
    """Hold a retry job until it is due. Runs before the gate, so waiting costs no slot.

    Returns:
        False if the job was cancelled while waiting.
    """
    raw = cfg.get("retry_not_before")
    try:
        due = datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        due = None
    if due is None or _utcnow() >= due:
        return True
    jm = get_job_manager()
    remaining = int((due - _utcnow()).total_seconds())
    if cfg.get("verify"):
        waiting_for = (
            f"Check starting in {remaining}s — servers often rescan a replaced file after its markers are sent"
        )
    else:
        waiting_for = f"Retry starting in {remaining}s — waiting for these files to appear on disk or on a server"
    jm.update_progress(
        job_id,
        percent=0,
        processed_items=0,
        total_items=0,
        current_item=waiting_for,
        retry_eta=raw,
        retry_wait_total=int(cfg.get("retry_delay") or remaining),
    )
    while _utcnow() < due:
        if cancel_check():
            return False
        time.sleep(_POLL_S)
    jm.update_progress(job_id, retry_eta=None)
    return True


def _all_libraries_listed(cfg: ServerConfig) -> ServerConfig:
    # Vendor enumeration skips libraries with previews turned off; markers have their own library selection.
    return dataclasses.replace(cfg, libraries=[dataclasses.replace(lib, enabled=True) for lib in cfg.libraries])


def build_items(
    job_config: dict,
    *,
    registry,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> tuple[list[ProcessableItem], list[str], dict[str, str]]:
    """Files for a job: explicit paths (webhook, manual, Inspector) or library enumeration.

    Args:
        job_config: The job's config (``file_paths`` + ``webhook_item_id_hints``, or ``libraries``).
        registry: The job's ``ServerRegistry``.
        cancel_check: True once the job is cancelled (stops enumeration).
        progress_callback: ``(current, total, message)`` for the enumeration banner.

    Returns:
        Items sorted by season folder then path (a season's episodes run together), warnings for the job, and for
        explicit paths the path each item was given as (a sender's view, before path mapping); empty for a listing.
    """
    from ..jobs import orchestrator
    from ..plex_client import _expand_directory_to_media_files

    warnings: list[str] = []
    items: list[ProcessableItem] = []
    sender_paths: dict[str, str] = {}
    file_paths = [str(p).strip() for p in (job_config.get("file_paths") or []) if str(p).strip()]
    if file_paths:
        # Intro & Credits ownership ignores the preview opt-in (markers/ownership.py), so the local path is picked among
        # every library; with preview-enabled libraries only, a markers-only library's sender path would stay unmapped.
        configs = [_all_libraries_listed(cfg) for cfg in registry.configs()]
        mappings = [m for cfg in configs for m in (cfg.path_mappings or [])]
        hints = job_config.get("webhook_item_id_hints") or {}
        for raw in _expand_directory_to_media_files(file_paths, mappings):
            canonical, _owners = orchestrator._resolve_webhook_path_to_canonical(raw, configs, log_resolution=False)
            sender_paths.setdefault(canonical, raw)
            items.append(
                ProcessableItem(
                    canonical_path=canonical,
                    server_id="",
                    item_id_by_server=dict(hints.get(raw) or {}),
                    title=os.path.basename(canonical),
                )
            )
    else:
        requested: dict[str, list[str]] = {}
        for entry in job_config.get("libraries") or []:
            requested.setdefault(str(entry["server_id"]), []).append(str(entry["library_id"]))
        library_ids: dict[str, list[str]] = {}
        candidates: list[ServerConfig] = []
        known: set[str] = set()
        for cfg in registry.configs():
            known.add(cfg.id)
            if requested and cfg.id not in requested:
                continue
            name = cfg.name or cfg.id
            if not cfg.enabled:
                if requested:
                    warnings.append(f"Skipped {name}: {name} is disabled")
                continue
            if not load_server(cfg.markers, cfg.type.value).enabled:
                if requested:
                    warnings.append(f"Skipped {name}: Intro & Credits is turned off on {name}")
                continue
            allowed = [lib.id for lib in marker_libraries(cfg)]
            if cfg.id in requested:
                names = {lib.id: lib.name for lib in cfg.libraries}
                ids = [lid for lid in requested[cfg.id] if lid in allowed]
                warnings.extend(
                    f"Skipped {names.get(lid) or lid}: Intro & Credits isn't on for it"
                    for lid in requested[cfg.id]
                    if lid not in allowed
                )
            else:
                ids = allowed
            if not ids:
                continue
            library_ids[cfg.id] = ids
            candidates.append(_all_libraries_listed(cfg))
        warnings.extend(f"Skipped server {sid}: {sid} is no longer configured" for sid in requested if sid not in known)
        pairs, errors = orchestrator._enumerate_items_for_servers(
            candidates,
            enumerate_one=lambda processor, cfg: processor.list_canonical_paths(
                cfg,
                library_ids=library_ids[cfg.id],
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            ),
            cancel_check=cancel_check,
            label="Intro & Credits",
            progress_callback=progress_callback,
        )
        items = [item for _cfg, item in pairs]
        warnings.extend(f"Couldn't list {name}: {err}" for name, err in errors)
    unique: dict[str, ProcessableItem] = {}
    for item in sorted(items, key=lambda i: (os.path.dirname(i.canonical_path), i.canonical_path)):
        unique.setdefault(item.canonical_path, item)
    return list(unique.values()), warnings, sender_paths


def _in_flight(job_id: str) -> bool:
    with _inflight_lock:
        return job_id in _inflight_jobs


def _wait_for_preceding_job(job_id: str, follows_job_id: str | None, cancel_check: Callable[[], bool]) -> bool:
    """Hold a webhook follow-up until its preview job has finished.

    Priority alone can't order them: users can set incoming preview jobs to Normal or Low, and then this job
    (which does less work before the gate) would take the slot first and run before the previews for the same
    files. Runs before the gate, so waiting costs no slot. A preview job counting down to a retry
    (PENDING with ``progress.retry_eta``) counts as finished: its backoff can run for over an hour; files a server
    hasn't indexed yet, or not on disk yet, get their own retry job (``_queue_retry``). A PENDING preview job with no
    thread to run it (too old to be revived after a restart) counts as finished after ``_ORPHAN_GRACE_POLLS``; while
    processing is paused it doesn't, as resuming starts it.

    Returns:
        False if the job was cancelled while waiting.
    """
    if not follows_job_id:
        return True
    jm = get_job_manager()
    announced = False
    polls_without_thread = 0
    while True:
        if cancel_check():
            return False
        preceding = jm.get_job(follows_job_id)
        if preceding is None or preceding.status in _FINISHED:
            return True
        progress = getattr(preceding, "progress", None)
        if preceding.status is JobStatus.PENDING and progress is not None and progress.retry_eta:
            return True
        if (
            preceding.status is JobStatus.PENDING
            and not get_settings_manager().processing_paused
            and not _in_flight(follows_job_id)
        ):
            polls_without_thread += 1
            if polls_without_thread > _ORPHAN_GRACE_POLLS:
                jm.add_log(job_id, "INFO - The preview job for these files isn't queued to run; starting without it")
                return True
        else:
            polls_without_thread = 0
        if not announced:
            jm.update_progress(
                job_id,
                percent=0,
                processed_items=0,
                total_items=0,
                current_item="Queued — waiting for the preview job for these files to finish",
            )
            announced = True
        time.sleep(_POLL_S)


def _hold_pause_from_before_restart(job_id: str, cancel_check: Callable[[], bool]) -> bool:
    """Keep a job the user paused before a restart paused, holding no slot, until they resume it.

    The pause and resume routes only act on running jobs, so the job is marked running and paused again.

    Returns:
        False if the job was cancelled while paused.
    """
    jm = get_job_manager()
    jm.start_job(job_id)
    jm.request_pause(job_id)
    jm.add_log(job_id, "INFO - Still paused from before the restart; resume the job to continue")
    jm.update_progress(job_id, current_item="Paused — resume this job to continue")
    while jm.is_pause_requested(job_id):
        if cancel_check():
            return False
        time.sleep(_POLL_S)
    return not cancel_check()


# Outcomes a restart can't change. Waiting, skipped, file-not-found and failed files run again: the server may have
# indexed the item meanwhile, a plugin may have been installed, a stale mount often comes back with the restart.
_SETTLED_OUTCOMES = frozenset(
    {
        FileOutcome.PUBLISHED.value,
        FileOutcome.UP_TO_DATE.value,
        FileOutcome.NEEDS_REVIEW.value,
        FileOutcome.NO_MARKERS.value,
        FileOutcome.NO_OWNERS.value,
    }
)


def _unchanged_since_analysed(store, path: str) -> bool:
    record = store.get_file(path)
    if record is None:
        return False
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (st.st_size, st.st_mtime_ns) == (record.size, record.mtime_ns)


def _skip_finished_before_restart(
    jm, job_id: str, items: list[ProcessableItem], store
) -> tuple[list[ProcessableItem], dict[str, int]]:
    """Drop files this job settled before a restart revived it, so they aren't looked up or published twice.

    Their outcomes come from the job's own Files-panel rows (only a revived job has any) and are carried into the
    job's counts. A file replaced since it was analysed (size or mtime differ from the markers store), or with a
    server still waiting for it, runs again.

    Returns:
        The items still to do, and the carried outcome counts.
    """
    try:
        rows = jm.get_file_results(job_id)
    except Exception as exc:
        logger.warning("Couldn't read the files this job already finished ({}); checking every file", exc)
        return items, {}
    settled = {
        row.get("file"): row.get("outcome")
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        and row.get("file")
        and row.get("outcome") in _SETTLED_OUTCOMES
        # A file published to one server while another hadn't indexed it yet still needs its retry.
        and not any(
            isinstance(server, dict) and server.get("status") == ServerStatus.WAITING.value
            for server in row.get("servers") or []
        )
    }
    if not settled:
        return items, {}
    carried: Counter[str] = Counter()
    remaining = []
    for item in items:
        outcome = settled.get(item.canonical_path)
        if outcome is not None and _unchanged_since_analysed(store, item.canonical_path):
            carried[outcome] += 1
        else:
            remaining.append(item)
    if carried:
        logger.info(
            "Resuming after a restart: {} file(s) finished before it are not checked again", sum(carried.values())
        )
    return remaining, dict(carried)


def _complete(jm, job_id: str, outcome: dict[str, int], warnings: list[str]) -> None:
    """Complete the job: failed (red) when every counted file failed, a warning (amber) when some did or on warnings."""
    failed = outcome.get(FileOutcome.FAILED.value, 0)
    succeeded = sum(count for key, count in outcome.items() if key != FileOutcome.FAILED.value)
    if failed and not succeeded:
        jm.complete_job(job_id, error=" | ".join([f"All {failed} file(s) failed — see the Files panel", *warnings]))
        return
    parts = ([f"{failed} file(s) failed"] if failed else []) + warnings
    jm.complete_job(job_id, warning=" | ".join(parts) or None)


def _wait_releasing_slot_while_paused(
    tracker,
    *,
    job_id: str,
    slot: dict,
    live_priority: Callable[[], int],
    cancel_check: Callable[[], bool],
    on_wait: Callable[[int, int, int], None],
) -> None:
    """Wait for the tracker; while this job is paused on its own, give its gate slot back.

    A paused job does no work, so holding its slot would block every other job — at max_concurrent_jobs=1 a
    paused backfill would stop all webhook preview jobs. The tracker's pause_check also reads ``slot["held"]``,
    so no item is dispatched between resume and re-admission.
    """
    jm = get_job_manager()
    gate = get_job_gate()
    while not tracker.wait(timeout=_POLL_S):
        if cancel_check():
            continue
        paused = jm.is_pause_requested(job_id)
        if paused and slot["held"]:
            gate.release(slot["priority"])
            slot["held"] = False
            jm.add_log(job_id, "INFO - Paused; active slot handed back until resume")
        elif not paused and not slot["held"]:
            priority = live_priority()
            # In-flight items can finish the job while its slot is handed back; don't queue a finished job.
            if gate.acquire(
                priority=priority,
                cancel_check=lambda: cancel_check() or jm.is_pause_requested(job_id) or tracker.done_event.is_set(),
                on_wait=on_wait,
            ):
                slot["priority"] = priority
                slot["held"] = True


def run_intro_credits_job(job_id: str) -> None:
    """Run one Intro & Credits job to completion (called on its own thread).

    Args:
        job_id: The job to run.
    """
    from loguru import logger as loguru_logger

    jm = get_job_manager()
    job = jm.get_job(job_id)
    if job is None:
        return
    settings = get_settings_manager()
    if settings.processing_paused:
        logger.info("Intro & Credits job {} not started — processing is paused; job stays pending", job_id)
        return
    register_job_thread(job_id)
    handler_id = loguru_logger.add(
        lambda message: jm.add_log(job_id, f"{message.record['level'].name} - {message.record['message']}"),
        level=str(settings.get("log_level", "INFO")).upper(),
        format="{message}",
        filter=lambda record: is_job_thread_for(record["thread"].id, job_id),
        enqueue=True,
    )
    # "priority" is the value the slot was admitted at: the user can re-prioritise the job, and release() must
    # settle at the admitted value.
    slot = {"held": False, "priority": job.priority}
    cfg = dict(job.config or {})
    dispatcher = None

    def cancel_check() -> bool:
        return jm.is_cancellation_requested(job_id)

    def live_priority() -> int:
        # ``job`` is the job manager's own object, which the priority route updates in place.
        return job.priority

    def on_wait(active: int, cap: int, effective_cap: int) -> None:
        jm.update_progress(
            job_id,
            percent=0,
            processed_items=0,
            total_items=0,
            current_item=format_wait_message(active, cap, effective_cap),
        )

    try:
        with failure_scope(job_id):
            try:
                if not _wait_for_preceding_job(job_id, cfg.get("follows_job_id"), cancel_check):
                    jm.add_log(job_id, "WARNING - Job cancelled while waiting for its preview job")
                    jm.cancel_job(job_id)
                    return
                if not _wait_for_retry_time(job_id, cfg, cancel_check):
                    jm.add_log(job_id, "WARNING - Job cancelled while waiting to retry")
                    jm.cancel_job(job_id)
                    return
                if job.paused and not _hold_pause_from_before_restart(job_id, cancel_check):
                    jm.add_log(job_id, "WARNING - Job cancelled while paused")
                    jm.cancel_job(job_id)
                    return
                slot["priority"] = live_priority()
                if not get_job_gate().acquire(priority=slot["priority"], cancel_check=cancel_check, on_wait=on_wait):
                    jm.add_log(job_id, "WARNING - Job cancelled while waiting for active slot")
                    jm.cancel_job(job_id)
                    return
                slot["held"] = True
                jm.start_job(job_id)
                jm.add_log(job_id, "INFO - Intro & Credits job started")

                def progress_callback(current, total, message, percent_override=None):
                    if percent_override is not None:
                        percent = percent_override
                    else:
                        percent = (current / total * 100) if total else 0
                    jm.update_progress(
                        job_id, percent=percent, processed_items=current, total_items=total, current_item=message
                    )

                def worker_callback(workers_list):
                    keys = set()
                    for w in workers_list:
                        key = f"{w['worker_type']}_{w['worker_id']}"
                        keys.add(key)
                        remaining = w.get("remaining_time")
                        jm.update_worker_status(
                            key,
                            WorkerStatus(
                                worker_id=w["worker_id"],
                                worker_type=w["worker_type"],
                                worker_name=w["worker_name"],
                                status=w["status"],
                                current_title=w.get("current_title", ""),
                                library_name=w.get("library_name", ""),
                                progress_percent=w.get("progress_percent", 0),
                                speed=w.get("speed", "0.0x"),
                                eta=_format_eta(float(remaining))
                                if isinstance(remaining, int | float) and remaining > 0
                                else "",
                                ffmpeg_started=bool(w.get("ffmpeg_started", False)),
                                current_phase=w.get("current_phase", "") or "",
                            ),
                        )
                    jm.prune_worker_statuses(keys)
                    jm.emit_worker_statuses()

                config = load_config()
                registry = _build_multi_server_registry(config)
                if registry is None:
                    jm.complete_job(job_id, error="Couldn't load the media servers configuration")
                    return
                items, warnings, sender_paths = build_items(
                    cfg, registry=registry, cancel_check=cancel_check, progress_callback=progress_callback
                )
                if cancel_check():
                    jm.cancel_job(job_id)
                    return
                if not items:
                    jm.complete_job(job_id, warning=" ".join(["No files to check.", *warnings]))
                    return
                ctx = build_context(
                    registry=registry, config=config, priority=live_priority, force=bool(cfg.get("force"))
                )
                items, carried = _skip_finished_before_restart(jm, job_id, items, ctx.store)
                if not items:
                    jm.set_job_outcome(job_id, carried)
                    _complete(jm, job_id, carried, warnings)
                    return
                waiting: dict[str, set[str]] = {}
                replaced: set[str] = set()
                unchecked: dict[str, set[str]] = {}
                # Webhook paths (and their retries) can arrive before the file is visible here (an import still
                # copying over NFS); the preview job retries those too. A file the user picked, or a library
                # listing, that isn't on disk won't appear by waiting; a verify job's file was there already.
                sent_files = bool(cfg.get("file_paths")) and cfg.get("source") not in _USER_PICKED_SOURCES
                retries_missing_files = sent_files and not cfg.get("verify")
                # Only files just sent were just replaced: a listing's replaced file may have changed days ago, and
                # servers rescanned it long since. A verify chain checks once.
                checks_replaced_later = sent_files and not (cfg.get("verify") or cfg.get("verify_chain"))

                def on_file_result(file_path, outcome, reason, worker, servers=None):
                    # Any server that can take the file later, even when another server was written.
                    codes = {code for row in servers or [] if (code := retry_reason(row))}
                    for code in codes:
                        waiting.setdefault(code, set()).add(file_path)
                    if not codes and retries_missing_files and outcome == FileOutcome.FILE_NOT_FOUND.value:
                        waiting.setdefault(NOT_ON_DISK, set()).add(file_path)
                    if any(isinstance(row, dict) and row.get(VERIFY_LATER) for row in servers or []):
                        replaced.add(file_path)
                    for row in servers or []:
                        if isinstance(row, dict) and row.get(READ_BACK_FAILED):
                            name = str(row.get("server_name") or row.get("server_id") or "a server")
                            unchecked.setdefault(name, set()).add(file_path)
                    jm.record_file_result(
                        job_id, file_path, outcome, reason, worker, servers=servers, server_messages=True
                    )

                set_file_result_callback(on_file_result, job_id=job_id)
                dispatcher = get_or_create_dispatcher(config, _build_selected_gpus(settings))
                tracker = dispatcher.submit_items(
                    job_id=job_id,
                    items=items,
                    config=config,
                    registry=registry,
                    title_max_width=200,
                    library_name="",
                    callbacks={
                        "progress_callback": progress_callback,
                        "worker_callback": worker_callback,
                        "cancel_check": cancel_check,
                        "pause_check": lambda: (
                            not slot["held"]
                            or jm.is_pause_requested(job_id)
                            or get_settings_manager().processing_paused
                        ),
                    },
                    priority=live_priority(),
                    kind=JOB_KIND_INTRO_CREDITS,
                    handlers=kind_handlers(ctx),
                    carried_outcome=carried,
                )
                # The priority route skips the dispatcher while this job has no tracker yet; a change that landed
                # between reading the priority and registering the tracker would otherwise be lost.
                current = live_priority()
                if tracker.priority != current:
                    dispatcher.update_job_priority(job_id, current)
                _wait_releasing_slot_while_paused(
                    tracker,
                    job_id=job_id,
                    slot=slot,
                    live_priority=live_priority,
                    cancel_check=cancel_check,
                    on_wait=lambda active, cap, eff: jm.update_progress(
                        job_id, current_item=format_wait_message(active, cap, eff)
                    ),
                )
                result = tracker.get_result()
                outcome = dict(result["outcome"])  # includes the carried counts
                jm.set_job_outcome(job_id, outcome)
                if result["cancelled"] or cancel_check():
                    jm.cancel_job(job_id)
                    return
                # Those files stay "Up to date": a read failure mustn't rewrite them, but it mustn't go unseen either.
                unchecked_warnings = [
                    f"Couldn't check what {len(files)} file(s) show on {name}"
                    for name, files in sorted(unchecked.items())
                ]
                _complete(jm, job_id, outcome, [*warnings, *unchecked_warnings, *budget_exhausted_warnings(ctx)])
                if waiting:
                    _queue_retry(job, cfg, waiting, sender_paths)
                if replaced and checks_replaced_later:
                    _queue_verify(job, cfg, replaced, sender_paths)
            finally:
                clear_failures()
    except Exception as exc:
        logger.exception("Intro & Credits job {} failed", job_id)
        if dispatcher is not None:
            # Its checks would otherwise go on publishing for a failed job, without a slot or Files-panel rows.
            try:
                dispatcher.cancel_job(job_id)
            except Exception as cancel_exc:
                logger.warning("Could not stop the remaining files of Intro & Credits job {}: {}", job_id, cancel_exc)
        try:
            jm.complete_job(job_id, error=f"{type(exc).__name__}: {exc}")
        except Exception as complete_exc:
            logger.warning("Could not mark Intro & Credits job {} failed: {}", job_id, complete_exc)
    finally:
        # Same teardown as the preview runner (web/routes/job_runner.py run_job finally): slot first so the next
        # waiter admits quickly, then per-job flags, then worker cards once nothing else is running.
        if slot["held"]:
            try:
                get_job_gate().release(slot["priority"])
            except Exception as exc:
                logger.debug("Could not release job gate for {}: {}", job_id, exc)
            slot["held"] = False
        set_file_result_callback(None, job_id=job_id)
        jm.clear_pause_flag(job_id)
        jm.clear_cancellation_flag(job_id)
        try:
            if not jm.get_running_jobs():
                jm.clear_worker_statuses()
        except Exception as exc:
            logger.debug("Could not clear worker statuses after {}: {}", job_id, exc)
        unregister_job_thread()
        try:
            loguru_logger.complete()
            loguru_logger.remove(handler_id)
        except (ValueError, TypeError):
            logger.debug("Could not remove the job log handler for {}", job_id)


def start_intro_credits_job_async(job_id: str, config_overrides: dict | None = None) -> None:
    """Start the job on a daemon thread (a second start for a job already in flight is ignored).

    Args:
        job_id: The job to start.
        config_overrides: Keys merged into the job's config first (resume paths pass the job's own config).
    """
    if config_overrides:
        jm = get_job_manager()
        job = jm.get_job(job_id)
        if job is not None:
            merged = {**(job.config or {}), **config_overrides}
            if merged != (job.config or {}):
                jm.update_job_config(job_id, merged)
    with _inflight_lock:
        if job_id in _inflight_jobs:
            logger.info("Skipping duplicate Intro & Credits start for {} — already in flight", job_id)
            return
        _inflight_jobs.add(job_id)

    def _run() -> None:
        try:
            run_intro_credits_job(job_id)
        finally:
            with _inflight_lock:
                _inflight_jobs.discard(job_id)

    threading.Thread(target=_run, daemon=True, name=f"run_job_intro_credits_{job_id}").start()
