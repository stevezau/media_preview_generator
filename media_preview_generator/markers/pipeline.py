"""Intro & Credits per-file pipeline (spec §6.2).

owners → identity → kind → evidence in the user's source order (a normal run stops asking once every enabled type is
decided by more than chapters alone, but still reads a server never asked for the file; a forced re-detect asks every
source) →
decide from everything stored → store →
publish to each owner. ``check_item`` runs on the dispatcher's checking threads (no worker slot); it returns
None only when a registered local detector that needs a worker has to run (a type it can decide is undecided and its
answer is due, or its stored answer is from another version), which sends the item to a GPU/CPU worker where
``process_item`` runs the same steps plus the detectors. A detector that needs no worker runs right there, unless
another detector that has to run at the same source needs one: detectors at one source go to the worker together.
"""

from __future__ import annotations

import itertools
import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from ..config.paths import is_path_excluded
from ..job_kinds import ItemOutcome, KindHandlers
from ..jobs.worker import JOB_LOG_SKIP
from ..processing.multi_server import source_replaced_reason
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import OwnershipMatch
from ..servers.registry import server_config_from_dict
from ..web.settings_manager import get_settings_manager
from .audio.fingerprint import ChromaprintState, chromaprint_state
from .audio.season import frame_rate_of, season_audio_spec, season_intro_chapter_limits
from .carry_over import ReadNow, carry_over, is_carried_over, previous_decisions
from .credits.detector import CPU_RECHECK_PHASE, CUT_SHORT, credits_text_spec
from .credits.frames import gpu_failure_lines
from .credits.textdet_helper import TextDetState, text_detection_state
from .decide import (
    APP_PUBLISH_WHEN,
    DECIDE_RULES,
    DECIDE_RULES_VERSION,
    DecisionContext,
    DecisionStatus,
    FileLimits,
    TypeDecision,
    credits_limits_ms,
    decide,
    file_clock_may_matter,
    keep_published,
    kept_before_rule_change,
    unusable_server_marker,
)
from .external_ids import ids_from_path, ids_from_server_dict, is_extra, is_season_folder, merge_ids
from .job_log import (
    ALREADY_DECIDED,
    EPISODE_ONLY_SOURCES,
    SEASON_RECHECK_LABEL,
    RunNotes,
    SeasonEpisode,
    ServerResult,
    chapter_names,
    clock,
    decide_again_line,
    decided_line,
    display_name,
    done_line,
    file_start_line,
    file_title,
    gpu_failure_line,
    kept_types,
    last_seasons_audio_line,
    make_source_view,
    nothing_was_sent,
    online_recheck_line,
    path_year,
    read_result_line,
    reading_line,
    review_note,
    season_line,
    season_of,
    server_result_line,
    server_source_line,
    show_name,
    source_line,
    titled,
    totals_line,
    version_tag,
    worker_completed_line,
    worker_device,
    write_line,
)
from .locks import FILE_RUN_LOCKS
from .locks import KeyedLocks as _KeyedLocks
from .missing import gone_now, mark_if_missing
from .models import (
    SERVER_SOURCES,
    STALE_SERVER_MARKERS_DETAIL,
    Candidate,
    FileIdentity,
    Marker,
    MarkerType,
    MediaIds,
    Source,
)
from .outcomes import (
    EXTRAS_NOT_CHECKED,
    FILE_BUSY,
    NOT_IN_LIBRARY,
    OUTCOME_KEYS,
    PLEX_DB_BUSY,
    PLEX_PASS_UNKNOWN,
    READ_BACK_FAILED,
    REPLACED_OWN,
    RETRY_REASON_CODES,
    STATE_BY_STATUS,
    UNCHECKED_FILES,
    VERIFY_LATER,
    VERSIONS_UNCHECKED,
    VERSIONS_WAITING,
    FileOutcome,
    ServerStatus,
    file_outcome,
    is_kept_own,
    kept_note,
    kept_own_reason,
    replaced_own_note,
    replaced_stale_note,
    review_message,
    with_kept_note,
    with_sentence,
)
from .ownership import marker_matches, owning_servers
from .probe import ProbeError, ffprobe_path_for, probe_media
from .publishers.base import (
    NEXT_RUN,
    RETRY_SOON,
    Capability,
    CapabilityReport,
    DatabaseBusyError,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
    Shown,
    cancellable_waits,
    same_times,
    wait_cancelled,
)
from .publishers.factory import publisher_for, supported_types_for
from .publishers.plex_db import STALE_READ_WAIT_S, WAIT_CANCELLED, WAIT_SLICE_S, WORKER_BUSY_TIMEOUT_S
from .settings import GlobalMarkersSettings, ServerMarkersSettings, get_global_settings, load_server
from .source_counts import DecidedByTally, decided_groups
from .sources import introdb, skipdb, theintrodb
from .sources.chapters import CHAPTER_RULES_VERSION, chapter_candidates
from .sources.introdb import IntroDbClient
from .sources.online import LookupResult, is_budget_exhausted
from .sources.ratelimit import PRIORITY_LOW, RESET_TIME_LABEL, capped_waits
from .sources.server_markers import (
    PLEX_CHECKED_SINCE,
    READER_VERSION,
    imported_detail,
    importer_plugin,
    read_server_markers,
)
from .sources.skipdb import SkipDbClient
from .sources.theintrodb import TheIntroDbClient, is_key_refusal
from .store import EvidenceRow, FileRecord, ItemPublishStateRow, MarkerStore, PreviousDecision, get_marker_store
from .titles import NO_TITLE, TITLE_CACHE, title_of
from .titles import look_up as look_up_title

NO_DATA_RETRY = timedelta(days=14)
# TheIntroDB's daily budget is small (1,000 lookups with a key) and whole shows are missing from it (talk shows, some
# anime): once this many episodes of a series got "no entry" and none an answer (nor any file of the show a stored
# one), the series isn't asked about for SERIES_NO_ENTRY_PAUSE from then; the next pause needs as many new "no entry"
# answers after it ends. A forced re-detect always asks (``MarkerStore.record_series_lookup``).
SERIES_NO_ENTRY_MISSES = 3
SERIES_NO_ENTRY_PAUSE = timedelta(days=7)
_SERIES_PAUSED_SOURCES = frozenset({Source.THEINTRODB})
# Types still worth another source's answer once a file's run ends.
_UNDECIDED = frozenset({DecisionStatus.NEEDS_REVIEW, DecisionStatus.NO_EVIDENCE})
# Servers detect their own markers on a schedule (Plex overnight), so "none there" is asked again a day later.
EMPTY_SERVER_MARKERS_RETRY = timedelta(days=1)
# Check servers asks a server again for a decided file it had no markers for once the answer is this old, a step further
# after each re-read that stays empty, and never after the last step (a server without detection of its own is asked 5
# times in a month).
RECHECK_AFTER = tuple(timedelta(days=days) for days in (1, 2, 4, 8, 16))
# A file replaced while it is analysed is detected again from scratch; one that keeps changing is being written.
MAX_ATTEMPTS = 3
_ONLINE_LABELS = {Source.THEINTRODB: "TheIntroDB", Source.INTRODB: "IntroDB", Source.SKIPDB: "SkipDB"}
ONLINE_SOURCES = tuple(_ONLINE_LABELS)
PARSER_VERSIONS = {
    Source.THEINTRODB: theintrodb.PARSER_VERSION,
    Source.INTRODB: introdb.PARSER_VERSION,
    Source.SKIPDB: skipdb.PARSER_VERSION,
}
_STORED_LOOKUPS = ("ok", "no_data")
_CHAPTERS_AND_SERVERS = frozenset({Source.CHAPTERS.value, *(s.value for s in SERVER_SOURCES)})
# An intro season audio decided alone (with the previous season's hint, or beside agreeing server markers): an online
# answer may still disagree and send it to review, so the sources keep being asked on their schedule (owner, 2026-09-24).
_SEASON_AUDIO_AND_SERVERS = frozenset(
    {Source.SEASON_AUDIO.value, Source.SEASON_AUDIO_PREVIOUS.value, *(s.value for s in SERVER_SOURCES)}
)
# Plex and Emby can't tell our markers from their own, and Plex shows one marker set per item across its versions, so
# their markers are never read back from an item we published to. Jellyfin's reader leaves ours out itself.
_ITEM_WIDE_MARKERS = frozenset({ServerType.PLEX, ServerType.EMBY})
# Servers where a plugin can import a crowd skip database into the server's own markers; Plex detects its own.
_IMPORTER_PLUGIN_SERVERS = frozenset({ServerType.JELLYFIN, ServerType.EMBY})
PLUGINS_UNKNOWN_DETAIL = "Couldn't read this server's plugins, so its markers aren't used"
# A Plex answer stored while Plex couldn't tell which of its markers were made for an earlier file (an agent older
# than that answer, a busy database): counted as before, and asked again like an unusable answer until Plex can tell.
STALENESS_UNKNOWN_DETAIL = "Plex couldn't tell yet whether these markers were made for this file; asked again"
UNUSABLE_SERVER_MARKERS_DETAIL = (
    "Couldn't read this server's markers, they may describe another cut, or its library hides a type in Plex"
)
# What a reader older than ``PLEX_CHECKED_SINCE`` stored from a Plex server that shows our markers now: it can't be read
# again (Plex can't tell ours from its own), so it is never checked for markers made for an earlier file, and counts for
# nothing.
OURS_SHOWN_DETAIL = "This server shows our markers now; what an older version read from it isn't used"
# The same for a Plex item this file left nothing of ours on that may show ours all the same: another version's, or a
# type kept as Plex's own that can hold a marker of ours (``MarkerStore.published_to_item``). The reader skips it too.
OURS_ON_ITEM_DETAIL = "This server's item may show our markers now; what an older version read from it isn't used"
_CANCELLED = "cancelled by user"
# A ready Plex whose Plex Pass didn't answer (usually restarting): files within this long share the answer instead of
# each running the whole check (lock probe, Plex's HTTP connect with its retries, schema and library scans).
PLEX_PASS_UNKNOWN_TTL_S = 5.0
# Answers read from the saved settings alone: cheap, and wrong the moment the user saves, so never reused.
_SETTINGS_ANSWERS = frozenset({Capability.DISABLED, Capability.NEEDS_CONFIRMATION})
_SEQUENCE = itertools.count(1)
_SEQUENCE_LOCK = threading.Lock()


def sequence_number() -> int:
    """A number larger than every one handed out before in this process: which of two events came first (a file's run
    starting, another job asking for that file to be run again).

    Returns:
        The next number.
    """
    with _SEQUENCE_LOCK:
        return next(_SEQUENCE)


# The Inspector editor's publish (``publish_now``) runs inside a web request on one of eight gunicorn threads, so it is
# bounded (ruling P-R1). Each server's own HTTP calls are capped by ``ServerConfig.timeout``, which the route shortens
# to this.
PUBLISH_NOW_SERVER_TIMEOUT_S = 8
# Plex waits on locks, not on HTTP, so its own bound is separate: ``publishers/plex_db.BUSY_TIMEOUT_S`` is a job's
# 120 s wait, which alone outlasts the gate below. The publish-now path gives the database locks the same bound the
# servers' HTTP calls get, so one busy Plex can't hold a web thread for minutes.
PUBLISH_NOW_DB_WAIT_S = float(PUBLISH_NOW_SERVER_TIMEOUT_S)
# A START GATE, not a cancellation: a server the fan-out hasn't STARTED by then isn't started at all. It stops one
# unreachable server from multiplying into one wait per server; it cannot cut a server's call short once it began.
PUBLISH_NOW_DEADLINE_S = 25.0
# How long it waits for a job that is running the same file. A run can take minutes, so it gives up almost at once.
PUBLISH_NOW_LOCK_WAIT_S = 2.0
# The longest a worker-stage online lookup waits for its source's next request slot (``ratelimit.capped_waits``): the
# worker holds a GPU or CPU worker previews need, so a slot taken by another request's spacing (0.5 s at most) is waited
# for, and a longer wait (a 429 block, a queue of requests) is refused: nothing is stored, so the next job that runs the
# file asks again (the weekly online re-check only lists files a source answered "no entry" for). The checking stage,
# which holds no worker, keeps the limiter's own wait.
WORKER_LOOKUP_WAIT_S = 1.0
PUBLISH_DEADLINE_MESSAGE = "Couldn't publish to this server in time; the next Intro & Credits run publishes it"
# Not "that run publishes it": a job that already read the markers publishes the pre-save answer, and the run after
# it puts the user's marker there (the file's publish basis no longer matches, so it is written again).
PUBLISH_BUSY_MESSAGE = "Intro & Credits is running for this file; the next run publishes your marker"
SERVER_MARKERS_OFF = "Intro & Credits is off for this server"

LocalDetector = Callable[..., "list[Candidate] | DetectorAnswer"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _no_phase(_text: str) -> None:
    return None


class DetectorUnavailableError(Exception):
    """A local detector couldn't answer this time (its tool failed, the job was cancelled). No answer is stored for it,
    so the next run asks it again, unless the detector itself gives up at once there (credit text on a file that timed
    out lately or was found cut short)."""


@dataclass(frozen=True)
class DetectorAnswer:
    """A local detector's answer with what it was based on (season audio: the season's files and fingerprints).

    The pipeline stores both in one transaction, so an answer can't be recorded as current without being stored.
    """

    candidates: tuple[Candidate, ...]
    signature: str


@dataclass(frozen=True)
class LocalDetectorSpec:
    """A detector that reads the file itself (phase 2: season audio; phase 3: credit text).

    Attributes:
        source: The source whose place in the user's order the detector runs at.
        types: Marker types it can decide.
        detect: ``detect(file, *, ctx, gpu, gpu_device_path, phase_callback, cancel_check, pause_check, ffmpeg_threads,
            fallback_callback, gpu_worker)``: a list of candidates, or a ``DetectorAnswer`` whose signature is stored as
            the answer's basis (``detector_runs``); raises ``DetectorUnavailableError`` when it can't answer this time.
            On a worker, ``pause_check`` is ``PipelineContext.freeze_check``, ``ffmpeg_threads`` the GPU worker's own
            (None on a CPU worker), ``fallback_callback`` shows a CPU fallback inside the detector on the worker's row,
            and ``gpu_worker`` says whether a GPU worker runs it (its CPU rerun after a failed GPU decode included,
            where ``gpu`` is None); on the checking thread the first three are None and ``gpu_worker`` False.
        stores: Sources its candidates are stored under, each candidate under its own ``source``; empty = ``source``.
        version: Stored with its answer; an answer from another version is asked again, even for decided types.
        version_of: ``version_of(file, ctx)``: the version for this file when it depends on the file or the settings
            (credit text: the window the user chose for the file's kind); None: ``version``.
        version_step: What part of a stored version is the detector's own: it is compared modulo this, 0 compares it
            whole (credit text stores the user's window above it, as ``versions.AnswerVersion.version_step``).
        due: ``due(file, ctx)``: whether a stored answer of this version is out of date anyway (None: never).
        needs_worker: ``needs_worker(file, ctx)``: whether it needs a GPU/CPU worker now (None: always). One that
            doesn't runs on the checking thread, unless another detector that has to run at the same source needs a
            worker: then they all run on the worker.
        followups: ``followups(file, ctx)``: other files whose answer is out of date and whose decision could change
            with it (season audio: siblings matched before this episode arrived). Every run of a file of a type the
            detector decides asks the job to run them again, before any worker handoff (None: none).
        failed_here: ``failed_here(file, ctx)``: whether it failed to read the file as it is now (credit text: a decode
            error or a timeout recorded for this identity), so a rule waiting for its answer stops waiting (None:
            never).
        checks_chapters: It reads the file even for a type chapters decided alone, so the decision rules can check the
            chapter against the file itself (credit text: a release's "Credits" chapter is often seconds to minutes
            off the first card, spec §5.5 rule 3).
        compared: ``compared(file, ctx)``: whether its answer at this version had something to compare the file with,
            so finding nothing there checked a marker (season audio: another episode of the season has a fingerprint);
            None: always. A marker published before a rule change is taken off only by a detector that did
            (``_keep_published_before_rule_change``).
    """

    source: Source
    types: frozenset[MarkerType]
    detect: LocalDetector
    stores: frozenset[Source] = frozenset()
    version: int = 1
    version_of: Callable[[FileRecord, PipelineContext], int] | None = None
    version_step: int = 0
    due: Callable[[FileRecord, PipelineContext], bool] | None = None
    needs_worker: Callable[[FileRecord, PipelineContext], bool] | None = None
    followups: Callable[[FileRecord, PipelineContext], Iterable[str]] | None = None
    failed_here: Callable[[FileRecord, PipelineContext], bool] | None = None
    checks_chapters: bool = False
    compared: Callable[[FileRecord, PipelineContext], bool] | None = None

    def answer_version(self, rec: FileRecord, ctx: PipelineContext) -> int:
        """The version a stored answer for ``rec`` must have to count."""
        return self.version if self.version_of is None else self.version_of(rec, ctx)

    @property
    def stored_sources(self) -> frozenset[Source]:
        """The sources this detector's answers are stored under."""
        return self.stores or frozenset({self.source})


def live_server_config(server_id: str) -> ServerConfig | None:
    """A server's config as saved right now.

    Args:
        server_id: The server's id.

    Returns:
        Its config, or None when no saved server has that id.
    """
    for raw in get_settings_manager().get("media_servers") or []:
        if isinstance(raw, dict) and str(raw.get("id") or "") == server_id:
            return server_config_from_dict(raw)
    return None


@dataclass
class PipelineContext:
    """Everything one Intro & Credits job needs to process items.

    Attributes:
        registry: The job's ``ServerRegistry``.
        config: The job's ``Config``.
        settings: Global detection settings, read once per job.
        store: The markers store.
        priority: Returns the job's current priority (users can change it while the job runs), passed to the
            online sources' limiters.
        ffprobe: ffprobe binary.
        force: Re-detect: read chapters, every online source and the markers on servers we never published to again,
            and run every local detector, without stopping early. Publishing still skips servers that already show
            the result.
        clients: Online client per source id (``build_clients``).
        local_detectors: Detectors that read the file itself (on a worker unless their ``needs_worker`` says not).
        now: Current UTC time (tests use a fake clock).
        capability_ttl_s: How long a server's capability answer is reused.
        live_config: A server's saved config right now (None once it was removed). The registry is a snapshot from
            when the job started; consent is read from here before every write.
        recheck_empty_server_markers: Check servers: a server whose stored answer for a file is empty (or unusable) is
            read again on its backoff even when everything is decided, since its own detection may have run since
            (spec §5.5 rule 7 shortening). With a type still undecided, an unusable answer is read again as on any run,
            and an empty one a day old waits for the backoff too; a re-read that fails still counts.
        chromaprint: What the job's check for an ffmpeg with chromaprint found. Without a registered season audio
            detector, any state but UNKNOWN keeps stored season audio answers from helping decide (``_decide``);
            UNKNOWN (ffmpeg didn't answer) still registers no detector, but stored answers count as if it were there.
        credits_text: What the job's check for credit text detection found. ABSENT keeps stored credits text answers
            from helping decide (they may still hold a type in review); UNKNOWN (the check didn't answer) registers no
            detector, but stored answers count as if it were there.
        decided_by: Files per marker type and source group this job decided; a file counts once its run reaches
            publishing and doesn't fail (the job summary's "Decided by" counts).
        db_timeout_s: The longest Plex's publisher waits for the database locks in one check or write; None leaves it
            at ``plex_db.BUSY_TIMEOUT_S``. ``publish_now`` shortens it, since that wait alone outlasts the deadline a
            web request may take (ruling P-R1).
        season_recheck: A Season job: a file whose decisions didn't change logs no lines of its own; the job ends with
            one line per season instead (``summary_lines``).
        recheck_label: How those per-season lines name the job (a TheIntroDB recheck logs them too).
        decide_again: The one-off job after settings v16 and v17 that decides the files in Needs review (and those
            waiting for their item's other versions, and those whose intro rests on season audio) again: like a Season
            job, a file whose decisions didn't change logs no lines
            of its own, and the job ends with one line for them (``summary_lines``). It runs files as any job does.
        online_recheck: The weekly job that asks the online databases again about files they had no entry for
            (``online_recheck_files``): only a file an online database now has an entry for, or whose decisions
            changed, logs lines of its own, and the job ends with one line for them all (``summary_lines``). It runs
            files as any job does.
        busy_writes_retried: The job queues a retry for a file whose write gave up on a busy database, or that another
            job kept running past a worker's wait for it, so its row says this job tries again in a few minutes rather
            than on the next run (``job_runner``), for up to ``retry_file_cap`` files (``promise_busy_retry``).
        retry_file_cap: The most files one retry job takes (``job_runner.MAX_RETRY_FILES``).
        freeze_check: True while the job's running work must stop where it is, as previews' FFmpeg does: all
            processing paused (Pause all, quiet hours) or this job paused by its schedule's stop time
            (``job_runner``). A pause of this job by hand isn't one: it gives the job's slot back and lets the running
            file finish. Handed to the detectors on a worker; None: never.
        monotonic: The clock the job log's step times are read from (tests use a fake one).
    """

    registry: Any
    config: Any
    settings: GlobalMarkersSettings
    store: MarkerStore
    priority: Callable[[], int]
    ffprobe: str
    force: bool = False
    clients: dict[str, Any] = field(default_factory=dict)
    local_detectors: tuple[LocalDetectorSpec, ...] = ()
    now: Callable[[], datetime] = _utcnow
    capability_ttl_s: float = 300.0
    live_config: Callable[[str], ServerConfig | None] = live_server_config
    recheck_empty_server_markers: bool = False
    chromaprint: ChromaprintState = ChromaprintState.AVAILABLE
    credits_text: TextDetState = TextDetState.AVAILABLE
    db_timeout_s: float | None = None
    season_recheck: bool = False
    recheck_label: str = SEASON_RECHECK_LABEL
    decide_again: bool = False
    online_recheck: bool = False
    busy_writes_retried: bool = False
    retry_file_cap: int = 500
    freeze_check: Callable[[], bool] | None = None
    monotonic: Callable[[], float] = time.monotonic
    decided_by: DecidedByTally = field(default_factory=DecidedByTally, repr=False)
    _capabilities: dict[str, tuple[float, CapabilityReport]] = field(default_factory=dict, repr=False)
    _capability_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _capability_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Server ids whose Plex can never tell on this job which of its markers were made for an earlier file (an agent
    # older than that answer, ``MarkerPublisher.stale_types_unanswerable``): not asked again for the job's other files.
    # A set's add and membership test are atomic under the GIL, so the check threads share it without a lock.
    _stale_unanswerable: set[str] = field(default_factory=set, repr=False)
    # Per file of a forced run: the sources it already refreshed, so the worker stage doesn't ask those sources twice
    # and still refreshes the sources after the detector that handed the item to a worker. Dropped once the file has
    # its outcome, so a library-wide forced job doesn't keep an entry for every file it ran.
    _refreshed: dict[str, set[Source]] = field(default_factory=dict, repr=False)
    # Per server id: its importer plugins of a crowd database (IntroDB/TheIntroDB, SkipDB, AniSkip), joined ("" = none),
    # or None when its plugin list couldn't be read; the copy counts as that database's group.
    _importers: dict[str, str | None] = field(default_factory=dict, repr=False)
    _importer_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _importer_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _followups: set[str] = field(default_factory=set, repr=False)
    # Files of this job whose season audio answer left out a sibling that had changed on disk
    # (``note_changed_sibling_left_out``).
    _left_out_changed: set[str] = field(default_factory=set, repr=False)
    _followups_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Files this job checked without a source because its daily budget ran out, by source (job_runner turns this into
    # a completion warning once the job finishes). Counted when a file's run finishes, so a file asked again in the
    # same job (its worker stage after the checking thread, a retry after it changed on disk) is still one file.
    _budget_exhausted: dict[Source, int] = field(default_factory=dict, repr=False)
    # The same for a source that refused the API key: its first refusal (e.g. "TheIntroDB rejected the API key (HTTP
    # 401)") and how many files were checked without it.
    _key_refused: dict[Source, tuple[str, int]] = field(default_factory=dict, repr=False)
    # Per file whose run was handed on before it finished (to a worker, to the worker's CPU rerun, or to a retry after
    # it changed on disk): the sources its earlier stages were checked without for the whole job's reason, with the
    # answer. The stage that finishes the file counts them even when it doesn't ask that source again (a forced run
    # asks each source once), and drops one that answers when it is asked again.
    _pending_skips: dict[str, dict[Source, str]] = field(default_factory=dict, repr=False)
    # Sources whose running out this job already logged.
    _budget_warned: set[Source] = field(default_factory=set, repr=False)
    # Files checked without TheIntroDB because its daily budget ran out that ended with a type undecided, and when the
    # first of them was refused (``take_budget_rechecks``).
    _budget_rechecks: set[str] = field(default_factory=set, repr=False)
    _budget_refused_at: datetime | None = field(default=None, repr=False)
    _budget_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Per file being run: the thread running it and what that run computes once (``run_memo``).
    _run_memos: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _run_memos_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Per file handed on before it finished, like ``_pending_skips``: what its earlier stages did with each source, so
    # the stage that finishes it logs a source the checking thread asked as asked by this job.
    _run_notes: dict[str, RunNotes] = field(default_factory=dict, repr=False)
    # Per file of this job: its stored answers (``_answer_key``) when the job's first stage of it began, dropped with
    # the file's outcome like ``_run_notes``: a rule-only re-decide tells a new or changed answer from one this job only
    # stored again (``_keep_published_before_rule_change``).
    _answers_before: dict[str, frozenset[tuple]] = field(default_factory=dict, repr=False)
    # Per file of this job, set and dropped with ``_answers_before``: whether the file on disk was another than the one
    # stored when the job's first stage of it began. The checking stage stores the new file before it hands it to a
    # worker, so the worker's stage can't tell on its own that it publishes a replaced file (``VERIFY_LATER``).
    _replaced_at_start: dict[str, bool] = field(default_factory=dict, repr=False)
    # For the job's last lines: files written per server name, a Season job's unchanged episodes per season, per file a
    # decide-again job ran, whether its decisions changed and whether a type is still in review, and per file the weekly
    # online re-check ran, whether an online database now has an entry for it and whether its decisions changed.
    _sent: dict[str, int] = field(default_factory=dict, repr=False)
    _seasons: dict[str, list[SeasonEpisode]] = field(default_factory=dict, repr=False)
    _decided_again: list[tuple[bool, bool]] = field(default_factory=list, repr=False)
    _rechecked_online: list[tuple[bool, bool]] = field(default_factory=list, repr=False)
    _summary_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Per file: the ``sequence_number`` its latest run of this job started at (``ran_since``).
    _run_started: dict[str, int] = field(default_factory=dict, repr=False)
    # How many files this job has already named with each job-log title (``_note_title``): a second file with the
    # same title (two copies of one film) gets a short tell-apart, the first stays plain. A dict's ``get``/``[]=``
    # aren't atomic like a set's ``add``, so this one does take a lock.
    _titles_seen: dict[str, int] = field(default_factory=dict, repr=False)
    _titles_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Whether any run of this job stored an answer, decision or file identity that differs from before
    # (``answers_changed``).
    _answers_changed: bool = field(default=False, repr=False)
    # Files this job marked missing from disk (``take_missing``).
    _missing: int = field(default=0, repr=False)
    # Files whose busy row said this job tries again in a few minutes (``promise_busy_retry``).
    _busy_promised: set[str] = field(default_factory=set, repr=False)

    def run_memo(self, canonical_path: str) -> dict[str, Any]:
        """Values the current run of a file reads once and reuses (a detector's folder listing and due answer).

        Args:
            canonical_path: The file being run.

        Returns:
            The run's memo, dropped when the run ends and after a detector ran. Outside a run of that file on this
            thread, a new empty dict every call, so nothing is kept.
        """
        with self._run_memos_guard:
            owner = self._run_memos.get(canonical_path)
        return owner[1] if owner is not None and owner[0] == threading.get_ident() else {}

    def ran_since(self, canonical_path: str, number: int) -> bool:
        """Whether this job's latest run of a file started after ``number`` was handed out, so it read everything stored
        before then (a worker stage reads everything again, so it counts as a run).

        Args:
            canonical_path: The file.
            number: A ``sequence_number``.

        Returns:
            False when the file wasn't run since, or not at all.
        """
        with self._run_memos_guard:
            return self._run_started.get(canonical_path, 0) > number

    def note_answer_changed(self) -> None:
        """Remember that a run of this job stored something new about its file (``answers_changed``)."""
        self._answers_changed = True

    def answers_changed(self) -> bool:
        """Whether a run of this job changed what is stored about its file: its identity or chapters, a local
        detector's answer, or its decisions. A job that changed none of them can't have left a sibling's answer out of
        date.

        Returns:
            True once any run did.
        """
        return self._answers_changed

    @contextmanager
    def _running(self, canonical_path: str) -> Iterator[None]:
        with self._run_memos_guard:
            self._run_memos[canonical_path] = (threading.get_ident(), {})
            self._run_started[canonical_path] = sequence_number()
        try:
            yield
        finally:
            with self._run_memos_guard:
                self._run_memos.pop(canonical_path, None)

    def request_followups(self, paths: Iterable[str]) -> None:
        """Ask the job to run these files again after it finishes (their decision may change with this job's work).

        Args:
            paths: Local paths of the files.
        """
        with self._followups_guard:
            self._followups.update(paths)

    def take_followups(self) -> list[str]:
        """The requested files, sorted, and forget them.

        Returns:
            Every path requested since the last call, sorted.
        """
        with self._followups_guard:
            taken = sorted(self._followups)
            self._followups.clear()
        return taken

    def take_budget_rechecks(self) -> tuple[list[str], datetime | None]:
        """The files to check again once TheIntroDB's daily budget resets, and forget them.

        Returns:
            The local paths, sorted, of files checked without TheIntroDB because its budget ran out that ended with a
            type undecided; and when the first of them was refused (None when there are none).
        """
        with self._budget_lock:
            taken = sorted(self._budget_rechecks)
            refused_at = self._budget_refused_at if taken else None
            self._budget_rechecks.clear()
            self._budget_refused_at = None
        return taken, refused_at

    def note_changed_sibling_left_out(self, canonical_path: str) -> None:
        """Remember that this file's season audio answer left out a sibling that had changed on disk.

        Args:
            canonical_path: Local path of the file whose answer it was.
        """
        with self._followups_guard:
            self._left_out_changed.add(canonical_path)

    def promise_busy_retry(self, canonical_path: str) -> bool:
        """Whether a file a busy database refused may be told this job tries again in a few minutes.

        Only when the job queues retries (``busy_writes_retried``), and for at most ``retry_file_cap`` files: the retry
        job takes those first (``busy_promised``), so no row promises a retry that a file past the cap won't get.

        Args:
            canonical_path: The file.

        Returns:
            True when the file has the promise (again, for a file that already had it).
        """
        if not self.busy_writes_retried:
            return False
        with self._summary_lock:
            if canonical_path not in self._busy_promised and len(self._busy_promised) >= self.retry_file_cap:
                return False
            self._busy_promised.add(canonical_path)
            return True

    def may_promise_busy_retry(self, canonical_path: str) -> bool:
        """Whether :meth:`promise_busy_retry` would promise this file a retry, without taking a promise.

        Args:
            canonical_path: The file.

        Returns:
            True when the job queues retries and the file has a promise already or one is left.
        """
        if not self.busy_writes_retried:
            return False
        with self._summary_lock:
            return canonical_path in self._busy_promised or len(self._busy_promised) < self.retry_file_cap

    def release_busy_retry(self, canonical_path: str) -> None:
        """Give back a file's retry promise (``promise_busy_retry``) that no retry will use.

        Args:
            canonical_path: The file.
        """
        with self._summary_lock:
            self._busy_promised.discard(canonical_path)

    def busy_promised(self) -> set[str]:
        """The files promised a retry (``promise_busy_retry``)."""
        with self._summary_lock:
            return set(self._busy_promised)

    def note_missing(self) -> None:
        """Count one file this job marked missing from disk (``missing.mark_if_missing``)."""
        with self._summary_lock:
            self._missing += 1

    def take_missing(self) -> int:
        """How many files this job marked missing from disk since the last call, and start counting again.

        Returns:
            The count.
        """
        with self._summary_lock:
            taken, self._missing = self._missing, 0
        return taken

    def take_changed_siblings_left_out(self) -> list[str]:
        """The files noted by ``note_changed_sibling_left_out``, sorted, and forget them.

        Returns:
            Every path noted since the last call, sorted.
        """
        with self._followups_guard:
            taken = sorted(self._left_out_changed)
            self._left_out_changed.clear()
        return taken

    def _note_finished(
        self,
        rows: list[dict],
        season: tuple[str, SeasonEpisode] | None,
        decided_again: tuple[bool, bool] | None,
        rechecked_online: tuple[bool, bool] | None = None,
    ) -> None:
        """Count one file's rows for the job's totals line, a Season job's episode for its season's line, and a
        decide-again or weekly online re-check job's file for its line."""
        with self._summary_lock:
            for row in rows:
                name = str(row.get("server_name") or row.get("server_id") or "")
                written = row.get("status") == ServerStatus.WRITTEN.value
                self._sent[name] = self._sent.get(name, 0) + int(written)
            if season is not None:
                self._seasons.setdefault(season[0], []).append(season[1])
            if decided_again is not None:
                self._decided_again.append(decided_again)
            if rechecked_online is not None:
                self._rechecked_online.append(rechecked_online)

    def summary_lines(self, outcome: dict[str, int]) -> list[str]:
        """The lines a job ends its log with: a Season job's one line per season (a decide-again or weekly online
        re-check job's one line), then the totals.

        Args:
            outcome: The job's file counts per outcome (files finished before a restart included).

        Returns:
            The lines, e.g. ``Done: 3 files · 2 sent to Plex · 1 needs review · 0 nothing found``.
        """
        with self._summary_lock:
            sent = dict(self._sent)
            seasons = {season: list(episodes) for season, episodes in self._seasons.items()}
            decided_again = list(self._decided_again)
            rechecked_online = list(self._rechecked_online)
        with self._budget_lock:
            skipped: dict[str, int] = {}
            for source, count in self._budget_exhausted.items():
                skipped[_ONLINE_LABELS[source]] = skipped.get(_ONLINE_LABELS[source], 0) + count
            for source, (_detail, count) in self._key_refused.items():
                skipped[_ONLINE_LABELS[source]] = skipped.get(_ONLINE_LABELS[source], 0) + count
        lines = [season_line(season, episodes, self.recheck_label) for season, episodes in sorted(seasons.items())]
        if self.decide_again:
            lines.append(decide_again_line(decided_again))
        if self.online_recheck:
            lines.append(online_recheck_line(rechecked_online))
        return [*lines, totals_line(outcome, sent, skipped)]


@dataclass(frozen=True)
class _Owning:
    """An enabled server with at least one library holding the file (Intro & Credits may be off there)."""

    server: Any
    config: ServerConfig
    matches: tuple[OwnershipMatch, ...]


# A job's whole run of one file (``locks.FILE_RUN_LOCKS``).
_PATH_LOCKS = FILE_RUN_LOCKS
# How long a worker waits for a file's run lock between asking whether to stop waiting.
_PATH_LOCK_SLICE_S = 0.5
# The longest a worker waits for a file another job is running when its own job retries the file a few minutes later:
# the worker holds a GPU or CPU worker previews need, and the other run may take minutes (a season step) or be paused.
WORKER_FILE_WAIT_S = 60.0
# The longest it waits when no retry can be promised: long enough for any running file (a season step), but a worker
# must never wait out another job paused for hours (the file goes to the next run instead).
WORKER_FILE_WAIT_NO_RETRY_S = 900.0
FILE_BUSY_MESSAGE = "Another Intro & Credits job is running this file"

# The run lock holders' freeze checks (``PipelineContext.freeze_check`` of a worker's run), so a waiter can tell a
# holder stopped where it is (Pause all, its schedule's stop time) from one still running.
_FILE_RUN_HOLDERS: dict[str, Callable[[], bool]] = {}
_FILE_RUN_HOLDERS_GUARD = threading.Lock()


def _holder_frozen(path: str) -> bool:
    """Whether the run holding ``path``'s lock is frozen: it keeps the file until its job resumes."""
    with _FILE_RUN_HOLDERS_GUARD:
        frozen = _FILE_RUN_HOLDERS.get(path)
    try:
        return bool(frozen is not None and frozen())
    except Exception:  # noqa: BLE001 - another job's check failing must not end this job's run
        return False


@contextmanager
def _file_run_lock(
    path: str,
    *,
    wait_s: float | None,
    stop: Callable[[float], bool],
    frozen: Callable[[], bool] | None = None,
) -> Iterator[bool]:
    """Hold a file's run lock (``_PATH_LOCKS``) for the block; yields whether it is held.

    Another job's worker can hold it for minutes. The checking stage doesn't wait for it (``wait_s`` 0): the file goes
    to a worker, as a preview check hands on a file it can't finish, and the checking thread is free for the job's other
    files. A worker waits for it in slices, until ``wait_s`` runs out or ``stop`` says so.

    Args:
        path: The file.
        wait_s: The longest wait; 0 tries once, None waits until ``stop``.
        stop: Asked between slices with the seconds waited so far; True gives up.
        frozen: The holder's freeze check while it holds the lock (``_holder_frozen``); None: never frozen.

    Yields:
        True while holding the lock; False when it wasn't taken.
    """
    started = time.monotonic()
    deadline = None if wait_s is None else started + wait_s
    while True:
        left = (
            _PATH_LOCK_SLICE_S if deadline is None else max(0.0, min(_PATH_LOCK_SLICE_S, deadline - time.monotonic()))
        )
        with _PATH_LOCKS.try_hold(path, left) as held:
            if not held and deadline is not None and time.monotonic() >= deadline:
                # Out of time (the check stage tries once): nothing to ask ``stop``, which may take a retry promise.
                yield False
                return
            if held:
                if frozen is not None:
                    with _FILE_RUN_HOLDERS_GUARD:
                        _FILE_RUN_HOLDERS[path] = frozen
                try:
                    yield True
                finally:
                    if frozen is not None:
                        with _FILE_RUN_HOLDERS_GUARD:
                            _FILE_RUN_HOLDERS.pop(path, None)
                return
            if stop(time.monotonic() - started):
                yield False
                return


def _run_by_another_job(item: ProcessableItem, ctx: PipelineContext, *, retried: bool) -> ItemOutcome:
    """The outcome of a file a worker gave back because another job kept running it (``_run``): every server the job
    publishes it to waits (``FILE_BUSY``), for the job's retry when it promised one, else for the next run. Nothing
    about the file was read or stored.

    Args:
        item: The file.
        ctx: The job's context.
        retried: Whether the job promised the file a retry (``PipelineContext.promise_busy_retry``).

    Returns:
        The file's outcome.
    """
    path = item.canonical_path
    owners = _publishing_owners(item, ctx)
    if isinstance(owners, ItemOutcome):
        return owners
    message = f"{FILE_BUSY_MESSAGE}; {RETRY_SOON if retried else NEXT_RUN}"
    logger.info("{}: {}", os.path.basename(path), message)
    rows = [_row(owner.config, "", ServerStatus.WAITING, message, path, reason_code=FILE_BUSY) for owner in owners]
    return ItemOutcome(FileOutcome.WAITING.value, message, rows)


def _forget_run(ctx: PipelineContext, path: str) -> None:
    """Drop what the job kept about a file's run between its stages, once the file has its outcome."""
    ctx._refreshed.pop(path, None)
    ctx._pending_skips.pop(path, None)
    ctx._run_notes.pop(path, None)
    ctx._answers_before.pop(path, None)
    ctx._replaced_at_start.pop(path, None)


# Versions of one Plex item run on different threads under different path locks. Each publish reads what is ours on
# the item, writes, and records the result; another version's publish in between would leave that record wrong and
# our markers on the item untracked.
_ITEM_LOCKS = _KeyedLocks()


class _FileChangedError(Exception):
    """The file on disk no longer has the identity that was analysed."""


class _ItemServers:
    """The owning servers of one file, asked for item ids, external ids and markers at most once each."""

    def __init__(
        self, item: ProcessableItem, owning: list[_Owning], cancel_check: Callable[[], bool] | None = None
    ) -> None:
        self._path = item.canonical_path
        # The job's cancel, for the database reads made on the file's behalf before detection.
        self.cancel_check = cancel_check
        self._hints = item.item_id_by_server or {}
        self.owning = owning
        self._item_ids: dict[str, str | None] = {}
        self._asked_ids = False
        self._server_ids: MediaIds | None = None
        self._title: tuple[str | None, object] = NO_TITLE
        self._markers: dict[str, list[Candidate] | None] = {}
        self._parts: dict[str, dict[str, list[int | None] | None]] = {}
        self._stale: dict[str, frozenset[MarkerType] | None] = {}

    def item_id(self, owner: _Owning) -> str | None:
        """The server's item id for this file (hint first), or None when the server doesn't have it (yet).

        Looked up in the libraries that hold the file: Plex otherwise searches only libraries with previews on.
        """
        sid = owner.config.id
        if sid not in self._item_ids:
            hint = self._hints.get(sid)
            if hint:
                self._item_ids[sid] = str(hint)
            else:
                try:
                    self._item_ids[sid] = owner.server.resolve_remote_path_to_item_id(
                        self._path, library_ids=[m.library_id for m in owner.matches]
                    )
                except Exception as exc:
                    logger.debug("Item id lookup on {} failed for {}: {}", owner.config.name, self._path, exc)
                    self._item_ids[sid] = None
        return self._item_ids[sid]

    def server_ids(self) -> MediaIds | None:
        """External ids from the first owning server that answers, or None when none could."""
        if not self._asked_ids:
            self._asked_ids = True
            for owner in self.owning:
                item_id = self.item_id(owner)
                if not item_id:
                    continue
                try:
                    raw = owner.server.get_external_ids(item_id)
                except Exception as exc:
                    logger.debug("External ids from {} failed for {}: {}", owner.config.name, self._path, exc)
                    continue
                if isinstance(raw, dict):
                    self._server_ids = ids_from_server_dict(raw)
                    self._title = title_of(raw)
                    break
        return self._server_ids

    def answered_title(self) -> tuple[str | None, object] | None:
        """The title and year in the external ids answer this run already has, without asking.

        Returns:
            ``(title, year)``; ``titles.NO_TITLE`` when the run asked and no server gave a title; None when the run
            didn't ask.
        """
        return self._title if self._asked_ids else None

    def title_asks(self, owners: Iterable[_Owning] | None = None) -> list[Callable[[], object]]:
        """Per publishing owner (``owners``; every owning server when not given) whose item id for the file this run
        already knows (a hint, or looked up for another step), a call for its external ids answer; a server whose
        id isn't known yet isn't asked for it.

        The id is never searched for here: ``resolve_remote_path_to_item_id`` writes ``self._item_ids`` with no
        lock, so calling it from the title lookup while the file's own thread might be doing the same for the same
        server races -- whichever finishes last wins, even if it found nothing, which once cost a file its real
        item id and turned it FAILED/WAITING for reasons that had nothing to do with its markers. Naming the file
        is not worth that: without an id already at hand, it's simply named by its file name.
        """
        asks = []
        for owner in owners if owners is not None else self.owning:
            item_id = self._item_ids.get(owner.config.id) or self._hints.get(owner.config.id)
            if item_id:
                asks.append(lambda server=owner.server, item_id=str(item_id): server.get_external_ids(item_id))
        return asks

    def markers(self, owner: _Owning, item_id: str, duration_ms: int | None) -> list[Candidate] | None:
        """The server's markers for this file (``read_server_markers``), read at most once per run.

        The check for types every server keeps its own markers of runs before the evidence read, so the two share one
        answer. None: they couldn't be read, or may describe another cut.
        """
        sid = owner.config.id
        if sid not in self._markers:
            server = _PartsKept(owner.server, self._parts.setdefault(sid, {}))
            try:
                self._markers[sid] = read_server_markers(
                    server, owner.config, item_id, duration_ms=duration_ms, canonical_path=self._path
                )
            except Exception as exc:
                logger.debug("Reading markers on {} failed for {}: {}", owner.config.name, self._path, exc)
                self._markers[sid] = None
        return self._markers[sid]

    def stale_types(
        self, owner: _Owning, read: Callable[[], frozenset[MarkerType] | None]
    ) -> frozenset[MarkerType] | None:
        """The types whose own markers the server shows for this file were made for an earlier file at its path, read
        at most once per run (``read``); None when the server can't tell."""
        sid = owner.config.id
        if sid not in self._stale:
            self._stale[sid] = read()
        return self._stale[sid]

    def part_count(self, owner: _Owning, item_id: str) -> int | None:
        """How many parts the server's item has across its versions (Plex), as the markers read asked for them; None
        when that read didn't ask (no markers there) or got no answer."""
        durations = self._parts.get(owner.config.id, {}).get(item_id)
        return len(durations) if durations else None


class _PartsKept:
    """A server client whose ``get_part_durations`` answers are kept, so the markers read's request is asked once."""

    def __init__(self, server: Any, kept: dict[str, list[int | None] | None]) -> None:
        self._server = server
        self._kept = kept

    def __getattr__(self, name: str) -> Any:
        return getattr(self._server, name)

    def get_part_durations(self, item_id: str) -> list[int | None] | None:
        if item_id not in self._kept:
            self._kept[item_id] = self._server.get_part_durations(item_id)
        return self._kept[item_id]


def build_clients(settings: GlobalMarkersSettings) -> dict[str, Any]:
    """Online clients for the enabled online sources.

    Args:
        settings: Global detection settings.

    Returns:
        Client per enabled online source id.
    """
    clients: dict[str, Any] = {}
    if settings.source_enabled("theintrodb"):
        clients["theintrodb"] = TheIntroDbClient(settings.source("theintrodb").api_key)
    if settings.source_enabled("introdb"):
        clients["introdb"] = IntroDbClient()
    if settings.source_enabled("skipdb"):
        clients["skipdb"] = SkipDbClient()
    return clients


def default_local_detectors(
    settings: GlobalMarkersSettings,
    config: Any,
    chromaprint: ChromaprintState = ChromaprintState.ABSENT,
    credits_text: TextDetState = TextDetState.ABSENT,
) -> tuple[LocalDetectorSpec, ...]:
    """The local detectors a job uses: season audio when its source is on and an ffmpeg with chromaprint exists, and
    credit text when its source and credits detection are on and text detection can run here.

    Args:
        settings: Global detection settings.
        config: The job's ``Config`` (its ``ffmpeg_path``).
        chromaprint: The job's :func:`chromaprint_state`, for the warning when no detector can be registered.
        credits_text: The job's :func:`text_detection_state`, for the same reason.

    Returns:
        The detector specs, in no particular order (the pipeline runs them at their source's place).
    """
    detectors: list[LocalDetectorSpec] = []
    if settings.source_enabled(Source.SEASON_AUDIO.value):
        spec = season_audio_spec(getattr(config, "ffmpeg_path", None))
        if spec is not None:
            detectors.append(spec)
        elif chromaprint is ChromaprintState.UNKNOWN:
            logger.warning(
                "Season audio matching is on, but ffmpeg didn't answer the check for chromaprint; no episode is matched "
                "by this job, saved season audio answers still count, and ffmpeg is checked again in 10 minutes"
            )
        else:
            logger.warning(
                "Season audio matching is on, but no ffmpeg with chromaprint was found; TV intros come from the other "
                "sources only"
            )
    if settings.detect_credits and settings.source_enabled(Source.CREDITS_TEXT.value):
        if credits_text is TextDetState.AVAILABLE:
            detectors.append(credits_text_spec())
        elif credits_text is TextDetState.UNKNOWN:
            logger.warning(
                "On-screen credit text is on, but the text detection check didn't answer; this job reads no credit "
                "text, saved credit text answers still count, and the check runs again in 10 minutes"
            )
        else:
            logger.warning(
                "On-screen credit text is on, but text detection isn't available here (see Settings → Intro & "
                "Credits for the reason); credits come from the other sources only"
            )
    return tuple(detectors)


def run_detector_checks(
    ffmpeg_path: str | None, settings: GlobalMarkersSettings | None = None
) -> tuple[ChromaprintState, TextDetState]:
    """The checks that decide which local detectors a job registers, for the sources turned on now.

    Whether an ffmpeg with chromaprint exists (only while season audio is on) and whether credit text detection can run
    here (only while credits and credit text are on). Each answer is kept for the process (one that didn't come is asked
    again 10 minutes later), so only the first job pays for them; the job runner calls this before its job takes a gate
    slot, and :func:`build_context` again after it.

    Args:
        ffmpeg_path: The configured ffmpeg (None: the candidates ``fingerprint.chromaprint_state`` looks at anyway).
        settings: The detection settings (default: the saved ones).

    Returns:
        ``(chromaprint, credits_text)``; ABSENT for a source that is off.
    """
    settings = settings or get_global_settings()
    chromaprint = (
        chromaprint_state(ffmpeg_path)
        if settings.source_enabled(Source.SEASON_AUDIO.value)
        else ChromaprintState.ABSENT
    )
    credits_text = (
        text_detection_state()
        if settings.detect_credits and settings.source_enabled(Source.CREDITS_TEXT.value)
        else TextDetState.ABSENT
    )
    return chromaprint, credits_text


def build_context(
    *,
    registry: Any,
    config: Any,
    priority: int | Callable[[], int],
    force: bool = False,
    recheck_empty_server_markers: bool = False,
    season_recheck: bool = False,
    recheck_label: str = SEASON_RECHECK_LABEL,
    decide_again: bool = False,
    online_recheck: bool = False,
) -> PipelineContext:
    """Context from live settings (used by the job runner).

    Also runs the checks that decide which local detectors the job registers: whether an ffmpeg with chromaprint
    exists (only while season audio is on) and whether credit text detection can run here (only while credits and
    credit text are on). Each answer is kept for the process (one that didn't come is asked again 10 minutes later),
    so usually only the first job pays for it: ffmpeg listing its muxers, and a subprocess that loads the text
    detection model.

    Args:
        registry: The job's ``ServerRegistry``.
        config: The job's ``Config``.
        priority: The job's priority, or a callable returning its current value.
        force: Re-detect.
        recheck_empty_server_markers: Check servers (``PipelineContext.recheck_empty_server_markers``).
        season_recheck: A Season job or a TheIntroDB recheck (``PipelineContext.season_recheck``).
        recheck_label: Which of the two (``PipelineContext.recheck_label``).
        decide_again: The decide-again job after settings v16 and v17 (``PipelineContext.decide_again``).
        online_recheck: The weekly online re-check (``PipelineContext.online_recheck``).

    Returns:
        A context for one job.
    """
    settings = get_global_settings()
    ffmpeg_path = getattr(config, "ffmpeg_path", None)
    chromaprint, credits_text = run_detector_checks(ffmpeg_path, settings)
    return PipelineContext(
        registry=registry,
        config=config,
        settings=settings,
        store=get_marker_store(),
        priority=priority if callable(priority) else (lambda: priority),
        ffprobe=ffprobe_path_for(ffmpeg_path),
        force=force,
        clients=build_clients(settings),
        local_detectors=default_local_detectors(settings, config, chromaprint, credits_text),
        recheck_empty_server_markers=recheck_empty_server_markers,
        chromaprint=chromaprint,
        credits_text=credits_text,
        season_recheck=season_recheck,
        recheck_label=recheck_label,
        decide_again=decide_again,
        online_recheck=online_recheck,
    )


def markers_for_path(store: MarkerStore, canonical_path: str) -> dict[MarkerType, Marker] | None:
    """Decided markers for another local file (Plex multi-version agreement).

    Args:
        store: The markers store.
        canonical_path: Local path of the other file.

    Returns:
        Its markers by type (``{}`` = decided, nothing to show), or None when it was never decided or the file on disk
        is no longer the one that was decided (replaced or gone).
    """
    rec = store.get_file(canonical_path)
    if rec is None or not store.get_decisions(rec.id) or identity_changed(rec):
        return None
    return store.get_markers(rec.id)


def _owning_servers(item: ProcessableItem, ctx: PipelineContext) -> list[_Owning]:
    return [
        _Owning(server, cfg, tuple(matches))
        for cfg, server, matches in owning_servers(item.canonical_path, ctx.registry)
    ]


def _marker_owners(owning: list[_Owning], canonical_path: str, pin: str | None = None) -> list[_Owning]:
    keep = marker_matches(canonical_path, [owner.config for owner in owning])
    return [owner for owner in owning if owner.config.id in keep and pin in (None, owner.config.id)]


def _job_pin(ctx: PipelineContext) -> str | None:
    """The one server the job publishes to (``config.server_id_filter``, set from its pin by the job runner), as a
    preview job's ``resolve_per_item_pin`` reads it; None for every server with Intro & Credits on."""
    pin = getattr(ctx.config, "server_id_filter", None)
    return pin if isinstance(pin, str) and pin else None


def _publishing_owners(
    item: ProcessableItem, ctx: PipelineContext, owning: list[_Owning] | None = None
) -> list[_Owning] | ItemOutcome:
    """The servers the job publishes this file's markers to (its pin's only, for a pinned job), or the file's
    ``NO_OWNERS`` outcome when there are none.

    Args:
        item: The file.
        ctx: The job's context.
        owning: The file's owning servers when the caller has them (``_owning_servers``).

    Returns:
        The owners, or the outcome that says why there are none.
    """
    pin = _job_pin(ctx)
    if owning is None:
        owning = _owning_servers(item, ctx)
    owners = _marker_owners(owning, item.canonical_path, pin)
    if owners:
        return owners
    if pin:
        pinned = ctx.registry.get_config(pin)
        name = pinned.name if pinned is not None and pinned.name else pin
        return ItemOutcome(
            FileOutcome.NO_OWNERS.value,
            f"This job publishes to {name} only, and {name} doesn't have Intro & Credits turned on for this file",
        )
    return ItemOutcome(FileOutcome.NO_OWNERS.value, "No server with Intro & Credits turned on has this file")


def _resolve_kind(
    path_ids: MediaIds, servers: _ItemServers, known_kind: str | None
) -> tuple[MediaIds, bool, str | None]:
    """Ids to detect with, whether online sources may be asked with them, and a newly confirmed kind to remember.

    A path "movie" can be a show folder with only a tmdb/imdb id holding a file without SxxEyy, so the owning server's
    kind wins. The kind a server gave for this version of the file is reused without asking again (ids still come
    from the server when a lookup needs them). When no server can answer the path kind stays, but online sources are
    not asked: TMDB movie and TV ids share numbers, and an answer stored for the wrong title would be kept for good.
    A later run asks again.
    """
    if path_ids.is_episode:
        return path_ids, True, None
    if known_kind is not None:
        ids = _ids_of_known_kind(path_ids, known_kind)
        return ids, ids.kind in ("movie", "episode"), None
    answer = servers.server_ids()
    if answer is None:
        return path_ids, False, None
    ids = merge_ids(path_ids, answer) if path_ids.kind == "movie" and answer.kind == "movie" else answer
    return ids, ids.kind in ("movie", "episode"), answer.kind


def _ids_of_known_kind(path_ids: MediaIds, known_kind: str) -> MediaIds:
    if known_kind == "movie" and path_ids.kind == "movie":
        return path_ids
    return MediaIds(known_kind) if known_kind in ("movie", "episode") else MediaIds()


def _stored_ids(store: MarkerStore, rec: FileRecord) -> MediaIds:
    """The kind another file's own last run decided with, as far as the store knows it (without asking its server)."""
    path_ids = ids_from_path(rec.canonical_path)
    known_kind = None if path_ids.is_episode else store.get_server_kind(rec.id)
    return path_ids if known_kind is None else _ids_of_known_kind(path_ids, known_kind)


def _lookup_ids(ids: MediaIds, servers: _ItemServers) -> MediaIds:
    """Complete ids from the server when they lack the imdb id most sources need (same kind only)."""
    if ids.kind in ("movie", "episode") and not ids.imdb:
        answer = servers.server_ids()
        if answer is not None and answer.kind == ids.kind:
            return merge_ids(ids, answer)
    return ids


def _enabled_types(settings: GlobalMarkersSettings, ids: MediaIds) -> frozenset[MarkerType]:
    types = set()
    if settings.detect_credits:
        types.add(MarkerType.CREDITS)
    # Intros and recaps exist for TV episodes only: an "Opening" chapter in a movie is a scene.
    if ids.is_episode:
        if settings.detect_intro:
            types.add(MarkerType.INTRO)
        if settings.detect_recap:
            types.add(MarkerType.RECAP)
    return frozenset(types)


def _needs_lookup(ctx: PipelineContext, rec: FileRecord, source: Source, refresh: bool) -> bool:
    fetched = ctx.store.evidence_fetched_at(rec.id, source)
    if fetched is None or refresh or ctx.store.evidence_version(rec.id, source) != PARSER_VERSIONS[source]:
        return True
    rows = [r for r in ctx.store.evidence_rows(rec.id) if r.source is source and r.origin == ""]
    return all(r.type is None for r in rows) and ctx.now() - fetched > NO_DATA_RETRY


def online_recheck_files(store: MarkerStore, settings: GlobalMarkersSettings, now: datetime) -> Iterator[str]:
    """The files the weekly online re-check lists: an enabled online source's stored "no entry" is due again (older
    than ``NO_DATA_RETRY``, as ``_needs_lookup`` asks it again), and its answer could still change a decision.

    That is a file with a type undecided (Needs review or nothing found), or one season audio decided alone: an online
    answer confirms that intro or sends it to review (``_decided_beyond_chapters``). A file decided otherwise, by
    chapters alone included, isn't listed; nor is one gone from disk, which would otherwise be listed every week.

    Args:
        store: The markers store.
        settings: Global detection settings.
        now: The current time (UTC).

    Yields:
        The local paths, sorted, each checked as it is reached (so a caller can stop early); none when every online
        source is off.
    """
    sources = [source for source in ONLINE_SOURCES if settings.source_enabled(source.value)]
    if not sources:
        return
    for path in store.files_with_old_empty_lookups(sources, now - NO_DATA_RETRY):
        if _online_answer_could_decide(store, path) and os.path.isfile(path):
            yield path


def _online_answer_could_decide(store: MarkerStore, path: str) -> bool:
    """Whether a file has a type undecided (True when the store knows no decisions for it), one decided by season
    audio alone, or one carried over from a file it replaced (a locked marker never counts)."""
    rec = store.get_file(path)
    if rec is None:
        return False
    decisions = store.get_decisions(rec.id)
    if not decisions:
        return True
    if any(row.status in _UNDECIDED for row in decisions.values()):
        return True
    # Only a decided type keeps an unlocked marker (``MarkerStore.save_decisions``). A marker carried over from a
    # replaced file stands only until the file has evidence of its own.
    return any(
        not marker.locked and (set(marker.decided_by) <= _SEASON_AUDIO_AND_SERVERS or is_carried_over(marker))
        for marker in store.get_markers(rec.id).values()
    )


# Sources without a switch of their own, each ranked right after the source whose switch they ride on.
_RIDERS = {
    Source.SERVER_MARKERS.value: Source.SERVER_MARKERS_IMPORTED.value,
    Source.SEASON_AUDIO.value: Source.SEASON_AUDIO_PREVIOUS.value,
}
# Sources whose answers only a local detector makes (the previous-season hint included).
_LOCAL_DETECTOR_SOURCES = frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS, Source.CREDITS_TEXT})


def _frame_rate(
    ctx: PipelineContext, rec: FileRecord, evidence: Iterable[Candidate], order: tuple[str, ...]
) -> float | None:
    """The frame rate decisions read online times on the file's clock by (``decide`` rule 12): the stored one, read
    first for a file stored before frame rates were (``season.frame_rate_of``) only when, among the evidence of the
    sources in ``order`` (the ones turned on), an answer that may be timed on another release has a candidate of its
    type from another source to agree with (``decide.file_clock_may_matter``). A server marker made for an earlier file
    (``Candidate.stale``) isn't one: decide drops it before reading any clock. Season audio reads the rate it needs
    itself."""
    if file_clock_may_matter((c for c in evidence if c.source.value in order and not c.stale), rec.duration_ms or 0):
        return frame_rate_of(ctx, rec, probe=probe_media)
    return ctx.store.get_frame_rate(rec.id)[1]


def _decision_order(settings: GlobalMarkersSettings) -> tuple[str, ...]:
    """Enabled sources in the user's order; importer-plugin copies ride on the server-markers switch and the
    previous-season hint on the season-audio switch, each ranked right after its switch."""
    order: list[str] = []
    for source_id in settings.ordered_enabled_sources():
        order.append(source_id)
        if source_id in _RIDERS:
            order.append(_RIDERS[source_id])
    return tuple(order)


def _file_limits(rec: FileRecord) -> FileLimits:
    """The limits a server's own marker must fit for this file (``decide.unusable_server_marker``)."""
    return FileLimits(rec.duration_ms or 0)


def _decide(
    ctx: PipelineContext,
    rec: FileRecord,
    types: frozenset[MarkerType],
    intro_chapter_limit: int | None = None,
) -> dict[MarkerType, TypeDecision]:
    """Decide from everything stored for the enabled sources, so a forced and a normal run always agree.

    Stored answers of a local detector this job doesn't have can't be produced again by this job (season audio can't be
    re-matched as the season changes; credit text can't be re-read at all), so they may hold a type in review but never
    help decide it: a type the decision with them decides is decided again without them. When the chromaprint check
    didn't answer (``ctx.chromaprint`` UNKNOWN), stored season audio answers count as usual until it does, and the same
    for credit text while ``ctx.credits_text`` is UNKNOWN.
    ``intro_chapter_limit`` is the season's limit on an intro chapter deciding alone (``season_intro_chapter_limits``).
    """
    order = _decision_order(ctx.settings)
    evidence = ctx.store.get_evidence(rec.id)
    # A lock always wins (spec §5.5 rule 1): the editor only ever writes a lock the user asked for, and dropping it here
    # would silently republish over that edit with no way back (the detected answer a lock replaced isn't stored). To
    # let detection decide a type again the user unlocks it.
    locked = ctx.store.get_locked(rec.id)
    frame_rate = _frame_rate(ctx, rec, evidence, order)

    # The window of the file's kind, chosen the way the detector chooses it (an episode has a season key); the credit
    # text detector bounds its reads with the same two numbers (``credits.detector._earliest_start_s``).
    credits_window_ms, movie_cap_ms = credits_limits_ms(
        is_episode=rec.season_key is not None,
        tv_window_s=ctx.settings.credits_tv_s,
        movie_window_s=ctx.settings.credits_movie_s,
    )

    def decide_from(sources: tuple[str, ...]) -> dict[MarkerType, TypeDecision]:
        enabled = set(sources)
        dctx = DecisionContext(
            rec.duration_ms or 0,
            rec.is_movie,
            APP_PUBLISH_WHEN,
            types,
            sources,
            intro_chapter_limit,
            movie_credits_max_from_end_ms=movie_cap_ms,
            credits_window_ms=credits_window_ms,
            frame_rate=frame_rate,
        )
        return decide([c for c in evidence if c.source.value in enabled], dctx, locked)

    registered = {source.value for spec in ctx.local_detectors for source in (spec.source, *spec.stored_sources)}
    unavailable = {source.value for source in _LOCAL_DETECTOR_SOURCES} - registered
    if ctx.chromaprint is ChromaprintState.UNKNOWN:
        unavailable -= {Source.SEASON_AUDIO.value, Source.SEASON_AUDIO_PREVIOUS.value}
    if ctx.credits_text is TextDetState.UNKNOWN:
        unavailable -= {Source.CREDITS_TEXT.value}
    # A local detector with nothing stored stays in the order only while it may still answer this file: a rule waiting
    # for its answer (credit text checking a credits chapter SkipDB contradicts, spec §5.5 rule 3) must not wait for
    # a detector that can't run here, that found nothing at its version now, or that failed to read the file as it is
    # (the owner's rule: decisions are automatic, never an open-ended wait in Needs review). An older version's
    # "nothing" is read again (``_detector_pending``), so the rule waits for that.
    answered = {c.source.value for c in evidence}
    order = tuple(
        source_id
        for source_id in order
        if source_id in answered
        or Source(source_id) not in _LOCAL_DETECTOR_SOURCES
        or (source_id not in unavailable and _may_still_answer(ctx, rec, Source(source_id)))
    )
    decisions = decide_from(order)
    if not any(c.source.value in unavailable and c.source.value in order for c in evidence):
        return decisions
    without = decide_from(tuple(source_id for source_id in order if source_id not in unavailable))
    return {
        mtype: without[mtype] if decision.status is DecisionStatus.DECIDED else decision
        for mtype, decision in decisions.items()
    }


def _may_still_answer(ctx: PipelineContext, rec: FileRecord, source: Source) -> bool:
    """Whether a local detector registered here may still give an answer stored under ``source`` for this file: it
    hasn't answered at its version now, and hasn't failed to read the file as it is (``LocalDetectorSpec.failed_here``).
    """
    if _answered_at_this_version(ctx, rec, source):
        return False
    return not any(
        source in spec.stored_sources and spec.failed_here is not None and spec.failed_here(rec, ctx)
        for spec in ctx.local_detectors
    )


def _answered_at_this_version(ctx: PipelineContext, rec: FileRecord, source: Source) -> bool:
    """Whether a local detector stored an answer under ``source`` for this file at its version now. A detector not
    registered here can't say what its version is, so its stored answer counts as it is. Its ``due`` isn't asked: season
    audio's reads the whole season, and credit text's is true for an answer stored without ``LOOK_BACK_BASIS`` (no
    answer of today's version is) or read against another credits chapter than the rules choose now, which a rule
    waiting for credit text still counts as an answer: its start is the file's own reading either way."""
    if ctx.store.evidence_fetched_at(rec.id, source) is None:
        return False
    stored = ctx.store.evidence_version(rec.id, source)
    return not any(
        source in spec.stored_sources and stored != spec.answer_version(rec, ctx) for spec in ctx.local_detectors
    )


def _carry_over(
    ctx: PipelineContext,
    rec: FileRecord,
    servers: _ItemServers,
    owners: list[_Owning],
    decisions: dict[MarkerType, TypeDecision],
) -> dict[MarkerType, TypeDecision]:
    """The file's final decisions with the carry-over (spec §5.5 rule 15, ``carry_over``): a type no source answered
    for keeps what the file it replaced had decided, at the same length, unless only content detectors that read this
    file now decided it. The servers' item ids are asked only when a type has no evidence (publishing asks them next
    anyway); a server that can't name the item, or a replaced file's disk that can't tell, leaves a marker carried
    before as it is."""

    def previous(wanted: frozenset[MarkerType]) -> dict[MarkerType, PreviousDecision | None]:
        item_ids = {owner.config.id: servers.item_id(owner) for owner in owners}
        items = [(server_id, item_id) for server_id, item_id in item_ids.items() if item_id]
        configs = list(ctx.registry.configs())
        return previous_decisions(
            ctx.store,
            rec,
            items,
            wanted=wanted,
            gone=lambda other: gone_now(other, configs),
            items_known=len(items) == len(item_ids),
        )

    return carry_over(
        decisions,
        rec.duration_ms or 0,
        previous,
        kept=ctx.store.get_markers(rec.id),
        enabled=_decision_order(ctx.settings),
        read_by=lambda mtype: _content_read_by(ctx, rec, mtype),
    )


def _content_read_by(ctx: PipelineContext, rec: FileRecord, mtype: MarkerType) -> dict[str, ReadNow]:
    """The local detectors of a type whose stored answer is today's verdict on the file (``_read_now``), per source
    their answers are stored under, with the version they read it at (the carry-over's ``read_by``)."""
    return {
        source.value: ReadNow(spec.answer_version(rec, ctx), spec.version_step)
        for spec in _read_now(ctx, rec, mtype)
        for source in spec.stored_sources
    }


def _read_now(ctx: PipelineContext, rec: FileRecord, mtype: MarkerType) -> list[LocalDetectorSpec]:
    """The local detectors of a type whose stored answer is today's verdict on the file: read at their version now,
    with something to compare it with (``LocalDetectorSpec.compared``), and not out of date (``LocalDetectorSpec.due``).
    An answer that is due and whose read again failed this time (the detector unavailable, a cancel) is an older
    verdict: season audio's "nothing" from before a sibling was fingerprinted counts for nothing, though "compared"
    asked now would say it had one."""
    return [
        spec
        for spec in ctx.local_detectors
        if mtype in spec.types
        and _answered_at_this_version(ctx, rec, spec.source)
        and (spec.compared is None or spec.compared(rec, ctx))
        and (spec.due is None or not spec.due(rec, ctx))
    ]


def _keep_published_before_rule_change(
    ctx: PipelineContext, rec: FileRecord, decisions: dict[MarkerType, TypeDecision]
) -> dict[MarkerType, TypeDecision]:
    """The file's decisions with a marker published before the decision rules changed kept where today's rules leave
    its type in Needs review or without a marker (``decide.keep_published``): a rule change alone never takes a marker
    off the servers; new or changed evidence can, and so can a detector that read the file and found nothing to agree
    with a marker resting only on sources that never decide alone (below).

    A type is looked at when its stored decision is decided with a marker of ours, and either it was kept this way
    before, or the file was last decided under older rules (``DECIDE_RULES`` in ``version_reruns``) and a server was
    sent that marker (its type and start in a publish state). Only answers of the sources turned on count; the new or
    changed ones are those not stored when the job's first stage of the file began (``_answers_before``), so an answer
    only stored again (a forced run, a parser's new version) is no news. A locked type is always decided (``decide``),
    and a marker carried over from a replaced file rests on no source, so neither is ever kept here. The local detectors
    of the type whose stored answer is today's verdict on the file (``_read_now``: at their version now, with something
    to compare it with, not due) go with it (``read_by``): a lone online answer isn't kept once one of them read the file without an answer agreeing
    with it. Season audio with no other episode to match finds nothing whatever the file holds, so it doesn't count.
    """
    undecided = [
        t for t, d in decisions.items() if d.status in (DecisionStatus.NEEDS_REVIEW, DecisionStatus.NO_EVIDENCE)
    ]
    if not undecided:
        return decisions
    stored = ctx.store.get_decisions(rec.id)
    markers = ctx.store.get_markers(rec.id)
    older_rules = ctx.store.version_rerun(rec.id, DECIDE_RULES) != DECIDE_RULES_VERSION
    sent: set[tuple[MarkerType, int]] | None = None
    answers: list[tuple[Candidate, tuple]] | None = None
    known = ctx._answers_before.get(rec.canonical_path, frozenset())
    out = dict(decisions)
    for mtype in undecided:
        row, marker = stored.get(mtype), markers.get(mtype)
        if row is None or row.status is not DecisionStatus.DECIDED or marker is None:
            continue
        if not kept_before_rule_change(row.reason):
            if not older_rules:
                continue
            if sent is None:
                sent = {(m.type, m.start_ms) for state in ctx.store.publish_states(rec.id) for m in state.markers}
            if (mtype, marker.start_ms) not in sent:
                continue
        if answers is None:
            answers = _answers_now(ctx, rec)
        of_type = [(c, key) for c, key in answers if c.type is mtype]
        out[mtype] = keep_published(
            decisions[mtype],
            marker,
            candidates=[c for c, _key in of_type],
            changed=[c for c, key in of_type if key not in known],
            duration_ms=rec.duration_ms or 0,
            read_by=[spec.source for spec in _read_now(ctx, rec, mtype)],
        )
    return out


def _answer_key(row: EvidenceRow) -> tuple:
    """What one stored answer says, whenever it was stored."""
    return (row.source, row.origin, row.type, row.start_ms, row.end_ms)


def _answers_now(ctx: PipelineContext, rec: FileRecord) -> list[tuple[Candidate, tuple]]:
    """The file's stored answers from the sources turned on, each with its ``_answer_key``; a server's marker made for
    an earlier file (``Candidate.stale``) left out, as ``decide`` leaves it out."""
    enabled = set(_decision_order(ctx.settings))
    return [
        (Candidate(row.type, row.start_ms, row.end_ms, row.source), _answer_key(row))
        for row in ctx.store.evidence_rows(rec.id)
        if row.type is not None
        and row.start_ms is not None
        and row.source.value in enabled
        and row.detail != STALE_SERVER_MARKERS_DETAIL
    ]


def _decisions_changed(
    store: MarkerStore, file_id: int, decisions: dict[MarkerType, TypeDecision], fingerprint: str
) -> bool:
    """Whether saving would change anything (``decided_at`` and marker times then mean "last changed")."""
    stored = store.get_decisions(file_id)
    markers = store.get_markers(file_id)
    for mtype, d in decisions.items():
        row = stored.get(mtype)
        proposed = (d.proposed.start_ms, d.proposed.end_ms) if d.proposed else (None, None)
        if row is None or (row.status, row.reason, (row.proposed_start_ms, row.proposed_end_ms)) != (
            d.status,
            d.reason,
            proposed,
        ):
            return True
        if row.settings_fingerprint != fingerprint:
            return True
        current = markers.get(mtype)
        if current is not None and current.locked:
            continue
        if current != (d.marker if d.status is DecisionStatus.DECIDED else None):
            return True
    return False


def _rests_only_on(decision: TypeDecision, sources: frozenset[str]) -> bool:
    """Whether a decided type's marker was decided by these sources alone."""
    if decision.status is not DecisionStatus.DECIDED or decision.marker is None:
        return False
    return set(decision.marker.decided_by) <= sources


def _decided_beyond_chapters(decision: TypeDecision) -> bool:
    """Decided, and not by chapters alone nor by season audio alone: two agreeing sources may still veto a chapter
    (spec §5.5 rule 3), and one disagreeing source sends a season-audio intro to review (owner, 2026-09-24). A marker
    carried over from a replaced file (rule 15) stands only until the file has evidence of its own.

    A chapter or season audio answer that markers already on servers shortened or confirmed still stands alone: server
    markers never decide on their own (rule 7).
    """
    if decision.status is not DecisionStatus.DECIDED or is_carried_over(decision.marker):
        return False
    return not (_rests_only_on(decision, _CHAPTERS_AND_SERVERS) or _rests_only_on(decision, _SEASON_AUDIO_AND_SERVERS))


def _all_decided(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> bool:
    return all(_decided_beyond_chapters(decisions[t]) for t in types)


def _only_confirming_chapters(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> bool:
    """Every type is decided and some only by chapters, so the search goes on only to let sources contradict them.

    A type season audio decided alone isn't: TheIntroDB is asked for it on its usual schedule, like every online source.
    """
    return (
        all(decisions[t].status is DecisionStatus.DECIDED for t in types)
        and not _all_decided(decisions, types)
        and not any(_rests_only_on(decisions[t], _SEASON_AUDIO_AND_SERVERS) for t in types)
    )


def _stale_evidence(ctx: PipelineContext, rec: FileRecord, source: Source) -> bool:
    """Whether stored evidence of an online source or a local detector was made by an older parser or detector
    version, so it is derived again. (Markers on servers are always visited; ``_server_markers_due`` checks their
    reader version per server.)"""
    if source in _ONLINE_LABELS:
        stored = ctx.store.evidence_fetched_at(rec.id, source) is not None
        return stored and ctx.store.evidence_version(rec.id, source) != PARSER_VERSIONS[source]
    return any(_answer_from_another_version(ctx, rec, spec) for spec in ctx.local_detectors if spec.source is source)


def _refreshing(ctx: PipelineContext, path: str, source: Source) -> bool:
    """Whether a forced run still has to ask ``source`` again for this file."""
    return ctx.force and source not in ctx._refreshed.get(path, ())


def _mark_refreshed(ctx: PipelineContext, path: str, source: Source) -> None:
    if ctx.force:
        ctx._refreshed.setdefault(path, set()).add(source)


def _answer_from_another_version(ctx: PipelineContext, rec: FileRecord, spec: LocalDetectorSpec) -> bool:
    """Whether the detector stored an answer (under any of its sources) with a version other than its own."""
    return any(
        ctx.store.evidence_fetched_at(rec.id, source) is not None
        and ctx.store.evidence_version(rec.id, source) != spec.answer_version(rec, ctx)
        for source in spec.stored_sources
    )


def _detector_due(ctx: PipelineContext, rec: FileRecord, spec: LocalDetectorSpec) -> bool:
    """Whether a detector's stored answer is missing, from another version, or out of date by its own ``due``."""
    version = spec.answer_version(rec, ctx)
    if any(ctx.store.evidence_version(rec.id, source) != version for source in spec.stored_sources):
        return True
    return bool(spec.due and spec.due(rec, ctx))


def _rests_on_detector(
    spec: LocalDetectorSpec, decisions: dict[MarkerType, TypeDecision], wanted: frozenset[MarkerType]
) -> bool:
    """Whether a decided type's marker was decided with this detector's own answer (a user's lock never counts).

    Such a decision can change when the answer does (season audio: a 1/1 match made before the rest of the season
    arrived), so a due answer is asked again even though the type is decided.
    """
    answers = {source.value for source in spec.stored_sources}
    return any(
        decisions[t].status is DecisionStatus.DECIDED
        and decisions[t].marker is not None
        and not decisions[t].marker.locked
        and not answers.isdisjoint(decisions[t].marker.decided_by)
        for t in wanted
    )


def _detector_pending(
    ctx: PipelineContext,
    rec: FileRecord,
    spec: LocalDetectorSpec,
    decisions: dict[MarkerType, TypeDecision],
    types: frozenset[MarkerType],
    *,
    refresh: bool,
) -> bool:
    """Whether a detector has to run at its source now.

    A forced run runs it once per file, and again (on the worker after the checking thread ran it) only when its answer
    is due. A normal run runs it when its stored answer is from another version, even for decided types (like an older
    parser's answer), or when its answer is due and a type it can decide is still undecided or was decided with that
    answer, or (``checks_chapters``) by chapters alone. A type decided by other sources doesn't ask whether the answer
    is due.
    """
    wanted = spec.types & types
    if not wanted:
        return False
    if refresh or _answer_from_another_version(ctx, rec, spec):
        return True
    asks = (
        ctx.force
        or any(decisions[t].status is not DecisionStatus.DECIDED for t in wanted)
        or _rests_on_detector(spec, decisions, wanted)
        or (
            spec.checks_chapters
            and any(_decided_by_chapters_alone(decisions[t]) for t in wanted)
            # The chapter decides without the answer: a file the detector failed on isn't read again for it.
            and not (spec.failed_here is not None and spec.failed_here(rec, ctx))
        )
    )
    return asks and _detector_due(ctx, rec, spec)


def _decided_by_chapters_alone(decision: TypeDecision) -> bool:
    """Decided by chapters (markers already on servers may have confirmed or shortened it), not locked."""
    return (
        decision.marker is not None and not decision.marker.locked and _rests_only_on(decision, _CHAPTERS_AND_SERVERS)
    )


def _decided_with_a_due_answer(
    ctx: PipelineContext,
    rec: FileRecord,
    source: Source,
    decisions: dict[MarkerType, TypeDecision],
    types: frozenset[MarkerType],
) -> bool:
    """Whether a type was decided with the answer of a detector at ``source`` that is due again."""
    return any(
        spec.source is source
        and _rests_on_detector(spec, decisions, spec.types & types)
        and _detector_due(ctx, rec, spec)
        for spec in ctx.local_detectors
    )


def _needs_worker(ctx: PipelineContext, rec: FileRecord, spec: LocalDetectorSpec) -> bool:
    return spec.needs_worker is None or spec.needs_worker(rec, ctx)


def _run_detector(
    ctx: PipelineContext,
    rec: FileRecord,
    spec: LocalDetectorSpec,
    *,
    gpu: str | None,
    gpu_device_path: str | None,
    phase: Callable[[str], None],
    cancel_check: Callable[[], bool] | None,
    pause_check: Callable[[], bool] | None,
    ffmpeg_threads: int | None = None,
    fallback_callback: Callable[[str], None] | None = None,
    gpu_worker: bool = False,
) -> str | None:
    """Run one detector and store its answer under each of its sources with its version, and its basis when it gave one,
    in one transaction.

    Anything but ``DetectorUnavailableError`` propagates, so a GPU error reaches the worker's CPU fallback.

    Returns:
        None once the answer is stored; why there was none when the detector couldn't answer this time.
    """
    try:
        answer = spec.detect(
            rec,
            ctx=ctx,
            gpu=gpu,
            gpu_device_path=gpu_device_path,
            phase_callback=phase,
            cancel_check=cancel_check,
            pause_check=pause_check,
            ffmpeg_threads=ffmpeg_threads,
            fallback_callback=fallback_callback,
            gpu_worker=gpu_worker,
        )
        found = list(answer.candidates if isinstance(answer, DetectorAnswer) else answer)
    except DetectorUnavailableError as exc:
        logger.info(
            "{} had no answer for {} this time: {}", spec.source.value, os.path.basename(rec.canonical_path), exc
        )
        return str(exc) or type(exc).__name__
    stray = [c for c in found if c.source not in spec.stored_sources]
    if stray:
        logger.warning("{} returned candidates for sources it doesn't store: {}", spec.source.value, stray)
    ctx.store.replace_detector_answer(
        rec.id,
        {source: [c for c in found if c.source is source] for source in spec.stored_sources},
        version=spec.answer_version(rec, ctx),
        run=(spec.source, answer.signature) if isinstance(answer, DetectorAnswer) else None,
    )
    ctx.note_answer_changed()
    return None


def _warn_budget_exhausted(ctx: PipelineContext, source: Source) -> None:
    """Log that ``source``'s daily budget ran out, once per job per source.

    Not once per file: a run-dry source can affect hundreds of files, and a line per file would flood the log for no
    extra information (spec finding 4 raised this from DEBUG). Never logs the key.
    """
    with ctx._budget_lock:
        first = source not in ctx._budget_warned
        ctx._budget_warned.add(source)
    if first:
        logger.warning(
            "{}'s daily lookup budget ran out; checking remaining files without it until it resets",
            _ONLINE_LABELS[source],
        )


def _job_wide_refusal(source: Source, result: LookupResult | None) -> str | None:
    """The answer when a source refused a lookup for a reason that holds for the whole job: its daily budget ran out,
    or (TheIntroDB) it refused the API key, requires one, or the key can't be sent. None for anything else."""
    if result is None or result.status != "unavailable":
        return None
    if is_budget_exhausted(result.detail) or (source is Source.THEINTRODB and is_key_refusal(result.detail)):
        return result.detail
    return None


def _count_skipped(ctx: PipelineContext, skipped: dict[Source, str]) -> None:
    """Count one file whose finished run was checked without these sources, each with the answer that stopped it (a
    used-up daily budget or a refused API key)."""
    with ctx._budget_lock:
        for source, detail in skipped.items():
            if is_budget_exhausted(detail):
                ctx._budget_exhausted[source] = ctx._budget_exhausted.get(source, 0) + 1
            else:
                first_detail, count = ctx._key_refused.get(source, (detail, 0))
                ctx._key_refused[source] = (first_detail, count + 1)


def _files_were(count: int) -> str:
    return f"{count} file was" if count == 1 else f"{count} files were"


def budget_exhausted_warnings(ctx: PipelineContext) -> list[str]:
    """Job-completion warnings for online sources this job checked files without: a daily budget that ran out, or an
    API key the source refused (one line per source, whatever the number of files).

    Args:
        ctx: The job's context, read after every file has run.

    Returns:
        User-facing warnings (empty when every source answered), e.g. "TheIntroDB's daily lookup limit was reached: 39
        files were checked without it. It resets at 00:00 UTC; the files it left undecided are checked again
        automatically after that (or add a TheIntroDB API key for a higher limit)." or "TheIntroDB rejected the API key
        (HTTP 401): 39 files were checked without it. Check the TheIntroDB API key in Settings → Intro & Credits."
    """
    with ctx._budget_lock:
        exhausted = dict(ctx._budget_exhausted)
        refused = dict(ctx._key_refused)
    warnings = []
    for source in sorted(exhausted, key=lambda s: _ONLINE_LABELS[s]):
        label = _ONLINE_LABELS[source]
        # job_runner queues TheIntroDB's undecided files for after the reset (``take_budget_rechecks``).
        after = (
            "the files it left undecided are checked again automatically after that (or add a TheIntroDB API key for a "
            "higher limit)"
            if source is Source.THEINTRODB
            else "run the library again after that"
        )
        warnings.append(
            f"{label}'s daily lookup limit was reached: {_files_were(exhausted[source])} checked without it. "
            f"It resets at {RESET_TIME_LABEL}; {after}."
        )
    for source in sorted(refused, key=lambda s: _ONLINE_LABELS[s]):
        detail, count = refused[source]
        label = _ONLINE_LABELS[source]
        warnings.append(
            f"{detail}: {_files_were(count)} checked without it. Check the {label} API key in Settings → Intro & "
            "Credits."
        )
    return warnings


def _lookup(
    client: Any,
    source: Source,
    ids: MediaIds,
    rec: FileRecord,
    ctx: PipelineContext,
    cancel_check,
    *,
    max_wait_s: float | None = None,
) -> LookupResult | None:
    """Ask one online source and store what it found.

    Args:
        client: The source's client.
        source: The source.
        ids: The ids to look up.
        rec: The file.
        ctx: The job's context.
        cancel_check: True once the job is cancelled.
        max_wait_s: The longest the source's limiter may wait for a request slot (``ratelimit.capped_waits``); None
            leaves the limiter's own wait.

    Returns:
        The source's answer (stored when ``ok`` or ``no_data``), or None when the client raised or answered with
        something that isn't a ``LookupResult``.
    """
    try:
        with capped_waits(max_wait_s):
            result = client.lookup(ids, duration_ms=rec.duration_ms, priority=ctx.priority(), cancel_check=cancel_check)
    except Exception as exc:
        logger.warning("{} lookup failed for {}: {}", _ONLINE_LABELS[source], rec.canonical_path, type(exc).__name__)
        return None
    if not isinstance(result, LookupResult):
        logger.debug("{} lookup for {}: {}", _ONLINE_LABELS[source], rec.canonical_path, result)
        return None
    if result.status in _STORED_LOOKUPS:
        ctx.store.replace_evidence(
            rec.id, source, list(result.candidates), detail=result.detail, version=PARSER_VERSIONS[source]
        )
        return result
    # unavailable / not_applicable: nothing is stored, so the next run asks again. A refused TheIntroDB key is logged
    # by its client, once per client (a job builds its own clients).
    if is_budget_exhausted(result.detail):
        _warn_budget_exhausted(ctx, source)
    else:
        logger.debug("{} lookup for {}: {}", _ONLINE_LABELS[source], rec.canonical_path, result)
    return result


def _show_folder(path: str) -> str:
    """The folder of an episode's show: its season folder's parent, or its own folder when episodes sit straight in the
    show folder."""
    folder = os.path.dirname(path)
    return os.path.dirname(folder) if is_season_folder(os.path.basename(folder)) else folder


def _series_lookups_paused_until(ctx: PipelineContext, source: Source, ids: MediaIds, path: str) -> datetime | None:
    """Until when ``source`` isn't asked about this episode: enough of its series' episodes had no entry there.

    A forced run (a re-detect) always asks.

    Returns:
        The end of the pause, or None when the episode may be asked now. The file's job log line names the skip.
    """
    key = theintrodb.series_key(ids) if source in _SERIES_PAUSED_SOURCES else None
    if key is None or ctx.force:
        return None
    return ctx.store.series_lookups_paused_until(
        source, key, pause=SERIES_NO_ENTRY_PAUSE, show_folder=_show_folder(path)
    )


def _note_series_answer(
    ctx: PipelineContext, source: Source, ids: MediaIds, path: str, result: LookupResult | None
) -> None:
    """Remember a stored answer for the episode's series, and say so when it starts a pause."""
    if source not in _SERIES_PAUSED_SOURCES or result is None or result.status not in _STORED_LOOKUPS:
        return
    key = theintrodb.series_key(ids)
    if key is None or ids.season is None or ids.episode is None:
        return
    until = ctx.store.record_series_lookup(
        source,
        key,
        f"S{ids.season:02d}E{ids.episode:02d}",
        found=result.status == "ok",
        misses=SERIES_NO_ENTRY_MISSES,
        pause=SERIES_NO_ENTRY_PAUSE,
        show_folder=_show_folder(path),
    )
    if until is not None:
        logger.info(
            "{} has no entry for {} or more episodes of {} ({}); its episodes aren't looked up there until {}",
            _ONLINE_LABELS[source],
            SERIES_NO_ENTRY_MISSES,
            show_name(path),
            key,
            until.strftime("%Y-%m-%d %H:%M UTC"),
        )


def _importer_plugin(ctx: PipelineContext, owner: _Owning) -> tuple[bool, str | None]:
    """Whether the server's plugin list could be read, and its importer plugins of a crowd database (IntroDB/TheIntroDB,
    SkipDB, AniSkip), joined; the copy counts as that database's group. Asked once per server per job."""
    sid = owner.config.id
    with ctx._importer_guard:
        lock = ctx._importer_locks.setdefault(sid, threading.Lock())
    with lock:
        if sid not in ctx._importers:
            try:
                names = owner.server.get_plugin_names()
            except Exception as exc:
                logger.debug("Plugin list on {} failed: {}", owner.config.name, type(exc).__name__)
                names = None
            ctx._importers[sid] = None if names is None else (importer_plugin(names) or "")
        found = ctx._importers[sid]
    return found is not None, found or None


def _counted_as(ctx: PipelineContext, owner: _Owning, found: list[Candidate]) -> tuple[Source, list[Candidate], str]:
    """The source a server's markers count as, the markers, and the detail stored with them (spec §5.5 rule 8).

    Markers on a server with an importer plugin are that database's copy; on one whose plugin list couldn't be read
    they may be, so they count as "none there" and are read again like an empty answer.
    """
    if found and owner.config.type in _IMPORTER_PLUGIN_SERVERS:
        readable, importer = _importer_plugin(ctx, owner)
        if not readable:
            return Source.SERVER_MARKERS, [], PLUGINS_UNKNOWN_DETAIL
        if importer:
            return Source.SERVER_MARKERS_IMPORTED, found, imported_detail(importer)
    return Source.SERVER_MARKERS, found, ""


def _read_server_markers(
    ctx: PipelineContext,
    rec: FileRecord,
    servers: _ItemServers,
    refresh: bool,
    *,
    first_read_only: bool,
    notes: RunNotes | None = None,
) -> tuple[set[str], set[str]]:
    """Store each owning server's current markers for the file as evidence, when they are due.

    Args:
        ctx: The job's context.
        rec: The file.
        servers: The file's owning servers and their item ids.
        refresh: A forced run: read every server we never published to.
        first_read_only: Every wanted type is already decided, so only a server never asked for this file (or asked by
            an older reader), and not showing our markers, is read -- its own markers can still shorten decided credits
            (spec §5.5 rule 7). An empty or unusable answer isn't asked again on such a run, unless the job checks
            servers (``ctx.recheck_empty_server_markers``): then it is on its backoff
            (``MarkerStore.server_recheck_due``). A Plex answer stored while Plex couldn't tell whether its markers
            were made for this file is read again once it can (``_staleness_known_now``). A server showing our markers
            is never read, nor a Plex or Emby item that may show ours (another version's, or a type kept as the
            server's own); an answer from such a Plex server or item stored before ``PLEX_CHECKED_SINCE`` stops
            counting (``_drop_older_reader_answer``) -- the server itself is never contacted for that.
        notes: The file's run notes, so a server never read at all for this file says why (``server_not_read``); None
            when nothing logs this run (``publish_now``'s own use of this function).

    Returns:
        ``(asked, dropped)``: the ids of the servers actually contacted this run, and of the servers whose stored
        answer was cleared without contacting them (``_drop_older_reader_answer``).
    """
    asked: set[str] = set()
    dropped: set[str] = set()
    for owner in servers.owning:
        cfg = owner.config
        published = ctx.store.get_publish_state(rec.id, cfg.id)
        if published and published.markers:
            # What's there now is (partly) ours: never a second opinion.
            if notes is not None:
                notes.server_not_read[cfg.id] = "this server shows our markers"
            if cfg.type is ServerType.PLEX and _drop_older_reader_answer(ctx, rec, cfg.id):
                dropped.add(cfg.id)
            continue
        if (
            not refresh
            and not _server_markers_due(ctx, rec, cfg.id, first_read_only=first_read_only)
            and not _staleness_known_now(ctx, rec, servers, owner)
        ):
            if notes is not None:
                notes.server_not_read[cfg.id] = "not due yet"
            continue
        item_id = servers.item_id(owner)
        if not item_id:
            if notes is not None:
                notes.server_not_read[cfg.id] = "not in this server's library yet"
            continue
        item_wide = cfg.type in _ITEM_WIDE_MARKERS
        if item_wide and ctx.store.published_to_item(cfg.id, item_id):
            if notes is not None:
                notes.server_not_read[cfg.id] = "another version of this item shows our markers"
            if cfg.type is ServerType.PLEX and _drop_older_reader_answer(ctx, rec, cfg.id, OURS_ON_ITEM_DETAIL):
                dropped.add(cfg.id)
            continue
        found = servers.markers(owner, item_id, rec.duration_ms)
        if found is None:
            rows = _server_rows(ctx, rec, cfg.id)
            if not rows or ctx.store.evidence_version(rec.id, rows[0].source, cfg.id) != READER_VERSION:
                # Remembered, so a run with everything decided doesn't ask again; a run still missing evidence does.
                # An older reader's answer goes too: it would be due again on every run, and this reader can't vouch
                # for it (a Plex library that hides a type used to read as "none there").
                ctx.store.replace_evidence(
                    rec.id,
                    Source.SERVER_MARKERS,
                    [],
                    origin=cfg.id,
                    detail=UNUSABLE_SERVER_MARKERS_DETAIL,
                    version=READER_VERSION,
                    also_replaces=SERVER_SOURCES - {Source.SERVER_MARKERS},
                )
                asked.add(cfg.id)  # servers.markers() above did contact it -- it just had nothing usable to give
            elif ctx.recheck_empty_server_markers:
                # The stored answer stays, but the re-read counts: a read that always fails (another cut on a Plex
                # item, a server that can't serve markers) stops being asked after the last backoff step too.
                ctx.store.count_failed_server_reread(rec.id, cfg.id)
            continue
        if item_wide and ctx.store.published_to_item(cfg.id, item_id):
            # Another version of this item was published while the read was out: it may show ours.
            if notes is not None:
                notes.server_not_read[cfg.id] = "another version of this item shows our markers"
            if cfg.type is ServerType.PLEX and _drop_older_reader_answer(ctx, rec, cfg.id, OURS_ON_ITEM_DETAIL):
                dropped.add(cfg.id)
            continue
        source, found, detail = _counted_as(ctx, owner, found)
        if found and cfg.type is ServerType.PLEX:
            stale = _plex_types_not_made_for_file(ctx, servers, owner, item_id)
            if stale is None:
                # Counted as before, and kept due so the next run asks Plex again (``_server_markers_due``).
                detail = STALENESS_UNKNOWN_DETAIL
            else:
                # Stored flagged, so decide counts them for nothing (``Candidate.stale``) and the Inspector says why.
                found = [replace(c, stale=True) if c.type in stale else c for c in found]
        ctx.store.replace_evidence(
            rec.id,
            source,
            list(found),
            origin=cfg.id,
            detail=detail,
            version=READER_VERSION,
            also_replaces=SERVER_SOURCES - {source},
        )
        asked.add(cfg.id)
    return asked, dropped


def _drop_older_reader_answer(
    ctx: PipelineContext, rec: FileRecord, server_id: str, detail: str = OURS_SHOWN_DETAIL
) -> bool:
    """Stop counting a Plex answer stored before answers were checked for markers made for an earlier file
    (``PLEX_CHECKED_SINCE``), once the server shows (or its item may show) our markers.

    Such an answer can't be read again to be checked, so, as when the reader can't read the server
    (``_read_server_markers``), it goes: kept, a stale Plex marker would still confirm online times timed on another
    release. A checked answer stays as it was stored, whatever ``READER_VERSION`` is now: counted, flagged as made for
    an earlier file (``Candidate.stale``), or counted while Plex couldn't tell (``STALENESS_UNKNOWN_DETAIL``; dropping
    that one would take Plex's own marker away wherever Plex can never tell, e.g. an agent older than the answer). It
    is recorded as today's reader's, so it isn't due, and its item looked up, on every run after a version bump.

    Args:
        ctx: The job's context.
        rec: The file.
        server_id: The Plex server.
        detail: Why, as stored with the empty answer.

    Returns:
        Whether the stored answer was replaced.
    """
    rows = _server_rows(ctx, rec, server_id)
    if not rows:
        return False
    version = ctx.store.evidence_version(rec.id, rows[0].source, server_id)
    if version is not None and version >= PLEX_CHECKED_SINCE:
        if version != READER_VERSION:
            ctx.store.restamp_evidence_version(rec.id, rows[0].source, server_id, READER_VERSION)
        return False
    ctx.store.replace_evidence(
        rec.id,
        Source.SERVER_MARKERS,
        [],
        origin=server_id,
        detail=detail,
        version=READER_VERSION,
        also_replaces=SERVER_SOURCES - {Source.SERVER_MARKERS},
    )
    return True


def _staleness_known_now(ctx: PipelineContext, rec: FileRecord, servers: _ItemServers, owner: _Owning) -> bool:
    """Whether Plex, which couldn't tell last time whether its stored markers were made for this file, can tell now.

    Then the answer is read again and stored flagged even on a run that needs no more evidence, so decide stops
    counting markers Plex now says were made for an earlier file.
    """
    if not any(r.detail == STALENESS_UNKNOWN_DETAIL for r in _server_rows(ctx, rec, owner.config.id)):
        return False
    item_id = servers.item_id(owner)
    return bool(item_id) and _plex_types_not_made_for_file(ctx, servers, owner, item_id) is not None


def _server_rows(ctx: PipelineContext, rec: FileRecord, server_id: str) -> list[EvidenceRow]:
    return [r for r in ctx.store.evidence_rows(rec.id) if r.source in SERVER_SOURCES and r.origin == server_id]


_ASKED_AGAIN_DETAILS = frozenset(
    {UNUSABLE_SERVER_MARKERS_DETAIL, STALE_SERVER_MARKERS_DETAIL, STALENESS_UNKNOWN_DETAIL}
)


def _server_markers_due(ctx: PipelineContext, rec: FileRecord, server_id: str, *, first_read_only: bool) -> bool:
    rows = _server_rows(ctx, rec, server_id)
    if not rows or ctx.store.evidence_version(rec.id, rows[0].source, server_id) != READER_VERSION:
        return True
    if first_read_only:
        if not ctx.recheck_empty_server_markers:
            return False
        # Check servers: an empty answer is asked again on its backoff (RECHECK_AFTER), then no more.
        return ctx.store.server_recheck_due(rec.id, server_id, now=ctx.now(), after=RECHECK_AFTER)
    if any(r.detail in _ASKED_AGAIN_DETAILS for r in rows):
        # Unreadable, another cut, a Plex library hiding a type, markers made for an earlier file, or Plex couldn't tell
        # whether they were, last time: asked again on every run that still needs evidence.
        return True
    fetched = max(datetime.fromisoformat(r.fetched_at) for r in rows)
    if not all(r.type is None for r in rows) or ctx.now() - fetched <= EMPTY_SERVER_MARKERS_RETRY:
        return False
    if ctx.recheck_empty_server_markers:
        # Check servers keeps to its backoff for an empty answer it already read again.
        return ctx.store.server_recheck_due(rec.id, server_id, now=ctx.now(), after=RECHECK_AFTER)
    return True


def _plex_types_not_made_for_file(
    ctx: PipelineContext, servers: _ItemServers, owner: _Owning, item_id: str
) -> frozenset[MarkerType] | None:
    """The types whose markers Plex shows for this file were made for an earlier file at its path.

    Plex keeps an item's markers when its file is replaced, and only its database can tell (the HTTP API has no
    timestamps), so the publisher reads it (``types_not_made_for_file``), locally or through the Plex marker agent,
    briefly and stopping when the job is cancelled. Asked once per run for the file.

    Returns:
        Those types. Empty when this job may not use the server's database at all (Intro & Credits off, or the
        database write not confirmed): nothing will ever tell, so every marker counts as before. None when Plex
        couldn't tell this time (an agent older than the answer, a busy or unreachable database): counted as before,
        and asked again on the next run -- an agent too old to tell isn't asked again for this job's other files
        (``ctx._stale_unanswerable``).
    """
    cfg = owner.config

    def read() -> frozenset[MarkerType] | None:
        if cfg.id in ctx._stale_unanswerable:
            return None
        try:
            # Built to wait briefly: its capability check waits for the database locks too, and a busy database means
            # "can't tell" (asked again next run), never a writer's wait for every file.
            publisher = publisher_for(
                owner.server,
                cfg,
                settings_provider=lambda: _live_markers_settings(ctx, cfg),
                ui_details=False,
                db_timeout_s=STALE_READ_WAIT_S,
            )
            if publisher is None:
                return frozenset()
            with cancellable_waits(servers.cancel_check):
                report = _cached_capability(ctx, cfg, publisher, wait_s=STALE_READ_WAIT_S)
                if report is None:
                    return None
                if report.state in _SETTINGS_ANSWERS:
                    return frozenset()
                if not report.ready:
                    return None
                stale = publisher.types_not_made_for_file(item_id)
            if stale is None and publisher.stale_types_unanswerable is True:
                ctx._stale_unanswerable.add(cfg.id)
            return stale
        except Exception as exc:
            logger.debug("Couldn't ask {} which markers are stale: {}", cfg.name, type(exc).__name__)
            return None

    return servers.stale_types(owner, read)


def _own_types_now(
    ctx: PipelineContext, rec: FileRecord, servers: _ItemServers, owner: _Owning, *, stale_counts: bool = False
) -> frozenset[MarkerType]:
    """The types a server shows markers of its own detection of for this file, read from the server on this run.

    Not from stored evidence: a stored answer with markers is never read again, and a server that lost its marker must
    be seen on the next run. The evidence read shares this answer (``_ItemServers.markers``), so a server the run
    reads anyway is asked once. "Own" is rule 7's (spec §5.5), from the same code the evidence comes from: the reader
    answers None for another cut (a Plex item with a version more than 2 s apart, another version's Emby item), an
    importer plugin's copy counts as its database (``_counted_as``), and Jellyfin's and Emby's readers leave ours out.
    Plex can't tell ours from its own, so a type this app has on the item or left there from this file is never the
    server's own, and after a failed write, when what is ours there may be unknown, no type is.

    Nor, for Plex, a type whose every marker can't be right for the file, or (unless ``stale_counts``) one whose
    markers were made for an earlier file at its path: the file is read for it. ``stale_counts`` is for a type left
    undecided once the file was read: then Plex's stale marker is still the closest there is, and "Keep Plex's" keeps
    it.
    """
    cfg = owner.config
    item_id = servers.item_id(owner)
    if not item_id:
        return frozenset()
    item_row = ctx.store.get_item_publish_state(cfg.id, item_id)
    if item_row is not None and item_row.status == "failed":
        return frozenset()
    last = ctx.store.get_publish_state(rec.id, cfg.id)
    ours = {m.type for m in (item_row.markers if item_row else ())} | {m.type for m in (last.markers if last else ())}
    found = servers.markers(owner, item_id, rec.duration_ms)
    if found is None:
        return frozenset()
    source, found, _detail = _counted_as(ctx, owner, found)
    if source is not Source.SERVER_MARKERS:
        return frozenset()
    own = frozenset(c.type for c in found) - ours
    if cfg.type is ServerType.PLEX:
        # A type whose every marker can't be right for this file means Plex hasn't really processed it for that type:
        # the file is read for it, and Plex's publisher doesn't keep those markers either (``_kept_types``). Emby's
        # plugin keeps its own rows whatever they say, so reading the file for Emby would find an answer never shown.
        limits = _file_limits(rec)
        usable = {c.type for c in found if not unusable_server_marker(c, limits)}
        own &= usable
        if own and not stale_counts:
            own -= _plex_types_not_made_for_file(ctx, servers, owner, item_id) or frozenset()
    if own and cfg.type is ServerType.PLEX and servers.part_count(owner, item_id) != 1:
        # Plex shows one marker set per item and writes a type only once every version decided it alike, so a version
        # left undecided keeps the others waiting. An item with several versions (or parts we can't count) is read as
        # before this feature.
        return frozenset()
    return own


def _kept_own_applies(ctx: PipelineContext, cfg: ServerConfig, rec: FileRecord, store: MarkerStore) -> bool:
    """Whether a stored kept status still names this server: it keeps its own now and the last run published there."""
    try:
        keeps = _live_markers_settings(ctx, cfg).keeps_server_markers
    except Exception as exc:
        logger.warning("Couldn't read the saved settings of {}: {}", cfg.name, type(exc).__name__)
        return False
    return keeps and store.get_publish_state(rec.id, cfg.id) is not None


def _kept_by_every_destination(
    ctx: PipelineContext,
    rec: FileRecord,
    servers: _ItemServers,
    owners: list[_Owning],
    types: frozenset[MarkerType],
    *,
    stale_counts: bool = False,
) -> frozenset[MarkerType]:
    """The types no answer of ours would be shown for: no local detector reads the file for them, and one left
    undecided isn't in review (``stale_counts``: see ``_own_types_now``).

    A type qualifies when every server the file's markers go to keeps its own markers ("Keep Plex's", "Keep Emby's")
    and shows its own of that type now; a locked type never does (a lock wins, spec §5.5 rule 1). Worked out on every
    run from the saved settings and what the servers show, and never stored, so switching a server to "Use ours", a
    server losing its marker or a new destination without one reads the file again. Every server's setting is checked
    before any server is asked.

    Returns:
        Those types; empty when any server's settings or markers can't be read.
    """
    candidates = types - frozenset(ctx.store.get_locked(rec.id))
    for owner in owners:
        # Only a type a server can show can be its own: a recap under Plex asks no server.
        candidates &= supported_types_for(owner.config.type)
    if not candidates or not owners:
        return frozenset()
    try:
        if not all(_live_markers_settings(ctx, owner.config).keeps_server_markers for owner in owners):
            return frozenset()
    except Exception as exc:
        logger.warning("Couldn't read the saved server settings for {}: {}", rec.canonical_path, type(exc).__name__)
        return frozenset()
    for owner in owners:
        candidates &= _own_types_now(ctx, rec, servers, owner, stale_counts=stale_counts)
        if not candidates:
            break
    return candidates


def _row(
    cfg: ServerConfig,
    adapter_name: str,
    status: ServerStatus,
    message: str,
    path: str,
    *,
    reason_code: str | None = None,
    kept_types: frozenset[MarkerType] = frozenset(),
) -> dict:
    row = {
        "server_id": cfg.id,
        "server_name": cfg.name,
        "server_type": cfg.type.value,
        "adapter_name": adapter_name or "markers",
        "status": status.value,
        "message": message,
        "canonical_path": path,
        "frame_source": "",
        "output_paths": [],
        # The item's kept types as of this same write, under the same lock the write itself already took (see
        # ``_ITEM_LOCKS`` above) -- so the job log's per-server line (``_server_result``) can read it straight
        # from here instead of taking that lock again just to log, which would otherwise queue behind another
        # version's write to the same item with no bound.
        "kept_types": kept_types,
    }
    if reason_code:
        row["reason_code"] = reason_code
    return row


def _plex_pass_unknown(report: CapabilityReport) -> bool:
    return "plex_pass" in report.details and report.details["plex_pass"] is None


def _still_fresh(ctx: PipelineContext, cached: tuple[float, CapabilityReport] | None) -> bool:
    if cached is None:
        return False
    ttl = ctx.capability_ttl_s
    if _plex_pass_unknown(cached[1]):
        ttl = min(ttl, PLEX_PASS_UNKNOWN_TTL_S)
    return time.monotonic() - cached[0] < ttl


def cached_capability(ctx: PipelineContext, cfg: ServerConfig, publisher: MarkerPublisher) -> CapabilityReport:
    """Per-server capability, cached for ``capability_ttl_s``; one fetch per server even when every check thread misses.

    An off or unconfirmed answer isn't cached: Plex reads it from the saved settings, and switching Intro & Credits
    back on must reach the job's next file, not one 5 minutes later. Nor is one that gave up on a busy database
    (``details["db_busy"]``). A ready Plex whose Plex Pass didn't answer is reused for ``PLEX_PASS_UNKNOWN_TTL_S`` only.
    """
    report = _cached_capability(ctx, cfg, publisher, wait_s=None)
    assert report is not None  # waiting without a bound always gets the check
    return report


def _cached_capability(
    ctx: PipelineContext, cfg: ServerConfig, publisher: MarkerPublisher, *, wait_s: float | None
) -> CapabilityReport | None:
    """:func:`cached_capability`, waiting at most ``wait_s`` for another thread's check of the server to finish.

    That check may wait minutes for a busy Plex database, so a caller that must answer briefly (the read of which Plex
    markers were made for an earlier file) waits in slices that stop once the job is cancelled
    (``base.cancellable_waits``), and gets None when the wait runs out. ``wait_s`` None waits for it.
    """
    cached = ctx._capabilities.get(cfg.id)
    if _still_fresh(ctx, cached):
        return cached[1]
    with ctx._capability_guard:
        lock = ctx._capability_locks.setdefault(cfg.id, threading.Lock())
    if wait_s is None:
        lock.acquire()
    elif not _acquire_briefly(lock, wait_s):
        return None
    try:
        cached = ctx._capabilities.get(cfg.id)
        if _still_fresh(ctx, cached):
            return cached[1]
        report = publisher.capability()
        if report.details.get("db_busy"):
            # A database busy just now says nothing about the job's next file, which asks again.
            ctx._capabilities.pop(cfg.id, None)
        elif report.state not in _SETTINGS_ANSWERS:
            ctx._capabilities[cfg.id] = (time.monotonic(), report)
        return report
    finally:
        lock.release()


def _acquire_briefly(lock: threading.Lock, wait_s: float) -> bool:
    """Take ``lock`` within ``wait_s``, giving up early once this thread's job is cancelled (``wait_cancelled``)."""
    deadline = time.monotonic() + wait_s
    while not wait_cancelled():
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        if lock.acquire(timeout=min(WAIT_SLICE_S, left)):
            return True
    return False


def identity_changed(rec: FileRecord) -> bool:
    """Whether the file on disk is no longer the one this record describes (spec §6.1: path + size + mtime).

    Args:
        rec: The stored file record.

    Returns:
        True when the file was replaced or is gone, so nothing stored about it can be trusted.
    """
    try:
        st = os.stat(rec.canonical_path)
    except OSError:
        return True
    return (st.st_size, st.st_mtime_ns) != (rec.size, rec.mtime_ns)


def _one_version_shows_other_times(cfg: ServerConfig, item_row: ItemPublishStateRow, wanted: list[Marker]) -> bool:
    """Whether a one-version Plex item holds times of ours other than decided for a type both have.

    Only another version can make a Plex item keep times other than the calling file's
    (``publishers.base.agreed_across_versions``). Until 2026-09-25 a one-version item kept its earlier times too when a
    decision moved by under ``VERSION_AGREEMENT_MS``; the decision hasn't changed since, so its publish basis still
    matches and only this sends it again.
    """
    if cfg.type is not ServerType.PLEX or item_row.item_files is None or len(item_row.item_files) != 1:
        return False
    for mtype in {m.type for m in item_row.markers}:
        decided = [m for m in wanted if m.type is mtype]
        if decided and not same_times([m for m in item_row.markers if m.type is mtype], decided):
            return True
    return False


def _previous_on_item(item_row: ItemPublishStateRow | None, publisher: MarkerPublisher) -> list[Marker] | None:
    """What this app last left on the server item, from any file (None = unknown).

    Plex serves one set per item across its versions, so this is the item's record, not the file's. After a failed
    write it is still exact for an atomic publisher (Plex: one transaction); the Jellyfin plugin may have stored data
    before its error, so there it is unknown, and a publisher then clears what it can prove is ours.
    """
    if item_row is None:
        return []
    if item_row.status == "failed" and not publisher.atomic_writes:
        return None
    return list(item_row.markers)


def _consent_problem(ctx: PipelineContext, cfg: ServerConfig, path: str) -> str | None:
    """Why the saved settings no longer allow writing this file to this server, or None when they do.

    A Plex block whose database-write confirmation was cleared loads as off.
    """
    live = ctx.live_config(cfg.id)
    if live is None:
        return "This server was removed"
    if not live.enabled:
        return "This server is turned off on the Servers page"
    if not load_server(live.markers, live.type.value).enabled:
        return "Intro & Credits is off for this server"
    if live.exclude_paths and is_path_excluded(path, live.exclude_paths):
        return "This file is excluded on this server"
    if not marker_matches(path, [live]):
        return "This library isn't selected for Intro & Credits on this server"
    return None


def _shown_on_server(
    publisher: MarkerPublisher,
    cfg: ServerConfig,
    item_id: str,
    ours: list[Marker],
    kept_types: frozenset[MarkerType],
    item_files: tuple[str, ...] | None,
) -> Shown | None:
    """What the server shows of ``ours`` (and of the types it keeps as its own, and of the item's versions) now.

    Returns:
        None when it couldn't be read.
    """
    try:
        return publisher.shows(item_id, ours, kept_types=kept_types, item_files=item_files)
    except Exception as exc:
        # A transient read problem mustn't fail or rewrite a file whose records say it is up to date.
        logger.debug("Couldn't read back the markers on {} for item {}: {}", cfg.name, item_id, type(exc).__name__)
        return None


def _live_markers_settings(ctx: PipelineContext, cfg: ServerConfig) -> ServerMarkersSettings:
    live = ctx.live_config(cfg.id)
    if live is None or not live.enabled:
        return load_server(None, cfg.type.value)
    return load_server(live.markers, live.type.value)


def _busy_wording(ctx: PipelineContext, message: str, path: str) -> str:
    """A busy database's message for a file's row: "this job tries again in a few minutes" only where the job does queue
    the file's retry (``PipelineContext.promise_busy_retry``), its own "trying again on the next run" otherwise."""
    return message.replace(NEXT_RUN, RETRY_SOON) if ctx.promise_busy_retry(path) else message


def _publish_to(
    owner: _Owning,
    rec: FileRecord,
    markers: dict[MarkerType, Marker],
    in_review: str,
    servers: _ItemServers,
    ctx: PipelineContext,
    phase: Callable[[str], None],
    *,
    kept_own: frozenset[MarkerType] = frozenset(),
    brief_db_wait: bool = False,
    notes: RunNotes | None = None,
) -> dict:
    # in_review: why the file's types in Needs review are there (``review_message``), "" when none is. kept_own: types
    # left undecided while every server keeps its own and shows one. The row's wording names them, and the file is
    # recorded on its item even with nothing to send; what is sent is exactly what an undecided type sends.
    # brief_db_wait: a worker whose job retries a busy write waits for Plex's database only
    # ``plex_db.WORKER_BUSY_TIMEOUT_S`` (the retry comes a few minutes later), also for another thread's check of it.
    # notes: so the job log's "Sent to" line can say "our last sent" (see the ``sent_before`` assignment below) --
    # None: unknown, the manual "publish now" path logs nothing.
    cfg = owner.config
    path = rec.canonical_path
    db_timeout_s = ctx.db_timeout_s
    if db_timeout_s is None and brief_db_wait:
        db_timeout_s = WORKER_BUSY_TIMEOUT_S
    store = ctx.store
    last = store.get_publish_state(rec.id, cfg.id)

    def _finish(
        status: ServerStatus,
        message: str,
        *,
        name: str,
        item_id: str | None = None,
        published=None,
        reason_code: str | None = None,
        kept_types: frozenset[MarkerType] = frozenset(),
    ) -> dict:
        # Rows that don't know the item keep the last one, so the Inspector still shows where our markers went.
        store.set_publish_state(
            rec.id,
            cfg.id,
            item_id=item_id or (last.item_id if last else None),
            markers=published,
            status=STATE_BY_STATUS[status],
            message=message,
        )
        return _row(cfg, name, status, message, path, reason_code=reason_code, kept_types=kept_types)

    def _not_written(
        status: ServerStatus, message: str, *, name: str, item_id: str | None = None, reason_code: str | None = None
    ) -> dict:
        store.clear_publish_basis(rec.id, cfg.id)
        return _finish(status, message, name=name, item_id=item_id, reason_code=reason_code)

    publisher = publisher_for(
        owner.server,
        cfg,
        sibling_markers=lambda p: markers_for_path(store, p),
        settings_provider=lambda: _live_markers_settings(ctx, cfg),
        ui_details=False,  # Plex's own detection settings are for the Edit dialog: one more Plex request per check
        db_timeout_s=db_timeout_s,
    )
    if publisher is None:
        return _not_written(ServerStatus.SKIPPED, "Not supported for this server type yet", name="")
    # Another thread's check of the same Plex server can itself wait minutes for its busy database.
    capability_wait_s = db_timeout_s if brief_db_wait and cfg.type is ServerType.PLEX else None
    try:
        report = _cached_capability(ctx, cfg, publisher, wait_s=capability_wait_s)
    except Exception as exc:
        logger.warning("Couldn't check whether {} can receive markers: {}", cfg.name, type(exc).__name__)
        message = f"Couldn't check this server: {type(exc).__name__}"
        return _not_written(ServerStatus.FAILED, message, name=publisher.name)
    if report is None:
        if wait_cancelled():
            return _not_written(ServerStatus.FAILED, WAIT_CANCELLED, name=publisher.name)
        message = _busy_wording(
            ctx, f"Another Intro & Credits task is still checking this Plex server; {NEXT_RUN}", path
        )
        return _not_written(ServerStatus.FAILED, message, name=publisher.name, reason_code=PLEX_DB_BUSY)
    if report.details.get("db_busy"):
        # The check gave up on a busy database: the file fails as a busy write does, and the job retries it.
        return _not_written(
            ServerStatus.FAILED,
            _busy_wording(ctx, report.message, path),
            name=publisher.name,
            reason_code=PLEX_DB_BUSY,
        )
    if not report.ready:
        return _not_written(ServerStatus.SKIPPED, report.message or report.state.value, name=publisher.name)
    if _plex_pass_unknown(report):
        # Plex serves no markers without a Pass (spec §6.3). Usually Plex is restarting: a file a few seconds later
        # asks again and the job retries this one.
        return _not_written(
            ServerStatus.WAITING,
            "Can't reach Plex to confirm Plex Pass",
            name=publisher.name,
            reason_code=PLEX_PASS_UNKNOWN,
        )
    item_id = servers.item_id(owner)
    if not item_id:
        message = "Not in this server's library yet"
        return _not_written(ServerStatus.WAITING, message, name=publisher.name, reason_code=NOT_IN_LIBRARY)
    if notes is not None:
        # A row's own ``markers`` field is trustworthy as what the server was last sent whenever it's non-empty (a
        # WAITING row can still store ``published=ours``; a FAILED row keeps the last WRITTEN set, per ``_finish``
        # below: "rows that don't know the item keep the last one") or when the status really is ``written`` (which
        # can itself carry an empty tuple -- markers we deliberately cleared, still a known answer). A merge or
        # split can move the file to a different item since that row was stored, whose markers we've never sent.
        known = last is not None and last.item_id == item_id and (last.status == "written" or last.markers)
        notes.sent_before[cfg.id] = tuple(last.markers) if known else None

    wanted = publisher.project(markers.values())
    decided_hash = MarkerStore.markers_hash(wanted)
    basis = store.get_publish_basis(rec.id, cfg.id)
    # After a merge or split the file's part still carries what it published on its old item.
    moved = last is not None and last.item_id is not None and last.item_id != item_id and bool(last.markers)
    own_previous = list(last.markers) if moved else None
    # Until the write lands, the old item is where this file's markers are: a failed attempt keeps pointing there
    # so the next run offers them again.
    attempted_item = last.item_id if moved else item_id

    def _unchanged(item_version: int) -> bool:
        # Neither this file's decided set nor the item changed since this file last published there: another
        # version's publish (or failure) on the same Plex item changes the item row's version, and this file's own
        # failed attempts clear its basis.
        return (
            last is not None
            and last.item_id == item_id
            and last.status == "written"
            and basis == (decided_hash, item_version)
        )

    vendor = cfg.type.value.capitalize()

    def _shown_differently(ours: list[Marker]) -> str:
        # How the server shows some of ours other than decided (Emby: credits that end before the file does).
        return publisher.projection_note(ours, duration_ms=rec.duration_ms)

    def _up_to_date(kept_types: frozenset[MarkerType]) -> dict:
        if in_review and not wanted:
            return _row(cfg, publisher.name, ServerStatus.NEEDS_REVIEW, in_review, path, kept_types=kept_types)
        message = with_kept_note("", kept_note(kept_types, wanted, vendor, not_decided=kept_own)) or "Up to date"
        message = with_kept_note(message, _shown_differently([m for m in wanted if m.type not in kept_types]))
        return _row(cfg, publisher.name, ServerStatus.UP_TO_DATE, message, path, kept_types=kept_types)

    # Held from reading what is ours on the item until the result is recorded (lock order: see _KeyedLocks).
    with _ITEM_LOCKS.hold((cfg.id, item_id)):
        # The job's registry is a snapshot: switching Intro & Credits (or the server, or a library) off must stop
        # a running or paused job's very next write.
        try:
            refused = _consent_problem(ctx, cfg, path)
        except Exception as exc:
            logger.warning("Couldn't read the saved settings of {}: {}", cfg.name, type(exc).__name__)
            ctx._capabilities.pop(cfg.id, None)
            message = f"Couldn't read this server's saved settings ({type(exc).__name__})"
            return _not_written(ServerStatus.FAILED, message, name=publisher.name)
        if refused:
            ctx._capabilities.pop(cfg.id, None)
            return _not_written(ServerStatus.SKIPPED, refused, name=publisher.name)
        item_row = store.get_item_publish_state(cfg.id, item_id)
        # A forced re-detect and a waiting row always look at the item again: a version may have been added (never
        # decided) or removed without this file's decision or the item row changing.
        if not ctx.force and item_row is not None and _unchanged(item_row.version):
            # Our records can't see the server: its own detection, or a rescan of the file, can drop or replace ours.
            # Whether a type is kept as the server's own (Keep Plex's) is the publisher's call, so any difference
            # goes through its write; so does a kept type once the server is set to restore ours.
            if not item_row.markers and not item_row.kept_types:
                return _up_to_date(item_row.kept_types)  # nothing of ours there to look for
            if item_row.markers and item_row.item_files is None and cfg.type is ServerType.PLEX:
                # Recorded before this app kept a Plex item's versions, so a version added since can't be seen: one
                # write records them (it changes nothing, and takes no write lock, while the item is as recorded).
                reason = "the item's versions aren't recorded yet"
            elif _one_version_shows_other_times(cfg, item_row, wanted):
                reason = "it shows other times than decided"
            else:
                shown = _shown_on_server(
                    publisher, cfg, item_id, list(item_row.markers), item_row.kept_types, item_row.item_files
                )
                if shown is None:
                    return {**_up_to_date(item_row.kept_types), READ_BACK_FAILED: True}
                # A kept type goes back to ours once the server is set to use ours — and once the user locks it, since
                # a marker the user adjusted or locked wins over "Keep Plex's" / "Keep Emby's" (spec §5.5 rule 1).
                # A lock alone doesn't change the decided times, so without this the unchanged test above would leave
                # the server showing its own markers for a type the user just took over.
                released = bool(item_row.kept_types) and (
                    not _live_markers_settings(ctx, cfg).keeps_server_markers
                    or bool(item_row.kept_types & {m.type for m in wanted if m.locked})
                )
                if shown is Shown.OURS and not released:
                    return _up_to_date(item_row.kept_types)
                reason = {
                    Shown.OURS: "set to restore this app's markers",
                    Shown.VERSIONS_CHANGED: "the item's versions changed since last run",
                    Shown.GONE: "the server no longer has this item",
                }.get(shown, f"markers {shown.value} since last run")
            logger.info("{} item {}: {}; publishing again", cfg.name, item_id, reason)
        previous = _previous_on_item(item_row, publisher)
        # A kept type is released by the write alone: Emby's plugin still stores ours for it out of sight, and a Plex
        # record left holding one would be read back as drift on every Check servers run. Plex sends nothing then.
        holds_kept = item_row is not None and bool(item_row.kept_types)
        # A type left to the server's own marker goes through the write too, which sends nothing (no markers, nothing
        # of ours before) but records the file on its item, so Check servers reads the server's own marker back.
        nothing_to_send = not wanted and previous == [] and own_previous is None and not holds_kept
        if nothing_to_send and not kept_own:
            if in_review:
                return _row(cfg, publisher.name, ServerStatus.NEEDS_REVIEW, in_review, path)
            message = "This server can't show the markers found for this file" if markers else "No markers found"
            return _row(cfg, publisher.name, ServerStatus.NONE, message, path)

        if identity_changed(rec):
            raise _FileChangedError(path)
        phase(f"Publishing to {cfg.name}…")
        try:
            ours = sorted(
                publisher.write(
                    item_id,
                    wanted,
                    previous=previous,
                    own_previous=own_previous,
                    duration_ms=rec.duration_ms,
                    canonical_path=path,
                    kept_types=item_row.kept_types if item_row is not None else frozenset(),
                    limits=_file_limits(rec),
                ),
                key=lambda m: (m.start_ms, m.type.value),
            )
        except ItemNotFoundError as exc:
            return _not_written(
                ServerStatus.WAITING,
                str(exc),
                name=publisher.name,
                item_id=attempted_item,
                reason_code=NOT_IN_LIBRARY,
            )
        except PublishError as exc:
            if exc.state not in (None, Capability.READY):
                # The server stopped being able to take markers (DB moved, Plex down, schema changed): check it again.
                # Per-item failures carry no state and say nothing about the server.
                ctx._capabilities.pop(cfg.id, None)
            store.set_item_publish_state(cfg.id, item_id, None, "failed")
            # A database locked past the wait is usually free again within minutes: the job retries the file, and
            # only then does the row say so.
            busy = PLEX_DB_BUSY if isinstance(exc, DatabaseBusyError) else None
            message = _busy_wording(ctx, str(exc), path) if busy else str(exc)
            return _not_written(
                ServerStatus.FAILED, message, name=publisher.name, item_id=attempted_item, reason_code=busy
            )
        except Exception as exc:
            logger.exception("Publishing markers to {} failed for {}", cfg.name, path)
            store.set_item_publish_state(cfg.id, item_id, None, "failed")
            message = f"{type(exc).__name__}: {exc}"
            return _not_written(ServerStatus.FAILED, message, name=publisher.name, item_id=attempted_item)

        changed = publisher.last_write_changed
        kept = publisher.last_kept_types
        replaced_own = publisher.last_replaced_own_types
        unchecked_versions = publisher.last_unchecked_versions
        unchecked_files = [str(path) for path in publisher.last_unchecked_files] if unchecked_versions else []
        if nothing_to_send and item_row is not None and item_row.status != "written":
            # Nothing was sent: another version's failed write stays on record for its retry (files_of_failed_items).
            version = item_row.version
        else:
            version = store.set_item_publish_state(
                cfg.id, item_id, ours, "written", kept_types=kept, item_files=publisher.last_item_files
            )
        if not changed and _unchanged(version):
            return _up_to_date(kept)  # a forced run whose write changed nothing
        store.set_publish_basis(rec.id, cfg.id, decided_hash=decided_hash, item_version=version)
        note = with_kept_note(kept_note(kept, wanted, vendor, not_decided=kept_own), _shown_differently(ours))
        # Ours replaced markers the server keeps: the user's lock, or the server's markers were made for an earlier file.
        override = with_sentence(
            replaced_own_note(replaced_own, vendor), replaced_stale_note(publisher.last_replaced_stale_types, vendor)
        )

        def _says_override(row: dict) -> dict:
            # The user's lock took the server's own markers off it although the server keeps its own: the row names
            # the types so the Inspector and the editor can say which servers that happened on (spec §5.5 rule 1).
            if not replaced_own:
                return row
            return {**row, REPLACED_OWN: [t.value for t in MarkerType if t in replaced_own]}

        shown_types = {m.type for m in ours} | kept
        waiting_for = [m.type.value for m in wanted if m.type not in shown_types]
        if waiting_for:
            # Plex shows a type only when every version of the item is decided and agrees on it. A version not checked
            # yet may be checked (or deleted) later, so the job tries this file again; versions that disagree don't.
            message = with_kept_note(f"{VERSIONS_WAITING}: {', '.join(waiting_for)}", note)
            message = with_sentence(message, override)
            row = _finish(
                ServerStatus.WAITING,
                message,
                name=publisher.name,
                item_id=item_id,
                published=ours,
                reason_code=VERSIONS_UNCHECKED if unchecked_versions else None,
                kept_types=kept,
            )
            if unchecked_files:
                row[UNCHECKED_FILES] = unchecked_files
            return _says_override(row)
        if ours:
            message = f"{len(ours)} marker(s)"
        else:
            message = "Cleared our markers from this server" if changed or not note else ""
        message = with_sentence(with_kept_note(message, note), override)
        written = _finish(
            ServerStatus.WRITTEN, message, name=publisher.name, item_id=item_id, published=ours, kept_types=kept
        )
        # Recorded either way, but only a write that changed the server says so: it already showed this.
        return _says_override(written) if changed else _up_to_date(kept)


class FileNotAnalysedError(Exception):
    """The file has no stored duration, so nothing can be published for it yet (it was never detected)."""


class FileChangedError(Exception):
    """The file on disk is no longer the one markers.db describes."""


def publish_now(
    canonical_path: str,
    *,
    registry: Any,
    live_config: Callable[[str], ServerConfig | None] = live_server_config,
    deadline_s: float = PUBLISH_NOW_DEADLINE_S,
    lock_wait_s: float = PUBLISH_NOW_LOCK_WAIT_S,
    clock: Callable[[], float] = time.monotonic,
) -> list[dict]:
    """Publish a file's stored markers to every owning server once, inside one bounded web request (ruling P-R1).

    This is the Inspector editor's publish, not a job: it takes no worker slot, queues nothing and starts no thread,
    and every server it reaches goes through the same ``_publish_to`` a job uses — the same ``previous``,
    ``duration_ms``, ``canonical_path``, ``own_previous`` and ``kept_types`` arguments, the same ``publish_state`` rows
    and the same messages (types the last run left to the servers' own markers read from its stored decisions), so
    Check servers can't tell the two apart.

    The bound has two halves. Each of a server's own calls is capped by its ``ServerConfig.timeout`` (HTTP), which the
    caller shortens for this path, or by ``PUBLISH_NOW_DB_WAIT_S`` (Plex's database locks) — **per call**, so a Plex
    leg that checks the server and then writes can spend that wait several times over. On top of that, ``deadline_s``
    is a start gate: a server the fan-out hasn't started by then isn't started at all, its row says so and its publish
    basis is cleared, so the next run publishes it. So N slow servers can't multiply, and one server already under way
    runs to a small multiple of the shortened wait instead of a small multiple of ``plex_db.BUSY_TIMEOUT_S``.

    Args:
        canonical_path: Local path of the file (already validated by the caller).
        registry: A ``ServerRegistry``, ideally built with shortened per-server timeouts.
        live_config: A server's saved config right now; consent is read from it before every write, as in a job.
        deadline_s: Seconds the whole fan-out may take before the servers left are reported instead of contacted.
        lock_wait_s: Seconds to wait for a job that is running this file right now.
        clock: Monotonic clock (tests pass a fake).

    Returns:
        One row per owning server, in registry order, in the job's ``publisher_rows`` shape. Servers with Intro &
        Credits off are included, as ``markers_skipped`` rows saying why.

    Raises:
        FileNotAnalysedError: The file isn't in markers.db, or its duration was never read.
        FileChangedError: The file on disk changed since it was analysed.
    """
    store = get_marker_store()
    rec = store.get_file(canonical_path)
    if rec is None or not rec.duration_ms:
        raise FileNotAnalysedError(canonical_path)
    if identity_changed(rec):
        raise FileChangedError(canonical_path)
    ctx = PipelineContext(
        registry=registry,
        config=None,  # only build_context's detector checks read it; this path runs no detector
        settings=get_global_settings(),
        store=store,
        priority=lambda: PRIORITY_LOW,
        ffprobe="",  # nothing here probes: the file was probed by the run that stored its duration
        clients={},
        local_detectors=(),
        chromaprint=ChromaprintState.ABSENT,
        credits_text=TextDetState.ABSENT,
        live_config=live_config,
        db_timeout_s=PUBLISH_NOW_DB_WAIT_S,
    )
    item = ProcessableItem(canonical_path=canonical_path, server_id="", item_id_by_server={})
    owning = _owning_servers(item, ctx)
    marker_owners = _marker_owners(owning, canonical_path)
    owner_ids = {owner.config.id for owner in marker_owners}
    servers = _ItemServers(item, marker_owners)
    markers = store.get_markers(rec.id)
    decisions = store.get_decisions(rec.id)
    in_review = review_message(decisions, decisions.keys())
    # The types the file's last run left to the servers' own markers, so the rows say so as that run's did.
    kept_own = frozenset(mtype for mtype, d in decisions.items() if is_kept_own(d.status, d.reason))
    stop_at = clock() + deadline_s

    def _later(cfg: ServerConfig, message: str, status: ServerStatus = ServerStatus.FAILED) -> dict:
        """Record a server this request didn't publish to, so the next run does."""
        last = store.get_publish_state(rec.id, cfg.id)
        store.clear_publish_basis(rec.id, cfg.id)
        store.set_publish_state(
            rec.id,
            cfg.id,
            item_id=last.item_id if last else None,
            markers=None,
            status=STATE_BY_STATUS[status],
            message=message,
        )
        return _row(cfg, "", status, message, canonical_path)

    rows = []
    # A job already running this file decided from the evidence as it was before the save, so letting the two publish
    # at once could leave the user's edit on markers.db and the old answer on the servers. Waiting for the whole run
    # would block a web thread for minutes, so the request gives up quickly and leaves the publish to the next run.
    with _PATH_LOCKS.try_hold(canonical_path, lock_wait_s) as taken:
        for owner in owning:
            cfg = owner.config
            if cfg.id not in owner_ids:
                try:
                    refused = _consent_problem(ctx, cfg, canonical_path)
                except Exception as exc:
                    logger.warning("Couldn't read the saved settings of {}: {}", cfg.name, type(exc).__name__)
                    refused = f"Couldn't read this server's saved settings ({type(exc).__name__})"
                rows.append(_row(cfg, "", ServerStatus.SKIPPED, refused or SERVER_MARKERS_OFF, canonical_path))
                continue
            if not taken:
                rows.append(_later(cfg, PUBLISH_BUSY_MESSAGE, ServerStatus.WAITING))
                continue
            if clock() >= stop_at:
                rows.append(_later(cfg, PUBLISH_DEADLINE_MESSAGE))
                continue
            try:
                # Only a server that still keeps its own and took the last run's rows: one switched to Use ours or
                # added since gets what an undecided type gets.
                left = kept_own if _kept_own_applies(ctx, cfg, rec, store) else frozenset()
                rows.append(_publish_to(owner, rec, markers, in_review, servers, ctx, _no_phase, kept_own=left))
            except _FileChangedError:
                rows.append(_later(cfg, "The file changed while it was being published"))
            except Exception as exc:
                logger.exception("Publishing the user's markers to {} failed for {}", cfg.name, canonical_path)
                rows.append(_later(cfg, f"{type(exc).__name__}: {exc}"))
    return rows


def _summary(
    decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType], budget_exhausted: tuple[str, ...] = ()
) -> str:
    parts = []
    for mtype in MarkerType:
        d = decisions[mtype]
        if mtype not in types and not (d.marker and d.marker.locked):
            continue
        if d.status is DecisionStatus.DECIDED and d.marker:
            by = ", ".join(d.marker.decided_by)
            parts.append(f"{mtype.value} {clock(d.marker.start_ms)}–{clock(d.marker.end_ms)} ({by})")
        elif d.status is DecisionStatus.NEEDS_REVIEW:
            proposed = ""
            if d.proposed is not None:
                by = ", ".join(d.proposed.decided_by)
                proposed = f": {clock(d.proposed.start_ms)}–{clock(d.proposed.end_ms)} from {by}"
            parts.append(f"{mtype.value} needs review ({d.reason}){proposed}")
        elif d.status is DecisionStatus.DISABLED:
            # Only a type every server keeps its own of is off among the enabled types (``kept_own_reason``).
            parts.append(f"{mtype.value}: {d.reason}")
        else:
            parts.append(f"{mtype.value}: none")
    text = "; ".join(parts) or "Nothing to detect for this file"
    # Only when this file's result could still change once the source is available again: everything already
    # decided beyond chapters alone means asking it again would tell the user nothing new.
    if budget_exhausted and not _all_decided(decisions, types):
        note = "; ".join(f"{label} not checked (daily limit reached)" for label in budget_exhausted)
        return f"{text}; {note}"
    return text


def _request_season_chapter_followups(ctx: PipelineContext, sibling_limits: dict[str, int | None]) -> None:
    """Ask again for siblings decided with a season intro-chapter limit that has changed since (finding F1).

    State, not events: every season step compares each sibling's stored limit with the one it would get now, so a
    change is noticed whichever episode runs next, after a worker handoff, a restart, a deleted or replaced episode, or
    a member another episode's step probed. A sibling is asked again only when the new limit changes its intro
    decision, re-decided with the kind its own run used. One never decided is left alone (its own run sees the whole
    group). One changed on disk since it was decided is asked again without deciding it here: its stored evidence is
    the old file's, and one run reads the new file.
    """
    stale = []
    for path, limit in sibling_limits.items():
        sibling = ctx.store.get_file(path)
        if sibling is None:
            continue
        stored = ctx.store.get_decisions(sibling.id).get(MarkerType.INTRO)
        if stored is None:
            continue
        if identity_changed(sibling):
            stale.append(path)
            continue
        if ctx.store.get_intro_chapter_limit(sibling.id) == (True, limit):
            continue
        types = _enabled_types(ctx.settings, _stored_ids(ctx.store, sibling))
        decision = _decide(ctx, sibling, types, limit)[MarkerType.INTRO]
        if is_kept_own(stored.status, stored.reason) and decision.status is not DecisionStatus.DECIDED:
            continue  # still undecided, so still left to the servers' own marker; its own run checks the servers
        if _decisions_changed(ctx.store, sibling.id, {MarkerType.INTRO: decision}, stored.settings_fingerprint):
            stale.append(path)
    if stale:
        ctx.request_followups(stale)


def _not_on_disk(path: str, ctx: PipelineContext) -> ItemOutcome:
    """The outcome of a file not on disk, by previews' rule (``source_replaced_reason``): one a newer file replaced in
    its folder (on any disk of its library) is gone from disk for good, and its job queues no retry (the newer file is
    run on its own); any other is not found, and a webhook's job tries it again (it may still be copying in)."""
    replaced = source_replaced_reason(path, ctx.registry)
    if replaced is None:
        return ItemOutcome(FileOutcome.FILE_NOT_FOUND.value, "File not found on disk")
    # The app log's account; the job's log gets the file's one line (``_log_gone``).
    logger.bind(**{JOB_LOG_SKIP: True}).info(
        "Source file {} is no longer on disk and a newer file took its place ({}); skipping without a retry. "
        "The newer file gets its own Intro & Credits run from its own webhook or the next scan.",
        path,
        replaced.removeprefix("Skipped: "),
    )
    return ItemOutcome(FileOutcome.SOURCE_GONE.value, replaced)


def _built_line(build: Callable[[], str]) -> str | None:
    """``build()``, or None (logged at debug) when building it raised."""
    try:
        return build()
    except Exception as exc:
        logger.debug("Couldn't write a job log line: {}", type(exc).__name__)
        return None


def _log_live(build: Callable[[], str], level: str = "INFO") -> None:
    """Log one line as the file's work happens, live. Never raises: a bug building it (an unexpected shape in a
    step's own data) mustn't fail the file or hold up another owner's write -- the same guard ``_log_file`` gives
    the file's last lines, for every line logged before them.

    Args:
        build: Builds the line's text; logged only when it doesn't raise.
        level: The line's level.
    """
    line = _built_line(build)
    if line is not None:
        write_line(line, level)


def _log_source(notes: RunNotes, key: tuple[Source, str], build: Callable[[], str]) -> None:
    """A source's (or a server's own markers') line, guarded like ``_log_live`` -- but logged at most once per run
    for the same ``key`` (a source, or a server's id for its own markers): a worker picking up where the checking
    thread left off, with nothing new to say about it, doesn't repeat the line. A fresh answer to that key clears
    it first (``RunNotes.answered``), so a genuine change is never held back by it, even for a source (an online
    lookup, say) whose own line is logged through here every time, answered or not.

    Args:
        notes: The run's notes (``logged_sources`` remembers each key, across stages).
        key: The source, and a server's id for its own markers' line (else "").
        build: Builds the line's text; logged only when it doesn't raise and this key hasn't logged yet this run.
    """
    if key in notes.logged_sources:
        return
    line = _built_line(build)
    if line is None:
        return
    notes.logged_sources.add(key)
    write_line(line)


def _attempt(
    item: ProcessableItem,
    ctx: PipelineContext,
    *,
    local: bool,
    gpu: str | None,
    gpu_device_path: str | None,
    phase: Callable[[str], None],
    cancel_check: Callable[[], bool] | None,
    pause_check: Callable[[], bool] | None,
    skipped: dict[Source, str],
    notes: RunNotes,
    ffmpeg_threads: int | None = None,
    fallback_callback: Callable[[str], None] | None = None,
    gpu_worker: bool = False,
) -> ItemOutcome | None:
    """One run of a file; ``skipped`` (updated in place) holds the sources it was checked without for the whole job's
    reason, carried over from the file's earlier stages (``_run`` counts them once the file has its outcome), and
    ``notes`` (updated in place) what the file's stages did with each source, for its job log lines."""
    path = item.canonical_path

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    owning = _owning_servers(item, ctx)
    owners = _publishing_owners(item, ctx, owning)
    if isinstance(owners, ItemOutcome):
        return owners
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        # Held under the file's run lock; the row is read before its disk is checked.
        if mark_if_missing(ctx.store, ctx.store.get_file(path), list(ctx.registry.configs())):
            ctx.note_missing()
        return _not_on_disk(path, ctx)
    except OSError as exc:
        return ItemOutcome(FileOutcome.FAILED.value, f"Couldn't read the file: {type(exc).__name__}")
    if not stat.S_ISREG(st.st_mode):
        return ItemOutcome(FileOutcome.FILE_NOT_FOUND.value, "File not found on disk")

    refresh_probe = _refreshing(ctx, path, Source.CHAPTERS)
    existing = ctx.store.get_file(path)
    if existing is not None and existing.missing_since is not None:
        ctx.store.clear_missing(existing.id)  # on disk again, even if this run stops before storing it
    unchanged = existing is not None and (existing.size, existing.mtime_ns) == (st.st_size, st.st_mtime_ns)
    if path not in ctx._answers_before:
        ctx._answers_before[path] = (
            frozenset(_answer_key(r) for r in ctx.store.evidence_rows(existing.id)) if unchanged else frozenset()
        )
        ctx._replaced_at_start[path] = existing is not None and not unchanged
    probe = None
    stale_rules = unchanged and ctx.store.evidence_version(existing.id, Source.CHAPTERS) != CHAPTER_RULES_VERSION
    if refresh_probe or not unchanged or not existing.duration_ms or stale_rules:
        phase("Reading chapters…")
        try:
            probe = probe_media(path, ffprobe=ctx.ffprobe)
        except ProbeError as exc:
            return ItemOutcome(FileOutcome.FAILED.value, f"Couldn't read the file: {exc}")
    if cancelled():
        return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)

    servers = _ItemServers(item, owning, cancel_check)
    known_kind = ctx.store.get_server_kind(existing.id) if unchanged else None
    path_ids = ids_from_path(path)
    ids, lookups_allowed, confirmed_kind = _resolve_kind(path_ids, servers, known_kind)
    types = _enabled_types(ctx.settings, ids)
    notes.types, notes.is_episode = types, ids.is_episode
    title = _note_title(ctx, notes, item, ids, servers, owners)
    if not notes.worker and not notes.start_logged:
        # A worker's own pickup line already named this (``log_pickup``); the checking thread names itself here,
        # live, rather than waiting for the file to finish.
        notes.start_logged = True
        _log_live(lambda: file_start_line(title, types, is_episode=ids.is_episode))
    rec = ctx.store.upsert_file(
        FileIdentity(path, st.st_size, st.st_mtime_ns),
        duration_ms=probe.duration_ms if probe else None,
        season_key=os.path.dirname(path) if ids.is_episode else None,
        is_movie=ids.kind == "movie",
    )
    if confirmed_kind is not None:
        ctx.store.set_server_kind(rec.id, confirmed_kind)
    if not unchanged or probe is not None:
        ctx.note_answer_changed()
    if probe is not None:
        ctx.store.set_frame_rate(rec.id, probe.frame_rate, identity=(rec.size, rec.mtime_ns))
        # The episode-only chapter names read the kind from the PATH, not the resolved kind: the path is
        # part of the file's identity, so stored candidates can never disagree with it, while a resolved
        # kind can change under a cache that only re-reads on CHAPTER_RULES_VERSION. Measured cost of the
        # stricter input: none (evidence/eval/phase4-chapters.md).
        ctx.store.replace_evidence(
            rec.id,
            Source.CHAPTERS,
            chapter_candidates(probe, is_episode=path_ids.is_episode),
            version=CHAPTER_RULES_VERSION,
        )
        notes.answered(Source.CHAPTERS)
        _log_source(
            notes,
            (Source.CHAPTERS, ""),
            lambda: titled(
                title, source_line(Source.CHAPTERS, make_source_view(ctx.store.evidence_rows(rec.id), notes, skipped, {}, frozenset()))
            ),
        )  # fmt: skip
    elif ctx.settings.source_enabled(Source.CHAPTERS.value):
        # Not reread this run (the file's unchanged): its line still comes from what's already stored, same as
        # every other reused source, rather than being left out of the file's block (``_log_source`` skips it when
        # an earlier stage of this same run -- the checking thread -- already logged it: no second line for that).
        _log_source(
            notes,
            (Source.CHAPTERS, ""),
            lambda: titled(
                title, source_line(Source.CHAPTERS, make_source_view(ctx.store.evidence_rows(rec.id), notes, skipped, {}, frozenset()))
            ),
        )  # fmt: skip
    _mark_refreshed(ctx, path, Source.CHAPTERS)
    if not rec.duration_ms:
        return ItemOutcome(FileOutcome.FAILED.value, "Couldn't read the file's duration")
    # Season step for chapters (finding F1): an intro chapter far longer than the season's others needs a second source.
    # Siblings are compared before any detector can hand this file to a worker.
    intro_limit = None
    if MarkerType.INTRO in types and ctx.settings.source_enabled(Source.CHAPTERS.value):
        phase("Comparing the season's intro chapters…")
        intro_limit, sibling_limits = season_intro_chapter_limits(ctx, path)
        _request_season_chapter_followups(ctx, sibling_limits)
    for spec in ctx.local_detectors:
        if spec.followups is not None and spec.types & types and ctx.settings.source_enabled(spec.source.value):
            ctx.request_followups(spec.followups(rec, ctx))
    # A normal run stops asking once stored answers decide everything beyond chapters alone (answers from an older
    # parser, reader or detector version are still asked again; ``_detector_pending`` says when a detector runs); a
    # forced run asks every source and runs every detector once, so no stale answer is left behind. A server never
    # asked for this file is still read once everything is decided: its own markers can shorten decided credits
    # (spec §5.5 rule 7); once we publish to a server its markers are never read again.
    gather_all = ctx.force
    decisions = _decide(ctx, rec, types, intro_limit)
    lookup_ids: MediaIds | None = None
    # Types every server the markers go to keeps its own of and shows now: an answer of ours would never be shown, so
    # no local detector reads the file for them. Asked once a detector would run or a type ends undecided, at most once
    # per run.
    kept_everywhere: frozenset[MarkerType] | None = None
    # A source skipped as "not needed (already decided)", with the types it was skipped for: a later step can take away
    # the evidence they were decided with (an older reader's Plex answer dropped, markers Plex now says were made for an
    # earlier file), so each is asked once more at the end of this run while one of them is no longer decided.
    skipped_decided: dict[Source, frozenset[MarkerType]] = {}

    def sources_in_order() -> Iterator[Source]:
        yield from (Source(source_id) for source_id in ctx.settings.ordered_enabled_sources())
        for skipped_source, skipped_for in list(skipped_decided.items()):
            if not _all_decided(decisions, skipped_for):
                notes.not_asked.pop(skipped_source, None)
                # It's being asked for real this time (a later step took away the evidence it was "already decided"
                # with): its "not needed" line from earlier in this same run mustn't hold back the answer it gets.
                notes.logged_sources.discard((skipped_source, ""))
                yield skipped_source

    for source in sources_in_order():
        refresh = _refreshing(ctx, path, source)
        if (
            not gather_all
            and source is not Source.SERVER_MARKERS
            and _all_decided(decisions, types)
            and not _stale_evidence(ctx, rec, source)
            and not _decided_with_a_due_answer(ctx, rec, source, decisions, types)
        ):
            notes.not_asked[source] = ALREADY_DECIDED
            skipped_decided[source] = types
            if source is not Source.CHAPTERS and not (source in EPISODE_ONLY_SOURCES and not notes.is_episode):
                _log_source(
                    notes,
                    (source, ""),
                    lambda s=source, d=decisions, t=types: titled(
                        title, source_line(s, make_source_view(ctx.store.evidence_rows(rec.id), notes, skipped, d, t))
                    ),
                )  # fmt: skip
            continue
        if cancelled():
            return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
        pending: list = []
        if source in _ONLINE_LABELS:
            client = ctx.clients.get(source.value)
            if (
                source is Source.THEINTRODB
                and not gather_all
                and ctx.priority() >= PRIORITY_LOW
                and _only_confirming_chapters(decisions, types)
            ):
                # the only daily-budgeted source is kept for files it could still decide
                notes.not_asked[source] = "not asked (daily lookups kept for files it could still decide)"
                if not (source in EPISODE_ONLY_SOURCES and not notes.is_episode):
                    _log_source(
                        notes,
                        (source, ""),
                        lambda s=source, d=decisions, t=types: titled(
                            title,
                            source_line(s, make_source_view(ctx.store.evidence_rows(rec.id), notes, skipped, d, t)),
                        ),
                    )
                continue
            if not lookups_allowed:
                notes.not_asked[source] = "not asked (no server confirmed whether it's a movie or an episode)"
            elif client is None:
                notes.not_asked[source] = "not asked (not set up)"
            elif local and source in notes.unanswered:
                # An earlier stage or attempt of this file's run asked it and got no answer to store (unavailable,
                # blocked, paused for the show): asked again, it would hold this worker for the same answer. Its note
                # stays, and the next job that runs the file asks.
                pass
            elif _needs_lookup(ctx, rec, source, refresh):
                lookup_ids = lookup_ids or _lookup_ids(ids, servers)
                paused_until = _series_lookups_paused_until(ctx, source, lookup_ids, path)
                if paused_until is not None:
                    # Shown in place of any older saved answer: this run didn't ask.
                    notes.unanswered[source] = (
                        f"skipped (no data for this show; asked again after {paused_until:%Y-%m-%d})"
                    )
                else:
                    phase(f"Looking up {_ONLINE_LABELS[source]}…")
                    result = _lookup(
                        client,
                        source,
                        lookup_ids,
                        rec,
                        ctx,
                        cancel_check,
                        max_wait_s=WORKER_LOOKUP_WAIT_S if local else None,
                    )
                    _note_series_answer(ctx, source, lookup_ids, path, result)
                    refusal = _job_wide_refusal(source, result)
                    if refusal is not None:
                        skipped[source] = refusal
                    elif result is not None and result.status in _STORED_LOOKUPS:
                        skipped.pop(source, None)  # an earlier stage's refusal no longer holds (the budget reset)
                        notes.answered(source)
                    else:
                        notes.unanswered[source] = _unstored_lookup(source, result)
        elif source is Source.SERVER_MARKERS:
            phase("Reading markers already on servers…")
            first_read_only = not gather_all and _all_decided(decisions, types)
            asked_servers, dropped_servers = _read_server_markers(
                ctx, rec, servers, refresh, first_read_only=first_read_only, notes=notes
            )
            for server_id in asked_servers:
                notes.answered(Source.SERVER_MARKERS, server_id)
            for server_id in dropped_servers:
                notes.dropped_evidence(Source.SERVER_MARKERS, server_id)
        else:
            here = [spec for spec in ctx.local_detectors if spec.source is source]
            pending = [spec for spec in here if _detector_pending(ctx, rec, spec, decisions, types, refresh=refresh)]
            # Shown only when the file has no saved answer from this source.
            if not here:
                notes.not_asked[source] = "not available here"
            elif not any(spec.types & types for spec in here):
                notes.not_asked[source] = "doesn't apply to this file"
            elif not pending:
                notes.not_asked[source] = ALREADY_DECIDED
                if not gather_all:
                    answerable = frozenset(t for spec in here for t in spec.types & types)
                    skipped_decided[source] = frozenset(t for t in answerable if _decided_beyond_chapters(decisions[t]))
            if pending and kept_everywhere is None:
                kept_everywhere = _kept_by_every_destination(ctx, rec, servers, owners, types)
            if pending and kept_everywhere:
                reading = [
                    spec
                    for spec in pending
                    if _detector_pending(ctx, rec, spec, decisions, types - kept_everywhere, refresh=refresh)
                ]
                if len(reading) < len(pending):
                    kept_names = " and ".join(t.value for t in MarkerType if t in kept_everywhere)
                    logger.debug(
                        "Not reading {} at {}: every server keeps its own {}",
                        os.path.basename(path),
                        source.value,
                        kept_names,
                    )
                    notes.not_asked[source] = f"not read (every server keeps its own {kept_names})"
                pending = reading
            if not local and any(_needs_worker(ctx, rec, spec) for spec in pending):
                _note_title(ctx, notes, item, ids, servers, owners)  # the worker's pickup line names the file
                return None  # sources already refreshed stay marked; the worker refreshes the rest
            for spec in pending:
                fell_back: list[str] = []
                phases: list[str] = []
                on_gpu = gpu is not None
                device = worker_device(notes.worker) if on_gpu else ""
                _log_live(lambda s=source, g=on_gpu, d=device: titled(title, reading_line(s, on_gpu=g, device=d)))
                read_started = ctx.monotonic()
                try:
                    unanswered = _run_detector(
                        ctx,
                        rec,
                        spec,
                        gpu=gpu,
                        gpu_device_path=gpu_device_path,
                        phase=_noting(phases, phase),
                        cancel_check=cancel_check,
                        pause_check=pause_check,
                        ffmpeg_threads=ffmpeg_threads,
                        fallback_callback=_noting(fell_back, fallback_callback),
                        gpu_worker=gpu_worker,
                    )
                except Exception as exc:
                    # The worker reruns the file on the CPU and names the reason; what ffmpeg itself said on the GPU
                    # is lost with this run unless it is logged here.
                    said = gpu_failure_lines(exc)
                    if said:
                        _log_live(
                            lambda s=source, e=exc, lines=said: titled(title, gpu_failure_line(s, str(e), lines)),
                            "WARNING",
                        )
                    raise
                if unanswered is None:
                    for stored in spec.stored_sources:
                        notes.answered(stored)
                else:
                    notes.unanswered[spec.source] = _detector_unanswered(unanswered)
                seconds = ctx.monotonic() - read_started
                fallback = fell_back[0] if fell_back else ""
                gpu_read_nothing = CPU_RECHECK_PHASE in phases

                def _read_result_line(s=source, sec=seconds, fb=fallback, gn=gpu_read_nothing):
                    evidence_now = ctx.store.evidence_rows(rec.id)
                    own_rows = [r for r in evidence_now if r.source is s and r.origin == ""]
                    return titled(
                        title,
                        read_result_line(
                            s,
                            notes.unanswered.get(s),
                            own_rows,
                            chapter_names(evidence_now),
                            seconds=sec,
                            fallback=fb,
                            gpu_read_nothing=gn,
                        ),  # fmt: skip
                    )

                _log_live(_read_result_line)
            if pending:
                ctx.run_memo(path).clear()  # what the detectors' hooks read before they ran is out of date now
                # Its Reading/result lines already said this: a later stage's "already decided" render of the same
                # source (``_log_source`` below) doesn't repeat it.
                notes.logged_sources.add((source, ""))
        _mark_refreshed(ctx, path, source)
        if not gather_all:
            decisions = _decide(ctx, rec, types, intro_limit)
        hidden = source in EPISODE_ONLY_SOURCES and not notes.is_episode
        if source is Source.SERVER_MARKERS:
            for owner in owning:
                cfg = owner.config
                _log_source(
                    notes,
                    (Source.SERVER_MARKERS, cfg.id),
                    lambda i=cfg.id, n=cfg.name, d=decisions, t=types: titled(
                        title,
                        server_source_line(
                            i, n, make_source_view(ctx.store.evidence_rows(rec.id), notes, skipped, d, t), _UNUSED_SERVER_MARKERS
                        ),
                    ),
                )  # fmt: skip
        elif source is not Source.CHAPTERS and not hidden and not pending:
            _log_source(
                notes,
                (source, ""),
                lambda s=source, d=decisions, t=types: titled(
                    title, source_line(s, make_source_view(ctx.store.evidence_rows(rec.id), notes, skipped, d, t))
                ),
            )  # fmt: skip
        elif source is Source.SEASON_AUDIO and pending and not hidden:

            def _season_extra(d=decisions, t=types):
                view = make_source_view(ctx.store.evidence_rows(rec.id), notes, skipped, d, t)
                return last_seasons_audio_line(view) or None

            extra = _built_line(_season_extra)
            if extra:
                write_line(titled(title, extra))

    if cancelled():  # a detector stopped part way decides nothing: the file is left as it was for the next run
        return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
    decisions = _decide(ctx, rec, types, intro_limit)
    # A type that ends undecided while every server keeps its own and shows one is nothing for the user to review: the
    # servers' own markers stay whatever it would decide. That holds whether the file was skipped for it or an answer
    # stored earlier left it in review. A decided type stays decided (the publisher's own kept note names it).
    # Plex's marker made for an earlier file counts as the server's own here, though not before detection: the file was
    # read for its type, and with nothing of ours decided for it, Plex's marker is still the closest there is.
    undecided = frozenset(t for t in types if decisions[t].status is not DecisionStatus.DECIDED)
    kept_own = (
        undecided & _kept_by_every_destination(ctx, rec, servers, owners, undecided, stale_counts=True)
        if undecided
        else frozenset()
    )
    if kept_own:
        reason = kept_own_reason(owner.config.type.value.capitalize() for owner in owners)
        decisions = {
            **decisions,
            **{t: TypeDecision(t, DecisionStatus.DISABLED, None, None, reason) for t in kept_own},
        }
    decisions = _keep_published_before_rule_change(ctx, rec, decisions)
    decisions = _carry_over(ctx, rec, servers, owners, decisions)
    _log_live(lambda: titled(title, decided_line(decisions, types, ctx.store.evidence_rows(rec.id))))
    fingerprint = ctx.settings.detection_fingerprint()
    changed = _decisions_changed(ctx.store, rec.id, decisions, fingerprint)
    if changed:
        ctx.store.save_decisions(rec.id, decisions, settings_fingerprint=fingerprint)
        ctx.note_answer_changed()
    if ctx.store.version_rerun(rec.id, DECIDE_RULES) != DECIDE_RULES_VERSION:
        # Decided under today's rules: only a start after they change lists it to be decided again (``markers.versions``).
        ctx.store.record_version_reruns([(rec.canonical_path, DECIDE_RULES, DECIDE_RULES_VERSION)])
    if ctx.store.get_intro_chapter_limit(rec.id) != (True, intro_limit):
        ctx.store.set_intro_chapter_limit(rec.id, intro_limit)
    markers = ctx.store.get_markers(rec.id)
    in_review = review_message(decisions, types)
    if identity_changed(rec):
        raise _FileChangedError(path)
    replaced = ctx._replaced_at_start.get(path, existing is not None and not unchanged)
    rows = []
    # A worker holds a GPU or CPU worker previews need: when this job retries a write Plex's busy database refused, it
    # waits for that database only briefly (the checking stage, holding no worker, waits as long as a job may).
    brief_db_wait = local and ctx.may_promise_busy_retry(path)
    for owner in owners:
        # Per-server publish state already tolerates a partial fan-out; a busy Plex DB can hold a write for 120 s
        # (``plex_db.WORKER_BUSY_TIMEOUT_S`` with brief_db_wait).
        if cancelled():
            return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
        with cancellable_waits(cancel_check):  # a cancelled job stops waiting for a busy database
            row = _publish_to(
                owner,
                rec,
                markers,
                in_review,
                servers,
                ctx,
                phase,
                kept_own=kept_own,
                brief_db_wait=brief_db_wait,
                notes=notes,
            )
        if replaced and row["status"] in (ServerStatus.WRITTEN.value, ServerStatus.UP_TO_DATE.value):
            row[VERIFY_LATER] = True
        rows.append(row)
        # A bug describing what a server got mustn't stop the next owner's write: each row is already stored above.
        _log_live(lambda r=row: titled(title, server_result_line(_server_result(ctx, rec, r, decisions, notes))))
    waiting_to_retry = any(
        r["status"] == ServerStatus.WAITING.value and r.get("reason_code") in RETRY_REASON_CODES for r in rows
    )
    outcome = file_outcome({r["status"] for r in rows}, needs_review=bool(in_review), waiting_to_retry=waiting_to_retry)
    if outcome is not FileOutcome.FAILED:
        ctx.decided_by.add(decided_groups(decisions))
    try:
        _log_file(ctx, rec, decisions, types, rows, notes, changed=changed, outcome=outcome, title=title)
    except Exception as exc:
        # The file is done: a problem describing it mustn't fail it.
        logger.warning("Couldn't write the job log lines for {}: {}", path, type(exc).__name__)
    if is_budget_exhausted(skipped.get(Source.THEINTRODB, "")) and any(
        decisions[t].status in _UNDECIDED or is_carried_over(decisions[t].marker) for t in types
    ):
        with ctx._budget_lock:
            ctx._budget_rechecks.add(path)
            ctx._budget_refused_at = ctx._budget_refused_at or ctx.now()
    labels = tuple(sorted(_ONLINE_LABELS[source] for source, answer in skipped.items() if is_budget_exhausted(answer)))
    return ItemOutcome(outcome.value, _summary(decisions, types, labels), rows)


# Plain words for a server's stored markers answer that says they weren't used.
_UNUSED_SERVER_MARKERS = {
    UNUSABLE_SERVER_MARKERS_DETAIL: "couldn't be used (unreadable, another cut, or its library hides a type in Plex)",
    PLUGINS_UNKNOWN_DETAIL: "not used (couldn't read its plugins)",
    OURS_SHOWN_DETAIL: "not used (read by an older version; it shows our markers now)",
    OURS_ON_ITEM_DETAIL: "not used (read by an older version; its item may show our markers now)",
}


def _unstored_lookup(source: Source, result: LookupResult | None) -> str:
    """What the job log says about an online lookup whose answer wasn't stored (the next run asks again)."""
    if result is None:
        return "lookup failed"
    detail = result.detail.removeprefix(f"{_ONLINE_LABELS[source]} ")
    if result.status == "not_applicable":
        return f"not asked ({detail})" if detail else "not asked"
    return f"unavailable ({detail})" if detail else "unavailable"


def _prior_markers(notes: RunNotes | None, evidence: list, server_id: str) -> tuple[tuple[Marker, ...] | None, bool]:
    """What a server held before this run's write, and whether that's ours to call a removal if a type is missing.

    Returns:
        ``(markers, is_ours)``: our own last-published record (``notes.sent_before``) when we've published there
        before -- ``is_ours`` True, so a type missing from it now really was removed; else the server's own markers
        as its evidence (read this run or saved) says -- ``is_ours`` False, since a type only there was never ours
        to send in the first place, so never ours to call removed either; else ``(None, False)`` when neither is
        known.
    """
    sent_before = notes.sent_before.get(server_id) if notes is not None else None
    if sent_before is not None:
        return sent_before, True
    own = [r for r in evidence if r.source in SERVER_SOURCES and r.origin == server_id]
    if not own:
        return None, False
    markers = tuple(
        Marker(r.type, r.start_ms, r.end_ms, ()) for r in own if r.type is not None and r.start_ms is not None
    )
    return markers, False


def _server_result(
    ctx: PipelineContext,
    rec: FileRecord,
    row: dict,
    decisions: dict[MarkerType, TypeDecision],
    notes: RunNotes | None = None,
) -> ServerResult:
    """A server's row with our markers it has now, the types it keeps as its own, and what it held before this run's
    write (``notes``: None when nothing logs this run)."""
    ours: tuple[Marker, ...] = ()
    shown = (ServerStatus.WRITTEN.value, ServerStatus.UP_TO_DATE.value, ServerStatus.WAITING.value)
    state = ctx.store.get_publish_state(rec.id, row["server_id"]) if row["status"] in shown else None
    if state is not None:
        ours = tuple(state.markers)
    # From _publish_to's own result, read under the same lock its write already took (see _ITEM_LOCKS there) --
    # never taken again here just to log, which would otherwise queue behind another version's write to the same
    # item with no bound.
    item_kept: frozenset[MarkerType] = row.get("kept_types") or frozenset()
    kept = kept_types(decisions, item_kept)
    withheld = frozenset(t for t in kept if decisions[t].status is DecisionStatus.DECIDED)
    had, had_is_ours = (
        _prior_markers(notes, ctx.store.evidence_rows(rec.id), row["server_id"])
        if row["status"] == (ServerStatus.WRITTEN.value)
        else (None, False)
    )
    return ServerResult(row, ours, kept, withheld, had, had_is_ours)


def _found_online(ctx: PipelineContext, rec: FileRecord, notes: RunNotes) -> bool:
    """Whether an online source this run asked stored an entry for the file.

    A run that isn't forced asks a source only when it has no stored answer, a "no entry" that is due, or one from an
    older parser (``_needs_lookup``), so this is a database that newly has the file, bar the rare parser upgrade.

    Args:
        ctx: The job's context.
        rec: The file.
        notes: What this run did with each source.

    Returns:
        True when one did.
    """
    asked = {source for source, origin in notes.asked if source in _ONLINE_LABELS and origin == ""}
    return any(
        row.source in asked and row.origin == "" and row.type is not None for row in ctx.store.evidence_rows(rec.id)
    )


def _listed_title(item: ProcessableItem) -> str:
    """The title a server's library listing gave the item; "" for a file a sender or the store named (its title is its
    file name) or a listing that had no title (its path, or "<unknown>")."""
    title = (item.title or "").strip()
    if not item.server_id or not title or title.startswith("<") or "/" in title or "\\" in title:
        return ""
    return "" if title == os.path.basename(item.canonical_path) else title


def _claim_title(ctx: PipelineContext, title: str, path: str) -> str:
    """``title``, with a short tell-apart appended once another file of this job already has it (two copies of the
    same film, say): the file's own resolution or cut tag when its name gives one -- data already at hand, never a
    lookup -- else the plain title, same as the file it collides with.
    """
    with ctx._titles_guard:
        seen = ctx._titles_seen.get(title, 0)
        ctx._titles_seen[title] = seen + 1
    if seen == 0:
        return title
    tag = version_tag(path)
    return f"{title} ({tag})" if tag else title


def _note_title(
    ctx: PipelineContext,
    notes: RunNotes,
    item: ProcessableItem,
    ids: MediaIds,
    servers: _ItemServers,
    owners: list[_Owning],
) -> str:
    """The file's title for its job log lines, found once per job (``job_log.file_title``). Never raises.

    An episode is named from its path, with no lookup; anything else by its server's title: from the answer this
    run already has, then the title this process kept (``titles.TITLE_CACHE``), then the title its library listing
    gave (the year from its file name); only when the file's server was never asked this process, one ask of a
    publishing owner whose item id this run already knows (``title_asks``: never a search for one). Without a
    title, its file name. A second file of this job that resolves to the same title -- two copies of one film, or
    two versions of one episode (``SxxEyy`` alone doesn't tell them apart) -- gets a short tell-apart
    (``_claim_title``). Called once the run's decisions are made (its log lines, or its hand-off to a worker),
    outside any publisher's or Plex database's lock.
    """
    if notes.title:
        return notes.title
    path = item.canonical_path
    if ids.is_episode or ids_from_path(path).is_episode:
        notes.title = _claim_title(ctx, display_name(path), path)
        return notes.title
    try:
        answer = servers.answered_title()
        if answer is not None:
            TITLE_CACHE.put(path, answer)  # the run's own ask counts as the file's one lookup
        found = answer if answer is not None and answer[0] else TITLE_CACHE.get(path)
        if found is not None and found[0]:
            title = file_title(path, *found)
        elif listed := _listed_title(item):
            title = file_title(path, listed, path_year(path))
        elif found is None:
            title = file_title(path, *look_up_title(path, servers.title_asks(owners)))
        else:
            title = display_name(path)
    except Exception as exc:
        logger.debug("Couldn't name {} for the job log: {}", path, type(exc).__name__)
        title = display_name(path)
    notes.title = _claim_title(ctx, title, path)
    return notes.title


def _noting(noted: list[str], callback: Callable[[str], None] | None) -> Callable[[str], None] | None:
    """A detector's ``fallback_callback`` or ``phase_callback`` that also keeps what it was told (why a step fell back
    to the CPU, the steps it went through), for the detector's job log line."""
    if callback is None:
        return None

    def told(text: str) -> None:
        noted.append(text)
        callback(text)

    return told


def _detector_unanswered(reason: str) -> str:
    """How a detector's line says it had no answer: a file found cut short says so plainly (it isn't read again until it
    changes); anything else had no answer this time."""
    return reason if reason.startswith(CUT_SHORT) else f"no answer this time ({reason})"


def _start_stage(notes: RunNotes, started: float, *, worker: str, cpu_rerun: bool) -> None:
    """Note which stage runs the file and when it started; a stage run again on the same worker (its CPU rerun, a retry
    after the file changed) keeps its first start."""
    if notes.started is None or notes.worker != worker:
        notes.started = started
    notes.worker = worker
    notes.cpu_rerun = cpu_rerun


def _stage_seconds(ctx: PipelineContext, notes: RunNotes) -> float | None:
    return None if notes.started is None else max(0.0, ctx.monotonic() - notes.started)


def _outcome_status(outcome: FileOutcome) -> str:
    if outcome is FileOutcome.FAILED:
        return "failed"
    if outcome is FileOutcome.SKIPPED:
        return "skipped"
    return "success"


def _outcome_reason(rows: list[dict], status: str) -> str:
    """Why a worker's "completed" line ends "failed" or "skipped": the matching server row's own message, if one."""
    if status == "success":
        return ""
    matching = ServerStatus.FAILED.value if status == "failed" else ServerStatus.SKIPPED.value
    for row in rows:
        if row.get("status") == matching and row.get("message"):
            return str(row["message"])
    return ""


def _log_file(
    ctx: PipelineContext,
    rec: FileRecord,
    decisions: dict[MarkerType, TypeDecision],
    types: frozenset[MarkerType],
    rows: list[dict],
    notes: RunNotes,
    *,
    changed: bool,
    outcome: FileOutcome,
    title: str,
) -> None:
    """Log the file's last line(s): a Season job's (or a TheIntroDB recheck's, a decide-again job's, the weekly online
    re-check's) own count of this file, and the worker's "completed" line or the checking thread's "done" line. Every
    other line -- each source's, "Decided" and each server's -- was already logged live as that work happened.

    Every file gets its "completed"/"done" line, whatever it did: an unchanged file, a Season job's unchanged episode,
    a decide-again job's unchanged file and a weekly online re-check's file with nothing new all log it too. A failed
    file is always logged, at WARNING.
    """
    season = decided_again = rechecked_online = None
    if ctx.season_recheck and (rec.season_key is not None or ctx.recheck_label == SEASON_RECHECK_LABEL):
        name, episode = season_of(rec.canonical_path)
        season = (name, SeasonEpisode(episode, changed, review_note(decisions, types)))
    if ctx.decide_again:
        in_review = any(decisions[t].status is DecisionStatus.NEEDS_REVIEW for t in types if t in decisions)
        decided_again = (changed, in_review)
    if ctx.online_recheck:
        rechecked_online = (_found_online(ctx, rec, notes), changed)
    ctx._note_finished(rows, season, decided_again, rechecked_online)
    failed = outcome is FileOutcome.FAILED
    seconds = _stage_seconds(ctx, notes)
    level = "WARNING" if failed else "INFO"
    if notes.worker:
        status = _outcome_status(outcome)
        reason = _outcome_reason(rows, status)
        write_line(
            worker_completed_line(
                notes.worker, title, seconds, status=status, reason=reason, cpu_rerun=notes.cpu_rerun
            ),
            level,
        )
    else:
        write_line(titled(title, done_line(seconds, failed=failed, nothing_sent=nothing_was_sent(rows))), level)
    notes.logged = True


def _log_failure(ctx: PipelineContext, path: str, notes: RunNotes, message: str) -> None:
    """Log a file that failed before its "Decided" line and its "completed"/"done" line were written (it couldn't be
    read, or kept changing while it was analysed): its start line too, if nothing logged it yet. Never raises: the
    file already has its outcome."""
    try:
        title = notes.title or display_name(path)
        path_ids = ids_from_path(path)
        types = notes.types if notes.types is not None else _enabled_types(ctx.settings, path_ids)
        is_episode = notes.is_episode if notes.is_episode is not None else path_ids.is_episode
        if not notes.start_logged:
            notes.start_logged = True
            write_line(file_start_line(title, types, is_episode=is_episode, worker=notes.worker))
        write_line(titled(title, f"Failed: {message}"), "WARNING")
        seconds = _stage_seconds(ctx, notes)
        if notes.worker:
            write_line(
                worker_completed_line(
                    notes.worker, title, seconds, status="failed", reason=message, cpu_rerun=notes.cpu_rerun
                ),
                "WARNING",
            )
        else:
            write_line(titled(title, done_line(seconds, failed=True)), "WARNING")
        notes.logged = True
    except Exception as exc:
        logger.warning("Couldn't write the job log lines for {}: {}", path, type(exc).__name__)


def _log_gone(path: str, notes: RunNotes, message: str) -> None:
    """Log the one line of a file a newer file replaced (``_not_on_disk``). Never raises."""
    try:
        write_line(f"{notes.title or display_name(path)}: {message}")
        notes.logged = True
    except Exception as exc:
        logger.warning("Couldn't write the job log line for {}: {}", path, type(exc).__name__)


def log_pickup(item: ProcessableItem, worker: str, *, ctx: PipelineContext) -> None:
    """Log that a worker started a file (``KindHandlers.pickup_fn``): its title and what it's checked for, as the
    checking stage that ran before it found them (from its path, checking nothing yet known, when no checking stage
    ran it first).

    Args:
        item: The file.
        worker: The worker's display name.
        ctx: The job's context.
    """
    path = item.canonical_path
    notes = ctx._run_notes.get(path)
    title = (notes.title if notes is not None else "") or display_name(path)
    path_ids = ids_from_path(path)
    is_episode = notes.is_episode if notes is not None and notes.is_episode is not None else path_ids.is_episode
    # No checking stage ran first to work out the file's real ids (a server's own kind, an online id lookup): its
    # settings alone still say what it's checked for, rather than wrongly saying every type it could have is off.
    types = notes.types if notes is not None and notes.types is not None else _enabled_types(ctx.settings, path_ids)
    if notes is not None:
        notes.start_logged = True
    write_line(file_start_line(title, types, is_episode=is_episode, worker=worker))


def _run(
    item: ProcessableItem,
    ctx: PipelineContext,
    *,
    local: bool,
    gpu: str | None = None,
    gpu_device_path: str | None = None,
    phase_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
    job_paused: Callable[[], bool] | None = None,
    ffmpeg_threads: int | None = None,
    fallback_callback: Callable[[str], None] | None = None,
    gpu_worker: bool = False,
    worker_name: str | None = None,
) -> ItemOutcome | None:
    """Run one stage of a file under its run lock (``check_item``, ``process_item``).

    ``pause_check`` goes to the detectors (a worker's ``ctx.freeze_check``); ``job_paused`` is the dispatcher's pause
    for the job, which only decides whether a worker waiting for another job's run of the file gives it back.
    ``worker_name`` names the worker in the file's job log lines.
    """
    started = ctx.monotonic()
    if cancel_check and cancel_check():
        return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
    if is_extra(item.canonical_path):
        # No source describes a trailer or featurette and no server lists one as an item, so checking it would only
        # wait (and queue retries) for an item that never comes. Folder jobs and library listings both contain them.
        return ItemOutcome(FileOutcome.SKIPPED.value, EXTRAS_NOT_CHECKED)
    path = item.canonical_path
    # A worker waiting for another job's run of the file gives it back with a retry (taken as it gives it back) after
    # WORKER_FILE_WAIT_S, or at once while its own job is paused or frozen or the holder is frozen; with no retry left
    # it waits on, for the next run, until the holder is frozen or WORKER_FILE_WAIT_NO_RETRY_S. A cancel ends any wait.
    retried = False

    def stop_waiting(waited_s: float) -> bool:
        nonlocal retried
        if cancel_check and cancel_check():
            return True
        if not local:
            return False  # the check stage never waits (its ``wait_s`` is 0) and never takes a retry promise
        holder_frozen = _holder_frozen(path)
        paused = bool(job_paused and job_paused()) or bool(pause_check and pause_check())
        if (waited_s >= WORKER_FILE_WAIT_S or paused or holder_frozen) and ctx.promise_busy_retry(path):
            retried = True
            return True
        return holder_frozen or waited_s >= WORKER_FILE_WAIT_NO_RETRY_S

    notes: RunNotes | None = None
    for _ in range(MAX_ATTEMPTS):
        try:
            wait_s = None if local else 0.0
            # The check stage is never frozen (``check_item``), so only a worker's run can hold a file frozen.
            frozen = ctx.freeze_check if local else None
            with _file_run_lock(path, wait_s=wait_s, stop=stop_waiting, frozen=frozen) as held:
                if not held:
                    if not local:
                        logger.debug("{} is being run by another job; its worker stage runs it", path)
                        return None
                    _forget_run(ctx, path)
                    if cancel_check and cancel_check():
                        if retried:
                            ctx.release_busy_retry(path)
                        return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
                    given_back = _run_by_another_job(item, ctx, retried=retried)
                    if retried and given_back.outcome_key == FileOutcome.NO_OWNERS.value:
                        ctx.release_busy_retry(path)  # nothing to publish for this job: no retry follows it
                    return given_back
                with ctx._running(path):
                    skipped = ctx._pending_skips.pop(path, {})
                    notes = ctx._run_notes.pop(path, None) or RunNotes()
                    _start_stage(
                        notes,
                        started,
                        worker=(worker_name or "a worker") if local else "",
                        cpu_rerun=local and gpu_worker and gpu is None,
                    )
                    try:
                        outcome = _attempt(
                            item,
                            ctx,
                            local=local,
                            gpu=gpu,
                            gpu_device_path=gpu_device_path,
                            phase=phase_callback or _no_phase,
                            cancel_check=cancel_check,
                            pause_check=pause_check,
                            skipped=skipped,
                            notes=notes,
                            ffmpeg_threads=ffmpeg_threads,
                            fallback_callback=fallback_callback,
                            gpu_worker=gpu_worker,
                        )
                    except BaseException:
                        # A rerun goes on from here: the retry below after the file changed, or the worker's CPU rerun
                        # after a GPU error; a forced run doesn't ask the sources it already asked again.
                        if skipped:
                            ctx._pending_skips[path] = skipped
                        ctx._run_notes[path] = notes
                        raise
                    if outcome is None:  # handed to a worker
                        if skipped:
                            ctx._pending_skips[path] = skipped
                        ctx._run_notes[path] = notes
                        return None
                    ctx._refreshed.pop(path, None)
                    ctx._answers_before.pop(path, None)
                    ctx._replaced_at_start.pop(path, None)
                    _count_skipped(ctx, skipped)
                    if (
                        not notes.logged
                        and outcome.outcome_key == FileOutcome.FAILED.value
                        and outcome.message != _CANCELLED
                    ):
                        _log_failure(ctx, path, notes, outcome.message)
                    elif not notes.logged and outcome.outcome_key == FileOutcome.SOURCE_GONE.value:
                        _log_gone(path, notes, outcome.message)
                    return outcome
        except _FileChangedError:
            logger.info("{} changed while its markers were detected; detecting again", path)
            ctx._answers_before.pop(path, None)  # what the new file had stored is read again
            ctx._replaced_at_start.pop(path, None)
    _forget_run(ctx, path)
    message = "The file kept changing while it was analysed; it will be tried again on the next run"
    _log_failure(ctx, path, notes or RunNotes(), message)
    return ItemOutcome(FileOutcome.FAILED.value, message)


def check_item(
    item: ProcessableItem, *, ctx: PipelineContext, cancel_check: Callable[[], bool] | None = None
) -> ItemOutcome | None:
    """Check stage: decide from cheap sources and publish.

    Args:
        item: The file.
        ctx: The job's context.
        cancel_check: True once the job is cancelled.

    Returns:
        The item's outcome, or None when a local detector that needs a worker has to run, or another job is running the
        file (send it to a worker, which waits for that run). A detector that doesn't need one runs here, unless
        another detector that has to run at the same source needs a worker.
    """
    # Never frozen here: the detectors that run on a checking thread are the cheap ones (a season step from cached
    # fingerprints), and a job frozen by its schedule's stop time would otherwise keep checking threads other jobs need.
    return _run(item, ctx, local=False, cancel_check=cancel_check)


def process_item(
    item: ProcessableItem,
    *,
    ctx: PipelineContext,
    gpu: str | None = None,
    gpu_device_path: str | None = None,
    progress_callback: Callable[..., None] | None = None,
    phase_callback: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
    ffmpeg_threads: int | None = None,
    fallback_callback: Callable[[str], None] | None = None,
    gpu_worker: bool = False,
    worker_name: str | None = None,
) -> ItemOutcome:
    """Worker stage: the same steps plus local detectors on the worker's GPU/CPU.

    A job paused on its own doesn't block here: the running file finishes (holding a worker for it would keep previews
    from it). Everything paused, or the job's schedule's stop time, freezes the detectors' running ffmpeg where it is,
    as it freezes previews' (``ctx.freeze_check``). A file another job is running is waited for in slices a cancel
    ends. It is given back for the job's retry (``FILE_BUSY``, the promise taken then) after ``WORKER_FILE_WAIT_S``, or
    at once while this job is paused or frozen or the holder is frozen. With no retry to promise it is given back for
    the next run at once when the holder is frozen, else after ``WORKER_FILE_WAIT_NO_RETRY_S``.

    Args:
        item: The file.
        ctx: The job's context.
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.
        progress_callback: Unused until detectors report progress.
        phase_callback: Shows the current step on the worker row.
        cancel_check: True once the job is cancelled.
        pause_check: The dispatcher's pause for the job (its own pause included). Only a wait for another job's run of
            the file uses it (``WORKER_FILE_WAIT_S``); the detectors get ``ctx.freeze_check`` instead.
        ffmpeg_threads: The GPU worker's own ``ffmpeg_threads`` (its GPU's ``gpu_config`` entry); None on a CPU worker
            and for a GPU worker's CPU rerun, which leave ffmpeg its own thread count, as previews do.
        fallback_callback: Shows on the worker's row that a step fell back from the GPU to the CPU inside a detector.
        gpu_worker: Whether a GPU worker runs the file, its CPU rerun included (``gpu`` None): CPU text detection it
            asks for then is its own, not one of the CPU workers' (``TextDetectorPool.detect_boxes``).
        worker_name: The worker's display name, for the file's job log lines.

    Returns:
        The item's outcome.
    """
    return _run(
        item,
        ctx,
        local=True,
        gpu=gpu,
        gpu_device_path=gpu_device_path,
        phase_callback=phase_callback,
        cancel_check=cancel_check,
        pause_check=ctx.freeze_check,
        job_paused=pause_check,
        ffmpeg_threads=ffmpeg_threads,
        fallback_callback=fallback_callback,
        gpu_worker=gpu_worker,
        worker_name=worker_name,
    )


def kind_handlers(ctx: PipelineContext) -> KindHandlers:
    """Dispatcher handlers bound to one job's context.

    Args:
        ctx: The job's context.

    Returns:
        Handlers for ``submit_items(kind=intro_credits)``.
    """
    return KindHandlers(
        check_fn=lambda item, *, cancel_check=None: check_item(item, ctx=ctx, cancel_check=cancel_check),
        process_fn=lambda item, **kwargs: process_item(item, ctx=ctx, **kwargs),
        pickup_fn=lambda item, worker: log_pickup(item, worker, ctx=ctx),
        outcome_keys=OUTCOME_KEYS,
        check_label="Looking up markers…",
        check_worker_label="Intro & Credits",
        check_share=0.25,
    )
