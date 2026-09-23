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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from loguru import logger

from .decide import DecisionStatus, TypeDecision
from .models import SERVER_SOURCES, Candidate, FileIdentity, Marker, MarkerType, Source
from .outcomes import VERSIONS_WAITING, is_kept_own
from .sources.server_markers import importer_database

SCHEMA_VERSION = 2
_SERVER_SOURCE_VALUES = (Source.SERVER_MARKERS.value, Source.SERVER_MARKERS_IMPORTED.value)
# A file Check servers took for a server isn't taken for it again sooner (it may not have run: gone from disk, cancelled).
_RECHECK_TAKEN_AGAIN = timedelta(days=1)
# ``meta`` key: the last file id whose fingerprint the cache sweep checked (``fingerprint_checks``).
_FINGERPRINT_CHECKED_UP_TO = "fingerprint_checked_up_to"
# ``meta`` key: when the weekly online re-check is due next (``triggers.schedule_online_recheck``).
_ONLINE_RECHECK_DUE = "online_recheck_due"
# The fingerprint window whose points ``season_pairs`` runs are matched from: ``audio.fingerprint.WINDOW`` (which
# imports this module, so it can't be imported here; test_store_audio pins the two together).
SEASON_PAIR_WINDOW = "intro"

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
    # ``locked_at`` is when the user locked the row, kept apart from ``updated_at`` so re-deciding or re-publishing
    # around a lock can't overwrite the date the Inspector shows for the user's own edit. NULL on unlocked rows.
    """CREATE TABLE IF NOT EXISTS markers (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        start_ms INTEGER NOT NULL,
        end_ms INTEGER NOT NULL,
        decided_by TEXT NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        locked_at TEXT,
        PRIMARY KEY (file_id, type))""",
    # ``decided_by`` holds the PROPOSED marker's sources (JSON list, ``[]`` when there is no proposal): a proposal has
    # no row in ``markers``, and the editor shows what it was based on before the user overrides it.
    """CREATE TABLE IF NOT EXISTS decisions (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        proposed_start_ms INTEGER,
        proposed_end_ms INTEGER,
        settings_fingerprint TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        decided_by TEXT NOT NULL DEFAULT '[]',
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
    "CREATE INDEX IF NOT EXISTS idx_publish_state_item ON publish_state(server_id, item_id)",
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
    # The version files a server item had when this app last wrote its markers (Plex: one set for every version).
    """CREATE TABLE IF NOT EXISTS item_versions (
        server_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        files_json TEXT NOT NULL,
        PRIMARY KEY (server_id, item_id))""",
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
    # What a local detector's last failed attempt for a file was based on (season audio: its own fingerprint failed), so
    # other files' runs don't ask for it again until that changes.
    """CREATE TABLE IF NOT EXISTS detector_failures (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        source TEXT NOT NULL,
        signature TEXT NOT NULL,
        failed_at TEXT NOT NULL,
        PRIMARY KEY (file_id, source))""",
    # The season's limit on an intro chapter deciding alone that a file's last decisions used (NULL: none), so a later
    # run of any episode of the season can tell which siblings were decided with a limit that has since changed.
    """CREATE TABLE IF NOT EXISTS intro_chapter_limits (
        file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        limit_ms INTEGER,
        decided_at TEXT NOT NULL)""",
    # A file another episode's season step couldn't probe, with the identity it had then: it isn't probed again (up to
    # a 60 s ffprobe on a checking thread) until that identity changes or the entry is old. Not tied to a file row: an
    # unreadable file never gets one.
    """CREATE TABLE IF NOT EXISTS member_probe_failures (
        canonical_path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        failed_at TEXT NOT NULL)""",
    # A file whose fingerprint failed, with the identity it had then: other episodes' season steps don't run ffmpeg on
    # it again (up to 300 s on a worker, per sibling) until that identity changes or the entry is old. Its own run and a
    # forced re-detect still try. Kept apart from probe failures, which also stop a member's chapters being read.
    """CREATE TABLE IF NOT EXISTS member_fingerprint_failures (
        canonical_path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        failed_at TEXT NOT NULL)""",
    # A file whose credit text decode timed out (a stalled read, spec §5.4), with the identity it had then: its credit
    # text isn't decoded again (up to 600 s per decode on a worker, three decodes at worst) until that identity changes,
    # the entry is a day old, a forced re-detect, or a credit text answer is stored for it. Not tied to a file row, like
    # the member failures above.
    """CREATE TABLE IF NOT EXISTS credits_text_timeouts (
        canonical_path TEXT PRIMARY KEY,
        size INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL,
        failed_at TEXT NOT NULL)""",
    # A server's empty (or unusable) answer for a file, asked again by Check servers: how many times it was read again
    # without markers (the backoff step; gone once the answer has markers or the file changes), when the last re-read
    # that couldn't replace the answer happened (the backoff counts from it), and when Check servers last took the
    # file for it: once the file ran, or at once for a file it can't run (gone from disk, not in that library), so
    # those take turns with the rest.
    """CREATE TABLE IF NOT EXISTS server_marker_rereads (
        file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
        server_id TEXT NOT NULL,
        rereads INTEGER NOT NULL DEFAULT 0,
        taken_at TEXT,
        failed_reread_at TEXT,
        PRIMARY KEY (file_id, server_id))""",
    # When Check servers last listed a drifted server item's files, so items it can't fix take turns with the rest.
    """CREATE TABLE IF NOT EXISTS drift_listings (
        server_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        listed_at TEXT NOT NULL,
        PRIMARY KEY (server_id, item_id))""",
    # How many times Check servers ran the files of a server item whose last publish failed, since the failure that
    # item row version records, and when it last did (the RECHECK_AFTER backoff counts from it).
    """CREATE TABLE IF NOT EXISTS failed_item_retries (
        server_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        item_version INTEGER NOT NULL,
        retries INTEGER NOT NULL,
        retried_at TEXT NOT NULL,
        PRIMARY KEY (server_id, item_id))""",
    """CREATE TABLE IF NOT EXISTS source_usage (
        source TEXT NOT NULL,
        day TEXT NOT NULL,
        used INTEGER,
        limit_ INTEGER,
        remaining INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source, day))""",
    # An online source's last answer for each episode of a series ("found" or "no entry"), keyed by the id the lookup
    # was sent with, so a series the source has nothing for stops costing its daily budget
    # (``record_series_lookup``). Not tied to a file row: a replaced episode keeps its series' answers.
    """CREATE TABLE IF NOT EXISTS series_lookups (
        source TEXT NOT NULL,
        series_key TEXT NOT NULL,
        episode TEXT NOT NULL,
        found INTEGER NOT NULL,
        answered_at TEXT NOT NULL,
        PRIMARY KEY (source, series_key, episode))""",
    # When a source's lookups of a series were paused: the pause runs from here (later "no entry" answers don't extend
    # it), and only "no entry" answers after it ended can start the next one. A separate table, so a markers.db that
    # already has ``series_lookups`` only gains it.
    """CREATE TABLE IF NOT EXISTS series_pauses (
        source TEXT NOT NULL,
        series_key TEXT NOT NULL,
        paused_at TEXT NOT NULL,
        PRIMARY KEY (source, series_key))""",
)

# Ordered migrations: _MIGRATIONS[v] holds the statements that take an existing database from schema
# version v to v+1. `_SCHEMA` is CREATE TABLE IF NOT EXISTS only, so a column added there alone never
# reaches an existing install -- every new column needs its ALTER TABLE here too. A future bump appends
# `SCHEMA_VERSION - 1: (...)` here rather than changing this class's open logic.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "ALTER TABLE markers ADD COLUMN locked_at TEXT",
        "ALTER TABLE decisions ADD COLUMN decided_by TEXT NOT NULL DEFAULT '[]'",
    ),
}

# The reason stored (and shown) for a type the user locked in the Inspector. `decide.decide()` writes the same words
# for a locked marker on the next run; `test_store.py` pins the two together so a save and a run never disagree.
LOCKED_BY_USER = "locked by user"
# The reason a just-unlocked type carries until the next detection run decides it again (`unlock_markers`).
UNLOCKED_PENDING = "unlocked; the next run decides this type again"


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
    """One stored evidence row; ``type`` is None for "looked it up, nothing there".

    ``label`` is the candidate's own title, e.g. a chapter name or season audio's "10/10" (empty when it has none).
    """

    source: Source
    origin: str
    type: MarkerType | None
    start_ms: int | None
    end_ms: int | None
    confidence: float | None
    detail: str
    fetched_at: str
    label: str = ""


@dataclass(frozen=True)
class DecisionRow:
    """Stored decision for one marker type.

    ``decided_by`` is what the *proposed* marker was based on (empty when the row proposes nothing): the Inspector's
    editor shows it before the user overrides the proposal, and a proposal never reaches the ``markers`` table.
    """

    type: MarkerType
    status: DecisionStatus
    reason: str
    proposed_start_ms: int | None
    proposed_end_ms: int | None
    settings_fingerprint: str
    decided_at: str
    decided_by: tuple[str, ...] = ()


def _same_identity_on_disk(path: str, size: int, mtime_ns: int) -> bool:
    """Whether the file on disk is still the one a record describes (path + size + mtime, spec §6.1)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return (st.st_size, st.st_mtime_ns) == (size, mtime_ns)


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
    # The item's version files when this app last wrote it (Plex); None when not recorded.
    item_files: tuple[str, ...] | None = None
    # Types a file of the item left to the server's own marker without deciding them (the kept status,
    # ``outcomes.is_kept_own``), read from those files' decisions. Filled by ``published_items`` only, so Check servers
    # reads them back; kept apart from ``kept_types``, which ``published_to_item`` reads.
    own_types: frozenset[MarkerType] = frozenset()


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
class FingerprintCheck:
    """A fingerprinted file for the cache sweep to look for on disk, with the identity its row had when listed."""

    file_id: int
    canonical_path: str
    size: int
    mtime_ns: int


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
        self._clock = clock or (lambda: datetime.now(UTC))
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

        The version is read a *second* time as the first statement inside that transaction, and both
        the refusal and the migrations are decided from that read. Two processes opening the same store
        at once would otherwise both see the old version outside it: two of this build would both run
        ``_MIGRATIONS``' bare ``ALTER TABLE`` and the second would die with "duplicate column name",
        and a build older than the one that won the race would open a schema it doesn't support.
        ``BEGIN IMMEDIATE`` serialises them, so the one that waited sees the version the other just
        wrote. Only the *first* refusal leaves the file untouched -- by the second, WAL has already
        rewritten it -- but a refused open closes the connection either way and writes nothing.

        Raises:
            RuntimeError: The database's ``schema_version`` is newer than this build supports.
        """
        self._refuse_newer(self._schema_version(self._conn))
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._tx() as conn:
            current = self._schema_version(conn)
            self._refuse_newer(current)
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

    @staticmethod
    def _refuse_newer(current: int) -> None:
        """Stop opening a database written by a build that knows more than this one.

        Args:
            current: The version just read.

        Raises:
            RuntimeError: ``current`` is newer than this build supports.
        """
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"markers.db was created by a newer version (schema {current}, this build supports {SCHEMA_VERSION})"
            )

    @staticmethod
    def _schema_version(conn: sqlite3.Connection) -> int:
        """The recorded ``schema_version``, or this build's when the database has nothing to migrate from.

        Args:
            conn: The connection to read on.

        Returns:
            The version. A database with no ``meta`` table (or no row in it) is brand new, so there is no
            version to refuse or migrate from and this build's own is the answer.
        """
        meta_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if not meta_exists:
            return SCHEMA_VERSION
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        return int(row["value"]) if row else SCHEMA_VERSION

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
        detector runs and failures, the season intro-chapter limit, Check servers' re-read counts and every server's
        ``publish_basis``, so the next run offers the markers to every server again
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
                        "detector_failures",
                        "intro_chapter_limits",
                        "server_marker_rereads",
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

    def files_in_review(self) -> list[str]:
        """Canonical paths of the files with at least one marker type in Needs review, sorted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT f.canonical_path FROM decisions d JOIN files f ON f.id = d.file_id "
                "WHERE d.status=? ORDER BY f.canonical_path",
                (DecisionStatus.NEEDS_REVIEW.value,),
            ).fetchall()
        return [r["canonical_path"] for r in rows]

    def files_waiting_for_other_versions(self) -> list[str]:
        """Canonical paths of the files whose last publish to a server waits for its item's other versions to agree
        (``outcomes.VERSIONS_WAITING``), sorted."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT f.canonical_path FROM publish_state p JOIN files f ON f.id = p.file_id "
                "WHERE p.status='waiting' AND substr(p.message, 1, ?) = ? ORDER BY f.canonical_path",
                (len(VERSIONS_WAITING), VERSIONS_WAITING),
            ).fetchall()
        return [r["canonical_path"] for r in rows]

    def files_with_old_empty_lookups(self, sources: Iterable[Source], before: datetime) -> list[str]:
        """Canonical paths of the files whose stored lookup of any of these online sources found nothing ("no entry")
        and was made before ``before``, sorted.

        Args:
            sources: The online sources.
            before: The time (UTC) such a lookup is older than.

        Returns:
            The paths; empty when no source is given.
        """
        values = [source.value for source in sources]
        if not values:
            return []
        marks = ",".join("?" * len(values))
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT f.canonical_path FROM files f JOIN (SELECT file_id FROM evidence "
                f"WHERE origin='' AND source IN ({marks}) GROUP BY file_id, source "  # noqa: S608
                "HAVING COUNT(type) = 0 AND MAX(fetched_at) < ?) e ON e.file_id = f.id ORDER BY f.canonical_path",
                (*values, before.isoformat()),
            ).fetchall()
        return [r["canonical_path"] for r in rows]

    def online_recheck_due(self) -> datetime | None:
        """When the weekly online re-check is due next.

        Returns:
            The stored time (None = never set).

        Raises:
            ValueError: The stored value isn't a time.
        """
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (_ONLINE_RECHECK_DUE,)).fetchone()
        return datetime.fromisoformat(row["value"]) if row else None

    def set_online_recheck_due(self, due: datetime) -> None:
        """Store when the weekly online re-check is due next.

        Args:
            due: The time (UTC).
        """
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_ONLINE_RECHECK_DUE, due.isoformat()),
            )

    def record_member(
        self,
        identity: FileIdentity,
        *,
        duration_ms: int,
        season_key: str,
        chapters: list[Candidate],
        chapter_version: int,
    ) -> FileRecord | None:
        """Record a file another file's season step probed, with its chapters, in one transaction.

        A file the store never saw is added. A known file is refreshed only while its row still has the identity that
        was probed: the season step never changes another file's identity, which only that file's own run (holding its
        path lock) may do.

        Args:
            identity: The identity the file had when it was probed.
            duration_ms: Its duration.
            season_key: The season folder, kept when the row already has one.
            chapters: Its chapter candidates.
            chapter_version: The chapter rules version that made them.

        Returns:
            The record, or None when the row has another identity now.
        """
        now = self._now()
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM files WHERE canonical_path=?", (identity.canonical_path,)).fetchone()
            if row is None:
                file_id = conn.execute(
                    "INSERT INTO files (canonical_path, size, mtime_ns, duration_ms, season_key, is_movie, updated_at) "
                    "VALUES (?,?,?,?,?,0,?)",
                    (identity.canonical_path, identity.size, identity.mtime_ns, duration_ms, season_key, now),
                ).lastrowid
            elif (row["size"], row["mtime_ns"]) != (identity.size, identity.mtime_ns):
                return None
            else:
                file_id = row["id"]
                conn.execute(
                    "UPDATE files SET duration_ms=?, season_key=COALESCE(season_key, ?), updated_at=? WHERE id=?",
                    (duration_ms, season_key, now, file_id),
                )
            self._write_evidence(conn, file_id, Source.CHAPTERS, chapters, "", "", chapter_version, (), now)
            new_row = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return self._file(new_row)

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
            if source in SERVER_SOURCES and origin:
                self._count_server_reread(conn, file_id, origin, has_markers=bool(candidates))
            self._write_evidence(conn, file_id, source, candidates, origin, detail, version, also_replaces, now)

    def replace_detector_answer(
        self,
        file_id: int,
        answers: Mapping[Source, list[Candidate]],
        *,
        version: int,
        run: tuple[Source, str] | None = None,
    ) -> None:
        """Store a local detector's answer under each of its sources, and what it was based on, in one transaction.

        A failure (or the process ending) part way leaves the old answer and the old basis, so the answer stays due. A
        credit text answer (found or not) means its decode finished, so the file's decode timeout is forgotten: an
        answer asked again the same day (a detector version bump) isn't held back by a timeout the answer replaced.

        Args:
            file_id: The file.
            answers: Candidates per source the detector stores under (an empty list: "looked, nothing there").
            version: The detector's version.
            run: ``(source, signature)`` for ``detector_runs``, or None when the detector keeps no basis.
        """
        now = self._now()
        with self._tx() as conn:
            for source in sorted(answers, key=lambda s: s.value):
                self._write_evidence(conn, file_id, source, answers[source], "", "", version, (), now)
            if run is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO detector_runs (file_id, source, signature, run_at) VALUES (?,?,?,?)",
                    (file_id, run[0].value, run[1], now),
                )
            if Source.CREDITS_TEXT in answers:
                conn.execute(
                    "DELETE FROM credits_text_timeouts "
                    "WHERE canonical_path = (SELECT canonical_path FROM files WHERE id=?)",
                    (file_id,),
                )

    @staticmethod
    def _count_server_reread(conn: sqlite3.Connection, file_id: int, server_id: str, *, has_markers: bool) -> None:
        """Before a server's new answer for a file is stored: an answer with markers forgets the re-reads; an empty one
        that replaces an empty one counts one more (``server_rechecks_due`` backs off by that count)."""
        if has_markers:
            conn.execute("DELETE FROM server_marker_rereads WHERE file_id=? AND server_id=?", (file_id, server_id))
            return
        marks = ",".join("?" * len(SERVER_SOURCES))
        stored, typed = conn.execute(
            f"SELECT COUNT(*), COUNT(type) FROM evidence WHERE file_id=? AND origin=? AND source IN ({marks})",  # noqa: S608
            (file_id, server_id, *_SERVER_SOURCE_VALUES),
        ).fetchone()
        if stored and not typed:
            conn.execute(
                "INSERT INTO server_marker_rereads (file_id, server_id, rereads) VALUES (?,?,1) "
                "ON CONFLICT(file_id, server_id) DO UPDATE SET rereads = rereads + 1",
                (file_id, server_id),
            )

    @staticmethod
    def _write_evidence(
        conn: sqlite3.Connection,
        file_id: int,
        source: Source,
        candidates: list[Candidate],
        origin: str,
        detail: str,
        version: int | None,
        also_replaces: Iterable[Source],
        now: str,
    ) -> None:
        """:meth:`replace_evidence` inside an open transaction."""
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
                label=r["label"] or "",
            )
            for r in self._evidence_query(file_id)
        ]

    def get_evidence(self, file_id: int) -> list[Candidate]:
        """Candidates for the decision rules (empty lookups excluded).

        A candidate's ``origin`` is rebuilt from the row's own ``label`` (e.g. a chapter title) when set,
        falling back to the replace/lookup key in ``origin`` -- the two differ for chapters, which share one
        lookup key ("") across many differently-titled candidates. An importer plugin's copy gets ``copied_from``
        from the plugin names its row's detail carries, so rows stored before the database was read get it too.
        """
        return [
            Candidate(
                MarkerType(r["type"]),
                r["start_ms"],
                r["end_ms"],
                Source(r["source"]),
                r["confidence"] if r["confidence"] is not None else 1.0,
                r["label"] or r["origin"],
                importer_database(r["detail"]) if r["source"] == Source.SERVER_MARKERS_IMPORTED.value else "",
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
                    "settings_fingerprint, decided_at, decided_by) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        file_id,
                        mtype.value,
                        d.status.value,
                        d.reason,
                        proposed.start_ms if proposed else None,
                        proposed.end_ms if proposed else None,
                        settings_fingerprint,
                        now,
                        json.dumps(list(proposed.decided_by) if proposed else []),
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
        """Store one user-locked marker; detection never replaces it.

        The low-level primitive: it writes the ``markers`` row only. The Inspector editor calls
        :meth:`save_user_markers`, which also records the decision so the file doesn't keep reading as
        "Needs review" until the next run.
        """
        now = self._now()
        with self._tx() as conn:
            self._write_lock(conn, file_id, marker, now)

    @staticmethod
    def _write_lock(conn: sqlite3.Connection, file_id: int, marker: Marker, now: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO markers (file_id, type, start_ms, end_ms, decided_by, locked, updated_at, "
            "locked_at) VALUES (?,?,?,?,?,1,?,?)",
            (file_id, marker.type.value, marker.start_ms, marker.end_ms, json.dumps(list(marker.decided_by)), now, now),
        )

    def save_user_markers(
        self, file_id: int, markers: Iterable[Marker], *, settings_fingerprint: str
    ) -> dict[MarkerType, Marker]:
        """Lock the user's own markers and record each type as decided by them, in one transaction.

        The whole save lands or none of it does, and it lands before any server is contacted (plan ruling P-R1), so a
        publish that fails can never lose the edit. The decision row is rewritten to
        :data:`LOCKED_BY_USER` so the Inspector and the Season view stop saying "Needs review" straight away, with the
        same words the next detection run writes for a locked type. The proposal the lock replaces is dropped for the
        same reason: a run's own decision for a locked type carries none, so keeping it would make the row flip back
        on the next run.

        Args:
            file_id: The file's row id.
            markers: The markers to lock (one per type; a repeated type keeps the last).
            settings_fingerprint: ``GlobalMarkersSettings.detection_fingerprint()`` right now, so the next run doesn't
                rewrite these rows only because their fingerprint was stale.

        Returns:
            The locked markers by type, as stored.
        """
        saved = {m.type: replace(m, locked=True) for m in markers}
        now = self._now()
        with self._tx() as conn:
            for mtype, marker in saved.items():
                self._write_lock(conn, file_id, marker, now)
                conn.execute(
                    "INSERT OR REPLACE INTO decisions (file_id, type, status, reason, proposed_start_ms, "
                    "proposed_end_ms, settings_fingerprint, decided_at, decided_by) VALUES (?,?,?,?,NULL,NULL,?,?,'[]')",
                    (file_id, mtype.value, DecisionStatus.DECIDED.value, LOCKED_BY_USER, settings_fingerprint, now),
                )
        return saved

    def unlock_markers(self, file_id: int, types: Iterable[MarkerType]) -> frozenset[MarkerType]:
        """Drop the user's lock on these types and send each back to "Needs review" until the next run decides it.

        The stored decision can't be restored here — a locked type's row says "locked by user", not what detection had
        found — so the type is left with no answer and a stale fingerprint, which makes the next run re-decide and
        re-publish it. What the servers still show is left alone on purpose: the next run replaces it.

        Args:
            file_id: The file's row id.
            types: The marker types to unlock.

        Returns:
            The types that were actually locked (the rest were already unlocked or absent).
        """
        now = self._now()
        unlocked: set[MarkerType] = set()
        with self._tx() as conn:
            for mtype in types:
                cur = conn.execute(
                    "DELETE FROM markers WHERE file_id=? AND type=? AND locked=1", (file_id, mtype.value)
                )
                if not cur.rowcount:
                    continue
                unlocked.add(mtype)
                # An empty fingerprint never equals a real one, so `_decisions_changed` always re-saves this type.
                conn.execute(
                    "INSERT OR REPLACE INTO decisions (file_id, type, status, reason, proposed_start_ms, "
                    "proposed_end_ms, settings_fingerprint, decided_at, decided_by) VALUES (?,?,?,?,NULL,NULL,'',?,'[]')",
                    (file_id, mtype.value, DecisionStatus.NEEDS_REVIEW.value, UNLOCKED_PENDING, now),
                )
        return frozenset(unlocked)

    def locked_at(self, file_id: int) -> dict[MarkerType, str]:
        """When the user locked each locked marker of a file, by type (rows locked before this column are left out)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT type, locked_at FROM markers WHERE file_id=? AND locked=1 AND locked_at IS NOT NULL",
                (file_id,),
            ).fetchall()
        return {MarkerType(r["type"]): r["locked_at"] for r in rows}

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
                tuple(json.loads(r["decided_by"] or "[]")),
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

        One that replaces a stored fingerprint (made another way: ``get_fingerprint`` refused it) also drops the matcher
        runs cached with the old one.

        Returns:
            False (nothing stored) when the file's row has another identity now: the file was replaced while ffmpeg
            ran, and its own next run fingerprints the new file.
        """
        with self._tx() as conn:
            row = conn.execute("SELECT size, mtime_ns FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None or (row["size"], row["mtime_ns"]) != (size, mtime_ns):
                return False
            replaced = conn.execute(
                "SELECT 1 FROM fingerprints WHERE file_id=? AND window=?", (file_id, window)
            ).fetchone()
            if replaced:
                conn.execute("DELETE FROM season_pairs WHERE file_a=? OR file_b=?", (file_id, file_id))
            conn.execute(
                "INSERT OR REPLACE INTO fingerprints (file_id, window, start_s, length_s, algorithm, points) "
                "VALUES (?,?,?,?,?,?)",
                (file_id, window, start_s, length_s, algorithm, points),
            )
        return True

    @staticmethod
    def _made_as(row: sqlite3.Row, algorithm: int | None, length_s: float | None) -> bool:
        # length_s is compared to the millisecond: the ffmpeg command passes it with three decimals.
        return (algorithm is None or row["algorithm"] == algorithm) and (
            length_s is None or round(row["length_s"], 3) == round(length_s, 3)
        )

    def get_fingerprint(
        self, file_id: int, window: str, *, algorithm: int | None = None, length_s: float | None = None
    ) -> StoredFingerprint | None:
        """The cached fingerprint of a file.

        Args:
            file_id: The file.
            window: The fingerprinted window.
            algorithm: The chromaprint algorithm it must have been made with (None: any).
            length_s: The window length it must have been made with (None: any).

        Returns:
            The fingerprint, or None when it was never fingerprinted (or changed since), or was made with another
            algorithm or window length (it is computed again).
        """
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM fingerprints WHERE file_id=? AND window=?", (file_id, window)
            ).fetchone()
        if r is None or not self._made_as(r, algorithm, length_s):
            return None
        return StoredFingerprint(
            r["file_id"], r["window"], r["start_s"], r["length_s"], r["algorithm"], bytes(r["points"])
        )

    def has_fingerprint(
        self, file_id: int, window: str, *, algorithm: int | None = None, length_s: float | None = None
    ) -> bool:
        """Whether ``get_fingerprint`` with the same arguments finds one, without reading its points."""
        with self._lock:
            r = self._conn.execute(
                "SELECT algorithm, length_s FROM fingerprints WHERE file_id=? AND window=?", (file_id, window)
            ).fetchone()
        return r is not None and self._made_as(r, algorithm, length_s)

    def fingerprint_checks(self, limit: int) -> list[FingerprintCheck]:
        """The next files with a cached fingerprint for the cache sweep to look for on disk (nothing is marked).

        In file id order from just after the cursor ``finish_fingerprint_checks`` left, wrapping round to the lowest id
        once the highest is passed, so each file is checked once per pass over the cache.

        Args:
            limit: Most files to return.

        Returns:
            Each file with its row's identity, in that order.
        """
        if limit <= 0:
            return []
        query = (
            "SELECT p.file_id, f.canonical_path, f.size, f.mtime_ns FROM fingerprints p JOIN files f ON f.id = p.file_id "
            "WHERE p.file_id {} ? GROUP BY p.file_id ORDER BY p.file_id LIMIT ?"
        )
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (_FINGERPRINT_CHECKED_UP_TO,)).fetchone()
            after = int(row["value"]) if row else 0
            batch = self._conn.execute(query.format(">"), (after, limit)).fetchall()
            if len(batch) < limit:
                batch += self._conn.execute(query.format("<="), (after, limit - len(batch))).fetchall()
        return [FingerprintCheck(r["file_id"], r["canonical_path"], r["size"], r["mtime_ns"]) for r in batch]

    def finish_fingerprint_checks(self, checked_up_to: int, gone: Iterable[FingerprintCheck]) -> int:
        """Record a sweep's checks in one transaction: drop the cache of the files found gone, and move the cursor.

        A gone file's fingerprints, the matcher runs cached with them and its unreadable-member entries go. Its
        ``files`` row stays, with the decisions, locked markers and publish records tied to it. A file whose row has
        another identity than when it was listed is left alone: a new file came to that path, and its own run may
        already have fingerprinted it.

        Args:
            checked_up_to: The id of the last file checked (``fingerprint_checks`` order); the next sweep starts after it.
            gone: Each file found gone, as ``fingerprint_checks`` listed it.

        Returns:
            How many files' cache was dropped.
        """
        dropped = 0
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_FINGERPRINT_CHECKED_UP_TO, str(checked_up_to)),
            )
            for check in gone:
                unchanged = conn.execute(
                    "SELECT 1 FROM files WHERE id=? AND size=? AND mtime_ns=?",
                    (check.file_id, check.size, check.mtime_ns),
                ).fetchone()
                if unchanged is None:
                    continue
                dropped += 1
                conn.execute("DELETE FROM fingerprints WHERE file_id=?", (check.file_id,))
                conn.execute("DELETE FROM season_pairs WHERE file_a=? OR file_b=?", (check.file_id, check.file_id))
                for table in ("member_probe_failures", "member_fingerprint_failures"):
                    conn.execute(f"DELETE FROM {table} WHERE canonical_path=?", (check.canonical_path,))
        return dropped

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
        self,
        file_a: int,
        file_b: int,
        matcher_version: int,
        runs: list[tuple[float, float, float, float]],
        *,
        identity_a: tuple[int, int],
        identity_b: tuple[int, int],
    ) -> bool:
        """Cache matcher runs between two files.

        Args:
            file_a: The matcher's first file.
            file_b: The second file.
            matcher_version: The version the runs are valid for.
            runs: The runs.
            identity_a: ``(size, mtime_ns)`` of the first file as it was matched.
            identity_b: The same for the second file.

        Returns:
            False (nothing stored) when either file's row has another identity now or its fingerprint is gone: one of
            them was replaced while the season was matched.
        """
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT f.id, f.size, f.mtime_ns FROM files f JOIN fingerprints p ON p.file_id = f.id "
                "AND p.window=? WHERE f.id IN (?, ?)",
                (SEASON_PAIR_WINDOW, file_a, file_b),
            ).fetchall()
            matched = {file_a: tuple(identity_a), file_b: tuple(identity_b)}
            if len(rows) != 2 or any((r["size"], r["mtime_ns"]) != matched[r["id"]] for r in rows):
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

    def get_detector_failure(self, file_id: int, source: Source) -> str | None:
        """The signature a local detector's last failed attempt for a file was based on, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT signature FROM detector_failures WHERE file_id=? AND source=?", (file_id, source.value)
            ).fetchone()
        return r["signature"] if r else None

    def set_detector_failure(self, file_id: int, source: Source, signature: str) -> None:
        """Record what a local detector's failed attempt for a file was based on (replaces the previous one)."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO detector_failures (file_id, source, signature, failed_at) VALUES (?,?,?,?)",
                (file_id, source.value, signature, self._now()),
            )

    def get_intro_chapter_limit(self, file_id: int) -> tuple[bool, int | None]:
        """The season intro-chapter limit a file's last decisions used.

        Returns:
            ``(stored, limit_ms)``: whether one was stored, and the limit (None: no limit applied).
        """
        with self._lock:
            r = self._conn.execute("SELECT limit_ms FROM intro_chapter_limits WHERE file_id=?", (file_id,)).fetchone()
        return (False, None) if r is None else (True, r["limit_ms"])

    def set_intro_chapter_limit(self, file_id: int, limit_ms: int | None) -> None:
        """Record the season intro-chapter limit a file's decisions were just made with."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO intro_chapter_limits (file_id, limit_ms, decided_at) VALUES (?,?,?)",
                (file_id, limit_ms, self._now()),
            )

    def record_member_probe_failure(
        self, identity: FileIdentity, failed_at: datetime, *, forget_before: datetime
    ) -> None:
        """Remember that a season member with this identity couldn't be probed (replaces the path's older entry).

        Entries that no longer keep a member from being probed are forgotten in the same write: the table isn't tied to
        file rows, and a path gone from disk is never probed or recorded again, so its entry would otherwise stay.

        Args:
            identity: The file as it was when probing failed.
            failed_at: When (the job's clock).
            forget_before: Entries of any path that failed before this are removed.
        """
        with self._tx() as conn:
            conn.execute("DELETE FROM member_probe_failures WHERE failed_at < ?", (forget_before.isoformat(),))
            conn.execute(
                "INSERT OR REPLACE INTO member_probe_failures (canonical_path, size, mtime_ns, failed_at) "
                "VALUES (?,?,?,?)",
                (identity.canonical_path, identity.size, identity.mtime_ns, failed_at.isoformat()),
            )

    def member_probe_failed_at(self, identity: FileIdentity) -> datetime | None:
        """When probing a season member with exactly this identity last failed, or None (never, or another identity)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT failed_at FROM member_probe_failures WHERE canonical_path=? AND size=? AND mtime_ns=?",
                (identity.canonical_path, identity.size, identity.mtime_ns),
            ).fetchone()
        return datetime.fromisoformat(r["failed_at"]) if r else None

    def record_member_fingerprint_failure(
        self, identity: FileIdentity, failed_at: datetime, *, forget_before: datetime
    ) -> None:
        """Remember that a file with this identity couldn't be fingerprinted (replaces the path's older entry).

        Entries that no longer keep a file from being fingerprinted are forgotten in the same write, as for probe
        failures.

        Args:
            identity: The file as it was when ffmpeg failed.
            failed_at: When (the job's clock).
            forget_before: Entries of any path that failed before this are removed.
        """
        with self._tx() as conn:
            conn.execute("DELETE FROM member_fingerprint_failures WHERE failed_at < ?", (forget_before.isoformat(),))
            conn.execute(
                "INSERT OR REPLACE INTO member_fingerprint_failures (canonical_path, size, mtime_ns, failed_at) "
                "VALUES (?,?,?,?)",
                (identity.canonical_path, identity.size, identity.mtime_ns, failed_at.isoformat()),
            )

    def member_fingerprint_failed_at(self, identity: FileIdentity) -> datetime | None:
        """When fingerprinting a file with exactly this identity last failed, or None (never, or another identity)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT failed_at FROM member_fingerprint_failures WHERE canonical_path=? AND size=? AND mtime_ns=?",
                (identity.canonical_path, identity.size, identity.mtime_ns),
            ).fetchone()
        return datetime.fromisoformat(r["failed_at"]) if r else None

    def record_credits_text_timeout(
        self, identity: FileIdentity, failed_at: datetime, *, forget_before: datetime
    ) -> None:
        """Remember that a file with this identity timed out decoding its credit text (replaces the path's older entry).

        Entries that no longer hold a file back are forgotten in the same write, as for member failures.

        Args:
            identity: The file as it was when the decode timed out.
            failed_at: When (the job's clock).
            forget_before: Entries of any path that timed out before this are removed.
        """
        with self._tx() as conn:
            conn.execute("DELETE FROM credits_text_timeouts WHERE failed_at < ?", (forget_before.isoformat(),))
            conn.execute(
                "INSERT OR REPLACE INTO credits_text_timeouts (canonical_path, size, mtime_ns, failed_at) "
                "VALUES (?,?,?,?)",
                (identity.canonical_path, identity.size, identity.mtime_ns, failed_at.isoformat()),
            )

    def credits_text_timed_out_at(self, identity: FileIdentity) -> datetime | None:
        """When decoding this identity's credit text last timed out, or None (never, or another identity)."""
        with self._lock:
            r = self._conn.execute(
                "SELECT failed_at FROM credits_text_timeouts WHERE canonical_path=? AND size=? AND mtime_ns=?",
                (identity.canonical_path, identity.size, identity.mtime_ns),
            ).fetchone()
        return datetime.fromisoformat(r["failed_at"]) if r else None

    def get_item_publish_state(self, server_id: str, item_id: str) -> ItemPublishStateRow | None:
        """What this app last left on a server item, or None when it never published there."""
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM item_publish_state WHERE server_id=? AND item_id=?", (server_id, item_id)
            ).fetchone()
            if r is None:
                return None
            kept = self._kept_types(self._conn, server_id, item_id)
            files_row = self._conn.execute(
                "SELECT files_json FROM item_versions WHERE server_id=? AND item_id=?", (server_id, item_id)
            ).fetchone()
        item_files = tuple(json.loads(files_row["files_json"])) if files_row else None
        return ItemPublishStateRow(
            r["server_id"],
            r["item_id"],
            self._markers_from_json(r["markers_json"]),
            r["status"],
            r["version"],
            r["updated_at"],
            kept,
            item_files,
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
        item_files: Iterable[str] | None = None,
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
            status: ``written``, ``failed``, or ``gone`` (the server no longer has the item; ``mark_item_gone``).
            kept_types: Types whose rows on the item are the server's own and kept there; None keeps the recorded
                ones. A change bumps the version like a change of markers.
            item_files: The item's version files the write computed the set for; None keeps the recorded ones.
                Recording them never bumps the version.

        Returns:
            The row's version.
        """
        now = self._now()
        with self._tx() as conn:
            if item_files is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO item_versions (server_id, item_id, files_json) VALUES (?,?,?)",
                    (server_id, item_id, json.dumps(sorted(item_files))),
                )
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

    def published_items(self, server_id: str) -> list[ItemPublishStateRow]:
        """Server items where this app's last write succeeded and left markers of ours, kept the server's own, or
        left a type to the server's own marker without deciding it (``ItemPublishStateRow.own_types``).

        Args:
            server_id: The server.

        Returns:
            The items' rows, by item id (text order).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM item_publish_state WHERE server_id=? AND status='written' ORDER BY item_id", (server_id,)
            ).fetchall()
            kept: dict[str, set[MarkerType]] = {}
            for r in self._conn.execute("SELECT item_id, type FROM item_kept_types WHERE server_id=?", (server_id,)):
                kept.setdefault(r["item_id"], set()).add(MarkerType(r["type"]))
            own: dict[str, set[MarkerType]] = {}
            kept_own_rows = [
                r
                for r in self._conn.execute(
                    "SELECT p.item_id, d.type, d.reason, f.canonical_path, f.size, f.mtime_ns FROM publish_state p "
                    "JOIN decisions d ON d.file_id = p.file_id JOIN files f ON f.id = p.file_id "
                    "WHERE p.server_id=? AND p.item_id IS NOT NULL AND d.status=?",
                    (server_id, DecisionStatus.DISABLED.value),
                )
                if is_kept_own(DecisionStatus.DISABLED, r["reason"])
            ]
        for r in kept_own_rows:
            # A file gone or replaced since its run says nothing about the item now (a deleted version's kept status
            # would list the item as missing on every run).
            if _same_identity_on_disk(r["canonical_path"], r["size"], r["mtime_ns"]):
                own.setdefault(r["item_id"], set()).add(MarkerType(r["type"]))
        with self._lock:
            files = {
                r["item_id"]: tuple(json.loads(r["files_json"]))
                for r in self._conn.execute(
                    "SELECT item_id, files_json FROM item_versions WHERE server_id=?", (server_id,)
                )
            }
        out = []
        for r in rows:
            markers = self._markers_from_json(r["markers_json"])
            kept_types = frozenset(kept.get(r["item_id"], ()))
            own_types = frozenset(own.get(r["item_id"], ())) - {m.type for m in markers} - kept_types
            if markers or kept_types or own_types:
                out.append(
                    ItemPublishStateRow(
                        r["server_id"],
                        r["item_id"],
                        markers,
                        r["status"],
                        r["version"],
                        r["updated_at"],
                        kept_types,
                        files.get(r["item_id"]),
                        own_types,
                    )
                )
        return out

    def failed_items_due(
        self, server_ids: Iterable[str], *, now: datetime, after: Sequence[timedelta]
    ) -> list[tuple[str, str]]:
        """Server items whose last publish failed and whose next Check servers retry is due.

        The n-th retry is due once ``after[n]`` has passed since the failure or the retry before it; none after
        ``len(after)`` retries. A publish that succeeds, or fails again after one that did, starts the count over (the
        item row's version changes; a failure repeated on a retry doesn't change it).

        Args:
            server_ids: The servers.
            now: The current time.
            after: The backoff steps.

        Returns:
            ``(server_id, item_id)`` pairs, items never retried first, then the longest waiting.
        """
        ids = sorted(set(server_ids))
        if not ids or not after:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                "SELECT i.server_id, i.item_id, i.updated_at, r.retries, r.retried_at FROM item_publish_state i "
                "LEFT JOIN failed_item_retries r ON r.server_id = i.server_id AND r.item_id = i.item_id "
                "AND r.item_version = i.version "
                f"WHERE i.status = 'failed' AND i.server_id IN ({marks})",  # noqa: S608 - placeholders only
                ids,
            ).fetchall()
        due = []
        for r in rows:
            retries = int(r["retries"] or 0)
            since = max(r["updated_at"], r["retried_at"] or "")
            if retries < len(after) and datetime.fromisoformat(since) < now - after[retries]:
                due.append((retries > 0, since, r["server_id"], r["item_id"]))
        return [(server_id, item_id) for _, _, server_id, item_id in sorted(due)]

    def record_failed_item_retries(self, pairs: Iterable[tuple[str, str]], *, listed_at: str | None = None) -> None:
        """Count one Check servers retry now for each of these ``(server_id, item_id)`` failed items.

        Args:
            pairs: The items.
            listed_at: When the Check servers run whose file ran listed them (ISO time): an item already counted since
                then isn't counted again (another of its files ran first, or a run revived after a restart ran the file
                again). None counts every item.
        """
        now = self._now()
        with self._tx() as conn:
            for server_id, item_id in pairs:
                conn.execute(
                    "INSERT INTO failed_item_retries (server_id, item_id, item_version, retries, retried_at) "
                    "SELECT server_id, item_id, version, 1, ? FROM item_publish_state WHERE server_id=? AND item_id=? "
                    "ON CONFLICT(server_id, item_id) DO UPDATE SET retries = CASE WHEN "
                    "failed_item_retries.item_version = excluded.item_version THEN failed_item_retries.retries + 1 "
                    "ELSE 1 END, item_version = excluded.item_version, retried_at = excluded.retried_at "
                    "WHERE ? IS NULL OR failed_item_retries.retried_at < ?",
                    (now, server_id, item_id, listed_at, listed_at),
                )

    def files_for_item(self, server_id: str, item_id: str) -> list[str]:
        """Local files whose last publish to this server went to this item.

        Args:
            server_id: The server.
            item_id: The server's item id.

        Returns:
            Their canonical paths, sorted.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.canonical_path FROM publish_state p JOIN files f ON f.id = p.file_id "
                "WHERE p.server_id=? AND p.item_id=? ORDER BY f.canonical_path",
                (server_id, item_id),
            ).fetchall()
        return [r["canonical_path"] for r in rows]

    def files_with_undelivered_locks(self, server_ids: Iterable[str]) -> list[tuple[str, str]]:
        """Files with a marker the user locked whose last publish to one of these servers didn't land.

        ``failed`` (the write or the publish deadline failed) and ``skipped`` (the server couldn't take markers then:
        down, plugin missing) both mean the user's edit never reached the server. ``waiting`` is left out: it is a Plex
        item whose other versions haven't agreed yet, and publishing this file again can't make them agree.

        Args:
            server_ids: The servers to look at.

        Returns:
            ``(canonical_path, server_id)`` pairs, sorted by path then server.
        """
        ids = sorted(set(server_ids))
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT f.canonical_path, p.server_id FROM markers m "
                "JOIN files f ON f.id = m.file_id JOIN publish_state p ON p.file_id = m.file_id "
                f"WHERE m.locked=1 AND p.status IN ('failed','skipped') AND p.server_id IN ({marks}) "  # noqa: S608
                "ORDER BY f.canonical_path, p.server_id",
                ids,
            ).fetchall()
        return [(r["canonical_path"], r["server_id"]) for r in rows]

    def server_rechecks_due(
        self, server_ids: Iterable[str], *, now: datetime, after: Sequence[timedelta], limit: int
    ) -> list[tuple[str, str]]:
        """Decided files due to have a server asked again for its own markers. Nothing is written: a Check servers run
        marks what it took once the file ran (``mark_server_rechecks_taken``), so a run that ends first leaves it due.

        A file qualifies for a server when its credits or preview are decided (the types a server's own markers can
        shorten, spec §5.5 rule 7), nothing of ours is on the server item it last published to, and that server's
        stored answer for it is empty (or unusable) and old enough: at least ``after[n]`` old after ``n`` re-reads
        without markers (counted from the last re-read that failed, when that is later), never once it was read again
        ``len(after)`` times. A file taken for a server in the last day
        isn't taken again for it. Pairs never taken come first, then the least recently taken, then the oldest answers.

        Args:
            server_ids: Servers whose answers count.
            now: The current time.
            after: How old the answer must be before each re-read (the backoff steps).
            limit: Most (file, server) pairs.

        Returns:
            ``(canonical_path, server_id)`` pairs in that order.
        """
        ids = sorted(set(server_ids))
        if not ids or limit <= 0 or not after:
            return []
        steps = " ".join("WHEN ? THEN ?" for _ in after)
        marks = ",".join("?" * len(ids))
        with self._lock:
            rows = self._conn.execute(
                "WITH answers AS ("
                "  SELECT file_id, origin AS server_id, MAX(fetched_at) AS fetched_at FROM evidence"
                f"  WHERE source IN (?, ?) AND origin IN ({marks})"  # noqa: S608 - placeholders only
                "  GROUP BY origin, file_id HAVING COUNT(type) = 0"
                ") "
                "SELECT a.file_id, a.server_id, f.canonical_path FROM answers a "
                "JOIN files f ON f.id = a.file_id "
                "LEFT JOIN server_marker_rereads r ON r.file_id = a.file_id AND r.server_id = a.server_id "
                "WHERE MAX(a.fetched_at, COALESCE(r.failed_reread_at, '')) "
                f"< (CASE COALESCE(r.rereads, 0) {steps} END) "  # noqa: S608 - placeholders only
                "AND (r.taken_at IS NULL OR r.taken_at < ?) "
                "AND EXISTS (SELECT 1 FROM decisions d WHERE d.file_id = a.file_id AND d.status = ? "
                "            AND d.type IN (?, ?)) "
                "AND NOT EXISTS (SELECT 1 FROM publish_state p WHERE p.file_id = a.file_id "
                "                AND p.server_id = a.server_id AND p.markers_json != '[]') "
                "AND NOT EXISTS (SELECT 1 FROM publish_state p JOIN item_publish_state i "
                "                ON i.server_id = p.server_id AND i.item_id = p.item_id "
                "                WHERE p.file_id = a.file_id AND p.server_id = a.server_id "
                "                AND (i.markers_json != '[]' OR EXISTS (SELECT 1 FROM item_kept_types k "
                "                     WHERE k.server_id = i.server_id AND k.item_id = i.item_id))) "
                "ORDER BY r.taken_at IS NOT NULL, r.taken_at, a.fetched_at, f.canonical_path LIMIT ?",
                (
                    *_SERVER_SOURCE_VALUES,
                    *ids,
                    *(value for n, step in enumerate(after) for value in (n, (now - step).isoformat())),
                    (now - _RECHECK_TAKEN_AGAIN).isoformat(),
                    DecisionStatus.DECIDED.value,
                    MarkerType.CREDITS.value,
                    MarkerType.PREVIEW.value,
                    int(limit),
                ),
            ).fetchall()
        return [(r["canonical_path"], r["server_id"]) for r in rows]

    def mark_server_rechecks_taken(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Remember that Check servers took these ``(canonical_path, server_id)`` pairs now (``server_rechecks_due``).

        After their file ran, or at once for a file no run can check (gone from disk, not in that server's library), so
        it takes its turn behind the rest. A pair whose server's answer has markers by now (the run's re-read found
        some) keeps nothing, like any answer with markers.
        """
        marks = ",".join("?" * len(SERVER_SOURCES))
        now = self._now()
        with self._tx() as conn:
            conn.executemany(
                "INSERT INTO server_marker_rereads (file_id, server_id, taken_at) "
                "SELECT f.id, ?, ? FROM files f WHERE f.canonical_path = ? AND ("
                "  SELECT COUNT(*) > 0 AND COUNT(e.type) = 0 FROM evidence e"
                f"  WHERE e.file_id = f.id AND e.origin = ? AND e.source IN ({marks})"  # noqa: S608 - placeholders only
                ") ON CONFLICT(file_id, server_id) DO UPDATE SET taken_at = excluded.taken_at",
                [(server_id, now, path, server_id, *_SERVER_SOURCE_VALUES) for path, server_id in pairs],
            )

    def server_recheck_due(self, file_id: int, server_id: str, *, now: datetime, after: Sequence[timedelta]) -> bool:
        """Whether a server's stored answer for a file is empty and due to be read again (``server_rechecks_due``).

        Args:
            file_id: The file.
            server_id: The server.
            now: The current time.
            after: The backoff steps.

        Returns:
            True when every stored row is empty and the answer (or the last failed re-read) is older than the step its
            re-reads so far reached.
        """
        marks = ",".join("?" * len(SERVER_SOURCES))
        with self._lock:
            stored, typed, fetched = self._conn.execute(
                "SELECT COUNT(*), COUNT(type), MAX(fetched_at) FROM evidence "
                f"WHERE file_id=? AND origin=? AND source IN ({marks})",  # noqa: S608 - placeholders only
                (file_id, server_id, *_SERVER_SOURCE_VALUES),
            ).fetchone()
            row = self._conn.execute(
                "SELECT rereads, failed_reread_at FROM server_marker_rereads WHERE file_id=? AND server_id=?",
                (file_id, server_id),
            ).fetchone()
        rereads = int(row["rereads"]) if row else 0
        if not stored or typed or rereads >= len(after):
            return False
        answered = max(fetched, (row["failed_reread_at"] if row else None) or "")
        return datetime.fromisoformat(answered) < now - after[rereads]

    def mark_item_gone(self, server_id: str, item_id: str) -> None:
        """Take a server item that no longer exists there out of ``published_items`` until this app writes it again.

        Its row keeps what was ours on it; the status becomes ``gone`` (a new version, so the next publish of any file
        to that item writes instead of trusting its basis). Only a ``written`` row changes.
        """
        with self._tx() as conn:
            conn.execute(
                "UPDATE item_publish_state SET status='gone', version=version+1, updated_at=? "
                "WHERE server_id=? AND item_id=? AND status='written'",
                (self._now(), server_id, item_id),
            )

    def count_failed_server_reread(self, file_id: int, server_id: str) -> None:
        """Count a Check servers re-read of a stored answer that failed (an unreadable read, another cut on the item).

        The stored answer stays; the re-read moves the backoff on like one that stayed empty (``server_rechecks_due``).

        Args:
            file_id: The file.
            server_id: The server whose read failed.
        """
        now = self._now()
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO server_marker_rereads (file_id, server_id, rereads, failed_reread_at) VALUES (?,?,1,?) "
                "ON CONFLICT(file_id, server_id) DO UPDATE SET rereads = rereads + 1, failed_reread_at = excluded.failed_reread_at",
                (file_id, server_id, now),
            )

    def drift_listed_at(self, server_id: str, item_ids: Iterable[str]) -> dict[str, str]:
        """When Check servers last listed each of these drifted items' files (items never listed are left out)."""
        wanted = set(item_ids)
        with self._lock:
            rows = self._conn.execute(
                "SELECT item_id, listed_at FROM drift_listings WHERE server_id=?", (server_id,)
            ).fetchall()
        return {r["item_id"]: r["listed_at"] for r in rows if r["item_id"] in wanted}

    def record_drift_listed(self, pairs: Iterable[tuple[str, str]]) -> None:
        """Remember that Check servers listed the files of these ``(server_id, item_id)`` drifted items now."""
        now = self._now()
        with self._tx() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO drift_listings (server_id, item_id, listed_at) VALUES (?,?,?)",
                [(server_id, item_id, now) for server_id, item_id in pairs],
            )

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

    @staticmethod
    def _series_has_answers(conn: sqlite3.Connection, source: Source, show_folder: str) -> bool:
        """Whether any file under the show's folder has a stored answer from ``source`` with a marker in it (answers
        stored before ``series_lookups`` existed, or by a sibling folder's lookups, count too)."""
        folder = show_folder.rstrip("/")
        if not folder:
            return False
        # Every path under "<folder>/" sorts between "<folder>/" and "<folder>0" ("0" follows "/"): a range the
        # canonical_path index answers, unlike a LIKE prefix.
        row = conn.execute(
            "SELECT 1 FROM files f JOIN evidence e ON e.file_id = f.id "
            "WHERE f.canonical_path >= ? AND f.canonical_path < ? AND e.source = ? AND e.origin = '' "
            "AND e.type IS NOT NULL LIMIT 1",
            (f"{folder}/", f"{folder}0", source.value),
        ).fetchone()
        return row is not None

    def record_series_lookup(
        self,
        source: Source,
        series_key: str,
        episode: str,
        *,
        found: bool,
        misses: int,
        pause: timedelta,
        show_folder: str = "",
    ) -> datetime | None:
        """Remember an online source's answer for one episode of a series, and pause the series when it has none.

        The episode's older answer is replaced. An answer with something in it ends any pause. A "no entry" starts a
        pause once ``misses`` episodes answered "no entry" since the last pause ended (or ever, before the first),
        while no episode of the series has an answer: none recorded here, and none stored for any file under
        ``show_folder``. A "no entry" during a pause (a forced run's) doesn't extend it.

        Args:
            source: The online source.
            series_key: The series id the lookup was sent with, e.g. ``tmdb:12345``.
            episode: The episode, e.g. ``S01E02``.
            found: Whether the source had anything for it.
            misses: Episodes with "no entry" that start a pause.
            pause: How long a pause lasts.
            show_folder: The show's folder ("" skips the check of its files' stored answers).

        Returns:
            The end of the pause this answer started, else None.
        """
        now = self._clock()
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO series_lookups (source, series_key, episode, found, answered_at) "
                "VALUES (?,?,?,?,?)",
                (source.value, series_key, episode, int(found), now.isoformat()),
            )
            if found:
                conn.execute("DELETE FROM series_pauses WHERE source=? AND series_key=?", (source.value, series_key))
                return None
            paused = conn.execute(
                "SELECT paused_at FROM series_pauses WHERE source=? AND series_key=?", (source.value, series_key)
            ).fetchone()
            counted_from = ""
            if paused is not None:
                last_end = datetime.fromisoformat(paused["paused_at"]) + pause
                if last_end > now:
                    return None
                counted_from = last_end.isoformat()
            r = conn.execute(
                "SELECT MAX(found) AS any_found, SUM(found = 0 AND answered_at >= ?) AS missed "
                "FROM series_lookups WHERE source=? AND series_key=?",
                (counted_from, source.value, series_key),
            ).fetchone()
            if r["any_found"] or (r["missed"] or 0) < misses or self._series_has_answers(conn, source, show_folder):
                return None
            conn.execute(
                "INSERT OR REPLACE INTO series_pauses (source, series_key, paused_at) VALUES (?,?,?)",
                (source.value, series_key, now.isoformat()),
            )
        return now + pause

    def series_lookups_paused_until(
        self, source: Source, series_key: str, *, pause: timedelta, show_folder: str = ""
    ) -> datetime | None:
        """Until when a source isn't asked about a series it has no entries for (``record_series_lookup``).

        A pause lasts ``pause`` from when it started, and ends early once any file under ``show_folder`` has a stored
        answer from the source with a marker in it.

        Args:
            source: The online source.
            series_key: The series id the lookups were sent with.
            pause: How long a pause lasts.
            show_folder: The show's folder ("" skips the check of its files' stored answers).

        Returns:
            The end of the pause, or None when the series may be asked now.
        """
        with self._lock:
            paused = self._conn.execute(
                "SELECT paused_at FROM series_pauses WHERE source=? AND series_key=?", (source.value, series_key)
            ).fetchone()
            if paused is None:
                return None
            until = datetime.fromisoformat(paused["paused_at"]) + pause
            if until <= self._clock() or self._series_has_answers(self._conn, source, show_folder):
                return None
        return until

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
