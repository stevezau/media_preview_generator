"""Files missing from disk: marked in markers.db (``files.missing_since``), never deleted, and cleared when seen again.

A series deleted whole (Sonarr removing a show) takes its season folders with it, so the folder rule the fingerprint
sweep uses can't tell it from a disk that isn't mounted. Here a file counts as missing only when nothing is at its path,
not even a symlink (a library of links into a remote mount that dropped keeps them, dangling), every disk root it lies
under (its library's folder, its path mapping's local folder) is there and holds entries, and the nearest folder above
it that still exists holds entries too (``fs.gone_from_disk`` with ``trust_roots``). A root that is missing, empty or
unreadable (a stale handle) keeps the file unmarked, and so does a file under no library.

Even then the file is only marked: a mergerfs pool with a branch down, or a bind mount showing a stale underlay, looks
exactly like a deletion from here. Nothing stored about the file goes (decisions, locked and edited markers, publish
records), and the mark comes off as soon as the file is seen again: any job that finds it on disk, a new file stored at
its path, or the sweep below. Marked files are left out of what lists files to work on (Needs review, the decide-again
job, Check servers' re-reads of servers' own markers and undelivered locks).

Three places mark: a job's run of a file it finds missing (holding that file's run lock), the decide-again job before
it lists its files, and a sweep on the fingerprint sweep's thread and schedule, which also clears the marks of files
that are back. The last two skip a file a job is running (``locks.FILE_RUN_LOCKS``).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

from loguru import logger

from ..servers.ownership import path_mapping_candidates
from .fs import gone_from_disk
from .locks import FILE_RUN_LOCKS

if TYPE_CHECKING:
    from ..servers.base import ServerConfig
    from .store import FileRecord, MarkerStore

# The INFO line each pass that marks files logs (``logger.info(MISSING_LINE, count)``).
MISSING_LINE = "{} files are missing from disk; they're hidden from Needs review until they come back"
# How long one file's check may take on a job's thread. A stalled network share blocks a stat instead of failing, so a
# check that doesn't answer in time leaves the file unmarked.
CHECK_TIMEOUT_S = 5.0
# How long the decide-again job checks its listed files before listing the rest as they are.
LIST_BUDGET_S = 60.0
# Files one sweep checks, from where the last one stopped, and the time after which it checks no further file.
MAX_SWEEP_CHECKS = 2_000
SWEEP_BUDGET_S = 60.0
_monotonic = time.monotonic


def disk_roots(canonical_path: str, configs: Iterable[ServerConfig]) -> tuple[str, ...]:
    """The disk roots a local file lies under: each enabled server's library folder holding it, and the path mapping
    folder that library folder came from.

    Args:
        canonical_path: Local path of the file.
        configs: The servers' configs.

    Returns:
        The roots, each once, in server and library order; empty when no library holds the file. ``/`` is never one:
        it holds entries whatever is mounted.
    """
    roots: list[str] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        mappings = list(cfg.path_mappings or [])
        for library in cfg.libraries:
            for remote in library.remote_paths:
                if not (remote or "").strip():
                    continue
                for local, mapping_root in path_mapping_candidates(remote, mappings):
                    folder = local.rstrip("/")
                    if folder and canonical_path.startswith(folder + "/"):
                        roots.extend(root for root in (mapping_root, folder) if root and root != "/")
    return tuple(dict.fromkeys(roots))


def _missing(path: str, configs: Sequence[ServerConfig], folders: dict[str, bool]) -> bool:
    roots = disk_roots(path, configs)
    return bool(roots) and gone_from_disk([path], folders, roots={path: roots}, trust_roots=True)


def _on_disk(path: str) -> bool:
    """Whether something is at the path, a dangling symlink included (a read that fails with another error says no)."""
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def _check(store: MarkerStore, rec: FileRecord, configs: Sequence[ServerConfig], folders: dict[str, bool]) -> str:
    """Mark or clear one file as its disk says now, unless a job is running it.

    ``rec`` is the row as read before the disk is checked: a job that stores the file again meanwhile (an upgrade
    landing) changes its identity or clears its mark, and marking it then does nothing.

    Returns:
        "marked", "cleared" or "" (nothing changed).
    """
    with FILE_RUN_LOCKS.try_hold(rec.canonical_path, 0) as taken:
        if not taken:
            return ""
        if rec.missing_since is None:
            if _missing(rec.canonical_path, configs, folders) and store.mark_missing(rec):
                return "marked"
        elif _on_disk(rec.canonical_path) and store.clear_missing(rec.id):
            return "cleared"
    return ""


def _log_marked(marked: int) -> None:
    if marked:
        logger.info(MISSING_LINE, marked)


def _within(work: Callable[[threading.Event], None], timeout_s: float) -> None:
    """Run ``work`` on a helper thread for at most ``timeout_s`` (a stalled share blocks a stat rather than fail); it is
    told to stop (the event) when the time is up, and checks it between files. A failure is logged at DEBUG."""
    stop = threading.Event()

    def run() -> None:
        try:
            work(stop)
        except Exception as exc:
            logger.debug("Couldn't tell whether files are missing from disk: {}", type(exc).__name__)

    worker = threading.Thread(target=run, name="markers-missing-check", daemon=True)
    worker.start()
    worker.join(timeout_s)
    stop.set()


def mark_if_missing(store: MarkerStore, rec: FileRecord | None, configs: Sequence[ServerConfig]) -> bool:
    """Mark a file a job found missing, when its disk says it is gone (see the module docstring). Never raises.

    The caller holds the file's run lock (``pipeline`` runs every file under it) and read ``rec`` before this checks
    the disk, which takes at most ``CHECK_TIMEOUT_S``.

    Args:
        store: The markers store.
        rec: The file's row; None (never stored) and a row already marked are left alone.
        configs: The servers' configs (for its disk roots).

    Returns:
        True when it was marked now.
    """
    if rec is None or rec.missing_since is not None:
        return False
    answer: list[bool] = []
    _within(lambda _stop: answer.append(_missing(rec.canonical_path, configs, {})), CHECK_TIMEOUT_S)
    try:
        return bool(answer and answer[0]) and store.mark_missing(rec)
    except Exception as exc:
        logger.warning("Couldn't mark a file missing from disk in markers.db: {}", exc)
        return False


def mark_missing_files(store: MarkerStore, paths: Iterable[str], configs: Sequence[ServerConfig]) -> int:
    """Mark the files among ``paths`` that are missing from disk, checking for at most ``LIST_BUDGET_S``, and log how
    many. A file a job is running is skipped. Never raises.

    Args:
        store: The markers store.
        paths: Local paths of files markers.db knows.
        configs: The servers' configs (for their disk roots).

    Returns:
        How many files were marked.
    """
    marked: list[str] = []

    def work(stop: threading.Event) -> None:
        folders: dict[str, bool] = {}
        for path in sorted(set(paths)):
            if stop.is_set():
                return
            rec = store.get_file(path)
            if rec is not None and _check(store, rec, configs, folders) == "marked":
                marked.append(path)

    _within(work, LIST_BUDGET_S)
    _log_marked(len(marked))
    return len(marked)


def sweep_missing_files(
    store: MarkerStore,
    configs: Sequence[ServerConfig],
    *,
    limit: int = MAX_SWEEP_CHECKS,
    budget_s: float = SWEEP_BUDGET_S,
) -> int:
    """Mark the files among the next ``limit`` of markers.db that are missing from disk, clear the marks of those that
    are back, and log how many were marked.

    Runs on the fingerprint sweep's own thread (a stalled hard-mounted share blocks its stats without an error): files
    are checked in id order until ``budget_s`` has passed, and the next sweep goes on from the first file left unchecked.

    Args:
        store: The markers store.
        configs: The servers' configs (for their disk roots).
        limit: Most files to check.
        budget_s: Time after which no further file is checked.

    Returns:
        How many files were marked.
    """
    deadline = _monotonic() + budget_s
    folders: dict[str, bool] = {}
    changes = {"marked": 0, "cleared": 0, "": 0}
    checked_up_to = None
    for rec in store.file_checks(limit):
        if _monotonic() >= deadline:
            break
        changes[_check(store, rec, configs, folders)] += 1
        checked_up_to = rec.id
    if checked_up_to is not None:
        store.finish_file_checks(checked_up_to)
    _log_marked(changes["marked"])
    if changes["cleared"]:
        logger.debug("{} files that were missing from disk are back", changes["cleared"])
    return changes["marked"]
