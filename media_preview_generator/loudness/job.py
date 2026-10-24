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
from functools import partial
from typing import Any, cast

from loguru import logger

from ..config import load_config
from ..job_kinds import JOB_KIND_LOUDNESS, ItemOutcome, KindHandlers
from ..jobs.checkpoints import read_checkpoint
from ..jobs.dispatcher import get_or_create_dispatcher
from ..jobs.group_runtime import admission_options, runtime_capacity, wait_for_capacity
from ..jobs.orchestrator import _build_multi_server_registry, fold_publisher_rows_into_aggregate
from ..jobs.parking import JobParked, park_if_unavailable
from ..jobs.worker import JOB_LOG_SKIP, is_job_thread_for, register_job_thread, unregister_job_thread
from ..markers.external_ids import is_extra
from ..markers.job_log import display_name, write_line
from ..markers.job_runner import (
    FILES_SEALED,
    WEBHOOK_FILES,
    build_items,
    hold_pause_from_before_restart,
    job_freeze_check,
    sent_by_a_sender,
    server_pin,
    wait_for_retry_time,
    wait_for_tracker,
    worker_cards,
)
from ..markers.outcomes import FileOutcome
from ..markers.ownership import owning_servers
from ..markers.publishers.base import Capability, DatabaseBusyError, PublishError, cancellable_waits
from ..markers.publishers.plex_db import BUSY_TIMEOUT_S, LocalPlexDb
from ..markers.publishers.plex_remote import saved_server_node_id
from ..nodes import remote_ops
from ..nodes.executor import current_placement
from ..output.plex_hash import calculate_plex_hash
from ..processing.generator import clear_failures, failure_scope, set_file_result_callback
from ..processing.multi_server import source_replaced_reason
from ..processing.retry_queue import retry_policy, scaled_backoff_delay
from ..processing.types import ProcessableItem
from ..scan_filters import pick_filter_config
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import apply_inverse_path_mappings
from ..utils import redact_secrets
from ..web.job_gate import format_wait_message, get_job_gate, release_slot
from ..web.jobs import JobLogSink, JobStatus, get_job_manager
from ..web.routes._helpers import _ensure_gpu_cache
from ..web.routes.job_runner import _build_selected_gpus, _inflight_jobs, _inflight_lock
from ..web.settings_manager import get_settings_manager
from . import analyze, chain, silent, undecodable
from .deleted_sources import confirmed_deleted_paths
from .guard import GuardedLoudnessDb, RemoteLoudnessDb, create_loudness_db
from .inputs import load_file_input
from .plex_db import (
    PLEX_TYPE_EXTRA,
    AudioStream,
    NotAnalysedError,
    SourceChangedError,
    SourceFingerprint,
    has_silent_loudness,
    mark_item,
    needs_analysis,
    read_item_snapshot,
    read_streams,
    write_stream,
)
from .resume import CompletionLedger, delete_ledger, source_fingerprint
from .settings import library_chosen, load_server_loudness, loudness_libraries

# Serialises a webhook's files joining a waiting loudness job with its runner sealing the files it runs.
WEBHOOK_JOIN_LOCK = threading.Lock()
LABEL = "Plex loudness"
WRITTEN = "loudness_written"
UP_TO_DATE = "loudness_up_to_date"
NO_OWNERS = "loudness_no_owners"
NOT_IN_LIBRARY = "loudness_not_in_library"
# Plex's database couldn't be written just then (Plex restarting, its lock busy): tried again later, like NOT_IN_LIBRARY.
WAITING = "loudness_waiting"
# A trailer or other extra, as Plex lists it (``plex_db.PLEX_TYPE_EXTRA``), or by its path (``external_ids.is_extra``)
# when Plex doesn't list the file: Plex reads no loudness for one, as markers check none.
SKIPPED = "loudness_skipped"
EXTRAS_NOT_ANALYSED = "Extras aren't analysed for loudness"
FILE_NOT_FOUND = FileOutcome.FILE_NOT_FOUND.value
SOURCE_GONE = FileOutcome.SOURCE_GONE.value
FAILED = FileOutcome.FAILED.value
OUTCOME_KEYS = (
    WRITTEN,
    UP_TO_DATE,
    NO_OWNERS,
    NOT_IN_LIBRARY,
    WAITING,
    SKIPPED,
    FILE_NOT_FOUND,
    SOURCE_GONE,
    FAILED,
)
# A file's outcome from its per-server rows: the first of these any server has.
_PRECEDENCE = (FAILED, WAITING, NOT_IN_LIBRARY, WRITTEN, UP_TO_DATE, SKIPPED)
# Read-only lookups share the dispatcher's checking threads with preview checks: half of them at most.
CHECK_SHARE = 0.5
# A database that wasn't writable (Plex restarting, its lock busy) is checked again after this long.
RECHECK_S = 60.0
# A batch uses one decoder and loudnorm filter per track concurrently; keep its peak CPU and memory bounded.
MAX_AUDIO_BATCH = 3
DEFAULT_NICE_LEVEL = 10
MAX_NICE_LEVEL = 19


@dataclass
class LoudnessContext:
    """What every item of one job shares."""

    registry: Any
    ffmpeg: str
    freeze_check: Callable[[], bool] | None = None
    # The job's pin (``job_runner.server_pin``): only this server; None = every server with loudness on.
    server_id: str | None = None
    deleted_paths: frozenset[str] = frozenset()
    # Library scans only (``trusts_server_hash``): Plex's part hash stands in for reading the file when the size matches.
    trust_server_hash: bool = False
    _dbs: dict[str, LocalPlexDb | RemoteLoudnessDb] = field(default_factory=dict)
    # server id → (why it can't be written, "" when it can; when that was found)
    _ready: dict[str, tuple[str, float]] = field(default_factory=dict)
    _states: dict[str, Capability] = field(default_factory=dict)
    # One lock per server: a busy Plex database doesn't hold up the others' checks.
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _locks_lock: threading.Lock = field(default_factory=threading.Lock)
    # (server id, folder) pairs Plex was already asked to scan in this job.
    _scan_requested: set[tuple[str, str]] = field(default_factory=set)
    # What this ffmpeg can't decode, read from loudness.db on first use and kept current as this job records more.
    _undecodable: frozenset[tuple[str, int, str]] | None = None
    _undecodable_lock: threading.Lock = field(default_factory=threading.Lock)
    # Streams already measured silent over their whole length (``silent``), read and kept current the same way.
    _silent: frozenset[tuple[str, int, str]] | None = None
    _silent_lock: threading.Lock = field(default_factory=threading.Lock)

    def undecodable_keys(self) -> frozenset[tuple[str, int, str]]:
        """The ``(part hash, stream index, codec)`` keys this job's ffmpeg can't decode, read once per job."""
        with self._undecodable_lock:
            return self._load_undecodable()

    def note_undecodable(self, streams: list[AudioStream]) -> None:
        """Add streams this job just recorded as undecodable."""
        with self._undecodable_lock:
            if self._undecodable is not None:  # not loaded yet: the first read gets these from loudness.db
                self._undecodable = self._undecodable | {
                    (s.part_hash, s.index, s.codec) for s in streams if s.part_hash
                }

    def _load_undecodable(self) -> frozenset[tuple[str, int, str]]:
        if self._undecodable is None:
            read = undecodable.read_skipped_keys(undecodable.ffmpeg_identity(self.ffmpeg))
            if read is None:
                return frozenset()  # unreadable: not remembered, so the next file reads it again
            self._undecodable = read
        return self._undecodable

    def silent_keys(self) -> frozenset[tuple[str, int, str]]:
        """The ``(part hash, stream index, codec)`` keys already measured silent over their whole length."""
        with self._silent_lock:
            if self._silent is None:
                read = silent.read_keys()
                if read is None:
                    return frozenset()  # unreadable: the next file reads it again
                self._silent = read
            return self._silent

    def note_silent(self, streams: list[AudioStream]) -> None:
        """Add streams this job just recorded as silent."""
        with self._silent_lock:
            if self._silent is not None:
                self._silent = self._silent | {(s.part_hash, s.index, s.codec) for s in streams if s.part_hash}

    def request_plex_scan(self, cfg: ServerConfig, path: str) -> None:
        """Ask Plex to scan the file's folder, once per folder in the job, so a file it hasn't added yet turns up before
        the retry instead of whenever Plex's own scan gets to it. A failure is logged and never fails the file."""
        key = (cfg.id, os.path.dirname(path))
        with self._locks_lock:
            if key in self._scan_requested:
                return
            self._scan_requested.add(key)
        try:
            server = self.registry.get(cfg.id)
            if server is None:
                return
            server.trigger_refresh(item_id=None, remote_path=path)
            write_line(
                f"{display_name(path)}: Plex hasn't added this file yet; requested a scan of its folder from {cfg.name}"
            )
        except Exception as exc:
            logger.warning("Couldn't ask {} to scan the folder of {}: {}", cfg.name, path, exc)

    def db(self, cfg: ServerConfig) -> tuple[LocalPlexDb | RemoteLoudnessDb, str]:
        """The server's Plex database, and "" when it can be written or why not.

        A writable answer holds for the job; any other is asked again after ``RECHECK_S``, so a moment when Plex was
        restarting doesn't fail a whole library. A Plex database node this app refused (``details["node_refused"]``:
        turned off, or its new id held) is asked again for the next file: the Nodes page or the registry's next check
        lifts that at any moment, and the refusal costs no request.
        """
        with self._locks_lock:
            lock = self._locks.setdefault(cfg.id, threading.Lock())
        with lock:
            db = self._dbs.get(cfg.id)
            if db is None:
                # Kept for the job: the saved server's node is read per request, so a re-pointed server stops it.
                db = self._dbs[cfg.id] = create_loudness_db(
                    cfg, self.registry.get(cfg.id), live_node_id=partial(saved_server_node_id, cfg.id)
                )
            known = self._ready.get(cfg.id)
            if known is None or (known[0] and time.monotonic() - known[1] > RECHECK_S):
                report = db.file_checks(deadline=time.monotonic() + BUSY_TIMEOUT_S)
                self._states[cfg.id] = report.state
                known = ("" if report.state is Capability.READY else report.message, time.monotonic())
                if report.details.get("node_refused"):
                    self._ready.pop(cfg.id, None)
                else:
                    self._ready[cfg.id] = known
            return db, known[0]


def loudness_nice_level() -> int:
    """The saved ``loudness_nice_level``, read per analysis so a change applies to the next file.

    Returns:
        The level clamped to 0..19; 10 when it is missing or isn't a number (settings.json can be hand-edited).
    """
    try:
        level = int(get_settings_manager().get("loudness_nice_level", DEFAULT_NICE_LEVEL))
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_NICE_LEVEL
    return max(0, min(MAX_NICE_LEVEL, level))


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
    row: dict[str, Any] = {
        "server_id": cfg.id,
        "server_name": cfg.name,
        "server_type": "plex",
        "status": status,
        "message": message,
    }
    if retryable:
        row["retryable"] = True
    return row


def _read_source(path: str) -> SourceFingerprint | None:
    """The file's identity from one stat, or None when it isn't a readable regular file (as ``os.path.isfile``)."""
    try:
        return SourceFingerprint.read(path)
    except SourceChangedError:
        return None


# Sources whose jobs list a whole library (started by hand or by a schedule), not files a sender just reported.
_LIBRARY_SCAN_SOURCES = frozenset({"manual", "schedule"})


def trusts_server_hash(cfg: dict) -> bool:
    """Whether the job may use Plex's part hash without reading the file: a library scan, not a webhook, a picked
    file list or a retry, where Plex may not have rescanned the file yet. An unknown source reads the file."""
    return cfg.get("source") in _LIBRARY_SCAN_SOURCES and not cfg.get("file_paths") and not cfg.get("retry_attempt")


def _lookup(
    ctx: LoudnessContext,
    cfg: ServerConfig,
    path: str,
    source: SourceFingerprint | None = None,
    *,
    trust_server_hash: bool = False,
) -> tuple[dict | None, list[AudioStream], list[AudioStream]]:
    """The file's audio streams in the server's Plex and those to measure (without loudness yet, or stored as ``-inf``
    and not measured again since, ``_remeasure_silent``); or, instead, the server's row when
    there's nothing to analyse there (not in Plex yet, no audio track, the database unreadable just then).

    ``source`` is the file's identity the caller already read; the one re-stat at the end proves it didn't change.
    ``trust_server_hash`` skips reading the file when Plex's recorded size equals its current size."""
    try:
        db, why = ctx.db(cfg)
        if why:
            status = WAITING if ctx._states.get(cfg.id, Capability.UNREACHABLE) is Capability.UNREACHABLE else FAILED
            return _row(cfg, status, why), [], []
        plex_paths = apply_inverse_path_mappings(path, cfg.path_mappings or [])
        streams, in_plex = read_streams(db, plex_paths, deadline=time.monotonic() + BUSY_TIMEOUT_S)
        if streams:
            source = source or SourceFingerprint.read(path)
            if any(not s.part_hash or s.part_size is None for s in streams):
                raise SourceChangedError("Plex has not fully indexed the audio source; retry after its scan")
            current_hash = None if trust_server_hash else calculate_plex_hash(path, fingerprint=source[1:])
            source.verify()
            if any(
                (current_hash is not None and s.part_hash != current_hash) or s.part_size != source.size
                for s in streams
            ):
                raise SourceChangedError("Plex's indexed source differs from the file; retry after its scan")
        skipped = ctx.undecodable_keys()
        needed = [
            s
            for s in streams
            if not undecodable.is_skipped(s, skipped) and (needs_analysis(s) or _remeasure_silent(ctx, db, s))
        ]
        if (
            streams
            and not needed
            and all(s.item_marked for s in streams)
            and isinstance(db, GuardedLoudnessDb | RemoteLoudnessDb)
        ):
            # verify_streams requires complete loudness on every stream it is given, which undecodable ones never have.
            db.verify_streams([s for s in streams if not undecodable.is_skipped(s, skipped)])
            cast(SourceFingerprint, source).verify()
    except (DatabaseBusyError, SourceChangedError, OSError) as exc:
        return _row(cfg, WAITING, str(exc)), [], []
    except NotAnalysedError as exc:
        if exc.plex_type == PLEX_TYPE_EXTRA:
            return _row(cfg, SKIPPED, EXTRAS_NOT_ANALYSED), [], []
        return _row(cfg, FAILED, f"Couldn't read the file's audio streams in Plex: {exc}"), [], []
    except PublishError as exc:
        status = WAITING if exc.state is Capability.UNREACHABLE else FAILED
        return _row(cfg, status, f"Couldn't read the file's audio streams in Plex: {exc}"), [], []
    if not in_plex and is_extra(path):
        # Plex ignores some extras (samples) and lists the rest as extras: waiting for it to add one would only retry.
        return _row(cfg, SKIPPED, EXTRAS_NOT_ANALYSED), [], []
    if not in_plex:
        return _row(cfg, NOT_IN_LIBRARY, "Plex hasn't added this file yet"), [], []
    if not streams:
        return _row(cfg, UP_TO_DATE, "No audio track"), [], []
    return None, streams, needed


def _remeasure_silent(ctx: LoudnessContext, db: LocalPlexDb | RemoteLoudnessDb, stream: AudioStream) -> bool:
    """Whether a stream stored as ``-inf`` gets measured again: once per file, on a local Plex database only, since a
    Plex database node can't replace stored loudness. Older releases stored ``-inf`` for audio that changed near its
    end (``silent``)."""
    return (
        not isinstance(db, RemoteLoudnessDb)
        and has_silent_loudness(stream.extra_data)
        and (stream.part_hash, stream.index, stream.codec) not in ctx.silent_keys()
    )


def _skip_undecodable(
    path: str, index: int, same: list[AudioStream], exc: analyze.LoudnessCantMeasure, ffmpeg: str
) -> tuple[bool, str]:
    """Remember a stream this ffmpeg can't measure so it isn't tried again until the file or the ffmpeg changes.

    Returns:
        Whether every row of the stream was remembered, and the text for the outcome row.
    """
    codec = same[0].codec
    if isinstance(exc, analyze.LoudnessNoDecoder):
        why, what = "FFmpeg can't decode it", f"ffmpeg can't decode audio track {index} ({codec})"
    else:
        why, what = "its audio format changes mid-track", f"audio track {index} ({codec}) changes format mid-track"
    identity = undecodable.ffmpeg_identity(ffmpeg)
    remembered = all(undecodable.record(s.part_hash, s.index, s.codec, str(exc), identity) for s in same)
    if not remembered:
        return False, f"stream {index} ({codec}): {why} and that couldn't be remembered: {exc}"
    write_line(f"{display_name(path)}: {what}; loudness skips it")
    logger.bind(**{JOB_LOG_SKIP: True}).info("{}: {}; loudness skips it", os.path.basename(path), what)
    return True, f"stream {index} ({codec}) skipped: {why}"


def _not_on_disk(path: str, ctx: LoudnessContext) -> ItemOutcome:
    """The outcome of a file not on disk, by previews' rule (``source_replaced_reason``): one a newer file replaced in
    its folder is gone for good and queues no retry; any other may still be copying in, so a webhook's job retries it."""
    replaced = source_replaced_reason(path, ctx.registry)
    if replaced is None:
        write_line(f"{display_name(path)}: not on disk")
        return ItemOutcome(FILE_NOT_FOUND, "Not on disk")
    logger.bind(**{JOB_LOG_SKIP: True}).info(
        "Source file {} is no longer on disk and a newer file took its place ({}); skipping without a retry. "
        "The newer file gets its own loudness run from its own webhook or the next scan.",
        path,
        replaced.removeprefix("Skipped: "),
    )
    write_line(f"{display_name(path)}: {replaced}")
    return ItemOutcome(SOURCE_GONE, replaced)


def check_item(item: ProcessableItem, *, ctx: LoudnessContext) -> ItemOutcome | None:
    """Check stage: settle a file with nothing to analyse; None sends it to a worker."""
    path = item.canonical_path
    source = _read_source(path)
    if source is None:
        if os.path.normpath(path) in ctx.deleted_paths:
            deleted = "Skipped: import webhook confirmed this absent source was deleted"
            write_line(f"{display_name(path)}: {deleted}")
            return ItemOutcome(SOURCE_GONE, deleted)
        return _not_on_disk(path, ctx)
    servers = owners(path, ctx.registry, ctx.server_id)
    if not servers:
        return ItemOutcome(NO_OWNERS, "No Plex server has loudness on for its library")
    rows = []
    for cfg in servers:
        row, streams, todo = _lookup(ctx, cfg, path, source, trust_server_hash=ctx.trust_server_hash)
        if row is not None:
            rows.append(row)
            if row["status"] == NOT_IN_LIBRARY:
                ctx.request_plex_scan(cfg, path)
        elif todo or not all(s.item_marked for s in streams):
            # Analysis to do, or an item analysed but not yet marked (Plex would analyse it again).
            return None
        else:
            rows.append(_row(cfg, UP_TO_DATE))
    outcome = _settle(rows)
    if outcome.outcome_key == SKIPPED:
        write_line(f"{display_name(path)}: extras aren't analysed for loudness")
    return outcome


def _stream_progress_reporter(
    progress_callback: Callable[[float, float, float, str | None, float | None], None] | None,
    *,
    n: int,
    streams: int,
    durations_s: list[float],
) -> Callable[[float, float | None], None] | None:
    """The ``analyze.run`` progress hook for stream ``n`` (1-based) of ``streams``, in the worker callback's terms.

    The percent spans the whole file: finished streams plus this one's fraction, each stream counting equally. The ETA
    is this stream's remainder plus the later streams' full lengths, at the current speed.
    """
    if progress_callback is None:
        return None
    total_s = durations_s[n - 1]
    later_s = sum(durations_s[n:])

    def report(processed_s: float, speed_x: float | None) -> None:
        fraction = min(1.0, processed_s / total_s) if total_s > 0 else 0.0
        percent = max(0.0, min(100.0, (n - 1 + fraction) / streams * 100))
        remaining = (max(0.0, total_s - processed_s) + later_s) / speed_x if speed_x and total_s > 0 else None
        progress_callback(percent, processed_s, total_s, f"{speed_x:.1f}x" if speed_x else None, remaining)

    return report


def _batch_progress_reporter(
    progress_callback: Callable[[float, float, float, str | None, float | None], None] | None,
    *,
    first: int,
    count: int,
    streams: int,
    duration_s: float,
    later_s: float,
) -> Callable[[float, float | None], None] | None:
    """Report parallel audio tracks as one file-time span covering ``count`` stream slots."""
    if progress_callback is None:
        return None

    def report(processed_s: float, speed_x: float | None) -> None:
        fraction = min(1.0, processed_s / duration_s) if duration_s > 0 else 0.0
        percent = max(0.0, min(100.0, (first - 1 + count * fraction) / streams * 100))
        remaining = (max(0.0, duration_s - processed_s) + later_s) / speed_x if speed_x and duration_s > 0 else None
        progress_callback(percent, processed_s, duration_s, f"{speed_x:.1f}x" if speed_x else None, remaining)

    return report


def process_item(
    item: ProcessableItem,
    *,
    ctx: LoudnessContext,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
    phase_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[float, float, float, str | None, float | None], None] | None = None,
    **_ignored,
) -> ItemOutcome:
    """Analyse missing audio in groups of at most three, then write each result through Plex's guarded path.

    Each group reads the file once; a file with more than three missing streams needs multiple input passes.
    A GPU worker runs this audio work on the CPU too.
    """
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
        skipped: list[str] = []
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
        ordered = sorted(todo.items())
        for index, same in ordered:
            if has_silent_loudness(same[0].extra_data):
                write_line(
                    f"{display_name(path)}: audio track {index} ({same[0].codec}) was stored as silent; "
                    "measuring it again over the whole track"
                )
        durations_s = [(same[0].duration_ms or 0) / 1000 for _index, same in ordered]
        stop_analysis = False
        for start in range(0, len(ordered), MAX_AUDIO_BATCH):
            group = ordered[start : start + MAX_AUDIO_BATCH]
            batch_fields: dict[int, dict[str, str] | analyze.LoudnessError] = {}
            if len(group) > 1:
                if cancel_check and cancel_check():
                    errors.append(f"cancelled before stream {group[0][0]}")
                    break
                if phase_callback:
                    phase_callback(f"Loudness {start + 1}-{start + len(group)}/{len(ordered)}")
                # A remote worker measures on its node; the writes below stay here.
                run_many = remote_ops.loudness_run_many if current_placement() else analyze.run_many
                try:
                    batch_fields = run_many(
                        ctx.ffmpeg,
                        path,
                        [(index, same[0].codec, same[0].duration_ms) for index, same in group],
                        cancel_check=cancel_check,
                        pause_check=ctx.freeze_check or pause_check,
                        nice_level=loudness_nice_level(),
                        on_progress=_batch_progress_reporter(
                            progress_callback,
                            first=start + 1,
                            count=len(group),
                            streams=len(ordered),
                            duration_s=max(durations_s[start : start + len(group)]),
                            later_s=sum(
                                max(durations_s[next_start : next_start + MAX_AUDIO_BATCH])
                                for next_start in range(start + len(group), len(ordered), MAX_AUDIO_BATCH)
                            ),
                        ),
                    )
                except (analyze.LoudnessCancelled, analyze.LoudnessTimeout) as exc:
                    errors.append(f"streams {[index for index, _same in group]}: {exc}")
                    break
                except analyze.LoudnessError as exc:
                    if cancel_check and cancel_check():
                        errors.append(f"cancelled before retrying streams {[index for index, _same in group]}")
                        break
                    try:
                        source.verify()
                    except (SourceChangedError, OSError) as changed:
                        waiting.append(str(changed))
                        break
                    logger.warning(
                        "Loudness batch failed for {}: {}; retrying its streams separately", os.path.basename(path), exc
                    )
            for offset, (index, same) in enumerate(group):
                n = start + offset + 1
                if cancel_check and cancel_check():
                    errors.append(f"cancelled before stream {index}")
                    stop_analysis = True
                    break
                if phase_callback and not batch_fields:
                    phase_callback(f"Loudness {n}/{len(todo)}")
                try:
                    fields = batch_fields.get(index)
                    if isinstance(fields, analyze.LoudnessError):
                        # A single-track run would hit the same deterministic failure.
                        errors.append(f"stream {index} ({same[0].codec}): {fields}")
                        continue
                    if fields is None:
                        run_one = remote_ops.loudness_run if current_placement() else analyze.run
                        fields = run_one(
                            ctx.ffmpeg,
                            path,
                            index,
                            duration_ms=same[0].duration_ms,
                            codec=same[0].codec,
                            cancel_check=cancel_check,
                            pause_check=ctx.freeze_check or pause_check,
                            nice_level=loudness_nice_level(),
                            on_progress=_stream_progress_reporter(
                                progress_callback, n=n, streams=len(todo), durations_s=durations_s
                            ),
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
                                replace_silent=has_silent_loudness(stream.extra_data),
                            )
                    if fields["ln:loudness"] == "-inf" and all(
                        silent.record(s.part_hash, s.index, s.codec) for s in same
                    ):
                        ctx.note_silent(same)
                    if progress_callback and (not batch_fields or offset == len(group) - 1):
                        try:
                            progress_callback(n / len(todo) * 100, durations_s[n - 1], durations_s[n - 1], None, 0.0)
                        except Exception as exc:  # a display error must not lose a finished measurement
                            logger.warning("Loudness progress callback failed: {}", exc)
                except (DatabaseBusyError, SourceChangedError) as exc:
                    waiting.append(f"stream {index} ({same[0].codec}): {exc}")
                    stop_analysis = True
                    break
                except analyze.LoudnessCantMeasure as exc:
                    remembered, text = _skip_undecodable(path, index, same, exc, ctx.ffmpeg)
                    if remembered:
                        ctx.note_undecodable(same)
                    (skipped if remembered else errors).append(text)
                except (analyze.LoudnessError, PublishError) as exc:
                    if isinstance(exc, PublishError) and exc.state is Capability.UNREACHABLE:
                        waiting.append(f"stream {index} ({same[0].codec}): {exc}")
                        stop_analysis = True
                        break
                    errors.append(f"stream {index} ({same[0].codec}): {exc}")
            if stop_analysis:
                break
        marked = 0
        if not errors and not waiting:
            # Plex analyses an item again unless it's marked; marked once all its streams (all versions) are measured or recorded undecodable.
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
                            skipped=undecodable.skipped_keys(undecodable.ffmpeg_identity(ctx.ffmpeg)),
                        )
                except (DatabaseBusyError, SourceChangedError) as exc:
                    waiting.append(f"marking item {item_id}: {exc}")
                except PublishError as exc:
                    target = waiting if exc.state is Capability.UNREACHABLE else errors
                    target.append(f"marking item {item_id}: {exc}")
        if not errors and not waiting and isinstance(db, GuardedLoudnessDb | RemoteLoudnessDb):
            try:
                current, _ = read_streams(
                    db,
                    apply_inverse_path_mappings(path, cfg.path_mappings or []),
                    deadline=time.monotonic() + BUSY_TIMEOUT_S,
                )
                if {s.identity() for s in current} != {s.identity() for s in streams}:
                    raise SourceChangedError("Plex's audio sources changed before playback verification")
                skipped_keys = undecodable.skipped_keys(undecodable.ffmpeg_identity(ctx.ffmpeg))
                db.verify_streams([s for s in current if not undecodable.is_skipped(s, skipped_keys)])
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
        elif written:
            done = [f"{written} stream(s)"] + (["item marked analysed"] if marked else []) + skipped
            rows.append(_row(cfg, WRITTEN, ", ".join(done)))
        elif marked:
            done = ["Item marked analysed" if skipped else "Existing loudness reused; item marked analysed", *skipped]
            rows.append(_row(cfg, UP_TO_DATE, ", ".join(done)))
        else:
            rows.append(_row(cfg, UP_TO_DATE, ", ".join(skipped)))
    if not rows:
        return ItemOutcome(NO_OWNERS, "No Plex server has loudness on for its library")
    return _settle(rows)


def _settle(rows: list[dict]) -> ItemOutcome:
    """The file's outcome from its per-server rows (``_PRECEDENCE``): a failure anywhere fails it, a server still to
    try again keeps it for the retry, else written beats up to date."""
    statuses = {r["status"] for r in rows}
    key = next(k for k in _PRECEDENCE if k in statuses)
    # Two servers saying the same thing ("Extras aren't analysed for loudness") say it once.
    message = "; ".join(dict.fromkeys(r["message"] for r in rows if r["status"] == key and r["message"]))
    return ItemOutcome(key, message, publisher_rows=rows)


def kind_handlers(
    ctx: LoudnessContext,
    *,
    active_files_callback: Callable[[str, bool], None] | None = None,
    completion_ledger: CompletionLedger | None = None,
) -> KindHandlers:
    """The loudness kind's handlers for one job."""

    def call(item: ProcessableItem, fn: Callable, **kwargs) -> ItemOutcome | None:
        path = item.canonical_path
        if active_files_callback:
            active_files_callback(path, True)
        try:
            before = source_fingerprint(item) if completion_ledger is not None else None
            result = fn(item, ctx=ctx, **kwargs)
            if completion_ledger is not None and result is not None:
                completion_ledger.record(item, result, before)
            return result
        finally:
            if active_files_callback:
                active_files_callback(path, False)

    return KindHandlers(
        check_fn=lambda item, *, cancel_check=None: call(item, check_item),
        process_fn=cast(Callable[..., ItemOutcome], lambda item, **kwargs: call(item, process_item, **kwargs)),
        outcome_keys=OUTCOME_KEYS,
        check_label="Checking Plex loudness…",
        check_worker_label="Loudness",
        check_share=CHECK_SHARE,
    )


def _seal_files(jm, job_id: str, cfg: dict) -> dict:
    """A webhook's job's files as of now: files later webhooks report join it until this seal (``markers.triggers``)."""
    with WEBHOOK_JOIN_LOCK:
        jm.merge_job_config(job_id, {FILES_SEALED: True})
        latest = jm.get_job(job_id)
        paths = ((latest.config if latest is not None else None) or {}).get("file_paths") or cfg.get("file_paths")
    return {**cfg, "file_paths": list(paths or []), FILES_SEALED: True}


def run_loudness_job(job_id: str) -> None:
    """Run or resume one job, releasing each parked pass's queues and registry."""
    while _run_loudness_pass(job_id):
        pass


def _run_loudness_pass(job_id: str) -> bool | None:
    """Run one loudness job to completion (on its own thread)."""
    jm = get_job_manager()
    job = jm.get_job(job_id)
    if job is None or job.status is JobStatus.CANCELLED or (job.config or {}).get("is_retry_chain"):
        return None
    settings = get_settings_manager()
    if settings.processing_paused:
        logger.info("Loudness job {} not started: processing is paused; job stays pending", job_id)
        return None
    register_job_thread(job_id)
    handler_id = logger.add(
        JobLogSink(jm, job_id),
        level=str(settings.get("log_level", "INFO")).upper(),
        format="{message}",
        filter=lambda record: not record["extra"].get(JOB_LOG_SKIP) and is_job_thread_for(record["thread"].id, job_id),
    )
    # Held from the gate until the files are submitted to the dispatcher (or the job ends first).
    slot = {"held": False}
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

    def on_wait(active: int) -> None:
        jm.update_progress(job_id, current_item=format_wait_message(active))
        jm.note_slot_wait(job_id)
        if chain_head:
            _chain_state(jm, chain_head, cfg, "queued_for_slot", wait_active=active)

    def held_by_pause() -> bool:
        return jm.is_pause_requested(job_id) or get_settings_manager().processing_paused

    def progress_callback(current, total, message, percent_override=None):
        percent = percent_override if percent_override is not None else (current / total * 100 if total else 0)
        jm.update_progress(job_id, percent=percent, processed_items=current, total_items=total, current_item=message)
        if chain_head:
            jm.update_progress(
                chain_head, percent=percent, processed_items=current, total_items=total, current_item=message
            )

    def active_files_callback(path: str, started: bool) -> None:
        activity: dict[str, Any] = {"file_started" if started else "file_finished": path}
        jm.update_progress(job_id, **activity)
        if chain_head:
            jm.update_progress(chain_head, **activity)

    def acquire_slot() -> bool:
        while True:
            if not wait_for_capacity(jm, job_id, JOB_KIND_LOUDNESS, cancel_check, held_by_pause):
                return False
            jm.note_slot_wait(job_id)
            if not get_job_gate().acquire(
                priority=live_priority(),
                cancel_check=cancel_check,
                on_wait=on_wait,
                **admission_options(jm, job_id, JOB_KIND_LOUDNESS),
            ):
                return False
            slot["held"] = True
            if runtime_capacity(JOB_KIND_LOUDNESS)["open"] and not held_by_pause():
                return True
            release_slot(slot, get_job_gate())

    dispatcher = None
    completion_ledger = None
    try:
        with failure_scope(job_id):
            try:
                cfg = load_file_input(jm.config_dir, cfg)
                retry_wait_kwargs = {"progress_job_id": chain_head} if chain_head else {}
                if not wait_for_retry_time(job_id, cfg, cancel_check, **retry_wait_kwargs):
                    jm.cancel_job(job_id)
                    return None
                if job.paused and not hold_pause_from_before_restart(job_id, cancel_check):
                    jm.cancel_job(job_id)
                    return None
                if not acquire_slot():
                    jm.cancel_job(job_id)
                    return None
                with logger.contextualize(**{JOB_LOG_SKIP: True}):
                    if cfg.get("parked_checkpoint") or job.config.get("resource_wait"):
                        jm.resume_parked_job(job_id)
                    else:
                        jm.start_job(job_id)
                if chain_head:
                    _chain_state(jm, chain_head, cfg, "running")
                if cfg.get(WEBHOOK_FILES):
                    cfg = _seal_files(jm, job_id, cfg)
                config = load_config()
                config.server_id_filter = server_pin(cfg)
                registry = _build_multi_server_registry()
                if registry is None:
                    raise RuntimeError("Couldn't load the media servers configuration")
                if cfg.get("parked_checkpoint"):
                    read_checkpoint(jm.config_dir, job_id, cfg["parked_checkpoint"])
                completion_ledger = CompletionLedger(jm.config_dir, job_id)
                selection = completion_ledger.selection()
                if selection is None:
                    items, warnings, sender_paths = build_items(
                        cfg,
                        registry=registry,
                        cancel_check=cancel_check,
                        progress_callback=progress_callback,
                        enabled=lambda server: server.type is ServerType.PLEX and load_server_loudness(server).enabled,
                        libraries=loudness_libraries,
                        label=LABEL,
                        sort_by=cfg.get("sort_by"),
                    )
                    if not cancel_check():
                        completion_ledger.save_selection(items, warnings, sender_paths)
                else:
                    items, warnings, sender_paths = selection
                cfg["retry_sender_paths"] = {**cfg.get("retry_sender_paths", {}), **sender_paths}
                if cancel_check():
                    jm.cancel_job(job_id)
                    return None
                logger.info("{}: {} file(s) to check", LABEL, len(items))
                if not items:
                    _finish(jm, job_id, {}, ["No files to check.", *warnings])
                    if chain_head:
                        _finish_chain(jm, chain_head, cfg, warnings, attempt_id=job_id)
                    return None
                carried: dict[str, int] = {}
                loudness_ffmpeg = analyze.resolve_ffmpeg(getattr(config, "ffmpeg_path", None) or "ffmpeg")
                logger.info("{}: using {}", LABEL, loudness_ffmpeg)
                ctx = LoudnessContext(
                    registry=registry,
                    ffmpeg=loudness_ffmpeg,
                    freeze_check=job_freeze_check(jm, job_id),
                    server_id=server_pin(cfg),
                    trust_server_hash=trusts_server_hash(cfg),
                    deleted_paths=confirmed_deleted_paths(
                        jm.get_all_jobs(),
                        registry,
                        server_pin(cfg),
                        event_times=jm._storage.original_job_times() if jm._storage is not None else {},
                    ),
                )

                capacity_lock = threading.Lock()

                def validate_saved(item: ProcessableItem) -> ItemOutcome | None:
                    if cancel_check():
                        raise InterruptedError("Loudness resume cancelled")
                    # Called from several threads; one at a time releases the slot and waits, the rest queue behind it.
                    with capacity_lock:
                        if held_by_pause() or not runtime_capacity(JOB_KIND_LOUDNESS)["open"]:
                            # A wait here can last hours (a pause, closed worker hours); holding a start-up slot
                            # through it would stall every other job's start-up.
                            release_slot(slot, get_job_gate())
                            if not wait_for_capacity(jm, job_id, JOB_KIND_LOUDNESS, cancel_check, held_by_pause):
                                raise InterruptedError("Loudness resume cancelled")
                    return check_item(item, ctx=ctx)

                def report_restore_progress(restored: int, total: int) -> None:
                    jm.update_progress(
                        job_id,
                        current_item=f"Validating saved loudness progress… {restored}/{total}",
                    )

                items, carried_state = completion_ledger.restore(
                    items,
                    check=validate_saved,
                    cancel_check=cancel_check,
                    progress=report_restore_progress,
                )
                if cancel_check():
                    jm.cancel_job(job_id)
                    return None
                _restore_file_history(jm, job_id, chain_head, cfg, completion_ledger)
                jm.set_publishers(job_id, list(carried_state["publishers_aggregate"].values()))
                jm.set_job_outcome(job_id, carried_state["outcome_counts"])
                restored = carried_state["successful"]
                total = len(items) + restored
                progress_callback(restored, total, f"Resuming loudness: {restored}/{total} already completed")
                if restored:
                    logger.info("{}: restored {} completed file(s); {} remaining", LABEL, restored, len(items))
                if not items:
                    _finish(jm, job_id, carried_state["outcome_counts"], warnings)
                    if chain_head:
                        _finish_chain(
                            jm, chain_head, cfg, warnings, attempt_id=job_id, completion_ledger=completion_ledger
                        )
                    return None
                # A sender's files (a webhook, its retries) can reach the disk after the job starts; a listing's won't.
                retry_missing = bool(cfg.get("file_paths")) and sent_by_a_sender(cfg.get("source"))
                to_retry: list[str] = []
                retry_previous: dict[str, dict] = {}
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
                        "pause_check": held_by_pause,
                    },
                    priority=live_priority(),
                    kind=JOB_KIND_LOUDNESS,
                    handlers=kind_handlers(
                        ctx, active_files_callback=active_files_callback, completion_ledger=completion_ledger
                    ),
                    carried_state=carried_state,
                )
                release_slot(slot, get_job_gate())
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

                wait_for_tracker(tracker, cancel_check=cancel_check, park_check=park_check)
                result = tracker.get_result()
                if completion_ledger.error:
                    raise RuntimeError(completion_ledger.error)
                outcome = dict(result["outcome"])
                for key, count in carried.items():
                    outcome[key] = outcome.get(key, 0) + count
                jm.set_job_outcome(job_id, outcome)
                if result["cancelled"] or cancel_check():
                    jm.cancel_job(job_id)
                    return None
                if chain_head:
                    _recount_chain(jm, chain_head, cfg, job_id, completion_ledger=completion_ledger)
                retried = bool(to_retry) and _queue_retry(job, cfg, to_retry, sender_paths, retry_previous)
                if to_retry and not retried:
                    warnings.append(f"{len(set(to_retry))} file(s) still waiting for Plex or the disk; not tried again")
                missing_not_queued = 0 if retry_missing else outcome.get(FILE_NOT_FOUND, 0)
                _finish(jm, job_id, outcome, warnings, retry_queued=bool(retried), unretried=missing_not_queued)
                if chain_head:
                    if not retried:
                        _finish_chain(
                            jm, chain_head, cfg, warnings, attempt_id=job_id, completion_ledger=completion_ledger
                        )
            finally:
                clear_failures()
    except JobParked:
        return True
    except InterruptedError:
        jm.cancel_job(job_id)
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
                _finish_chain(jm, chain_head, cfg, [detail], attempt_id=job_id, completion_ledger=completion_ledger)
        except Exception as complete_exc:
            logger.warning("Could not mark loudness job {} failed: {}", job_id, complete_exc)
    finally:
        if completion_ledger is not None:
            completion_ledger.close()
            # Only a job that can still resume needs its ledger; a stop leaves the job RUNNING for revival.
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                try:
                    delete_ledger(jm.config_dir, job_id)
                except OSError as exc:
                    logger.warning("Could not remove loudness continuation for {}: {}", job_id, exc)
        if chain_head and job.status is JobStatus.CANCELLED:
            jm.cancel_job(chain_head)
        release_slot(slot, get_job_gate())
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
    return None


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
            run_loudness_job(job_id)
        finally:
            finish_admission(job_id, admission_owner)
            with _inflight_lock:
                _inflight_jobs.discard(job_id)

    try:
        threading.Thread(target=_run, daemon=True, name=f"run_job_loudness_{job_id}").start()
    except BaseException:
        finish_admission(job_id, admission_owner)
        with _inflight_lock:
            _inflight_jobs.discard(job_id)
        raise


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
    parent_job_id: str | None = None,
    max_retries: int = 0,
    retry_baseline: dict | None = None,
    parent_schedule_id: str = "",
    scan_filters: dict | None = None,
    webhook_files: bool = False,
    sort_by: str | None = None,
):
    """Create and start a loudness job.

    Args:
        library_name: Job title shown in the queue.
        priority: 1 high, 2 normal, 3 low.
        source: What created it (``manual``, a webhook source).
        libraries: ``[{"server_id", "library_id"}]``; empty with no ``file_paths`` = every library loudness is on for.
        file_paths: Explicit files or folders instead of libraries.
        follows_job_id: The preview job a follow-up was queued for. Shown as its companion; never waited for.
        server_id: Only this server (the webhook's or the preview job's pin); None = every server with loudness on.
        retry_attempt: For a retry (``_queue_retry``): which retry (1-based).
        retry_delay_s: Seconds to wait before it takes a slot: a retry's delay, or a webhook's.
        parent_job_id: Original job whose row represents this hidden retry.
        max_retries: Global retry limit in force when this retry was queued.
        retry_baseline: Immutable aggregate and previous-file counts for this attempt.
        parent_schedule_id: Schedule that created it (a stop time pauses and resumes the job by it).
        scan_filters: Scan-filter keys (``scan_filters.FILTER_CONFIG_KEYS``) limiting which library files are listed;
            other keys are ignored. Not used with ``file_paths``.
        webhook_files: A webhook's job: files later webhooks from the same sender report join it until it runs
            (``markers.triggers``).
        sort_by: Processing Order for a library listing: ``newest``, ``oldest`` or ``random``; None keeps the
            season-folder order. Not used with ``file_paths``.

    Returns:
        The created job.
    """
    config: dict[str, Any] = {
        **pick_filter_config(scan_filters),
        "kind": JOB_KIND_LOUDNESS,
        "source": source,
        "libraries": list(libraries or []),
        "file_paths": list(file_paths or []),
        "follows_job_id": follows_job_id,
    }
    if server_id:
        config["server_id"] = server_id
    if sort_by:
        config["sort_by"] = sort_by
    if retry_attempt:
        config["retry_attempt"] = int(retry_attempt)
    if webhook_files:
        config[WEBHOOK_FILES] = True
    if retry_attempt or retry_delay_s:
        # The due time is stored so a job revived after a restart doesn't wait again in full.
        config["retry_not_before"] = (datetime.now(UTC) + timedelta(seconds=int(retry_delay_s))).isoformat()
        config["retry_delay"] = int(retry_delay_s)
    if parent_job_id:
        config.update(is_retry=True, parent_job_id=parent_job_id, max_retries=int(max_retries))
    if retry_baseline is not None:
        config["retry_baseline"] = retry_baseline
    job = get_job_manager().create_job(
        library_name=library_name,
        config=config,
        priority=priority,
        kind=JOB_KIND_LOUDNESS,
        parent_schedule_id=parent_schedule_id,
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


def _publisher_rows(row: dict) -> list[dict]:
    return [
        {
            "server_id": server["id"],
            "server_name": server["name"],
            "server_type": server["type"],
            "status": server["status"],
        }
        for server in row.get("servers", [])
    ]


def _restore_file_history(jm, job_id: str, head_id: str | None, cfg: dict, ledger: CompletionLedger) -> None:
    """Repair a crash between the durable completion and its display-only callback."""
    restored = ledger.current_results()
    if not restored:
        return
    for target_id in dict.fromkeys([job_id, *([head_id] if head_id else [])]):
        existing = {row["file"]: row for row in jm.get_file_results(target_id) if row.get("file")}
        for row in restored:
            sender = cfg.get("retry_sender_paths", {}).get(row["file"], row["file"])
            original = cfg.get("retry_baseline", {}).get("files", {}).get(sender, {})
            path = original.get("file", sender if head_id else row["file"])
            previous = existing.get(path)
            if previous and previous.get("outcome") == row["outcome"]:
                normalized = chain.previous_result(path, previous["outcome"], previous.get("servers"))
                if normalized["servers"] == row["servers"]:
                    continue
            jm.record_file_result(
                target_id,
                path,
                row["outcome"],
                row.get("reason", ""),
                "Resume",
                servers=_publisher_rows(row),
                server_messages=True,
                uncapped=bool(previous) or bool(head_id),
            )


def _recount_chain(
    jm,
    head_id: str,
    cfg: dict | None = None,
    attempt_id: str | None = None,
    *,
    completion_ledger: CompletionLedger | None = None,
) -> dict[str, int]:
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
            completion_ledger.current_results()
            if completion_ledger is not None
            else jm.get_file_results(attempt_id, dedup_by_path=True),
            cast(dict, cfg).get("retry_sender_paths", {}),
        )
        jm.set_job_outcome(head_id, outcome)
        jm.set_publishers(head_id, publishers)
        return outcome
    rows = jm.get_file_results(head_id, dedup_by_path=True)
    # Capped Files panels do not contain the entire job; never report their subset as the whole job.
    if any(not row.get("file") for row in rows):
        head = jm.get_job(head_id)
        return dict(head.progress.outcome or {})
    if completion_ledger is not None:
        latest = {row["file"]: row for row in rows}
        sender_paths = (cfg or {}).get("retry_sender_paths", {})
        for row in completion_ledger.current_results():
            path = sender_paths.get(row["file"], row["file"])
            latest[path] = {**row, "file": path}
        rows = list(latest.values())
    outcome = dict(Counter(row["outcome"] for row in rows))
    jm.set_job_outcome(head_id, outcome)
    if completion_ledger is not None:
        aggregate: dict[str, dict] = {}
        for row in rows:
            fold_publisher_rows_into_aggregate(aggregate, _publisher_rows(row))
        jm.set_publishers(head_id, list(aggregate.values()))
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


def _finish(
    jm, job_id: str, outcome: dict[str, int], warnings: list[str], *, retry_queued: bool = False, unretried: int = 0
) -> None:
    """Settle one attempt, leaving a scheduled chain's lifecycle to the shared manager.

    Files still waiting for Plex or the disk with a retry queued for them are not a failure: the attempt completes with
    a warning. A file that truly failed, or one nothing will retry (``unretried``: unresolved files not queued, such as
    a missing file when missing files aren't retried), fails it.
    """
    successes, reason = _completion(outcome, warnings)
    unresolved = sum(outcome.get(key, 0) for key in (FAILED, FILE_NOT_FOUND, WAITING, NOT_IN_LIBRARY))
    only_waiting = retry_queued and not outcome.get(FAILED, 0) and not unretried
    failed = bool(unresolved) and not successes and not only_waiting
    jm.complete_job(job_id, error=reason if failed else None, warning=None if failed else reason)


def _finish_chain(
    jm,
    head_id: str,
    cfg: dict,
    warnings: list[str],
    *,
    attempt_id: str | None = None,
    completion_ledger: CompletionLedger | None = None,
) -> None:
    """Finish from all files, preserving earlier failures outside this retry attempt."""
    outcome = _recount_chain(jm, head_id, cfg, attempt_id, completion_ledger=completion_ledger)
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
