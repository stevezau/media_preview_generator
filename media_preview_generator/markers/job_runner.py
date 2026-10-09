"""Intro & Credits job thread: gate → enumerate/resolve files → shared dispatcher with marker handlers → complete."""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from typing import TypedDict

from loguru import logger

from ..config import load_config
from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..jobs.checkpoints import checkpoint_items, read_checkpoint
from ..jobs.dispatcher import get_or_create_dispatcher
from ..jobs.group_runtime import admission_options, refresh_worker_groups, runtime_capacity, wait_for_capacity
from ..jobs.orchestrator import _build_multi_server_registry
from ..jobs.parking import JobParked, park_if_unavailable
from ..jobs.worker import JOB_LOG_SKIP, is_job_thread_for, register_job_thread, unregister_job_thread
from ..processing.generator import clear_failures, failure_scope, set_file_result_callback
from ..processing.retry_queue import retry_policy, scaled_backoff_delay
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig
from ..utils import redact_secrets, redacted_traceback
from ..web.job_gate import format_wait_message, get_job_gate, release_slot
from ..web.jobs import (
    PAUSED_BY_SCHEDULE,
    JobStatus,
    WorkerStatus,
    get_job_manager,
)
from ..web.routes._helpers import _ensure_gpu_cache
from ..web.routes.job_runner import (
    _build_selected_gpus,
    _format_eta,
    _inflight_jobs,
    _inflight_lock,
    _is_force_fire_now_set,
    _retry_job_label,
)
from ..web.settings_manager import get_settings_manager
from .audio.fingerprint import start_fingerprint_sweep
from .credits import decode_check
from .job_log import start_line
from .missing import MISSING_LINE
from .models import Source
from .models import utcnow as _utcnow
from .outcomes import (
    FILE_BUSY,
    NOT_IN_LIBRARY,
    PLEX_DB_BUSY,
    PLEX_PASS_UNKNOWN,
    READ_BACK_FAILED,
    RETRY_REASON_CODES,
    VERIFY_LATER,
    FileOutcome,
    ServerStatus,
)
from .ownership import marker_libraries
from .park_state import restore_context, snapshot_context
from .pipeline import (
    PipelineContext,
    budget_exhausted_warnings,
    build_context,
    cached_capability,
    kind_handlers,
    run_detector_checks,
)
from .reconcile import LISTING_CONFIG_KEY, RECONCILE_SOURCE, CheckServersListing
from .settings import load_server
from .source_counts import DecidedByTally, stored_groups
from .store import MarkerStore

_POLL_S = 1.0
# Polls a PENDING preview job may go without a thread before its follow-up stops waiting for it. Covers the moment
# between two starts of the pending drain or the restart requeue; a job not revived after a restart never gets one.
_ORPHAN_GRACE_POLLS = 30
# Largest retry (or verify) job, and the most files one Check servers run takes; a bigger backlog (a new library's
# unindexed files, many drifted items) waits for a later run instead.
MAX_RETRY_FILES = 500
# A replaced file is checked again this long after its publish at the least (servers rescan it after the job).
MIN_VERIFY_DELAY_S = 600
VERIFY_DELAY_FACTOR = 3
# Job sources where the user chose the files (API/Start job dialog, Inspector re-detect, Season view Publish).
_USER_PICKED_SOURCES = frozenset({"manual", "inspector", "inspector_season"})
# The follow-up of a scheduled Recently Added scan (``jobs.orchestrator._queue_intro_credits_follow_ups``).
RECENTLY_ADDED_SOURCE = "recently_added"
# A follow-up's config key once its runner has read its files: nothing joins it after that.
FILES_SEALED = "files_sealed"
# Serialises webhook episodes joining a waiting follow-up with its runner reading them.
FOLLOW_UP_LOCK = threading.Lock()
# Config keys updated by webhook joins or the runner's file seal.
_JOINED_KEYS = ("file_paths", "webhook_item_id_hints", FILES_SEALED)
# Job sources whose files no sender just reported: a file missing from disk won't appear by waiting (and nothing was
# just replaced). A retry Check servers queued is one of them; its not-in-library retries still chain. A scheduled
# Recently Added scan's follow-up lists what servers had already indexed, like a library listing.
_NO_RETRY_SOURCES = _USER_PICKED_SOURCES | {
    RECENTLY_ADDED_SOURCE,
    RECONCILE_SOURCE,
}


def server_pin(config: object) -> str | None:
    """The one server a job publishes to (its config's ``server_id``), or None for every server with markers on.

    Args:
        config: The job's config.

    Returns:
        The pinned server's id; None when the job isn't pinned.
    """
    pin = config.get("server_id") if isinstance(config, dict) else None
    return pin if isinstance(pin, str) and pin else None


class _Pin(TypedDict, total=False):
    server_id: str


def _pinned_to(config: object) -> _Pin:
    """``create_intro_credits_job``'s ``server_id`` for a job this job queues for its own files (a retry or a verify
    job): they publish only where it does. Empty for an unpinned job."""
    pin = server_pin(config)
    return {"server_id": pin} if pin else {}


def retry_reason(row: object) -> str | None:
    """Why a per-server row's file is worth trying again later, if it is.

    Args:
        row: One of the pipeline's per-server rows.

    Returns:
        The reason code of a waiting row the job retries (the server hasn't indexed the file yet, Plex didn't answer
        its Plex Pass check, or another job kept running the file past the worker's wait), or ``PLEX_DB_BUSY`` for
        a failed row whose write gave up waiting for Plex's database; None for any other row.
    """
    if not isinstance(row, dict):
        return None
    code = row.get("reason_code")
    if row.get("status") == ServerStatus.FAILED.value:
        return code if code == PLEX_DB_BUSY else None
    if row.get("status") != ServerStatus.WAITING.value:
        return None
    return code if code in RETRY_REASON_CODES else None


NOT_ON_DISK = "not_on_disk"
# Retry log wording per reason, in the order a combined line lists them.
_RETRY_WORDS = {
    NOT_ON_DISK: "not on disk",
    NOT_IN_LIBRARY: "not in a server's library",
    PLEX_PASS_UNKNOWN: "not checked on Plex",
    PLEX_DB_BUSY: "not written to Plex's busy database",
    FILE_BUSY: "not released by another job",
}


def _retry_reason(waiting: dict[str, set[str]]) -> str:
    words = [text for reason, text in _RETRY_WORDS.items() if waiting.get(reason)]
    listed = words[0] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"
    return f"{listed} yet"


def _upsert_chain(
    jm,
    head_id: str,
    *,
    attempt: int,
    max_attempts: int,
    outcome: str,
    next_run_at: str | None = None,
    wait_seconds: int | None = None,
    reason: str | None = None,
) -> None:
    """Move the chain head's row through the preview retries' chain states (``upsert_retry_chain_job``). Never raises.

    Args:
        jm: The job manager.
        head_id: The job whose row the chain is.
        attempt: The retry this state is about.
        max_attempts: The retry count in force.
        outcome: ``scheduled``, ``running``, ``completed`` or ``exhausted``.
        next_run_at: When the scheduled retry starts.
        wait_seconds: Its delay (the countdown bar).
        reason: Why the chain is exhausted.
    """
    try:
        jm.upsert_retry_chain_job(
            canonical_path="",
            basename="",
            attempt=attempt,
            max_attempts=max_attempts,
            next_run_at=next_run_at,
            wait_seconds=wait_seconds,
            outcome=outcome,
            originating_job_id=head_id,
            reason=reason,
        )
    except Exception as exc:
        # As the preview runner: the retry still runs, the row just doesn't show it.
        logger.warning(
            "upsert_retry_chain_job({}) failed for Intro & Credits job {}: {}: {}",
            outcome,
            head_id,
            type(exc).__name__,
            exc,
        )


# File outcomes whose markers a run doesn't count in "Decided by" (``pipeline`` counts a file unless it failed; no
# owner or no file means nothing was decided).
_NOT_DECIDED_OUTCOMES = frozenset(
    {
        FileOutcome.FAILED.value,
        FileOutcome.NO_OWNERS.value,
        FileOutcome.FILE_NOT_FOUND.value,
        FileOutcome.SOURCE_GONE.value,
    }
)


def _recount_chain_head(jm, head_id: str, store) -> None:
    """Count the chain head's outcome and "Decided by" again from its Files-panel rows, which its retries update.

    A retry counts only its own files; the head's row shows every file's latest result. Rows capped by the Files
    panel's per-outcome limit no longer list every file, so the counts are left as they are then. Never raises; the
    next chain state (``_upsert_chain``) stores them.
    """
    try:
        rows = jm.get_file_results(head_id, dedup_by_path=True)
        if any(not row.get("file") for row in rows):
            return
        sources = DecidedByTally()
        for row in rows:
            if row.get("outcome") not in _NOT_DECIDED_OUTCOMES:
                sources.add(stored_groups(store, row["file"]))
        jm.set_job_outcome(head_id, dict(Counter(row.get("outcome") for row in rows)))
        jm.set_marker_sources(head_id, sources.snapshot())
    except Exception as exc:
        logger.warning("Couldn't count the results of Intro & Credits job {} again: {}", head_id, type(exc).__name__)


def _end_chain(jm, cfg: dict, waiting: dict[str, set[str]]) -> None:
    """Give the chain head of a retry that queued no further retry its final status, as a preview chain ends.

    Completed once no file waits; failed ("exhausted") while some still do.
    """
    attempt = int(cfg.get("retry_attempt") or 0)
    left = {path for files in waiting.values() for path in files}
    reason = None
    if left:
        reason = (
            f"{len(left)} file(s) still {_retry_reason(waiting).removesuffix(' yet')} after {attempt} "
            f"retr{'y' if attempt == 1 else 'ies'}. Check the Files panel for the affected paths."
        )
    _upsert_chain(
        jm,
        cfg["parent_job_id"],
        attempt=attempt,
        max_attempts=int(cfg.get("max_retries") or attempt),
        outcome="exhausted" if left else "completed",
        reason=reason,
    )


def _queue_retry(
    job,
    cfg: dict,
    waiting: dict[str, set[str]],
    sender_paths: dict[str, str],
    promised: Collection[str] = frozenset(),
) -> list[str]:
    """Create the delayed retry job for files that weren't on disk yet, that a server could take later, or whose write
    gave up waiting for Plex's busy database (a few minutes later it is usually free).

    Up to ``webhook_retry_count`` retries, one job for every reason; a verify job's retries go on counting from the
    retries its chain used before it, and never queue another verify. The retry gets each file as its sender gave it
    (with that path's item id hints), like the preview retries: a file not on disk yet was given the first mapped
    disk's path, and only the sender's path is resolved again against every disk. Never raises.

    The retry is a hidden job of the job's retry chain, as a preview job's retries are: the job that found the files
    (or the job a retry belongs to) stays the one row, pending with the "Retry N/M" chip and its countdown.

    Args:
        job: The job that found the files.
        cfg: Its config.
        waiting: Local paths per reason (``NOT_ON_DISK`` or a row's retry reason code).
        sender_paths: The path each local path was given as (``build_items``); a missing entry is retried as is.
        promised: Local paths whose row said this job tries again (``PipelineContext.busy_promised``, at most
            ``MAX_RETRY_FILES``): taken first when more files wait than a retry job takes.

    Returns:
        The paths the retry job lists (as sent); empty when none was queued.
    """
    jm = get_job_manager()
    waiting_paths = {path for files in waiting.values() for path in files}
    first = _sent_paths(waiting_paths & set(promised), sender_paths)
    seen = set(first)
    paths = [*first, *(path for path in _sent_paths(waiting_paths, sender_paths) if path not in seen)]
    reason = _retry_reason(waiting)
    # Check servers leaves the items of a file it doesn't retry to be read back again; any other job's file is only
    # tried again by a job that lists it (a retry chain started by Check servers included: its items are gone).
    again = (
        "Check servers checks them again on its next run"
        if cfg.get("reconcile")
        else "a later job for them tries again"
    )
    try:
        attempt = int(cfg.get("retry_attempt") or cfg.get("chain_attempt") or 0) + 1
        count, delay_setting = retry_policy(get_settings_manager())
        if count < 1:
            jm.add_log(job.id, f"WARNING - {len(paths)} file(s) {reason}; retries are off, so {again}")
            return []
        if attempt > count:
            jm.add_log(
                job.id,
                f"WARNING - {len(paths)} file(s) still {reason.removesuffix(' yet')} after {count} retr"
                f"{'y' if count == 1 else 'ies'}; {again}",
            )
            return []
        from .triggers import create_intro_credits_job

        if len(paths) > MAX_RETRY_FILES:
            jm.add_log(job.id, f"INFO - {len(paths) - MAX_RETRY_FILES} more files {reason} get no retry; {again}")
            paths = paths[:MAX_RETRY_FILES]
        delay = scaled_backoff_delay(attempt, delay_setting)
        # A retry's own retry joins the same chain; any other job (an old top-level "Retry:" job included) heads one.
        head_id = cfg.get("parent_job_id") or job.id
        retry = create_intro_credits_job(
            library_name=_later_job_name("Retry: ", job, paths),
            priority=job.priority,
            source=str(cfg.get("source") or "retry"),
            file_paths=paths,
            item_id_hints=_hints_for(cfg, paths) or None,
            retry_attempt=attempt,
            retry_delay_s=delay,
            verify_chain=bool(cfg.get("verify") or cfg.get("verify_chain")),
            parent_job_id=head_id,
            max_retries=count,
            **_pinned_to(cfg),
        )
        _upsert_chain(
            jm,
            head_id,
            attempt=attempt,
            max_attempts=count,
            outcome="scheduled",
            next_run_at=retry.config.get("retry_not_before"),
            wait_seconds=delay,
        )
        jm.add_log(
            job.id,
            f"INFO - {len(paths)} file(s) {reason}; retry {attempt} of {count} in {delay}s (job {retry.id[:8]})",
        )
        return paths
    except Exception:
        logger.exception("Could not queue the retry for files job {} found waiting", job.id)
        return []


def _later_job_name(prefix: str, job, paths: list[str]) -> str:
    """Name a retry or verify job after the job it follows, counting the files the later job itself runs.

    Args:
        prefix: ``"Retry: "`` or ``"Verify: "``.
        job: The job it follows (a "Retry: " or "Verify: " prefix of its own is dropped, so they never stack).
        paths: The files the later job is created with.

    Returns:
        The later job's name: a trailing "N files" of the followed job's name becomes this job's own count.
    """
    followed = (job.library_name or "Intro & Credits").removeprefix("Retry: ").removeprefix("Verify: ")
    return prefix + _retry_job_label(followed, paths).removeprefix("Retry: ")


def _sent_paths(files: set[str], sender_paths: dict[str, str]) -> list[str]:
    """Local paths as their senders gave them, sorted: what a later job for these files is created with."""
    return sorted({sender_paths.get(path, path) for path in files})


def _with_merged_sender_hints(cfg: dict, items: list[ProcessableItem], sender_paths: dict[str, str]) -> dict:
    """The job's config with each sent file's item ids as ``build_items`` merged them from every sender of the file.

    A retry or verify job lists a file as its first sender gave it, with that path's hints (``_hints_for``); the ids a
    second sender gave (Plex's, next to Sonarr's) would otherwise be lost. The job's stored config is left as it is.
    """
    merged = {
        sender_paths[item.canonical_path]: dict(item.item_id_by_server)
        for item in items
        if item.item_id_by_server and item.canonical_path in sender_paths
    }
    if not merged:
        return cfg
    return {**cfg, "webhook_item_id_hints": {**(cfg.get("webhook_item_id_hints") or {}), **merged}}


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
        delay = max(MIN_VERIFY_DELAY_S, scaled_backoff_delay(1, delay_setting) * VERIFY_DELAY_FACTOR)
        check = create_intro_credits_job(
            library_name=_later_job_name("Verify: ", job, paths),
            priority=job.priority,
            source=str(cfg.get("source") or "verify"),
            file_paths=paths,
            item_id_hints=_hints_for(cfg, paths) or None,
            retry_delay_s=delay,
            verify=True,
            chain_attempt=int(cfg.get("retry_attempt") or 0),
            **_pinned_to(cfg),
        )
        jm.add_log(job.id, f"INFO - {len(paths)} replaced file(s) are checked again in {delay}s (job {check.id[:8]})")
    except Exception:
        logger.exception("Could not queue the later check of the replaced files job {} published", job.id)


# What started the preview job a follow-up follows (its ``source``), in words.
_SENDER_WORDS = {
    "radarr": "Radarr import",
    "sonarr": "Sonarr import",
    "tdarr": "Tdarr",
    "plex": "Plex webhook",
    "emby": "Emby webhook",
    "jellyfin": "Jellyfin webhook",
    "webhook": "webhook",
    RECENTLY_ADDED_SOURCE: "Recently Added scan",
    "manual": "manual run",
    "schedule": "schedule",
}


def trigger_words(job, cfg: dict) -> str:
    """What started an Intro & Credits job, in plain words, for its first log line (``job_log.start_line``).

    Args:
        job: The job.
        cfg: Its config.

    Returns:
        E.g. ``follow-up to preview job c7ca6327 (Radarr import)``, ``scheduled "Nightly"`` or ``manual Re-detect``.
    """
    source = str(cfg.get("source") or "")
    if cfg.get("retry_attempt"):
        attempt, most = int(cfg["retry_attempt"]), int(cfg.get("max_retries") or 0)
        head = str(cfg.get("parent_job_id") or "")[:8]
        text = f"retry {attempt} of {most}" if most else f"retry {attempt}"
        return f"{text} for job {head}" if head else text
    if cfg.get("verify"):
        return "checking again files published just after they were replaced"
    if cfg.get("reconcile"):
        return "Check servers"
    if cfg.get("follows_job_id"):
        sender = _SENDER_WORDS.get(source, source or "webhook")
        return f"follow-up to preview job {str(cfg['follows_job_id'])[:8]} ({sender})"
    if source == "schedule":
        from ..web.scheduler import schedule_name

        name = schedule_name(str(getattr(job, "parent_schedule_id", "") or ""))
        return f'scheduled "{name}"' if name else "scheduled"
    if source == "inspector":
        return "manual Re-detect" if cfg.get("force") else "publishing your saved markers"
    if source == "inspector_season":
        return "Publish from the Season view"
    return _SENDER_WORDS.get(source, source or "manual run")


def _seal_files(jm, job_id: str, job, cfg: dict) -> dict:
    """The config whose files the job lists now: re-read and sealed when other requests may have added files to it.

    Webhook episodes can join until the runner seals its files under the same lock.

    Returns:
        The config to list the files of.
    """
    if not cfg.get("follows_job_id"):
        return cfg
    with FOLLOW_UP_LOCK:
        # Only the seal is written (under the job manager's lock): a key another thread set meanwhile stays.
        jm.merge_job_config(job_id, {FILES_SEALED: True})
        latest = jm.get_job(job_id) or job
        return {**(latest.config or {}), FILES_SEALED: True}


def wait_for_retry_time(
    job_id: str, cfg: dict, cancel_check: Callable[[], bool], *, progress_job_id: str | None = None
) -> bool:
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
    get_job_gate().defer_preflight(job_id)
    jm = get_job_manager()
    remaining = int((due - _utcnow()).total_seconds())

    def update_wait(**kwargs) -> None:
        jm.update_progress(job_id, **kwargs)
        if progress_job_id:
            jm.update_progress(progress_job_id, **kwargs)

    if cfg.get("verify"):
        waiting_for = (
            f"Check starting in {remaining}s — servers often rescan a replaced file after its markers are sent"
        )
    else:
        waiting_for = f"Retry starting in {remaining}s — waiting for these files to appear on disk or on a server"
    update_wait(
        percent=0,
        processed_items=0,
        total_items=0,
        current_item=waiting_for,
        retry_eta=raw,
        retry_wait_total=int(cfg.get("retry_delay") or remaining),
    )
    was_paused = False
    while _utcnow() < due:
        if cancel_check():
            return False
        # The chain head's "Retry now" (POST /api/jobs/<head>/retry-now) flags this retry, as for preview retries.
        if _is_force_fire_now_set(jm, job_id):
            jm.add_log(job_id, "INFO - Retry backoff skipped — operator forced fire-now")
            break
        paused = get_settings_manager().processing_paused
        if was_paused and not paused:
            update_wait(retry_eta=due.isoformat())
        was_paused = paused
        slept_from = _utcnow()
        time.sleep(_POLL_S)
        if paused:
            # The countdown stands still while everything is paused (Pause all, quiet hours), as a preview retry's does.
            due += _utcnow() - slept_from
    update_wait(retry_eta=None)
    return True


def _all_libraries_listed(cfg: ServerConfig) -> ServerConfig:
    # Vendor enumeration skips libraries with previews turned off; markers have their own library selection.
    return dataclasses.replace(cfg, libraries=[dataclasses.replace(lib, enabled=True) for lib in cfg.libraries])


# Config keys of job types removed from the app; a job saved by an older build must not fall through to a full scan.
RETIRED_JOB_CONFIG_KEYS = frozenset({"decide_again", "online_recheck", "version_rerun"})


def build_items(
    job_config: dict,
    *,
    registry,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[..., None] | None = None,
    enabled: Callable[[ServerConfig], bool] = lambda cfg: load_server(cfg.markers, cfg.type.value).enabled,
    libraries: Callable[[ServerConfig], list] = marker_libraries,
    label: str = "Intro & Credits",
) -> tuple[list[ProcessableItem], list[str], dict[str, str]]:
    """Files for a job: explicit paths (webhook, manual, Inspector) or library enumeration.

    Args:
        job_config: The job's config (``file_paths`` + ``webhook_item_id_hints``, or ``libraries``).
        registry: The job's ``ServerRegistry``.
        cancel_check: True once the job is cancelled (stops enumeration).
        progress_callback: ``(current, total, message)`` for the enumeration banner.
        enabled: Whether the feature is on for a server (default: Intro & Credits' ``markers.enabled``).
        libraries: The libraries the feature goes to on a server.
        label: The feature's name, for warnings and the enumeration banner.

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
        by_path: dict[str, ProcessableItem] = {}
        for raw in _expand_directory_to_media_files(file_paths, mappings):
            canonical, _owners = orchestrator._resolve_webhook_path_to_canonical(raw, configs, log_resolution=False)
            sender_hints = dict(hints.get(raw) or {})
            known = by_path.get(canonical)
            if known is not None:
                # Two senders (Sonarr and Plex, say) reported one file: one item, with each server's id either knows;
                # the first sender's id wins a clash.
                for server_id, item_id in sender_hints.items():
                    known.item_id_by_server.setdefault(server_id, item_id)
                continue
            sender_paths[canonical] = raw
            by_path[canonical] = ProcessableItem(
                canonical_path=canonical,
                server_id="",
                item_id_by_server=sender_hints,
                title=os.path.basename(canonical),
            )
        items = list(by_path.values())
    elif RETIRED_JOB_CONFIG_KEYS & job_config.keys():
        warnings.append("This job type was retired; nothing to do")
    else:
        requested: dict[str, list[str]] = {}
        for entry in job_config.get("libraries") or []:
            requested.setdefault(str(entry["server_id"]), []).append(str(entry["library_id"]))
        library_ids: dict[str, list[str]] = {}
        candidates: list[ServerConfig] = []
        known_ids: set[str] = set()
        for cfg in registry.configs():
            known_ids.add(cfg.id)
            if requested and cfg.id not in requested:
                continue
            name = cfg.name or cfg.id
            if not cfg.enabled:
                if requested:
                    warnings.append(f"Skipped {name}: {name} is disabled")
                continue
            if not enabled(cfg):
                if requested:
                    warnings.append(f"Skipped {name}: {label} is turned off on {name}")
                continue
            allowed = [lib.id for lib in libraries(cfg)]
            if cfg.id in requested:
                names = {lib.id: lib.name for lib in cfg.libraries}
                ids = [lid for lid in requested[cfg.id] if lid in allowed]
                warnings.extend(
                    f"Skipped {names.get(lid) or lid}: {label} isn't on for it"
                    for lid in requested[cfg.id]
                    if lid not in allowed
                )
            else:
                ids = allowed
            if not ids:
                continue
            library_ids[cfg.id] = ids
            candidates.append(_all_libraries_listed(cfg))
        warnings.extend(
            f"Skipped server {sid}: {sid} is no longer configured" for sid in requested if sid not in known_ids
        )
        pairs, errors = orchestrator._enumerate_items_for_servers(
            candidates,
            enumerate_one=lambda processor, cfg: processor.list_canonical_paths(
                cfg,
                library_ids=library_ids[cfg.id],
                cancel_check=cancel_check,
                progress_callback=progress_callback,
            ),
            cancel_check=cancel_check,
            label=label,
            progress_callback=progress_callback,
        )
        items = [item for _cfg, item in pairs]
        warnings.extend(f"Couldn't list {name}: {err}" for name, err in errors)
    unique: dict[str, ProcessableItem] = {}
    for item in sorted(items, key=lambda i: (os.path.dirname(i.canonical_path), i.canonical_path)):
        unique.setdefault(item.canonical_path, item)
    return list(unique.values()), warnings, sender_paths


def _confirmed_gone_items(listing, registry, path: str, servers: list | None) -> set[tuple[str, str]]:
    """``CheckServersListing.confirmed_gone_items`` for a file whose result is already recorded. Never raises.

    Returns:
        The ``(server_id, item_id)`` items a server confirmed gone (the file waits for a retry).
    """
    try:
        return listing.confirmed_gone_items(registry, path, servers or [])
    except Exception as exc:
        # Only the exception's type: a message can carry a server's URL or token.
        logger.warning("Check servers couldn't check the items of {}: {}", os.path.basename(path), type(exc).__name__)
        return set()


def _mark_retried_items_gone(
    store, gone_items: dict[str, set[tuple[str, str]]], retried: list[str], sender_paths: dict[str, str]
) -> None:
    """Take the confirmed-gone items of the files the retry job lists out of Check servers. Never raises.

    Only once that retry exists: a file whose run ended before (cancelled, failed, a restart) or that got no retry
    (retries off, past the cap) keeps its items, so the next Check servers run reads them back, confirms them again and
    queues the retry then.

    Args:
        store: The markers store.
        gone_items: Per local path, the items confirmed gone.
        retried: The paths the retry job lists (as sent).
        sender_paths: The path each local path was given as.
    """
    listed = set(retried)
    for path, items in gone_items.items():
        if sender_paths.get(path, path) not in listed:
            continue
        for server_id, item_id in sorted(items):
            try:
                store.mark_item_gone(server_id, item_id)
                logger.info("{} no longer has item {}; Check servers stops reading it back", server_id, item_id)
            except Exception as exc:
                logger.warning("Check servers couldn't mark item {} of {} gone: {}", item_id, server_id,
                               type(exc).__name__)  # fmt: skip


def _in_flight(job_id: str) -> bool:
    with _inflight_lock:
        return job_id in _inflight_jobs


def wait_for_preceding_job(job_id: str, follows_job_id: str | None, cancel_check: Callable[[], bool]) -> bool:
    """Hold a webhook follow-up until its preview job has finished.

    Priority alone can't order them: users can set incoming preview jobs to Normal or Low, and then this job
    (which does less work before the gate) would take the slot first and run before the previews for the same
    files. Runs before the gate, so waiting costs no slot. A preview job counting down to a retry
    (PENDING with ``progress.retry_eta``) counts as finished, so this job starts after the preview job's first try: its
    backoff can run for over an hour; files a server hasn't indexed yet, or not on disk yet, are retried in this job's
    own retry chain (``_queue_retry``). A PENDING preview job with no thread to run it (too old to be revived after a
    restart) counts as finished after ``_ORPHAN_GRACE_POLLS``; while processing is paused it doesn't, as resuming
    starts it.

    Returns:
        False if the job was cancelled while waiting.
    """
    if not follows_job_id:
        return True
    from ..jobs.admission import preceding_job_ready

    jm = get_job_manager()
    announced = False
    polls_without_thread = 0
    while True:
        if cancel_check():
            return False
        preceding = jm.get_job(follows_job_id)
        if preceding_job_ready(preceding):
            return True
        assert preceding is not None  # preceding_job_ready is True for a missing job
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


def hold_pause_from_before_restart(job_id: str, cancel_check: Callable[[], bool]) -> bool:
    """Keep a job paused before a restart paused, holding no slot, until it is resumed.

    The pending job keeps its existing pause owners. A schedule's next start
    clears its own stop-time hold; any independent manual hold remains.

    Returns:
        False if the job was cancelled while paused.
    """
    jm = get_job_manager()
    job = jm.get_job(job_id)
    jm.request_pause(job_id, by_schedule=bool(job and (job.config or {}).get(PAUSED_BY_SCHEDULE)))
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
) -> tuple[list[ProcessableItem], dict[str, int], set[str], dict[str, str]]:
    """Drop files this job settled before a restart revived it, so they aren't looked up or published twice.

    Their outcomes come from the job's own Files-panel rows (only a revived job has any) and are carried into the
    job's counts. A file replaced since it was analysed (size or mtime differ from the markers store), or with a
    server still waiting for it, runs again.

    Returns:
        The items still to do, the carried outcome counts, the carried files published after they were replaced
        (a row flagged ``VERIFY_LATER``): the job still owes them their later check, and each carried file's outcome.
    """
    try:
        rows = jm.get_file_results(job_id)
    except Exception as exc:
        logger.warning("Couldn't read the files this job already finished ({}); checking every file", exc)
        return items, {}, set(), {}
    settled: dict[str, str] = {}
    to_verify: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("file") or row.get("outcome") not in _SETTLED_OUTCOMES:
            continue
        servers = [server for server in row.get("servers") or [] if isinstance(server, dict)]
        # A file published to one server while another hadn't indexed it yet still needs its retry.
        if any(server.get("status") == ServerStatus.WAITING.value for server in servers):
            continue
        settled[row["file"]] = row["outcome"]
        if any(server.get(VERIFY_LATER) for server in servers):
            to_verify.add(row["file"])
    if not settled:
        return items, {}, set(), {}
    carried_outcomes: dict[str, str] = {}
    carried_to_verify: set[str] = set()
    remaining = []
    for item in items:
        outcome = settled.get(item.canonical_path)
        if outcome is not None and _unchanged_since_analysed(store, item.canonical_path):
            carried_outcomes[item.canonical_path] = outcome
            if item.canonical_path in to_verify:
                carried_to_verify.add(item.canonical_path)
        else:
            remaining.append(item)
    carried = Counter(carried_outcomes.values())
    if carried:
        logger.info(
            "Resuming after a restart: {} file(s) finished before it are not checked again", sum(carried.values())
        )
    return remaining, dict(carried), carried_to_verify, carried_outcomes


def _start_fingerprint_sweep(cfg: dict, store: MarkerStore, configs: Sequence[ServerConfig]) -> None:
    """After a job completed and gave back its slot: start marking a batch of files missing from disk (and clearing
    the marks of those back) and clearing the cached fingerprints of files gone, so markers.db doesn't grow with every
    renamed or deleted episode (``start_fingerprint_sweep``: its own thread, at most one an hour).

    Retry and verify jobs don't: they follow a job that did. Never raises.
    """
    if cfg.get("retry_attempt") or cfg.get("verify"):
        return
    try:
        start_fingerprint_sweep(store, configs=configs)
    except Exception as exc:
        logger.warning("Couldn't start clearing old audio fingerprints: {}", exc)


def _retry_follows(cfg: dict) -> bool:
    """Whether this job queues a retry for a file that still needs one (``_queue_retry``: retries are on and this job
    isn't its chain's last attempt)."""
    count, _delay = retry_policy(get_settings_manager())
    return count >= int(cfg.get("retry_attempt") or cfg.get("chain_attempt") or 0) + 1


def _retries_missing_files(cfg: dict) -> bool:
    """Whether the job tries a file it finds missing from disk again later (``_queue_retry``'s ``NOT_ON_DISK``).

    Webhook paths (and their retries) can arrive before the file is visible here (an import still copying over NFS);
    the preview job retries those too. A file the user picked, or a library listing, that isn't on disk won't appear by
    waiting; a verify job's file was there already.
    """
    return _sends_files(cfg) and not cfg.get("verify")


def _sends_files(cfg: dict) -> bool:
    """Whether the job lists files a sender just reported (not a listing, nor a source that picks files itself)."""
    return bool(cfg.get("file_paths")) and sent_by_a_sender(cfg.get("source"))


def sent_by_a_sender(source: object) -> bool:
    """Whether a job of this source lists files a sender just reported (a webhook, its retries), which get a retry when
    missing from disk and a later verify when replaced, rather than files a listing or the user picked.

    Args:
        source: The job's ``source``.

    Returns:
        True for a sender's files.
    """
    return source not in _NO_RETRY_SOURCES


def _log_missing(ctx: PipelineContext) -> None:
    """Log how many files this job marked missing from disk (``PipelineContext.take_missing``)."""
    missing = ctx.take_missing()
    if missing:
        logger.info(MISSING_LINE, missing)


def _log_summary(jm, job_id: str, outcome: dict[str, int], ctx: PipelineContext) -> None:
    """End the job's log with the totals line. Never raises."""
    try:
        logger.complete()  # the files' lines, still queued for the job's log, come before these
        for line in ctx.summary_lines(outcome):
            jm.add_log(job_id, f"INFO - {line}")
    except Exception as exc:
        # The job's work is done: a summary that can't be written mustn't fail it.
        logger.warning("Couldn't write the summary of Intro & Credits job {}: {}", job_id, type(exc).__name__)


def finish_job(
    jm,
    job_id: str,
    outcome: dict[str, int],
    warnings: list[str],
    ctx: PipelineContext | None = None,
    *,
    retried: bool = False,
) -> None:
    """Complete the job: failed (red) when every counted file failed or wasn't on disk, a warning (amber) when some
    failed or on warnings.

    Files missing from disk count against the job like failures only when it queued no retry, as in the preview
    runner (``web.routes.job_runner._classify_job_completion``'s ``all_not_found``: any retry keeps the job's chain
    pending). Given the job's pipeline context,
    the job's log gets its closing lines first (``PipelineContext.summary_lines``).

    Args:
        jm: The job manager.
        job_id: The job.
        outcome: Its file outcome counts.
        warnings: Lines for the job's completion message.
        ctx: The job's pipeline context, for its closing log lines.
        retried: Whether this run queued a retry.
    """
    if ctx is not None:
        _log_summary(jm, job_id, outcome, ctx)
    failed = outcome.get(FileOutcome.FAILED.value, 0)
    missing = 0 if retried else outcome.get(FileOutcome.FILE_NOT_FOUND.value, 0)
    succeeded = sum(outcome.values()) - failed - missing
    if (failed or missing) and not succeeded:
        if not missing:
            message = f"All {failed} file(s) failed — see the Files panel"
        elif not failed:
            message = f"All {missing} file(s) weren't found on disk — check the path mappings"
        else:
            message = f"All {failed + missing} file(s) failed or weren't found on disk — see the Files panel"
        jm.complete_job(job_id, error=" | ".join([message, *warnings]))
        return
    parts = ([f"{failed} file(s) failed"] if failed else []) + warnings
    jm.complete_job(job_id, warning=" | ".join(parts) or None)


def wait_for_tracker(
    tracker,
    *,
    cancel_check: Callable[[], bool],
    park_check: Callable[[], None] | None = None,
) -> None:
    """Wait for the tracker, giving the job a chance to park on each tick."""
    while not tracker.wait(timeout=_POLL_S):
        if cancel_check():
            continue
        if park_check is not None:
            park_check()


def job_freeze_check(jm, job_id: str) -> Callable[[], bool]:
    """The job's ``PipelineContext.freeze_check``: True while its running files' ffmpeg must stop where it is, as
    previews' does -- all processing paused (Pause all, quiet hours) or this job paused by its schedule's stop time
    (``PAUSED_BY_SCHEDULE``). A pause of this job by hand is not one: it lets the running file finish and only stops new
    files being picked.
    """

    def frozen() -> bool:
        if get_settings_manager().processing_paused:
            return True
        if not jm.is_pause_requested(job_id):
            return False
        job = jm.get_job(job_id)
        return bool(job is not None and (job.config or {}).get(PAUSED_BY_SCHEDULE))

    return frozen


def _start_decode_checks(ctx: PipelineContext, config, selected_gpus: Sequence[tuple]) -> None:
    """Start the credits decode check of each GPU the pool has workers for, in the background, when this job reads
    credit text (``decode_check.start_checks``: once per GPU for the process, never on a worker, nothing waits)."""
    if not any(spec.source is Source.CREDITS_TEXT for spec in ctx.local_detectors):
        return
    try:
        decode_check.start_checks(
            [(gpu_type, device) for gpu_type, device, *_info in selected_gpus],
            ffmpeg=getattr(config, "ffmpeg_path", None) or "ffmpeg",
        )
    except Exception as exc:  # a diagnostic: it never stops the job
        logger.warning("Couldn't start the credits decoding check: {}", exc)


def _cancel_check_waiting_out_pause(
    *, job_id: str, cancel_check: Callable[[], bool], on_pause: Callable[[], None]
) -> Callable[[], bool]:
    """A cancel check for work done on the job's own thread (Check servers' read-back): it doesn't return while the
    job or all processing is paused.

    Args:
        job_id: The job doing the work.
        cancel_check: True once the job is cancelled.
        on_pause: Called before waiting out a pause, so the wait doesn't hold a start-up slot.

    Returns:
        The check: True once the job is cancelled.
    """
    jm = get_job_manager()

    def check() -> bool:
        while not cancel_check():
            if not jm.is_pause_requested(job_id) and not get_settings_manager().processing_paused:
                return False
            on_pause()
            time.sleep(_POLL_S)
        return True

    return check


def worker_cards(jm) -> Callable[[list], None]:
    """The dispatcher's ``worker_callback`` for a kind's runner: keeps the dashboard's worker cards current."""

    def update(workers_list) -> None:
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
                    group_id=w.get("group_id"),
                    member_id=w.get("member_id"),
                    group_name=w.get("group_name"),
                    group_resource=w.get("group_resource"),
                    retiring=bool(w.get("retiring", False)),
                    status=w["status"],
                    job_id=w.get("job_id"),
                    current_file=w.get("current_file", ""),
                    current_title=w.get("current_title", ""),
                    library_name=w.get("library_name", ""),
                    progress_percent=w.get("progress_percent", 0),
                    speed=w.get("speed", "0.0x"),
                    eta=_format_eta(float(remaining)) if isinstance(remaining, int | float) and remaining > 0 else "",
                    ffmpeg_started=bool(w.get("ffmpeg_started", False)),
                    current_phase=w.get("current_phase", "") or "",
                    chapter_progress=w.get("chapter_progress"),
                    fallback_active=bool(w.get("fallback_active", False)),
                    fallback_reason=w.get("fallback_reason"),
                    fallback_title=w.get("fallback_title", "") or "",
                ),
            )
        jm.prune_worker_statuses(keys)
        jm.emit_worker_statuses()

    return update


def run_intro_credits_job(job_id: str) -> None:
    """Run the same job across availability windows without retaining parked queues."""
    while _run_intro_credits_pass(job_id):
        pass


@dataclasses.dataclass
class _PassState:
    """What the teardown of one job pass needs, kept as the pass goes (it can end at any step).

    Attributes:
        cfg: The job's config; replaced when the files are sealed or the sender hints merged.
        slot: Whether the pass holds the job gate's slot (``release_slot`` clears it).
        dispatcher: Set once the pool exists, so a failure can stop the files still running.
        sweep_store: Set once the job completes: the fingerprint cache sweep starts after the slot is given back.
        sweep_configs: The servers' configs for the deleted-file sweep that runs first.
        listing_on_job: Whether the config holds the Check servers listing (a revived job's from the start): only a
            revive needs it, so the teardown takes it off the ended job (the config ships in every job payload).
        parked: Whether the job parked itself (its listing is kept for the revive).
    """

    cfg: dict
    slot: dict = dataclasses.field(default_factory=lambda: {"held": False})
    dispatcher: object | None = None
    sweep_store: MarkerStore | None = None
    sweep_configs: list[ServerConfig] = dataclasses.field(default_factory=list)
    listing_on_job: bool = False
    parked: bool = False


def _run_intro_credits_pass(job_id: str) -> bool | None:
    """Run one Intro & Credits job to completion (called on its own thread).

    Args:
        job_id: The job to run.

    Returns:
        True when the job parked itself and should run again after its availability window.
    """
    from loguru import logger as loguru_logger

    jm = get_job_manager()
    job = jm.get_job(job_id)
    if job is None:
        return None
    if (job.config or {}).get("is_retry_chain"):
        # Its hidden retry job runs the files still waiting; a resume that starts every pending job mustn't run the
        # whole job again.
        logger.info("Intro & Credits job {} not started — its retry runs the files still waiting", job_id)
        return None
    settings = get_settings_manager()
    if settings.processing_paused:
        logger.info("Intro & Credits job {} not started — processing is paused; job stays pending", job_id)
        return None
    register_job_thread(job_id)
    handler_id = loguru_logger.add(
        lambda message: jm.add_log(job_id, f"{message.record['level'].name} - {message.record['message']}"),
        level=str(settings.get("log_level", "INFO")).upper(),
        format="{message}",
        filter=lambda record: not record["extra"].get(JOB_LOG_SKIP) and is_job_thread_for(record["thread"].id, job_id),
        enqueue=True,
    )
    cfg = dict(job.config or {})
    state = _PassState(cfg=cfg, listing_on_job=LISTING_CONFIG_KEY in cfg)

    def cancel_check() -> bool:
        return jm.is_cancellation_requested(job_id)

    def live_priority() -> int:
        # ``job`` is the job manager's own object, which the priority route updates in place.
        return job.priority

    try:
        with failure_scope(job_id):
            try:
                _run_job(jm, job_id, job, settings, state, cancel_check, live_priority)
            finally:
                clear_failures()
    except JobParked:
        state.parked = True
        return True
    except Exception as exc:
        _fail_job(jm, job_id, exc, state.dispatcher)
    finally:
        _tear_down_pass(jm, job_id, state, handler_id, loguru_logger)
    return None


def _fail_job(jm, job_id: str, exc: Exception, dispatcher) -> None:
    """Log a job's failure, stop the files it still has running and mark it failed.

    Args:
        jm: The job manager.
        job_id: The failed job.
        exc: What the job raised.
        dispatcher: The job's dispatcher, or None when it ended before submitting its files.
    """
    # The job's error is served by GET /api/jobs, and exception text can carry a server URL with its token.
    detail = redact_secrets(f"{type(exc).__name__}: {exc}")
    logger.error("Intro & Credits job {} failed: {}", job_id, detail)
    logger.bind(**{JOB_LOG_SKIP: True}).error(
        "Traceback of Intro & Credits job {}:\n{}", job_id, redacted_traceback(exc)
    )
    if dispatcher is not None:
        # Its checks would otherwise go on publishing for a failed job, without a slot or Files-panel rows.
        try:
            dispatcher.cancel_job(job_id)
        except Exception as cancel_exc:
            logger.warning("Could not stop the remaining files of Intro & Credits job {}: {}", job_id, cancel_exc)
    try:
        jm.complete_job(job_id, error=detail)
    except Exception as complete_exc:
        logger.warning("Could not mark Intro & Credits job {} failed: {}", job_id, complete_exc)


def _tear_down_pass(jm, job_id: str, state: _PassState, handler_id: int, loguru_logger) -> None:
    """End a job pass: give the slot back, clear its per-job flags, start the cache sweep, drop its log handler.

    Args:
        jm: The job manager.
        job_id: The job that ran.
        state: What the pass kept as it went.
        handler_id: The job log handler to remove.
        loguru_logger: The loguru logger the handler was added to.
    """
    # Same teardown as the preview runner (web/routes/job_runner.py run_job finally): slot first (a job that
    # ended before submitting its files still holds one), then per-job flags, then worker cards once nothing
    # else is running.
    release_slot(state.slot, get_job_gate())
    set_file_result_callback(None, job_id=job_id)
    jm.clear_pause_flag(job_id)
    jm.clear_cancellation_flag(job_id)
    jm.clear_active_worker_pool(job_id)
    if state.listing_on_job and not state.parked:
        try:
            jm.merge_job_config(job_id, {}, remove=(LISTING_CONFIG_KEY,))
        except Exception as exc:
            logger.debug("Could not drop the Check servers listing of {}: {}", job_id, exc)
    try:
        if not jm.get_running_jobs():
            jm.clear_worker_statuses()
    except Exception as exc:
        logger.debug("Could not clear worker statuses after {}: {}", job_id, exc)
    unregister_job_thread()
    # After the slot is back, and outside the job's log: a skipped cleanup's warning isn't about this job.
    if state.sweep_store is not None:
        _start_fingerprint_sweep(state.cfg, state.sweep_store, state.sweep_configs)
    try:
        loguru_logger.complete()
        loguru_logger.remove(handler_id)
    except (ValueError, TypeError):
        logger.debug("Could not remove the job log handler for {}", job_id)


def _wait_for_job_slot(
    jm,
    job_id: str,
    job,
    cfg: dict,
    state: _PassState,
    cancel_check: Callable[[], bool],
    live_priority: Callable[[], int],
) -> bool:
    """Wait for the job's turn: the job it follows, its retry time, a pause, then capacity and a gate slot.

    Args:
        jm: The job manager.
        job_id: The job waiting.
        job: The job manager's job object.
        cfg: The job's config.
        state: The pass state; ``slot`` is held on a True return.
        cancel_check: True once the job is cancelled.
        live_priority: The job's current priority.

    Returns:
        True with the slot held; False when the job was cancelled while waiting (it is already marked cancelled).
    """

    def on_wait(active: int) -> None:
        jm.update_progress(job_id, current_item=format_wait_message(active))
        jm.note_slot_wait(job_id)  # JobManager.requeue_interrupted_jobs ages a waiting job by the downtime only

    if not wait_for_preceding_job(job_id, cfg.get("follows_job_id"), cancel_check):
        jm.add_log(job_id, "WARNING - Job cancelled while waiting for its preview job")
        jm.cancel_job(job_id)
        return False
    if not wait_for_retry_time(job_id, cfg, cancel_check):
        jm.add_log(job_id, "WARNING - Job cancelled while waiting to retry")
        jm.cancel_job(job_id)
        return False
    if job.paused and not hold_pause_from_before_restart(job_id, cancel_check):
        jm.add_log(job_id, "WARNING - Job cancelled while paused")
        jm.cancel_job(job_id)
        return False
    # The first job of a process pays for these (ffmpeg listing its muxers, up to 30 s of the text
    # detection check), so they run before the job holds a slot; build_context reads the answers they keep.
    # None: the ffmpeg load_config picks is one of the same candidates (``fingerprint._ffmpeg_candidates``).
    run_detector_checks(None)
    while True:
        if not wait_for_capacity(
            jm,
            job_id,
            JOB_KIND_INTRO_CREDITS,
            cancel_check,
            lambda: jm.is_pause_requested(job_id) or get_settings_manager().processing_paused,
        ):
            jm.cancel_job(job_id)
            return False
        jm.note_slot_wait(job_id)
        if not get_job_gate().acquire(
            priority=live_priority(),
            cancel_check=cancel_check,
            on_wait=on_wait,
            **admission_options(jm, job_id, JOB_KIND_INTRO_CREDITS),
        ):
            jm.cancel_job(job_id)
            return False
        state.slot["held"] = True
        if (
            runtime_capacity(JOB_KIND_INTRO_CREDITS)["open"]
            and not jm.is_pause_requested(job_id)
            and not get_settings_manager().processing_paused
        ):
            return True
        release_slot(state.slot, get_job_gate())


def _list_files(
    *,
    jm,
    job_id: str,
    state: _PassState,
    ctx: PipelineContext,
    registry,
    checkpoint: dict | None,
    saved: dict,
    cancel_check: Callable[[], bool],
    progress_callback: Callable[..., None],
) -> tuple[list[ProcessableItem], list[str], dict[str, str], CheckServersListing | None]:
    """List the files the job runs: a parked job's saved ones, a Check servers listing, or the job's own selection.

    Args:
        jm: The job manager.
        job_id: The job listing its files.
        state: The pass state; ``cfg`` takes the merged sender hints and ``listing_on_job`` is set when a listing is
            saved on the job.
        ctx: The job's pipeline context.
        registry: The media servers registry.
        checkpoint: The parked job's checkpoint, if it is being revived.
        saved: The bookkeeping stored in the checkpoint.
        cancel_check: True once the job is cancelled.
        progress_callback: Reports the listing's progress.

    Returns:
        The items, the warnings, the sender paths and the Check servers listing (None for other jobs).
    """
    cfg = state.cfg
    listing = None
    if checkpoint:
        items, warnings, sender_paths = (
            checkpoint_items(checkpoint),
            saved.get("warnings", []),
            saved.get("sender_paths", {}),
        )
        listing = CheckServersListing.from_config(cfg.get(LISTING_CONFIG_KEY)) if cfg.get("reconcile") else None
    elif cfg.get("reconcile"):
        from .reconcile import check_servers_listing

        # A run revived after a restart checks the files its first run listed, without reading every
        # server back again; the checks of the files it had finished were used then (count_checked).
        listing = CheckServersListing.from_config(cfg.get(LISTING_CONFIG_KEY))
        if listing is None and LISTING_CONFIG_KEY in cfg:
            logger.warning("Check servers couldn't read the files it listed before the restart; listing them again")
        if listing is None:
            listing = check_servers_listing(
                registry=registry,
                store=ctx.store,
                max_files=MAX_RETRY_FILES,
                capability=lambda server_cfg, publisher: cached_capability(ctx, server_cfg, publisher),
                cancel_check=_cancel_check_waiting_out_pause(
                    job_id=job_id,
                    cancel_check=cancel_check,
                    on_pause=lambda: release_slot(state.slot, get_job_gate()),
                ),
                progress_callback=progress_callback,
            )
            if not cancel_check() and jm.merge_job_config(job_id, {LISTING_CONFIG_KEY: listing.to_config()}):
                state.listing_on_job = True
        items, warnings, sender_paths = listing.items, listing.warnings, {}
    else:
        items, warnings, sender_paths = build_items(
            cfg, registry=registry, cancel_check=cancel_check, progress_callback=progress_callback
        )
        state.cfg = _with_merged_sender_hints(cfg, items, sender_paths)
    return items, warnings, sender_paths, listing


def _file_result_callback(
    *,
    jm,
    job_id: str,
    chain_head: str | None,
    ctx: PipelineContext,
    registry,
    listing: CheckServersListing | None,
    retries_missing_files: bool,
    waiting: dict[str, set[str]],
    replaced: set[str],
    unchecked: dict[str, set[str]],
    gone_items: dict[str, set[tuple]],
    cancel_check: Callable[[], bool],
) -> Callable:
    """Build the callback that records each finished file: its Files-panel row and what the job must come back to.

    Args:
        jm: The job manager.
        job_id: The job.
        chain_head: The retry chain's head job whose row takes the files, if the job is a retry.
        ctx: The job's pipeline context.
        registry: The media servers registry.
        listing: The Check servers listing, if the job is one.
        retries_missing_files: Whether files not on disk wait for a retry.
        waiting: Files waiting for a retry, by reason (filled in by the callback).
        replaced: Files whose replacement a later check must verify (filled in).
        unchecked: Files a server couldn't be read back for, by server name (filled in).
        gone_items: Items a server confirmed gone, by file (filled in).
        cancel_check: True once the job is cancelled.

    Returns:
        The ``on_file_result`` callback.
    """

    def on_file_result(file_path, outcome, reason, worker, servers=None):
        # Any server that can take the file later, even when another server was written. Check servers
        # retries only a write Plex's busy database refused or a file another job kept running (its next
        # run is a day away) and the files whose old item a server confirmed gone (below).
        codes = {code for row in servers or [] if (code := retry_reason(row))}
        if listing is not None:
            codes &= {PLEX_DB_BUSY, FILE_BUSY}
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
        # Before the row is kept: a restart in between runs the file again, which counts nothing twice.
        # A result that comes in after a cancel (the file stopped part way) leaves its checks due.
        if listing is not None and not cancel_check():
            listing.count_checked(ctx.store, file_path)
        jm.record_file_result(
            chain_head or job_id, file_path, outcome, reason, worker, servers=servers, server_messages=True
        )
        # The pipeline counted the file before handing its result here (``PipelineContext.decided_by``).
        jm.set_marker_sources(job_id, ctx.decided_by.snapshot())
        if listing is not None and (gone := _confirmed_gone_items(listing, registry, file_path, servers)):
            gone_items[file_path] = gone
            waiting.setdefault(NOT_IN_LIBRARY, set()).add(file_path)

    return on_file_result


def _start_dispatcher(jm, settings, job_id: str, config, state: _PassState, ctx: PipelineContext):
    """Get the dispatcher, register its pool for the job and size the pool to the saved settings.

    Args:
        jm: The job manager.
        settings: The settings manager.
        job_id: The job.
        config: The loaded config.
        state: The pass state; ``dispatcher`` is set as soon as it exists.
        ctx: The job's pipeline context.

    Returns:
        The dispatcher.
    """
    # Detected before the settings lock below, so detection never runs while it's held.
    detected_gpus = _ensure_gpu_cache()
    selected_gpus = _build_selected_gpus(settings, detected=detected_gpus)
    dispatcher = state.dispatcher = get_or_create_dispatcher(config, selected_gpus)
    _start_decode_checks(ctx, config, selected_gpus)
    # The running job's pool for the per-job worker routes, as the preview runner registers it; complete_job
    # and cancel_job clear it.
    jm.set_active_worker_pool(job_id, dispatcher.worker_pool)
    # The saved settings, not the config read before the files were listed: a count saved while no pool
    # existed had nothing to resize. The pool is registered above, so a later save finds it. Read and
    # resize under the settings lock so a save landing in between (including one after the GPU
    # selection above was read) isn't undone.
    try:
        with settings.locked():
            selected_gpus = _build_selected_gpus(settings, detected=detected_gpus)
            if not refresh_worker_groups(dispatcher.worker_pool, config, selected_gpus):
                if selected_gpus:
                    dispatcher.worker_pool.reconcile_gpu_workers(selected_gpus)
                dispatcher.worker_pool.reconcile_cpu_workers(settings.cpu_threads)
    except Exception as exc:
        logger.debug("Could not reconcile the worker pool with the saved settings: {}", exc)
    return dispatcher


def _run_job(
    jm,
    job_id: str,
    job,
    settings,
    state: _PassState,
    cancel_check: Callable[[], bool],
    live_priority: Callable[[], int],
) -> None:
    """Wait for the job's slot, list its files, run them and finish the job.

    Args:
        jm: The job manager.
        job_id: The job to run.
        job: The job manager's job object.
        settings: The settings manager.
        state: The pass state the teardown reads.
        cancel_check: True once the job is cancelled.
        live_priority: The job's current priority.
    """
    cfg = state.cfg
    if not _wait_for_job_slot(jm, job_id, job, cfg, state, cancel_check, live_priority):
        return
    # The job's own start line (start_line) follows once its files are listed.
    with logger.contextualize(**{JOB_LOG_SKIP: True}):
        if cfg.get("parked_checkpoint") or (job.config or {}).get("resource_wait"):
            jm.resume_parked_job(job_id)
        else:
            jm.start_job(job_id)
    # A retry in a chain shows its run on the chain head's row, and records its files there (below).
    chain_head = cfg.get("parent_job_id")
    if chain_head:
        _upsert_chain(
            jm,
            chain_head,
            attempt=int(cfg.get("retry_attempt") or 0),
            max_attempts=int(cfg.get("max_retries") or 0),
            outcome="running",
        )

    def progress_callback(current, total, message, percent_override=None):
        if percent_override is not None:
            percent = percent_override
        else:
            percent = (current / total * 100) if total else 0
        jm.update_progress(job_id, percent=percent, processed_items=current, total_items=total, current_item=message)

    worker_callback = worker_cards(jm)

    config = load_config()
    # A job following a pinned preview job (and its retries and checks) publishes where the previews did.
    config.server_id_filter = server_pin(cfg)
    registry = _build_multi_server_registry()
    if registry is None:
        jm.complete_job(job_id, error="Couldn't load the media servers configuration")
        return
    cfg = state.cfg = _seal_files(jm, job_id, job, cfg)
    ctx = build_context(
        registry=registry,
        config=config,
        priority=live_priority,
        force=bool(cfg.get("force")),
        recheck_empty_server_markers=bool(cfg.get("reconcile")),
    )
    ctx.busy_writes_retried = _retry_follows(cfg)
    ctx.retry_file_cap = MAX_RETRY_FILES
    ctx.freeze_check = job_freeze_check(jm, job_id)
    state.sweep_configs = list(registry.configs())
    checkpoint = (
        read_checkpoint(jm.config_dir, job_id, cfg["parked_checkpoint"]) if cfg.get("parked_checkpoint") else None
    )
    saved = checkpoint.get("bookkeeping", {}) if checkpoint else {}
    if checkpoint:
        restore_context(ctx, saved.get("context", {}))
    items, warnings, sender_paths, listing = _list_files(
        jm=jm,
        job_id=job_id,
        state=state,
        ctx=ctx,
        registry=registry,
        checkpoint=checkpoint,
        saved=saved,
        cancel_check=cancel_check,
        progress_callback=progress_callback,
    )
    cfg = state.cfg
    if cancel_check():
        jm.cancel_job(job_id)
        return
    try:
        trigger = trigger_words(job, cfg)
    except Exception as exc:
        # A line describing the job mustn't end it.
        logger.debug("Couldn't describe what started job {}: {}", job_id, type(exc).__name__)
        trigger = ""
    logger.info("{}", start_line(job_id, len(items), trigger))
    logger.complete()  # the lines added straight to the job's log below come after it
    if not items:
        if listing is not None:
            jm.add_log(job_id, "INFO - Every server checked still shows what this app published")
            jm.complete_job(job_id, warning=" | ".join(warnings) or None)
        else:
            jm.complete_job(job_id, warning=" ".join(["No files to check.", *warnings]))
        state.sweep_store = ctx.store
        return
    sent_files = _sends_files(cfg)
    retries_missing_files = _retries_missing_files(cfg)
    # Only files just sent were just replaced: a listing's replaced file may have changed days ago, and
    # servers rescanned it long since. A verify chain checks once.
    checks_replaced_later = sent_files and not (cfg.get("verify") or cfg.get("verify_chain"))
    # A revived job still owes the later check of the replaced files it published before the restart.
    # A retry's rows are its chain head's.
    if checkpoint:
        carried: dict[str, int] = {}
        replaced_before_restart: set[str] = set()
        carried_outcomes: dict[str, str] = {}
    else:
        items, carried, replaced_before_restart, carried_outcomes = _skip_finished_before_restart(
            jm, chain_head or job_id, items, ctx.store
        )
    if carried_outcomes:
        # The pipeline never decides for a file without an owner; the store may still hold an old run's.
        for path, file_outcome in sorted(carried_outcomes.items()):
            if file_outcome != FileOutcome.NO_OWNERS.value:
                ctx.decided_by.add(stored_groups(ctx.store, path))
        jm.set_marker_sources(job_id, ctx.decided_by.snapshot())
    if not items:
        jm.set_job_outcome(job_id, carried)
        finish_job(jm, job_id, carried, warnings, ctx)
        if chain_head:
            # A retry revived after a restart that had settled all its files: nothing is left to wait.
            _recount_chain_head(jm, chain_head, ctx.store)
            _end_chain(jm, cfg, {})
        if replaced_before_restart and checks_replaced_later:
            _queue_verify(job, cfg, replaced_before_restart, sender_paths)
        state.sweep_store = ctx.store
        return
    waiting = {key: set(values) for key, values in saved.get("waiting", {}).items()}
    replaced = set(saved.get("replaced", replaced_before_restart))
    unchecked = {key: set(values) for key, values in saved.get("unchecked", {}).items()}
    gone_items = {key: {tuple(value) for value in values} for key, values in saved.get("gone_items", {}).items()}

    set_file_result_callback(
        _file_result_callback(
            jm=jm,
            job_id=job_id,
            chain_head=chain_head,
            ctx=ctx,
            registry=registry,
            listing=listing,
            retries_missing_files=retries_missing_files,
            waiting=waiting,
            replaced=replaced,
            unchecked=unchecked,
            gone_items=gone_items,
            cancel_check=cancel_check,
        ),
        job_id=job_id,
    )
    dispatcher = _start_dispatcher(jm, settings, job_id, config, state, ctx)
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
            "pause_check": lambda: jm.is_pause_requested(job_id) or get_settings_manager().processing_paused,
        },
        priority=live_priority(),
        kind=JOB_KIND_INTRO_CREDITS,
        handlers=kind_handlers(ctx),
        **({"carried_state": checkpoint["state"]} if checkpoint else {"carried_outcome": carried}),
    )
    release_slot(state.slot, get_job_gate())
    # The priority route skips the dispatcher while this job has no tracker yet; a change that landed
    # between reading the priority and registering the tracker would otherwise be lost.
    current = live_priority()
    if tracker.priority != current:
        dispatcher.update_job_priority(job_id, current)

    def park_check():
        park_if_unavailable(
            dispatcher,
            tracker,
            jm,
            job_id,
            JOB_KIND_INTRO_CREDITS,
            lambda: {
                "context": snapshot_context(ctx),
                "warnings": warnings,
                "sender_paths": sender_paths,
                "waiting": {key: sorted(values) for key, values in waiting.items()},
                "replaced": sorted(replaced),
                "unchecked": {key: sorted(values) for key, values in unchecked.items()},
                "gone_items": {key: sorted(values) for key, values in gone_items.items()},
            },
        )

    wait_for_tracker(tracker, cancel_check=cancel_check, park_check=park_check)
    result = tracker.get_result()
    _log_missing(ctx)
    outcome = dict(result["outcome"])  # includes the carried counts
    jm.set_job_outcome(job_id, outcome)
    # Worker threads store their snapshots in any order; the last one stored may not be the newest.
    jm.set_marker_sources(job_id, ctx.decided_by.snapshot())
    if chain_head and not (result["cancelled"] or cancel_check()):
        _recount_chain_head(jm, chain_head, ctx.store)
    if result["cancelled"] or cancel_check():
        jm.cancel_job(job_id)
        return
    # Those files stay "Up to date": a read failure mustn't rewrite them, but it mustn't go unseen either.
    unchecked_warnings = [
        f"Couldn't check what {len(files)} file(s) show on {name}" for name, files in sorted(unchecked.items())
    ]
    # The retry is queued before completing, as the preview runner does: its chain keeps the chain head's
    # row pending, so completing the head only settles its run's bookkeeping.
    # Check servers only waits for files whose old item a server confirmed gone: they get the retry a normal
    # job queues (once from here, the retry job counts on), and only then leave Check servers. Any other
    # file still waiting keeps its item and is listed again by a later run.
    retried = _queue_retry(job, cfg, waiting, sender_paths, ctx.busy_promised()) if waiting else []
    _mark_retried_items_gone(ctx.store, gone_items, retried, sender_paths)
    finish_job(
        jm,
        job_id,
        outcome,
        [*warnings, *unchecked_warnings, *budget_exhausted_warnings(ctx)],
        ctx,
        retried=bool(retried),
    )
    if chain_head and not retried:
        _end_chain(jm, cfg, waiting)
    if replaced and checks_replaced_later:
        _queue_verify(job, cfg, replaced, sender_paths)
    state.sweep_store = ctx.store


def start_intro_credits_job_async(job_id: str, config_overrides: dict | None = None) -> None:
    """Start the job on a daemon thread (a second start for a job already in flight is ignored).

    Args:
        job_id: The job to start.
        config_overrides: Keys merged into the job's config first (resume paths pass a snapshot of the job's own
            config); files that joined the job since the snapshot are kept.
    """
    queued = get_job_manager().get_job(job_id)
    if queued is None or (queued.config or {}).get("is_retry_chain"):
        return
    if config_overrides:
        jm = get_job_manager()
        # Resume paths pass a snapshot of the job's config; webhook episodes that joined it since are kept.
        # Only this branch takes the lock: create_intro_credits_job starts jobs without overrides,
        # and its caller submit_webhook_follow_up holds the (non-reentrant) lock then.
        with FOLLOW_UP_LOCK:
            job = jm.get_job(job_id)
            if job is not None:
                live = job.config or {}
                # Only the keys an override changes are written, so a key another thread sets meanwhile (a stop-time
                # pause) stays.
                updates = {
                    key: value
                    for key, value in config_overrides.items()
                    if key not in {"pause_reasons", "paused_by_schedule", "resource_wait", "parked_checkpoint"}
                    and not (key in _JOINED_KEYS and key in live)
                    and (key not in live or live[key] != value)
                }
                if updates:
                    jm.merge_job_config(job_id, updates)
    with _inflight_lock:
        if job_id in _inflight_jobs:
            logger.info("Skipping duplicate Intro & Credits start for {} — already in flight", job_id)
            return
        _inflight_jobs.add(job_id)

    from ..jobs.admission import claim_admission, finish_admission

    try:
        admission_owner = claim_admission(get_job_manager(), job_id, job=queued)
    except BaseException:
        finish_admission(job_id, None)
        with _inflight_lock:
            _inflight_jobs.discard(job_id)
        raise

    def _run() -> None:
        try:
            run_intro_credits_job(job_id)
        finally:
            finish_admission(job_id, admission_owner)
            with _inflight_lock:
                _inflight_jobs.discard(job_id)

    try:
        threading.Thread(target=_run, daemon=True, name=f"run_job_intro_credits_{job_id}").start()
    except BaseException:
        finish_admission(job_id, admission_owner)
        with _inflight_lock:
            _inflight_jobs.discard(job_id)
        raise
