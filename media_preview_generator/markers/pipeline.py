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

import os
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from ..config.paths import is_path_excluded
from ..job_kinds import ItemOutcome, KindHandlers
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import OwnershipMatch
from ..servers.registry import server_config_from_dict
from ..web.settings_manager import get_settings_manager
from .audio.fingerprint import ChromaprintState, chromaprint_state
from .audio.season import season_audio_spec, season_intro_chapter_limits
from .credits.detector import credits_text_spec
from .credits.textdet_helper import TextDetState, text_detection_state
from .decide import (
    DecisionContext,
    DecisionStatus,
    TypeDecision,
    decide,
)
from .external_ids import ids_from_path, ids_from_server_dict, is_extra, merge_ids
from .locks import KeyedLocks as _KeyedLocks
from .models import SERVER_SOURCES, Candidate, FileIdentity, Marker, MarkerType, MediaIds, Source
from .outcomes import (
    EXTRAS_NOT_CHECKED,
    NOT_IN_LIBRARY,
    OUTCOME_KEYS,
    PLEX_PASS_UNKNOWN,
    READ_BACK_FAILED,
    STATE_BY_STATUS,
    VERIFY_LATER,
    FileOutcome,
    ServerStatus,
    file_outcome,
    kept_note,
    with_kept_note,
)
from .ownership import marker_matches, owning_servers
from .probe import ProbeError, ffprobe_path_for, probe_media
from .publishers.base import (
    Capability,
    CapabilityReport,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
    Shown,
)
from .publishers.factory import publisher_for
from .settings import GlobalMarkersSettings, ServerMarkersSettings, get_global_settings, load_server
from .sources import introdb, skipdb, theintrodb
from .sources.chapters import CHAPTER_RULES_VERSION, chapter_candidates
from .sources.introdb import IntroDbClient
from .sources.online import LookupResult, is_budget_exhausted
from .sources.ratelimit import PRIORITY_LOW, RESET_TIME_LABEL
from .sources.server_markers import READER_VERSION, imported_detail, importer_plugin, read_server_markers
from .sources.skipdb import SkipDbClient
from .sources.theintrodb import TheIntroDbClient
from .store import EvidenceRow, FileRecord, ItemPublishStateRow, MarkerStore, get_marker_store

NO_DATA_RETRY = timedelta(days=14)
# Servers detect their own markers on a schedule (Plex overnight), so "none there" is asked again a day later.
EMPTY_SERVER_MARKERS_RETRY = timedelta(days=1)
# Check servers asks a server again for a decided file it had no markers for once the answer is this old, a step further
# after each re-read that stays empty, and never after the last step (a server without detection of its own is asked 5
# times in a month).
RECHECK_AFTER = tuple(timedelta(days=days) for days in (1, 2, 4, 8, 16))
# A file replaced while it is analysed is detected again from scratch; one that keeps changing is being written.
MAX_ATTEMPTS = 3
_ONLINE_LABELS = {Source.THEINTRODB: "TheIntroDB", Source.INTRODB: "IntroDB", Source.SKIPDB: "SkipDB"}
PARSER_VERSIONS = {
    Source.THEINTRODB: theintrodb.PARSER_VERSION,
    Source.INTRODB: introdb.PARSER_VERSION,
    Source.SKIPDB: skipdb.PARSER_VERSION,
}
_STORED_LOOKUPS = ("ok", "no_data")
_CHAPTERS_AND_SERVERS = frozenset({Source.CHAPTERS.value, *(s.value for s in SERVER_SOURCES)})
# Plex and Emby can't tell our markers from their own, and Plex shows one marker set per item across its versions, so
# their markers are never read back from an item we published to. Jellyfin's reader leaves ours out itself.
_ITEM_WIDE_MARKERS = frozenset({ServerType.PLEX, ServerType.EMBY})
# Servers where a plugin can import a crowd skip database into the server's own markers; Plex detects its own.
_IMPORTER_PLUGIN_SERVERS = frozenset({ServerType.JELLYFIN, ServerType.EMBY})
PLUGINS_UNKNOWN_DETAIL = "Couldn't read this server's plugins, so its markers aren't used"
UNUSABLE_SERVER_MARKERS_DETAIL = "Couldn't read this server's markers, or they may describe another cut"
_CANCELLED = "cancelled by user"
# A ready Plex whose Plex Pass didn't answer (usually restarting): files within this long share the answer instead of
# each running the whole check (lock probe, Plex's HTTP connect with its retries, schema and library scans).
PLEX_PASS_UNKNOWN_TTL_S = 5.0
# Answers read from the saved settings alone: cheap, and wrong the moment the user saves, so never reused.
_SETTINGS_ANSWERS = frozenset({Capability.DISABLED, Capability.NEEDS_CONFIRMATION})

LocalDetector = Callable[..., "list[Candidate] | DetectorAnswer"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _no_phase(_text: str) -> None:
    return None


class DetectorUnavailableError(Exception):
    """A local detector couldn't answer this time (its tool failed, the job was cancelled). Nothing is stored for it,
    so the next run asks it again."""


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
        detect: ``detect(file, *, ctx, gpu, gpu_device_path, phase_callback, cancel_check, pause_check)``: a list of
            candidates, or a ``DetectorAnswer`` whose signature is stored as the answer's basis (``detector_runs``);
            raises ``DetectorUnavailableError`` when it can't answer this time.
        stores: Sources its candidates are stored under, each candidate under its own ``source``; empty = ``source``.
        version: Stored with its answer; an answer from another version is asked again, even for decided types.
        due: ``due(file, ctx)``: whether a stored answer of this version is out of date anyway (None: never).
        needs_worker: ``needs_worker(file, ctx)``: whether it needs a GPU/CPU worker now (None: always). One that
            doesn't runs on the checking thread, unless another detector that has to run at the same source needs a
            worker: then they all run on the worker.
        followups: ``followups(file, ctx)``: other files whose answer is out of date and whose decision could change
            with it (season audio: siblings matched before this episode arrived). Every run of a file of a type the
            detector decides asks the job to run them again, before any worker handoff (None: none).
    """

    source: Source
    types: frozenset[MarkerType]
    detect: LocalDetector
    stores: frozenset[Source] = frozenset()
    version: int = 1
    due: Callable[[FileRecord, PipelineContext], bool] | None = None
    needs_worker: Callable[[FileRecord, PipelineContext], bool] | None = None
    followups: Callable[[FileRecord, PipelineContext], Iterable[str]] | None = None

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
    _capabilities: dict[str, tuple[float, CapabilityReport]] = field(default_factory=dict, repr=False)
    _capability_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _capability_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
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
    # Sources whose running out this job already logged.
    _budget_warned: set[Source] = field(default_factory=set, repr=False)
    _budget_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Per file being run: the thread running it and what that run computes once (``run_memo``).
    _run_memos: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict, repr=False)
    _run_memos_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

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

    @contextmanager
    def _running(self, canonical_path: str) -> Iterator[None]:
        with self._run_memos_guard:
            self._run_memos[canonical_path] = (threading.get_ident(), {})
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

    def note_changed_sibling_left_out(self, canonical_path: str) -> None:
        """Remember that this file's season audio answer left out a sibling that had changed on disk.

        Args:
            canonical_path: Local path of the file whose answer it was.
        """
        with self._followups_guard:
            self._left_out_changed.add(canonical_path)

    def take_changed_siblings_left_out(self) -> list[str]:
        """The files noted by ``note_changed_sibling_left_out``, sorted, and forget them.

        Returns:
            Every path noted since the last call, sorted.
        """
        with self._followups_guard:
            taken = sorted(self._left_out_changed)
            self._left_out_changed.clear()
        return taken


@dataclass(frozen=True)
class _Owning:
    """An enabled server with at least one library holding the file (Intro & Credits may be off there)."""

    server: Any
    config: ServerConfig
    matches: tuple[OwnershipMatch, ...]


# Two jobs on the same file (a backfill and a webhook after a replacement) take turns, so an answer gathered for the
# old file can never be written over the new file's.
_PATH_LOCKS = _KeyedLocks()
# Versions of one Plex item run on different threads under different path locks. Each publish reads what is ours on
# the item, writes, and records the result; another version's publish in between would leave that record wrong and
# our markers on the item untracked.
_ITEM_LOCKS = _KeyedLocks()


class _FileChangedError(Exception):
    """The file on disk no longer has the identity that was analysed."""


class _ItemServers:
    """The owning servers of one file, asked for item ids and external ids at most once each."""

    def __init__(self, item: ProcessableItem, owning: list[_Owning]) -> None:
        self._path = item.canonical_path
        self._hints = item.item_id_by_server or {}
        self.owning = owning
        self._item_ids: dict[str, str | None] = {}
        self._asked_ids = False
        self._server_ids: MediaIds | None = None

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
                    break
        return self._server_ids


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


def build_context(
    *,
    registry: Any,
    config: Any,
    priority: int | Callable[[], int],
    force: bool = False,
    recheck_empty_server_markers: bool = False,
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

    Returns:
        A context for one job.
    """
    settings = get_global_settings()
    ffmpeg_path = getattr(config, "ffmpeg_path", None)
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
    if rec is None or not store.get_decisions(rec.id) or _identity_changed(rec):
        return None
    return store.get_markers(rec.id)


def _owning_servers(item: ProcessableItem, ctx: PipelineContext) -> list[_Owning]:
    return [
        _Owning(server, cfg, tuple(matches))
        for cfg, server, matches in owning_servers(item.canonical_path, ctx.registry)
    ]


def _marker_owners(owning: list[_Owning], canonical_path: str) -> list[_Owning]:
    keep = marker_matches(canonical_path, [owner.config for owner in owning])
    return [owner for owner in owning if owner.config.id in keep]


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


# Sources without a switch of their own, each ranked right after the source whose switch they ride on.
_RIDERS = {
    Source.SERVER_MARKERS.value: Source.SERVER_MARKERS_IMPORTED.value,
    Source.SEASON_AUDIO.value: Source.SEASON_AUDIO_PREVIOUS.value,
}
# Sources whose answers only a local detector makes (the previous-season hint included).
_LOCAL_DETECTOR_SOURCES = frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS, Source.CREDITS_TEXT})


def _decision_order(settings: GlobalMarkersSettings) -> tuple[str, ...]:
    """Enabled sources in the user's order; importer-plugin copies ride on the server-markers switch and the
    previous-season hint on the season-audio switch, each ranked right after its switch."""
    order: list[str] = []
    for source_id in settings.ordered_enabled_sources():
        order.append(source_id)
        if source_id in _RIDERS:
            order.append(_RIDERS[source_id])
    return tuple(order)


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
    # A lock always wins (spec §5.5 rule 1); what respect_locks=False should change is for the phase 4 editor.
    locked = ctx.store.get_locked(rec.id)

    def decide_from(sources: tuple[str, ...]) -> dict[MarkerType, TypeDecision]:
        enabled = set(sources)
        dctx = DecisionContext(
            rec.duration_ms or 0, rec.is_movie, ctx.settings.publish_when, types, sources, intro_chapter_limit
        )
        return decide([c for c in evidence if c.source.value in enabled], dctx, locked)

    decisions = decide_from(order)
    registered = {source.value for spec in ctx.local_detectors for source in (spec.source, *spec.stored_sources)}
    unavailable = {source.value for source in _LOCAL_DETECTOR_SOURCES} - registered
    if ctx.chromaprint is ChromaprintState.UNKNOWN:
        unavailable -= {Source.SEASON_AUDIO.value, Source.SEASON_AUDIO_PREVIOUS.value}
    if ctx.credits_text is TextDetState.UNKNOWN:
        unavailable -= {Source.CREDITS_TEXT.value}
    if not any(c.source.value in unavailable and c.source.value in order for c in evidence):
        return decisions
    without = decide_from(tuple(source_id for source_id in order if source_id not in unavailable))
    return {
        mtype: without[mtype] if decision.status is DecisionStatus.DECIDED else decision
        for mtype, decision in decisions.items()
    }


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


def _decided_beyond_chapters(decision: TypeDecision) -> bool:
    """Decided, and not by chapters alone: two agreeing sources may still veto a chapter (spec §5.5 rule 3).

    A chapter that markers already on servers shortened or confirmed is still chapters alone: server markers never
    decide on their own (rule 7).
    """
    if decision.status is not DecisionStatus.DECIDED:
        return False
    return not set(decision.marker.decided_by) <= _CHAPTERS_AND_SERVERS


def _all_decided(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> bool:
    return all(_decided_beyond_chapters(decisions[t]) for t in types)


def _only_confirming_chapters(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> bool:
    """Every type is decided and some only by chapters, so the search goes on only to let sources contradict them."""
    return all(decisions[t].status is DecisionStatus.DECIDED for t in types) and not _all_decided(decisions, types)


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
        and ctx.store.evidence_version(rec.id, source) != spec.version
        for source in spec.stored_sources
    )


def _detector_due(ctx: PipelineContext, rec: FileRecord, spec: LocalDetectorSpec) -> bool:
    """Whether a detector's stored answer is missing, from another version, or out of date by its own ``due``."""
    if any(ctx.store.evidence_version(rec.id, source) != spec.version for source in spec.stored_sources):
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
    answer. A type decided by other sources doesn't ask whether the answer is due.
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
    )
    return asks and _detector_due(ctx, rec, spec)


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
) -> None:
    """Run one detector and store its answer under each of its sources with its version, and its basis when it gave one,
    in one transaction.

    Anything but ``DetectorUnavailableError`` propagates, so a GPU error reaches the worker's CPU fallback.
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
        )
        found = list(answer.candidates if isinstance(answer, DetectorAnswer) else answer)
    except DetectorUnavailableError as exc:
        logger.info(
            "{} had no answer for {} this time: {}", spec.source.value, os.path.basename(rec.canonical_path), exc
        )
        return
    stray = [c for c in found if c.source not in spec.stored_sources]
    if stray:
        logger.warning("{} returned candidates for sources it doesn't store: {}", spec.source.value, stray)
    ctx.store.replace_detector_answer(
        rec.id,
        {source: [c for c in found if c.source is source] for source in spec.stored_sources},
        version=spec.version,
        run=(spec.source, answer.signature) if isinstance(answer, DetectorAnswer) else None,
    )


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


def _count_budget_exhausted(ctx: PipelineContext, sources: Iterable[Source]) -> None:
    """Count one file whose finished run was checked without these sources (their daily budget ran out)."""
    with ctx._budget_lock:
        for source in sources:
            ctx._budget_exhausted[source] = ctx._budget_exhausted.get(source, 0) + 1


def budget_exhausted_warnings(ctx: PipelineContext) -> list[str]:
    """Job-completion warnings for online sources this job ran out of daily budget for.

    Args:
        ctx: The job's context, read after every file has run.

    Returns:
        One user-facing warning per source that ran out (empty when nothing did), e.g. "TheIntroDB's daily lookup
        limit was reached: 39 files were checked without it. It resets at 00:00 UTC; run the library again after
        that (or add a TheIntroDB API key for a higher limit)."
    """
    with ctx._budget_lock:
        counts = dict(ctx._budget_exhausted)
    warnings = []
    for source in sorted(counts, key=lambda s: _ONLINE_LABELS[s]):
        count = counts[source]
        label = _ONLINE_LABELS[source]
        file_word = "file" if count == 1 else "files"
        verb = "was" if count == 1 else "were"
        key_hint = " (or add a TheIntroDB API key for a higher limit)" if source is Source.THEINTRODB else ""
        warnings.append(
            f"{label}'s daily lookup limit was reached: {count} {file_word} {verb} checked without it. "
            f"It resets at {RESET_TIME_LABEL}; run the library again after that{key_hint}."
        )
    return warnings


def _lookup(client: Any, source: Source, ids: MediaIds, rec: FileRecord, ctx: PipelineContext, cancel_check) -> bool:
    """Ask one online source and store what it found.

    Returns:
        Whether the source refused because its daily budget ran out (so the caller can note it on the file, and count
        the file on the job once its run finishes).
    """
    try:
        result = client.lookup(ids, duration_ms=rec.duration_ms, priority=ctx.priority(), cancel_check=cancel_check)
    except Exception as exc:
        logger.warning("{} lookup failed for {}: {}", _ONLINE_LABELS[source], rec.canonical_path, type(exc).__name__)
        return False
    if not isinstance(result, LookupResult) or result.status not in _STORED_LOOKUPS:
        # unavailable / not_applicable: nothing is stored, so the next run asks again.
        exhausted = isinstance(result, LookupResult) and is_budget_exhausted(result.detail)
        if exhausted:
            _warn_budget_exhausted(ctx, source)
        else:
            logger.debug("{} lookup for {}: {}", _ONLINE_LABELS[source], rec.canonical_path, result)
        return exhausted
    ctx.store.replace_evidence(
        rec.id, source, list(result.candidates), detail=result.detail, version=PARSER_VERSIONS[source]
    )
    return False


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


def _read_server_markers(
    ctx: PipelineContext, rec: FileRecord, servers: _ItemServers, refresh: bool, *, first_read_only: bool
) -> None:
    """Store each owning server's current markers for the file as evidence, when they are due.

    Args:
        ctx: The job's context.
        rec: The file.
        servers: The file's owning servers and their item ids.
        refresh: A forced run: read every server we never published to.
        first_read_only: Every wanted type is already decided, so only a server never asked for this file (or asked by
            an older reader) is read -- its own markers can still shorten decided credits (spec §5.5 rule 7). An empty
            or unusable answer isn't asked again on such a run, unless the job checks servers
            (``ctx.recheck_empty_server_markers``): then it is on its backoff (``MarkerStore.server_recheck_due``).
    """
    for owner in servers.owning:
        cfg = owner.config
        published = ctx.store.get_publish_state(rec.id, cfg.id)
        if published and published.markers:
            continue  # what's there now is (partly) ours: never a second opinion
        if not refresh and not _server_markers_due(ctx, rec, cfg.id, first_read_only=first_read_only):
            continue
        item_id = servers.item_id(owner)
        if not item_id:
            continue
        item_wide = cfg.type in _ITEM_WIDE_MARKERS
        if item_wide and ctx.store.published_to_item(cfg.id, item_id):
            continue
        try:
            found = read_server_markers(owner.server, cfg, item_id, duration_ms=rec.duration_ms)
        except Exception as exc:
            logger.debug("Reading markers on {} failed for {}: {}", cfg.name, rec.canonical_path, exc)
            found = None
        if found is None:
            if not _server_rows(ctx, rec, cfg.id):
                # Remembered, so a run with everything decided doesn't ask again; a run still missing evidence does.
                ctx.store.replace_evidence(
                    rec.id,
                    Source.SERVER_MARKERS,
                    [],
                    origin=cfg.id,
                    detail=UNUSABLE_SERVER_MARKERS_DETAIL,
                    version=READER_VERSION,
                )
            elif ctx.recheck_empty_server_markers:
                # The stored answer stays, but the re-read counts: a read that always fails (another cut on a Plex
                # item, a server that can't serve markers) stops being asked after the last backoff step too.
                ctx.store.count_failed_server_reread(rec.id, cfg.id)
            continue
        if item_wide and ctx.store.published_to_item(cfg.id, item_id):
            continue  # another version of this item was published while the read was out: it may show ours
        source, detail = Source.SERVER_MARKERS, ""
        if found and cfg.type in _IMPORTER_PLUGIN_SERVERS:
            readable, importer = _importer_plugin(ctx, owner)
            if not readable:
                # They may be a crowd database's copy: stored as "none there", so they don't count and are read again
                # like an empty answer.
                found, detail = [], PLUGINS_UNKNOWN_DETAIL
            elif importer:
                source, detail = Source.SERVER_MARKERS_IMPORTED, imported_detail(importer)
        ctx.store.replace_evidence(
            rec.id,
            source,
            list(found),
            origin=cfg.id,
            detail=detail,
            version=READER_VERSION,
            also_replaces=SERVER_SOURCES - {source},
        )


def _server_rows(ctx: PipelineContext, rec: FileRecord, server_id: str) -> list[EvidenceRow]:
    return [r for r in ctx.store.evidence_rows(rec.id) if r.source in SERVER_SOURCES and r.origin == server_id]


def _server_markers_due(ctx: PipelineContext, rec: FileRecord, server_id: str, *, first_read_only: bool) -> bool:
    rows = _server_rows(ctx, rec, server_id)
    if not rows or ctx.store.evidence_version(rec.id, rows[0].source, server_id) != READER_VERSION:
        return True
    if first_read_only:
        if not ctx.recheck_empty_server_markers:
            return False
        # Check servers: an empty answer is asked again on its backoff (RECHECK_AFTER), then no more.
        return ctx.store.server_recheck_due(rec.id, server_id, now=ctx.now(), after=RECHECK_AFTER)
    if any(r.detail == UNUSABLE_SERVER_MARKERS_DETAIL for r in rows):
        return True  # unreadable or another cut last time: asked again on every run that still needs evidence
    fetched = max(datetime.fromisoformat(r.fetched_at) for r in rows)
    if not all(r.type is None for r in rows) or ctx.now() - fetched <= EMPTY_SERVER_MARKERS_RETRY:
        return False
    if ctx.recheck_empty_server_markers:
        # Check servers keeps to its backoff for an empty answer it already read again.
        return ctx.store.server_recheck_due(rec.id, server_id, now=ctx.now(), after=RECHECK_AFTER)
    return True


def _row(
    cfg: ServerConfig,
    adapter_name: str,
    status: ServerStatus,
    message: str,
    path: str,
    *,
    reason_code: str | None = None,
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
    back on must reach the job's next file, not one 5 minutes later. A ready Plex whose Plex Pass didn't answer is
    reused for ``PLEX_PASS_UNKNOWN_TTL_S`` only.
    """
    cached = ctx._capabilities.get(cfg.id)
    if _still_fresh(ctx, cached):
        return cached[1]
    with ctx._capability_guard:
        lock = ctx._capability_locks.setdefault(cfg.id, threading.Lock())
    with lock:
        cached = ctx._capabilities.get(cfg.id)
        if _still_fresh(ctx, cached):
            return cached[1]
        report = publisher.capability()
        if report.state not in _SETTINGS_ANSWERS:
            ctx._capabilities[cfg.id] = (time.monotonic(), report)
        return report


def _identity_changed(rec: FileRecord) -> bool:
    try:
        st = os.stat(rec.canonical_path)
    except OSError:
        return True
    return (st.st_size, st.st_mtime_ns) != (rec.size, rec.mtime_ns)


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


def _publish_to(
    owner: _Owning,
    rec: FileRecord,
    markers: dict[MarkerType, Marker],
    needs_review: bool,
    servers: _ItemServers,
    ctx: PipelineContext,
    phase: Callable[[str], None],
) -> dict:
    cfg = owner.config
    path = rec.canonical_path
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
        return _row(cfg, name, status, message, path, reason_code=reason_code)

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
    )
    if publisher is None:
        return _not_written(ServerStatus.SKIPPED, "Not supported for this server type yet", name="")
    try:
        report = cached_capability(ctx, cfg, publisher)
    except Exception as exc:
        logger.warning("Couldn't check whether {} can receive markers: {}", cfg.name, type(exc).__name__)
        message = f"Couldn't check this server: {type(exc).__name__}"
        return _not_written(ServerStatus.FAILED, message, name=publisher.name)
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
        if needs_review and not wanted:
            return _row(cfg, publisher.name, ServerStatus.NEEDS_REVIEW, "Sources don't agree yet", path)
        message = with_kept_note("", kept_note(kept_types, wanted, vendor)) or "Up to date"
        message = with_kept_note(message, _shown_differently([m for m in wanted if m.type not in kept_types]))
        return _row(cfg, publisher.name, ServerStatus.UP_TO_DATE, message, path)

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
            else:
                shown = _shown_on_server(
                    publisher, cfg, item_id, list(item_row.markers), item_row.kept_types, item_row.item_files
                )
                if shown is None:
                    return {**_up_to_date(item_row.kept_types), READ_BACK_FAILED: True}
                released = bool(item_row.kept_types) and not _live_markers_settings(ctx, cfg).keeps_server_markers
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
        if not wanted and previous == [] and own_previous is None and not holds_kept:
            if needs_review:
                return _row(cfg, publisher.name, ServerStatus.NEEDS_REVIEW, "Sources don't agree yet", path)
            message = "This server can't show the markers found for this file" if markers else "No markers found"
            return _row(cfg, publisher.name, ServerStatus.NONE, message, path)

        if _identity_changed(rec):
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
            return _not_written(ServerStatus.FAILED, str(exc), name=publisher.name, item_id=attempted_item)
        except Exception as exc:
            logger.exception("Publishing markers to {} failed for {}", cfg.name, path)
            store.set_item_publish_state(cfg.id, item_id, None, "failed")
            message = f"{type(exc).__name__}: {exc}"
            return _not_written(ServerStatus.FAILED, message, name=publisher.name, item_id=attempted_item)

        changed = publisher.last_write_changed
        kept = publisher.last_kept_types
        version = store.set_item_publish_state(
            cfg.id, item_id, ours, "written", kept_types=kept, item_files=publisher.last_item_files
        )
        if not changed and _unchanged(version):
            return _up_to_date(kept)  # a forced run whose write changed nothing
        store.set_publish_basis(rec.id, cfg.id, decided_hash=decided_hash, item_version=version)
        note = with_kept_note(kept_note(kept, wanted, vendor), _shown_differently(ours))
        shown_types = {m.type for m in ours} | kept
        waiting_for = [m.type.value for m in wanted if m.type not in shown_types]
        if waiting_for:
            # Plex shows a type only when every version of the item is decided and agrees on it.
            message = with_kept_note(
                f"Waiting for this item's other versions to agree on: {', '.join(waiting_for)}", note
            )
            return _finish(ServerStatus.WAITING, message, name=publisher.name, item_id=item_id, published=ours)
        if ours:
            message = f"{len(ours)} marker(s)"
        else:
            message = "Cleared our markers from this server" if changed or not note else ""
        message = with_kept_note(message, note)
        written = _finish(ServerStatus.WRITTEN, message, name=publisher.name, item_id=item_id, published=ours)
        # Recorded either way, but only a write that changed the server says so: it already showed this.
        return written if changed else _up_to_date(kept)


def _clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


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
            parts.append(f"{mtype.value} {_clock(d.marker.start_ms)}–{_clock(d.marker.end_ms)} ({by})")
        elif d.status is DecisionStatus.NEEDS_REVIEW:
            parts.append(f"{mtype.value} needs review")
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
        if _identity_changed(sibling):
            stale.append(path)
            continue
        if ctx.store.get_intro_chapter_limit(sibling.id) == (True, limit):
            continue
        types = _enabled_types(ctx.settings, _stored_ids(ctx.store, sibling))
        decision = _decide(ctx, sibling, types, limit)[MarkerType.INTRO]
        if _decisions_changed(ctx.store, sibling.id, {MarkerType.INTRO: decision}, stored.settings_fingerprint):
            stale.append(path)
    if stale:
        ctx.request_followups(stale)


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
) -> ItemOutcome | None:
    path = item.canonical_path

    def cancelled() -> bool:
        return bool(cancel_check and cancel_check())

    owning = _owning_servers(item, ctx)
    owners = _marker_owners(owning, path)
    if not owners:
        return ItemOutcome(FileOutcome.NO_OWNERS.value, "No server with Intro & Credits turned on has this file")
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ItemOutcome(FileOutcome.FILE_NOT_FOUND.value, "File not found on disk")
    except OSError as exc:
        return ItemOutcome(FileOutcome.FAILED.value, f"Couldn't read the file: {type(exc).__name__}")
    if not stat.S_ISREG(st.st_mode):
        return ItemOutcome(FileOutcome.FILE_NOT_FOUND.value, "File not found on disk")

    refresh_probe = _refreshing(ctx, path, Source.CHAPTERS)
    existing = ctx.store.get_file(path)
    unchanged = existing is not None and (existing.size, existing.mtime_ns) == (st.st_size, st.st_mtime_ns)
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

    servers = _ItemServers(item, owning)
    known_kind = ctx.store.get_server_kind(existing.id) if unchanged else None
    ids, lookups_allowed, confirmed_kind = _resolve_kind(ids_from_path(path), servers, known_kind)
    rec = ctx.store.upsert_file(
        FileIdentity(path, st.st_size, st.st_mtime_ns),
        duration_ms=probe.duration_ms if probe else None,
        season_key=os.path.dirname(path) if ids.is_episode else None,
        is_movie=ids.kind == "movie",
    )
    if confirmed_kind is not None:
        ctx.store.set_server_kind(rec.id, confirmed_kind)
    if probe is not None:
        ctx.store.replace_evidence(rec.id, Source.CHAPTERS, chapter_candidates(probe), version=CHAPTER_RULES_VERSION)
    _mark_refreshed(ctx, path, Source.CHAPTERS)
    if not rec.duration_ms:
        return ItemOutcome(FileOutcome.FAILED.value, "Couldn't read the file's duration")

    types = _enabled_types(ctx.settings, ids)
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
    budget_exhausted: set[Source] = set()
    for source_id in ctx.settings.ordered_enabled_sources():
        source = Source(source_id)
        refresh = _refreshing(ctx, path, source)
        if (
            not gather_all
            and source is not Source.SERVER_MARKERS
            and _all_decided(decisions, types)
            and not _stale_evidence(ctx, rec, source)
            and not _decided_with_a_due_answer(ctx, rec, source, decisions, types)
        ):
            continue
        if cancelled():
            return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
        if source in _ONLINE_LABELS:
            client = ctx.clients.get(source_id)
            if (
                source is Source.THEINTRODB
                and not gather_all
                and ctx.priority() >= PRIORITY_LOW
                and _only_confirming_chapters(decisions, types)
            ):
                continue  # the only daily-budgeted source is kept for files it could still decide
            if lookups_allowed and client is not None and _needs_lookup(ctx, rec, source, refresh):
                lookup_ids = lookup_ids or _lookup_ids(ids, servers)
                phase(f"Looking up {_ONLINE_LABELS[source]}…")
                if _lookup(client, source, lookup_ids, rec, ctx, cancel_check):
                    budget_exhausted.add(source)
        elif source is Source.SERVER_MARKERS:
            phase("Reading markers already on servers…")
            first_read_only = not gather_all and _all_decided(decisions, types)
            _read_server_markers(ctx, rec, servers, refresh, first_read_only=first_read_only)
        else:
            pending = [
                spec
                for spec in ctx.local_detectors
                if spec.source is source and _detector_pending(ctx, rec, spec, decisions, types, refresh=refresh)
            ]
            if not local and any(_needs_worker(ctx, rec, spec) for spec in pending):
                return None  # sources already refreshed stay marked; the worker refreshes the rest
            for spec in pending:
                _run_detector(
                    ctx,
                    rec,
                    spec,
                    gpu=gpu,
                    gpu_device_path=gpu_device_path,
                    phase=phase,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )
            if pending:
                ctx.run_memo(path).clear()  # what the detectors' hooks read before they ran is out of date now
        _mark_refreshed(ctx, path, source)
        if not gather_all:
            decisions = _decide(ctx, rec, types, intro_limit)

    decisions = _decide(ctx, rec, types, intro_limit)
    fingerprint = ctx.settings.detection_fingerprint()
    if _decisions_changed(ctx.store, rec.id, decisions, fingerprint):
        ctx.store.save_decisions(rec.id, decisions, settings_fingerprint=fingerprint)
    if ctx.store.get_intro_chapter_limit(rec.id) != (True, intro_limit):
        ctx.store.set_intro_chapter_limit(rec.id, intro_limit)
    markers = ctx.store.get_markers(rec.id)
    needs_review = any(decisions[t].status is DecisionStatus.NEEDS_REVIEW for t in types)
    if _identity_changed(rec):
        raise _FileChangedError(path)
    replaced = existing is not None and not unchanged
    rows = []
    for owner in owners:
        # Per-server publish state already tolerates a partial fan-out; a busy Plex DB can hold a write for 30 s.
        if cancelled():
            return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
        row = _publish_to(owner, rec, markers, needs_review, servers, ctx, phase)
        if replaced and row["status"] in (ServerStatus.WRITTEN.value, ServerStatus.UP_TO_DATE.value):
            row[VERIFY_LATER] = True
        rows.append(row)
    outcome = file_outcome({r["status"] for r in rows}, needs_review=needs_review)
    _count_budget_exhausted(ctx, budget_exhausted)
    labels = tuple(sorted(_ONLINE_LABELS[source] for source in budget_exhausted))
    return ItemOutcome(outcome.value, _summary(decisions, types, labels), rows)


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
) -> ItemOutcome | None:
    if cancel_check and cancel_check():
        return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
    if is_extra(item.canonical_path):
        # No source describes a trailer or featurette and no server lists one as an item, so checking it would only
        # wait (and queue retries) for an item that never comes. Folder jobs and library listings both contain them.
        return ItemOutcome(FileOutcome.SKIPPED.value, EXTRAS_NOT_CHECKED)
    for _ in range(MAX_ATTEMPTS):
        try:
            with _PATH_LOCKS.hold(item.canonical_path), ctx._running(item.canonical_path):
                outcome = _attempt(
                    item,
                    ctx,
                    local=local,
                    gpu=gpu,
                    gpu_device_path=gpu_device_path,
                    phase=phase_callback or _no_phase,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )
                if outcome is not None:
                    ctx._refreshed.pop(item.canonical_path, None)
                return outcome
        except _FileChangedError:
            logger.info("{} changed while its markers were detected; detecting again", item.canonical_path)
    return ItemOutcome(
        FileOutcome.FAILED.value, "The file kept changing while it was analysed; it will be tried again on the next run"
    )


def check_item(
    item: ProcessableItem, *, ctx: PipelineContext, cancel_check: Callable[[], bool] | None = None
) -> ItemOutcome | None:
    """Check stage: decide from cheap sources and publish.

    Args:
        item: The file.
        ctx: The job's context.
        cancel_check: True once the job is cancelled.

    Returns:
        The item's outcome, or None when a local detector that needs a worker has to run (send it to a worker). A
        detector that doesn't need one runs here, unless another detector that has to run at the same source needs a
        worker.
    """
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
) -> ItemOutcome:
    """Worker stage: the same steps plus local detectors on the worker's GPU/CPU.

    A paused job doesn't block here (that would hold a worker previews need); detectors receive ``pause_check``.

    Args:
        item: The file.
        ctx: The job's context.
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.
        progress_callback: Unused until detectors report progress.
        phase_callback: Shows the current step on the worker row.
        cancel_check: True once the job is cancelled.
        pause_check: True while the job is paused.

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
        pause_check=pause_check,
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
        outcome_keys=OUTCOME_KEYS,
        check_label="Looking up markers…",
        check_worker_label="Intro & Credits",
        check_share=0.25,
    )
