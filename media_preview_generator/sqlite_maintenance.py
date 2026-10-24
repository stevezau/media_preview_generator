"""Keep SQLite files from growing without bound: incremental auto-vacuum and WAL truncation."""

from __future__ import annotations

import sqlite3

from loguru import logger


def ensure_incremental_autovacuum(conn: sqlite3.Connection, label: str) -> None:
    """Enable SQLite incremental auto-vacuum, compacting an existing DB on first upgrade.

    Without auto_vacuum the .db file grows monotonically: deleted rows leave freelist pages that are reused for
    future inserts but never returned to the OS. ``auto_vacuum=INCREMENTAL`` is sticky per-database and only takes
    effect on an *existing* DB after a full ``VACUUM``. So a DB still at the default ``NONE`` is flipped to
    INCREMENTAL and vacuumed once; afterwards :func:`reclaim_space` returns freelist pages cheaply.

    Args:
        conn: Open connection with no transaction in progress (VACUUM cannot run inside one).
        label: Human name of the database for log lines (``"Jobs DB"``).
    """
    try:
        current = conn.execute("PRAGMA auto_vacuum").fetchone()
        mode = int(current[0]) if current else 0
    except Exception as exc:
        logger.debug("Could not read auto_vacuum PRAGMA of {}: {}", label, exc)
        return
    if mode == 2:
        return
    try:
        # Checkpoint first so pending WAL pages land in the main file: VACUUM rewrites it, and a large WAL backlog
        # would otherwise stay stranded until the next WAL rotation.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("{}: auto_vacuum set to INCREMENTAL and compacted", label)
    except Exception as exc:
        logger.warning(
            "Could not enable incremental auto_vacuum on {} ({}); "
            "the file may grow monotonically until a manual VACUUM. "
            "This is non-fatal: persistence still works.",
            label,
            exc,
        )


def reclaim_space(conn: sqlite3.Connection, label: str) -> None:
    """Return freelist pages to the OS and truncate the WAL after a bulk delete; best effort.

    Args:
        conn: Open connection with no transaction in progress.
        label: Human name of the database for log lines.
    """
    try:
        # The pragma frees one page per row it yields, so it only finishes once every row is fetched.
        conn.execute("PRAGMA incremental_vacuum").fetchall()
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    except Exception as exc:
        logger.debug("Space reclaim on {} failed: {}", label, exc)
        return
    if busy:
        # A reader on another connection held the WAL past the busy timeout. Automatic checkpoints never truncate,
        # so the WAL stays at its size until the next call here.
        logger.debug("Space reclaim on {}: WAL not truncated, a reader still holds it", label)
