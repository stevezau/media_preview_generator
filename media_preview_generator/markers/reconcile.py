"""Check servers (reconcile, spec §6.2 step 6): the job that makes sure servers still show what this app published.

A normal job already reads a file's markers back before calling it up to date, and replaced files get a delayed verify
job. Check servers covers every other file, on the schedule the user sets (nothing is scheduled by default) or when
asked (``POST /api/markers/reconcile``, the dashboard's Start job dialog). It reads back each published server item
(Plex item by item under its database lock proof, Jellyfin and Emby one request per item) and runs the pipeline only
for the files of items that drifted (Plex's own forced detection, a Jellyfin rescan, an Emby refresh the plugin couldn't
heal, a Plex version added since, an item the server replaced), which publishes them again under the normal rules
(Keep Plex's / Keep Emby's, versions, consent). A drifted Plex item's current version files run too, so a file that
replaced a version this app ran gets its own decision there. It also runs decided files whose server had no markers of
its own, with a backoff, so that server's detection since can still shorten their credits (rule 7), and the files of
items whose last publish failed, on the same backoff.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple

from loguru import logger

from ..job_kinds import JOB_KIND_INTRO_CREDITS
from ..processing.types import ProcessableItem
from ..servers.base import ServerConfig
from ..web.jobs import PRIORITY_LOW, Job, get_job_manager
from .outcomes import NOT_IN_LIBRARY, ServerStatus
from .ownership import marker_matches
from .pipeline import RECHECK_AFTER, markers_for_path
from .publishers.base import UNREADABLE_IN_A_ROW, CapabilityReport, MarkerPublisher, Shown, stopped_unreadable
from .publishers.factory import publisher_for
from .settings import load_server
from .store import MarkerStore

RECONCILE_SOURCE = "reconcile"
RECONCILE_JOB_NAME = "Intro & Credits · Check servers"
# Items per read-back call, so the job's progress moves and a cancel stops between calls.
READ_BACK_BATCH = 500
# Files of a run kept for decided files to ask servers again about while more drifted files wait than a run takes.
RECHECK_SHARE = 100
# Drift whose item still exists: its current files run along with the files this app published there.
_RUN_LIVE_FILES = frozenset({Shown.VERSIONS_CHANGED, Shown.REPLACED, Shown.MISSING})

# Serialises "is a Check servers job already queued?" with its creation (a schedule tick and a click at once).
_queue_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _runnable(path: str, configs: list[ServerConfig]) -> bool:
    """Whether the pipeline can run a file for these servers: it is on disk and a library Intro & Credits goes to on one
    of them holds it.

    A file deleted since it was published, or no longer in a selected library, would only end in a "not found" or
    "skipped" row on every run, ahead of files the job can fix.
    """
    return os.path.isfile(path) and bool(marker_matches(path, configs))


@dataclass(frozen=True)
class Drift:
    """A published server item that no longer shows what this app left there."""

    server_id: str
    item_id: str
    shown: Shown
    files: tuple[str, ...]


def find_drift(
    *,
    registry: Any,
    store: MarkerStore,
    capability: Callable[[ServerConfig, MarkerPublisher], CapabilityReport] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[list[Drift], list[str]]:
    """Read back every published item on every enabled server with Intro & Credits on.

    An item the server no longer has is drift while a file of it can still run (the pipeline finds its new item);
    otherwise it is marked gone quietly (``MarkerStore.mark_item_gone``).

    Args:
        registry: The job's ``ServerRegistry``.
        store: The markers store.
        capability: The job's cached capability check (``pipeline.cached_capability``); None asks the publisher.
        cancel_check: True once the job is cancelled; items not read by then are left out.
        progress_callback: ``(items read, items on this server, message)`` after each batch.

    Returns:
        The drifted items with their local files, and job warnings (servers skipped or that failed, items that
        couldn't be read).
    """
    drifts: list[Drift] = []
    warnings: list[str] = []
    for cfg in registry.configs():
        if cancel_check and cancel_check():
            break
        settings = load_server(cfg.markers, cfg.type.value)
        if not cfg.enabled or not settings.enabled:
            continue
        rows = store.published_items(cfg.id)
        if not rows:
            continue
        name = cfg.name or cfg.id
        server = registry.get(cfg.id)
        publisher = (
            publisher_for(server, cfg, sibling_markers=lambda path: markers_for_path(store, path), ui_details=False)
            if server is not None
            else None
        )
        if publisher is None:
            warnings.append(f"Couldn't check {name}: no connection to it")
            continue
        answers: dict[str, Shown | None] = {}
        live: dict[str, tuple[str, ...]] = {}
        stopped = False
        try:
            report = capability(cfg, publisher) if capability is not None else publisher.capability()
            if not report.ready:
                warnings.append(f"Skipped {name}: {report.message or report.state.value}")
                continue
            for first in range(0, len(rows), READ_BACK_BATCH):
                if cancel_check and cancel_check():
                    break
                batch = [
                    (r.item_id, list(r.markers), r.kept_types, r.item_files)
                    for r in rows[first : first + READ_BACK_BATCH]
                ]
                read = publisher.shows_many(batch, cancel_check=cancel_check)
                answers.update(read)
                live.update({item_id: tuple(publisher.live_files(item_id)) for item_id in read})
                if stopped_unreadable(batch, read):
                    stopped = True
                    break
                if progress_callback is not None:
                    progress_callback(first + len(batch), len(rows), f"Checking {name}…")
        except Exception as exc:
            # Only the exception's type: a message can carry the server's URL or credentials.
            logger.warning("Check servers couldn't check {}: {}", name, type(exc).__name__)
            warnings.append(f"Couldn't check {name}")
            continue
        found, unreadable = _drifted(store, cfg, rows, answers, live, release_kept=not settings.keeps_server_markers)
        drifts.extend(found)
        if stopped:
            logger.warning("Check servers stopped reading {}: {} reads in a row failed", name, UNREADABLE_IN_A_ROW)
            warnings.append(f"Couldn't check {name}")
        elif unreadable:
            warnings.append(f"Couldn't read what {unreadable} item(s) show on {name}")
    return drifts, warnings


def _drifted(
    store: MarkerStore,
    cfg: ServerConfig,
    rows: list,
    answers: dict[str, Shown | None],
    live: dict[str, tuple[str, ...]],
    *,
    release_kept: bool,
) -> tuple[list[Drift], int]:
    """One server's drifted items from its read-back answers (``find_drift``), and how many items couldn't be read.

    ``live`` holds each read item's current files (``MarkerPublisher.live_files``).
    """
    name = cfg.name or cfg.id
    drifts: list[Drift] = []
    unreadable = 0
    for row in rows:
        if row.item_id not in answers:
            continue  # cancelled before this item was read
        shown = answers[row.item_id]
        if shown is None:
            unreadable += 1
            continue
        # A type kept as the server's own goes back to ours once the server is set to use ours: the pipeline writes it.
        if shown is Shown.OURS and row.kept_types and release_kept:
            shown = Shown.REPLACED
        if shown is Shown.OURS:
            continue
        files = tuple(path for path in store.files_for_item(cfg.id, row.item_id) if _runnable(path, [cfg]))
        if shown in _RUN_LIVE_FILES:
            # A version replaced by a file this app never ran (an upgrade that deleted the old file) would keep the
            # markers decided for the old file: the new file's publish replaces or removes them.
            files += tuple(path for path in live.get(row.item_id, ()) if path not in files and _runnable(path, [cfg]))
        if files:
            drifts.append(Drift(cfg.id, row.item_id, shown, files))
        elif shown is Shown.GONE:
            logger.debug("{} no longer has item {}, and no file here runs for it", name, row.item_id)
            store.mark_item_gone(cfg.id, row.item_id)
        else:
            logger.debug("{} item {}: {}, but no file here that Intro & Credits goes to runs for it", name, row.item_id,
                         shown.value)  # fmt: skip
    return drifts, unreadable


def files_to_ask_servers_again(
    *, registry: Any, store: MarkerStore, limit: int, now: datetime | None = None
) -> list[str]:
    """Take decided files whose server had no markers of its own, on the ``RECHECK_AFTER`` backoff
    (``MarkerStore.take_server_rechecks``).

    Every enabled server counts, with Intro & Credits on or not: its markers are evidence either way. A taken file the
    pipeline couldn't run (gone from disk, no longer in a library Intro & Credits goes to) is left out, and waits for
    its turn again like the rest.

    Args:
        registry: The job's ``ServerRegistry``.
        store: The markers store.
        limit: Most (file, server) pairs to take.
        now: The current time (default: now).

    Returns:
        The files' paths, sorted.
    """
    if limit <= 0:
        return []
    configs = [cfg for cfg in registry.configs() if cfg.enabled]
    taken = store.take_server_rechecks(
        [cfg.id for cfg in configs], now=now or _utcnow(), after=RECHECK_AFTER, limit=limit
    )
    return [path for path in taken if _runnable(path, configs)]


def files_of_failed_items(*, registry: Any, store: MarkerStore, limit: int, now: datetime | None = None) -> list[str]:
    """Take server items whose last publish failed, on the ``RECHECK_AFTER`` backoff (``MarkerStore.failed_items_due``),
    and list their files.

    A failed write queues no retry and leaves the item out of the read-back, so its markers would stay unchecked and
    the new decision unpublished until another job ran the file. Only servers Check servers reads back count. Items are
    taken (a retry counted) while their files fit in ``limit``; one none of whose files can run is taken all the same,
    so it stops after the last step like the rest. A failed publish from a file whose part moved to another item (a
    merge or split) marks the new item failed while the file's record still points at the old item, so the new item
    lists no file here: the file runs again through the old item's drift instead.

    Args:
        registry: The job's ``ServerRegistry``.
        store: The markers store.
        limit: Most files to list.
        now: The current time (default: now).

    Returns:
        The files' paths, sorted.
    """
    if limit <= 0:
        return []
    configs = {
        cfg.id: cfg for cfg in registry.configs() if cfg.enabled and load_server(cfg.markers, cfg.type.value).enabled
    }
    taken: list[tuple[str, str]] = []
    paths: list[str] = []
    for server_id, item_id in store.failed_items_due(list(configs), now=now or _utcnow(), after=RECHECK_AFTER):
        files = [
            path
            for path in store.files_for_item(server_id, item_id)
            if path not in paths and _runnable(path, [configs[server_id]])
        ]
        if len(paths) + len(files) > limit:
            break
        taken.append((server_id, item_id))
        paths.extend(files)
    store.record_failed_item_retries(taken)
    return sorted(paths)


@dataclass(frozen=True)
class CheckServersListing:
    """The files one Check servers run checks.

    Attributes:
        items: The files, a season's episodes together, without item id hints (a drifted item may be gone or merged,
            so each file's item is looked up again).
        warnings: Job warnings.
        drifted: Per listed file, the ``(server_id, item_id)`` drifted items it was listed for.
    """

    items: list[ProcessableItem]
    warnings: list[str]
    drifted: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict)

    def confirmed_gone_items(self, registry: Any, path: str, rows: Iterable[object]) -> set[tuple[str, str]]:
        """After a listed file ran: the drifted items it was listed for that the server confirms no longer exist, where
        the file's row for that server says "not in this server's library". Nothing is written here: the job marks them
        gone (``MarkerStore.mark_item_gone``) only once it has queued the file's retry, so a run that ends before that
        leaves them to be read back and confirmed again next run. That row can come from a lookup that failed, so an
        item the server doesn't confirm gone stays and is read back on the next run.

        Args:
            registry: The job's ``ServerRegistry``.
            path: The file.
            rows: The file's per-server rows.

        Returns:
            The ``(server_id, item_id)`` items confirmed gone.
        """
        gone: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("status") != ServerStatus.WAITING.value:
                continue
            if row.get("reason_code") != NOT_IN_LIBRARY:
                continue
            for server_id, item_id in self.drifted.get(path, ()):
                if server_id != row.get("server_id"):
                    continue
                if _confirmed_missing(registry, server_id, item_id):
                    logger.info("{} no longer has item {}", server_id, item_id)
                    gone.add((server_id, item_id))
                else:
                    logger.info(
                        "{} didn't confirm item {} is gone; Check servers reads it back again", server_id, item_id
                    )
        return gone


def _confirmed_missing(registry: Any, server_id: str, item_id: str) -> bool:
    """Whether the server itself says the item doesn't exist (a failed lookup isn't that)."""
    cfg, server = registry.get_config(server_id), registry.get(server_id)
    if cfg is None or server is None:
        return False
    try:
        publisher = publisher_for(server, cfg, ui_details=False)
        return publisher is not None and publisher.item_missing(item_id) is True
    except Exception as exc:
        logger.debug("Couldn't ask {} whether item {} exists: {}", server_id, item_id, type(exc).__name__)
        return False


def check_servers_listing(
    *,
    registry: Any,
    store: MarkerStore,
    max_files: int,
    capability: Callable[[ServerConfig, MarkerPublisher], CapabilityReport] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> CheckServersListing:
    """Check servers' files: those of drifted published items, of items whose last publish failed (on their backoff),
    then decided files to ask servers again about.

    At most ``max_files``. Drifted items take turns across runs (never listed first, then the least recently listed);
    while more drifted files wait than a run takes, ``RECHECK_SHARE`` of them are kept for the files to ask servers
    again about, which take turns too (``MarkerStore.take_server_rechecks``).

    Args:
        registry: The job's ``ServerRegistry``.
        store: The markers store.
        max_files: Most files in the run.
        capability: The job's cached capability check.
        cancel_check: True once the job is cancelled; asked between items, it is also where the job waits out a
            pause (``job_runner._cancel_check_releasing_slot_while_paused``).
        progress_callback: ``(current, total, message)`` for the job banner.

    Returns:
        The listing.
    """
    check = cancel_check or (lambda: False)
    drifts, warnings = find_drift(
        registry=registry, store=store, capability=capability, cancel_check=check, progress_callback=progress_callback
    )
    chosen, left = _take_drifts(store, drifts, max_files)
    drifted: dict[str, set[tuple[str, str]]] = {}
    for drift in chosen:
        for path in drift.files:
            drifted.setdefault(path, set()).add((drift.server_id, drift.item_id))
    if left:
        warnings.append(f"{left} more changed file(s) are checked on a later run")
    store.record_drift_listed((drift.server_id, drift.item_id) for drift in chosen)
    retried: list[str] = []
    asked_again: list[str] = []
    if not check():
        retried = files_of_failed_items(registry=registry, store=store, limit=max_files - len(drifted))
        listed = set(drifted) | set(retried)
        asked_again = files_to_ask_servers_again(registry=registry, store=store, limit=max_files - len(listed))
    logger.info(
        "Check servers: {} published item(s) changed on servers ({} file(s) this run); {} file(s) of items whose last "
        "publish failed; {} decided file(s) to ask servers again for their own markers",
        len(drifts),
        len(drifted),
        len(retried),
        len(asked_again),
    )
    paths = sorted(set(drifted) | set(retried) | set(asked_again), key=lambda p: (os.path.dirname(p), p))
    items = [
        ProcessableItem(canonical_path=p, server_id="", item_id_by_server={}, title=os.path.basename(p)) for p in paths
    ]
    return CheckServersListing(items, warnings, {path: frozenset(pairs) for path, pairs in drifted.items()})


def _take_drifts(store: MarkerStore, drifts: list[Drift], max_files: int) -> tuple[list[Drift], int]:
    """The drifted items this run lists, in turn, and how many of their files wait for a later run."""
    all_files = {path for drift in drifts for path in drift.files}
    if len(all_files) <= max_files:
        return list(drifts), 0
    listed: dict[tuple[str, str], str] = {}
    for server_id in {drift.server_id for drift in drifts}:
        ids = [drift.item_id for drift in drifts if drift.server_id == server_id]
        listed.update({(server_id, item): at for item, at in store.drift_listed_at(server_id, ids).items()})
    ordered = sorted(
        drifts, key=lambda d: ((d.server_id, d.item_id) in listed, listed.get((d.server_id, d.item_id), ""), d.files)
    )
    budget = max(1, max_files - RECHECK_SHARE)
    chosen: list[Drift] = []
    files: set[str] = set()
    for drift in ordered:
        if chosen and len(files | set(drift.files)) > budget:
            continue
        chosen.append(drift)
        files |= set(drift.files)
    return chosen, len(all_files - files)


def unfinished_reconcile_job() -> Job | None:
    """The Check servers job that is queued or running, if any."""
    jm = get_job_manager()
    for job in [*jm.get_pending_jobs(), *jm.get_running_jobs()]:
        if job.kind == JOB_KIND_INTRO_CREDITS and (job.config or {}).get("reconcile"):
            return job
    return None


class ReconcileQueued(NamedTuple):
    """What ``run_markers_reconcile`` did: the Check servers job's id (None when Intro & Credits is off everywhere) and
    whether this call created it (False: one was already queued or running)."""

    job_id: str | None
    created: bool


def run_markers_reconcile(*, priority: int = PRIORITY_LOW, parent_schedule_id: str = "") -> ReconcileQueued:
    """Queue Intro & Credits · Check servers (a schedule tick, ``POST /api/markers/reconcile``).

    Args:
        priority: The new job's priority (LOW unless the schedule or the request sets one).
        parent_schedule_id: The schedule that queued it.

    Returns:
        The job's id and whether it was created now; a Check servers job already queued or running is reused.
    """
    from .triggers import create_intro_credits_job, markers_enabled_anywhere

    if not markers_enabled_anywhere():
        logger.info("Check servers: Intro & Credits is off on every server; nothing to check")
        return ReconcileQueued(None, False)
    with _queue_lock:
        existing = unfinished_reconcile_job()
        if existing is not None:
            logger.info("Check servers is already queued as job {}", existing.id[:8])
            return ReconcileQueued(existing.id, False)
        job = create_intro_credits_job(
            library_name=RECONCILE_JOB_NAME,
            priority=priority,
            source=RECONCILE_SOURCE,
            reconcile=True,
            parent_schedule_id=parent_schedule_id,
        )
    return ReconcileQueued(job.id, True)
