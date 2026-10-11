"""Single-flight background runner for the library health check and the Plex re-read."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from loguru import logger

from ..servers.base import ServerConfig, ServerType
from ..servers.registry import ServerRegistry
from . import embyish as embyish_counter
from . import plex as plex_counter
from .markers_db import markers_db_path as default_markers_db_path
from .markers_db import nothing_found
from .models import CellState, CheckCancelled, CheckProgress, ServerResult
from .store import HealthStore, default_store

REREAD_INTERVAL_S = 0.5  # Plex analyzes before it answers; the pause leaves it room for everything else
FILES_PER_JOB = 1000  # the same cap the page uses before it switches to a whole-library job

_BUSY_MESSAGE = "A check or re-read is already running"
_NO_REGISTRY_MESSAGE = "Could not read your media servers"


@dataclass(frozen=True)
class RereadRefusal:
    """Why a re-read could not start, with the HTTP status the route should answer with."""

    message: str
    status: int


class HealthRunner:
    """Runs at most one check or re-read at a time, on a daemon thread."""

    def __init__(
        self,
        store: HealthStore,
        *,
        registry_factory: Callable[[], ServerRegistry | None],
        markers_db_path: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
        start_files_job: Callable[[ServerConfig, list[str], str], None] | None = None,
    ) -> None:
        """Create a runner.

        Args:
            store: Where finished results are saved.
            registry_factory: Builds the live server registry, or returns None when it can't be read.
            markers_db_path: Returns the path of the markers database.
            sleep: Pause function used between re-read calls (replaced in tests).
            start_files_job: Starts a preview job for ``(server, paths, title)``; used after a re-read to put
                back the chapter thumbnails Plex drops when it re-reads an item. None skips that.
        """
        self._store = store
        self._registry_factory = registry_factory
        self._markers_db_path = markers_db_path
        self._sleep = sleep
        self._start_files_job = start_files_job
        # _lock guards every field below it; counters call back from the worker thread.
        self._lock = threading.Lock()
        self._current: CheckProgress | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._last_error = ""
        self._server_errors: dict[str, str] = {}
        self._server_labels: dict[str, tuple[str, str]] = {}  # server id -> (name, type)

    # ------------------------------------------------------------ public API
    def start_check(self, reason: str = "manual") -> tuple[bool, CheckProgress]:
        """Start a check of every enabled server unless one is already running.

        Returns:
            ``(True, progress)`` when started, ``(False, current progress)`` when busy.
        """
        with self._lock:
            if self._current is not None:
                return False, replace(self._current)
            progress = self._begin("check", reason)
            self._launch(lambda: self._check_servers(None, reason=reason, registry=None))
            return True, replace(progress)

    def start_reread(self, server_id: str) -> tuple[bool, CheckProgress | RereadRefusal]:
        """Ask Plex to re-read every item whose previews exist but aren't showing.

        Returns:
            ``(True, progress)`` when started, otherwise ``(False, refusal)``.
        """
        busy = RereadRefusal(_BUSY_MESSAGE, 409)
        if self.progress() is not None:
            return False, busy
        registry = self._registry_factory()
        if registry is None:
            return False, RereadRefusal(_NO_REGISTRY_MESSAGE, 400)
        cfg = next((c for c in registry.configs() if c.id == server_id), None)
        server = registry.get(server_id)
        if cfg is None or server is None:
            return False, RereadRefusal("That server isn't available", 404)
        if cfg.type is not ServerType.PLEX:
            return False, RereadRefusal("Only Plex servers can be asked to re-read", 400)
        paths = self._store.reread_paths(server_id)
        item_ids = list(dict.fromkeys(item_id for item_id, _library in self._store.reread_items(server_id)))
        if not item_ids:
            return False, RereadRefusal("Nothing to re-read", 400)

        with self._lock:
            if self._current is not None:
                return False, busy
            progress = self._begin("reread", "manual")
            progress.server_name = cfg.name
            self._launch(lambda: self._reread(registry, cfg, server, item_ids, paths))
            return True, replace(progress)

    def cancel(self) -> bool:
        """Ask the running check or re-read to stop. Returns False when nothing is running."""
        with self._lock:
            if self._current is None:
                return False
            self._current.cancelled = True
            self._cancel.set()
            return True

    def progress(self) -> CheckProgress | None:
        """A copy of the live progress, or None when idle."""
        with self._lock:
            return replace(self._current) if self._current is not None else None

    def last_error(self) -> str:
        """Why the latest run could not start or finish; empty when it went fine."""
        with self._lock:
            return self._last_error

    def server_error(self, server_id: str) -> str:
        """The latest run's failure for one server; empty if it succeeded or wasn't checked."""
        with self._lock:
            return self._server_errors.get(server_id, "")

    def failed_servers(self) -> list[tuple[str, str, str, str]]:
        """``(server id, name, type, error)`` for every server whose latest check failed."""
        with self._lock:
            return [
                (server_id, *self._server_labels.get(server_id, (server_id, "")), error)
                for server_id, error in self._server_errors.items()
            ]

    def wait(self, timeout: float | None = None) -> None:
        """Block until the running work finishes (used by tests)."""
        with self._lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ------------------------------------------------------------ internals
    def _begin(self, kind: str, reason: str) -> CheckProgress:
        """Claim the single slot. Caller must hold ``_lock`` and have verified it is free."""
        self._cancel.clear()
        self._last_error = ""
        self._current = CheckProgress(kind=kind, started_at=time.time(), reason=reason)
        return self._current

    def _launch(self, body: Callable[[], None]) -> None:
        """Start the worker thread. Caller must hold ``_lock``; frees the slot if the thread can't start."""

        def run() -> None:
            try:
                body()
            except CheckCancelled:
                logger.info("Library health run cancelled")
            except Exception as exc:
                logger.exception("Library health run failed")
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    self._current = None

        thread = threading.Thread(target=run, name="library-health", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._current = None
            raise
        self._thread = thread

    def _is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def _report(self, step: str, done: int, total: int) -> None:
        with self._lock:
            if self._current is not None:
                self._current.step = step
                self._current.done = done
                self._current.total = total

    def _set_server_name(self, name: str) -> None:
        with self._lock:
            if self._current is not None:
                self._current.server_name = name
                self._current.step = ""
                self._current.done = 0
                self._current.total = 0

    def _check_servers(self, server_ids: list[str] | None, *, reason: str, registry: ServerRegistry | None) -> None:
        """Count the given servers (all enabled ones when None) and save what succeeded."""
        if registry is None:
            registry = self._registry_factory()
        if registry is None:
            with self._lock:
                self._last_error = _NO_REGISTRY_MESSAGE
            return
        found = nothing_found(self._markers_db_path())
        configs = [c for c in registry.configs() if c.enabled and (server_ids is None or c.id in server_ids)]
        enabled_ids = {c.id for c in registry.configs() if c.enabled}
        with self._lock:
            # A server that was removed or disabled must not keep a card of its own.
            for server_id in [sid for sid in self._server_errors if sid not in enabled_ids]:
                del self._server_errors[server_id]
                self._server_labels.pop(server_id, None)

        for cfg in configs:
            if self._is_cancelled():
                raise CheckCancelled
            self._set_server_name(cfg.name)
            self._check_one(registry, cfg, found)
        # Enabled ids, not just the ones checked now, so a limited check keeps other servers' rows.
        self._store.remove_missing_servers(enabled_ids)

    def _check_one(self, registry: ServerRegistry, cfg: ServerConfig, found: dict) -> None:
        """Count one server; a failure is remembered for that server and leaves its saved rows alone."""
        server = registry.get(cfg.id)
        started = time.monotonic()
        try:
            if server is None:
                raise RuntimeError("This server type isn't supported")
            if cfg.type is ServerType.PLEX:
                result: ServerResult = plex_counter.count_plex(
                    cfg, server, nothing_found=found, cancel_check=self._is_cancelled, progress=self._report
                )
            else:
                result = embyish_counter.count_embyish(
                    cfg, server, nothing_found=found, cancel_check=self._is_cancelled, progress=self._report
                )
            self._carry_forward_errors(result)
            result.checked_at = time.time()
            result.duration_s = time.monotonic() - started
            self._store.replace_server(result)
            if _every_countable_cell_failed(result):
                self._remember_failure(cfg, _first_error_reason(result))
            else:
                self._forget_failure(cfg)
        except CheckCancelled:
            raise
        except Exception as exc:
            logger.exception("Library health check failed for {}", cfg.name)
            self._remember_failure(cfg, str(exc) or type(exc).__name__)

    def _forget_failure(self, cfg: ServerConfig) -> None:
        with self._lock:
            self._server_errors.pop(cfg.id, None)
            self._server_labels.pop(cfg.id, None)

    def _remember_failure(self, cfg: ServerConfig, message: str) -> None:
        with self._lock:
            self._server_errors[cfg.id] = message
            self._server_labels[cfg.id] = (cfg.name, cfg.type.value)

    def _carry_forward_errors(self, result: ServerResult) -> None:
        """Keep the last good numbers and to-do rows for every cell that errored this time.

        The cell stays in the ERROR state with its new reason, so the page shows both.
        """
        previous = next((s for s in self._store.load() if s.server_id == result.server_id), None)
        if previous is None:
            return
        old_libraries = {lib.library_id: lib for lib in previous.libraries}
        for library in result.libraries:
            old_library = old_libraries.get(library.library_id)
            if old_library is None:
                continue
            for feature, cell in library.cells.items():
                old_cell = old_library.cells.get(feature)
                if cell.state is not CellState.ERROR or old_cell is None or old_cell.total <= 0:
                    continue
                if old_cell.state not in (CellState.COUNTED, CellState.ERROR):
                    continue
                cell.total = old_cell.total
                cell.done = old_cell.done
                cell.nothing_found = old_cell.nothing_found
                cell.not_showing = old_cell.not_showing
                library.total = max(library.total, old_library.total)
                result.todo.extend(self._store.todo_for(result.server_id, library.library_id, feature))

    def _reread(
        self,
        registry: ServerRegistry,
        cfg: ServerConfig,
        server: object,
        item_ids: list[str],
        paths: dict[str, list[str]],
    ) -> None:
        """Send Plex an analyze request per item, slowly, put chapter thumbnails back, then re-check that server."""
        step = "Asking Plex to re-read"
        total = len(item_ids)
        self._report(step, 0, total)
        plex = server._connect()  # type: ignore[attr-defined]
        failures = 0
        sent: list[str] = []
        for index, item_id in enumerate(item_ids):
            if index:
                self._sleep(REREAD_INTERVAL_S)
            if self._is_cancelled():
                logger.info("Plex re-read cancelled after {} of {} items", index, total)
                # Plex was already asked about these, so they are no longer "not showing" as far as we know.
                self._store.remove_not_showing(cfg.id, sent)
                self._restore_chapters(cfg, sent, paths)
                return
            if not item_id.isdecimal():
                logger.warning("Skipping re-read of non-numeric Plex item id {!r}", item_id)
                failures += 1
            else:
                try:
                    plex.query(f"/library/metadata/{item_id}/analyze", method=plex._session.put)
                    sent.append(item_id)
                except Exception as exc:
                    failures += 1
                    logger.warning("Plex re-read failed for item {}: {}", item_id, exc)
            self._report(step, index + 1, total)
        logger.info("Asked Plex to re-read {} items ({} failed)", total, failures)
        self._restore_chapters(cfg, sent, paths)
        self._check_servers([cfg.id], reason="after_reread", registry=registry)

    def _restore_chapters(self, cfg: ServerConfig, item_ids: list[str], paths: dict[str, list[str]]) -> None:
        """Re-read drops the chapter thumbnails the app registered, so a preview job registers them again.

        The previews already exist, so the job skips them and only redoes chapter thumbnails.
        """
        if self._start_files_job is None or cfg.output.get("chapter_thumbnails") is not True:
            return
        files = list(dict.fromkeys(path for item_id in item_ids for path in paths.get(item_id, ())))
        if not files:
            return
        try:
            self._start_files_job(cfg, files, "Chapter thumbnails after Plex re-read")
        except Exception:
            logger.exception("Could not start the job that puts back chapter thumbnails after the Plex re-read")


def _countable_cells(result: ServerResult) -> list:
    """Cells the check tried to count: counted ones and ones that failed."""
    return [
        cell
        for library in result.libraries
        for cell in library.cells.values()
        if cell.state in (CellState.COUNTED, CellState.ERROR)
    ]


def _every_countable_cell_failed(result: ServerResult) -> bool:
    cells = _countable_cells(result)
    return bool(cells) and all(cell.state is CellState.ERROR for cell in cells)


def _first_error_reason(result: ServerResult) -> str:
    return next((c.reason for c in _countable_cells(result) if c.reason), "Could not read this server")


_runner: HealthRunner | None = None
_runner_lock = threading.Lock()


def _settings_registry() -> ServerRegistry | None:
    """Build the live registry from the saved ``media_servers`` setting; None when it can't be read."""
    from ..web.settings_manager import get_settings_manager

    try:
        raw_servers = list(get_settings_manager().get("media_servers") or [])
        return ServerRegistry.from_settings(raw_servers)
    except Exception as exc:
        logger.warning("Library health could not build the media-server registry ({}: {})", type(exc).__name__, exc)
        return None


def get_runner() -> HealthRunner:
    """The process-wide runner, wired to the default store, saved servers and markers database."""
    global _runner
    with _runner_lock:
        if _runner is None:
            _runner = HealthRunner(
                default_store(),
                registry_factory=_settings_registry,
                markers_db_path=default_markers_db_path,
                start_files_job=start_files_job,
            )
        return _runner


def start_files_job(cfg: ServerConfig, paths: list[str], title: str) -> None:
    """Start preview jobs for these files on this server, at most ``FILES_PER_JOB`` files each."""
    from ..web.jobs import PRIORITY_LOW, get_job_manager
    from ..web.routes.job_runner import _start_job_async

    for start in range(0, len(paths), FILES_PER_JOB):
        chunk = paths[start : start + FILES_PER_JOB]
        overrides = {"webhook_paths": chunk, "force_generate": False, "server_id": cfg.id}
        job = get_job_manager().create_job(
            library_name=f"{title}: {len(chunk):,} files",
            config={"webhook_paths": chunk, "force_generate": False},
            priority=PRIORITY_LOW,
            server_id=cfg.id,
            server_name=cfg.name,
            server_type=cfg.type.value,
        )
        _start_job_async(job.id, overrides)


def nightly_check() -> None:
    """Scheduler entry point; logs instead of raising so APScheduler never sees a failure."""
    try:
        get_runner().start_check("nightly")
    except Exception:
        logger.exception("Nightly library health check could not start")
