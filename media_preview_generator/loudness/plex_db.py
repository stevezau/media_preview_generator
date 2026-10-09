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
import uuid
from collections.abc import Callable
from typing import NamedTuple, cast

from ..markers.publishers.base import Capability, PublishError
from ..markers.publishers.plex_db import (
    LocalPlexDb,
    _same_extra_data,
    decode_extra_data,
    encode_extra_data,
    publish_error_from_sqlite,
)
from ..output.plex_hash import get_source_fingerprint
from .analyze import ANALYSIS_VERSION, LoudnessError, ln_fields

AUDIO = 2
# The mark on the item, and on each stream beside its measurements.
VERSION_FIELD = "ln:loudnessAnalysisVersion"
# The columns read or written, as in Plex Media Server 1.43.4.
_REQUIRED_COLUMNS = {
    "media_streams": {
        "id",
        "media_part_id",
        "stream_type_id",
        "index",
        "codec",
        "extra_data",
        "updated_at",
        "created_at",
        "channels",
    },
    "media_parts": {"id", "media_item_id", "file", "deleted_at", "hash", "size", "updated_at"},
    "media_items": {"id", "deleted_at", "duration", "metadata_item_id", "proxy_type"},
    "metadata_items": {"id", "extra_data", "guid", "metadata_type", "created_at"},
}
# A trigger that could fire on the item write: any UPDATE trigger except ``UPDATE OF <columns without extra_data>``
# (Plex 1.43's title search triggers are ``UPDATE OF title, title_sort, original_title``).
_UPDATE_OF = re.compile(r"\bUPDATE\b(?:\s+OF\s+(.+?))?\s+ON\b", re.IGNORECASE | re.DOTALL)
_log_lock = threading.Lock()
MEASUREMENT_FIELDS = frozenset({"ln:loudness", "ln:peak", "ln:lra", "ln:threshold", "ln:gainOffset"})


class SourceChangedError(PublishError):
    """The analyzed source no longer matches Plex; retry from a fresh snapshot."""


class SourceFingerprint(NamedTuple):
    """Local source identity, including replacement and in-place modification."""

    path: str
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def read(cls, path: str) -> SourceFingerprint:
        """Capture the file before analysis; missing/inaccessible files remain retryable."""
        try:
            fingerprint = get_source_fingerprint(path)
        except OSError as exc:
            raise SourceChangedError(
                "The audio source disappeared or became inaccessible; retry after Plex scans it"
            ) from exc
        return cls(path, *fingerprint)

    def verify(self) -> None:
        """Refuse results computed from bytes that have changed."""
        if self.read(self.path) != self:
            raise SourceChangedError("The audio source changed during analysis; retry after Plex scans it")


def database_identity(path: str) -> dict:
    """Bind undo to this exact database file; copied/replaced databases fail closed."""
    stat = os.stat(path)
    return {"path": os.path.realpath(path), "device": stat.st_dev, "inode": stat.st_ino}


def has_analysis(extra_data: str | None) -> bool:
    """Recognize a complete tested result without overwriting ambiguous native data."""
    fields = decode_extra_data(extra_data)[0]
    present = {key for key in fields if key.startswith("ln:")}
    if not present:
        return False
    try:
        ln_fields(
            {
                "input_i": fields["ln:loudness"],
                "input_tp": fields["ln:peak"],
                "input_lra": fields["ln:lra"],
                "input_thresh": fields["ln:threshold"],
                "target_offset": fields["ln:gainOffset"],
            }
        )
        complete = fields.get(VERSION_FIELD) == ANALYSIS_VERSION
    except (KeyError, TypeError, ValueError, LoudnessError):
        complete = False
    if not complete:
        raise PublishError(
            "Plex has incomplete or unsupported loudness metadata; leaving it unchanged. Run Plex's native analysis."
        )
    return True


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
        if update and (update.group(1) is None or "extra_data" in update.group(1).lower()):
            raise PublishError(
                "Plex database has a trigger on metadata_items' extra_data, unlike the tested version; not writing "
                "loudness.",
                state=Capability.UNSUPPORTED_SCHEMA,
            )


class AudioStream(NamedTuple):
    """One audio stream of a live Plex part."""

    id: int
    index: int  # type: ignore[assignment]  # field name shadows tuple.index; renaming would change the API
    codec: str
    extra_data: str | None
    duration_ms: int | None
    file: str
    metadata_item_id: int
    item_marked: bool
    part_id: int
    media_item_id: int
    part_hash: str | None
    part_size: int | None
    part_updated_at: int | None
    created_at: int | None
    channels: int | None

    def identity(self) -> tuple:
        """Fields tying an index to the same indexed source and audio stream."""
        return (
            self.id,
            self.index,
            self.codec,
            self.duration_ms,
            self.file,
            self.metadata_item_id,
            self.part_id,
            self.media_item_id,
            self.part_hash,
            self.part_size,
            self.part_updated_at,
            self.created_at,
            self.channels,
        )


def write_log_path() -> str:
    """Where each write's before/after is kept (the undo record): ``<CONFIG_DIR>/loudness-writes.jsonl``."""
    return os.path.join(os.environ.get("CONFIG_DIR", "/config"), "loudness-writes.jsonl")


def needs_analysis(stream: AudioStream) -> bool:
    """Whether Plex has no loudness for a stream yet."""
    return not has_analysis(stream.extra_data)


def _optimized_copy(file: str, proxy_type: object) -> bool:
    # As the markers publisher tells them (``_is_optimized_copy``): a proxy_type and a "Plex Versions" folder.
    return bool(proxy_type) and "Plex Versions" in str(file).replace("\\", "/").split("/")


def _marked(item_extra_data: str | None) -> bool:
    fields = decode_extra_data(item_extra_data)[0]
    if VERSION_FIELD in fields and fields[VERSION_FIELD] != ANALYSIS_VERSION:
        raise PublishError("Plex's item has an unsupported loudness analysis version; leaving it unchanged")
    return fields.get(VERSION_FIELD) == ANALYSIS_VERSION


_STREAM_SELECT = (
    'SELECT ms.id, ms."index", ms.codec, ms.extra_data, mi.duration, mp.file, md.id, md.extra_data, '
    "mp.id, mi.id, mp.hash, mp.size, mp.updated_at, ms.created_at, ms.channels, md.metadata_type "
    "FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id "
    "JOIN metadata_items md ON md.id = mi.metadata_item_id "
    'LEFT JOIN media_streams ms ON ms.media_part_id = mp.id AND ms.stream_type_id = 2 AND ms."index" IS NOT NULL '
)


def _audio_stream(row: tuple) -> AudioStream:
    if row[-1] not in (1, 4):
        raise PublishError("Loudness analysis supports Plex movies and TV episodes; music analysis is not supported")
    return AudioStream(
        row[0], int(row[1]), str(row[2] or ""), row[3], row[4], row[5], row[6], _marked(row[7]), *row[8:-1]
    )


def item_snapshot(conn: sqlite3.Connection, metadata_item_id: int) -> tuple:
    """All live original indexed audio sources, excluding mutable analysis values."""
    rows = conn.execute(
        _STREAM_SELECT + "WHERE md.id = ? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL "
        "AND NOT (COALESCE(mi.proxy_type, 0) != 0 AND REPLACE(mp.file, char(92), '/') LIKE '%/Plex Versions/%') "
        "ORDER BY mp.id, ms.id",
        (metadata_item_id,),
    ).fetchall()
    return tuple(_audio_stream(row).identity() for row in rows if row[0] is not None)


def read_item_snapshot(db: LocalPlexDb, metadata_item_id: int, *, deadline: float) -> tuple:
    """Capture all versions before a worker starts changing their measurements."""
    try:
        with db._database(read_only=True, deadline=deadline) as conn:
            check_schema(conn)
            return item_snapshot(conn, metadata_item_id)
    except sqlite3.Error as exc:
        raise publish_error_from_sqlite(exc) from exc


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
                _STREAM_SELECT + f"WHERE mp.file IN ({marks}) AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL "  # noqa: S608
                "AND NOT (COALESCE(mi.proxy_type, 0) != 0 AND REPLACE(mp.file, char(92), '/') LIKE '%/Plex Versions/%') "
                'ORDER BY mp.id, ms."index"',
                plex_paths,
            ).fetchall()
    except sqlite3.Error as exc:
        raise publish_error_from_sqlite(exc) from exc
    streams = [_audio_stream(r) for r in rows if r[0] is not None]
    return streams, bool(rows)


def write_stream(
    db: LocalPlexDb,
    stream_id: int,
    fields: dict[str, str],
    *,
    deadline: float,
    expected: AudioStream | None = None,
    source: SourceFingerprint | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Add ``fields`` to one stream's extra_data, keeping its other fields, unless it already has loudness.

    Args:
        db: The server's Plex database.
        stream_id: ``media_streams.id``.
        fields: The ``ln:*`` fields (``analyze.ln_fields``).
        deadline: When to stop waiting for the database locks.
        expected: Indexed stream captured before analysis; checked under the write lock.
        source: Local file fingerprint captured before analysis.
        cancel_check: Checked again inside the transaction before changing the row.

    Returns:
        True when written; False when the stream already had loudness (Plex, or another job, got there first).

    Raises:
        PublishError: Publication or verification failed. Receipt/read-back failures can occur after commit;
            other failures roll back the transaction.
    """
    if set(fields) != MEASUREMENT_FIELDS | {VERSION_FIELD} or not has_analysis(encode_extra_data(fields)):
        raise PublishError("Refusing an incomplete loudness measurement")
    try:
        with db._database(read_only=False, deadline=deadline) as conn:
            check_schema(conn)
            db._begin_write(conn, deadline)
            try:
                if source is not None:
                    source.verify()
                if expected is not None:
                    target = conn.execute(
                        _STREAM_SELECT + "WHERE ms.id = ? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL",
                        (stream_id,),
                    ).fetchone()
                    if target is None or _audio_stream(target).identity() != expected.identity():
                        raise SourceChangedError("Plex's audio source changed during analysis; retry after its scan")
                row = conn.execute("SELECT extra_data FROM media_streams WHERE id = ?", (stream_id,)).fetchone()
                if row is None:
                    raise PublishError(f"Plex's audio stream {stream_id} is gone")
                before = row[0]
                existing, url_form = decode_extra_data(before)
                if has_analysis(before):
                    conn.execute("ROLLBACK")
                    return False
                if cancel_check and cancel_check():
                    raise PublishError("Loudness analysis cancelled before publication")
                # In the form the row was in, as the markers writer keeps a part's (``merge_part_extra_data``).
                after = encode_extra_data({**existing, **fields}, url_form=url_form)
                conn.execute(
                    "UPDATE media_streams SET extra_data = ?, updated_at = ? WHERE id = ?",
                    (after, int(time.time()), stream_id),
                )
                receipt = _log_write(db, {"stream_id": stream_id}, before, after, conn=conn)
                conn.execute("COMMIT")
                _log_commit(receipt)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            stored = conn.execute("SELECT extra_data FROM media_streams WHERE id = ?", (stream_id,)).fetchone()
    except sqlite3.Error as exc:
        raise publish_error_from_sqlite(exc) from exc
    if stored is None or not _same_extra_data(stored[0], after):
        raise PublishError(f"Plex's audio stream {stream_id} doesn't show the loudness just written")
    return True


def mark_item(
    db: LocalPlexDb,
    metadata_item_id: int,
    *,
    deadline: float,
    expected: tuple | None = None,
    source: SourceFingerprint | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> bool:
    """Mark an item analysed (``ln:loudnessAnalysisVersion`` in its extra_data) once all its audio streams are.

    All its live parts count, so an item with several versions is marked when the last one is done. Only
    ``extra_data`` is set: Plex's triggers on the table fire on title changes only (``check_schema``).

    Returns:
        True when marked; False when it was already marked or a stream still lacks loudness.

    Raises:
        PublishError: Publication or verification failed. Receipt/read-back failures can occur after commit;
            other failures roll back the transaction.
    """
    try:
        with db._database(read_only=False, deadline=deadline) as conn:
            check_schema(conn)
            db._begin_write(conn, deadline)
            try:
                if source is not None:
                    source.verify()
                snapshot = item_snapshot(conn, metadata_item_id)
                if expected is not None and snapshot != expected:
                    raise SourceChangedError("Plex's audio versions changed during analysis; retry after its scan")
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
                    if not _optimized_copy(file, proxy_type) and not has_analysis(extra)
                ]
                if _marked(before) or pending or not snapshot:
                    conn.execute("ROLLBACK")
                    return False
                if cancel_check and cancel_check():
                    raise PublishError("Loudness analysis cancelled before marking the item")
                after = encode_extra_data({**existing, VERSION_FIELD: ANALYSIS_VERSION}, url_form=url_form)
                conn.execute("UPDATE metadata_items SET extra_data = ? WHERE id = ?", (after, metadata_item_id))
                receipt = _log_write(db, {"metadata_item_id": metadata_item_id}, before, after, conn=conn)
                conn.execute("COMMIT")
                _log_commit(receipt)
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            stored = conn.execute("SELECT extra_data FROM metadata_items WHERE id = ?", (metadata_item_id,)).fetchone()
    except sqlite3.Error as exc:
        raise publish_error_from_sqlite(exc) from exc
    if stored is None or not _same_extra_data(stored[0], after):
        raise PublishError(f"Plex's item {metadata_item_id} doesn't show the loudness mark just written")
    return True


def undo_target(conn: sqlite3.Connection, row: dict) -> list:
    """Protect undo from row-ID reuse, including a stream attached to another source."""
    if "stream_id" in row:
        target = conn.execute(_STREAM_SELECT + "WHERE ms.id = ?", (row["stream_id"],)).fetchone()
        return list(_audio_stream(target).identity()) if target is not None else []
    target = conn.execute(
        "SELECT id, guid, metadata_type, created_at FROM metadata_items WHERE id = ?", (row["metadata_item_id"],)
    ).fetchone()
    if target is None:
        return []
    return [*target, [list(stream) for stream in item_snapshot(conn, row["metadata_item_id"])]]


def _append_record(record: dict) -> None:
    with _log_lock, open(write_log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _log_write(
    db: LocalPlexDb, row: dict[str, int], before: str | None, after: str, *, conn: sqlite3.Connection
) -> dict:
    """Durably journal before commit; inability to keep the undo record rolls back the write."""
    try:
        receipt = {
            "format": 2,
            "kind": "commit",
            "transaction_id": uuid.uuid4().hex,
            "database": database_identity(cast(str, db._path())),
        }
        _append_record(
            {
                **receipt,
                "kind": "intent",
                "target": undo_target(conn, row),
                **row,
                "before": before,
                "after": after,
                "at": int(time.time()),
            }
        )
    except OSError as exc:
        raise PublishError("Cannot save the loudness undo record; the database write was rolled back") from exc
    return receipt


def _log_commit(receipt: dict) -> None:
    """Confirm the intent only after SQLite commits; ambiguous intents are never undone."""
    try:
        _append_record(receipt)
    except OSError as exc:
        raise PublishError(
            "Loudness was committed, but its undo confirmation could not be saved; keep the journal for recovery"
        ) from exc
