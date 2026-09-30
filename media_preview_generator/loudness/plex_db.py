"""Plex's audio streams of a file, and the two writes loudness makes, as Plex makes them.

Each analysed stream gets ``ln:*`` fields in ``media_streams.extra_data``; an item whose streams all have them gets
``ln:loudnessAnalysisVersion`` in ``metadata_items.extra_data``, the mark Plex checks before analysing an item (without
it Plex analyses the item again). It reuses the Intro & Credits Plex database plumbing (``markers.publishers.plex_db``):
this process's lock on the file, ``BEGIN IMMEDIATE`` with busy waits, and Plex's own extra_data encoding. Its schema
check is its own, so a loudness table change never stops Intro & Credits.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from typing import NamedTuple

from loguru import logger

from ..markers.publishers.base import Capability, PublishError
from ..markers.publishers.plex_db import (
    LocalPlexDb,
    _same_extra_data,
    decode_extra_data,
    encode_extra_data,
    publish_error_from_sqlite,
)
from .analyze import ANALYSIS_VERSION

AUDIO = 2
# The mark on the item, and on each stream beside its measurements.
VERSION_FIELD = "ln:loudnessAnalysisVersion"
# The columns read or written, as in Plex Media Server 1.43.4.
_REQUIRED_COLUMNS = {
    "media_streams": {"id", "media_part_id", "stream_type_id", "index", "codec", "extra_data", "updated_at"},
    "media_parts": {"id", "media_item_id", "file", "deleted_at"},
    "media_items": {"id", "deleted_at", "duration", "metadata_item_id", "proxy_type"},
    "metadata_items": {"id", "extra_data"},
}
# A trigger that could fire on the item write: any UPDATE trigger except ``UPDATE OF <columns without extra_data>``
# (Plex 1.43's title search triggers are ``UPDATE OF title, title_sort, original_title``).
_UPDATE_OF = re.compile(r"\bUPDATE\b(?:\s+OF\s+(.+?))?\s+ON\b", re.IGNORECASE | re.DOTALL)
_log_lock = threading.Lock()


def check_schema(conn: sqlite3.Connection) -> None:
    """Refuse a database whose tables differ from the tested version, or that has a trigger on ``media_streams``.

    Raises:
        PublishError: UNSUPPORTED_SCHEMA.
    """
    for table, required in _REQUIRED_COLUMNS.items():
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608 - fixed table names
        if missing := required - cols:
            raise PublishError(
                f"Plex database looks different from the tested version (table {table} lacks {sorted(missing)}); "
                "not writing loudness.",
                state=Capability.UNSUPPORTED_SCHEMA,
            )
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='media_streams'").fetchone():
        raise PublishError(
            "Plex database has a trigger on media_streams, unlike the tested version; not writing loudness.",
            state=Capability.UNSUPPORTED_SCHEMA,
        )
    for (sql,) in conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='metadata_items'"):
        update = _UPDATE_OF.search(sql or "")
        if update and (update.group(1) is None or "extra_data" in update.group(1)):
            raise PublishError(
                "Plex database has a trigger on metadata_items' extra_data, unlike the tested version; not writing "
                "loudness.",
                state=Capability.UNSUPPORTED_SCHEMA,
            )


class AudioStream(NamedTuple):
    """One audio stream of a live Plex part."""

    id: int
    index: int
    codec: str
    extra_data: str | None
    duration_ms: int | None
    file: str
    metadata_item_id: int
    item_marked: bool


def write_log_path() -> str:
    """Where each write's before/after is kept (the undo record): ``<CONFIG_DIR>/loudness-writes.jsonl``."""
    return os.path.join(os.environ.get("CONFIG_DIR", "/config"), "loudness-writes.jsonl")


def needs_analysis(stream: AudioStream) -> bool:
    """Whether Plex has no loudness for a stream yet."""
    return "ln:loudness" not in decode_extra_data(stream.extra_data)[0]


def _optimized_copy(file: str, proxy_type: object) -> bool:
    # As the markers publisher tells them (``_is_optimized_copy``): a proxy_type and a "Plex Versions" folder.
    return bool(proxy_type) and "Plex Versions" in str(file).replace("\\", "/").split("/")


def _marked(item_extra_data: str | None) -> bool:
    return VERSION_FIELD in decode_extra_data(item_extra_data)[0]


def read_streams(db: LocalPlexDb, plex_paths: list[str], *, deadline: float) -> tuple[list[AudioStream], bool]:
    """The audio streams of the live parts at these paths (as Plex names them).

    Returns:
        The streams, and whether Plex has a live part there at all (a file with no audio track has one but no streams).

    Raises:
        PublishError: The database couldn't be read or isn't the tested schema.
    """
    if not plex_paths:
        return [], False
    marks = ",".join("?" * len(plex_paths))
    try:
        with db._database(read_only=True, deadline=deadline) as conn:
            check_schema(conn)
            rows = conn.execute(
                'SELECT ms.id, ms."index", ms.codec, ms.extra_data, mi.duration, mp.file, md.id, md.extra_data '
                "FROM media_parts mp "
                "JOIN media_items mi ON mi.id = mp.media_item_id "
                "JOIN metadata_items md ON md.id = mi.metadata_item_id "
                'LEFT JOIN media_streams ms ON ms.media_part_id = mp.id AND ms.stream_type_id = ? AND ms."index" IS NOT NULL '
                f"WHERE mp.file IN ({marks}) AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL "  # noqa: S608
                'ORDER BY mp.id, ms."index"',
                [AUDIO, *plex_paths],
            ).fetchall()
    except sqlite3.Error as exc:
        raise publish_error_from_sqlite(exc) from exc
    streams = [
        AudioStream(r[0], int(r[1]), str(r[2] or ""), r[3], r[4], r[5], r[6], _marked(r[7]))
        for r in rows
        if r[0] is not None
    ]
    return streams, bool(rows)


def write_stream(db: LocalPlexDb, stream_id: int, fields: dict[str, str], *, deadline: float) -> bool:
    """Add ``fields`` to one stream's extra_data, keeping its other fields, unless it already has loudness.

    Args:
        db: The server's Plex database.
        stream_id: ``media_streams.id``.
        fields: The ``ln:*`` fields (``analyze.ln_fields``).
        deadline: When to stop waiting for the database locks.

    Returns:
        True when written; False when the stream already had loudness (Plex, or another job, got there first).

    Raises:
        PublishError: Nothing was written (busy, cancelled, schema, the stream is gone, or the read-back differed).
    """
    try:
        with db._database(read_only=False, deadline=deadline) as conn:
            check_schema(conn)
            db._begin_write(conn, deadline)
            try:
                row = conn.execute("SELECT extra_data FROM media_streams WHERE id = ?", (stream_id,)).fetchone()
                if row is None:
                    raise PublishError(f"Plex's audio stream {stream_id} is gone")
                before = row[0]
                existing, url_form = decode_extra_data(before)
                if "ln:loudness" in existing:
                    conn.execute("ROLLBACK")
                    return False
                # In the form the row was in, as the markers writer keeps a part's (``merge_part_extra_data``).
                after = encode_extra_data({**existing, **fields}, url_form=url_form)
                conn.execute(
                    "UPDATE media_streams SET extra_data = ?, updated_at = ? WHERE id = ?",
                    (after, int(time.time()), stream_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            # Committed: record it for undo before anything else can fail.
            _log_write({"stream_id": stream_id}, before, after)
            stored = conn.execute("SELECT extra_data FROM media_streams WHERE id = ?", (stream_id,)).fetchone()
    except sqlite3.Error as exc:
        raise publish_error_from_sqlite(exc) from exc
    if stored is None or not _same_extra_data(stored[0], after):
        raise PublishError(f"Plex's audio stream {stream_id} doesn't show the loudness just written")
    return True


def mark_item(db: LocalPlexDb, metadata_item_id: int, *, deadline: float) -> bool:
    """Mark an item analysed (``ln:loudnessAnalysisVersion`` in its extra_data) once all its audio streams are.

    All its live parts count, so an item with several versions is marked when the last one is done. Only
    ``extra_data`` is set: Plex's triggers on the table fire on title changes only (``check_schema``).

    Returns:
        True when marked; False when it was already marked or a stream still lacks loudness.

    Raises:
        PublishError: Nothing was written (busy, cancelled, schema, the item is gone, or the read-back differed).
    """
    try:
        with db._database(read_only=False, deadline=deadline) as conn:
            check_schema(conn)
            db._begin_write(conn, deadline)
            try:
                row = conn.execute("SELECT extra_data FROM metadata_items WHERE id = ?", (metadata_item_id,)).fetchone()
                if row is None:
                    raise PublishError(f"Plex's item {metadata_item_id} is gone")
                before = row[0]
                existing, url_form = decode_extra_data(before)
                # The streams read_streams would give: Plex's optimized copies are left out, as the app never analyses them.
                pending = [
                    extra
                    for extra, file, proxy_type in conn.execute(
                        "SELECT ms.extra_data, mp.file, mi.proxy_type FROM media_streams ms "
                        "JOIN media_parts mp ON mp.id = ms.media_part_id "
                        "JOIN media_items mi ON mi.id = mp.media_item_id "
                        'WHERE mi.metadata_item_id = ? AND ms.stream_type_id = ? AND ms."index" IS NOT NULL '
                        "AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL",
                        (metadata_item_id, AUDIO),
                    )
                    if not _optimized_copy(file, proxy_type) and "ln:loudness" not in decode_extra_data(extra)[0]
                ]
                if VERSION_FIELD in existing or pending:
                    conn.execute("ROLLBACK")
                    return False
                after = encode_extra_data({**existing, VERSION_FIELD: ANALYSIS_VERSION}, url_form=url_form)
                conn.execute("UPDATE metadata_items SET extra_data = ? WHERE id = ?", (after, metadata_item_id))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            _log_write({"metadata_item_id": metadata_item_id}, before, after)
            stored = conn.execute("SELECT extra_data FROM metadata_items WHERE id = ?", (metadata_item_id,)).fetchone()
    except sqlite3.Error as exc:
        raise publish_error_from_sqlite(exc) from exc
    if stored is None or not _same_extra_data(stored[0], after):
        raise PublishError(f"Plex's item {metadata_item_id} doesn't show the loudness mark just written")
    return True


def _log_write(row: dict[str, int], before: str | None, after: str) -> None:
    """Record a committed write for undo: the row (``stream_id`` or ``metadata_item_id``), before and after."""
    line = json.dumps({**row, "before": before, "after": after, "at": int(time.time())})
    try:
        with _log_lock, open(write_log_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        # The write itself stands; only its undo record is missing.
        logger.warning("Couldn't record loudness write of {} in {}: {}", row, write_log_path(), exc)
