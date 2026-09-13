"""SQLite store for Intro & Credits (spec §6.1).

One connection shared by the web threads, checking threads and workers: every call takes a lock, multi-statement
changes run in ``BEGIN IMMEDIATE`` transactions. Reads queue up behind writes just like everything else here,
since every caller shares one connection and one lock -- WAL mode only benefits a second, independent connection
(an external inspection tool, a future replica), not callers going through this class.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from .decide import DecisionStatus, TypeDecision
from .models import Candidate, FileIdentity, Marker, MarkerType, Source

SCHEMA_VERSION = 1

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY,
        canonical_path TEXT NOT NULL UNIQUE,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        duration_ms INTEGER,
        season_key TEXT,
        is_movie INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_files_season ON files(season_key)",
    """CREATE TABLE IF NOT EXISTS fingerprints (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        window TEXT NOT NULL,
        start_s REAL NOT NULL,
        length_s REAL NOT NULL,
        algorithm INTEGER NOT NULL,
        points BLOB NOT NULL,
        PRIMARY KEY (file_id, window))""",
    """CREATE TABLE IF NOT EXISTS evidence (
        id INTEGER PRIMARY KEY,
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        type TEXT,
        start_ms INTEGER,
        end_ms INTEGER,
        confidence REAL,
        detail TEXT NOT NULL DEFAULT '',
        fetched_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_evidence_file ON evidence(file_id, source, origin)",
    """CREATE TABLE IF NOT EXISTS markers (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        decided_by TEXT NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (file_id, type))""",
    """CREATE TABLE IF NOT EXISTS decisions (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        proposed_start_ms INTEGER,
        proposed_end_ms INTEGER,
        settings_fingerprint TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (file_id, type))""",
    """CREATE TABLE IF NOT EXISTS publish_state (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        server_id TEXT NOT NULL,
        item_id TEXT,
        markers_hash TEXT,
        markers_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        message TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        verified_at TEXT,
        PRIMARY KEY (file_id, server_id))""",
    """CREATE TABLE IF NOT EXISTS source_usage (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        used INTEGER,
        limit_ INTEGER,
        remaining INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source, day))""",
)

# Ordered migrations: _MIGRATIONS[v] holds the statements that take an existing database from schema
# version v to v+1. Empty for now -- SCHEMA_VERSION stays 1 until the first real migration lands; a
# future bump appends `SCHEMA_VERSION - 1: (...)` here rather than changing this class's open logic.
_MIGRATIONS: dict[int, tuple[str, ...]] = {}


@dataclass(frozen=True)
class FileRecord:
    """A row of ``files`` plus whether the last upsert created or invalidated it."""

    id: int
    canonical_path: str
    size: int
    mtime_ns: int
    duration_ms: int | None
    season_key: str | None
    is_movie: bool
    created: bool = False
    changed: bool = False


@dataclass(frozen=True)
class EvidenceRow:
    """One stored evidence row; ``type`` is None for "looked it up, nothing there"."""

    source: Source
    origin: str
    type: MarkerType | None
    start_ms: int | None
    end_ms: int | None
    confidence: float | None
    detail: str
    fetched_at: str


@dataclass(frozen=True)
class DecisionRow:
    """Stored decision for one marker type."""

    type: MarkerType
    status: DecisionStatus
    reason: str
    proposed_start_ms: int | None
    proposed_end_ms: int | None
    settings_fingerprint: str
    decided_at: str


@dataclass(frozen=True)
class PublishStateRow:
    """What we last sent to one server for one file."""

    server_id: str
    item_id: str | None
    markers_hash: str | None
    markers: tuple[Marker, ...]
    status: str
    message: str
    updated_at: str
    verified_at: str | None


class MarkerStore:
    """Thread-safe access to ``markers.db``."""

    def __init__(self, db_path: str, *, clock: Callable[[], datetime] | None = None) -> None:
        """Open (and create) the store.

        Args:
            db_path: Path to the SQLite file.
            clock: Returns the current UTC datetime (tests inject a fake clock).
        """
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._tx_owner: int | None = None
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            with self._lock:
                self._open_schema()
        except BaseException:
            # A newer-version refusal or a failed migration must not leave a half-open connection
            # holding the database file locked for whoever tries to open it next.
            self._conn.close()
            raise

    def _open_schema(self) -> None:
        """Check (and, if needed, upgrade) the schema version, then create or catch up the schema.

        The version check runs straight off ``sqlite_master`` before any DDL -- including the switch
        to WAL, which also rewrites the file -- so a database from a newer build is refused truly
        untouched: still its original journal mode, no ``-wal`` file, no tables gained. On an
        existing, non-newer database (or a brand-new one with no ``meta`` table yet), migrations, the
        (idempotent) ``_SCHEMA`` statements and the version write all run inside one transaction, so a
        migration statement that fails leaves both the schema and the recorded version exactly where
        they were.

        Raises:
            RuntimeError: The database's ``schema_version`` is newer than this build supports.
        """
        meta_exists = self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if meta_exists:
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row["value"]) if row else SCHEMA_VERSION
        else:
            current = SCHEMA_VERSION  # nothing written yet -- no version to refuse or migrate from
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"markers.db was created by a newer version (schema {current}, this build supports {SCHEMA_VERSION})"
            )
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._tx() as conn:
            while current < SCHEMA_VERSION:
                for stmt in _MIGRATIONS.get(current, ()):
                    conn.execute(stmt)
                current += 1
            for stmt in _SCHEMA:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(current),),
            )

    def _now(self) -> str:
        return self._clock().isoformat()

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction under the store lock; the lock is released on every path.

        Raises:
            RuntimeError: Called again on the same thread while its own transaction is still open --
                the store lock is reentrant, so a plain nested call would otherwise silently start
                (or roll back) the wrong transaction.
        """
        with self._lock:
            if self._tx_owner is not None:
                raise RuntimeError("nested MarkerStore transaction")
            if self._conn.in_transaction:
                # A COMMIT or ROLLBACK that failed (disk full, I/O error) can leave the transaction open;
                # without this every later BEGIN raises "cannot start a transaction within a transaction".
                self._rollback_quietly()
            self._conn.execute("BEGIN IMMEDIATE")
            self._tx_owner = threading.get_ident()
            try:
                try:
                    yield self._conn
                except BaseException:
                    self._rollback_quietly()
                    raise
                try:
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._rollback_quietly()
                    raise
            finally:
                self._tx_owner = None

    def _rollback_quietly(self) -> None:
        if not self._conn.in_transaction:
            return
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.Error as exc:
            logger.warning("markers.db rollback failed: {}", exc)

    @staticmethod
    def _file(row: sqlite3.Row, *, created: bool = False, changed: bool = False) -> FileRecord:
        return FileRecord(
            id=row["id"],
            canonical_path=row["canonical_path"],
            size=row["size"],
            mtime_ns=row["mtime_ns"],
            duration_ms=row["duration_ms"],
            season_key=row["season_key"],
            is_movie=bool(row["is_movie"]),
            created=created,
            changed=changed,
        )

    def upsert_file(
        self, identity: FileIdentity, *, duration_ms: int | None, season_key: str | None, is_movie: bool
    ) -> FileRecord:
        """Insert or refresh a file; a size/mtime change invalidates derived data (locked markers survive).

        A changed identity also clears every server's ``markers_hash``/``verified_at`` in ``publish_state``
        (keeping ``markers_json``/``status`` so the next publish still knows what to replace) -- an in-place
        replacement (e.g. a Tdarr transcode) landing on identical times must still be re-written, since some
        servers (Jellyfin) drop segments outright when a file's mtime changes. ``duration_ms=None`` on a
        changed identity stores NULL, since the old duration can no longer be trusted; on an unchanged
        identity it keeps the previous value.

        Returns:
            The record, with ``created``/``changed`` describing what happened.
        """
        now = self._now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM files WHERE canonical_path=?", (identity.canonical_path,)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO files (canonical_path, size, mtime_ns, duration_ms, season_key, is_movie, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (
                        identity.canonical_path,
                        identity.size,
                        identity.mtime_ns,
                        duration_ms,
                        season_key,
                        int(is_movie),
                        now,
                    ),
                )
                file_id, created, changed = cur.lastrowid, True, False
            else:
                file_id, created = row["id"], False
                changed = (row["size"], row["mtime_ns"]) != (identity.size, identity.mtime_ns)
                if changed:
                    for table in ("evidence", "fingerprints", "decisions"):
                        conn.execute(f"DELETE FROM {table} WHERE file_id=?", (file_id,))
                    conn.execute("DELETE FROM markers WHERE file_id=? AND locked=0", (file_id,))
                    conn.execute(
                        "UPDATE publish_state SET markers_hash=NULL, verified_at=NULL WHERE file_id=?", (file_id,)
                    )
                    conn.execute(
                        "UPDATE files SET size=?, mtime_ns=?, duration_ms=?, season_key=?, is_movie=?, updated_at=? "
                        "WHERE id=?",
                        (identity.size, identity.mtime_ns, duration_ms, season_key, int(is_movie), now, file_id),
                    )
                else:
                    conn.execute(
                        "UPDATE files SET size=?, mtime_ns=?, duration_ms=COALESCE(?, duration_ms), season_key=?, "
                        "is_movie=?, updated_at=? WHERE id=?",
                        (identity.size, identity.mtime_ns, duration_ms, season_key, int(is_movie), now, file_id),
                    )
            new_row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._file(new_row, created=created, changed=changed)

    def get_file(self, canonical_path: str) -> FileRecord | None:
        """Look a file up by canonical path."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE canonical_path=?", (canonical_path,)).fetchone()
        return self._file(row) if row else None

    def get_file_by_id(self, file_id: int) -> FileRecord | None:
        """Look a file up by id."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._file(row) if row else None

    def files_in_season(self, season_key: str) -> list[FileRecord]:
        """All known files of a season folder, sorted by path."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE season_key=? ORDER BY canonical_path", (season_key,)
            ).fetchall()
        return [self._file(r) for r in rows]

    def replace_evidence(
        self, file_id: int, source: Source, candidates: list[Candidate], *, origin: str = "", detail: str = ""
    ) -> None:
        """Replace one source's evidence under one lookup key. An empty list records "looked it up, nothing there".

        ``origin`` is the replace/lookup key (e.g. a server id) -- it alone scopes what this call deletes and
        re-inserts. Each candidate's own ``c.origin`` (e.g. a chapter title) is stored separately as ``label``
        and never affects scoping, so replacing chapters under the shared default key ("") always clears every
        previous chapter candidate, however many different titles they carried.
        """
        now = self._now()
        with self._tx() as conn:
            conn.execute(
                "DELETE FROM evidence WHERE file_id=? AND source=? AND origin=?", (file_id, source.value, origin)
            )
            if not candidates:
                conn.execute(
                    "INSERT INTO evidence (file_id, source, origin, detail, fetched_at) VALUES (?,?,?,?,?)",
                    (file_id, source.value, origin, detail, now),
                )
            for c in candidates:
                conn.execute(
                    "INSERT INTO evidence (file_id, source, origin, label, type, start_ms, end_ms, confidence, "
                    "detail, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        file_id,
                        source.value,
                        origin,
                        c.origin,
                        c.type.value,
                        c.start_ms,
                        c.end_ms,
                        c.confidence,
                        detail,
                        now,
                    ),
                )

    def evidence_fetched_at(self, file_id: int, source: Source, origin: str = "") -> datetime | None:
        """When a source was last looked up for this file (None = never)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(fetched_at) AS t FROM evidence WHERE file_id=? AND source=? AND origin=?",
                (file_id, source.value, origin),
            ).fetchone()
        return datetime.fromisoformat(row["t"]) if row and row["t"] else None

    def _evidence_query(self, file_id: int) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute("SELECT * FROM evidence WHERE file_id=? ORDER BY id", (file_id,)).fetchall()

    def evidence_rows(self, file_id: int) -> list[EvidenceRow]:
        """Every evidence row for a file, including empty lookups."""
        return [
            EvidenceRow(
                source=Source(r["source"]),
                origin=r["origin"],
                type=MarkerType(r["type"]) if r["type"] else None,
                start_ms=r["start_ms"],
                end_ms=r["end_ms"],
                confidence=r["confidence"],
                detail=r["detail"],
                fetched_at=r["fetched_at"],
            )
            for r in self._evidence_query(file_id)
        ]

    def get_evidence(self, file_id: int) -> list[Candidate]:
        """Candidates for the decision rules (empty lookups excluded).

        A candidate's ``origin`` is rebuilt from the row's own ``label`` (e.g. a chapter title) when set,
        falling back to the replace/lookup key in ``origin`` -- the two differ for chapters, which share one
        lookup key ("") across many differently-titled candidates.
        """
        return [
            Candidate(
                MarkerType(r["type"]),
                r["start_ms"],
                r["end_ms"],
                Source(r["source"]),
                r["confidence"] if r["confidence"] is not None else 1.0,
                r["label"] or r["origin"],
            )
            for r in self._evidence_query(file_id)
            if r["type"] is not None and r["start_ms"] is not None
        ]

    def save_decisions(
        self, file_id: int, decisions: dict[MarkerType, TypeDecision], *, settings_fingerprint: str
    ) -> None:
        """Store decisions; DECIDED writes the marker, anything else removes an unlocked marker of that type."""
        now = self._now()
        with self._tx() as conn:
            for mtype, d in decisions.items():
                locked = conn.execute(
                    "SELECT 1 FROM markers WHERE file_id=? AND type=? AND locked=1", (file_id, mtype.value)
                ).fetchone()
                proposed = d.proposed
                conn.execute(
                    "INSERT OR REPLACE INTO decisions (file_id, type, status, reason, proposed_start_ms, proposed_end_ms, "
                    "settings_fingerprint, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        file_id,
                        mtype.value,
                        d.status.value,
                        d.reason,
                        proposed.start_ms if proposed else None,
                        proposed.end_ms if proposed else None,
                        settings_fingerprint,
                        now,
                    ),
                )
                if locked:
                    continue
                if d.status is DecisionStatus.DECIDED and d.marker is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO markers (file_id, type, start_ms, end_ms, decided_by, locked, updated_at) "
                        "VALUES (?,?,?,?,?,0,?)",
                        (
                            file_id,
                            mtype.value,
                            d.marker.start_ms,
                            d.marker.end_ms,
                            json.dumps(list(d.marker.decided_by)),
                            now,
                        ),
                    )
                else:
                    conn.execute("DELETE FROM markers WHERE file_id=? AND type=? AND locked=0", (file_id, mtype.value))

    def _markers(self, file_id: int, locked_only: bool) -> dict[MarkerType, Marker]:
        sql = "SELECT * FROM markers WHERE file_id=?" + (" AND locked=1" if locked_only else "")
        with self._lock:
            rows = self._conn.execute(sql, (file_id,)).fetchall()
        return {
            MarkerType(r["type"]): Marker(
                MarkerType(r["type"]), r["start_ms"], r["end_ms"], tuple(json.loads(r["decided_by"])), bool(r["locked"])
            )
            for r in rows
        }

    def get_markers(self, file_id: int) -> dict[MarkerType, Marker]:
        """Desired markers (decided + locked) for a file."""
        return self._markers(file_id, locked_only=False)

    def get_locked(self, file_id: int) -> dict[MarkerType, Marker]:
        """User-locked markers for a file."""
        return self._markers(file_id, locked_only=True)

    def lock_marker(self, file_id: int, marker: Marker) -> None:
        """Store a user-locked marker; detection never replaces it (the Inspector editor calls this in phase 4)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO markers (file_id, type, start_ms, end_ms, decided_by, locked, updated_at) "
                "VALUES (?,?,?,?,?,1,?)",
                (
                    file_id,
                    marker.type.value,
                    marker.start_ms,
                    marker.end_ms,
                    json.dumps(list(marker.decided_by)),
                    self._now(),
                ),
            )

    def get_decisions(self, file_id: int) -> dict[MarkerType, DecisionRow]:
        """Stored decisions by type."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM decisions WHERE file_id=?", (file_id,)).fetchall()
        return {
            MarkerType(r["type"]): DecisionRow(
                MarkerType(r["type"]),
                DecisionStatus(r["status"]),
                r["reason"],
                r["proposed_start_ms"],
                r["proposed_end_ms"],
                r["settings_fingerprint"],
                r["decided_at"],
            )
            for r in rows
        }

    def set_publish_state(
        self,
        file_id: int,
        server_id: str,
        *,
        item_id: str | None,
        markers: list[Marker] | None,
        status: str,
        message: str = "",
        verified: bool = False,
    ) -> None:
        """Record a publish attempt. ``markers=None`` keeps the last successfully published set (and its
        ``verified_at``) -- ``verified`` is ignored in that case, since there's nothing new to verify."""
        now = self._now()
        with self._tx() as conn:
            if markers is None:
                prev = conn.execute(
                    "SELECT markers_hash, markers_json, verified_at FROM publish_state WHERE file_id=? AND server_id=?",
                    (file_id, server_id),
                ).fetchone()
                markers_hash = prev["markers_hash"] if prev else None
                markers_json = prev["markers_json"] if prev else "[]"
                verified_at = prev["verified_at"] if prev else None
            else:
                markers_hash = self.markers_hash(markers)
                markers_json = json.dumps(
                    [[m.type.value, m.start_ms, m.end_ms, list(m.decided_by), m.locked] for m in markers]
                )
                verified_at = now if verified else None
            conn.execute(
                "INSERT OR REPLACE INTO publish_state (file_id, server_id, item_id, markers_hash, markers_json, status, "
                "message, updated_at, verified_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (file_id, server_id, item_id, markers_hash, markers_json, status, message, now, verified_at),
            )

    @staticmethod
    def _publish_row(r: sqlite3.Row) -> PublishStateRow:
        markers = tuple(
            Marker(MarkerType(t), start, end, tuple(by), bool(locked))
            for t, start, end, by, locked in json.loads(r["markers_json"] or "[]")
        )
        return PublishStateRow(
            r["server_id"],
            r["item_id"],
            r["markers_hash"],
            markers,
            r["status"],
            r["message"],
            r["updated_at"],
            r["verified_at"],
        )

    def get_publish_state(self, file_id: int, server_id: str) -> PublishStateRow | None:
        """Last publish record for one server."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM publish_state WHERE file_id=? AND server_id=?", (file_id, server_id)
            ).fetchone()
        return self._publish_row(r) if r else None

    def publish_states(self, file_id: int) -> list[PublishStateRow]:
        """Publish records for every server, sorted by server id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM publish_state WHERE file_id=? ORDER BY server_id", (file_id,)
            ).fetchall()
        return [self._publish_row(r) for r in rows]

    def record_source_usage(
        self, source_id: str, *, day: str, used: int | None, limit: int | None, remaining: int | None
    ) -> None:
        """Persist today's usage for a rate-limited source (shown in Settings)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO source_usage (source, day, used, limit_, remaining, updated_at) VALUES (?,?,?,?,?,?)",
                (source_id, day, used, limit, remaining, self._now()),
            )

    def source_usage(self, source_id: str, day: str) -> dict | None:
        """Usage for a source on a UTC day (YYYY-MM-DD)."""
        with self._lock:
            r = self._conn.execute("SELECT * FROM source_usage WHERE source=? AND day=?", (source_id, day)).fetchone()
        return {"used": r["used"], "limit": r["limit_"], "remaining": r["remaining"]} if r else None

    def _count(self, table: str) -> int:
        with self._lock:
            return int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    @staticmethod
    def markers_hash(markers: Iterable[Marker]) -> str:
        """Stable hash of what a server should show (type + times only)."""
        payload = sorted((m.type.value, m.start_ms, m.end_ms) for m in markers)
        return hashlib.sha1(json.dumps(payload).encode(), usedforsecurity=False).hexdigest()


_store: MarkerStore | None = None
_store_lock = threading.Lock()


def get_marker_store(config_dir: str | None = None) -> MarkerStore:
    """Process-wide store at ``<CONFIG_DIR>/markers.db``."""
    global _store
    with _store_lock:
        if _store is None:
            base = config_dir or os.environ.get("CONFIG_DIR", "/config")
            _store = MarkerStore(os.path.join(base, "markers.db"))
        return _store


def reset_marker_store() -> None:
    """Close and forget the singleton (tests)."""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
