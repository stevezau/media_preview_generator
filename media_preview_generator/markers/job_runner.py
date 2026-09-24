"""Intro & Credits job thread: gate → enumerate/resolve files → shared dispatcher with marker handlers → complete."""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from loguru import logger

from ..config import load_config
from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..jobs.dispatcher import get_or_create_dispatcher
from ..jobs.orchestrator import _build_multi_server_registry
from ..jobs.worker import JOB_LOG_SKIP, is_job_thread_for, register_job_thread, unregister_job_thread
from ..processing.generator import clear_failures, failure_scope, set_file_result_callback
from ..processing.retry_queue import BACKOFF_SCHEDULE, retry_policy
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig
from ..utils import redact_secrets, redacted_traceback
from ..web.job_gate import format_wait_message, get_job_gate
from ..web.jobs import (
    PAUSED_BY_SCHEDULE,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    JobStatus,
    WorkerStatus,
    get_job_manager,
    is_live_retry_chain,
)
from ..web.routes._helpers import _ensure_gpu_cache
from ..web.routes.job_runner import (
    _build_selected_gpus,
    _format_eta,
    _inflight_jobs,
    _inflight_lock,
    _is_force_fire_now_set,
)
from ..web.settings_manager import get_settings_manager
from .audio.fingerprint import start_fingerprint_sweep
from .audio.season import season_audio_answer_outdated
from .decide import DecisionStatus
from .external_ids import is_season_folder
from .job_log import BUDGET_RECHECK_LABEL, SEASON_RECHECK_LABEL
from .missing import MISSING_LINE, mark_missing_files
from .models import Source
from .outcomes import (
    NOT_IN_LIBRARY,
    PLEX_DB_BUSY,
    PLEX_PASS_UNKNOWN,
    READ_BACK_FAILED,
    RETRY_REASON_CODES,
    VERIFY_LATER,
    VERSIONS_UNCHECKED,
    FileOutcome,
    ServerStatus,
)
from .ownership import marker_libraries
from .pipeline import (
    PipelineContext,
    budget_exhausted_warnings,
    build_context,
    cached_capability,
    kind_handlers,
    online_recheck_files,
    sequence_number,
)
from .reconcile import LISTING_CONFIG_KEY, RECONCILE_SOURCE, CheckServersListing
from .settings import load_server
from .source_counts import DecidedByTally, stored_groups
from .sources.ratelimit import RESET_TIME_LABEL
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
_FINISHED = frozenset({JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED})
# Job sources where the user chose the files (API/Start job dialog, Inspector re-detect, Season view Publish).
_USER_PICKED_SOURCES = frozenset({"manual", "inspector", "inspector_season"})
SEASON_SOURCE = "season"
# A job that checks files again once TheIntroDB's daily budget has reset (``_queue_budget_recheck``).
BUDGET_RECHECK_SOURCE = "theintrodb_recheck"
# How long after the UTC day roll that job starts, so the limiter and TheIntroDB have both started the new day.
BUDGET_RECHECK_AFTER_RESET = timedelta(minutes=5)
# The one-off job that decides files again after an upgrade changed the rules (``triggers.submit_decide_again``): its
# source, and the config key that lists those files when it runs (``_items_to_decide_again``).
DECIDE_AGAIN_SOURCE = "decide_again"
DECIDE_AGAIN = "decide_again"
# The weekly job that asks the online databases again about files they had no entry for
# (``triggers.submit_online_recheck``): its source, and the config key that lists those files when it runs
# (``pipeline.online_recheck_files``).
ONLINE_RECHECK_SOURCE = "online_recheck"
ONLINE_RECHECK = "online_recheck"
# A follow-up's config key once its runner has read its files: nothing joins it after that.
FILES_SEALED = "files_sealed"
# A running Season job's config keys: the episodes other jobs asked for while it ran, each with the
# ``pipeline.sequence_number`` of the request, and, once it has passed them on, the seal that stops more joining.
LATE_REQUESTS = "late_requests"
LATE_SEALED = "late_requests_sealed"
# Serialises adding files to a waiting follow-up (webhook episodes in triggers.py, Season requests here) with its runner
# reading them, so a file joins exactly one job.
FOLLOW_UP_LOCK = threading.Lock()
# Config keys files join a job through (``_queue_season_followups``, ``triggers._join``) or its seals write.
_JOINED_KEYS = ("file_paths", "webhook_item_id_hints", FILES_SEALED, LATE_REQUESTS, LATE_SEALED)
# Job sources whose files no sender just reported: a file missing from disk won't appear by waiting (and nothing was
# just replaced). A retry Check servers queued is one of them; its not-in-library retries still chain.
_NO_RETRY_SOURCES = _USER_PICKED_SOURCES | {
    SEASON_SOURCE,
    RECONCILE_SOURCE,
    BUDGET_RECHECK_SOURCE,
    DECIDE_AGAIN_SOURCE,
    ONLINE_RECHECK_SOURCE,
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def retry_reason(row: object) -> str | None:
    """Why a per-server row's file is worth trying again later, if it is.

    Args:
        row: One of the pipeline's per-server rows.

    Returns:
        The reason code of a waiting row the job retries (the server hasn't indexed the file yet, Plex didn't answer
        its Plex Pass check, or another version of its Plex item hasn't been checked yet), or ``PLEX_DB_BUSY`` for a
        failed row whose write gave up waiting for Plex's database; None for any other row.
    """
    if not isinstance(row, dict):
        return None
    code = row.get("reason_code")
    if row.get("status") == ServerStatus.FAILED.value:
        return code if code == PLEX_DB_BUSY else None
    if row.get("status") != ServerStatus.WAITING.value:
        return None
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
    VERSIONS_UNCHECKED: "with another version not checked",
    PLEX_DB_BUSY: "not written to Plex's busy database",
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
    {FileOutcome.FAILED.value, FileOutcome.NO_OWNERS.value, FileOutcome.FILE_NOT_FOUND.value}
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
    job, cfg: dict, waiting: dict[str, set[str]], sender_paths: dict[str, str], promised: set[str] = frozenset()
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
    paths = [*first, *(path for path in _sent_paths(waiting_paths, sender_paths) if path not in set(first))]
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
        delay = retry_delay_s(attempt, delay_setting)
        base_name = (job.library_name or "Intro & Credits").removeprefix("Retry: ").removeprefix("Verify: ")
        # A retry's own retry joins the same chain; any other job (an old top-level "Retry:" job included) heads one.
        head_id = cfg.get("parent_job_id") or job.id
        retry = create_intro_credits_job(
            library_name=f"Retry: {base_name}",
            priority=job.priority,
            source=str(cfg.get("source") or "retry"),
            file_paths=paths,
            item_id_hints=_hints_for(cfg, paths) or None,
            retry_attempt=attempt,
            retry_delay_s=delay,
            verify_chain=bool(cfg.get("verify") or cfg.get("verify_chain")),
            parent_job_id=head_id,
            max_retries=count,
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


def _season_job_name(paths: list[str]) -> str:
    folders = sorted({os.path.dirname(p) for p in paths})
    if len(folders) > 1:
        return f"Season: {len(folders)} seasons"
    name = os.path.basename(folders[0])
    if is_season_folder(name):
        return f"Season: {os.path.basename(os.path.dirname(folders[0]))} · {name}"
    return f"Season: {name}"


def _is_season_job_taking_files(job) -> bool:
    cfg = job.config or {}
    if job.kind != JOB_KIND_INTRO_CREDITS or cfg.get("source") != SEASON_SOURCE:
        return False
    return not (cfg.get("retry_attempt") or cfg.get("verify"))


def _waiting_season_jobs(jm) -> list:
    """Season jobs whose runner hasn't read their files yet (call under ``FOLLOW_UP_LOCK``).

    A file they list is decided with everything known when they run, so asking for it again adds nothing.
    """
    return [
        job
        for job in jm.get_pending_jobs()
        if _is_season_job_taking_files(job) and not (job.config or {}).get(FILES_SEALED)
    ]


def _running_season_jobs(jm, priority: int) -> list:
    """Running Season jobs at ``priority`` that still take requests (call under ``FOLLOW_UP_LOCK``).

    Season jobs start within milliseconds of being queued, so a request that only joined waiting ones would queue a new
    Season job for every job that finishes while one runs (spec §14, 2026-09-24).
    """
    return [
        job
        for job in jm.get_running_jobs()
        if _is_season_job_taking_files(job) and job.priority == priority and not (job.config or {}).get(LATE_SEALED)
    ]


def _leave_to_running_season_jobs(jm, job, paths: list[str], priority: int) -> list[str]:
    """Hand each file to a running Season job at ``priority`` that lists an episode of its folder (call under
    ``FOLLOW_UP_LOCK``). The Season job runs it again once it has finished, in one follow-up, only when its own run of
    the file started before this request (``_pass_on_late_requests``).

    Returns:
        The files no running Season job took.
    """
    left = list(paths)
    for season_job in _running_season_jobs(jm, priority):
        cfg = season_job.config or {}
        late = dict(cfg.get(LATE_REQUESTS) or {})
        folders = {os.path.dirname(path) for path in [*(cfg.get("file_paths") or []), *late]}
        room = MAX_RETRY_FILES - len(late)
        taken = [path for path in left if os.path.dirname(path) in folders][: max(0, room)]
        if not taken:
            continue
        number = sequence_number()
        late.update(dict.fromkeys(taken, number))
        if not jm.merge_job_config(season_job.id, {LATE_REQUESTS: late}):
            continue
        jm.add_log(
            job.id,
            f"INFO - {len(taken)} episode(s) of the same season are left to the running Season job "
            f"{season_job.id[:8]}, which checks them with this job's results",
        )
        taken_set = set(taken)
        left = [path for path in left if path not in taken_set]
        if not left:
            break
    return left


def _queue_season_followups(job, paths: list[str], *, priority: int | None = None) -> None:
    """Queue episodes of this job's seasons to be decided again (spec §5.3). Never raises.

    A running Season job at the job's Season priority that lists an episode of a file's folder takes the file (it runs
    the file again after it finishes when its own run came first: ``_pass_on_late_requests``); one waiting Season job at
    that priority takes the rest when they fit; a file a waiting Season job (at any priority) or a webhook follow-up
    that has never started already lists isn't queued again: it reads the file after this job's work. Season jobs run
    at LOW, or at NORMAL for a webhook follow-up (the webhook rule), never ahead of the job that asked; a webhook
    follow-up's retry or verify job queues its Season job at LOW.

    Args:
        job: The job that just finished.
        paths: Files its season steps asked about that weren't its own items, and its own items whose season audio
            answer left out a sibling it read again later (``_queue_season_followups_after``).
        priority: The Season priority; None works it out from ``job`` as above (a Season job passing on requests
            gives its own).
    """
    jm = get_job_manager()
    try:
        from .triggers import _queued_in_waiting_follow_ups, create_intro_credits_job

        cfg = job.config or {}
        if priority is None:
            priority = max(PRIORITY_NORMAL, job.priority) if cfg.get("follows_job_id") else PRIORITY_LOW
        with FOLLOW_UP_LOCK:
            waiting = _waiting_season_jobs(jm)
            queued = {path for season_job in waiting for path in season_job.config.get("file_paths") or []}
            queued |= _queued_in_waiting_follow_ups(jm)
            fresh = sorted(set(paths) - queued)
            if not fresh:
                jm.add_log(job.id, f"INFO - {len(paths)} episode(s) of the same season are already queued")
                return
            fresh = _leave_to_running_season_jobs(jm, job, fresh, priority)
            if not fresh:
                return
            chosen = fresh[:MAX_RETRY_FILES]
            if len(fresh) > len(chosen):
                jm.add_log(
                    job.id, f"INFO - {len(fresh) - len(chosen)} more episode(s) are decided again on their own next run"
                )
            target = next(
                (
                    season_job
                    for season_job in waiting
                    if season_job.priority == priority
                    and len(season_job.config.get("file_paths") or []) + len(chosen) <= MAX_RETRY_FILES
                ),
                None,
            )
            if target is not None:
                files = sorted([*(target.config.get("file_paths") or []), *chosen])
                # Refused when the job was cancelled since it was listed: the files get a new job instead.
                if jm.update_job_config_if_pending(target.id, {**target.config, "file_paths": files}):
                    jm.update_job_library_name(target.id, _season_job_name(files))
                else:
                    target = None
            if target is None:
                target = create_intro_credits_job(
                    library_name=_season_job_name(chosen), priority=priority, source=SEASON_SOURCE, file_paths=chosen
                )
        jm.add_log(
            job.id,
            f"INFO - {len(chosen)} episode(s) of the same season are checked again with this job's results "
            f"(job {target.id[:8]})",
        )
    except Exception:
        logger.exception("Could not queue the season follow-up for Intro & Credits job {}", job.id)


def _pass_on_late_requests(job, ctx: PipelineContext | None) -> None:
    """A Season job that ended: stop taking requests, and queue one follow-up for the files other jobs asked for while it
    ran that its own run of them didn't read (the run started before the request, or there was none). Only the first
    call does anything. Never raises.

    The requesting jobs handed those files over and queue nothing for them themselves, so a job that didn't finish
    (cancelled, failed, or not revived after a restart) passes every one of them on.

    Args:
        job: The Season job.
        ctx: Its context (``PipelineContext.ran_since``); None when the job didn't finish.
    """
    jm = get_job_manager()
    try:
        with FOLLOW_UP_LOCK:
            latest = jm.get_job(job.id)
            config = (latest.config or {}) if latest is not None else {}
            if config.get(LATE_SEALED):
                return
            jm.merge_job_config(job.id, {LATE_SEALED: True})
        late = config.get(LATE_REQUESTS)
        if not isinstance(late, dict) or not late:
            return
        again = sorted(path for path, number in late.items() if ctx is None or not ctx.ran_since(path, int(number)))
        if ctx is None:
            jm.add_log(
                job.id,
                f"INFO - This job didn't finish: the {len(again)} episode(s) other jobs asked for while it ran go to "
                "another Season job",
            )
        if again:
            _queue_season_followups(job, again, priority=job.priority)
        else:
            jm.add_log(
                job.id, f"INFO - The {len(late)} episode(s) other jobs asked for while this job ran were checked after"
            )
    except Exception:
        logger.exception("Could not pass on what other jobs asked Season job {} for", job.id)


def pass_on_requests_of_unrevived_jobs(jobs: list) -> None:
    """Season jobs a restart left behind and marked failed: queue what other jobs had handed them. Never raises.

    Args:
        jobs: The jobs ``JobManager.fail_unrevived_interrupted_jobs`` marked failed.
    """
    for job in jobs:
        if (job.config or {}).get("source") == SEASON_SOURCE:
            _pass_on_late_requests(job, None)


def _queue_season_followups_after(job, cfg: dict, ctx, listed: set[str]) -> None:
    """Queue the Season job for the files this job's season steps asked about that weren't its items, and for its own
    items whose season audio answer left out a sibling changed on disk that the job read again after them.

    A Season job queues none of its own: a sibling whose answer is still out of date is asked for again by the season's
    next run, so nothing loops. It only passes on what other jobs asked it for while it ran (``_pass_on_late_requests``).
    A retry whose runs changed nothing stored (``PipelineContext.answers_changed``) queues none either: whatever its
    season steps found out of date was so before it ran, and the job that made it so queued it then.
    """
    if cfg.get("source") == SEASON_SOURCE:
        _pass_on_late_requests(job, ctx)
        return
    paths = [path for path in ctx.take_followups() if path not in listed]
    for path in ctx.take_changed_siblings_left_out():
        if path in paths:
            continue
        try:
            outdated = season_audio_answer_outdated(ctx, path)
        except Exception as exc:
            # The job has completed: a failed read only leaves this episode to its next run.
            logger.warning("Couldn't check whether {} needs its season asked again: {}", path, type(exc).__name__)
            continue
        if outdated:
            paths.append(path)
    if not paths:
        return
    if cfg.get("retry_attempt") and not ctx.answers_changed():
        get_job_manager().add_log(
            job.id,
            f"INFO - {len(paths)} episode(s) of the same season aren't checked again: this retry changed nothing",
        )
        return
    _queue_season_followups(job, paths)


def budget_recheck_due(refused_at: datetime) -> datetime:
    """When files TheIntroDB's used-up daily budget refused at ``refused_at`` are checked again.

    Args:
        refused_at: When (UTC) the first of them was refused.

    Returns:
        Shortly after the next UTC day roll, when the budget resets.
    """
    next_day = (refused_at + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return next_day + BUDGET_RECHECK_AFTER_RESET


def _waiting_budget_recheck(jm):
    """The recheck job still counting down to the next reset, if any (call under ``FOLLOW_UP_LOCK``).

    One already due (waiting for a slot) runs before the budget resets again: files refused since then would only be
    refused again, so they get a new job.
    """
    now = _utcnow()
    for job in jm.get_pending_jobs():
        cfg = job.config or {}
        if job.kind != JOB_KIND_INTRO_CREDITS or cfg.get("source") != BUDGET_RECHECK_SOURCE or cfg.get(FILES_SEALED):
            continue
        try:
            due = datetime.fromisoformat(cfg.get("retry_not_before") or "")
        except (TypeError, ValueError):
            continue
        if due > now:
            return job
    return None


def _queue_budget_recheck(job, ctx) -> None:
    """Queue the files this job checked without TheIntroDB, because its daily budget had run out, that it left with a
    type undecided: they're checked again just after the budget resets at 00:00 UTC. Never raises.

    One waiting recheck job (LOW, like a backfill) takes the files of every job that ran out that day, up to
    ``MAX_RETRY_FILES``; it lists each file only if TheIntroDB is still on and the file still has a type undecided when
    it runs (``_budget_recheck_items``). Its own files that run out again are queued for the next reset the same way.

    Args:
        job: The job that just finished.
        ctx: Its pipeline context.
    """
    jm = get_job_manager()
    try:
        paths, refused_at = ctx.take_budget_rechecks()
        if not paths or refused_at is None or not ctx.settings.source_enabled(Source.THEINTRODB.value):
            return
        from .triggers import create_intro_credits_job

        with FOLLOW_UP_LOCK:
            target = _waiting_budget_recheck(jm)
            listed = list((target.config or {}).get("file_paths") or []) if target is not None else []
            fresh = [path for path in paths if path not in listed]
            room = MAX_RETRY_FILES - len(listed)
            chosen, left_out = fresh[: max(0, room)], fresh[max(0, room) :]
            if target is not None and chosen:
                files = [*listed, *chosen]
                # Refused when the job was cancelled since it was listed: the files get a new job instead.
                if jm.update_job_config_if_pending(target.id, {**target.config, "file_paths": files}):
                    jm.update_job_library_name(target.id, f"TheIntroDB recheck: {len(files)} files")
                else:
                    target = None
            if target is None and chosen:
                due = budget_recheck_due(refused_at)
                target = create_intro_credits_job(
                    library_name=f"TheIntroDB recheck: {len(chosen)} files",
                    priority=PRIORITY_LOW,
                    source=BUDGET_RECHECK_SOURCE,
                    file_paths=chosen,
                    retry_delay_s=max(0, int((due - _utcnow()).total_seconds())),
                )
        if chosen:
            jm.add_log(
                job.id,
                f"INFO - {len(chosen)} file(s) checked without TheIntroDB (daily limit reached) are checked again after "
                f"it resets at {RESET_TIME_LABEL} (job {target.id[:8]})",
            )
        if left_out:
            jm.add_log(
                job.id,
                f"INFO - {len(left_out)} more file(s) checked without TheIntroDB aren't checked again automatically; "
                "the next run of their library checks them",
            )
    except Exception:
        logger.exception("Could not queue the TheIntroDB recheck for Intro & Credits job {}", job.id)


def _still_undecided(store: MarkerStore, path: str) -> bool:
    """Whether a file has a type left undecided (True when the store knows no decisions for it)."""
    rec = store.get_file(path)
    decisions = store.get_decisions(rec.id) if rec is not None else {}
    return not decisions or any(
        row.status in (DecisionStatus.NEEDS_REVIEW, DecisionStatus.NO_EVIDENCE) for row in decisions.values()
    )


def _budget_recheck_items(job_id: str, ctx, items: list[ProcessableItem]) -> list[ProcessableItem]:
    """A recheck job's files that are still worth TheIntroDB's answer: none once TheIntroDB is off, and only files
    another job hasn't decided since."""
    jm = get_job_manager()
    if not ctx.settings.source_enabled(Source.THEINTRODB.value):
        jm.add_log(job_id, "INFO - TheIntroDB is turned off now, so these files aren't checked again")
        return []
    still = [item for item in items if _still_undecided(ctx.store, item.canonical_path)]
    if len(still) < len(items):
        jm.add_log(job_id, f"INFO - {len(items) - len(still)} file(s) were decided since; they aren't checked again")
    return still


def _seal_files(jm, job_id: str, job, cfg: dict) -> dict:
    """The config whose files the job lists now: re-read and sealed when other requests may have added files to it.

    Episodes of a season join a webhook follow-up, Season requests join a Season job, and files TheIntroDB's budget
    refused join the waiting recheck job, while it waits; the read and the seal happen under the lock those additions
    take, so a file added to a job is listed by it and nothing is added once it has read its files. (A file can still be
    listed by two jobs, e.g. a Season job and a started follow-up.)

    Returns:
        The config to list the files of.
    """
    if not (cfg.get("follows_job_id") or cfg.get("source") in (SEASON_SOURCE, BUDGET_RECHECK_SOURCE)):
        return cfg
    with FOLLOW_UP_LOCK:
        # Only the seal is written (under the job manager's lock): a key another thread set meanwhile stays.
        jm.merge_job_config(job_id, {FILES_SEALED: True})
        latest = jm.get_job(job_id) or job
        return {**(latest.config or {}), FILES_SEALED: True}


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
    elif cfg.get("source") == BUDGET_RECHECK_SOURCE:
        waiting_for = f"Check starting in {remaining}s — after TheIntroDB's daily limit resets at {RESET_TIME_LABEL}"
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
        # The chain head's "Retry now" (POST /api/jobs/<head>/retry-now) flags this retry, as for preview retries.
        if _is_force_fire_now_set(jm, job_id):
            jm.add_log(job_id, "INFO - Retry backoff skipped — operator forced fire-now")
            break
        time.sleep(_POLL_S)
    jm.update_progress(job_id, retry_eta=None)
    return True


def _stored_file_items(paths: set[str]) -> list[ProcessableItem]:
    """Items for files the markers store listed, sorted by season folder then path, as ``build_items`` lists a job's
    files (no item id hints: each is looked up again)."""
    ordered = sorted(paths, key=lambda p: (os.path.dirname(p), p))
    return [
        ProcessableItem(canonical_path=p, server_id="", item_id_by_server={}, title=os.path.basename(p))
        for p in ordered
    ]


def _files_to_decide_again(store: MarkerStore) -> set[str]:
    return {
        *store.files_in_review(),
        *store.files_waiting_for_other_versions(),
        *store.files_with_season_audio_intro(),
        *store.files_decided_by_online_and_server_markers(),
    }


def _items_to_decide_again(store: MarkerStore, configs: Sequence[ServerConfig] = ()) -> list[ProcessableItem]:
    """The files in Needs review now, those whose last publish waits for their item's other versions, those whose
    unlocked intro was decided with a season audio answer (settings v17: season audio's guards changed its answers), and
    those whose unlocked intro or credits rests on an online answer and a server's own marker alone (settings v18:
    online times on the file's clock).

    Given the servers' configs, those files that are missing from disk are marked first (``missing``), so a series
    deleted since isn't listed; any the check had no time for are marked when their run finds them missing.
    """
    if configs:
        mark_missing_files(store, sorted(_files_to_decide_again(store)), configs)
    return _stored_file_items(_files_to_decide_again(store))


def _items_for_online_recheck(ctx: PipelineContext, cancel_check: Callable[[], bool]) -> list[ProcessableItem]:
    """The files whose "no entry" from an enabled online source is due again and whose decision it could still change
    (``online_recheck_files``: a disk check per file). A cancel stops the listing; the caller then cancels the job."""
    paths: set[str] = set()
    for path in online_recheck_files(ctx.store, ctx.settings, ctx.now()):
        if cancel_check():
            break
        paths.add(path)
    return _stored_file_items(paths)


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


def _wait_for_preceding_job(job_id: str, follows_job_id: str | None, cancel_check: Callable[[], bool]) -> bool:
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
    """Keep a job paused before a restart paused, holding no slot, until it is resumed.

    The pause and resume routes only act on running jobs, so the job is marked running and paused again. A pause from
    the schedule's stop time is held as one, so that schedule's next start still resumes it.

    Returns:
        False if the job was cancelled while paused.
    """
    jm = get_job_manager()
    job = jm.get_job(job_id)
    jm.start_job(job_id)
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

    Season, retry and verify jobs don't: they follow a job that did. Never raises.
    """
    if cfg.get("source") == SEASON_SOURCE or cfg.get("retry_attempt") or cfg.get("verify"):
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
    waiting; a verify job's file was there already, and a Season job's files were on disk when the season step saw
    them.
    """
    return _sends_files(cfg) and not cfg.get("verify")


def _sends_files(cfg: dict) -> bool:
    """Whether the job lists files a sender just reported (not a listing, nor a source that picks files itself)."""
    return bool(cfg.get("file_paths")) and cfg.get("source") not in _NO_RETRY_SOURCES


def _log_missing(ctx: PipelineContext) -> None:
    """Log how many files this job marked missing from disk (``PipelineContext.take_missing``)."""
    missing = ctx.take_missing()
    if missing:
        logger.info(MISSING_LINE, missing)


def _log_summary(jm, job_id: str, outcome: dict[str, int], ctx: PipelineContext) -> None:
    """End the job's log with a Season job's one line per season and the totals line. Never raises."""
    try:
        for line in ctx.summary_lines(outcome):
            jm.add_log(job_id, f"INFO - {line}")
    except Exception as exc:
        # The job's work is done: a summary that can't be written mustn't fail it.
        logger.warning("Couldn't write the summary of Intro & Credits job {}: {}", job_id, type(exc).__name__)


def _complete(
    jm, job_id: str, outcome: dict[str, int], warnings: list[str], ctx: PipelineContext | None = None
) -> None:
    """Complete the job: failed (red) when every counted file failed, a warning (amber) when some did or on warnings.

    Given the job's pipeline context, the job's log gets its closing lines first (``PipelineContext.summary_lines``).
    """
    if ctx is not None:
        _log_summary(jm, job_id, outcome, ctx)
    failed = outcome.get(FileOutcome.FAILED.value, 0)
    succeeded = sum(count for key, count in outcome.items() if key != FileOutcome.FAILED.value)
    if failed and not succeeded:
        jm.complete_job(job_id, error=" | ".join([f"All {failed} file(s) failed — see the Files panel", *warnings]))
        return
    parts = ([f"{failed} file(s) failed"] if failed else []) + warnings
    jm.complete_job(job_id, warning=" | ".join(parts) or None)


def _settle_decide_again(jm, job_id: str, cfg: dict) -> None:
    """Once the decide-again job has completed, clear the upgrade's request for it (``upgrade.DECIDE_AGAIN_KEY``).

    Only a completed job clears it: one cancelled, failed or cut short by a restart leaves it, so the next start queues
    the job again (``web.app._decide_again_after_upgrade``). Never raises.
    """
    if not cfg.get(DECIDE_AGAIN):
        return
    try:
        finished = jm.get_job(job_id)
        if finished is None or finished.status is not JobStatus.COMPLETED:
            return
        from ..upgrade import DECIDE_AGAIN_KEY

        get_settings_manager().delete(DECIDE_AGAIN_KEY)
    except Exception as exc:
        logger.warning("Couldn't record that the decide-again job finished: {}; the next start queues it again", exc)


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


def _cancel_check_releasing_slot_while_paused(
    *,
    job_id: str,
    slot: dict,
    live_priority: Callable[[], int],
    cancel_check: Callable[[], bool],
    on_wait: Callable[[int, int, int], None],
) -> Callable[[], bool]:
    """A cancel check for work done on the job's own thread (Check servers' read-back): it doesn't return while the
    job or all processing is paused, and gives the job's gate slot back while this job is paused on its own, taking a
    slot again on resume (``_wait_releasing_slot_while_paused``).

    Returns:
        The check: True once the job is cancelled.
    """
    jm = get_job_manager()
    gate = get_job_gate()

    def check() -> bool:
        while not cancel_check():
            paused = jm.is_pause_requested(job_id)
            if paused and slot["held"]:
                gate.release(slot["priority"])
                slot["held"] = False
                jm.add_log(job_id, "INFO - Paused; active slot handed back until resume")
            elif not paused and not slot["held"]:
                priority = live_priority()
                if gate.acquire(
                    priority=priority,
                    cancel_check=lambda: cancel_check() or jm.is_pause_requested(job_id),
                    on_wait=on_wait,
                ):
                    slot["priority"] = priority
                    slot["held"] = True
                continue
            elif not paused and not get_settings_manager().processing_paused:
                return False
            time.sleep(_POLL_S)
        return True

    return check


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
    if is_live_retry_chain(job.config):
        # Its hidden retry job runs the files still waiting; a resume that starts every pending job mustn't run the
        # whole job again.
        logger.info("Intro & Credits job {} not started — its retry runs the files still waiting", job_id)
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
        filter=lambda record: not record["extra"].get(JOB_LOG_SKIP) and is_job_thread_for(record["thread"].id, job_id),
        enqueue=True,
    )
    # "priority" is the value the slot was admitted at: the user can re-prioritise the job, and release() must
    # settle at the admitted value.
    slot = {"held": False, "priority": job.priority}
    cfg = dict(job.config or {})
    dispatcher = None
    # Set once the job completes: the fingerprint cache sweep starts after the slot is given back, with the servers'
    # configs for the deleted-file sweep that runs first.
    sweep_store = None
    sweep_configs: list[ServerConfig] = []
    # While the config holds the Check servers listing (a revived job's from the start): only a revive needs it, so the
    # teardown takes it off the ended job (the config ships in every job payload).
    listing_on_job = LISTING_CONFIG_KEY in cfg
    # A Season job's requests from other jobs, passed on where it completes; the teardown passes on the rest whatever
    # ended the job (``_pass_on_late_requests``).
    requests_passed_on = False

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
                cfg = _seal_files(jm, job_id, job, cfg)
                ctx = build_context(
                    registry=registry,
                    config=config,
                    priority=live_priority,
                    force=bool(cfg.get("force")),
                    recheck_empty_server_markers=bool(cfg.get("reconcile")),
                    # A TheIntroDB recheck mostly finds nothing new either: one line per season, as a Season job.
                    season_recheck=cfg.get("source") in (SEASON_SOURCE, BUDGET_RECHECK_SOURCE),
                    recheck_label=(
                        BUDGET_RECHECK_LABEL if cfg.get("source") == BUDGET_RECHECK_SOURCE else SEASON_RECHECK_LABEL
                    ),
                    decide_again=bool(cfg.get(DECIDE_AGAIN)),
                    online_recheck=bool(cfg.get(ONLINE_RECHECK)),
                )
                ctx.busy_writes_retried = _retry_follows(cfg)
                ctx.retry_file_cap = MAX_RETRY_FILES
                sweep_configs = list(registry.configs())
                listing = None
                if cfg.get("reconcile"):
                    from .reconcile import check_servers_listing

                    # A run revived after a restart checks the files its first run listed, without reading every
                    # server back again; the checks of the files it had finished were used then (count_checked).
                    listing = CheckServersListing.from_config(cfg.get(LISTING_CONFIG_KEY))
                    if listing is None and LISTING_CONFIG_KEY in cfg:
                        logger.warning(
                            "Check servers couldn't read the files it listed before the restart; listing them again"
                        )
                    if listing is None:
                        listing = check_servers_listing(
                            registry=registry,
                            store=ctx.store,
                            max_files=MAX_RETRY_FILES,
                            capability=lambda server_cfg, publisher: cached_capability(ctx, server_cfg, publisher),
                            cancel_check=_cancel_check_releasing_slot_while_paused(
                                job_id=job_id,
                                slot=slot,
                                live_priority=live_priority,
                                cancel_check=cancel_check,
                                on_wait=on_wait,
                            ),
                            progress_callback=progress_callback,
                        )
                        if not cancel_check() and jm.merge_job_config(
                            job_id, {LISTING_CONFIG_KEY: listing.to_config()}
                        ):
                            listing_on_job = True
                    items, warnings, sender_paths = listing.items, listing.warnings, {}
                elif cfg.get(DECIDE_AGAIN):
                    items, warnings, sender_paths = _items_to_decide_again(ctx.store, sweep_configs), [], {}
                elif cfg.get(ONLINE_RECHECK):
                    items, warnings, sender_paths = _items_for_online_recheck(ctx, cancel_check), [], {}
                else:
                    items, warnings, sender_paths = build_items(
                        cfg, registry=registry, cancel_check=cancel_check, progress_callback=progress_callback
                    )
                    cfg = _with_merged_sender_hints(cfg, items, sender_paths)
                    if cfg.get("source") == BUDGET_RECHECK_SOURCE:
                        items = _budget_recheck_items(job_id, ctx, items)
                if cancel_check():
                    jm.cancel_job(job_id)
                    return
                if not items:
                    if listing is not None:
                        jm.add_log(job_id, "INFO - Every server checked still shows what this app published")
                        jm.complete_job(job_id, warning=" | ".join(warnings) or None)
                    elif cfg.get(DECIDE_AGAIN):
                        jm.add_log(job_id, "INFO - No file is in Needs review or waiting for its item's other versions")
                        jm.complete_job(job_id)
                        _settle_decide_again(jm, job_id, cfg)
                    elif cfg.get(ONLINE_RECHECK):
                        jm.add_log(job_id, "INFO - No file is due to be asked again online")
                        jm.complete_job(job_id)
                    else:
                        jm.complete_job(job_id, warning=" ".join(["No files to check.", *warnings]))
                    if cfg.get("source") == SEASON_SOURCE:
                        _pass_on_late_requests(job, ctx)
                        requests_passed_on = True
                    sweep_store = ctx.store
                    return
                sent_files = _sends_files(cfg)
                retries_missing_files = _retries_missing_files(cfg)
                # Only files just sent were just replaced: a listing's replaced file may have changed days ago, and
                # servers rescanned it long since. A verify chain checks once.
                checks_replaced_later = sent_files and not (cfg.get("verify") or cfg.get("verify_chain"))
                listed = {item.canonical_path for item in items}
                # A revived job still owes the later check of the replaced files it published before the restart.
                # A retry's rows are its chain head's.
                items, carried, replaced_before_restart, carried_outcomes = _skip_finished_before_restart(
                    jm, chain_head or job_id, items, ctx.store
                )
                if carried_outcomes:
                    # The pipeline never decides for a file without an owner; the store may still hold an old run's.
                    for path, outcome in sorted(carried_outcomes.items()):
                        if outcome != FileOutcome.NO_OWNERS.value:
                            ctx.decided_by.add(stored_groups(ctx.store, path))
                    jm.set_marker_sources(job_id, ctx.decided_by.snapshot())
                if not items:
                    jm.set_job_outcome(job_id, carried)
                    _complete(jm, job_id, carried, warnings, ctx)
                    _settle_decide_again(jm, job_id, cfg)
                    if chain_head:
                        # A retry revived after a restart that had settled all its files: nothing is left to wait.
                        _recount_chain_head(jm, chain_head, ctx.store)
                        _end_chain(jm, cfg, {})
                    if replaced_before_restart and checks_replaced_later:
                        _queue_verify(job, cfg, replaced_before_restart, sender_paths)
                    if cfg.get("source") == SEASON_SOURCE:
                        _pass_on_late_requests(job, ctx)
                        requests_passed_on = True
                    sweep_store = ctx.store
                    return
                waiting: dict[str, set[str]] = {}
                replaced: set[str] = set(replaced_before_restart)
                unchecked: dict[str, set[str]] = {}
                gone_items: dict[str, set[tuple[str, str]]] = {}

                def on_file_result(file_path, outcome, reason, worker, servers=None):
                    # Any server that can take the file later, even when another server was written. Check servers
                    # retries only a write Plex's busy database refused (its next run is a day away for a failed item)
                    # and the files whose old item a server confirmed gone (below).
                    codes = {code for row in servers or [] if (code := retry_reason(row))}
                    if listing is not None:
                        codes &= {PLEX_DB_BUSY}
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

                set_file_result_callback(on_file_result, job_id=job_id)
                # Detected before the settings lock below, so detection never runs while it's held.
                detected_gpus = _ensure_gpu_cache()
                dispatcher = get_or_create_dispatcher(config, _build_selected_gpus(settings, detected=detected_gpus))
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
                        if selected_gpus:
                            dispatcher.worker_pool.reconcile_gpu_workers(selected_gpus)
                        dispatcher.worker_pool.reconcile_cpu_workers(settings.cpu_threads)
                except Exception as exc:
                    logger.debug("Could not reconcile the worker pool with the saved settings: {}", exc)
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
                    f"Couldn't check what {len(files)} file(s) show on {name}"
                    for name, files in sorted(unchecked.items())
                ]
                # The retry is queued before completing, as the preview runner does: its chain keeps the chain head's
                # row pending, so completing the head only settles its run's bookkeeping.
                # Check servers only waits for files whose old item a server confirmed gone: they get the retry a normal
                # job queues (once from here, the retry job counts on), and only then leave Check servers. Any other
                # file still waiting keeps its item and is listed again by a later run.
                retried = _queue_retry(job, cfg, waiting, sender_paths, ctx.busy_promised()) if waiting else []
                _mark_retried_items_gone(ctx.store, gone_items, retried, sender_paths)
                _complete(jm, job_id, outcome, [*warnings, *unchecked_warnings, *budget_exhausted_warnings(ctx)], ctx)
                _settle_decide_again(jm, job_id, cfg)
                if chain_head and not retried:
                    _end_chain(jm, cfg, waiting)
                _queue_season_followups_after(job, cfg, ctx, listed)
                requests_passed_on = True
                _queue_budget_recheck(job, ctx)
                if replaced and checks_replaced_later:
                    _queue_verify(job, cfg, replaced, sender_paths)
                sweep_store = ctx.store
            finally:
                clear_failures()
    except Exception as exc:
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
        jm.clear_active_worker_pool(job_id)
        if listing_on_job:
            try:
                jm.merge_job_config(job_id, {}, remove=(LISTING_CONFIG_KEY,))
            except Exception as exc:
                logger.debug("Could not drop the Check servers listing of {}: {}", job_id, exc)
        try:
            if not jm.get_running_jobs():
                jm.clear_worker_statuses()
        except Exception as exc:
            logger.debug("Could not clear worker statuses after {}: {}", job_id, exc)
        if cfg.get("source") == SEASON_SOURCE and not requests_passed_on:
            # Cancelled or failed before it completed: what other jobs handed over still needs a Season job.
            _pass_on_late_requests(job, None)
        unregister_job_thread()
        # After the slot is back, and outside the job's log: a skipped cleanup's warning isn't about this job.
        if sweep_store is not None:
            _start_fingerprint_sweep(cfg, sweep_store, sweep_configs)
        try:
            loguru_logger.complete()
            loguru_logger.remove(handler_id)
        except (ValueError, TypeError):
            logger.debug("Could not remove the job log handler for {}", job_id)


def start_intro_credits_job_async(job_id: str, config_overrides: dict | None = None) -> None:
    """Start the job on a daemon thread (a second start for a job already in flight is ignored).

    Args:
        job_id: The job to start.
        config_overrides: Keys merged into the job's config first (resume paths pass a snapshot of the job's own
            config); files that joined the job since the snapshot are kept.
    """
    if config_overrides:
        jm = get_job_manager()
        # Resume paths pass a snapshot of the job's config; files that joined it since (webhook episodes, Season
        # requests) are kept. Only this branch takes the lock: create_intro_credits_job starts jobs without overrides,
        # and its callers submit_webhook_follow_up and _queue_season_followups hold the (non-reentrant) lock then.
        with FOLLOW_UP_LOCK:
            job = jm.get_job(job_id)
            if job is not None:
                live = job.config or {}
                # Only the keys an override changes are written, so a key another thread sets meanwhile (a stop-time
                # pause) stays.
                updates = {
                    key: value
                    for key, value in config_overrides.items()
                    if not (key in _JOINED_KEYS and key in live) and (key not in live or live[key] != value)
                }
                if updates:
                    jm.merge_job_config(job_id, updates)
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
