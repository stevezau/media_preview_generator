"""The loudness job kind: which files need it (check stage), analysing their streams (workers), and the job runner.

Runs through the same dispatcher, worker pool, priorities, job gate, pause and cancel as previews and Intro & Credits
(``job_kinds.KindHandlers``). A file is checked against each Plex server with loudness on for its library: streams
Plex already analysed are skipped, so a file Plex (or an earlier job) finished never reaches a worker.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loguru import logger

from ..config import load_config
from ..job_kinds import JOB_KIND_LOUDNESS, ItemOutcome, KindHandlers
from ..jobs.checkpoints import checkpoint_items, read_checkpoint
from ..jobs.dispatcher import get_or_create_dispatcher
from ..jobs.group_runtime import admission_options, runtime_capacity, wait_for_capacity
from ..jobs.orchestrator import _build_multi_server_registry
from ..jobs.parking import JobParked, park_if_unavailable
from ..jobs.worker import JOB_LOG_SKIP, is_job_thread_for, register_job_thread, unregister_job_thread
from ..markers.job_runner import (
    build_items,
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
from ..markers.publishers.plex_db import BUSY_TIMEOUT_S, LocalPlexDb
from ..output.plex_hash import calculate_plex_hash
from ..processing.generator import clear_failures, failure_scope, set_file_result_callback
from ..processing.retry_queue import retry_policy, scaled_backoff_delay
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import apply_inverse_path_mappings
from ..utils import redact_secrets
from ..web.job_gate import format_wait_message, get_job_gate
from ..web.jobs import JobStatus, get_job_manager
from ..web.routes._helpers import _ensure_gpu_cache
from ..web.routes.job_runner import _build_selected_gpus, _inflight_jobs, _inflight_lock
from ..web.settings_manager import get_settings_manager
from . import analyze, chain
from .deleted_sources import confirmed_deleted_paths
from .guard import GuardedLoudnessDb, create_loudness_db
from .inputs import load_file_input
from .plex_db import (
    AudioStream,
    SourceChangedError,
    SourceFingerprint,
    mark_item,
    needs_analysis,
    read_item_snapshot,
    read_streams,
    write_stream,
)
from .settings import library_chosen, load_server_loudness, loudness_libraries

LABEL = "Plex loudness"
MAX_FOLLOW_UP_DEPENDENCIES = 500
WRITTEN = "loudness_written"
UP_TO_DATE = "loudness_up_to_date"
NO_OWNERS = "loudness_no_owners"
NOT_IN_LIBRARY = "loudness_not_in_library"
# Plex's database couldn't be written just then (Plex restarting, its lock busy): tried again later, like NOT_IN_LIBRARY.
WAITING = "loudness_waiting"
FILE_NOT_FOUND = FileOutcome.FILE_NOT_FOUND.value
SOURCE_GONE = FileOutcome.SOURCE_GONE.value
FAILED = FileOutcome.FAILED.value
OUTCOME_KEYS = (WRITTEN, UP_TO_DATE, NO_OWNERS, NOT_IN_LIBRARY, WAITING, FILE_NOT_FOUND, SOURCE_GONE, FAILED)
# A file's outcome from its per-server rows: the first of these any server has.
_PRECEDENCE = (FAILED, WAITING, NOT_IN_LIBRARY, WRITTEN, UP_TO_DATE)
# Read-only lookups share the dispatcher's checking threads with preview checks: half of them at most.
CHECK_SHARE = 0.5
# A database that wasn't writable (Plex restarting, its lock busy) is checked again after this long.
RECHECK_S = 60.0


@dataclass
class LoudnessContext:
    """What every item of one job shares."""

    registry: object
    ffmpeg: str
    freeze_check: Callable[[], bool] | None = None
    # The job's pin (``job_runner.server_pin``): only this server; None = every server with loudness on.
    server_id: str | None = None
    deleted_paths: frozenset[str] = frozenset()
    _dbs: dict[str, LocalPlexDb] = field(default_factory=dict)
    # server id → (why it can't be written, "" when it can; when that was found)
    _ready: dict[str, tuple[str, float]] = field(default_factory=dict)
    _states: dict[str, Capability] = field(default_factory=dict)
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
                db = self._dbs[cfg.id] = create_loudness_db(cfg, self.registry.get(cfg.id))
            known = self._ready.get(cfg.id)
            if known is None or (known[0] and time.monotonic() - known[1] > RECHECK_S):
                report = db.file_checks(deadline=time.monotonic() + BUSY_TIMEOUT_S)
                self._states[cfg.id] = report.state
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


def _row(cfg: ServerConfig, status: str, message: str = "", *, retryable: bool = False) -> dict:
    row = {"server_id": cfg.id, "server_name": cfg.name, "server_type": "plex", "status": status, "message": message}
    if retryable:
        row["retryable"] = True
    return row


def _lookup(
    ctx: LoudnessContext, cfg: ServerConfig, path: str
) -> tuple[dict | None, list[AudioStream], list[AudioStream]]:
    """The file's audio streams in the server's Plex and those without loudness yet; or, instead, the server's row when
    there's nothing to analyse there (not in Plex yet, no audio track, the database unreadable just then)."""
    try:
        db, why = ctx.db(cfg)
        if why:
            status = WAITING if ctx._states.get(cfg.id, Capability.UNREACHABLE) is Capability.UNREACHABLE else FAILED
            return _row(cfg, status, why), [], []
        plex_paths = apply_inverse_path_mappings(path, cfg.path_mappings or [])
        streams, in_plex = read_streams(db, plex_paths, deadline=time.monotonic() + BUSY_TIMEOUT_S)
        if streams:
            source = SourceFingerprint.read(path)
            if any(not s.part_hash or s.part_size is None for s in streams):
                raise SourceChangedError("Plex has not fully indexed the audio source; retry after its scan")
            current_hash = calculate_plex_hash(path)
            source.verify()
            if any(s.part_hash != current_hash or s.part_size != source.size for s in streams):
                raise SourceChangedError("Plex's indexed source differs from the file; retry after its scan")
        needed = [s for s in streams if needs_analysis(s)]
        if streams and not needed and all(s.item_marked for s in streams) and isinstance(db, GuardedLoudnessDb):
            db.verify_streams(streams)
            source.verify()
    except (DatabaseBusyError, SourceChangedError, OSError) as exc:
        return _row(cfg, WAITING, str(exc)), [], []
    except PublishError as exc:
        status = WAITING if exc.state is Capability.UNREACHABLE else FAILED
        return _row(cfg, status, f"Couldn't read the file's audio streams in Plex: {exc}"), [], []
    if not in_plex:
        return _row(cfg, NOT_IN_LIBRARY, "Plex hasn't added this file yet"), [], []
    if not streams:
        return _row(cfg, UP_TO_DATE, "No audio track"), [], []
    return None, streams, needed


def check_item(item: ProcessableItem, *, ctx: LoudnessContext) -> ItemOutcome | None:
    """Check stage: settle a file with nothing to analyse; None sends it to a worker."""
    path = item.canonical_path
    if not os.path.isfile(path):
        if os.path.normpath(path) in ctx.deleted_paths:
            return ItemOutcome(SOURCE_GONE, "Skipped: import webhook confirmed this absent source was deleted")
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
    if cancel_check and cancel_check():
        return ItemOutcome(FAILED, "Loudness analysis cancelled")
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
        waiting = []
        db, _ = ctx.db(cfg)
        try:
            source = SourceFingerprint.read(path)
            snapshots = {
                item_id: read_item_snapshot(db, item_id, deadline=time.monotonic() + BUSY_TIMEOUT_S)
                for item_id in {s.metadata_item_id for s in streams}
            }
            if any(s.part_size is not None and s.part_size != source.size for s in streams):
                raise SourceChangedError("Plex's indexed file size differs from the source; retry after Plex scans it")
            if any(s.part_hash != calculate_plex_hash(path) for s in streams):
                raise SourceChangedError("Plex's indexed source differs from the file; retry after Plex scans it")
            source.verify()
        except (DatabaseBusyError, SourceChangedError, OSError) as exc:
            rows.append(_row(cfg, WAITING, str(exc)))
            continue
        except PublishError as exc:
            rows.append(_row(cfg, WAITING if exc.state is Capability.UNREACHABLE else FAILED, str(exc)))
            continue
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
                        written += write_stream(
                            db,
                            stream.id,
                            fields,
                            deadline=time.monotonic() + BUSY_TIMEOUT_S,
                            expected=stream,
                            source=source,
                            cancel_check=cancel_check,
                        )
            except (DatabaseBusyError, SourceChangedError) as exc:
                waiting.append(f"stream {index} ({same[0].codec}): {exc}")
                break
            except (analyze.LoudnessError, PublishError) as exc:
                if isinstance(exc, PublishError) and exc.state is Capability.UNREACHABLE:
                    waiting.append(f"stream {index} ({same[0].codec}): {exc}")
                    break
                errors.append(f"stream {index} ({same[0].codec}): {exc}")
        marked = 0
        if not errors and not waiting:
            # Plex analyses an item again unless it's marked; marked once all its streams (all versions) are done.
            for item_id in sorted({s.metadata_item_id for s in streams if not s.item_marked}):
                try:
                    with cancellable_waits(cancel_check):
                        marked += mark_item(
                            db,
                            item_id,
                            deadline=time.monotonic() + BUSY_TIMEOUT_S,
                            expected=snapshots[item_id],
                            source=source,
                            cancel_check=cancel_check,
                        )
                except (DatabaseBusyError, SourceChangedError) as exc:
                    waiting.append(f"marking item {item_id}: {exc}")
                except PublishError as exc:
                    target = waiting if exc.state is Capability.UNREACHABLE else errors
                    target.append(f"marking item {item_id}: {exc}")
        if not errors and not waiting and isinstance(db, GuardedLoudnessDb):
            try:
                current, _ = read_streams(
                    db,
                    apply_inverse_path_mappings(path, cfg.path_mappings or []),
                    deadline=time.monotonic() + BUSY_TIMEOUT_S,
                )
                if {s.identity() for s in current} != {s.identity() for s in streams}:
                    raise SourceChangedError("Plex's audio sources changed before playback verification")
                db.verify_streams(current)
                source.verify()
            except (DatabaseBusyError, SourceChangedError) as exc:
                waiting.append(str(exc))
            except PublishError as exc:
                target = waiting if exc.state is Capability.UNREACHABLE else errors
                target.append(str(exc))
        if errors:
            rows.append(_row(cfg, FAILED, "; ".join([*errors, *waiting]), retryable=bool(waiting)))
        elif waiting:
            rows.append(_row(cfg, WAITING, "; ".join(waiting)))
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


def kind_handlers(
    ctx: LoudnessContext, *, active_files_callback: Callable[[str, bool], None] | None = None
) -> KindHandlers:
    """The loudness kind's handlers for one job."""

    def call(item: ProcessableItem, fn: Callable, **kwargs) -> ItemOutcome | None:
        path = item.canonical_path
        if active_files_callback:
            active_files_callback(path, True)
        try:
            return fn(item, ctx=ctx, **kwargs)
        finally:
            if active_files_callback:
                active_files_callback(path, False)

    return KindHandlers(
        check_fn=lambda item, *, cancel_check=None: call(item, check_item),
        process_fn=lambda item, **kwargs: call(item, process_item, **kwargs),
        outcome_keys=OUTCOME_KEYS,
        check_label="Checking Plex loudness…",
        check_worker_label="Loudness",
        check_share=CHECK_SHARE,
    )


def run_loudness_job(job_id: str) -> None:
    """Run or resume one job, releasing each parked pass's queues and registry."""
    while _run_loudness_pass(job_id):
        pass


def _run_loudness_pass(job_id: str) -> bool | None:
    """Run one loudness job to completion (on its own thread)."""
    jm = get_job_manager()
    job = jm.get_job(job_id)
    if job is None or job.status is JobStatus.CANCELLED or (job.config or {}).get("is_retry_chain"):
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
    slot = {"held": False, "priority": job.priority, "kind": JOB_KIND_LOUDNESS}
    cfg = dict(job.config or {})
    chain_head = cfg.get("parent_job_id")
    jm.update_progress(job_id, current_files=[])
    if chain_head:
        jm.update_progress(chain_head, current_files=[])

    def cancel_check() -> bool:
        parent = jm.get_job(chain_head) if chain_head else None
        return jm.is_cancellation_requested(job_id) or bool(
            chain_head
            and (parent is None or parent.status is JobStatus.CANCELLED or jm.is_cancellation_requested(chain_head))
        )

    def live_priority() -> int:
        return job.priority

    def on_wait(active: int, cap: int, effective_cap: int) -> None:
        jm.update_progress(
            job_id,
            current_item=format_wait_message(active, cap, effective_cap),
        )
        jm.note_slot_wait(job_id)
        if chain_head:
            _chain_state(
                jm,
                chain_head,
                cfg,
                "queued_for_slot",
                wait_active=active,
                wait_cap=cap,
                wait_effective_cap=effective_cap,
            )

    def progress_callback(current, total, message, percent_override=None):
        percent = percent_override if percent_override is not None else (current / total * 100 if total else 0)
        jm.update_progress(job_id, percent=percent, processed_items=current, total_items=total, current_item=message)
        if chain_head:
            jm.update_progress(
                chain_head, percent=percent, processed_items=current, total_items=total, current_item=message
            )

    def active_files_callback(path: str, started: bool) -> None:
        activity = {"file_started" if started else "file_finished": path}
        jm.update_progress(job_id, **activity)
        if chain_head:
            jm.update_progress(chain_head, **activity)

    dispatcher = None
    try:
        with failure_scope(job_id):
            try:
                cfg = load_file_input(jm.config_dir, cfg)
                dependencies = dict.fromkeys([cfg.get("follows_job_id"), *(cfg.get("follows_job_ids") or [])])
                for dependency in dependencies:
                    if not wait_for_preceding_job(job_id, dependency, cancel_check):
                        jm.cancel_job(job_id)
                        return
                retry_wait_kwargs = {"progress_job_id": chain_head} if chain_head else {}
                if not wait_for_retry_time(job_id, cfg, cancel_check, **retry_wait_kwargs):
                    jm.cancel_job(job_id)
                    return
                if job.paused and not hold_pause_from_before_restart(job_id, cancel_check):
                    jm.cancel_job(job_id)
                    return
                while True:
                    if not wait_for_capacity(
                        jm,
                        job_id,
                        JOB_KIND_LOUDNESS,
                        cancel_check,
                        lambda: jm.is_pause_requested(job_id) or get_settings_manager().processing_paused,
                    ):
                        jm.cancel_job(job_id)
                        return
                    slot["priority"] = live_priority()
                    jm.note_slot_wait(job_id)
                    if not get_job_gate().acquire(
                        priority=slot["priority"],
                        cancel_check=cancel_check,
                        on_wait=on_wait,
                        **admission_options(jm, job_id, JOB_KIND_LOUDNESS),
                    ):
                        jm.cancel_job(job_id)
                        return
                    slot["held"] = True
                    if (
                        runtime_capacity(JOB_KIND_LOUDNESS)["open"]
                        and not jm.is_pause_requested(job_id)
                        and not get_settings_manager().processing_paused
                    ):
                        break
                    get_job_gate().release(slot["priority"], kind=JOB_KIND_LOUDNESS)
                    slot["held"] = False
                with logger.contextualize(**{JOB_LOG_SKIP: True}):
                    if cfg.get("parked_checkpoint") or job.config.get("resource_wait"):
                        jm.resume_parked_job(job_id)
                    else:
                        jm.start_job(job_id)
                if chain_head:
                    _chain_state(jm, chain_head, cfg, "running")
                config = load_config()
                config.server_id_filter = server_pin(cfg)
                registry = _build_multi_server_registry(config)
                if registry is None:
                    raise RuntimeError("Couldn't load the media servers configuration")
                checkpoint = (
                    read_checkpoint(jm.config_dir, job_id, cfg["parked_checkpoint"])
                    if cfg.get("parked_checkpoint")
                    else None
                )
                saved = checkpoint.get("bookkeeping", {}) if checkpoint else {}
                if checkpoint:
                    items = checkpoint_items(checkpoint)
                    warnings, sender_paths = saved.get("warnings", []), saved.get("sender_paths", {})
                else:
                    items, warnings, sender_paths = build_items(
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
                if not checkpoint:
                    # A path's saved result cannot prove its bytes or Plex metadata
                    # survived a restart. Native checks rebuild this attempt safely.
                    jm._delete_file_results(job_id)
                    jm.set_publishers(job_id, [])
                    jm.set_job_outcome(job_id, {})
                logger.info("{}: {} file(s) to check", LABEL, len(items))
                if not items:
                    _finish(jm, job_id, {}, ["No files to check.", *warnings])
                    if chain_head:
                        _finish_chain(jm, chain_head, cfg, warnings, attempt_id=job_id)
                    return
                carried = saved.get("carried", {}) if checkpoint else {}
                ctx = LoudnessContext(
                    registry=registry,
                    ffmpeg=getattr(config, "ffmpeg_path", None) or "ffmpeg",
                    freeze_check=job_freeze_check(jm, job_id),
                    server_id=server_pin(cfg),
                    deleted_paths=confirmed_deleted_paths(
                        jm.get_all_jobs(),
                        registry,
                        server_pin(cfg),
                        event_times=jm._storage.original_job_times() if jm._storage is not None else {},
                    ),
                )
                # A sender's files (a webhook, its retries) can reach the disk after the job starts; a listing's won't.
                retry_missing = bool(cfg.get("file_paths")) and sent_by_a_sender(cfg.get("source"))
                to_retry: list[str] = saved.get("to_retry", [])
                retry_previous: dict[str, dict] = saved.get("retry_previous", {})
                retry_lock = threading.Lock()

                def on_file_result(file_path, outcome, reason, worker, servers=None):
                    sender_key = sender_paths.get(file_path, file_path)
                    original = cfg.get("retry_baseline", {}).get("files", {}).get(sender_key, {})
                    pending_server = any(
                        row.get("status") in (NOT_IN_LIBRARY, WAITING) or row.get("retryable")
                        for row in (servers or [])
                    )
                    if (
                        pending_server
                        or outcome in (NOT_IN_LIBRARY, WAITING)
                        or (outcome == FILE_NOT_FOUND and retry_missing)
                    ):
                        to_retry.append(file_path)
                        with retry_lock:
                            retry_previous[sender_key] = chain.previous_result(
                                original.get("file", file_path), outcome, servers
                            )
                    jm.record_file_result(
                        chain_head or job_id,
                        original.get("file", file_path),
                        outcome,
                        reason,
                        worker,
                        servers=servers,
                        server_messages=True,
                        uncapped=bool(chain_head),
                    )
                    if chain_head:
                        jm.record_file_result(
                            job_id,
                            original.get("file", file_path),
                            outcome,
                            reason,
                            worker,
                            servers=servers,
                            server_messages=True,
                            uncapped=True,
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
                    handlers=kind_handlers(ctx, active_files_callback=active_files_callback),
                    **({"carried_state": checkpoint["state"]} if checkpoint else {}),
                )
                current = live_priority()
                if tracker.priority != current:
                    dispatcher.update_job_priority(job_id, current)

                def park_check():
                    park_if_unavailable(
                        dispatcher,
                        tracker,
                        jm,
                        job_id,
                        JOB_KIND_LOUDNESS,
                        lambda: {
                            "warnings": warnings,
                            "sender_paths": sender_paths,
                            "carried": carried,
                            "to_retry": to_retry,
                            "retry_previous": retry_previous,
                        },
                    )

                wait_releasing_slot_while_paused(
                    tracker,
                    job_id=job_id,
                    slot=slot,
                    live_priority=live_priority,
                    cancel_check=cancel_check,
                    on_wait=lambda active, cap, eff: jm.update_progress(
                        job_id, current_item=format_wait_message(active, cap, eff)
                    ),
                    park_check=park_check,
                )
                result = tracker.get_result()
                outcome = dict(result["outcome"])
                for key, count in carried.items():
                    outcome[key] = outcome.get(key, 0) + count
                jm.set_job_outcome(job_id, outcome)
                if result["cancelled"] or cancel_check():
                    jm.cancel_job(job_id)
                    return
                if chain_head:
                    _recount_chain(jm, chain_head, cfg, job_id)
                retried = bool(to_retry) and _queue_retry(job, cfg, to_retry, sender_paths, retry_previous)
                if to_retry and not retried:
                    warnings.append(f"{len(set(to_retry))} file(s) still waiting for Plex or the disk; not tried again")
                _finish(jm, job_id, outcome, warnings)
                if chain_head:
                    if not retried:
                        _finish_chain(jm, chain_head, cfg, warnings, attempt_id=job_id)
            finally:
                clear_failures()
    except JobParked:
        return True
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
            if chain_head:
                _finish_chain(jm, chain_head, cfg, [detail], attempt_id=job_id)
        except Exception as complete_exc:
            logger.warning("Could not mark loudness job {} failed: {}", job_id, complete_exc)
    finally:
        if chain_head and job.status is JobStatus.CANCELLED:
            jm.cancel_job(chain_head)
        if slot["held"]:
            try:
                get_job_gate().release(slot["priority"], kind=JOB_KIND_LOUDNESS)
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
    queued = get_job_manager().get_job(job_id)
    if queued is None or (queued.config or {}).get("is_retry_chain"):
        return
    if config_overrides:
        jm = get_job_manager()
        job = jm.get_job(job_id)
        live = (job.config or {}) if job is not None else {}
        updates = {
            k: v
            for k, v in config_overrides.items()
            if k not in {"pause_reasons", "paused_by_schedule", "resource_wait", "parked_checkpoint"}
            and live.get(k) != v
        }
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
    follows_job_ids: list[str] | None = None,
    server_id: str | None = None,
    retry_attempt: int = 0,
    retry_delay_s: int = 0,
    parent_job_id: str | None = None,
    max_retries: int = 0,
    retry_baseline: dict | None = None,
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
        follows_job_ids: Additional preview and marker jobs whose first attempts must finish before this job.
        server_id: Only this server (the preview job's pin); None = every server with loudness on.
        retry_attempt: For a retry (``_queue_retry``): which retry (1-based).
        retry_delay_s: For a retry: seconds to wait before it takes a slot.
        parent_job_id: Original job whose row represents this hidden retry.
        max_retries: Global retry limit in force when this retry was queued.
        retry_baseline: Immutable aggregate and previous-file counts for this attempt.

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
    if follows_job_ids:
        dependencies = list(dict.fromkeys([*(follows_job_ids or []), *([follows_job_id] if follows_job_id else [])]))
        if len(dependencies) > MAX_FOLLOW_UP_DEPENDENCIES:
            raise ValueError("Too many preceding jobs for one loudness follow-up")
        config["follows_job_ids"] = dependencies
    if server_id:
        config["server_id"] = server_id
    if retry_attempt:
        config["retry_attempt"] = int(retry_attempt)
        # The due time is stored so a job revived after a restart doesn't wait again in full.
        config["retry_not_before"] = (datetime.now(UTC) + timedelta(seconds=int(retry_delay_s))).isoformat()
        config["retry_delay"] = int(retry_delay_s)
    if parent_job_id:
        config.update(is_retry=True, parent_job_id=parent_job_id, max_retries=int(max_retries))
    if retry_baseline is not None:
        config["retry_baseline"] = retry_baseline
    job = get_job_manager().create_job(
        library_name=library_name, config=config, priority=priority, kind=JOB_KIND_LOUDNESS
    )
    start_loudness_job_async(job.id)
    logger.info("Created loudness job {} ({}, source={})", job.id[:8], library_name, source)
    return job


def _chain_state(jm, head_id: str, cfg: dict, state: str, **kwargs) -> None:
    """Use the shared retry lifecycle: one visible original job, hidden attempt jobs."""
    kwargs.setdefault("next_run_at", None)
    kwargs.setdefault("wait_seconds", None)
    jm.upsert_retry_chain_job(
        canonical_path="",
        basename="",
        originating_job_id=head_id,
        attempt=int(cfg.get("retry_attempt") or 0),
        max_attempts=int(cfg.get("max_retries") or 0),
        outcome=state,
        **kwargs,
    )


def _recount_chain(jm, head_id: str, cfg: dict | None = None, attempt_id: str | None = None) -> dict[str, int]:
    """Replace attempt counts with every file's latest result on the original job."""
    baseline = (cfg or {}).get("retry_baseline")
    if baseline is None and (cfg or {}).get("retry_baseline_in_input"):
        # Input validation failed before dispatch; a partial Files panel cannot
        # reconstruct the immutable accounting that was stored with that input.
        head = jm.get_job(head_id)
        return dict(head.progress.outcome or {})
    if baseline is not None and attempt_id:
        outcome, publishers = chain.replace_results(
            baseline,
            jm.get_file_results(attempt_id, dedup_by_path=True),
            cfg.get("retry_sender_paths", {}),
        )
        jm.set_job_outcome(head_id, outcome)
        jm.set_publishers(head_id, publishers)
        return outcome
    rows = jm.get_file_results(head_id, dedup_by_path=True)
    # Capped Files panels do not contain the entire job; never report their subset as the whole job.
    if any(not row.get("file") for row in rows):
        head = jm.get_job(head_id)
        return dict(head.progress.outcome or {})
    outcome = dict(Counter(row["outcome"] for row in rows))
    jm.set_job_outcome(head_id, outcome)
    return outcome


def _completion(outcome: dict[str, int], warnings: list[str]) -> tuple[int, str | None]:
    """Count verified successes; waiting and missing results can never produce a green completion."""
    successes = outcome.get(WRITTEN, 0) + outcome.get(UP_TO_DATE, 0)
    failed = outcome.get(FAILED, 0) + outcome.get(FILE_NOT_FOUND, 0)
    waiting = outcome.get(WAITING, 0) + outcome.get(NOT_IN_LIBRARY, 0)
    details = list(warnings)
    if failed:
        details.insert(0, f"{failed} file(s) failed or missing. Check the Files panel.")
    if waiting:
        details.insert(0, f"{waiting} file(s) still waiting for Plex or the disk. Check the Files panel.")
    return successes, " ".join(details) or None


def _finish(jm, job_id: str, outcome: dict[str, int], warnings: list[str]) -> None:
    """Settle one attempt, leaving a scheduled chain's lifecycle to the shared manager."""
    successes, reason = _completion(outcome, warnings)
    unresolved = sum(outcome.get(key, 0) for key in (FAILED, FILE_NOT_FOUND, WAITING, NOT_IN_LIBRARY))
    jm.complete_job(
        job_id,
        error=reason if unresolved and not successes else None,
        warning=reason if not unresolved or successes else None,
    )


def _finish_chain(jm, head_id: str, cfg: dict, warnings: list[str], *, attempt_id: str | None = None) -> None:
    """Finish from all files, preserving earlier failures outside this retry attempt."""
    outcome = _recount_chain(jm, head_id, cfg, attempt_id)
    successes, reason = _completion(outcome, warnings)
    _chain_state(jm, head_id, cfg, "exhausted" if reason else "completed", successes=successes, reason=reason)


def _queue_retry(
    job, cfg: dict, paths: list[str], sender_paths: dict[str, str], previous: dict[str, dict] | None = None
) -> list[str]:
    """Queue a hidden attempt using the global policy and original sender paths, as other job kinds do."""
    try:
        count, delay_setting = retry_policy(get_settings_manager())
        attempt = int(cfg.get("retry_attempt") or 0) + 1
        if not paths or attempt > count:
            return []
        paths = sorted({sender_paths.get(path, path) for path in paths})
        delay = scaled_backoff_delay(attempt, delay_setting)
        head_id = cfg.get("parent_job_id") or job.id
        jm = get_job_manager()
        baseline = chain.snapshot(jm.get_job(head_id), previous) if previous is not None else None
        retry = create_loudness_job(
            library_name=f"Retry: {LABEL}: {len(paths)} file(s)",
            priority=job.priority,
            source=str(cfg.get("source") or "retry"),
            file_paths=paths,
            server_id=cfg.get("server_id"),
            retry_attempt=attempt,
            retry_delay_s=delay,
            parent_job_id=head_id,
            max_retries=count,
            retry_baseline=baseline,
        )
        _chain_state(
            get_job_manager(),
            head_id,
            retry.config,
            "scheduled",
            next_run_at=retry.config.get("retry_not_before"),
            wait_seconds=delay,
        )
        return paths
    except Exception as exc:
        logger.warning("Couldn't queue the loudness retry for {} file(s): {}", len(paths), exc)
        return []
