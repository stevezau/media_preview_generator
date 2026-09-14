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
    # The chapter rules / parser / reader version one lookup's evidence rows were made with: a different version in
    # this build means the rows are derived again (an unchanged file is otherwise never probed or asked again).
    """CREATE TABLE IF NOT EXISTS evidence_versions (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        origin TEXT NOT NULL DEFAULT '',
        version INTEGER NOT NULL,
        PRIMARY KEY (file_id, source, origin))""",
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
    # markers_hash and verified_at are no longer written (always NULL); the columns stay until a schema bump.
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
    # What this app last left on a server item, whichever file published it: Plex serves one marker set per item
    # across all its versions. Not tied to a file row, so replacing or removing a file keeps it.
    """CREATE TABLE IF NOT EXISTS item_publish_state (
        server_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        markers_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (server_id, item_id))""",
    # Marker types a server's own detection replaced on an item whose server is set to keep them ("Keep Plex's").
    # A separate table so a markers.db from before it only gains the table.
    """CREATE TABLE IF NOT EXISTS item_kept_types (
        server_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        type TEXT NOT NULL,
        PRIMARY KEY (server_id, item_id, type))""",
    # What a file's last publish to a server was based on: its decided set and the item row version it saw.
    """CREATE TABLE IF NOT EXISTS publish_basis (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        server_id TEXT NOT NULL,
        decided_hash TEXT NOT NULL,
        item_version INTEGER NOT NULL,
        PRIMARY KEY (file_id, server_id))""",
    # The media server's own kind for a file (movie / episode / unknown), so it isn't asked again every run.
    """CREATE TABLE IF NOT EXISTS server_kinds (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        confirmed_at TEXT NOT NULL)""",
    # Runs the v3 matcher found between two fingerprinted files. file_a is the matcher's first argument: the matcher
    # isn't symmetric, and the season step always pairs two files the same way round (Task 7).
    """CREATE TABLE IF NOT EXISTS season_pairs (
        file_a INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        file_b INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        matcher_version INTEGER NOT NULL,
        runs_json TEXT NOT NULL,
        PRIMARY KEY (file_a, file_b))""",
    "CREATE INDEX IF NOT EXISTS idx_season_pairs_b ON season_pairs(file_b)",
    # What a local detector's last answer for a file was based on (season audio: the season's fingerprinted files), so
    # it runs again only when that changes.
    """CREATE TABLE IF NOT EXISTS detector_runs (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        signature TEXT NOT NULL,
        run_at TEXT NOT NULL,
        PRIMARY KEY (file_id, source))""",
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
    """A row of ``files``."""

    id: int
    canonical_path: str
    size: int
    mtime_ns: int
    duration_ms: int | None
    season_key: str | None
    is_movie: bool


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
class ItemPublishStateRow:
    """What this app last left on one server item (from any file)."""

    server_id: str
    item_id: str
    markers: tuple[Marker, ...]
    status: str
    version: int
    updated_at: str
    # Types the server's own markers replaced and are kept there (Plex "Keep Plex's"); never ours in ``markers``.
    kept_types: frozenset[MarkerType] = frozenset()


@dataclass(frozen=True)
class PublishStateRow:
    """What we last sent to one server for one file."""

    server_id: str
    item_id: str | None
    markers: tuple[Marker, ...]
    status: str
    message: str
    updated_at: str


@dataclass(frozen=True)
class StoredFingerprint:
    """A cached chromaprint fingerprint (raw little-endian uint32 points; empty for a file without audio)."""

    file_id: int
    window: str
    start_s: float
    length_s: float
    algorithm: int
    points: bytes


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
    def _file(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=row["id"],
            canonical_path=row["canonical_path"],
            size=row["size"],
            mtime_ns=row["mtime_ns"],
            duration_ms=row["duration_ms"],
            season_key=row["season_key"],
            is_movie=bool(row["is_movie"]),
        )

    def upsert_file(
        self, identity: FileIdentity, *, duration_ms: int | None, season_key: str | None, is_movie: bool
    ) -> FileRecord:
        """Insert or refresh a file; a size/mtime change invalidates derived data (locked markers survive).

        A changed identity clears evidence (and its versions), fingerprints, decisions, the server kind, season pairs,
        detector runs and every server's ``publish_basis``, so the next run offers the markers to every server again
        even when an in-place replacement (e.g. a Tdarr transcode) lands on identical times: the Jellyfin plugin
        serves nothing for a file whose size changed until it is sent again, and its publisher sends it when the
        stored size differs. ``publish_state`` keeps ``markers_json``/``status`` so that publish still knows what to
        replace. ``duration_ms=None`` on a changed identity stores NULL, since the old duration can no longer be
        trusted; on an unchanged identity it keeps the previous value.

        Returns:
            The record.
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
                file_id = cur.lastrowid
            else:
                file_id = row["id"]
                if (row["size"], row["mtime_ns"]) != (identity.size, identity.mtime_ns):
                    for table in (
                        "evidence",
                        "evidence_versions",
                        "fingerprints",
                        "decisions",
                        "server_kinds",
                        "publish_basis",
                        "detector_runs",
                    ):
                        conn.execute(f"DELETE FROM {table} WHERE file_id=?", (file_id,))
                    conn.execute("DELETE FROM season_pairs WHERE file_a=? OR file_b=?", (file_id, file_id))
                    conn.execute("DELETE FROM markers WHERE file_id=? AND locked=0", (file_id,))
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
        return self._file(new_row)

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
        self,
        file_id: int,
        source: Source,
        candidates: list[Candidate],
        *,
        origin: str = "",
        detail: str = "",
        version: int | None = None,
        also_replaces: Iterable[Source] = (),
    ) -> None:
        """Replace one source's evidence under one lookup key. An empty list records "looked it up, nothing there".

        ``origin`` is the replace/lookup key (e.g. a server id) -- it alone scopes what this call deletes and
        re-inserts. Each candidate's own ``c.origin`` (e.g. a chapter title) is stored separately as ``label``
        and never affects scoping, so replacing chapters under the shared default key ("") always clears every
        previous chapter candidate, however many different titles they carried. Rows are stored under ``source``,
        whatever each candidate's own source says.

        Args:
            file_id: The file.
            source: The source the rows are stored under.
            candidates: The answer's candidates.
            origin: The lookup key.
            detail: Shown with every row (the Inspector); never a secret.
            version: The chapter rules / parser / reader version that made the answer (None: unknown).
            also_replaces: Other sources whose rows and version under the same ``origin`` this answer replaces.
        """
        now = self._now()
        with self._tx() as conn:
            for replaced in dict.fromkeys((source, *also_replaces)):
                for table in ("evidence", "evidence_versions"):
                    conn.execute(
                        f"DELETE FROM {table} WHERE file_id=? AND source=? AND origin=?",
                        (file_id, replaced.value, origin),
                    )
            if version is not None:
                conn.execute(
                    "INSERT INTO evidence_versions (file_id, source, origin, version) VALUES (?,?,?,?)",
                    (file_id, source.value, origin, version),
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

    def evidence_version(self, file_id: int, source: Source, origin: str = "") -> int | None:
        """The version one lookup's stored evidence was made with (None = never stored, or stored without one)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM evidence_versions WHERE file_id=? AND source=? AND origin=?",
                (file_id, source.value, origin),
            ).fetchone()
        return int(row["version"]) if row else None

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
    ) -> None:
        """Record a publish attempt. ``markers=None`` keeps the last successfully published set."""
        now = self._now()
        with self._tx() as conn:
            if markers is None:
                prev = conn.execute(
                    "SELECT markers_json FROM publish_state WHERE file_id=? AND server_id=?", (file_id, server_id)
                ).fetchone()
                markers_json = prev["markers_json"] if prev else "[]"
            else:
                markers_json = self._markers_to_json(markers)
            conn.execute(
                "INSERT OR REPLACE INTO publish_state (file_id, server_id, item_id, markers_json, status, message, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (file_id, server_id, item_id, markers_json, status, message, now),
            )

    @staticmethod
    def _markers_to_json(markers: Iterable[Marker]) -> str:
        return json.dumps([[m.type.value, m.start_ms, m.end_ms, list(m.decided_by), m.locked] for m in markers])

    @staticmethod
    def _shown(markers_json: str) -> list[tuple[MarkerType, int, int]]:
        return [(m.type, m.start_ms, m.end_ms) for m in MarkerStore._markers_from_json(markers_json)]

    @staticmethod
    def _markers_from_json(text: str | None) -> tuple[Marker, ...]:
        return tuple(
            Marker(MarkerType(t), start, end, tuple(by), bool(locked))
            for t, start, end, by, locked in json.loads(text or "[]")
        )

    @staticmethod
    def _publish_row(r: sqlite3.Row) -> PublishStateRow:
        markers = MarkerStore._markers_from_json(r["markers_json"])
        return PublishStateRow(r["server_id"], r["item_id"], markers, r["status"], r["message"], r["updated_at"])

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

    def set_server_kind(self, file_id: int, kind: str) -> None:
        """Remember the kind an owning server reported for a file (cleared when the file changes).

        Args:
            file_id: The file.
            kind: ``movie``, ``episode`` or ``unknown``.
        """
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO server_kinds (file_id, kind, confirmed_at) VALUES (?,?,?)",
                (file_id, kind, self._now()),
            )

    def get_server_kind(self, file_id: int) -> str | None:
        """The kind a server reported for this version of the file, or None when none has."""
        with self._lock:
            row = self._conn.execute("SELECT kind FROM server_kinds WHERE file_id=?", (file_id,)).fetchone()
        return row["kind"] if row else None

    def set_fingerprint(
        self,
        file_id: int,
        *,
        size: int,
        mtime_ns: int,
        window: str,
        start_s: float,
        length_s: float,
        algorithm: int,
        points: bytes,
    ) -> bool:
        """Store a fingerprint computed from the file with identity ``(size, mtime_ns)``.

        Returns:
            False (nothing stored) when the file's row has another identity now: the file was replaced while ffmpeg
            ran, and its own next run fingerprints the new file.
        """
        with self._tx() as conn:
            row = conn.execute("SELECT size, mtime_ns FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None or (row["size"], row["mtime_ns"]) != (size, mtime_ns):
                return False
            conn.execute(
                "INSERT OR REPLACE INTO fingerprints (file_id, window, start_s, length_s, algorithm, points) "
                "VALUES (?,?,?,?,?,?)",
                (file_id, window, start_s, length_s, algorithm, points),
            )
        return True

    def get_fingerprint(self, file_id: int, window: str) -> StoredFingerprint | None:
        """The cached fingerprint of a file, or None when it was never fingerprinted (or changed since)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM fingerprints WHERE file_id=? AND window=?", (file_id, window)
            ).fetchone()
        if r is None:
            return None
        return StoredFingerprint(
            r["file_id"], r["window"], r["start_s"], r["length_s"], r["algorithm"], bytes(r["points"])
        )

    def get_season_pair(
        self, file_a: int, file_b: int, matcher_version: int
    ) -> list[tuple[float, float, float, float]] | None:
        """Cached matcher runs between two files (``file_a`` was the matcher's first argument); None when not computed."""
        with self._lock:
            r = self._conn.execute(
                "SELECT runs_json FROM season_pairs WHERE file_a=? AND file_b=? AND matcher_version=?",
                (file_a, file_b, matcher_version),
            ).fetchone()
        return None if r is None else [tuple(run) for run in json.loads(r["runs_json"])]

    def set_season_pair(
        self, file_a: int, file_b: int, matcher_version: int, runs: list[tuple[float, float, float, float]]
    ) -> bool:
        """Cache matcher runs between two files.

        Returns:
            False (nothing stored) when either file's fingerprint is gone: one of them changed while matching.
        """
        with self._tx() as conn:
            have = conn.execute(
                "SELECT COUNT(*) FROM fingerprints WHERE file_id IN (?, ?) AND window='intro'", (file_a, file_b)
            ).fetchone()[0]
            if have != 2:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO season_pairs (file_a, file_b, matcher_version, runs_json) VALUES (?,?,?,?)",
                (file_a, file_b, matcher_version, json.dumps([list(run) for run in runs])),
            )
        return True

    def get_detector_run(self, file_id: int, source: Source) -> str | None:
        """The signature a local detector's stored answer for a file was based on, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT signature FROM detector_runs WHERE file_id=? AND source=?", (file_id, source.value)
            ).fetchone()
        return r["signature"] if r else None

    def set_detector_run(self, file_id: int, source: Source, signature: str) -> None:
        """Record what a local detector's answer for a file was based on."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO detector_runs (file_id, source, signature, run_at) VALUES (?,?,?,?)",
                (file_id, source.value, signature, self._now()),
            )

    def get_item_publish_state(self, server_id: str, item_id: str) -> ItemPublishStateRow | None:
        """What this app last left on a server item, or None when it never published there."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM item_publish_state WHERE server_id=? AND item_id=?", (server_id, item_id)
            ).fetchone()
            if r is None:
                return None
            kept = self._kept_types(self._conn, server_id, item_id)
        return ItemPublishStateRow(
            r["server_id"],
            r["item_id"],
            self._markers_from_json(r["markers_json"]),
            r["status"],
            r["version"],
            r["updated_at"],
            kept,
        )

    @staticmethod
    def _kept_types(conn: sqlite3.Connection, server_id: str, item_id: str) -> frozenset[MarkerType]:
        rows = conn.execute(
            "SELECT type FROM item_kept_types WHERE server_id=? AND item_id=?", (server_id, item_id)
        ).fetchall()
        return frozenset(MarkerType(r["type"]) for r in rows)

    def set_item_publish_state(
        self,
        server_id: str,
        item_id: str,
        markers: list[Marker] | None,
        status: str,
        *,
        kept_types: Iterable[MarkerType] | None = None,
    ) -> int:
        """Record what is ours on a server item after a publish attempt.

        ``version`` goes up only when the status or what the server shows changes: markers compare by (type, start,
        end), so versions that agree on the times but were decided by different sources don't bump it for each other.
        The version doesn't stop two versions that show different times from rewriting the item in turn; the Plex
        publisher keeps the previous times when every version agrees with them.

        Args:
            server_id: The server.
            item_id: The server's item id.
            markers: Ours on the item now (sorted by start when stored); None keeps the markers recorded before (a
                failed write), or none for a new row.
            status: ``written`` or ``failed``.
            kept_types: Types whose rows on the item are the server's own and kept there; None keeps the recorded
                ones. A change bumps the version like a change of markers.

        Returns:
            The row's version.
        """
        now = self._now()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT markers_json, status, version FROM item_publish_state WHERE server_id=? AND item_id=?",
                (server_id, item_id),
            ).fetchone()
            if markers is None:
                markers_json = row["markers_json"] if row else "[]"
            else:
                markers_json = self._markers_to_json(sorted(markers, key=lambda m: (m.start_ms, m.type.value)))
            kept_before = self._kept_types(conn, server_id, item_id)
            kept = kept_before if kept_types is None else frozenset(kept_types)
            if (
                row is not None
                and row["status"] == status
                and self._shown(row["markers_json"]) == self._shown(markers_json)
                and kept == kept_before
            ):
                return int(row["version"])
            version = int(row["version"]) + 1 if row else 1
            conn.execute(
                "INSERT OR REPLACE INTO item_publish_state (server_id, item_id, markers_json, status, version, "
                "updated_at) VALUES (?,?,?,?,?,?)",
                (server_id, item_id, markers_json, status, version, now),
            )
            if kept != kept_before:
                conn.execute("DELETE FROM item_kept_types WHERE server_id=? AND item_id=?", (server_id, item_id))
                conn.executemany(
                    "INSERT INTO item_kept_types (server_id, item_id, type) VALUES (?,?,?)",
                    [(server_id, item_id, mtype.value) for mtype in sorted(kept, key=lambda t: t.value)],
                )
        return version

    def published_to_item(self, server_id: str, item_id: str) -> bool:
        """Whether our markers are (or may still be) on this server item, from any file's publish.

        Plex shows one marker set per item across all its versions, and its markers can't be told apart from ours, so
        a version that was never published itself must still not read them back as a second opinion. An item whose
        types are all kept as Plex's own counts too: a kept type can still hold a marker of ours.
        """
        row = self.get_item_publish_state(server_id, item_id)
        return bool(row and (row.markers or row.kept_types))

    def set_publish_basis(self, file_id: int, server_id: str, *, decided_hash: str, item_version: int) -> None:
        """Remember what a file's publish to a server was based on (cleared when the file changes)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO publish_basis (file_id, server_id, decided_hash, item_version) VALUES (?,?,?,?)",
                (file_id, server_id, decided_hash, item_version),
            )

    def clear_publish_basis(self, file_id: int, server_id: str) -> None:
        """Forget the basis after a publish attempt that didn't write, so the next run can't skip."""
        with self._tx() as conn:
            conn.execute("DELETE FROM publish_basis WHERE file_id=? AND server_id=?", (file_id, server_id))

    def get_publish_basis(self, file_id: int, server_id: str) -> tuple[str, int] | None:
        """``(decided_hash, item_version)`` of the file's last publish to the server, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT decided_hash, item_version FROM publish_basis WHERE file_id=? AND server_id=?",
                (file_id, server_id),
            ).fetchone()
        return (r["decided_hash"], int(r["item_version"])) if r else None

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
