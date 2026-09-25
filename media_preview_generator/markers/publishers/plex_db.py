"""Plex publisher: direct writes into Plex's library database (spec §3.1, §6.3).

Plex has no API for intro/credits markers. It serves markers from ``taggings`` rows on its single
``tags(tag_type=12, tag='')`` row, and rebuilds those rows from ``media_parts.extra_data`` when it re-detects, so
both places are written in one short transaction. Proven on PMS 1.43.4 in the lab; anything unexpected stops writes.

SQLite is only safe to share when both processes lock the very same file, so ``capability()``, every write and every
read-back first prove that another process (Plex) holds the database open through the path this app sees; only then
is the database opened. Markers read as evidence come over Plex's HTTP API (``sources.server_markers``); only the
read-back of what this app left on an item (``shows``) reads the database.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
import struct
import threading
import time
import urllib.parse
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger

from ..decide import FileLimits, unusable_server_marker
from ..fs import filesystem_type, gone_from_disk, is_local_filesystem, is_network_filesystem
from ..models import Candidate, Marker, MarkerType, Source
from .base import (
    NEXT_RUN,
    Capability,
    CapabilityReport,
    DatabaseBusyError,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
    ReadBackItem,
    Shown,
    agreed_across_versions,
    compare_shown,
    wait_cancelled,
)

if TYPE_CHECKING:
    from ...servers.base import ServerConfig
    from ...servers.plex import PlexServer
    from ..settings import ServerMarkersSettings

MARKER_TAG_TYPE = 12
# Plex serves credits starting 2 s later than stored and non-final credits ending 2 s earlier (spec §3.1).
CREDITS_SERVE_SHIFT_MS = 2_000
FINAL_TOLERANCE_MS = 2_000
INTRO_JSON_VERSION = 5
CREDITS_JSON_VERSION = 4
SAME_HOST_PATH_ADVICE = (
    "Mount the exact folder Plex uses, on the same machine "
    "(on unRAID, the same /mnt/cache or /mnt/user path Plex uses)."
)
# The default for how long one write() waits for locks in total (this process's lock on the database, then Plex's
# write lock) before giving up; a job then tries the file again a few minutes later. capability() allows this twice:
# once for the lock probe, once for its read-only checks after the Plex calls. It is a job's wait: another program
# writing to Plex's database (a Kometa-style tool) was observed holding its write lock 30.8 s, past the 30 s this was.
# A caller that must answer sooner passes its own ``db_timeout_s`` (the Inspector's publish, ruling P-R1).
BUSY_TIMEOUT_S = 120.0
# A job's worker holds a GPU or CPU worker previews need, so its publish waits this long instead, when the job retries a
# write the database refused a few minutes later (the checking stage, which holds no worker, keeps BUSY_TIMEOUT_S).
WORKER_BUSY_TIMEOUT_S = 10.0
# A write whose wait for another program's write lock begins this soon after the previous one gave up waiting for it
# counts the time Plex's database has been busy from where that one started (``_DatabaseLock``).
BUSY_STRETCH_GAP_S = 5.0
# Lock waits run in slices this long, asking between them whether the job was cancelled (``base.cancellable_waits``).
WAIT_SLICE_S = 1.0
WAIT_CANCELLED = "Stopped waiting for Plex's database: the job was cancelled"
# How long a write waits to learn whether another version's file is gone from disk before treating it as still there.
GONE_CHECK_TIMEOUT_S = 5.0
# How long the read before detection (``types_not_made_for_file``) waits for the database locks. Asked for every file
# Plex keeps its own markers of, so a busy database means "can't tell" (asked again next run), never a writer's wait.
STALE_READ_WAIT_S = 5.0
# Check servers reads Plex items back one read-only connection each, like a single file's read-back, and leaves this
# process's lock on the database free this long between them, so a write from another job waiting for it gets it.
READ_BACK_PAUSE_S = 0.001
# SQLite's WAL "dead-man switch": every process with the database open holds a read lock on this byte of <db>-shm.
_SHM_DMS_BYTE = 128
_TYPE_TEXT = {MarkerType.INTRO: "intro", MarkerType.CREDITS: "credits"}
_PART_KEY = {MarkerType.INTRO: "pv:intros", MarkerType.CREDITS: "pv:credits"}
_WRITTEN_TABLES = ("taggings", "media_parts")
_REQUIRED_COLUMNS = {
    "tags": {"id", "tag", "tag_type"},
    "taggings": {
        "id",
        "metadata_item_id",
        "tag_id",
        "index",
        "text",
        "time_offset",
        "end_time_offset",
        "thumb_url",
        "created_at",
        "extra_data",
    },
    "media_parts": {"id", "media_item_id", "file", "extra_data", "deleted_at", "updated_at"},
    "media_items": {"id", "metadata_item_id", "deleted_at", "proxy_type"},
    "metadata_items": {"id"},
}
# SQLite primary result codes (sqlite3.h): BUSY, LOCKED, PROTOCOL (a transient WAL race) / PERM, IOERR,
# READONLY, CANTOPEN, AUTH, NOTADB / FULL / CORRUPT.
_SQLITE_BUSY_CODES = frozenset({5, 6, 15})
_SQLITE_ACCESS_CODES = frozenset({3, 8, 10, 14, 23, 26})
_SQLITE_FULL = 13
_SQLITE_CORRUPT = 11


class _DatabaseLock:
    """This process's lock on one database file, and how long another program has kept its holder's write waiting.

    While the holder's ``BEGIN IMMEDIATE`` waits for another program's write lock (Plex itself, or a tool writing to
    Plex's database), the lock knows since when. A task of ours that gives up waiting behind that holder names the real
    cause, not the holder. When the holder gives up too, the next write that starts waiting within
    ``BUSY_STRETCH_GAP_S`` counts on from the same start, so every file of one busy stretch says how long it has lasted.
    Only the thread holding the lock writes these fields; a waiter reads them to word its error.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy_since: float | None = None
        self._waiting = False
        self._gave_up_at: float | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        """``threading.Lock.acquire``."""
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        """``threading.Lock.release``."""
        self._lock.release()

    def locked(self) -> bool:
        """``threading.Lock.locked``."""
        return self._lock.locked()

    def plex_busy_for(self, now: float) -> float | None:
        """Seconds another program has kept this database's writes waiting, while it still does (or just did).

        Args:
            now: ``time.monotonic()``.

        Returns:
            None when no write of ours is waiting for another program's write lock, and none gave up on it within the
            last ``BUSY_STRETCH_GAP_S``.
        """
        since, waiting, gave_up_at = self._busy_since, self._waiting, self._gave_up_at
        if since is None:
            return None
        if waiting or (gave_up_at is not None and now - gave_up_at <= BUSY_STRETCH_GAP_S):
            return now - since
        return None

    def start_waiting_for_plex(self) -> float:
        """Mark the holder as waiting for another program's write lock.

        Returns:
            When the busy stretch this wait belongs to began (``time.monotonic()``).
        """
        now = time.monotonic()
        if self.plex_busy_for(now) is None:
            self._busy_since = now
        self._waiting = True
        return self._busy_since if self._busy_since is not None else now

    def stop_waiting_for_plex(self, *, gave_up: bool) -> None:
        """The holder's wait ended: it got the write lock (the stretch is over), or gave up on it."""
        self._waiting = False
        if gave_up:
            self._gave_up_at = time.monotonic()
        else:
            self._busy_since = self._gave_up_at = None


_path_locks: dict[object, _DatabaseLock] = {}
_path_locks_guard = threading.Lock()
# Per thread: lock keys of databases this thread has a connection open to (see shm_lock_held_elsewhere), and the
# database lock it holds (``holding``, see ``_begin_write``).
_thread_state = threading.local()


def plex_busy_error(seconds: float) -> DatabaseBusyError:
    """The failure of a write that gave up because another program kept Plex's database locked.

    Args:
        seconds: How long that program has kept this app's writes waiting.

    Returns:
        The error, UNREACHABLE like any lock wait that ran out.
    """
    return DatabaseBusyError(
        f"Plex's database was busy (held by another program) for {max(1, round(seconds))} s; {NEXT_RUN}",
        state=Capability.UNREACHABLE,
    )


def report_of_failure(exc: PublishError, default: Capability, details: dict | None = None) -> CapabilityReport:
    """The capability report of a check that failed with ``exc``.

    A busy database stays one through the report (``details["db_busy"]``): ``write()`` raises it again as a
    ``DatabaseBusyError`` and the pipeline treats a busy capability check like a busy write, so the job retries the file
    instead of skipping it, and the answer isn't reused for the job's next files.

    Args:
        exc: The failure.
        default: The state when ``exc`` carries none.
        details: The report's details so far.

    Returns:
        The report.
    """
    busy = {"db_busy": True} if isinstance(exc, DatabaseBusyError) else {}
    return CapabilityReport(exc.state or default, str(exc), {**(details or {}), **busy})


def plex_db_path(plex_config_folder: str) -> str:
    """Library DB path under the "Plex Media Server" folder the app already uses for previews."""
    return os.path.join(plex_config_folder, "Plug-in Support", "Databases", "com.plexapp.plugins.library.db")


def _lock_key(db_path: str) -> object:
    # The file's identity, so bind-mount aliases and symlinks share one lock; realpath when it can't be stat'ed.
    try:
        st = os.stat(db_path)
    except OSError:
        return os.path.realpath(db_path)
    return (st.st_dev, st.st_ino)


def _db_lock(db_path: str) -> _DatabaseLock:
    """This process's lock for one database file.

    Closing any descriptor on a file drops every POSIX lock the process holds on it, including the locks of our own
    open SQLite connection. The lock probe opens and closes ``<db>-shm``, so it and every connection to the database
    hold this lock for their whole life.
    """
    key = _lock_key(db_path)
    with _path_locks_guard:
        return _path_locks.setdefault(key, _DatabaseLock())


@contextlib.contextmanager
def _holding_db_lock(db_path: str, deadline: float) -> Iterator[None]:
    """Hold this process's lock on the database until ``deadline`` at the latest.

    Waits in ``WAIT_SLICE_S`` slices, so a cancelled job (``base.cancellable_waits``) stops within one.

    Raises:
        DatabaseBusyError: The lock wasn't free in time. When its holder was itself waiting for another program's write
            lock, the error says so and for how long (``plex_busy_error``): that, not our own task, is the cause.
        PublishError: The job was cancelled while waiting (no state: nothing about the server).
    """
    lock = _db_lock(db_path)
    while not lock.acquire(timeout=max(0.0, min(WAIT_SLICE_S, deadline - time.monotonic()))):
        if time.monotonic() >= deadline:
            busy_for = lock.plex_busy_for(time.monotonic())
            if busy_for is not None:
                raise plex_busy_error(busy_for)
            raise DatabaseBusyError(
                f"Another Intro & Credits task is still using this Plex database; {NEXT_RUN}",
                state=Capability.UNREACHABLE,
            )
        if wait_cancelled():
            raise PublishError(WAIT_CANCELLED)
    held_before = getattr(_thread_state, "holding", None)
    _thread_state.holding = lock
    try:
        yield
    finally:
        _thread_state.holding = held_before
        lock.release()


def shm_lock_held_elsewhere(db_path: str, *, deadline: float | None = None) -> bool:
    """Whether another process has ``db_path`` open in WAL mode through this very file.

    Takes this process's lock on the database, so it never runs while one of our connections to it is open.

    Args:
        db_path: The database file.
        deadline: ``time.monotonic()`` value to stop waiting for that lock at (default: ``BUSY_TIMEOUT_S`` from now).

    Returns:
        True when some other process holds SQLite's dead-man-switch read lock on ``<db>-shm``.

    Raises:
        PublishError: UNREACHABLE when the lock wasn't free before the deadline.
        RuntimeError: Called while this thread has a connection to the database open.
    """
    if _lock_key(db_path) in getattr(_thread_state, "open", set()):
        raise RuntimeError("Lock probe inside an open connection to the same database would drop its locks")
    with _holding_db_lock(db_path, time.monotonic() + BUSY_TIMEOUT_S if deadline is None else deadline):
        return _shm_dms_locked_elsewhere(db_path)


def _shm_dms_locked_elsewhere(db_path: str) -> bool:
    try:
        fd = os.open(f"{db_path}-shm", os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        request = struct.pack("hhqqi", fcntl.F_WRLCK, os.SEEK_SET, _SHM_DMS_BYTE, 1, 0)
        lock_type = struct.unpack("hhqqi", fcntl.fcntl(fd, fcntl.F_GETLK, request))[0]
    except OSError:
        return False
    finally:
        os.close(fd)
    return lock_type != fcntl.F_UNLCK


def publish_error_from_sqlite(exc: sqlite3.Error) -> PublishError:
    """Map a SQLite failure to a ``PublishError`` whose state the UI can explain.

    Args:
        exc: The SQLite exception.

    Returns:
        UNREACHABLE for busy/locked (a ``DatabaseBusyError``, which a job tries again a few minutes later),
        MISCONFIGURED for files we can't open or write, UNSUPPORTED_SCHEMA otherwise.
    """
    code = getattr(exc, "sqlite_errorcode", None)  # Python 3.11+
    text = str(exc)
    if isinstance(code, int):
        primary = code & 0xFF
        busy, full, damaged = primary in _SQLITE_BUSY_CODES, primary == _SQLITE_FULL, primary == _SQLITE_CORRUPT
        access = primary in _SQLITE_ACCESS_CODES
    else:
        lowered = text.lower()
        busy = any(w in lowered for w in ("locked", "busy", "locking protocol"))
        full, damaged = "disk is full" in lowered, "malformed" in lowered
        access = any(w in lowered for w in ("readonly", "unable to open", "permission", "not a database", "i/o"))
    if busy:
        return DatabaseBusyError(
            f"Plex is busy writing its database; {NEXT_RUN} ({text})", state=Capability.UNREACHABLE
        )
    if full:
        return PublishError(
            f"Disk full: Plex's database can't take any writes ({text})", state=Capability.MISCONFIGURED
        )
    if damaged:
        return PublishError(
            f"Database damaged: Plex's database needs repairing before markers can be written ({text})",
            state=Capability.MISCONFIGURED,
        )
    if access:
        return PublishError(f"This app can't use Plex's database file ({text})", state=Capability.MISCONFIGURED)
    return PublishError(
        f"Plex's database returned an unexpected error; not writing markers ({text})",
        state=Capability.UNSUPPORTED_SCHEMA,
    )


def _plex_quote(value: str) -> str:
    # Matches Plex's own encoder: everything but alphanumerics and -_~ is %-escaped, including "." (verified
    # byte-for-byte against every extra_data row of media_parts, media_items, metadata_items, media_streams and
    # taggings in the lab Plex 1.43.4 DB, and of media_parts, media_items and marker taggings in the owner's
    # production DB: 517,476 rows, read-only, 2026-09-19).
    return urllib.parse.quote(value, safe="").replace(".", "%2E")


def _url_form(d: dict[str, str]) -> str:
    # With no fields left this is "", which is what Plex's own rollback of an emptied JSON part
    # (extra_data ->> 'url' of {"url":""}) writes, and reads back as no fields in either form. Plex itself leaves an
    # empty part NULL (production: 55 of them, no "" anywhere), but nothing reads the form of an empty part: the next
    # write that puts a key back writes JSON, exactly as it does over NULL.
    return "&".join(f"{_plex_quote(k)}={_plex_quote(d[k])}" for k in sorted(d) if k != "url")


def encode_extra_data(d: dict[str, str], *, url_form: bool = False) -> str:
    """Serialise an extra_data dict the way Plex does, in either of the two forms Plex stores.

    The JSON form, Plex's usual one since its 2023-09 schema migration, is compact JSON with sorted keys and a trailing
    ``url`` field holding the URL-encoded form of the other fields. The URL-encoded form alone (``k=v&k=v``, sorted
    keys, no ``url`` field) is the format before that migration, and what its rollback (``extra_data ->> 'url'``)
    turns a row back into. PMS 1.43.4 still writes it: its one-time credits ``final`` migration, run at a new
    database's first weekly optimize, rewrote the 24 parts it changed in this form (lab, 2026-09-17).

    Args:
        d: The fields; a ``url`` field is ignored and rebuilt.
        url_form: Write the URL-encoded form instead of JSON.

    Returns:
        The extra_data text.
    """
    if url_form:
        return _url_form(d)
    ordered = {k: d[k] for k in sorted(d) if k != "url"}
    ordered["url"] = _url_form(ordered)
    return json.dumps(ordered, separators=(",", ":"), ensure_ascii=False)


def decode_extra_data(extra: str | None) -> tuple[dict[str, str], bool]:
    """Read extra_data in either form Plex stores (see ``encode_extra_data``).

    Args:
        extra: The stored text; None or "" is no fields.

    Returns:
        The fields (the JSON form's ``url`` field included) and whether ``extra`` is the URL-encoded form.

    Raises:
        PublishError: UNSUPPORTED_SCHEMA when ``extra`` is JSON but not an object of text values, or is not text
            ``encode_extra_data`` writes back byte for byte in the URL-encoded form (so a part is only ever rewritten
            in a form Plex itself wrote).
    """
    if not extra:
        return {}, False
    if extra.startswith("{"):
        try:
            parsed = json.loads(extra)
        except ValueError as exc:
            raise PublishError("Plex extra_data is not valid JSON", state=Capability.UNSUPPORTED_SCHEMA) from exc
        if not isinstance(parsed, dict):
            raise PublishError("Plex extra_data is not a JSON object", state=Capability.UNSUPPORTED_SCHEMA)
        if any(not isinstance(v, str) for v in parsed.values()):
            raise PublishError(
                "Plex extra_data holds a non-text value; not writing markers.", state=Capability.UNSUPPORTED_SCHEMA
            )
        return parsed, False
    fields: dict[str, str] | None = {}
    try:
        for pair in extra.split("&"):
            key, equals, value = pair.partition("=")
            if not equals:
                raise ValueError(pair)
            fields[urllib.parse.unquote(key, errors="strict")] = urllib.parse.unquote(value, errors="strict")
    except ValueError:  # UnicodeDecodeError included
        fields = None
    # Unsorted or repeated keys, another escaping, a url field: not what Plex writes, so not what we'd write back.
    if fields is None or _url_form(fields) != extra:
        raise PublishError(
            "Plex extra_data is in a form this app doesn't know; not writing markers.",
            state=Capability.UNSUPPORTED_SCHEMA,
        )
    return fields, True


def _is_final(marker: Marker, duration_ms: int | None) -> bool:
    return marker.end_ms >= duration_ms - FINAL_TOLERANCE_MS


def _stored_times(marker: Marker, duration_ms: int | None) -> tuple[int, int, bool]:
    if marker.type is not MarkerType.CREDITS:
        return marker.start_ms, marker.end_ms, False
    if duration_ms is None or duration_ms <= 0:
        # Without a duration every credits marker would look "final", and Plex would skip straight past the end.
        raise PublishError("The file's duration is unknown, so credits markers can't be written to Plex")
    final = _is_final(marker, duration_ms)
    start = max(0, marker.start_ms - CREDITS_SERVE_SHIFT_MS)
    end = marker.end_ms if final else marker.end_ms + CREDITS_SERVE_SHIFT_MS
    return start, end, final


def _tagging_extra(marker: Marker, final: bool) -> str:
    if marker.type is MarkerType.INTRO:
        return encode_extra_data({"pv:version": str(INTRO_JSON_VERSION)})
    fields = {"pv:version": str(CREDITS_JSON_VERSION)}
    if final:
        fields["pv:final"] = "1"
    return encode_extra_data(fields)


def _served_times(mtype: MarkerType, start: int, end: int, final: bool) -> tuple[int, int]:
    """What Plex serves for a stored marker: the inverse of ``_stored_times``, needing no file duration."""
    if mtype is not MarkerType.CREDITS:
        return start, end
    return start + CREDITS_SERVE_SHIFT_MS, end if final else end - CREDITS_SERVE_SHIFT_MS


def _row_is_final(extra_data: str | None) -> bool:
    try:
        fields = decode_extra_data(extra_data)[0]
    except PublishError:
        return False  # never seen in a Plex database (lab or production): read as not final
    return fields.get("pv:final") == "1"


def _is_final_entry(entry: dict) -> bool:
    # Plex's JSON writer stores true; its credits final migration stores 1 (lab and production DBs).
    final = entry.get("final")
    return final is True or (type(final) is int and final == 1)


def _rows_served_with_final(rows: list[_TaggingRow], mtype: MarkerType) -> list[tuple[int, int, bool]]:
    """``(start, end, final)`` per taggings row of a type, in served times."""
    out = []
    for r in rows:
        if r.text == _TYPE_TEXT[mtype]:
            final = _row_is_final(r.extra_data)
            out.append((*_served_times(mtype, r.time_offset, r.end_time_offset, final), final))
    return sorted(out)


def _served_of(markers: list[Marker] | tuple[Marker, ...], mtype: MarkerType) -> list[tuple[int, int]]:
    return sorted((m.start_ms, m.end_ms) for m in markers if m.type is mtype)


def _served_with_final(markers: list[Marker] | tuple[Marker, ...], mtype: MarkerType, duration_ms: int) -> list:
    """``(start, end, final)`` per marker of a type, in served times, with the ``final`` flag this duration gives."""
    return sorted((m.start_ms, m.end_ms, _stored_times(m, duration_ms)[2]) for m in markers if m.type is mtype)


def _part_entries(mtype: MarkerType, value: str | None) -> list[tuple[int, int, bool]] | None:
    """``(start, end, final)`` per entry of a stored ``pv:intros``/``pv:credits`` value, in served times; None if
    unreadable."""
    try:
        entries = json.loads(value)["MediaPartMarkersArray"]["MediaPartMarker"]
        out = []
        for e in entries:
            final = _is_final_entry(e)
            out.append((*_served_times(mtype, int(e["startTimeOffset"]), int(e["endTimeOffset"]), final), final))
        return sorted(out)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _part_served(mtype: MarkerType, value: str | None) -> list[tuple[int, int]] | None:
    """Served times in a stored ``pv:intros``/``pv:credits`` value, each entry with its own ``final``; None if unreadable."""
    entries = _part_entries(mtype, value)
    return None if entries is None else [(start, end) for start, end, _final in entries]


def _stored_row(marker: Marker, duration_ms: int | None) -> tuple[int, int, str]:
    """``(time_offset, end_time_offset, extra_data)`` of the taggings row we write for a marker."""
    start, end, final = _stored_times(marker, duration_ms)
    return start, end, _tagging_extra(marker, final)


def _part_payload(mtype: MarkerType, markers: list[Marker], duration_ms: int | None) -> str:
    """The ``pv:intros``/``pv:credits`` value we write for a type."""
    entries = []
    for m in markers:
        start, end, final = _stored_times(m, duration_ms)
        entry: dict[str, object] = {"startTimeOffset": start, "endTimeOffset": end}
        if final:
            entry["final"] = True
        entries.append(entry)
    payload = {
        "MediaPartMarkersArray": {
            "attributeName": "intros" if mtype is MarkerType.INTRO else "credits",
            "version": INTRO_JSON_VERSION if mtype is MarkerType.INTRO else CREDITS_JSON_VERSION,
            "MediaPartMarker": entries,
        }
    }
    return json.dumps(payload, separators=(",", ":"))


def _check_marker_array(key: str, value: object) -> None:
    """Raise unless a stored ``pv:intros``/``pv:credits`` value is the tested shape and version (empty = cleared)."""
    if value == "":
        return
    attribute, version = ("intros", INTRO_JSON_VERSION) if key == "pv:intros" else ("credits", CREDITS_JSON_VERSION)
    try:
        arr = json.loads(value)["MediaPartMarkersArray"]
        found_attribute, found_version = arr.get("attributeName"), arr.get("version")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PublishError(
            f"Plex {key} data has an unknown shape; not writing markers.", state=Capability.UNSUPPORTED_SCHEMA
        ) from exc
    if found_attribute != attribute or found_version != version:
        raise PublishError(
            f"Plex stores {attribute} markers as {found_attribute} version {found_version} (tested: {version}); "
            "not writing markers.",
            state=Capability.UNSUPPORTED_SCHEMA,
        )


def merge_part_extra_data(
    existing: str | None,
    wanted: list[Marker],
    managed: set[MarkerType],
    duration_ms: int | None,
    *,
    previous: list[Marker] | tuple[Marker, ...] = (),
    refresh_final: frozenset[MarkerType] = frozenset(),
) -> str:
    """Rewrite ``pv:intros``/``pv:credits`` for managed types, keep every other key and the part's form.

    Args:
        existing: The part's current ``extra_data`` in either of Plex's forms (see ``encode_extra_data``), written back
            in the same form (JSON, with a rebuilt ``url``, when None or empty).
        wanted: Markers to show, in served times.
        managed: Types we set or remove on this item.
        duration_ms: File duration, for credits' "runs to the end" flag.
        previous: Markers we published before. A managed type without wanted markers loses its key only while the
            key still serves exactly these times (compared in served time, so a changed duration doesn't matter); a
            value Plex re-detected since stays.
        refresh_final: Types whose value is also rewritten when it serves the wanted times with another ``final``
            flag than ``duration_ms`` gives (see ``_refresh_final_types``). Other values serving the wanted times
            stay byte for byte.

    Returns:
        The new ``extra_data`` string.

    Raises:
        PublishError: existing extra_data is in neither of Plex's forms (see ``decode_extra_data``), or holds marker
            data of an untested shape/version.
    """
    d, url_form = decode_extra_data(existing)
    for key in _PART_KEY.values():
        if key in d:
            _check_marker_array(key, d[key])
    for mtype in (MarkerType.INTRO, MarkerType.CREDITS):
        if mtype not in managed:
            continue
        key = _PART_KEY[mtype]
        of_type = [m for m in wanted if m.type is mtype]
        if of_type:
            stale_final = mtype in refresh_final and _part_entries(mtype, d.get(key)) != _served_with_final(
                of_type, mtype, duration_ms
            )
            if _part_served(mtype, d.get(key)) != _served_of(of_type, mtype) or stale_final:
                d[key] = _part_payload(mtype, of_type, duration_ms)
            continue
        ours_before = [m for m in previous if m.type is mtype]
        # Removing the key (not writing "") lets Plex's own non-forced detection analyse the part again (spec §14).
        if ours_before and _part_served(mtype, d.get(key)) == _served_of(ours_before, mtype):
            del d[key]
    return encode_extra_data(d, url_form=url_form)


def _rating_key(item_id: str) -> int:
    bare = str(item_id or "").strip().rsplit("/", 1)[-1]
    if not bare.isdigit():
        raise ItemNotFoundError(f"{item_id!r} is not a Plex item id")
    return int(bare)


class _Part(NamedTuple):
    id: int
    media_item_id: int
    file: str
    extra_data: str | None
    proxy_type: int | None
    # When the part last changed, in seconds since the epoch: the file's mtime (Plex leaves ``created_at`` empty).
    updated_at: int | None = None


def _same_extra_data(a: str | None, b: str | None) -> bool:
    """Same keys and values, ignoring the derived ``url`` field (no rewrite for a byte difference)."""
    if a == b:
        return True
    try:
        da, db = decode_extra_data(a)[0], decode_extra_data(b)[0]
    except PublishError:
        return False
    return {k: v for k, v in da.items() if k != "url"} == {k: v for k, v in db.items() if k != "url"}


def _is_optimized_copy(part: _Part) -> bool:
    # Plex's "Optimize" transcodes carry media_items.proxy_type and live under a "Plex Versions" folder. Both must hold:
    # an unknown proxy_type on an ordinary file still takes part in the version agreement.
    return bool(part.proxy_type) and "Plex Versions" in part.file.replace("\\", "/").split("/")


def _lies_under(path: str, folder: str) -> bool:
    folder = folder.rstrip("/")
    return not folder or path.startswith(folder + "/")


def _version_files(parts: list[_Part]) -> tuple[str, ...]:
    """The item's versions as its sorted part files, Plex's optimized copies left out (they take no part in agreement)."""
    return tuple(sorted(p.file for p in parts if not _is_optimized_copy(p)))


def _same_files(a: list[_Part], b: list[_Part]) -> bool:
    """The same parts, files and versions (extra_data and the time a part changed aside: an app older than this
    code sends no time)."""
    return [p._replace(extra_data=None, updated_at=None) for p in a] == [
        p._replace(extra_data=None, updated_at=None) for p in b
    ]


class _TaggingRow(NamedTuple):
    id: int
    index: int
    text: str
    time_offset: int
    end_time_offset: int
    thumb_url: str | None
    extra_data: str | None
    created_at: int | None = None  # when the row was tagged, in seconds since the epoch


class _Plan(NamedTuple):
    """What one write changes; empty lists mean nothing to do."""

    replaced_texts: list[str]
    inserts: list[tuple[int, str, int, int, str]]  # (index, text, time_offset, end_time_offset, extra_data)
    reindex: list[tuple[int, int]]  # (index, taggings id)
    part_updates: list[tuple[str, int]]  # (extra_data, media_parts id)
    ours: list[Marker]  # the desired markers the item shows as ours after the write (kept types left out)
    kept_types: frozenset[MarkerType]  # types whose rows are Plex's own and stay untouched
    replaced_own: frozenset[MarkerType]  # locked types whose Plex rows this write replaces under "Keep Plex's"
    # Types "Keep Plex's" would have kept but whose Plex rows were made for an earlier file, so ours replace them
    replaced_stale: frozenset[MarkerType] = frozenset()

    @property
    def is_noop(self) -> bool:
        return not (self.replaced_texts or self.reindex or self.part_updates)


def _nothing_to_write(plan: _Plan) -> bool:
    # With nothing of ours to show, other rows (Plex's own) are only renumbered around rows we actually remove.
    return plan.is_noop or not (plan.ours or plan.replaced_texts or plan.part_updates)


def _refresh_final_types(
    parts: list[_Part], wanted: list[Marker], duration_ms: int | None, keep_plex: bool
) -> frozenset[MarkerType]:
    """The types whose rows and ``pv:`` key are rewritten when they serve the wanted times with a stale ``final`` flag.

    The flag says whether credits run to the end of the file, and Plex's docs say some apps open their post-play screen
    at the final credits, so a flag left from a version since deleted is put right. Only on an item with one version:
    with several, each version's runtime can give another flag for the same times, and rewriting would flip it back
    on the other version's next run. Only with the file's duration known. Under "Keep Plex's" only for a type the user
    locked: rows serving the wanted times can be Plex's own even when our record lists those times (a write that
    changed nothing still records them), and those rows are never touched — but a locked type's rows are ours whatever
    the setting says (spec §5.5 rule 1). Under "Use ours", Plex's rows with identical times get our flag.
    """
    if not duration_ms or duration_ms <= 0 or len(_version_files(parts)) != 1:
        return frozenset()
    return frozenset(m.type for m in wanted if m.locked or not keep_plex)


def _kept_types(
    rows: list[_TaggingRow],
    wanted: list[Marker],
    prior: list[Marker],
    own_prior: list[Marker],
    kept_before: frozenset[MarkerType],
    keep_plex: bool,
    *,
    limits: FileLimits | None = None,
    stale: frozenset[MarkerType] = frozenset(),
) -> frozenset[MarkerType]:
    """The types whose rows on the item are Plex's own and must stay ("Keep Plex's", ``on_plex_redetect``).

    A type this write would show becomes kept when Plex has rows of it that aren't provably ours: not what we'd write,
    not what we left there (``prior``), not what the calling file left on its item before a merge (``own_prior``).
    That covers Plex's own detection replacing ours, filling a type we removed, and markers already on an item we have
    no record of (a first publish, a reset markers.db, a re-added server): those can't be told from Plex's. Rows that
    already show what we'd write are treated as ours. A kept type stays kept, whatever gets written for the item,
    until Plex has no rows of it left or the server is set to restore ours.

    Never kept: a type whose every row besides ours can't be right for this file (``unusable_server_marker`` against
    ``limits``, e.g. credits that start after the file ends). Plex hasn't really processed this file for it, so ours
    is written. One of Plex's rows that can be right keeps the type, rows and all. Without ``limits`` (an older app
    talking to this agent) nothing is checked.

    Not kept either: a type ``stale`` names (its rows were made for an earlier file at this path,
    ``_types_not_made_for_file``) when this write has an answer of ours for it. Without one, Plex's rows stay: close
    is better than nothing.

    Locks are not read here: this answers only what the **setting** would keep. ``_plan`` takes the types the user
    locked back out of the answer, since a locked marker wins over "Keep Plex's" (spec §5.5 rule 1), and reports them
    as replaced instead.
    """
    if not keep_plex:
        return frozenset()
    kept = set()
    for mtype, text in _TYPE_TEXT.items():
        current = sorted(
            _served_times(mtype, r.time_offset, r.end_time_offset, _row_is_final(r.extra_data))
            for r in rows
            if r.text == text
        )
        if not current:
            continue
        would_show = _served_of(wanted, mtype)
        provably_ours = (would_show, _served_of(prior, mtype), _served_of(own_prior, mtype))
        if limits is not None and _plex_rows_cant_be_right(mtype, current, provably_ours, limits):
            continue
        if mtype in stale and would_show:
            continue
        if mtype in kept_before or (would_show and current not in provably_ours):
            kept.add(mtype)
    return frozenset(kept)


def _plex_rows_cant_be_right(
    mtype: MarkerType,
    current: list[tuple[int, int]],
    provably_ours: tuple[list[tuple[int, int]], ...],
    limits: FileLimits,
) -> bool:
    """Whether the item has rows of a type besides ours, and every one of them can't be right for the file."""
    ours = {times for served in provably_ours for times in served}
    plex_rows = [times for times in current if times not in ours]
    return bool(plex_rows) and all(
        unusable_server_marker(Candidate(mtype, start, end, Source.SERVER_MARKERS), limits) for start, end in plex_rows
    )


# A row tagged within this long before its file last changed can't be told from one tagged for the file: the rule was
# validated with a clear minute between the two.
STALE_MARGIN_S = 60


def _epoch(value: object) -> int | None:
    """A Plex timestamp column (seconds since the epoch); None when empty, not a number, or not after 1970."""
    try:
        seconds = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _types_not_made_for_file(rows: list[_TaggingRow], parts: list[_Part]) -> frozenset[MarkerType]:
    """The types whose marker rows were made for an earlier file at this path (stale).

    Plex's markers belong to the metadata item, so they outlive a file replacement (production, 2026-09-24: Bones'
    markers were detected 2025-06-17 against the old Blu-ray files, which Sonarr replaced with 25 fps files on
    2026-09-23, leaving intros 9-17 s late and credits past the end). A type counts only when both hold — the rule
    validated against Sonarr's imports, with no false positive among 5,493 re-detected files:

    * no live part carries Plex's own detection record of the rows (its ``pv:intros`` / ``pv:credits`` holding
      those times), which Plex writes with them when it analyses the file it has now;
    * every row was tagged more than :data:`STALE_MARGIN_S` before every live part last changed
      (``media_parts.updated_at``, the file's mtime).

    Anything that can't be read counts as made for the file.
    """
    changed = [_epoch(p.updated_at) for p in parts]
    if not changed or any(t is None for t in changed):
        return frozenset()
    file_changed = min(t for t in changed if t is not None)
    stale = set()
    for mtype, text in _TYPE_TEXT.items():
        of_type = [r for r in rows if r.text == text]
        tagged = [_epoch(r.created_at) for r in of_type]
        if not of_type or any(t is None for t in tagged):
            continue
        if max(t for t in tagged if t is not None) + STALE_MARGIN_S >= file_changed:
            continue
        if _a_part_records(mtype, of_type, parts) is False:
            stale.add(mtype)
    return frozenset(stale)


def _a_part_records(mtype: MarkerType, rows: list[_TaggingRow], parts: list[_Part]) -> bool | None:
    """Whether a live part's ``pv:`` key holds these rows' times; None when a part's key can't be read.

    Compared as stored (``time_offset``/``end_time_offset`` against ``startTimeOffset``/``endTimeOffset``), which Plex
    writes alike, so a ``final`` flag that differs between the row and the entry doesn't hide the record.
    """
    times = {(r.time_offset, r.end_time_offset) for r in rows}
    for part in parts:
        try:
            value = decode_extra_data(part.extra_data)[0].get(_PART_KEY[mtype])
        except PublishError:
            return None
        if value is None:
            continue
        try:
            entries = json.loads(value)["MediaPartMarkersArray"]["MediaPartMarker"]
            recorded = {(int(e["startTimeOffset"]), int(e["endTimeOffset"])) for e in entries}
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        if times <= recorded:
            return True
    return False


class ItemRead(NamedTuple):
    """One item as Plex's database has it right now."""

    exists: bool  # the database knows this rating key at all
    parts: list[_Part]  # its live files (deleted parts and items in Plex's trash left out)
    # Types whose rows were made for an earlier file at this path (``_types_not_made_for_file``); None when not read
    # (an agent older than this field).
    stale_types: frozenset[MarkerType] | None = None


class WriteRequest(NamedTuple):
    """One item's write: everything the decision half worked out, and the parts it worked it out for."""

    rating_key: int
    parts: list[_Part]  # the item's parts when ``wanted`` was decided; a different set now stops the write
    wanted: list[Marker]  # the markers the item should show, this server's types only
    prior: list[Marker]  # what this app last left on the item
    duration_ms: int | None
    own_prior: list[Marker]  # what the calling file left on the item it belonged to before a merge or split
    calling_part_ids: tuple[int, ...]  # the parts that are the calling file
    kept_types: frozenset[MarkerType]  # types the last write kept as Plex's own
    keep_plex: bool  # "Keep Plex's" is on for this server
    limits: FileLimits | None = None  # the file's limits a kept Plex marker must fit (None: not checked)


class WriteResult(NamedTuple):
    """What one write did (see ``MarkerPublisher.write`` for the fields' meaning)."""

    changed: bool
    ours: list[Marker]
    kept_types: frozenset[MarkerType]
    replaced_own: frozenset[MarkerType]
    replaced_stale: frozenset[MarkerType] = frozenset()  # an agent older than this field says nothing


class ShownAsk(NamedTuple):
    """One item of a read-back: ``ours`` is already projected to the types this publisher writes."""

    rating_key: int
    ours: list[Marker]
    kept_types: frozenset[MarkerType]
    item_files: tuple[str, ...] | None


class ShownAnswer(NamedTuple):
    """What one item shows, and its version files as Plex stores them (optimized copies left out)."""

    shown: Shown | None
    version_files: tuple[str, ...]


class ShownBatch(NamedTuple):
    """A read-back's answers in the order they were asked.

    Fewer answers than items and ``unreadable`` means the database itself stopped being readable and every item left
    is unread; fewer without it means the call was cancelled.
    """

    answers: list[ShownAnswer]
    unreadable: bool


class PlexDatabase(ABC):
    """Everything one Plex server's library database is asked for, wherever that file actually is.

    Two implementations: :class:`LocalPlexDb` opens the file this process can see, and
    ``plex_remote.RemotePlexDb`` asks the Plex marker agent to run :class:`LocalPlexDb` on Plex's own machine. The
    rules live in this module and run in one place either way — the schema guards, the marker tag row, the
    ``taggings`` + ``media_parts.extra_data`` pair, the ±2 s serving shifts, kept and locked types. A transport moves
    arguments and answers; it never decides anything, and it never takes a database path from its caller.
    """

    # True when a failed write is known to have changed nothing (one SQLite transaction, in this process). Over a
    # network an answer can be lost after the transaction committed, so the remote transport says False and the
    # publisher's record of what is ours is treated as unknown after a failure (``MarkerPublisher.atomic_writes``).
    atomic_writes: bool = True

    @abstractmethod
    def file_checks(self, *, deadline: float) -> CapabilityReport:
        """Check the database file itself: found, on a local disk, writable, and open in Plex's process."""

    @abstractmethod
    def db_checks(self, *, deadline: float) -> CapabilityReport:
        """Check what is inside it: the tested schema, marker data this app knows, and Plex's one marker tag row."""

    @abstractmethod
    def read_item(self, rating_key: int, *, deadline: float) -> ItemRead:
        """Read one item's live parts (schema and tag row checked first), and ``stale_types``: the types whose
        marker rows were made for an earlier file at its path (``_types_not_made_for_file``).

        Raises:
            PublishError: The database couldn't be read, or isn't the tested schema.
        """

    @abstractmethod
    def write_item(self, request: WriteRequest, *, deadline: float) -> WriteResult:
        """Make one item show ``request.wanted``, in one transaction.

        Raises:
            PublishError: Nothing was written.
        """

    @abstractmethod
    def shown_many(
        self, items: list[ShownAsk], *, timeout_s: float, cancel_check: Callable[[], bool] | None = None
    ) -> ShownBatch:
        """Read what items show of what this app left on them."""

    @abstractmethod
    def item_exists(self, rating_key: int, *, deadline: float) -> bool:
        """Whether the database has an item with this rating key.

        Raises:
            PublishError: The database couldn't be read.
        """


class LocalPlexDb(PlexDatabase):
    """Plex's database as a file this process can open — the only implementation that touches SQLite.

    The Plex marker agent runs this class on Plex's machine, so a remote write is the same code as a local one.
    """

    def __init__(
        self,
        path_provider: Callable[[], str | None],
        *,
        label: str = "",
        mountinfo_path: str = "/proc/self/mountinfo",
    ) -> None:
        """Create the local database.

        Args:
            path_provider: Returns the database file's path right now, or None when the Plex config folder isn't set.
            label: The server's name, for log lines.
            mountinfo_path: For tests.
        """
        self._path = path_provider
        self._label = label
        self._mountinfo_path = mountinfo_path

    def _connect(self, *, read_only: bool, timeout: float) -> sqlite3.Connection:
        mode = "ro" if read_only else "rw"
        # Quoted: SQLite reads "?", "#" and "%" in a file: URI as query, fragment and escapes, which would open a
        # different path for folders named like "Plex #2". mode=rw never creates a missing file.
        uri = f"file:{urllib.parse.quote(os.path.abspath(self._path() or ''))}?mode={mode}"
        return sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)

    @contextlib.contextmanager
    def _database(self, *, read_only: bool, deadline: float) -> Iterator[sqlite3.Connection]:
        """A connection that lives entirely under the database's process lock (see ``_db_lock``).

        Raises:
            PublishError: UNREACHABLE when the lock isn't free before ``deadline``; MISCONFIGURED without a folder.
        """
        db = self._path()
        if not db:
            raise PublishError("The Plex config folder isn't set", state=Capability.MISCONFIGURED)
        key = _lock_key(db)
        with _holding_db_lock(db, deadline):
            # SQLite's own busy wait gets only what is left of the same deadline.
            conn = self._connect(read_only=read_only, timeout=max(0.0, deadline - time.monotonic()))
            open_keys = _thread_state.__dict__.setdefault("open", set())
            open_keys.add(key)
            try:
                yield conn
            finally:
                open_keys.discard(key)
                conn.close()

    @staticmethod
    def _check_schema(conn: sqlite3.Connection) -> None:
        for table, required in _REQUIRED_COLUMNS.items():
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608 - fixed table names
            missing = required - cols
            if missing:
                raise PublishError(
                    f"Plex database looks different from the tested version (table {table} lacks {sorted(missing)}); "
                    "not writing markers.",
                    state=Capability.UNSUPPORTED_SCHEMA,
                )
        triggers = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?, ?) ORDER BY name",
            _WRITTEN_TABLES,
        ).fetchall()
        if triggers:
            found = ", ".join(f"{name} on {table}" for name, table in triggers)
            raise PublishError(
                f"Plex database has triggers on tables we write ({found}), unlike the tested version; "
                "not writing markers.",
                state=Capability.UNSUPPORTED_SCHEMA,
            )

    @staticmethod
    def _check_library_marker_versions(conn: sqlite3.Connection) -> None:
        # Two LIKE scans of media_parts per key: run from capability() (cached per job, for a few seconds only while
        # Plex Pass doesn't answer), never per write. Each write still validates the parts it touches in
        # merge_part_extra_data. Newest rows first, and each form its own window: Plex's credits final migration
        # rewrites parts where they are, so a URL-encoded part keeps its old id and a shared window of the newest 50
        # rows could hold none (see encode_extra_data).
        for key in _PART_KEY.values():
            url_form_like = "%" + _plex_quote(key).replace("%", r"\%") + r"=\%7B%"  # pv%3Aintros=%7B...
            rows = conn.execute(
                "SELECT extra_data FROM (SELECT extra_data FROM media_parts WHERE extra_data LIKE ? "
                "ORDER BY id DESC LIMIT 50) UNION ALL "
                "SELECT extra_data FROM (SELECT extra_data FROM media_parts WHERE extra_data LIKE ? ESCAPE '\\' "
                "ORDER BY id DESC LIMIT 50)",
                (f'%"{key}":"{{%', url_form_like),
            ).fetchall()
            for (extra,) in rows:
                try:
                    value = decode_extra_data(extra)[0][key]
                except (PublishError, KeyError):
                    continue
                _check_marker_array(key, value)

    @staticmethod
    def _item_parts(conn: sqlite3.Connection, rating_key: int) -> list[_Part]:
        rows = conn.execute(
            "SELECT mp.id, mp.media_item_id, mp.file, mp.extra_data, mi.proxy_type, mp.updated_at FROM media_parts mp "
            "JOIN media_items mi ON mi.id = mp.media_item_id "
            "WHERE mi.metadata_item_id=? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL ORDER BY mp.id",
            (rating_key,),
        ).fetchall()
        return [_Part(*row) for row in rows]

    @staticmethod
    def _item_rows(conn: sqlite3.Connection, rating_key: int, tag_id: int) -> list[_TaggingRow]:
        return [
            _TaggingRow(*row)
            for row in conn.execute(
                "SELECT id, [index], text, time_offset, end_time_offset, thumb_url, extra_data, created_at "
                "FROM taggings WHERE metadata_item_id=? AND tag_id=? ORDER BY [index], id",
                (rating_key, tag_id),
            )
        ]

    @staticmethod
    def _item_exists(conn: sqlite3.Connection, rating_key: int) -> bool:
        return conn.execute("SELECT 1 FROM metadata_items WHERE id=?", (rating_key,)).fetchone() is not None

    @staticmethod
    def _marker_tag_id(conn: sqlite3.Connection) -> int:
        rows = conn.execute(
            "SELECT id FROM tags WHERE tag_type=? AND tag='' ORDER BY id LIMIT 2", (MARKER_TAG_TYPE,)
        ).fetchall()
        if not rows:
            raise PublishError(
                "Plex hasn't created its marker tag yet. Run Plex's own intro or credits detection once on any "
                "item, then try again.",
                state=Capability.NEEDS_PLEX_DETECTION_ONCE,
            )
        if len(rows) > 1:
            raise PublishError(
                "Plex's database has more than one marker tag row, so it's unclear which one Plex serves; "
                "not writing markers.",
                state=Capability.UNSUPPORTED_SCHEMA,
            )
        return int(rows[0][0])

    @staticmethod
    def _unwritable_paths(db: str) -> list[str]:
        paths = [p for p in (db, f"{db}-wal", f"{db}-shm") if os.path.exists(p) and not os.access(p, os.W_OK)]
        folder = os.path.dirname(db)
        if not os.access(folder, os.W_OK | os.X_OK):
            paths.append(folder)
        return paths

    def file_checks(self, *, deadline: float) -> CapabilityReport:
        """The database file: found, on a local disk, writable, and held open by another process (Plex).

        Args:
            deadline: When to stop waiting for this process's lock on the database.

        Returns:
            READY when all pass. A missing lock holder is reported as UNREACHABLE with ``details["lock_holder"]``
            False, so ``capability()`` can tell "Plex stopped" from "different file".
        """
        db = self._path()
        if not db or not os.path.isfile(db):
            return CapabilityReport(
                Capability.MISCONFIGURED, f"Plex database not found at {db or '(Plex config folder not set)'}"
            )
        # realpath: a symlinked config folder or DB file must be judged by where SQLite actually locks it.
        fs_type = filesystem_type(os.path.realpath(db), mountinfo_path=self._mountinfo_path)
        details: dict = {"db_path": db, "fs_type": fs_type}
        if is_network_filesystem(fs_type):
            return CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                f"Plex's database is on a network share ({fs_type}). The app must run on the same machine as Plex "
                "to write markers; Plex stays read-only.",
                details,
            )
        if not is_local_filesystem(fs_type):
            return CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                f"Plex's database is on a filesystem this app doesn't recognise as a local disk ({fs_type or 'unknown'}); "
                "Plex stays read-only.",
                details,
            )
        unwritable = self._unwritable_paths(db)
        if unwritable:
            owner = "unknown"
            with contextlib.suppress(OSError):
                owner = str(os.stat(db).st_uid)
            # The facts as well as the sentence: with an agent this ran on Plex's machine, and ``capability()``
            # rebuilds the sentence around the agent rather than around this app (spec §6.3).
            details.update({"unwritable": list(unwritable), "writer_uid": str(os.geteuid()), "db_owner_uid": owner})
            return CapabilityReport(
                Capability.MISCONFIGURED,
                f"This app (user {os.geteuid()}) can't write {', '.join(unwritable)}. Run it with the user that owns "
                f"Plex's database (user {owner}).",
                details,
            )
        try:
            held = shm_lock_held_elsewhere(db, deadline=deadline)
        except PublishError as exc:
            return report_of_failure(exc, Capability.UNREACHABLE, details)
        details["lock_holder"] = held
        if not held:
            return CapabilityReport(
                Capability.UNREACHABLE,
                "Plex doesn't have its database open through this folder right now (Plex is stopped, or this app "
                "sees a different path to the file). Markers are only written while Plex is running.",
                details,
            )
        return CapabilityReport(Capability.READY, "", details)

    def db_checks(self, *, deadline: float) -> CapabilityReport:
        """The schema, the marker data this app knows how to read, and Plex's single marker tag row.

        Args:
            deadline: When to stop waiting for the database locks.

        Returns:
            READY, or the problem's own capability state and message (never raises).
        """
        try:
            with self._database(read_only=True, deadline=deadline) as conn:
                self._check_schema(conn)
                self._check_library_marker_versions(conn)
                self._marker_tag_id(conn)
        except PublishError as exc:
            return report_of_failure(exc, Capability.MISCONFIGURED)
        except sqlite3.Error as exc:
            return report_of_failure(publish_error_from_sqlite(exc), Capability.MISCONFIGURED)
        return CapabilityReport(Capability.READY, "")

    def read_item(self, rating_key: int, *, deadline: float) -> ItemRead:
        """One item's live parts, with the schema and the marker tag row checked on the same connection.

        Args:
            rating_key: Plex's metadata item id.
            deadline: When to stop waiting for the database locks.

        Returns:
            Whether the database knows the item, its live parts, and which types' rows were made for an earlier
            file at this path.

        Raises:
            PublishError: The database couldn't be read, or isn't the tested schema.
        """
        try:
            with self._database(read_only=True, deadline=deadline) as conn:
                self._check_schema(conn)
                tag_id = self._marker_tag_id(conn)
                parts = self._item_parts(conn, rating_key)
                stale = _types_not_made_for_file(self._item_rows(conn, rating_key, tag_id), parts)
                return ItemRead(bool(parts) or self._item_exists(conn, rating_key), parts, stale)
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc

    def write_item(self, request: WriteRequest, *, deadline: float) -> WriteResult:
        """Plan the item on a read-only snapshot first, and take Plex's write lock only for a real change.

        Args:
            request: The item, the parts its markers were decided for, and those markers.
            deadline: When to stop waiting for the database locks.

        Returns:
            Whether the database changed, and what the item shows as ours.

        Raises:
            PublishError: Nothing was written (one transaction).
        """
        calling = set(request.calling_part_ids)
        try:
            with self._database(read_only=True, deadline=deadline) as conn:
                snapshot = self._item_parts(conn, request.rating_key)
                same_files = _same_files(snapshot, request.parts)
                if same_files:
                    tag_id = self._marker_tag_id(conn)
                    plan = self._plan(
                        conn,
                        request.rating_key,
                        tag_id,
                        snapshot,
                        request.wanted,
                        request.prior,
                        request.duration_ms,
                        request.own_prior,
                        calling,
                        request.kept_types,
                        request.keep_plex,
                        limits=request.limits,
                    )
            if same_files and _nothing_to_write(plan):
                changed = False
            else:
                with self._database(read_only=False, deadline=deadline) as conn:
                    changed, plan = self._write_item(
                        conn,
                        deadline,
                        request.rating_key,
                        request.parts,
                        request.wanted,
                        request.prior,
                        request.duration_ms,
                        request.own_prior,
                        calling,
                        request.kept_types,
                        request.keep_plex,
                        limits=request.limits,
                    )
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc
        return WriteResult(changed, plan.ours, plan.kept_types, plan.replaced_own, plan.replaced_stale)

    def shown_many(
        self, items: list[ShownAsk], *, timeout_s: float, cancel_check: Callable[[], bool] | None = None
    ) -> ShownBatch:
        """Read items back one read-only connection each (the schema checked on the first), the lock free for a
        moment between them, so a write from another job waiting for it gets it.

        Args:
            items: What to ask about each item.
            timeout_s: The longest one item's read waits for the database locks.
            cancel_check: True once the job is cancelled; checked before each item.

        Returns:
            One answer per item read, in order (``shown`` None for an item whose own read failed), and whether the
            database stopped being readable at all.
        """
        answers: list[ShownAnswer] = []
        tag_id: int | None = None
        opened = False
        for position, ask in enumerate(items):
            if cancel_check and cancel_check():
                break
            if opened:
                time.sleep(READ_BACK_PAUSE_S)  # the lock was just released: a waiting write takes it now
            opened = True
            try:
                with self._database(read_only=True, deadline=time.monotonic() + timeout_s) as conn:
                    if tag_id is None:
                        self._check_schema(conn)
                        tag_id = self._marker_tag_id(conn)
                    shown, parts = self._shown_in(
                        conn, tag_id, ask.rating_key, ask.ours, ask.kept_types, ask.item_files
                    )
                answers.append(ShownAnswer(shown, _version_files(parts)))
            except PublishError as exc:
                logger.debug("Plex {}: couldn't read {} item(s) back: {}", self._label, len(items) - position,
                             type(exc).__name__)  # fmt: skip
                return ShownBatch(answers, True)
            except Exception as exc:
                # One unreadable item (SQLite busy, a row with a NULL offset) mustn't stop the others.
                logger.debug("Plex {}: couldn't read item {} back: {}", self._label, ask.rating_key,
                             type(exc).__name__)  # fmt: skip
                answers.append(ShownAnswer(None, ()))
        return ShownBatch(answers, False)

    def item_exists(self, rating_key: int, *, deadline: float) -> bool:
        """Whether the database has this rating key, read like a read-back (lock proof, read-only).

        Args:
            rating_key: Plex's metadata item id.
            deadline: When to stop waiting for the database locks.

        Returns:
            True when the item is there.

        Raises:
            PublishError: The database couldn't be read.
        """
        try:
            with self._database(read_only=True, deadline=deadline) as conn:
                return self._item_exists(conn, rating_key)
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc

    def _plan(
        self,
        conn: sqlite3.Connection,
        rating_key: int,
        tag_id: int,
        parts: list[_Part],
        wanted: list[Marker],
        prior: list[Marker],
        duration_ms: int | None,
        own_prior: list[Marker],
        calling_part_ids: set[int],
        kept_before: frozenset[MarkerType],
        keep_plex: bool,
        *,
        limits: FileLimits | None = None,
    ) -> _Plan:
        rows = self._item_rows(conn, rating_key, tag_id)
        # A marker the user adjusted or locked wins over "Keep Plex's" (spec §5.5 rule 1, §14 2026-09-20), so a locked
        # type is written even where the setting would have left Plex's own rows in place.
        locked = frozenset(m.type for m in wanted if m.locked)
        stale = _types_not_made_for_file(rows, parts)
        would_keep = _kept_types(rows, wanted, prior, own_prior, kept_before, keep_plex, limits=limits, stale=stale)
        # What the setting would have kept were Plex's rows made for this file: those ours replace because they weren't.
        replaced_stale = (
            _kept_types(rows, wanted, prior, own_prior, kept_before, keep_plex, limits=limits) - would_keep - locked
            if stale
            else frozenset()
        )
        kept_types = would_keep - locked
        # Plex's rows of a kept type, and the pv: key it rebuilds them from, are left exactly as they are.
        wanted = [m for m in wanted if m.type not in kept_types]
        prior = [m for m in prior if m.type not in kept_types]
        wanted_types = {m.type for m in wanted}
        refresh_final = _refresh_final_types(parts, wanted, duration_ms, keep_plex)
        replaced: set[str] = set()
        for mtype in wanted_types:
            # Rows already serving exactly the wanted times stay as they are, unless only their final flag is stale.
            text = _TYPE_TEXT[mtype]
            current = _rows_served_with_final(rows, mtype)
            if [(start, end) for start, end, _final in current] != _served_of(wanted, mtype):
                replaced.add(text)
            elif mtype in refresh_final and current != _served_with_final(wanted, mtype, duration_ms):
                replaced.add(text)
        for mtype in {m.type for m in prior} - wanted_types:
            # A type we no longer show: its rows go only while they still serve exactly what we published. Compared
            # in served time (each row with its own pv:final), so a file whose duration changed since still matches.
            text = _TYPE_TEXT[mtype]
            current = sorted(
                _served_times(mtype, r.time_offset, r.end_time_offset, _row_is_final(r.extra_data))
                for r in rows
                if r.text == text
            )
            if current == _served_of(prior, mtype):
                replaced.add(text)
        ours = [(_TYPE_TEXT[m.type], *_stored_row(m, duration_ms)) for m in wanted if _TYPE_TEXT[m.type] in replaced]
        kept = [r for r in rows if r.text not in replaced]
        # Plex numbers all marker rows of an item by text, then start (every native row in the lab DB, PROOF P3).
        entries = [(r.text, r.time_offset, r.end_time_offset, r) for r in kept] + [(*o[:3], o) for o in ours]
        entries.sort(key=lambda e: (e[0], e[1], e[2]))
        inserts, reindex = [], []
        for index, (_text, _start, _end, entry) in enumerate(entries):
            if isinstance(entry, _TaggingRow):
                if entry.index != index:
                    reindex.append((index, entry.id))
            else:
                inserts.append((index, *entry))
        managed = wanted_types | {m.type for m in prior}
        # Types the calling file published to the item it belonged to before a merge or split, not desired here.
        own_types = {m.type for m in own_prior} - wanted_types
        part_updates = []
        for part in parts:
            extra = merge_part_extra_data(
                part.extra_data, wanted, managed, duration_ms, previous=prior, refresh_final=refresh_final
            )
            if own_types and part.id in calling_part_ids:
                extra = merge_part_extra_data(extra, [], own_types, duration_ms, previous=own_prior)
            if not _same_extra_data(extra, part.extra_data):
                part_updates.append((extra, part.id))
        return _Plan(
            replaced_texts=sorted(replaced),
            inserts=inserts,
            reindex=reindex,
            part_updates=part_updates,
            ours=wanted,
            kept_types=kept_types,
            # Only the types whose rows this write really rewrites: a locked type the setting would have kept whose
            # rows already serve the wanted times takes nothing off Plex, so the row mustn't say it did.
            replaced_own=frozenset(t for t in would_keep & locked if _TYPE_TEXT[t] in replaced),
            replaced_stale=frozenset(t for t in replaced_stale if _TYPE_TEXT[t] in replaced),
        )

    @staticmethod
    def _begin_write(conn: sqlite3.Connection, deadline: float) -> None:
        """``BEGIN IMMEDIATE``, with this process's database lock marked as waiting for another program's write lock
        meanwhile (``_DatabaseLock``).

        Our own connections take turns under that lock, so only another program (Plex, a tool writing to its database)
        can make this wait. SQLite's own busy wait is set to ``WAIT_SLICE_S`` at a time, asking between slices whether
        the job was cancelled; once the write lock is taken, statements get what is left of ``deadline`` again.

        Raises:
            DatabaseBusyError: That program kept its write lock past ``deadline`` (``plex_busy_error``).
            PublishError: The job was cancelled while waiting.
            sqlite3.Error: Any other failure.
        """
        lock = getattr(_thread_state, "holding", None)
        since = lock.start_waiting_for_plex() if lock is not None else time.monotonic()
        gave_up = True
        try:
            while True:
                remaining = deadline - time.monotonic()
                conn.execute(f"PRAGMA busy_timeout = {int(max(0.0, min(WAIT_SLICE_S, remaining)) * 1000)}")
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.Error as exc:
                    if not isinstance(publish_error_from_sqlite(exc), DatabaseBusyError):
                        gave_up = False
                        raise
                    if time.monotonic() >= deadline:
                        raise plex_busy_error(time.monotonic() - since) from exc
                    if wait_cancelled():
                        raise PublishError(WAIT_CANCELLED) from exc
            gave_up = False
            conn.execute(f"PRAGMA busy_timeout = {int(max(0.0, deadline - time.monotonic()) * 1000)}")
        finally:
            if lock is not None:
                lock.stop_waiting_for_plex(gave_up=gave_up)

    def _write_item(
        self,
        conn: sqlite3.Connection,
        deadline: float,
        rating_key: int,
        parts: list[_Part],
        wanted: list[Marker],
        prior: list[Marker],
        duration_ms: int | None,
        own_prior: list[Marker],
        calling_part_ids: set[int],
        kept_before: frozenset[MarkerType],
        keep_plex: bool,
        *,
        limits: FileLimits | None = None,
    ) -> tuple[bool, _Plan]:
        """Write one item in one transaction.

        Returns:
            Whether anything changed (False when everything already matched), and the plan made under the lock.
        """
        # BEGIN IMMEDIATE can wait the rest of the call's deadline — exactly while Plex is writing, often to this item.
        # Everything we merge is re-read after it, or Plex's fresh extra_data keys would be overwritten from a stale read.
        self._begin_write(conn, deadline)
        try:
            self._check_schema(conn)
            tag_id = self._marker_tag_id(conn)
            fresh = self._item_parts(conn, rating_key)
            if not _same_files(fresh, parts):
                raise PublishError("Plex changed this item's files while we were writing; trying again on the next run")
            plan = self._plan(
                conn,
                rating_key,
                tag_id,
                fresh,
                wanted,
                prior,
                duration_ms,
                own_prior,
                calling_part_ids,
                kept_before,
                keep_plex,
                limits=limits,
            )
            if _nothing_to_write(plan):
                conn.execute("ROLLBACK")
                return False, plan
            if plan.replaced_texts:
                placeholders = ",".join("?" * len(plan.replaced_texts))
                conn.execute(
                    f"DELETE FROM taggings WHERE metadata_item_id=? AND tag_id=? AND text IN ({placeholders})",  # noqa: S608
                    (rating_key, tag_id, *plan.replaced_texts),
                )
            created_at = int(time.time())
            for index, text, start, end, extra in plan.inserts:
                conn.execute(
                    "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, "
                    "thumb_url, created_at, extra_data) VALUES (?,?,?,?,?,?,'',?,?)",
                    (rating_key, tag_id, index, text, start, end, created_at, extra),
                )
            conn.executemany("UPDATE taggings SET [index]=? WHERE id=?", plan.reindex)
            conn.executemany("UPDATE media_parts SET extra_data=? WHERE id=?", plan.part_updates)
            conn.execute("COMMIT")
            return True, plan
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    def _shown_in(
        self,
        conn: sqlite3.Connection,
        tag_id: int,
        rating_key: int,
        ours: list[Marker],
        kept_types: frozenset[MarkerType],
        item_files: tuple[str, ...] | None,
    ) -> tuple[Shown, list[_Part]]:
        """One item's rows compared with ``ours`` on an open connection, and the item's live parts.

        ``ours`` is already projected to the types this publisher writes (``ShownAsk``).
        """
        if not self._item_exists(conn, rating_key):
            return Shown.GONE, []
        rows = conn.execute(
            "SELECT text, time_offset, end_time_offset, extra_data FROM taggings WHERE metadata_item_id=? AND tag_id=?",
            (rating_key, tag_id),
        ).fetchall()
        parts = self._item_parts(conn, rating_key)
        if item_files is not None and _version_files(parts) != tuple(item_files):
            return Shown.VERSIONS_CHANGED, parts
        served = {
            mtype: [
                _served_times(mtype, start, end, _row_is_final(extra))
                for text, start, end, extra in rows
                if text == name
            ]
            for mtype, name in _TYPE_TEXT.items()
        }
        if any(not served.get(mtype) for mtype in kept_types):
            return Shown.MISSING, parts
        return compare_shown(ours, served, others_alongside=False), parts


class PlexMarkerPublisher(MarkerPublisher):
    """Writes markers into one Plex server's database."""

    supported_types = frozenset({MarkerType.INTRO, MarkerType.CREDITS})
    name = "plex_db"
    # One SQLite transaction per write — but __init__ takes this from the database half: a write through the
    # Plex marker agent can lose its answer after that transaction committed.
    atomic_writes = True

    def __init__(
        self,
        server: PlexServer,
        config: ServerConfig,
        settings: ServerMarkersSettings,
        *,
        sibling_markers: Callable[[str], dict[MarkerType, Marker] | None] | None = None,
        settings_provider: Callable[[], ServerMarkersSettings] | None = None,
        mountinfo_path: str = "/proc/self/mountinfo",
        ui_details: bool = True,
        db_timeout_s: float | None = None,
        db: PlexDatabase | None = None,
    ) -> None:
        """Create the publisher.

        Args:
            server: Live ``PlexServer`` client.
            config: That server's ``ServerConfig``.
            settings: That server's ``ServerMarkersSettings``.
            sibling_markers: Looks up decided markers for another local file (multi-version items).
            settings_provider: Returns the saved settings right now; when given, every check (and so every write)
                reads the switch and the database-write confirmation from it instead of ``settings``.
            mountinfo_path: For tests.
            ui_details: Ask Plex for its own detection settings in ``capability()`` (the Edit dialog shows them); a job
                doesn't need them.
            db_timeout_s: The longest a single check or write waits for the database locks; None uses
                ``BUSY_TIMEOUT_S``. The Inspector's publish-now path shortens it, since a job's wait alone outlasts the
                deadline a web request may take (ruling P-R1).
            db: Where the database work runs. The default opens the file this process can see; a server with a Plex
                marker agent gets ``plex_remote.RemotePlexDb`` instead (``publishers.factory``).
        """
        self._server = server
        self._config = config
        self._settings = settings
        self._settings_provider = settings_provider
        self._sibling_markers = sibling_markers or (lambda _path: None)
        self._ui_details = ui_details
        self._db_timeout_s = db_timeout_s
        self._db = db or LocalPlexDb(self.db_path, label=config.name, mountinfo_path=mountinfo_path)
        # A write through the Plex marker agent can fail with its answer lost after the transaction committed, so
        # that publisher is not atomic and its record of what is ours is unknown after a failure.
        self.atomic_writes = self._db.atomic_writes
        self._live_files: dict[str, tuple[str, ...]] = {}

    def _db_timeout(self) -> float:
        """The longest one check or write waits for the database locks."""
        return BUSY_TIMEOUT_S if self._db_timeout_s is None else self._db_timeout_s

    def _db_deadline(self) -> float:
        """When one check or write stops waiting for the database locks (``time.monotonic()``)."""
        return time.monotonic() + self._db_timeout()

    @staticmethod
    def db_path_for(config: ServerConfig) -> str | None:
        """DB path from a server's config, or None when its Plex config folder isn't set.

        Args:
            config: The server's ``ServerConfig``.

        Returns:
            The library database's path as this app sees it.
        """
        folder = str((config.output or {}).get("plex_config_folder") or "").strip()
        return plex_db_path(folder) if folder else None

    def db_path(self) -> str | None:
        """DB path as this app sees it, or None when the Plex config folder isn't set.

        With a Plex marker agent the file is on Plex's machine and this path is not opened: the agent reports its own
        (``capability().details["db_path"]``).
        """
        return self.db_path_for(self._config)

    def _live_settings(self) -> ServerMarkersSettings:
        return self._settings_provider() if self._settings_provider is not None else self._settings

    def _local_checks(self, *, deadline: float) -> CapabilityReport:
        """Settings (read live when a ``settings_provider`` was given), then the database's own file checks.

        The file checks run where the database is: in this process, or on the Plex marker agent (``PlexDatabase``).
        Neither needs a Plex connection.

        Args:
            deadline: When to stop waiting for the lock on the database.

        Returns:
            READY when all pass. A missing lock holder is reported as UNREACHABLE with ``details["lock_holder"]``
            False, so ``capability()`` can tell "Plex stopped" from "different file".
        """
        settings = self._live_settings()
        if not settings.enabled:
            return CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server")
        if not settings.db_write_confirmed_at:
            return CapabilityReport(Capability.NEEDS_CONFIRMATION, "Confirm the Plex database write to turn this on")
        report = self._db.file_checks(deadline=deadline)
        return self._another_plexs_agent(report) or report

    def _another_plexs_agent(self, report: CapabilityReport) -> CapabilityReport | None:
        """Refuse an agent that is next to a different Plex than this server (a mistyped address).

        Both sides name Plex's own machine identifier — this app from the server it is connected to, the agent from
        the Preferences.xml beside the database it would write. Anything else would write one Plex's markers into
        another Plex's database. Silence isn't proof: when either side doesn't say, the write goes ahead.

        Args:
            report: What the database half answered.

        Returns:
            The refusal, with ``details["agent"]["wrong_plex"]`` set, or None when the two identifiers match or one
            of them is unknown. The flag is what the Setup Health row reads: an agent that answered and was refused
            anyway is otherwise indistinguishable from one whose answer simply couldn't be decoded, and telling the
            user to check a correct address would send them after the wrong thing.
        """
        agent = report.details.get("agent") or {}
        theirs, ours = str(agent.get("machine_identifier") or ""), str(self._config.server_identity or "")
        if not theirs or not ours or theirs == ours:
            return None
        return CapabilityReport(
            Capability.AGENT_UNAVAILABLE,
            f"The Plex marker agent at {agent.get('url') or 'this address'} is next to a different Plex server than "
            f"{self._config.name or 'this one'}. Check the address: markers would have gone into the wrong database.",
            {**report.details, "agent": {**agent, "wrong_plex": True}},
        )

    def _through_the_agent(self, local: CapabilityReport) -> CapabilityReport | None:
        """The database half's refusal, said about the agent's container instead of about this app.

        ``LocalPlexDb.file_checks`` runs wherever the database is, but its wording is written from this app's
        side ("The app must run on the same machine as Plex", "This app (user N) can't write ..."). Through an
        agent that side is the agent's container, so the untouched wording tells the user to move the app —
        the one thing the agent exists to avoid. Spec §6.3: with an agent the path, the filesystem check and
        the lock proof are the agent's.

        Args:
            local: What the database half answered.

        Returns:
            The same state and details with the agent's wording, or None when no agent ran the check (or its
            answer says nothing about where the app runs).
        """
        if not local.details.get("agent"):
            return None
        details = local.details
        if local.state is Capability.NEEDS_LOCAL_DB:
            fs_type = str(details.get("fs_type") or "")
            where = (
                f"Plex's database is on a network share ({fs_type}) where the Plex marker agent runs."
                if is_network_filesystem(fs_type)
                else "Plex's database is on a filesystem the Plex marker agent doesn't recognise as a local disk "
                f"({fs_type or 'unknown'})."
            )
            return CapabilityReport(
                local.state,
                f"{where} Mount Plex's config folder into the agent's container from a local disk of the Plex "
                "machine; Plex stays read-only.",
                details,
            )
        if local.state is Capability.MISCONFIGURED and details.get("unwritable"):
            paths = ", ".join(str(p) for p in details["unwritable"])
            return CapabilityReport(
                local.state,
                f"The Plex marker agent (user {details.get('writer_uid') or 'unknown'}) can't write {paths}. Run the "
                f"agent's container with the user that owns Plex's database "
                f"(user {details.get('db_owner_uid') or 'unknown'}).",
                details,
            )
        return None

    def capability(self) -> CapabilityReport:
        """Check settings, DB location and lock sharing, Plex Pass, schema and the marker tag row."""
        local = self._local_checks(deadline=self._db_deadline())
        # The agent's own problems (unreachable, wrong key, wrong version, the wrong Plex) are about the agent, not
        # about where a database file sits, so they are never reworded as the same-host advice below.
        if local.state is Capability.AGENT_UNAVAILABLE:
            return local
        through_agent = self._through_the_agent(local)
        if through_agent is not None:
            return through_agent
        if local.details.get("lock_holder") is False:
            status = self._server.get_server_status()
            if status is None:
                return local
            # With an agent the path is the one IT sees; this app sees no database at all.
            whose = "the agent sees" if local.details.get("agent") else "this app sees"
            return CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                f"Plex is running, but not with the database file {whose} at {local.details['db_path']}. "
                f"{SAME_HOST_PATH_ADVICE}",
                {**local.details, "plex_pass": status.get("plex_pass"), "plex_version": status.get("version")},
            )
        if not local.ready:
            return local
        details = dict(local.details)
        status = self._server.get_server_status()
        plex_pass = None if status is None else status.get("plex_pass")
        details["plex_pass"] = plex_pass
        details["plex_version"] = None if status is None else status.get("version")
        if plex_pass is False:
            return CapabilityReport(
                Capability.NEEDS_PASS, "This Plex server has no Plex Pass, so Plex won't show any markers.", details
            )
        details["detection"] = {"intro": None, "credits": None}
        if plex_pass is not None and self._ui_details:  # unreachable: don't wait on a second connection for UI details
            try:
                details["detection"] = self._server.get_marker_detection_prefs()
            except Exception as exc:
                logger.debug("Plex {}: detection prefs unavailable: {}", self._config.name, exc)
        # A fresh deadline: the Plex calls above don't eat into the time allowed for the database lock.
        inside = self._db.db_checks(deadline=self._db_deadline())
        details.update(inside.details)
        if not inside.ready:
            return CapabilityReport(inside.state, inside.message, details)
        return CapabilityReport(Capability.READY, "Written into this Plex server's database", details)

    def _local_candidates(self, plex_path: str) -> list[str]:
        # Imported here, not at module level: the server layer pulls in the whole app, and the database half of
        # this module (``LocalPlexDb``) runs in the Plex marker agent's small image, which has none of it.
        from ...servers.ownership import apply_path_mappings

        return apply_path_mappings(plex_path, list(self._config.path_mappings or [])) or [plex_path]

    def _sibling_decision(self, plex_file: str) -> dict[MarkerType, Marker] | None:
        # A multi-disk mapping yields several local candidates; only the one that exists has a decision.
        for local in self._local_candidates(plex_file):
            try:
                decided = self._sibling_markers(local)
            except Exception as exc:
                # markers.db's errors are ours, not Plex's: no capability state, so Plex's status isn't touched.
                raise PublishError(
                    f"Couldn't look up the markers decided for another version of this item: {exc}"
                ) from exc
            if decided is not None:
                return decided
        return None

    def _local_candidates_and_roots(self, plex_path: str) -> dict[str, tuple[str, ...]]:
        """Each local candidate of a Plex path with the disk roots it lies under: the path mapping's local folder and
        the folder of each library holding it (``gone_from_disk``'s ``roots``)."""
        from ...servers.ownership import path_mapping_candidates

        mappings = list(self._config.path_mappings or [])
        library_roots = [
            local
            for library in self._config.libraries
            for remote in library.remote_paths
            for local, _root in path_mapping_candidates(remote, mappings)
        ]
        candidates: dict[str, tuple[str, ...]] = {}
        for local, mapping_root in path_mapping_candidates(plex_path, mappings):
            roots = [root.rstrip("/") or "/" for root in library_roots if _lies_under(local, root)]
            candidates[local] = tuple(dict.fromkeys([*([mapping_root] if mapping_root else []), *roots]))
        return candidates

    def _gone_from_disk(self, plex_file: str) -> bool:
        """Whether a version's file is on none of the disks the server's path mappings give for it (``gone_from_disk``:
        a disk that isn't mounted, or whose mount went stale, never makes it look gone).

        Asked while the item's lock is held, so a disk that doesn't answer within ``GONE_CHECK_TIMEOUT_S`` (a stalled
        network share blocks a stat rather than fail) counts as "not gone" instead of holding every publish to the item.
        """
        candidates = self._local_candidates_and_roots(plex_file)
        answer: list[bool] = []
        check = threading.Thread(
            target=lambda: answer.append(gone_from_disk(candidates, roots=candidates)),
            name="plex-version-on-disk",
            daemon=True,
        )
        check.start()
        check.join(GONE_CHECK_TIMEOUT_S)
        if not answer:
            logger.warning(
                "Plex {}: couldn't tell within {:.0f} s whether {} is still on disk; waiting for it",
                self._config.name,
                GONE_CHECK_TIMEOUT_S,
                os.path.basename(plex_file),
            )
            return False
        return answer[0]

    def _desired(
        self, parts: list[_Part], markers: list[Marker], canonical_path: str, prior: list[Marker]
    ) -> list[Marker]:
        """The item's marker set: the markers of every type all versions decided alike.

        Plex serves one marker set per item, across all its versions. A type is desired only when every version is
        decided, has that type and agrees within ``VERSION_AGREEMENT_MS`` (spec §6.3). Which version's times are
        written -- the calling file's, or what this app already left on the item -- is
        :func:`~.base.agreed_across_versions`'s rule, including the exception a locked type makes; it isn't restated
        here. A version never decided whose file is gone from disk (Plex lists a deleted file until its next scan)
        takes no part; one still on disk is waited for, and sets ``last_unchecked_versions``.

        Raises:
            PublishError: Stacked multi-part files.
            ItemNotFoundError: None of the item's versions is our file.
        """
        media_items = [p.media_item_id for p in parts]
        if len(set(media_items)) != len(media_items):
            raise PublishError("Plex item uses stacked multi-part files; markers for those are not supported")
        # Plex's optimized copies are transcodes we never decide: they share the item's markers but take no part in
        # the agreement.
        versions = [p for p in parts if not _is_optimized_copy(p)]
        others = [p for p in versions if canonical_path not in self._local_candidates(p.file)]
        if len(others) == len(versions):
            # The markers were decided for a file this item doesn't hold: never write them onto another file.
            raise ItemNotFoundError(
                "This Plex item has no file matching this path (yet); check the server's path mappings"
            )
        # None = never decided, so no type is desired yet. {} = decided with no markers.
        decisions = []
        for part in others:
            decided = self._sibling_decision(part.file)
            # A deleted file has no decision (markers_for_path), and waiting for one would hold the item back forever.
            if decided is None and self._gone_from_disk(part.file):
                continue
            decisions.append(decided)
        self.last_unchecked_versions = any(decided is None for decided in decisions)
        return self.project(agreed_across_versions(markers, decisions, prior, (MarkerType.INTRO, MarkerType.CREDITS)))

    def _local_files(self, version_files: tuple[str, ...]) -> tuple[str, ...]:
        """Every local candidate of every version file, sorted (see ``live_files``)."""
        return tuple(sorted({local for remote in version_files for local in self._local_candidates(remote)}))

    def write(
        self,
        item_id: str,
        markers: list[Marker],
        *,
        previous: list[Marker] | None,
        duration_ms: int | None,
        canonical_path: str,
        own_previous: list[Marker] | None = None,
        kept_types: frozenset[MarkerType] = frozenset(),
        limits: FileLimits | None = None,
    ) -> list[Marker]:
        """Make this item show its desired marker set (see ``_desired``) in ``taggings`` and every part's ``extra_data``.

        What to write is decided here; the database work runs where the file is (``PlexDatabase``).

        Types of ours that are no longer desired are removed only where they still serve exactly ``previous``; Plex's
        own rows of those types stay. A desired type replaces whatever rows the item has of that type, except a type
        kept as Plex's own while the server is set to "Keep Plex's" (see ``_kept_types``; never one whose every row of
        Plex's can't be right for the file's ``limits``) — a type the user locked is written even then, and reported in
        ``last_replaced_own_types`` (spec §5.5 rule 1). Rows and keys that already
        serve the desired times stay, unless only their credits ``final`` flag is stale (see ``_refresh_final_types``).

        Returns:
            The desired set now on the item, kept types left out (see ``MarkerPublisher.write``).

        Raises:
            PublishError: Nothing was written (one transaction; see ``MarkerPublisher.write``).
        """
        self.last_write_changed = False
        self.last_replaced_own_types = frozenset()
        self.last_replaced_stale_types = frozenset()
        self.last_item_files = None
        self.last_unchecked_versions = False
        keep_plex = self._live_settings().on_plex_redetect == "keep_plex"
        kept_before = frozenset(kept_types)
        # Until the item's rows are read, what was kept stays kept (or is released by the setting).
        self.last_kept_types = kept_before if keep_plex else frozenset()
        wanted = self.project(markers)
        # previous=None: nothing on the item is provably ours, so nothing is removed.
        prior = self.project(previous) if previous is not None else []
        own_prior = self.project(own_previous or [])
        # A kept type is read again even with nothing to write: it is released once Plex has no rows of it.
        if not wanted and not prior and not own_prior and not self.last_kept_types:
            return []
        rating_key = _rating_key(item_id)
        deadline = self._db_deadline()
        # capability() is cached per job; these checks (lock probe included) guard every write on their own.
        local = self._local_checks(deadline=deadline)
        if not local.ready:
            # A lock probe that gave up on a busy database stays one, so the job retries the file.
            failure = DatabaseBusyError if local.details.get("db_busy") else PublishError
            raise failure(local.message, state=local.state)
        item = self._db.read_item(rating_key, deadline=deadline)
        if not item.exists:
            raise ItemNotFoundError(f"Plex item {rating_key} not found in the database")
        if not item.parts:
            # Every version is deleted or in Plex's trash: nothing to show markers on until Plex finds a file again.
            raise ItemNotFoundError(
                f"Plex has no live files for this item ({rating_key}): they're deleted or in Plex's trash"
            )
        self.last_item_files = _version_files(item.parts)
        # With no Plex connection or lock held: the sibling lookups take markers.db's lock.
        desired = self._desired(item.parts, wanted, canonical_path, prior)
        if not desired and not prior and not own_prior and not self.last_kept_types:
            return []
        calling = tuple(sorted(p.id for p in item.parts if canonical_path in self._local_candidates(p.file)))
        result = self._db.write_item(
            WriteRequest(
                rating_key=rating_key,
                parts=item.parts,
                wanted=desired,
                prior=prior,
                duration_ms=duration_ms,
                own_prior=own_prior,
                calling_part_ids=calling,
                kept_types=kept_before,
                keep_plex=keep_plex,
                limits=limits,
            ),
            deadline=deadline,
        )
        if result.replaced_stale:
            logger.info(
                "Plex {}: item {} showed Plex's own {}, detected for an earlier file at its path; replaced with ours",
                self._config.name,
                rating_key,
                " and ".join(t.value for t in MarkerType if t in result.replaced_stale),
            )
        self.last_replaced_stale_types = result.replaced_stale
        newly_kept = result.kept_types - kept_before
        if newly_kept:
            logger.info(
                "Plex {}: item {} shows Plex's own {} instead of ours; keeping them (Keep Plex's)",
                self._config.name,
                rating_key,
                " and ".join(t.value for t in MarkerType if t in newly_kept),
            )
        if result.replaced_own:
            logger.info(
                "Plex {}: item {} showed Plex's own {}; this server keeps Plex's, but the user locked them, so "
                "they were replaced",
                self._config.name,
                rating_key,
                " and ".join(t.value for t in MarkerType if t in result.replaced_own),
            )
        if result.changed:
            logger.info(
                "Plex {}: item {} now shows {} marker(s) of ours", self._config.name, rating_key, len(result.ours)
            )
        self.last_write_changed = result.changed
        self.last_kept_types = result.kept_types
        self.last_replaced_own_types = result.replaced_own
        return result.ours

    def types_not_made_for_file(self, item_id: str) -> frozenset[MarkerType] | None:
        """The types whose markers on this item were made for an earlier file at its path.

        See ``_types_not_made_for_file`` for the rule.

        Read from Plex's database with the same lock proof and read-only connection as a write, locally or through the
        Plex marker agent, waiting at most :data:`STALE_READ_WAIT_S` for its locks (and less once the job is cancelled,
        ``base.cancellable_waits``).

        Args:
            item_id: Plex rating key.

        Returns:
            Those types; None when the database can't be read here or an agent older than this answer can't tell.
        """
        try:
            rating_key = _rating_key(item_id)
            deadline = time.monotonic() + STALE_READ_WAIT_S
            if not self._local_checks(deadline=deadline).ready:
                return None
            stale = self._db.read_item(rating_key, deadline=deadline).stale_types
            # Only an agent older than the answer reads the item without it.
            self.stale_types_unanswerable = stale is None
            return stale
        except PublishError as exc:
            logger.debug(
                "Plex {}: couldn't tell whether item {}'s markers are stale: {}", self._config.name, item_id, exc
            )
            return None

    def shows(
        self,
        item_id: str,
        ours: list[Marker],
        *,
        kept_types: frozenset[MarkerType] = frozenset(),
        item_files: tuple[str, ...] | None = None,
    ) -> Shown | None:
        """Read the item's marker rows from Plex's database (the same lock proof and read-only connection as a write).

        Args:
            item_id: Plex rating key.
            ours: What this app last left on the item.
            kept_types: Types kept as Plex's own; one without any rows left is MISSING (ours may go back).
            item_files: The item's version files at the last write (``last_item_files``); a different set now is
                VERSIONS_CHANGED, whatever the rows show. None: not compared.

        Returns:
            How Plex's rows of our types compare with ``ours``: GONE when the database has no item with that rating
            key (deleted, or merged into another item); None when the database couldn't be read.
        """
        return self.shows_many([(item_id, list(ours), frozenset(kept_types), item_files)]).get(item_id)

    def item_missing(self, item_id: str) -> bool | None:
        """Whether Plex's database has no item with this rating key, read like ``shows`` (lock proof, read-only).

        Args:
            item_id: Plex rating key.

        Returns:
            True when there is no such item, False when there is, None when the database couldn't be read (or the id
            isn't a rating key).
        """
        try:
            rating_key = _rating_key(item_id)
        except ItemNotFoundError:
            return None
        if not self._local_checks(deadline=self._db_deadline()).ready:
            return None
        try:
            return not self._db.item_exists(rating_key, deadline=self._db_deadline())
        except Exception as exc:
            logger.debug("Plex {}: couldn't look item {} up: {}", self._config.name, item_id, type(exc).__name__)
            return None

    def live_files(self, item_id: str) -> tuple[str, ...]:
        """The item's versions (optimized copies left out) at its last ``shows_many`` read, mapped to local paths.

        Args:
            item_id: Plex rating key.

        Returns:
            Every local candidate of every version's part file, sorted; empty when the item wasn't read or is gone.
        """
        return self._live_files.get(item_id, ())

    def shows_many(
        self, items: list[ReadBackItem], *, cancel_check: Callable[[], bool] | None = None
    ) -> dict[str, Shown | None]:
        """``shows`` for many items with a single read-back's lock footprint: the settings, database files and lock proof
        once, then one read-only connection per item (the schema checked on the first), the lock free for a moment
        between them.

        Args:
            items: ``(item_id, ours, kept_types, item_files)`` per item.
            cancel_check: True once the job is cancelled; checked before each item.

        Returns:
            Per item id read: how its rows compare. None for an id that isn't a Plex rating key and for an item whose
            read failed (a row this code can't read, SQLite busy); every item left is None once the database can't be
            shared or read at all (not local, Plex stopped, schema changed, the lock busy past its deadline). An id
            that isn't a rating key is answered before the read starts, so a cancelled call still reports it.
        """
        out: dict[str, Shown | None] = {}
        self._live_files = {}
        if not items:
            return out
        local = self._local_checks(deadline=self._db_deadline())
        if not local.ready:
            logger.debug("Plex {}: couldn't read {} item(s) back: {}", self._config.name, len(items), local.state.value)
            return {item_id: None for item_id, *_ in items}
        asked: list[str] = []
        asks: list[ShownAsk] = []
        for item_id, ours, kept_types, item_files in items:
            try:
                rating_key = _rating_key(item_id)
            except ItemNotFoundError:
                out[item_id] = None
                continue
            asked.append(item_id)
            asks.append(ShownAsk(rating_key, self.project(ours), frozenset(kept_types), item_files))
        batch = self._db.shown_many(asks, timeout_s=self._db_timeout(), cancel_check=cancel_check)
        for item_id, answer in zip(asked, batch.answers, strict=False):
            out[item_id] = answer.shown
            if answer.shown is not None:
                # Only a read that happened records the item's files: an id asked twice, whose second read failed,
                # keeps what the first one found (``live_files``).
                self._live_files[item_id] = self._local_files(answer.version_files)
        if batch.unreadable:
            out.update({item_id: None for item_id in asked[len(batch.answers) :]})
        return out
