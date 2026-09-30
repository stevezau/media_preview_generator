"""The loudness job kind: which files need it (check stage), analysing their streams (workers), and the job runner.

Runs through the same dispatcher, worker pool, priorities, job gate, pause and cancel as previews and Intro & Credits
(``job_kinds.KindHandlers``). A file is checked against each Plex server with loudness on for its library: streams
Plex already analysed are skipped, so a file Plex (or an earlier job) finished never reaches a worker.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger

from ..config import load_config
from ..job_kinds import JOB_KIND_LOUDNESS, ItemOutcome, KindHandlers
from ..jobs.dispatcher import get_or_create_dispatcher
from ..jobs.orchestrator import _build_multi_server_registry
from ..jobs.worker import JOB_LOG_SKIP, is_job_thread_for, register_job_thread, unregister_job_thread
from ..markers.job_runner import (
    build_items,
    finish_job,
    hold_pause_from_before_restart,
    job_freeze_check,
    sent_by_a_sender,
    server_pin,
    wait_for_preceding_job,
    wait_for_retry_time,
    wait_releasing_slot_while_paused,
    worker_cards,
)
from ..markers.outcomes import FileOutcome
from ..markers.ownership import owning_servers
from ..markers.publishers.base import Capability, DatabaseBusyError, PublishError, cancellable_waits
from ..markers.publishers.plex_db import BUSY_TIMEOUT_S, LocalPlexDb, PlexMarkerPublisher
from ..processing.generator import clear_failures, failure_scope, set_file_result_callback
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import apply_inverse_path_mappings
from ..utils import redact_secrets
from ..web.job_gate import format_wait_message, get_job_gate
from ..web.jobs import PRIORITY_LOW, get_job_manager
from ..web.routes._helpers import _ensure_gpu_cache
from ..web.routes.job_runner import _build_selected_gpus, _inflight_jobs, _inflight_lock
from ..web.settings_manager import get_settings_manager
from . import analyze
from .plex_db import AudioStream, mark_item, needs_analysis, read_streams, write_stream
from .settings import library_chosen, load_server_loudness, loudness_libraries

LABEL = "Plex loudness"
WRITTEN = "loudness_written"
UP_TO_DATE = "loudness_up_to_date"
NO_OWNERS = "loudness_no_owners"
NOT_IN_LIBRARY = "loudness_not_in_library"
# Plex's database couldn't be written just then (Plex restarting, its lock busy): tried again later, like NOT_IN_LIBRARY.
WAITING = "loudness_waiting"
FILE_NOT_FOUND = FileOutcome.FILE_NOT_FOUND.value
FAILED = FileOutcome.FAILED.value
OUTCOME_KEYS = (WRITTEN, UP_TO_DATE, NO_OWNERS, NOT_IN_LIBRARY, WAITING, FILE_NOT_FOUND, FAILED)
# A file's outcome from its per-server rows: the first of these any server has.
_PRECEDENCE = (FAILED, WAITING, NOT_IN_LIBRARY, WRITTEN, UP_TO_DATE)
# What a revived job carries over from before a restart instead of checking again.
_SETTLED = frozenset({WRITTEN, UP_TO_DATE, NO_OWNERS})
# Read-only lookups share the dispatcher's checking threads with preview checks: half of them at most.
CHECK_SHARE = 0.5
# A database that wasn't writable (Plex restarting, its lock busy) is checked again after this long.
RECHECK_S = 60.0
# Files Plex hadn't added yet (a webhook follow-up often starts before Plex scans): tried again this many times, the
# n-th retry this long after the job ends times n.
MAX_RETRIES = 3
RETRY_DELAY_S = 15 * 60


@dataclass
class LoudnessContext:
    """What every item of one job shares."""

    registry: object
    ffmpeg: str
    freeze_check: Callable[[], bool] | None = None
    # The job's pin (``job_runner.server_pin``): only this server; None = every server with loudness on.
    server_id: str | None = None
    _dbs: dict[str, LocalPlexDb] = field(default_factory=dict)
    # server id → (why it can't be written, "" when it can; when that was found)
    _ready: dict[str, tuple[str, float]] = field(default_factory=dict)
    # One lock per server: a busy Plex database doesn't hold up the others' checks.
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _locks_lock: threading.Lock = field(default_factory=threading.Lock)

    def db(self, cfg: ServerConfig) -> tuple[LocalPlexDb, str]:
        """The server's Plex database, and "" when it can be written or why not.

        A writable answer holds for the job; any other is asked again after ``RECHECK_S``, so a moment when Plex was
        restarting doesn't fail a whole library.
        """
        with self._locks_lock:
            lock = self._locks.setdefault(cfg.id, threading.Lock())
        with lock:
            db = self._dbs.get(cfg.id)
            if db is None:
                db = self._dbs[cfg.id] = LocalPlexDb(
                    lambda cfg=cfg: PlexMarkerPublisher.db_path_for(cfg), label=cfg.name
                )
            known = self._ready.get(cfg.id)
            if known is None or (known[0] and time.monotonic() - known[1] > RECHECK_S):
                report = db.file_checks(deadline=time.monotonic() + BUSY_TIMEOUT_S)
                known = ("" if report.state is Capability.READY else report.message, time.monotonic())
                self._ready[cfg.id] = known
            return db, known[0]


def owners(canonical_path: str, registry, server_id: str | None = None) -> list[ServerConfig]:
    """The enabled Plex servers with loudness on for a library holding the file (exclude rules applied), only
    ``server_id`` when given."""
    found = []
    for cfg, _server, matches in owning_servers(canonical_path, registry):
        if not cfg.enabled or cfg.type is not ServerType.PLEX or server_id not in (None, cfg.id):
            continue
        settings = load_server_loudness(cfg)
        if not settings.enabled:
            continue
        kinds = {lib.id: lib.kind for lib in cfg.libraries}
        if any(library_chosen(settings, library_id=m.library_id, kind=kinds.get(m.library_id)) for m in matches):
            found.append(cfg)
    return found


def _row(cfg: ServerConfig, status: str, message: str = "") -> dict:
    return {"server_id": cfg.id, "server_name": cfg.name, "server_type": "plex", "status": status, "message": message}


def _lookup(
    ctx: LoudnessContext, cfg: ServerConfig, path: str
) -> tuple[dict | None, list[AudioStream], list[AudioStream]]:
    """The file's audio streams in the server's Plex and those without loudness yet; or, instead, the server's row when
    there's nothing to analyse there (not in Plex yet, no audio track, the database unreadable just then)."""
    db, why = ctx.db(cfg)
    if why:
        return _row(cfg, WAITING, why), [], []
    try:
        plex_paths = apply_inverse_path_mappings(path, cfg.path_mappings or [])
        streams, in_plex = read_streams(db, plex_paths, deadline=time.monotonic() + BUSY_TIMEOUT_S)
        needed = [s for s in streams if needs_analysis(s)]
    except DatabaseBusyError as exc:
        return _row(cfg, WAITING, str(exc)), [], []
    except PublishError as exc:
        return _row(cfg, FAILED, f"Couldn't read the file's audio streams in Plex: {exc}"), [], []
    if not in_plex:
        return _row(cfg, NOT_IN_LIBRARY, "Plex hasn't added this file yet"), [], []
    if not streams:
        return _row(cfg, UP_TO_DATE, "No audio track"), [], []
    return None, streams, needed


def check_item(item: ProcessableItem, *, ctx: LoudnessContext) -> ItemOutcome | None:
    """Check stage: settle a file with nothing to analyse; None sends it to a worker."""
    path = item.canonical_path
    if not os.path.isfile(path):
        return ItemOutcome(FILE_NOT_FOUND, "Not on disk")
    servers = owners(path, ctx.registry, ctx.server_id)
    if not servers:
        return ItemOutcome(NO_OWNERS, "No Plex server has loudness on for its library")
    rows = []
    for cfg in servers:
        row, streams, todo = _lookup(ctx, cfg, path)
        if row is not None:
            rows.append(row)
        elif todo or not all(s.item_marked for s in streams):
            # Analysis to do, or an item analysed but not yet marked (Plex would analyse it again).
            return None
        else:
            rows.append(_row(cfg, UP_TO_DATE))
    return _settle(rows)


def process_item(
    item: ProcessableItem,
    *,
    ctx: LoudnessContext,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
    phase_callback: Callable[[str], None] | None = None,
    **_ignored,
) -> ItemOutcome:
    """Worker stage: analyse each stream Plex lacks loudness for and write it. A GPU worker runs it on the CPU too."""
    path = item.canonical_path
    rows = []
    for cfg in owners(path, ctx.registry, ctx.server_id):
        row, streams, needed = _lookup(ctx, cfg, path)
        if row is not None:
            rows.append(row)
            continue
        # Plex can list one file under several parts (duplicate items): the same stream of the same file is analysed
        # once and written to each.
        todo: dict[int, list[AudioStream]] = {}
        for stream in needed:
            todo.setdefault(stream.index, []).append(stream)
        written, errors = 0, []
        db, _ = ctx.db(cfg)
        for n, (index, same) in enumerate(sorted(todo.items()), 1):
            if cancel_check and cancel_check():
                errors.append(f"cancelled before stream {index}")
                break
            if phase_callback:
                phase_callback(f"Loudness {n}/{len(todo)}")
            try:
                fields = analyze.run(
                    ctx.ffmpeg,
                    path,
                    index,
                    duration_ms=same[0].duration_ms,
                    codec=same[0].codec,
                    cancel_check=cancel_check,
                    pause_check=ctx.freeze_check or pause_check,
                )
                with cancellable_waits(cancel_check):
                    for stream in same:
                        written += write_stream(db, stream.id, fields, deadline=time.monotonic() + BUSY_TIMEOUT_S)
            except (analyze.LoudnessError, PublishError) as exc:
                errors.append(f"stream {index} ({same[0].codec}): {exc}")
        marked = 0
        if not errors:
            # Plex analyses an item again unless it's marked; marked once all its streams (all versions) are done.
            for item_id in sorted({s.metadata_item_id for s in streams if not s.item_marked}):
                try:
                    with cancellable_waits(cancel_check):
                        marked += mark_item(db, item_id, deadline=time.monotonic() + BUSY_TIMEOUT_S)
                except PublishError as exc:
                    errors.append(f"marking item {item_id}: {exc}")
        if errors:
            rows.append(_row(cfg, FAILED, "; ".join(errors)))
        elif written or marked:
            done = ([f"{written} stream(s)"] if written else []) + (["item marked analysed"] if marked else [])
            rows.append(_row(cfg, WRITTEN, ", ".join(done)))
        else:
            rows.append(_row(cfg, UP_TO_DATE))
    if not rows:
        return ItemOutcome(NO_OWNERS, "No Plex server has loudness on for its library")
    return _settle(rows)


def _settle(rows: list[dict]) -> ItemOutcome:
    """The file's outcome from its per-server rows (``_PRECEDENCE``): a failure anywhere fails it, a server still to
    try again keeps it for the retry, else written beats up to date."""
    statuses = {r["status"] for r in rows}
    key = next(k for k in _PRECEDENCE if k in statuses)
    message = "; ".join(r["message"] for r in rows if r["status"] == key and r["message"])
    return ItemOutcome(key, message, publisher_rows=rows)


def kind_handlers(ctx: LoudnessContext) -> KindHandlers:
    """The loudness kind's handlers for one job."""
    return KindHandlers(
        check_fn=lambda item, *, cancel_check=None: check_item(item, ctx=ctx),
        process_fn=lambda item, **kwargs: process_item(item, ctx=ctx, **kwargs),
        outcome_keys=OUTCOME_KEYS,
        check_label="Checking Plex loudness…",
        check_worker_label="Loudness",
        check_share=CHECK_SHARE,
    )


def run_loudness_job(job_id: str) -> None:
    """Run one loudness job to completion (on its own thread)."""
    jm = get_job_manager()
    job = jm.get_job(job_id)
    if job is None:
        return
    settings = get_settings_manager()
    if settings.processing_paused:
        logger.info("Loudness job {} not started: processing is paused; job stays pending", job_id)
        return
    register_job_thread(job_id)
    handler_id = logger.add(
        lambda message: jm.add_log(job_id, f"{message.record['level'].name} - {message.record['message']}"),
        level=str(settings.get("log_level", "INFO")).upper(),
        format="{message}",
        filter=lambda record: not record["extra"].get(JOB_LOG_SKIP) and is_job_thread_for(record["thread"].id, job_id),
        enqueue=True,
    )
    slot = {"held": False, "priority": job.priority}
    cfg = dict(job.config or {})

    def cancel_check() -> bool:
        return jm.is_cancellation_requested(job_id)

    def live_priority() -> int:
        return job.priority

    def on_wait(active: int, cap: int, effective_cap: int) -> None:
        jm.update_progress(
            job_id,
            percent=0,
            processed_items=0,
            total_items=0,
            current_item=format_wait_message(active, cap, effective_cap),
        )
        jm.note_slot_wait(job_id)

    def progress_callback(current, total, message, percent_override=None):
        percent = percent_override if percent_override is not None else (current / total * 100 if total else 0)
        jm.update_progress(job_id, percent=percent, processed_items=current, total_items=total, current_item=message)

    dispatcher = None
    try:
        with failure_scope(job_id):
            try:
                if not wait_for_preceding_job(job_id, cfg.get("follows_job_id"), cancel_check):
                    jm.cancel_job(job_id)
                    return
                if not wait_for_retry_time(job_id, cfg, cancel_check):
                    jm.cancel_job(job_id)
                    return
                if job.paused and not hold_pause_from_before_restart(job_id, cancel_check):
                    jm.cancel_job(job_id)
                    return
                slot["priority"] = live_priority()
                jm.note_slot_wait(job_id)
                if not get_job_gate().acquire(priority=slot["priority"], cancel_check=cancel_check, on_wait=on_wait):
                    jm.add_log(job_id, "WARNING - Job cancelled while waiting for active slot")
                    jm.cancel_job(job_id)
                    return
                slot["held"] = True
                with logger.contextualize(**{JOB_LOG_SKIP: True}):
                    jm.start_job(job_id)
                config = load_config()
                config.server_id_filter = server_pin(cfg)
                registry = _build_multi_server_registry(config)
                if registry is None:
                    jm.complete_job(job_id, error="Couldn't load the media servers configuration")
                    return
                items, warnings, _senders = build_items(
                    cfg,
                    registry=registry,
                    cancel_check=cancel_check,
                    progress_callback=progress_callback,
                    enabled=lambda server: server.type is ServerType.PLEX and load_server_loudness(server).enabled,
                    libraries=loudness_libraries,
                    label=LABEL,
                )
                if cancel_check():
                    jm.cancel_job(job_id)
                    return
                logger.info("{}: {} file(s) to check", LABEL, len(items))
                if not items:
                    jm.complete_job(job_id, warning=" ".join(["No files to check.", *warnings]))
                    return
                items, carried = _carry_finished(jm, job_id, items)
                ctx = LoudnessContext(
                    registry=registry,
                    ffmpeg=getattr(config, "ffmpeg_path", None) or "ffmpeg",
                    freeze_check=job_freeze_check(jm, job_id),
                    server_id=server_pin(cfg),
                )
                # A sender's files (a webhook, its retries) can reach the disk after the job starts; a listing's won't.
                retry_missing = bool(cfg.get("file_paths")) and sent_by_a_sender(cfg.get("source"))
                to_retry: list[str] = []

                def on_file_result(file_path, outcome, reason, worker, servers=None):
                    if outcome in (NOT_IN_LIBRARY, WAITING) or (outcome == FILE_NOT_FOUND and retry_missing):
                        to_retry.append(file_path)
                    jm.record_file_result(
                        job_id, file_path, outcome, reason, worker, servers=servers, server_messages=True
                    )

                set_file_result_callback(on_file_result, job_id=job_id)
                detected_gpus = _ensure_gpu_cache()
                dispatcher = get_or_create_dispatcher(config, _build_selected_gpus(settings, detected=detected_gpus))
                jm.set_active_worker_pool(job_id, dispatcher.worker_pool)
                tracker = dispatcher.submit_items(
                    job_id=job_id,
                    items=items,
                    config=config,
                    registry=registry,
                    title_max_width=200,
                    library_name="",
                    callbacks={
                        "progress_callback": progress_callback,
                        "worker_callback": worker_cards(jm),
                        "cancel_check": cancel_check,
                        "pause_check": lambda: (
                            not slot["held"]
                            or jm.is_pause_requested(job_id)
                            or get_settings_manager().processing_paused
                        ),
                    },
                    priority=live_priority(),
                    kind=JOB_KIND_LOUDNESS,
                    handlers=kind_handlers(ctx),
                )
                current = live_priority()
                if tracker.priority != current:
                    dispatcher.update_job_priority(job_id, current)
                wait_releasing_slot_while_paused(
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
                outcome = dict(result["outcome"])
                for key, count in carried.items():
                    outcome[key] = outcome.get(key, 0) + count
                jm.set_job_outcome(job_id, outcome)
                if result["cancelled"] or cancel_check():
                    jm.cancel_job(job_id)
                    return
                retried = bool(to_retry) and _queue_retry(cfg, to_retry)
                if to_retry and not retried:
                    warnings.append(f"{len(set(to_retry))} file(s) still waiting for Plex or the disk; not tried again")
                # Missing files count against the job unless the retry covers them (only a sender's files).
                finish_job(jm, job_id, outcome, warnings, retried=retried and retry_missing)
            finally:
                clear_failures()
    except Exception as exc:
        detail = redact_secrets(f"{type(exc).__name__}: {exc}")
        logger.error("Loudness job {} failed: {}", job_id, detail)
        if dispatcher is not None:
            try:
                dispatcher.cancel_job(job_id)
            except Exception as cancel_exc:
                logger.warning("Could not stop the remaining files of loudness job {}: {}", job_id, cancel_exc)
        try:
            jm.complete_job(job_id, error=detail)
        except Exception as complete_exc:
            logger.warning("Could not mark loudness job {} failed: {}", job_id, complete_exc)
    finally:
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
        try:
            if not jm.get_running_jobs():
                jm.clear_worker_statuses()
        except Exception as exc:
            logger.debug("Could not clear worker statuses after {}: {}", job_id, exc)
        unregister_job_thread()
        try:
            logger.complete()
            logger.remove(handler_id)
        except (ValueError, TypeError):
            logger.debug("Could not remove the job log handler for {}", job_id)


def start_loudness_job_async(job_id: str, config_overrides: dict | None = None) -> None:
    """Start the job on a daemon thread (a second start for a job already in flight is ignored)."""
    if config_overrides:
        jm = get_job_manager()
        job = jm.get_job(job_id)
        live = (job.config or {}) if job is not None else {}
        updates = {k: v for k, v in config_overrides.items() if live.get(k) != v}
        if job is not None and updates:
            jm.merge_job_config(job_id, updates)
    with _inflight_lock:
        if job_id in _inflight_jobs:
            logger.info("Skipping duplicate loudness start for {}: already in flight", job_id)
            return
        _inflight_jobs.add(job_id)

    def _run() -> None:
        try:
            run_loudness_job(job_id)
        finally:
            with _inflight_lock:
                _inflight_jobs.discard(job_id)

    threading.Thread(target=_run, daemon=True, name=f"run_job_loudness_{job_id}").start()


def create_loudness_job(
    *,
    library_name: str,
    priority: int,
    source: str,
    libraries: list[dict] | None = None,
    file_paths: list[str] | None = None,
    follows_job_id: str | None = None,
    server_id: str | None = None,
    retry_attempt: int = 0,
    retry_delay_s: int = 0,
):
    """Create and start a loudness job.

    Args:
        library_name: Job title shown in the queue.
        priority: 1 high, 2 normal, 3 low.
        source: What created it (``manual``, a webhook source).
        libraries: ``[{"server_id", "library_id"}]``; empty with no ``file_paths`` = every library loudness is on for.
        file_paths: Explicit files or folders instead of libraries.
        follows_job_id: The job this one waits for before taking a slot: a webhook's Intro & Credits follow-up, else
            its preview job.
        server_id: Only this server (the preview job's pin); None = every server with loudness on.
        retry_attempt: For a retry (``_queue_retry``): which retry (1-based).
        retry_delay_s: For a retry: seconds to wait before it takes a slot.

    Returns:
        The created job.
    """
    config = {
        "kind": JOB_KIND_LOUDNESS,
        "source": source,
        "libraries": list(libraries or []),
        "file_paths": list(file_paths or []),
        "follows_job_id": follows_job_id,
    }
    if server_id:
        config["server_id"] = server_id
    if retry_attempt:
        config["retry_attempt"] = int(retry_attempt)
        # The due time is stored so a job revived after a restart doesn't wait again in full.
        config["retry_not_before"] = (datetime.now(UTC) + timedelta(seconds=int(retry_delay_s))).isoformat()
    job = get_job_manager().create_job(
        library_name=library_name, config=config, priority=priority, kind=JOB_KIND_LOUDNESS
    )
    start_loudness_job_async(job.id)
    logger.info("Created loudness job {} ({}, source={})", job.id[:8], library_name, source)
    return job


def _carry_finished(jm, job_id: str, items: list[ProcessableItem]) -> tuple[list[ProcessableItem], dict[str, int]]:
    """Drop the files a job settled before a restart revived it (its own Files-panel rows; only a revived job has any),
    so they aren't listed twice, and return their outcome counts to carry into the job's."""
    try:
        rows = jm.get_file_results(job_id)
    except Exception as exc:
        logger.warning("Couldn't read the files this job already finished ({}); checking every file", exc)
        return items, {}
    settled = {
        row["file"]: row["outcome"]
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and row.get("file") and row.get("outcome") in _SETTLED
    }
    carried: dict[str, int] = {}
    remaining = []
    for item in items:
        outcome = settled.get(item.canonical_path)
        if outcome is None:
            remaining.append(item)
        else:
            carried[outcome] = carried.get(outcome, 0) + 1
    if carried:
        logger.info(
            "Resuming after a restart: {} file(s) finished before it are not checked again", sum(carried.values())
        )
    return remaining, carried


def _queue_retry(cfg: dict, paths: list[str]) -> bool:
    """Queue a later job for the files Plex hadn't added yet, whose database wasn't writable, or (a sender's) not on
    disk yet, up to ``MAX_RETRIES`` times. Never raises."""
    attempt = int(cfg.get("retry_attempt") or 0) + 1
    if not paths or attempt > MAX_RETRIES:
        return False
    try:
        create_loudness_job(
            library_name=f"Retry: {LABEL}: {len(paths)} file(s)",
            priority=PRIORITY_LOW,
            source=str(cfg.get("source") or "retry"),
            file_paths=sorted(set(paths)),
            server_id=cfg.get("server_id"),
            retry_attempt=attempt,
            retry_delay_s=RETRY_DELAY_S * attempt,
        )
        return True
    except Exception as exc:
        logger.warning("Couldn't queue the loudness retry for {} file(s): {}", len(paths), exc)
        return False
