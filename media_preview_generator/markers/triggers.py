"""Create Intro & Credits jobs from the API, webhook batches and schedules."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from loguru import logger

from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..servers.base import ServerConfig
from ..servers.ownership import webhook_path_candidates
from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict
from ..web.jobs import PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_NORMAL, Job, get_job_manager, is_live_retry_chain
from ..web.settings_manager import get_settings_manager
from .audio.season import season_group
from .external_ids import ids_from_path, is_season_folder
from .job_runner import (
    DECIDE_AGAIN,
    DECIDE_AGAIN_SOURCE,
    FILES_SEALED,
    FOLLOW_UP_LOCK,
    MAX_RETRY_FILES,
    ONLINE_RECHECK,
    ONLINE_RECHECK_SOURCE,
    VERSION_RERUN,
    VERSION_RERUN_SOURCE,
    start_intro_credits_job_async,
)
from .ownership import marker_matches
from .pipeline import ONLINE_SOURCES, online_recheck_files
from .settings import get_global_settings, load_server
from .store import get_marker_store
from .versions import BATCH_FILES, files_to_read_again

# Serialises Inspector re-detect's "is this file already queued?" with the job creation, so a double-click queues one
# job. Webhook follow-ups use job_runner.FOLLOW_UP_LOCK, which their runner also takes to read the files.
_redetect_lock = threading.Lock()
_REDETECT_SOURCE = "inspector"
_SEASON_PUBLISH_SOURCE = "inspector_season"
DECIDE_AGAIN_JOB_NAME = "Intro & Credits: Needs review and waiting files, decided again"
ONLINE_RECHECK_JOB_NAME = "Intro & Credits: weekly online re-check"
VERSION_RERUN_JOB_NAME = "Intro & Credits: re-checking files after an update"
ONLINE_RECHECK_EVERY = timedelta(days=7)
# The timer that queues the next weekly online re-check (``schedule_online_recheck``), replaced under its lock.
_online_recheck_timer: threading.Timer | None = None
_online_recheck_timer_lock = threading.Lock()


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


def marker_owned_paths(paths: list[str]) -> list[str]:
    """Paths held by a library that Intro & Credits goes to on an enabled server.

    Runs on webhook threads, so it reads settings only: no disk or network access.

    Args:
        paths: File paths as the webhook sender reported them.

    Returns:
        The owned paths, in input order.
    """
    configs = _server_configs()
    return [
        path
        for path in paths
        if any(marker_matches(candidate, configs) for candidate in _local_candidates(path, configs))
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
    online_recheck: bool = False,
    version_rerun: bool = False,
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
        retry_delay_s: For a retry, a verify job or a TheIntroDB recheck: seconds to wait before it takes a slot.
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
        online_recheck: List the files the online databases are due to be asked about again when the job runs
            (``job_runner._items_for_online_recheck``) instead of libraries or paths.
        version_rerun: Take the next batch of files to re-check after an update when the job runs
            (``job_runner._items_to_read_again``) instead of libraries or paths.

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
    if online_recheck:
        config[ONLINE_RECHECK] = True
    if version_rerun:
        config[VERSION_RERUN] = True
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


def _queued_in_waiting_follow_ups(jm, configs: list[ServerConfig] | None = None) -> set[str]:
    """Local paths (every candidate) of the files webhook follow-ups that have never started list.

    A follow-up that has never started reads its files when it runs, so a file already listed there is covered. A
    revived one (PENDING again, started_at kept) may already have published the file's old version.

    Args:
        jm: The job manager.
        configs: Server configs; read from settings only when a waiting follow-up needs them.

    Returns:
        The covered local paths.
    """
    queued: set[str] = set()
    for job in jm.get_pending_jobs():
        cfg = job.config or {}
        if job.kind != JOB_KIND_INTRO_CREDITS or job.started_at is not None:
            continue
        if not cfg.get("follows_job_id") or cfg.get("force"):
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
) -> str | None:
    """Queue the Intro & Credits job that follows a webhook preview job (spec §6.4 item 9).

    Only files a server with Intro & Credits on holds are queued, and not files a waiting follow-up already lists.
    Vendor webhooks arrive one episode at a time: an episode whose season folder a waiting follow-up already covers
    joins it while that job stays within 500 files. A new job runs at NORMAL, or at the preview job's priority when that
    is lower; its runner waits for the preview job to finish. A joined episode doesn't wait for its own preview job
    (markers don't need previews): the job it joined waits only for the preview job it was created for.

    Args:
        preview_job_id: The preview job just started for the batch.
        paths: The batch's file paths.
        source: Webhook source (``sonarr``, ``plex``…).
        item_id_hints: ``{path: {server_id: item_id}}`` from vendor webhooks.

    Returns:
        The new job's id; the joined job's id when the episodes all joined waiting follow-ups; None when nothing needed
        queueing.
    """
    if not paths or not markers_enabled_anywhere():
        return None
    owned = marker_owned_paths(list(paths))
    if not owned:
        logger.debug("No server with Intro & Credits on holds the files of webhook job {}", preview_job_id)
        return None
    configs = _server_configs()
    jm = get_job_manager()
    with FOLLOW_UP_LOCK:
        queued = _queued_in_waiting_follow_ups(jm, configs)
        fresh = [p for p in owned if not (_local_candidates(p, configs) & queued)]
        if not fresh:
            logger.info("Intro & Credits for webhook job {}: its files are already queued", preview_job_id)
            return None
        rest = list(fresh)
        joined_id: str | None = None
        for waiting, folders in _joinable_follow_ups(jm, configs):
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
        )
    return job.id


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

    The group is the one the Season view lists (``season_group``: same folder and season number, at most the 40 nearest),
    so a flat folder holding several seasons sends only this season. Jobs publish every decided marker, so decided
    episodes go to every server that doesn't show them yet and undecided ones are checked again (owner ruling R4, at
    NORMAL priority since 2026-09-15). While a Publish of exactly these episodes is still queued or running, that job is
    returned instead.

    Args:
        episode: The local path of any episode of the season, already validated by the caller.

    Returns:
        The id of the new or the reused job.
    """
    group = season_group(episode)
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


def submit_decide_again() -> str | None:
    """Queue the one job that decides the files in Needs review, those waiting for their item's other versions, those
    whose intro rests on season audio, and those whose intro or credits rests on an online answer and a server's own
    marker alone, again.

    Queued after the settings upgrade that removed the stricter publish rule (``upgrade._migrate_to_v16``), so the files
    it held in Needs review are published now, not only when a later job happens to list them; a file whose last
    publish waits for its item's other versions is published again with them. Queued again after the one that added
    season audio's guards (``upgrade._migrate_to_v17``), so an intro that was only a network ident or cold-open music
    is decided again, and taken off the servers, now; and after the one that reads online times on the file's clock
    (``upgrade._migrate_to_v18``), so a pair of online times from another release and a server's marker made for an
    earlier file is decided again: the marker is read again and flagged, or, from a Plex server showing our markers
    (never read back), an older version's answer stops counting. It is an ordinary Intro & Credits job at LOW
    priority: stored answers that aren't due are reused, and only what is due or from an older version is asked or read again, as on any
    run. The job lists the files when it runs (``job_runner._items_to_decide_again``); while one is queued or running,
    that job is returned instead.

    Returns:
        The job's id; None when Intro & Credits is off on every server or no file is in Needs review, waiting, or has
        an unlocked intro decided with season audio or an unlocked intro or credits decided by an online answer and a
        server's marker alone.
    """
    if not markers_enabled_anywhere():
        logger.info("Intro & Credits is off on every server; no file in Needs review is decided again")
        return None
    store = get_marker_store()
    in_review, waiting = set(store.files_in_review()), set(store.files_waiting_for_other_versions())
    season_audio = set(store.files_with_season_audio_intro())
    online_and_server = set(store.files_decided_by_online_and_server_markers())
    if not in_review | waiting | season_audio | online_and_server:
        logger.info(
            "No file is in Needs review, waiting for its item's other versions, or has an intro from season audio or "
            "from an online answer and a server's marker; nothing to decide again"
        )
        return None
    jm = get_job_manager()
    with _redetect_lock:
        for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
            cfg = job.config or {}
            if job.kind == JOB_KIND_INTRO_CREDITS and cfg.get(DECIDE_AGAIN) and not is_live_retry_chain(cfg):
                logger.info("The files in Needs review are already queued as job {}", job.id[:8])
                return job.id
        job = create_intro_credits_job(
            library_name=DECIDE_AGAIN_JOB_NAME,
            priority=PRIORITY_LOW,
            source=DECIDE_AGAIN_SOURCE,
            decide_again=True,
        )
    logger.info(
        "{} file(s) in Needs review, {} waiting for their item's other versions and {} with an intro from season audio "
        "are decided again (job {})",
        len(in_review),
        len(waiting - in_review),
        len(season_audio - in_review - waiting),
        job.id[:8],
    )
    return job.id


def submit_version_reruns(delay_s: int = 0) -> str | None:
    """Queue the job that reads again the next batch of files whose answers rest on an older detector version.

    Called on every start (``web.app._read_again_after_detector_updates``) and when a batch has run
    (``job_runner._queue_next_batch``, with ``versions.BATCH_GAP`` as ``delay_s``). It is an ordinary Intro & Credits
    job at LOW priority, behind previews: it takes at most ``versions.BATCH_FILES`` files still on disk when it runs,
    and a run asks again only what is due or from an older version, as on any run. While one waits or runs, that job is
    returned instead.

    Args:
        delay_s: Seconds the job waits before it takes a slot (the gap between batches).

    Returns:
        The job's id; None when Intro & Credits is off on every server or no file is left to read again.
    """
    if not markers_enabled_anywhere():
        logger.info("Intro & Credits is off on every server; no file is read again for an updated detector")
        return None
    due = files_to_read_again(get_marker_store(), get_global_settings())
    if not due:
        logger.debug("No file has an answer from an older detector version or older decision rules to read again")
        return None
    jm = get_job_manager()
    with _redetect_lock:
        for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
            cfg = job.config or {}
            if job.kind == JOB_KIND_INTRO_CREDITS and cfg.get(VERSION_RERUN) and not is_live_retry_chain(cfg):
                return job.id
        job = create_intro_credits_job(
            library_name=VERSION_RERUN_JOB_NAME,
            priority=PRIORITY_LOW,
            source=VERSION_RERUN_SOURCE,
            version_rerun=True,
            retry_delay_s=int(delay_s),
        )
    by_detector: dict[str, int] = {}
    for taken in due.values():
        for detector in taken:
            by_detector[detector] = by_detector.get(detector, 0) + 1
    logger.info(
        "{} file(s) rest on an answer from an older detector version, were decided under older rules, or show times an "
        "older publish rule kept ({}); "
        "read again {} at a time (job {})",
        len(due),
        ", ".join(f"{detector} {count}" for detector, count in sorted(by_detector.items())),
        BATCH_FILES,
        job.id[:8],
    )
    return job.id


def _queued_online_recheck(jm) -> Job | None:
    """The weekly online re-check waiting to run or running, if any (call under ``FOLLOW_UP_LOCK``)."""
    for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
        if job.kind == JOB_KIND_INTRO_CREDITS and (job.config or {}).get(ONLINE_RECHECK):
            return job
    return None


def submit_online_recheck() -> str | None:
    """Queue the weekly job that asks the online databases again about files they had no entry for.

    It lists the files where an enabled online source's "no entry" is due again (older than ``NO_DATA_RETRY``) and whose
    decision it could still change (``pipeline.online_recheck_files``). It is an ordinary Intro & Credits job at LOW
    priority: stored answers that aren't due are reused, so only the due lookups happen, and a file is read again only
    when a detector answer of its own is due. TheIntroDB's daily budget and its per-show pause apply as on any run; a
    file its budget refused goes to the TheIntroDB recheck. The job lists the files when it runs; while one waits to
    run or runs, that job is returned instead.

    Returns:
        The job's id; None when Intro & Credits is off on every server, every online source is off, or no file is due.
    """
    if not markers_enabled_anywhere():
        logger.info("Intro & Credits is off on every server; the weekly online re-check is skipped")
        return None
    settings = get_global_settings()
    if not any(settings.source_enabled(source.value) for source in ONLINE_SOURCES):
        logger.info("Every online database is turned off; the weekly online re-check is skipped")
        return None
    # Stops at the first file due: the job lists them all when it runs.
    if not any(True for _path in online_recheck_files(get_marker_store(), settings, _utcnow())):
        logger.info("No file is due to be asked again online; the weekly online re-check is skipped")
        return None
    jm = get_job_manager()
    # The TheIntroDB recheck's lock: at most one re-check queued, like it.
    with FOLLOW_UP_LOCK:
        queued = _queued_online_recheck(jm)
        if queued is not None:
            logger.info("The weekly online re-check is already queued or running as job {}", queued.id[:8])
            return queued.id
        job = create_intro_credits_job(
            library_name=ONLINE_RECHECK_JOB_NAME,
            priority=PRIORITY_LOW,
            source=ONLINE_RECHECK_SOURCE,
            online_recheck=True,
        )
    logger.info("The weekly online re-check is queued (job {})", job.id[:8])
    return job.id


def schedule_online_recheck() -> None:
    """Arm the timer that queues the weekly online re-check when it is due. Never raises.

    The due time is kept in markers.db, so a restart doesn't put it off; the first start sets it a week ahead. One that
    passed while the app was down, or that can't be read (not a time, or one without a time zone), fires at once; one
    further off than a week (a clock set back) is brought to a week from now.
    """
    global _online_recheck_timer
    try:
        store = get_marker_store()
        now = _utcnow()
        try:
            due = store.online_recheck_due()
        except ValueError:
            due = now
        if due is None:
            due = now + ONLINE_RECHECK_EVERY
            store.set_online_recheck_due(due)
        elif due.tzinfo is None:
            due = now
        due = min(due, now + ONLINE_RECHECK_EVERY)
        delay = max(0.0, (due - now).total_seconds())
    except Exception as exc:
        logger.warning(
            "Couldn't schedule the weekly Intro & Credits online re-check ({}: {}); the next start tries again",
            type(exc).__name__,
            exc,
        )
        return
    timer = threading.Timer(delay, _run_online_recheck)
    timer.daemon = True
    timer.name = "markers-online-recheck"
    with _online_recheck_timer_lock:
        if _online_recheck_timer is not None:
            _online_recheck_timer.cancel()
        _online_recheck_timer = timer
        timer.start()
    logger.debug("The weekly Intro & Credits online re-check is due {}", due.isoformat())


def _run_online_recheck() -> None:
    """The timer's run: set the next re-check a week later, queue this one, and arm the next. Never raises.

    The next due time is stored first, so a ``schedule_online_recheck`` while this run queues the job arms the next
    week's, not this one again.
    """
    try:
        get_marker_store().set_online_recheck_due(_utcnow() + ONLINE_RECHECK_EVERY)
    except Exception as exc:
        # Queued and armed again, the old due time would fire at once, and again: the next start schedules it instead.
        logger.warning(
            "Couldn't store when the next weekly online re-check is due ({}: {}); the next start schedules it",
            type(exc).__name__,
            exc,
        )
        return
    try:
        submit_online_recheck()
    except Exception:
        logger.exception("Couldn't queue the weekly Intro & Credits online re-check")
    schedule_online_recheck()
