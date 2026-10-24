"""Durable per-file completion records, independent of capped display history."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from loguru import logger

from ..job_kinds import ItemOutcome
from ..jobs.checkpoints import checkpoint_items, item_descriptor
from ..jobs.orchestrator import fold_publisher_rows_into_aggregate
from ..processing.types import ProcessableItem
from .chain import previous_result
from .plex_db import SourceChangedError, SourceFingerprint

_SUCCESS = frozenset({"loudness_written", "loudness_up_to_date"})
# A revived job re-checks its saved files this many at a time, in groups of ``RESTORE_CHUNK``.
RESTORE_CHECK_THREADS = 8
RESTORE_CHUNK = 64


def _valid_servers(rows: object) -> bool:
    if not isinstance(rows, list):
        return False
    ids = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("server_id"), str)
            or not row["server_id"]
            or not isinstance(row.get("server_name", ""), str)
            or row.get("server_type") != "plex"
            or not isinstance(row.get("status"), str)
            or row["status"] not in _SUCCESS
            or not isinstance(row.get("message", ""), str)
            or row["server_id"] in ids
        ):
            return False
        ids.add(row["server_id"])
    return True


def _path(config_dir: str | Path, job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise ValueError("Invalid loudness resume job ID")
    return Path(config_dir) / "job_loudness_resume" / f"{job_id}.sqlite3"


def delete_ledger(config_dir: str | Path, job_id: str) -> None:
    """Remove completion records once the owning job can no longer resume."""
    path = _path(config_dir, job_id)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def delete_unneeded_ledgers(config_dir: str | Path, needed_job_ids: set[str], *, idle_seconds: float = 600) -> int:
    """Remove ledgers of jobs that can no longer resume, such as finished ones from before ledgers were cleaned up.

    A ledger written to within ``idle_seconds`` is left alone: its job may still be settling its last attempt.

    Args:
        config_dir: App config directory.
        needed_job_ids: Jobs that may still run or be revived; their ledgers are kept.
        idle_seconds: How long a ledger must have been untouched to be removed.

    Returns:
        How many ledgers were removed.
    """
    folder = Path(config_dir) / "job_loudness_resume"
    if not folder.is_dir():
        return 0
    cutoff = time.time() - idle_seconds
    removed = 0
    for entry in folder.glob("*.sqlite3"):
        job_id = entry.name.removesuffix(".sqlite3")
        if job_id in needed_job_ids:
            continue
        try:
            newest = max(
                Path(f"{entry}{suffix}").stat().st_mtime for suffix in ("", "-wal") if Path(f"{entry}{suffix}").exists()
            )
            if newest > cutoff:
                continue
            delete_ledger(config_dir, job_id)
            removed += 1
        except (OSError, ValueError) as exc:
            logger.debug("Could not remove loudness continuation {}: {}", entry.name, exc)
    return removed


def source_fingerprint(item: ProcessableItem) -> SourceFingerprint | None:
    """Capture source identity without converting a missing file into a job error."""
    try:
        return SourceFingerprint.read(item.canonical_path)
    except SourceChangedError:
        return None


class CompletionLedger:
    """Commit a settled file before its dispatcher can credit completion.

    A saved row is only a candidate for resume. Current source identity and
    native Plex readiness must both be verified before it can be carried.
    """

    def __init__(self, config_dir: str | Path, job_id: str) -> None:
        path = _path(config_dir, job_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._accepted: set[str] = set()
        self.error: str | None = None
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("CREATE TABLE IF NOT EXISTS completions (path TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS selection (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            self._conn.commit()
        except BaseException:
            self._conn.close()
            raise

    def selection(self) -> tuple[list[ProcessableItem], list[str], dict[str, str]] | None:
        """Read the original work selection, including files completed before parking."""
        with self._lock:
            row = self._conn.execute("SELECT payload FROM selection WHERE id=1").fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        items = checkpoint_items(payload)
        warnings, senders = payload.get("warnings", []), payload.get("sender_paths", {})
        if not isinstance(warnings, list) or not all(isinstance(value, str) for value in warnings):
            raise ValueError("Invalid saved loudness warnings")
        if not isinstance(senders, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in senders.items()
        ):
            raise ValueError("Invalid saved loudness sender paths")
        return items, warnings, senders

    def save_selection(self, items: list[ProcessableItem], warnings: list[str], sender_paths: dict[str, str]) -> None:
        """Commit the original selection once, before any file is dispatched."""
        payload = {
            "items": [item_descriptor(item) for item in items],
            "warnings": warnings,
            "sender_paths": sender_paths,
        }
        checkpoint_items(payload)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO selection(id, payload) VALUES (1, ?)",
                (json.dumps(payload, ensure_ascii=False, allow_nan=False),),
            )

    def current_results(self) -> list[dict]:
        """Return this pass's verified/settled rows for authoritative retry accounting."""
        with self._lock:
            rows = list(self._conn.execute("SELECT path, payload FROM completions"))
            accepted = set(self._accepted)
        results = []
        for path, encoded in rows:
            if path in accepted:
                row = json.loads(encoded)
                results.append({**previous_result(path, row["outcome"], row["servers"]), "reason": row["message"]})
        return results

    def close(self) -> None:
        """Release the journal after all handlers have stopped using it."""
        with self._lock:
            self._closed = True
            self._conn.close()

    def record(self, item: ProcessableItem, result: ItemOutcome, before: SourceFingerprint | None) -> None:
        """Save the latest outcome atomically; interrupted commits never earn credit."""
        after = source_fingerprint(item)
        payload = {
            "version": 1,
            "source": list(after) if after is not None and before is not None and before.matches(after) else None,
            "server_id": item.server_id,
            "item_id_by_server": item.item_id_by_server,
            "outcome": result.outcome_key,
            "message": result.message,
            "servers": result.publisher_rows,
        }
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            with self._lock:
                if self._closed:
                    return
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO completions(path, payload) VALUES (?, ?) "
                        "ON CONFLICT(path) DO UPDATE SET payload=excluded.payload",
                        (item.canonical_path, encoded),
                    )
                self._accepted.add(item.canonical_path)
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            self.error = f"Could not save loudness completion ({type(exc).__name__})"
            raise RuntimeError(self.error) from exc

    @staticmethod
    def _saved_candidate(item: ProcessableItem, encoded: str | None) -> dict | None:
        """The saved success row for ``item`` when it still describes the same file and selection, else None.

        A malformed private row means a fresh check, never an inferred success or an erased media result.
        """
        if not encoded:
            return None
        try:
            candidate = json.loads(encoded)
            fingerprint = source_fingerprint(item)
            if (
                isinstance(candidate, dict)
                and candidate.get("version") == 1
                and candidate.get("outcome") in _SUCCESS
                and fingerprint is not None
                and fingerprint.matches(candidate.get("source"))
                and candidate.get("server_id") == item.server_id
                and candidate.get("item_id_by_server") == item.item_id_by_server
                and _valid_servers(candidate.get("servers"))
            ):
                return candidate
        except (ValueError, TypeError):
            return None
        return None

    def restore(
        self,
        items: list[ProcessableItem],
        *,
        check: Callable[[ProcessableItem], ItemOutcome | None],
        cancel_check: Callable[[], bool],
        progress: Callable[[int, int], None] | None = None,
    ) -> tuple[list[ProcessableItem], dict]:
        """Reuse verified successes and return only work still requiring dispatch."""
        with self._lock:
            saved = dict(self._conn.execute("SELECT path, payload FROM completions"))
            self._accepted.clear()
        state: dict[str, Any] = {
            "successful": 0,
            "failed": 0,
            "failed_paths": [],
            "outcome_counts": {},
            "publishers_aggregate": {},
            "cpu_fallback_files": 0,
        }
        remaining = []
        last_progress = 0.0
        pool = ThreadPoolExecutor(RESTORE_CHECK_THREADS)
        try:
            for start in range(0, len(items), RESTORE_CHUNK):
                chunk = items[start : start + RESTORE_CHUNK]
                if cancel_check():
                    raise InterruptedError("Loudness resume cancelled")
                candidates = [self._saved_candidate(item, saved.get(item.canonical_path)) for item in chunk]
                # The check reads the share and Plex, so a chunk's checks run side by side; their results are
                # then judged here in file order.
                checks = [
                    pool.submit(check, item) if candidate else None
                    for item, candidate in zip(chunk, candidates, strict=True)
                ]
                for item, candidate, pending in zip(chunk, candidates, checks, strict=True):
                    if cancel_check():
                        raise InterruptedError("Loudness resume cancelled")
                    row = None
                    if pending is not None and candidate is not None:
                        current = pending.result()
                        fingerprint = source_fingerprint(item)
                        if (
                            current is not None
                            and current.outcome_key == "loudness_up_to_date"
                            and {server.get("server_id") for server in current.publisher_rows}
                            == {server.get("server_id") for server in candidate["servers"]}
                            and fingerprint is not None
                            and fingerprint.matches(candidate.get("source"))
                        ):
                            row = candidate
                    if row is None:
                        remaining.append(item)
                        continue
                    state["successful"] += 1
                    self._accepted.add(item.canonical_path)
                    key = row["outcome"]
                    counts = state["outcome_counts"]
                    counts[key] = counts.get(key, 0) + 1
                    fold_publisher_rows_into_aggregate(state["publishers_aggregate"], row["servers"])
                    now = time.monotonic()
                    if progress and now - last_progress >= 1:
                        progress(state["successful"], len(items))
                        last_progress = now
        finally:
            # On cancel the checks not yet started are dropped; only the ones in flight finish.
            pool.shutdown(wait=True, cancel_futures=True)
        return remaining, state
