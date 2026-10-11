"""SQLite store for the latest health check result per server."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .models import Feature, ServerResult, TodoFile

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS servers (server_id TEXT PRIMARY KEY, json TEXT NOT NULL, checked_at REAL NOT NULL)",
    """CREATE TABLE IF NOT EXISTS todo (
        server_id TEXT NOT NULL,
        library_id TEXT NOT NULL,
        feature TEXT NOT NULL,
        path TEXT NOT NULL,
        title TEXT NOT NULL,
        item_id TEXT NOT NULL,
        not_showing INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_todo_lookup ON todo(server_id, library_id, feature, path)",
)

_MAX_LIMIT = 500


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class HealthStore:
    """Holds one row per server plus its to-do files.

    One shared connection (``check_same_thread=False``) guarded by a lock, so the background runner and
    Flask request threads can call it at once.
    """

    def __init__(self, db_path: str) -> None:
        """Open (creating if needed) the database at ``db_path``."""
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._conn:
            for statement in _SCHEMA:
                self._conn.execute(statement)

    @contextmanager
    def _locked(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def replace_server(self, result: ServerResult) -> None:
        """Swap in a server's new result (summary and to-do files) in one transaction."""
        with self._locked() as conn, conn:
            conn.execute("DELETE FROM todo WHERE server_id = ?", (result.server_id,))
            conn.execute("DELETE FROM servers WHERE server_id = ?", (result.server_id,))
            conn.execute(
                "INSERT INTO servers (server_id, json, checked_at) VALUES (?, ?, ?)",
                (result.server_id, json.dumps(result.to_dict()), result.checked_at),
            )
            conn.executemany(
                "INSERT INTO todo (server_id, library_id, feature, path, title, item_id, not_showing) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        result.server_id,
                        entry.library_id,
                        entry.feature.value,
                        entry.path,
                        entry.title,
                        entry.item_id,
                        int(entry.not_showing),
                    )
                    for entry in result.todo
                ],
            )

    def remove_missing_servers(self, keep_ids: set[str]) -> None:
        """Forget servers that are no longer configured."""
        with self._locked() as conn, conn:
            stored = [row[0] for row in conn.execute("SELECT server_id FROM servers")]
            for server_id in stored:
                if server_id not in keep_ids:
                    conn.execute("DELETE FROM todo WHERE server_id = ?", (server_id,))
                    conn.execute("DELETE FROM servers WHERE server_id = ?", (server_id,))

    def remove_not_showing(self, server_id: str, item_ids: list[str]) -> None:
        """Drop the "not showing" rows of the given items and lower the stored counts to match."""
        with self._locked() as conn, conn:
            conn.executemany(
                "DELETE FROM todo WHERE server_id = ? AND not_showing = 1 AND item_id = ?",
                [(server_id, item_id) for item_id in item_ids],
            )
            row = conn.execute("SELECT json FROM servers WHERE server_id = ?", (server_id,)).fetchone()
            if row is None:
                return
            remaining = dict(
                conn.execute(
                    "SELECT library_id, COUNT(*) FROM todo WHERE server_id = ? AND not_showing = 1 AND feature = ? "
                    "GROUP BY library_id",
                    (server_id, Feature.PREVIEWS.value),
                ).fetchall()
            )
            data = json.loads(row[0])
            for library in data["libraries"]:
                cell = library["cells"].get(Feature.PREVIEWS.value)
                if cell is not None:
                    cell["not_showing"] = remaining.get(library["library_id"], 0)
            conn.execute("UPDATE servers SET json = ? WHERE server_id = ?", (json.dumps(data), server_id))

    def load(self) -> list[ServerResult]:
        """All stored servers, oldest-inserted first; their ``todo`` lists are left empty."""
        with self._locked() as conn:
            rows = conn.execute("SELECT json FROM servers ORDER BY rowid").fetchall()
        return [ServerResult.from_dict(json.loads(row[0])) for row in rows]

    def list_todo(
        self,
        server_id: str,
        library_id: str,
        feature: Feature,
        *,
        q: str = "",
        offset: int = 0,
        limit: int = 100,
        not_showing: bool = False,
    ) -> tuple[int, list[TodoFile]]:
        """One page of a library's to-do files for a feature.

        Args:
            server_id: Server to look in.
            library_id: Library to look in.
            feature: Feature the files are missing.
            q: Case-insensitive substring to match against the path.
            offset: Rows to skip.
            limit: Page size, clamped to 1..500.
            not_showing: List the Plex "BIF exists but not showing" files instead of the plain to-do ones.

        Returns:
            (total matching, the page of files ordered by path).
        """
        limit = max(1, min(limit, _MAX_LIMIT))
        offset = max(offset, 0)
        where = "server_id = ? AND library_id = ? AND feature = ? AND not_showing = ?"
        params: list[object] = [server_id, library_id, feature.value, int(not_showing)]
        if q:
            where += " AND LOWER(path) LIKE ? ESCAPE '\\'"
            params.append(f"%{_escape_like(q.lower())}%")
        with self._locked() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM todo WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT feature, path, title, item_id, not_showing, library_id FROM todo WHERE {where} "
                "ORDER BY path LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return total, [TodoFile(Feature(r[0]), r[1], r[2], r[3], bool(r[4]), r[5]) for r in rows]

    def todo_for(self, server_id: str, library_id: str, feature: Feature) -> list[TodoFile]:
        """Every stored to-do row (including "not showing" ones) of one library and feature."""
        with self._locked() as conn:
            rows = conn.execute(
                "SELECT feature, path, title, item_id, not_showing, library_id FROM todo "
                "WHERE server_id = ? AND library_id = ? AND feature = ?",
                (server_id, library_id, feature.value),
            ).fetchall()
        return [TodoFile(Feature(r[0]), r[1], r[2], r[3], bool(r[4]), r[5]) for r in rows]

    def reread_items(self, server_id: str) -> list[tuple[str, str]]:
        """Distinct (item_id, library_id) pairs whose preview exists but isn't showing."""
        with self._locked() as conn:
            rows = conn.execute(
                "SELECT DISTINCT item_id, library_id FROM todo WHERE server_id = ? AND not_showing = 1 "
                "AND item_id != '' ORDER BY library_id, item_id",
                (server_id,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def reread_paths(self, server_id: str) -> dict[str, list[str]]:
        """Item id -> the paths of its files whose preview exists but isn't showing."""
        with self._locked() as conn:
            rows = conn.execute(
                "SELECT DISTINCT item_id, path FROM todo WHERE server_id = ? AND not_showing = 1 AND item_id != '' "
                "ORDER BY item_id, path",
                (server_id,),
            ).fetchall()
        paths: dict[str, list[str]] = {}
        for item_id, path in rows:
            paths.setdefault(item_id, []).append(path)
        return paths


_default: HealthStore | None = None
_default_lock = threading.Lock()


def default_store() -> HealthStore:
    """The process-wide store at ``<CONFIG_DIR>/library_health.db``, opened on first use."""
    global _default
    with _default_lock:
        if _default is None:
            from ..web.auth import get_config_dir

            _default = HealthStore(os.path.join(get_config_dir(), "library_health.db"))
        return _default
