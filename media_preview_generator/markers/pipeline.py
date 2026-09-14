"""Intro & Credits per-file pipeline (spec §6.2).

owners → identity → kind → evidence in the user's source order (a normal run stops once every enabled type is
decided by more than chapters alone; a forced re-detect asks every source) → decide from everything stored → store →
publish to each owner. ``check_item`` runs on the dispatcher's checking threads (no worker slot); it returns
None only when a registered local detector (season audio, credit text) could still decide something, which sends the
item to a GPU/CPU worker where ``process_item`` runs the same steps plus the detectors.
"""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from ..config.paths import is_path_excluded
from ..job_kinds import ItemOutcome, KindHandlers
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import OwnershipMatch
from ..servers.registry import server_config_from_dict
from ..web.settings_manager import get_settings_manager
from .decide import DecisionContext, DecisionStatus, TypeDecision, decide
from .external_ids import ids_from_path, ids_from_server_dict, merge_ids
from .models import SERVER_SOURCES, Candidate, FileIdentity, Marker, MarkerType, MediaIds, Source
from .outcomes import NOT_IN_LIBRARY, OUTCOME_KEYS, STATE_BY_STATUS, FileOutcome, ServerStatus, file_outcome
from .ownership import marker_matches, owning_servers
from .probe import ProbeError, ffprobe_path_for, probe_media
from .publishers.base import (
    Capability,
    CapabilityReport,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
)
from .publishers.factory import publisher_for
from .settings import GlobalMarkersSettings, ServerMarkersSettings, get_global_settings, load_server
from .sources import introdb, skipdb, theintrodb
from .sources.chapters import CHAPTER_RULES_VERSION, chapter_candidates
from .sources.introdb import IntroDbClient
from .sources.online import LookupResult
from .sources.ratelimit import PRIORITY_LOW
from .sources.server_markers import READER_VERSION, imported_detail, importer_plugin, read_server_markers
from .sources.skipdb import SkipDbClient
from .sources.theintrodb import TheIntroDbClient
from .store import FileRecord, ItemPublishStateRow, MarkerStore, get_marker_store

NO_DATA_RETRY = timedelta(days=14)
# Servers detect their own markers on a schedule (Plex overnight), so "none there" is asked again a day later.
EMPTY_SERVER_MARKERS_RETRY = timedelta(days=1)
# A file replaced while it is analysed is detected again from scratch; one that keeps changing is being written.
MAX_ATTEMPTS = 3
_ONLINE_LABELS = {Source.THEINTRODB: "TheIntroDB", Source.INTRODB: "IntroDB", Source.SKIPDB: "SkipDB"}
PARSER_VERSIONS = {
    Source.THEINTRODB: theintrodb.PARSER_VERSION,
    Source.INTRODB: introdb.PARSER_VERSION,
    Source.SKIPDB: skipdb.PARSER_VERSION,
}
_STORED_LOOKUPS = ("ok", "no_data")
# Plex and Emby can't tell our markers from their own, and Plex shows one marker set per item across its versions, so
# their markers are never read back from an item we published to. Jellyfin's reader leaves ours out itself.
_ITEM_WIDE_MARKERS = frozenset({ServerType.PLEX, ServerType.EMBY})
# Servers where a plugin can import a crowd skip database into the server's own markers; Plex detects its own.
_IMPORTER_PLUGIN_SERVERS = frozenset({ServerType.JELLYFIN, ServerType.EMBY})
PLUGINS_UNKNOWN_DETAIL = "Couldn't read this server's plugins, so its markers aren't used"
_CANCELLED = "cancelled by user"

LocalDetector = Callable[..., list[Candidate]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _no_phase(_text: str) -> None:
    return None


@dataclass(frozen=True)
class LocalDetectorSpec:
    """A detector that needs a worker slot (phase 2: season audio; phase 3: credit text).

    Attributes:
        source: The evidence source it fills (its place in the user's source order).
        types: Marker types it can decide.
        detect: ``detect(file, *, ctx, gpu, gpu_device_path, phase_callback, cancel_check, pause_check)``.
    """

    source: Source
    types: frozenset[MarkerType]
    detect: LocalDetector


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
        local_detectors: Detectors that need a worker slot.
        now: Current UTC time (tests use a fake clock).
        capability_ttl_s: How long a server's capability answer is reused.
        live_config: A server's saved config right now (None once it was removed). The registry is a snapshot from
            when the job started; consent is read from here before every write.
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
    _capabilities: dict[str, tuple[float, CapabilityReport]] = field(default_factory=dict, repr=False)
    _capability_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _capability_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Paths whose evidence a forced run already refreshed, so the worker stage doesn't ask the sources twice.
    _refreshed: set[str] = field(default_factory=set, repr=False)
    # Per server id: its intro-DB importer plugin ("" = none), or None when its plugin list couldn't be read.
    _importers: dict[str, str | None] = field(default_factory=dict, repr=False)
    _importer_locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _importer_guard: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True)
class _Owning:
    """An enabled server with at least one library holding the file (Intro & Credits may be off there)."""

    server: Any
    config: ServerConfig
    matches: tuple[OwnershipMatch, ...]


class _KeyedLocks:
    """One lock per key, kept only while a run holds or waits for it. One process serves the app (a single gunicorn
    worker), so these locks are enough.

    Lock order, never taken the other way round: a path's lock (the whole run on one file), then an item's lock (one
    server item's publish), then Plex's database lock (inside the publisher), then markers.db's own lock (every store
    call). The Plex publisher reads sibling decisions from markers.db before it takes its database lock.
    """

    def __init__(self, lock_factory: Callable[[], Any] = threading.Lock) -> None:
        self._lock_factory = lock_factory
        self._guard = threading.Lock()
        self._locks: dict[Hashable, list] = {}  # key → [lock, runs holding or waiting]

    @contextmanager
    def hold(self, key: Hashable) -> Iterator[None]:
        """Hold ``key``'s lock for the duration of the block."""
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [self._lock_factory(), 0]
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]


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


def build_context(
    *, registry: Any, config: Any, priority: int | Callable[[], int], force: bool = False
) -> PipelineContext:
    """Context from live settings (used by the job runner).

    Args:
        registry: The job's ``ServerRegistry``.
        config: The job's ``Config``.
        priority: The job's priority, or a callable returning its current value.
        force: Re-detect.

    Returns:
        A context for one job.
    """
    settings = get_global_settings()
    return PipelineContext(
        registry=registry,
        config=config,
        settings=settings,
        store=get_marker_store(),
        priority=priority if callable(priority) else (lambda: priority),
        ffprobe=ffprobe_path_for(getattr(config, "ffmpeg_path", None)),
        force=force,
        clients=build_clients(settings),
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
        if known_kind == "movie" and path_ids.kind == "movie":
            ids = path_ids
        else:
            ids = MediaIds(known_kind) if known_kind in ("movie", "episode") else MediaIds()
        return ids, ids.kind in ("movie", "episode"), None
    answer = servers.server_ids()
    if answer is None:
        return path_ids, False, None
    ids = merge_ids(path_ids, answer) if path_ids.kind == "movie" and answer.kind == "movie" else answer
    return ids, ids.kind in ("movie", "episode"), answer.kind


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


def _decision_order(settings: GlobalMarkersSettings) -> tuple[str, ...]:
    """Enabled sources in the user's order; importer-plugin copies ride on the server-markers switch, ranked after it."""
    order: list[str] = []
    for source_id in settings.ordered_enabled_sources():
        order.append(source_id)
        if source_id == Source.SERVER_MARKERS.value:
            order.append(Source.SERVER_MARKERS_IMPORTED.value)
    return tuple(order)


def _decide(ctx: PipelineContext, rec: FileRecord, types: frozenset[MarkerType]) -> dict[MarkerType, TypeDecision]:
    """Decide from everything stored for the enabled sources, so a forced and a normal run always agree."""
    order = _decision_order(ctx.settings)
    enabled = set(order)
    candidates = [c for c in ctx.store.get_evidence(rec.id) if c.source.value in enabled]
    dctx = DecisionContext(rec.duration_ms or 0, rec.is_movie, ctx.settings.publish_when, types, order)
    # A lock always wins (spec §5.5 rule 1); what respect_locks=False should change is for the phase 4 editor.
    return decide(candidates, dctx, ctx.store.get_locked(rec.id))


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
    """Decided, and not by chapters alone: two agreeing sources may still veto a chapter (spec §5.5 rule 3)."""
    return decision.status is DecisionStatus.DECIDED and decision.marker.decided_by != (Source.CHAPTERS.value,)


def _all_decided(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> bool:
    return all(_decided_beyond_chapters(decisions[t]) for t in types)


def _only_confirming_chapters(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> bool:
    """Every type is decided and some only by chapters, so the search goes on only to let sources contradict them."""
    return all(decisions[t].status is DecisionStatus.DECIDED for t in types) and not _all_decided(decisions, types)


def _stale_evidence(ctx: PipelineContext, rec: FileRecord, source: Source) -> bool:
    """Whether stored evidence of ``source`` was made by an older parser or server reader, so it is derived again."""
    if source in _ONLINE_LABELS:
        stored = ctx.store.evidence_fetched_at(rec.id, source) is not None
        return stored and ctx.store.evidence_version(rec.id, source) != PARSER_VERSIONS[source]
    if source is Source.SERVER_MARKERS:
        reads = {(r.source, r.origin) for r in ctx.store.evidence_rows(rec.id) if r.source in SERVER_SOURCES}
        return any(ctx.store.evidence_version(rec.id, s, origin) != READER_VERSION for s, origin in reads)
    return False


def _lookup(client: Any, source: Source, ids: MediaIds, rec: FileRecord, ctx: PipelineContext, cancel_check) -> None:
    try:
        result = client.lookup(ids, duration_ms=rec.duration_ms, priority=ctx.priority(), cancel_check=cancel_check)
    except Exception as exc:
        logger.warning("{} lookup failed for {}: {}", _ONLINE_LABELS[source], rec.canonical_path, type(exc).__name__)
        return
    if not isinstance(result, LookupResult) or result.status not in _STORED_LOOKUPS:
        # unavailable / not_applicable: nothing is stored, so the next run asks again.
        logger.debug("{} lookup for {}: {}", _ONLINE_LABELS[source], rec.canonical_path, result)
        return
    ctx.store.replace_evidence(
        rec.id, source, list(result.candidates), detail=result.detail, version=PARSER_VERSIONS[source]
    )


def _importer_plugin(ctx: PipelineContext, owner: _Owning) -> tuple[bool, str | None]:
    """Whether the server's plugin list could be read, and its intro-DB importer plugin; asked once per server per job."""
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


def _read_server_markers(ctx: PipelineContext, rec: FileRecord, servers: _ItemServers, refresh: bool) -> None:
    for owner in servers.owning:
        cfg = owner.config
        published = ctx.store.get_publish_state(rec.id, cfg.id)
        if published and published.markers:
            continue  # what's there now is (partly) ours: never a second opinion
        if not refresh and not _server_markers_due(ctx, rec, cfg.id):
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
        if found is not None and item_wide and ctx.store.published_to_item(cfg.id, item_id):
            found = None  # another version of this item was published while the read was out: it may show ours
        if found is None:
            continue
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


def _server_markers_due(ctx: PipelineContext, rec: FileRecord, server_id: str) -> bool:
    rows = [r for r in ctx.store.evidence_rows(rec.id) if r.source in SERVER_SOURCES and r.origin == server_id]
    if not rows or ctx.store.evidence_version(rec.id, rows[0].source, server_id) != READER_VERSION:
        return True
    fetched = max(datetime.fromisoformat(r.fetched_at) for r in rows)
    return all(r.type is None for r in rows) and ctx.now() - fetched > EMPTY_SERVER_MARKERS_RETRY


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


def _capability(ctx: PipelineContext, cfg: ServerConfig, publisher: MarkerPublisher) -> CapabilityReport:
    """Per-server capability, cached for ``capability_ttl_s``; one fetch per server even when every check thread misses."""
    cached = ctx._capabilities.get(cfg.id)
    if cached and time.monotonic() - cached[0] < ctx.capability_ttl_s:
        return cached[1]
    with ctx._capability_guard:
        lock = ctx._capability_locks.setdefault(cfg.id, threading.Lock())
    with lock:
        cached = ctx._capabilities.get(cfg.id)
        if cached and time.monotonic() - cached[0] < ctx.capability_ttl_s:
            return cached[1]
        report = publisher.capability()
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
        report = _capability(ctx, cfg, publisher)
    except Exception as exc:
        logger.warning("Couldn't check whether {} can receive markers: {}", cfg.name, type(exc).__name__)
        message = f"Couldn't check this server: {type(exc).__name__}"
        return _not_written(ServerStatus.FAILED, message, name=publisher.name)
    if not report.ready:
        return _not_written(ServerStatus.SKIPPED, report.message or report.state.value, name=publisher.name)
    if "plex_pass" in report.details and report.details["plex_pass"] is None:
        # READY with an unknown Pass is what the status tab shows; Plex serves no markers without a Pass (spec §6.3).
        return _not_written(ServerStatus.SKIPPED, "Can't reach Plex to confirm Plex Pass", name=publisher.name)
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

    def _up_to_date() -> dict:
        if needs_review and not wanted:
            return _row(cfg, publisher.name, ServerStatus.NEEDS_REVIEW, "Sources don't agree yet", path)
        return _row(cfg, publisher.name, ServerStatus.UP_TO_DATE, "Up to date", path)

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
            return _up_to_date()
        previous = _previous_on_item(item_row, publisher)
        if not wanted and previous == [] and own_previous is None:
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

        version = store.set_item_publish_state(cfg.id, item_id, ours, "written")
        if _unchanged(version):
            return _up_to_date()  # a forced run whose write changed nothing
        store.set_publish_basis(rec.id, cfg.id, decided_hash=decided_hash, item_version=version)
        shown = {m.type for m in ours}
        waiting_for = [m.type.value for m in wanted if m.type not in shown]
        if waiting_for:
            # Plex shows a type only when every version of the item is decided and agrees on it.
            message = f"Waiting for this item's other versions to agree on: {', '.join(waiting_for)}"
            return _finish(ServerStatus.WAITING, message, name=publisher.name, item_id=item_id, published=ours)
        message = f"{len(ours)} marker(s)" if ours else "Cleared our markers from this server"
        return _finish(ServerStatus.WRITTEN, message, name=publisher.name, item_id=item_id, published=ours)


def _clock(ms: int) -> str:
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def _summary(decisions: dict[MarkerType, TypeDecision], types: frozenset[MarkerType]) -> str:
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
    return "; ".join(parts) or "Nothing to detect for this file"


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

    refresh = ctx.force and path not in ctx._refreshed
    existing = ctx.store.get_file(path)
    unchanged = existing is not None and (existing.size, existing.mtime_ns) == (st.st_size, st.st_mtime_ns)
    probe = None
    stale_rules = unchanged and ctx.store.evidence_version(existing.id, Source.CHAPTERS) != CHAPTER_RULES_VERSION
    if refresh or not unchanged or not existing.duration_ms or stale_rules:
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
    if not rec.duration_ms:
        return ItemOutcome(FileOutcome.FAILED.value, "Couldn't read the file's duration")

    types = _enabled_types(ctx.settings, ids)
    # A normal run stops asking once stored answers decide everything beyond chapters alone (answers from an older
    # parser or reader are still asked again); a forced run asks every source (and hands every detector source to a
    # worker) so no stale answer is left behind.
    gather_all = ctx.force
    decisions = _decide(ctx, rec, types)
    lookup_ids: MediaIds | None = None
    for source_id in ctx.settings.ordered_enabled_sources():
        source = Source(source_id)
        if not gather_all and _all_decided(decisions, types) and not _stale_evidence(ctx, rec, source):
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
                _lookup(client, source, lookup_ids, rec, ctx, cancel_check)
        elif source is Source.SERVER_MARKERS:
            phase("Reading markers already on servers…")
            _read_server_markers(ctx, rec, servers, refresh)
        else:
            pending = [
                spec
                for spec in ctx.local_detectors
                if spec.source is source
                and any(gather_all or decisions[t].status is not DecisionStatus.DECIDED for t in spec.types & types)
            ]
            if pending and not local:
                if refresh:
                    ctx._refreshed.add(path)
                return None
            for spec in pending:
                found = spec.detect(
                    rec,
                    ctx=ctx,
                    gpu=gpu,
                    gpu_device_path=gpu_device_path,
                    phase_callback=phase,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )
                ctx.store.replace_evidence(rec.id, source, list(found))
        if not gather_all:
            decisions = _decide(ctx, rec, types)
    if refresh:
        ctx._refreshed.add(path)

    decisions = _decide(ctx, rec, types)
    fingerprint = ctx.settings.detection_fingerprint()
    if _decisions_changed(ctx.store, rec.id, decisions, fingerprint):
        ctx.store.save_decisions(rec.id, decisions, settings_fingerprint=fingerprint)
    markers = ctx.store.get_markers(rec.id)
    needs_review = any(decisions[t].status is DecisionStatus.NEEDS_REVIEW for t in types)
    if _identity_changed(rec):
        raise _FileChangedError(path)
    rows = []
    for owner in owners:
        # Per-server publish state already tolerates a partial fan-out; a busy Plex DB can hold a write for 30 s.
        if cancelled():
            return ItemOutcome(FileOutcome.FAILED.value, _CANCELLED)
        rows.append(_publish_to(owner, rec, markers, needs_review, servers, ctx, phase))
    outcome = file_outcome({r["status"] for r in rows}, needs_review=needs_review)
    return ItemOutcome(outcome.value, _summary(decisions, types), rows)


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
    for _ in range(MAX_ATTEMPTS):
        try:
            with _PATH_LOCKS.hold(item.canonical_path):
                return _attempt(
                    item,
                    ctx,
                    local=local,
                    gpu=gpu,
                    gpu_device_path=gpu_device_path,
                    phase=phase_callback or _no_phase,
                    cancel_check=cancel_check,
                    pause_check=pause_check,
                )
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
        The item's outcome, or None when a local detector could still decide something (send it to a worker).
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
