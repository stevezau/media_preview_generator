"""Create Intro & Credits jobs from the API, webhook batches and schedules."""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from loguru import logger

from ..job_kinds import INTRO_CREDITS_FOLLOW_UP, JOB_KIND_INTRO_CREDITS, JOB_KIND_LOUDNESS
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import webhook_path_candidates
from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict
from ..web.jobs import (
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    Job,
    JobManager,
    get_job_manager,
    is_live_retry_chain,
)
from ..web.settings_manager import get_settings_manager
from .audio.season import season_group, season_videos
from .external_ids import ids_from_path, is_season_folder
from .job_runner import (
    DECIDE_AGAIN,
    FILES_SEALED,
    FOLLOW_UP_LOCK,
    MAX_RETRY_FILES,
    VERSION_RERUN,
    VERSION_RERUN_COUNTS,
    sent_by_a_sender,
    server_pin,
    start_intro_credits_job_async,
)
from .ownership import marker_matches
from .settings import load_server
from .versions import BATCH_FILES

# Serialises Inspector re-detect's "is this file already queued?" with the job creation, so a double-click queues one
# job. Webhook follow-ups use job_runner.FOLLOW_UP_LOCK, which their runner also takes to read the files.
_redetect_lock = threading.Lock()
# Serialises taking a preview job's request for its follow-up (``submit_pending_follow_up``), so two starts of one
# preview job queue it once.
_pending_follow_up_lock = threading.Lock()
_loudness_follow_up_lock = threading.Lock()
_REDETECT_SOURCE = "inspector"
_SEASON_PUBLISH_SOURCE = "inspector_season"
DECIDE_AGAIN_JOB_NAME = "Intro & Credits: files the old rules couldn't decide, decided again"
VERSION_RERUN_JOB_NAME = "Intro & Credits: Re-checking {total} after the app update · batch {batch} of {batches}"


def _utcnow() -> datetime:
    return datetime.now(UTC)


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


def marker_owned_paths(paths: list[str], server_id: str | None = None) -> list[str]:
    """Paths held by a library that Intro & Credits goes to on an enabled server.

    Runs on webhook threads, so it reads settings only: no disk or network access.

    Args:
        paths: File paths as the webhook sender reported them.
        server_id: Only this server counts (the job would be pinned to it); None = any server.

    Returns:
        The owned paths, in input order.
    """
    configs = _server_configs()
    owners = [cfg for cfg in configs if cfg.id == server_id] if server_id else configs
    return [
        path
        for path in paths
        if any(marker_matches(candidate, owners) for candidate in _local_candidates(path, configs))
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
    verify_chain: bool = False,
    chain_attempt: int = 0,
    reconcile: bool = False,
    parent_job_id: str | None = None,
    max_retries: int = 0,
    decide_again: bool = False,
    version_rerun: bool = False,
    version_rerun_counts: Mapping[str, int] | None = None,
    server_id: str | None = None,
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
        verify_chain: For a retry that follows a verify job (directly or through other retries): it queues no verify.
        chain_attempt: For a verify job: the retries its chain already used, so a retry it queues goes on counting.
        reconcile: Check servers: list the files of drifted published items instead of libraries or paths
            (``reconcile.find_drift``).
        parent_job_id: For a retry: the job whose retry chain it belongs to. The retry is hidden from the queue like
            a preview retry (``is_retry``); that job's row shows it.
        max_retries: For a retry: the retry count in force (the row's "Retry N/M").
        decide_again: List the files to decide again when the job runs (``job_runner._items_to_decide_again``)
            instead of libraries or paths.
        version_rerun: Take the next batch of files to re-check after an update when the job runs
            (``job_runner._items_to_read_again``) instead of libraries or paths.
        version_rerun_counts: For a version re-run: where its batch stands in the whole re-check
            (``job_runner.VERSION_RERUN_COUNTS``).
        server_id: Publish to this server only (``job_runner.server_pin``): the pin of the preview job it follows, as
            ``jobs.worker.resolve_per_item_pin`` resolved it, or of the job whose retry or check it is. None = every
            server with Intro & Credits on.

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
    if reconcile:
        config["reconcile"] = True
    if decide_again:
        config[DECIDE_AGAIN] = True
    if version_rerun:
        config[VERSION_RERUN] = True
    if version_rerun_counts:
        config[VERSION_RERUN_COUNTS] = dict(version_rerun_counts)
    if server_id:
        config["server_id"] = server_id
    if chain_attempt:
        config["chain_attempt"] = int(chain_attempt)
    if verify_chain:
        config["verify_chain"] = True
    if retry_attempt:
        config["retry_attempt"] = int(retry_attempt)
    if retry_attempt or verify or retry_delay_s:
        # The due time (not just the delay) is stored so a job revived after a restart doesn't wait again in full.
        config["retry_delay"] = int(retry_delay_s)
        config["retry_not_before"] = (_utcnow() + timedelta(seconds=int(retry_delay_s))).isoformat()
    if parent_job_id:
        config.update(is_retry=True, parent_job_id=parent_job_id, max_retries=int(max_retries))
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


def _queued_in_waiting_follow_ups(
    jm,
    configs: list[ServerConfig] | None = None,
    server_id: str | None = None,
    source: str | None = None,
    *,
    kind: str = JOB_KIND_INTRO_CREDITS,
) -> set[str]:
    """Local paths (every candidate) of the files webhook follow-ups that have never started list.

    A follow-up that has never started reads its files when it runs, so a file already listed there is covered. A
    revived one (PENDING again, started_at kept) may already have published the file's old version. So is one that
    publishes to fewer servers than the request: a follow-up pinned to one server covers only a request with the same
    pin; an unpinned one covers any request. And a Recently Added scan's follow-up doesn't cover a sender's files: it
    gives them no retry when missing from disk and no later verify (``job_runner.sent_by_a_sender``).

    Args:
        jm: The job manager.
        configs: Server configs; read from settings only when a waiting follow-up needs them.
        server_id: The request's pin (``job_runner.server_pin``); None = every server with Intro & Credits on.
        source: The request's source; None (a Season request) = any waiting follow-up covers it.
        kind: The feature's job kind; jobs of other kinds cannot cover this request.

    Returns:
        The covered local paths.
    """
    queued: set[str] = set()
    for job in jm.get_pending_jobs():
        cfg = job.config or {}
        if job.kind != kind or job.started_at is not None:
            continue
        if not cfg.get("follows_job_id") or cfg.get("force"):
            continue
        if server_pin(cfg) not in (None, server_id):
            continue
        if source is not None and sent_by_a_sender(source) and not sent_by_a_sender(cfg.get("source")):
            continue
        if configs is None:
            configs = _server_configs()
        for path in cfg.get("file_paths") or []:
            queued |= _local_candidates(str(path), configs)
    return queued


def _season_folders(paths: Iterable[str], configs: list[ServerConfig]) -> set[str]:
    """Season folders of the episode files among ``paths`` (every local candidate of each sender path)."""
    return {
        os.path.dirname(candidate)
        for path in paths
        for candidate in _local_candidates(str(path), configs)
        if ids_from_path(candidate).is_episode
    }


def _joinable_follow_ups(jm, configs: list[ServerConfig]) -> list[tuple[Job, set[str]]]:
    """Webhook follow-ups whose runner hasn't read its files yet, with the season folders they already cover.

    Call under ``FOLLOW_UP_LOCK``. Retries, verify jobs and forced re-detects keep their own file lists.
    """
    joinable = []
    for job in jm.get_pending_jobs():
        cfg = job.config or {}
        if job.kind != JOB_KIND_INTRO_CREDITS or not cfg.get("follows_job_id"):
            continue
        if cfg.get(FILES_SEALED) or cfg.get("force") or cfg.get("retry_attempt") or cfg.get("verify"):
            continue
        folders = _season_folders(cfg.get("file_paths") or [], configs)
        if folders:
            joinable.append((job, folders))
    return joinable


def _join(jm, job: Job, paths: list[str], hints: dict[str, dict[str, str]] | None) -> bool:
    """Add episodes (and their item id hints) to a waiting follow-up's config.

    Returns:
        False when the job is no longer pending (cancelled since it was listed, even between this read and the
        write), and nothing was added.
    """
    live = jm.get_job(job.id)
    if live is None:
        return False
    cfg = dict(live.config or {})
    cfg["file_paths"] = list(dict.fromkeys([*(cfg.get("file_paths") or []), *paths]))
    joined_hints = dict(cfg.get("webhook_item_id_hints") or {})
    joined_hints.update({p: h for p, h in (hints or {}).items() if p in paths})
    cfg["webhook_item_id_hints"] = joined_hints
    if not jm.update_job_config_if_pending(job.id, cfg):
        return False
    jm.update_job_library_name(job.id, f"Intro & Credits · {len(cfg['file_paths'])} files")
    return True


def submit_webhook_follow_up(
    *,
    preview_job_id: str,
    paths: list[str],
    source: str,
    item_id_hints: dict[str, dict[str, str]] | None = None,
    server_id: str | None = None,
) -> str | None:
    """Queue the Intro & Credits job that follows a webhook preview job (spec §6.4 item 9).

    Only files a server with Intro & Credits on holds are queued (the pinned server, for a pinned job), and not files a
    waiting follow-up that publishes at least as widely already lists. Vendor webhooks arrive one episode at a time: an
    episode whose season folder a waiting follow-up with the same pin already covers joins it while that job stays
    within 500 files. A new job runs at NORMAL, or at the preview job's priority when that is lower; its runner waits
    for the preview job to finish. A joined episode doesn't wait for its own preview job (markers don't need previews):
    the job it joined waits only for the preview job it was created for.

    Args:
        preview_job_id: The preview job just started for the batch.
        paths: The batch's file paths.
        source: Webhook source (``sonarr``, ``plex``…).
        item_id_hints: ``{path: {server_id: item_id}}`` from vendor webhooks.
        server_id: Publish to this server only: the preview job's pin for these files (``submit_follow_ups``).

    Returns:
        The new job's id; the joined job's id when the episodes all joined waiting follow-ups; None when nothing needed
        queueing.
    """
    if not paths or not markers_enabled_anywhere():
        return None
    owned = marker_owned_paths(list(paths), server_id)
    if not owned:
        logger.debug(
            "No server with Intro & Credits on{} holds the files of webhook job {}",
            f" that the job is pinned to ({server_id})" if server_id else "",
            preview_job_id,
        )
        return None
    configs = _server_configs()
    jm = get_job_manager()
    with FOLLOW_UP_LOCK:
        queued = _queued_in_waiting_follow_ups(jm, configs, server_id, source)
        fresh = [p for p in owned if not (_local_candidates(p, configs) & queued)]
        if not fresh:
            logger.info("Intro & Credits for webhook job {}: its files are already queued", preview_job_id)
            return None
        rest = list(fresh)
        joined_id: str | None = None
        for waiting, folders in _joinable_follow_ups(jm, configs):
            # Only a job that publishes where this one would, and whose files get the same retries and verify.
            if server_pin(waiting.config) != server_id:
                continue
            if sent_by_a_sender(waiting.config.get("source")) != sent_by_a_sender(source):
                continue
            mine = [p for p in rest if _season_folders([p], configs) & folders]
            if not mine or len(waiting.config.get("file_paths") or []) + len(mine) > MAX_RETRY_FILES:
                continue
            if not _join(jm, waiting, mine, item_id_hints):
                continue
            logger.info(
                "Intro & Credits for webhook job {}: {} episode(s) join follow-up {} of the same season",
                preview_job_id,
                len(mine),
                waiting.id[:8],
            )
            rest = [p for p in rest if p not in mine]
            joined_id = joined_id or waiting.id
        if not rest:
            return joined_id
        preview = jm.get_job(preview_job_id)
        if len(rest) == len(paths) and preview is not None and preview.library_name:
            name = f"Intro & Credits · {preview.library_name}"
        elif len(rest) == 1:
            name = f"Intro & Credits · {os.path.basename(rest[0])}"
        else:
            name = f"Intro & Credits · {len(rest)} files"
        hints = {p: h for p, h in (item_id_hints or {}).items() if p in rest}
        job = create_intro_credits_job(
            library_name=name,
            # Never ahead of the preview job it follows: with incoming jobs set to Low, previews still drain first.
            priority=max(PRIORITY_NORMAL, preview.priority) if preview is not None else PRIORITY_NORMAL,
            source=source,
            file_paths=rest,
            follows_job_id=preview_job_id,
            item_id_hints=hints or None,
            server_id=server_id,
        )
    return job.id


class _ServerLookup:
    """The part of a ``ServerRegistry`` that ``resolve_per_item_pin`` reads, over the saved server configs."""

    def __init__(self, configs: list[ServerConfig]) -> None:
        self._by_id = {cfg.id: cfg for cfg in configs}

    def get_config(self, server_id: str) -> ServerConfig | None:
        return self._by_id.get(server_id)


def submit_follow_ups(*, preview_job_id: str, items: list[ProcessableItem], source: str, pin: str | None) -> list[str]:
    """Queue the Intro & Credits follow-ups of a preview job's files, each published where the file's previews are.

    Each file's server comes from the rule the preview workers use (``jobs.worker.resolve_per_item_pin``): the job's pin
    wins; else a non-Plex server the item came from (an Emby or Jellyfin webhook, or a Recently Added listing) gets it
    alone; else every server with Intro & Credits on does. One ``submit_webhook_follow_up`` per server.

    Args:
        preview_job_id: The preview job the follow-ups wait for.
        items: Its files, with the server each came from (``ProcessableItem.server_id``) and their item id hints.
        source: What triggered the preview job (``sonarr``, ``emby``, ``recently_added``…).
        pin: The preview job's own pin (its config's ``server_id``); None when it has none.

    Returns:
        The ids of the jobs queued or joined, one per server group that needed one.
    """
    if not items or not markers_enabled_anywhere():
        return []
    from ..jobs.worker import resolve_per_item_pin

    lookup = _ServerLookup(_server_configs())
    job_config = SimpleNamespace(server_id_filter=pin or None)
    groups: dict[str | None, list[ProcessableItem]] = {}
    for item in items:
        groups.setdefault(resolve_per_item_pin(job_config, item, lookup), []).append(item)
    queued = []
    for server_id, group in groups.items():
        hints = {item.canonical_path: dict(item.item_id_by_server) for item in group if item.item_id_by_server}
        job_id = submit_webhook_follow_up(
            preview_job_id=preview_job_id,
            paths=[item.canonical_path for item in group],
            source=source,
            server_id=server_id,
            item_id_hints=hints or None,
        )
        if job_id:
            queued.append(job_id)
    return queued


def submit_pending_follow_up(preview_job_id: str, overrides: dict | None = None) -> list[str]:
    """Queue the Intro & Credits follow-up a webhook preview job asks for, and take the request off the job.

    The webhook sets ``INTRO_CREDITS_FOLLOW_UP`` in the preview job's saved config when its batch opens (a vendor
    webhook, when it creates the job), so a job revived after a restart asks too; the preview runner calls this each
    time it starts the job. The request is taken off even when queueing fails, so it's queued at most once; it stays
    when the batch took more files meanwhile (a start during the debounce), so the batch's fire queues those.

    Args:
        preview_job_id: The preview job being started.
        overrides: The start's config overrides (the fired batch's paths, pin and item ids); the saved config fills in
            the rest. Only the saved config's request counts: a stale copy of the config asks nothing.

    Returns:
        The ids of the jobs queued or joined (``submit_follow_ups``); empty when the job asked for none.
    """
    jm = get_job_manager()
    with _pending_follow_up_lock:
        job = jm.get_job(preview_job_id)
        saved = dict(job.config or {}) if job is not None else {}
        if not saved.get(INTRO_CREDITS_FOLLOW_UP):
            return []
        request = {**saved, **(overrides or {})}
        paths = [path for path in (str(p).strip() for p in request.get("webhook_paths") or []) if path]
        try:
            hints = request.get("webhook_item_id_hints") or {}
            # As the preview orchestrator builds a webhook path's item: the first server with an item id sent it.
            items = [
                ProcessableItem(
                    canonical_path=path,
                    server_id=next(iter(hints.get(path) or {}), ""),
                    item_id_by_server=dict(hints.get(path) or {}),
                )
                for path in paths
            ]
            queued = submit_follow_ups(
                preview_job_id=preview_job_id,
                items=items,
                source=str(request.get("source") or "webhook"),
                pin=server_pin(request),
            )
            _submit_loudness_follow_up(
                preview_job_id,
                queued,
                paths,
                str(request.get("source") or "webhook"),
                server_pin(request),
                defer_directories=True,
            )
            return queued
        finally:
            live = jm.get_job(preview_job_id)
            added = set((live.config or {}).get("webhook_paths") or []) - set(paths) if live is not None else set()
            if not added:
                jm.merge_job_config(preview_job_id, {}, remove=(INTRO_CREDITS_FOLLOW_UP,))


def _submit_loudness_follow_up(
    preview_job_id: str,
    intro_job_ids: list[str],
    paths: list[str],
    source: str,
    pin: str | None,
    *,
    defer_directories: bool = False,
) -> None:
    """Queue Plex loudness for eligible files enumerated by a preview run. Never raises:
    a failure here must not cost the files their Intro & Credits follow-up.

    Each job waits for its own preview and all overlapping Intro & Credits first attempts.
    """
    try:
        from ..loudness.job import create_loudness_job
        from ..loudness.settings import loudness_enabled_anywhere, loudness_matches

        configs = _server_configs()
        if not paths or not loudness_enabled_anywhere(configs):
            return
        if defer_directories:
            # Use only a type check, never traversal: the preview's existing expansion selects the children.
            # Missing explicit files stay intact for sender retries; dispatch then deduplicates actual files.
            paths = [path for path in paths if not any(os.path.isdir(p) for p in _local_candidates(path, configs))]
        if not paths:
            return
        # A known non-Plex originator can ask enabled Plex owners. A Plex pin never grants another server access.
        if pin is not None:
            pinned = next((cfg for cfg in configs if cfg.id == pin), None)
            if pinned is None:
                return
            if pinned.type is ServerType.PLEX:
                if not loudness_enabled_anywhere([pinned]):
                    return
            else:
                pin = None
        jm = get_job_manager()
        eligible = [cfg for cfg in configs if pin in (None, cfg.id)]
        paths = list(
            dict.fromkeys(
                path
                for path in paths
                if any(loudness_matches(candidate, eligible) for candidate in _local_candidates(path, configs))
            )
        )
        if not paths:
            return
        with _loudness_follow_up_lock:
            _queue_loudness_paths(jm, configs, preview_job_id, intro_job_ids, paths, source, pin, create_loudness_job)
    except Exception as exc:
        logger.warning("Couldn't queue the Plex loudness job for preview job {}: {}", preview_job_id[:8], exc)


def _queue_loudness_paths(
    jm: JobManager,
    configs: list[ServerConfig],
    preview_job_id: str,
    intro_job_ids: list[str],
    paths: list[str],
    source: str,
    pin: str | None,
    create_job: Callable[..., Job],
) -> None:
    """Persist follow-ups, deduplicating only this preview's own work under the creation lock."""
    from ..loudness.inputs import read_file_paths
    from ..loudness.job import MAX_FOLLOW_UP_DEPENDENCIES

    queued: set[str] = set()
    for earlier in jm.get_all_jobs():
        cfg = earlier.config or {}
        dependencies = [cfg.get("follows_job_id"), *(cfg.get("follows_job_ids") or [])]
        if earlier.kind == JOB_KIND_LOUDNESS and preview_job_id in dependencies and server_pin(cfg) in (None, pin):
            for path in read_file_paths(jm.config_dir, cfg):
                queued |= _local_candidates(path, configs)
    # Different previews need their own barriers: a prior pending job could start before this preview finishes.
    rest = [path for path in paths if not (_local_candidates(path, configs) & queued)]
    if not rest:
        return
    preview = jm.get_job(preview_job_id)
    if len(rest) == len(paths) and preview is not None and preview.library_name:
        name = f"Plex loudness · {preview.library_name}"
    elif len(rest) == 1:
        name = f"Plex loudness · {os.path.basename(rest[0])}"
    else:
        name = f"Plex loudness · {len(rest)} files"
    markers = {
        entry.id: entry
        for entry in [*jm.get_pending_jobs(), *jm.get_running_jobs()]
        if entry.kind == JOB_KIND_INTRO_CREDITS
    }
    # Index once, not a marker-jobs scan per file or batch. Include joined jobs omitted by submit_follow_ups.
    by_path: dict[str, set[str]] = {}
    for marker in markers.values():
        for path in (marker.config or {}).get("file_paths") or []:
            for candidate in _local_candidates(path, configs):
                by_path.setdefault(candidate, set()).add(marker.id)
    # A caller-supplied job without file metadata still has to finish; known jobs are scoped per batch below.
    common = {preview_job_id, *(mid for mid in intro_job_ids if mid not in markers)}
    batches: list[tuple[list[str], set[str]]] = []
    batch: list[str] = []
    dependencies = set(common)
    for path in rest:
        needed = set(common)
        for candidate in _local_candidates(path, configs):
            # A marker job can still hold the original directory request. Walk path components, not a string
            # prefix: /Movies must cover /Movies/a.mkv, never /Movies-other/a.mkv. No filesystem walk is needed.
            while candidate:
                needed.update(by_path.get(candidate, ()))
                parent = os.path.dirname(candidate)
                if parent == candidate:
                    break
                candidate = parent
        if len(needed) > MAX_FOLLOW_UP_DEPENDENCIES:
            # Never drop a barrier to force work through an overloaded queue.
            logger.warning("Too many preceding jobs to queue loudness for preview {}", preview_job_id[:8])
            continue
        if batch and len(dependencies | needed) > MAX_FOLLOW_UP_DEPENDENCIES:
            batches.append((batch, dependencies))
            batch, dependencies = [], set(common)
        batch.append(path)
        dependencies.update(needed)
    if batch:
        batches.append((batch, dependencies))
    for index, (batch, dependencies) in enumerate(batches, 1):
        ordered = [preview_job_id, *sorted(dependencies - {preview_job_id})]
        create_job(
            library_name=f"{name} · batch {index}/{len(batches)}" if len(batches) > 1 else name,
            priority=max(PRIORITY_NORMAL, preview.priority) if preview is not None else PRIORITY_NORMAL,
            source=source,
            file_paths=batch,
            follows_job_id=next((mid for mid in intro_job_ids if mid in dependencies), preview_job_id),
            follows_job_ids=ordered,
            server_id=pin,
        )


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
                # A job only counting down to its retry has done its forced run; its retry forces nothing.
                and not is_live_retry_chain(cfg)
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


def submit_publish_retry(path: str) -> str:
    """Queue the job that delivers an Inspector save to the servers its publish didn't write.

    The save's own publish is bounded to answer the page quickly: a server whose row came back waiting or failed (the
    file busy in a running job, the deadline, a busy Plex database, a server down or not indexed yet) gets it from
    this job instead. It is a single-file HIGH job, not forced: it publishes the saved markers and asks no source
    again; its retry chain takes a server that hasn't indexed the file yet, as any job's does. A job for the file
    that hasn't started yet (an earlier one of these, or a queued re-detect) reads the saved markers when it runs, so
    that job is returned instead. A running one isn't: it decided from the markers as they were before the save.

    Args:
        path: The file's local path, already validated by the caller.

    Returns:
        The id of the new or the reused job.
    """
    jm = get_job_manager()
    with _redetect_lock:
        for job in jm.get_pending_jobs():
            cfg = job.config or {}
            if (
                job.kind == JOB_KIND_INTRO_CREDITS
                and job.started_at is None
                and cfg.get("source") == _REDETECT_SOURCE
                and list(cfg.get("file_paths") or []) == [path]
                # A retry counting down to its due time could be an hour away.
                and not cfg.get("retry_not_before")
                and not is_live_retry_chain(cfg)
            ):
                logger.info("Intro & Credits for {} is already queued as job {}", os.path.basename(path), job.id[:8])
                return job.id
        job = create_intro_credits_job(
            library_name=f"Intro & Credits: {os.path.basename(path)}",
            priority=PRIORITY_HIGH,
            source=_REDETECT_SOURCE,
            file_paths=[path],
        )
    return job.id


def _season_job_name(episode: str, folder: str) -> str:
    """The job title: the show and the parsed season, so two seasons of one flat show folder read apart."""
    show_folder = os.path.dirname(folder) if is_season_folder(os.path.basename(folder)) else folder
    show = os.path.basename(show_folder)
    season = ids_from_path(episode).season
    if season is None:
        return f"Intro & Credits: {show}"
    return f"Intro & Credits: {show} · {'Specials' if season == 0 else f'Season {season}'}"


def submit_season_publish(episode: str) -> str:
    """Queue the Season view's "Publish": a NORMAL-priority, not forced job over the episodes of an episode's season group.

    The group is the one the Season view lists (``season_group``: the same show's season folders on every disk of the
    library, the same season number, at most the 40 nearest), so a flat folder holding several seasons sends only this
    season. Jobs publish every decided marker, so decided episodes go to every server that doesn't show them yet and
    undecided ones are checked again (owner ruling R4, at NORMAL priority since 2026-09-15). While a Publish of exactly
    these episodes is still queued or running, that job is returned instead.

    Args:
        episode: The local path of any episode of the season, already validated by the caller.

    Returns:
        The id of the new or the reused job.
    """
    group = season_group(episode, season_videos(episode, _server_configs()))
    episodes = list(group.episodes)
    jm = get_job_manager()
    # Shared with re-detect: a double-click on either Inspector button queues one job.
    with _redetect_lock:
        for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
            cfg = job.config or {}
            if (
                job.kind == JOB_KIND_INTRO_CREDITS
                and cfg.get("source") == _SEASON_PUBLISH_SOURCE
                and sorted(cfg.get("file_paths") or []) == episodes
                # A job only counting down to its retry has published; its retry lists only the files still waiting.
                and not is_live_retry_chain(cfg)
            ):
                logger.info("Publish for {} is already queued as job {}", os.path.basename(group.folder), job.id[:8])
                return job.id
        job = create_intro_credits_job(
            library_name=_season_job_name(episode, group.folder),
            priority=PRIORITY_NORMAL,
            source=_SEASON_PUBLISH_SOURCE,
            file_paths=episodes,
        )
    return job.id


def version_rerun_counts(listed: int, after: Mapping[str, object] | None = None) -> dict[str, int]:
    """Where the next batch of the re-check after an update stands: the next batch of the re-check ``after`` belongs
    to, or the first batch of a new one over the ``listed`` files.

    Args:
        listed: The files due to be read again now.
        after: The counts of the batch that just ran (``job_runner.VERSION_RERUN_COUNTS``); None for a first batch.

    Returns:
        ``{"total", "batch", "batch_size"}``.
    """
    previous = after if isinstance(after, Mapping) else {}
    try:
        total = int(previous.get("total") or 0)
        batch = int(previous.get("batch") or 0) + 1
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return {"total": listed, "batch": 1, "batch_size": BATCH_FILES}
    return {"total": total, "batch": batch, "batch_size": BATCH_FILES}


def version_rerun_job_name(counts: Mapping[str, int]) -> str:
    """The queue title of a batch of the re-check after an update.

    Args:
        counts: ``version_rerun_counts``.

    Returns:
        E.g. ``Intro & Credits: Re-checking 1,568 files after the app update · batch 1 of 16``. A batch past the count
        the first batch worked out (files still due after their batch) is its own last one.
    """
    total = int(counts["total"])
    batch = int(counts["batch"])
    batches = max(batch, math.ceil(total / max(1, int(counts["batch_size"]))))
    files = f"{total:,} file" if total == 1 else f"{total:,} files"
    return VERSION_RERUN_JOB_NAME.format(total=files, batch=batch, batches=batches)
