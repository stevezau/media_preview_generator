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
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger

from ...servers.ownership import apply_path_mappings
from ..fs import filesystem_type, is_local_filesystem, is_network_filesystem
from ..models import Marker, MarkerType
from .base import (
    Capability,
    CapabilityReport,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
    ReadBackItem,
    Shown,
    agreed_across_versions,
    compare_shown,
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
# The longest one write() waits for locks in total (this process's lock on the database, then Plex's write
# lock) before giving up until the next run. capability() allows this twice: once for the lock probe, once for its
# read-only checks after the Plex calls.
BUSY_TIMEOUT_S = 30.0
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
    "media_parts": {"id", "media_item_id", "file", "extra_data", "deleted_at"},
    "media_items": {"id", "metadata_item_id", "deleted_at", "proxy_type"},
    "metadata_items": {"id"},
}
# SQLite primary result codes (sqlite3.h): BUSY, LOCKED, PROTOCOL (a transient WAL race) / PERM, IOERR,
# READONLY, CANTOPEN, AUTH, NOTADB / FULL / CORRUPT.
_SQLITE_BUSY_CODES = frozenset({5, 6, 15})
_SQLITE_ACCESS_CODES = frozenset({3, 8, 10, 14, 23, 26})
_SQLITE_FULL = 13
_SQLITE_CORRUPT = 11

_path_locks: dict[object, threading.Lock] = {}
_path_locks_guard = threading.Lock()
# Per thread: lock keys of databases this thread has a connection open to (see shm_lock_held_elsewhere).
_thread_state = threading.local()


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


def _db_lock(db_path: str) -> threading.Lock:
    """This process's lock for one database file.

    Closing any descriptor on a file drops every POSIX lock the process holds on it, including the locks of our own
    open SQLite connection. The lock probe opens and closes ``<db>-shm``, so it and every connection to the database
    hold this lock for their whole life.
    """
    key = _lock_key(db_path)
    with _path_locks_guard:
        return _path_locks.setdefault(key, threading.Lock())


@contextlib.contextmanager
def _holding_db_lock(db_path: str, deadline: float) -> Iterator[None]:
    lock = _db_lock(db_path)
    if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise PublishError(
            "Another Intro & Credits task is still using this Plex database; trying again on the next run",
            state=Capability.UNREACHABLE,
        )
    try:
        yield
    finally:
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
        UNREACHABLE for busy/locked, MISCONFIGURED for files we can't open or write, UNSUPPORTED_SCHEMA otherwise.
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
        return PublishError(
            f"Plex is busy writing its database; trying again on the next run ({text})", state=Capability.UNREACHABLE
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


def _version_files(parts: list[_Part]) -> tuple[str, ...]:
    """The item's versions as its sorted part files, Plex's optimized copies left out (they take no part in agreement)."""
    return tuple(sorted(p.file for p in parts if not _is_optimized_copy(p)))


def _same_files(a: list[_Part], b: list[_Part]) -> bool:
    """The same parts, files and versions (extra_data aside)."""
    return [p._replace(extra_data=None) for p in a] == [p._replace(extra_data=None) for p in b]


class _TaggingRow(NamedTuple):
    id: int
    index: int
    text: str
    time_offset: int
    end_time_offset: int
    thumb_url: str | None
    extra_data: str | None


class _Plan(NamedTuple):
    """What one write changes; empty lists mean nothing to do."""

    replaced_texts: list[str]
    inserts: list[tuple[int, str, int, int, str]]  # (index, text, time_offset, end_time_offset, extra_data)
    reindex: list[tuple[int, int]]  # (index, taggings id)
    part_updates: list[tuple[str, int]]  # (extra_data, media_parts id)
    ours: list[Marker]  # the desired markers the item shows as ours after the write (kept types left out)
    kept_types: frozenset[MarkerType]  # types whose rows are Plex's own and stay untouched

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
    on the other version's next run. Only with the file's duration known. Never under "Keep Plex's": rows serving the
    wanted times can be Plex's own even when our record lists those times (a write that changed nothing still records
    them), and those rows are never touched. Under "Use ours", Plex's rows with identical times get our flag.
    """
    if keep_plex or not duration_ms or duration_ms <= 0 or len(_version_files(parts)) != 1:
        return frozenset()
    return frozenset(m.type for m in wanted)


def _kept_types(
    rows: list[_TaggingRow],
    wanted: list[Marker],
    prior: list[Marker],
    own_prior: list[Marker],
    kept_before: frozenset[MarkerType],
    keep_plex: bool,
) -> frozenset[MarkerType]:
    """The types whose rows on the item are Plex's own and must stay ("Keep Plex's", ``on_plex_redetect``).

    A type this write would show becomes kept when Plex has rows of it that aren't provably ours: not what we'd write,
    not what we left there (``prior``), not what the calling file left on its item before a merge (``own_prior``).
    That covers Plex's own detection replacing ours, filling a type we removed, and markers already on an item we have
    no record of (a first publish, a reset markers.db, a re-added server): those can't be told from Plex's. Rows that
    already show what we'd write are treated as ours. A kept type stays kept, whatever gets written for the item,
    until Plex has no rows of it left or the server is set to restore ours.
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
        if mtype in kept_before or (would_show and current not in provably_ours):
            kept.add(mtype)
    return frozenset(kept)


class PlexMarkerPublisher(MarkerPublisher):
    """Writes markers into one Plex server's database."""

    supported_types = frozenset({MarkerType.INTRO, MarkerType.CREDITS})
    name = "plex_db"
    atomic_writes = True  # one SQLite transaction per write

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
        """
        self._server = server
        self._config = config
        self._settings = settings
        self._settings_provider = settings_provider
        self._sibling_markers = sibling_markers or (lambda _path: None)
        self._mountinfo_path = mountinfo_path
        self._ui_details = ui_details
        self._live_files: dict[str, tuple[str, ...]] = {}

    def db_path(self) -> str | None:
        """DB path, or None when the Plex config folder isn't set."""
        folder = str((self._config.output or {}).get("plex_config_folder") or "").strip()
        return plex_db_path(folder) if folder else None

    def _connect(self, *, read_only: bool, timeout: float) -> sqlite3.Connection:
        mode = "ro" if read_only else "rw"
        # Quoted: SQLite reads "?", "#" and "%" in a file: URI as query, fragment and escapes, which would open a
        # different path for folders named like "Plex #2". mode=rw never creates a missing file.
        uri = f"file:{urllib.parse.quote(os.path.abspath(self.db_path() or ''))}?mode={mode}"
        return sqlite3.connect(uri, uri=True, timeout=timeout, isolation_level=None)

    @contextlib.contextmanager
    def _database(self, *, read_only: bool, deadline: float) -> Iterator[sqlite3.Connection]:
        """A connection that lives entirely under the database's process lock (see ``_db_lock``).

        Raises:
            PublishError: UNREACHABLE when the lock isn't free before ``deadline``; MISCONFIGURED without a folder.
        """
        db = self.db_path()
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
        # Two LIKE scans of media_parts: run from capability() (cached per job, for a few seconds only while Plex Pass
        # doesn't answer), never per write. Each write still validates the parts it touches in merge_part_extra_data.
        # Newest rows first: a new format shows up there. Either form of extra_data (see encode_extra_data).
        for key in _PART_KEY.values():
            url_form_like = "%" + _plex_quote(key).replace("%", r"\%") + r"=\%7B%"  # pv%3Aintros=%7B...
            rows = conn.execute(
                "SELECT extra_data FROM media_parts WHERE extra_data LIKE ? OR extra_data LIKE ? ESCAPE '\\' "
                "ORDER BY id DESC LIMIT 50",
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
            "SELECT mp.id, mp.media_item_id, mp.file, mp.extra_data, mi.proxy_type FROM media_parts mp "
            "JOIN media_items mi ON mi.id = mp.media_item_id "
            "WHERE mi.metadata_item_id=? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL ORDER BY mp.id",
            (rating_key,),
        ).fetchall()
        return [_Part(*row) for row in rows]

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

    def _live_settings(self) -> ServerMarkersSettings:
        return self._settings_provider() if self._settings_provider is not None else self._settings

    def _local_checks(self, *, deadline: float) -> CapabilityReport:
        """Settings (read live when a ``settings_provider`` was given), database files and the shared lock: everything
        that needs no Plex connection.

        Args:
            deadline: When to stop waiting for this process's lock on the database.

        Returns:
            READY when all pass. A missing lock holder is reported as UNREACHABLE with ``details["lock_holder"]``
            False, so ``capability()`` can tell "Plex stopped" from "different file".
        """
        settings = self._live_settings()
        if not settings.enabled:
            return CapabilityReport(Capability.DISABLED, "Intro & Credits is off for this server")
        if not settings.db_write_confirmed_at:
            return CapabilityReport(Capability.NEEDS_CONFIRMATION, "Confirm the Plex database write to turn this on")
        db = self.db_path()
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
            return CapabilityReport(
                Capability.MISCONFIGURED,
                f"This app (user {os.geteuid()}) can't write {', '.join(unwritable)}. Run it with the user that owns "
                f"Plex's database (user {owner}).",
                details,
            )
        try:
            held = shm_lock_held_elsewhere(db, deadline=deadline)
        except PublishError as exc:
            return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc), details)
        details["lock_holder"] = held
        if not held:
            return CapabilityReport(
                Capability.UNREACHABLE,
                "Plex doesn't have its database open through this folder right now (Plex is stopped, or this app "
                "sees a different path to the file). Markers are only written while Plex is running.",
                details,
            )
        return CapabilityReport(Capability.READY, "", details)

    def capability(self) -> CapabilityReport:
        """Check settings, DB location and lock sharing, Plex Pass, schema and the marker tag row."""
        local = self._local_checks(deadline=time.monotonic() + BUSY_TIMEOUT_S)
        if local.details.get("lock_holder") is False:
            status = self._server.get_server_status()
            if status is None:
                return local
            return CapabilityReport(
                Capability.NEEDS_LOCAL_DB,
                f"Plex is running, but not with the database file this app sees at {local.details['db_path']}. "
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
        try:
            # A fresh deadline: the Plex calls above don't eat into the time allowed for the database lock.
            with self._database(read_only=True, deadline=time.monotonic() + BUSY_TIMEOUT_S) as conn:
                self._check_schema(conn)
                self._check_library_marker_versions(conn)
                self._marker_tag_id(conn)
        except PublishError as exc:
            return CapabilityReport(exc.state or Capability.MISCONFIGURED, str(exc), details)
        except sqlite3.Error as exc:
            error = publish_error_from_sqlite(exc)
            return CapabilityReport(error.state or Capability.MISCONFIGURED, str(error), details)
        return CapabilityReport(Capability.READY, "Written into this Plex server's database", details)

    def _local_candidates(self, plex_path: str) -> list[str]:
        return apply_path_mappings(plex_path, list(self._config.path_mappings or [])) or [plex_path]

    def _local_version_files(self, parts: list[_Part]) -> tuple[str, ...]:
        return tuple(
            sorted({local for p in parts if not _is_optimized_copy(p) for local in self._local_candidates(p.file)})
        )

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

    def _desired(
        self, parts: list[_Part], markers: list[Marker], canonical_path: str, prior: list[Marker]
    ) -> list[Marker]:
        """The item's marker set: the markers of every type all versions decided alike.

        Plex serves one marker set per item, across all its versions. A type is desired only when every version is
        decided, has that type and agrees within ``VERSION_AGREEMENT_MS`` (spec §6.3). The times written are the
        calling file's, unless what this app already left on the item (``prior``) agrees with every version too:
        then that stays, so versions whose times differ slightly don't rewrite each other's markers on every run.

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
        decisions = [self._sibling_decision(p.file) for p in others]
        return self.project(agreed_across_versions(markers, decisions, prior, (MarkerType.INTRO, MarkerType.CREDITS)))

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
    ) -> _Plan:
        rows = [
            _TaggingRow(*row)
            for row in conn.execute(
                "SELECT id, [index], text, time_offset, end_time_offset, thumb_url, extra_data FROM taggings "
                "WHERE metadata_item_id=? AND tag_id=? ORDER BY [index], id",
                (rating_key, tag_id),
            )
        ]
        kept_types = _kept_types(rows, wanted, prior, own_prior, kept_before, keep_plex)
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
        )

    def _write_item(
        self,
        conn: sqlite3.Connection,
        rating_key: int,
        parts: list[_Part],
        wanted: list[Marker],
        prior: list[Marker],
        duration_ms: int | None,
        own_prior: list[Marker],
        calling_part_ids: set[int],
        kept_before: frozenset[MarkerType],
        keep_plex: bool,
    ) -> tuple[bool, _Plan]:
        """Write one item in one transaction.

        Returns:
            Whether anything changed (False when everything already matched), and the plan made under the lock.
        """
        # BEGIN IMMEDIATE can wait the rest of the call's deadline — exactly while Plex is writing, often to this item.
        # Everything we merge is re-read after it, or Plex's fresh extra_data keys would be overwritten from a stale read.
        conn.execute("BEGIN IMMEDIATE")
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
    ) -> list[Marker]:
        """Make this item show its desired marker set (see ``_desired``) in ``taggings`` and every part's ``extra_data``.

        Types of ours that are no longer desired are removed only where they still serve exactly ``previous``; Plex's
        own rows of those types stay. A desired type replaces whatever rows the item has of that type, except a type
        kept as Plex's own while the server is set to "Keep Plex's" (see ``_kept_types``). Rows and keys that already
        serve the desired times stay, unless only their credits ``final`` flag is stale (see ``_refresh_final_types``).

        Returns:
            The desired set now on the item, kept types left out (see ``MarkerPublisher.write``).

        Raises:
            PublishError: Nothing was written (one transaction; see ``MarkerPublisher.write``).
        """
        self.last_write_changed = False
        self.last_item_files = None
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
        deadline = time.monotonic() + BUSY_TIMEOUT_S
        # capability() is cached per job; these checks (lock probe included) guard every write on their own.
        local = self._local_checks(deadline=deadline)
        if not local.ready:
            raise PublishError(local.message, state=local.state)
        try:
            with self._database(read_only=True, deadline=deadline) as conn:
                self._check_schema(conn)
                self._marker_tag_id(conn)
                parts = self._item_parts(conn, rating_key)
                known = bool(parts) or self._item_exists(conn, rating_key)
            if not known:
                raise ItemNotFoundError(f"Plex item {rating_key} not found in the database")
            if not parts:
                # Every version is deleted or in Plex's trash: nothing to show markers on until Plex finds a file again.
                raise ItemNotFoundError(
                    f"Plex has no live files for this item ({rating_key}): they're deleted or in Plex's trash"
                )
            self.last_item_files = _version_files(parts)
            # With no Plex connection or lock held: the sibling lookups take markers.db's lock.
            desired = self._desired(parts, wanted, canonical_path, prior)
            if not desired and not prior and not own_prior and not self.last_kept_types:
                return []
            calling = {p.id for p in parts if canonical_path in self._local_candidates(p.file)}
            # Plan on a read-only snapshot first: a write that changes nothing never takes Plex's write lock. A real
            # change re-reads and re-plans under that lock in _write_item.
            with self._database(read_only=True, deadline=deadline) as conn:
                snapshot = self._item_parts(conn, rating_key)
                same_files = _same_files(snapshot, parts)
                if same_files:
                    tag_id = self._marker_tag_id(conn)
                    plan = self._plan(
                        conn,
                        rating_key,
                        tag_id,
                        snapshot,
                        desired,
                        prior,
                        duration_ms,
                        own_prior,
                        calling,
                        kept_before,
                        keep_plex,
                    )
            if same_files and _nothing_to_write(plan):
                changed = False
            else:
                with self._database(read_only=False, deadline=deadline) as conn:
                    changed, plan = self._write_item(
                        conn, rating_key, parts, desired, prior, duration_ms, own_prior, calling, kept_before, keep_plex
                    )
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc
        newly_kept = plan.kept_types - kept_before
        if newly_kept:
            logger.info(
                "Plex {}: item {} shows Plex's own {} instead of ours; keeping them (Keep Plex's)",
                self._config.name,
                rating_key,
                " and ".join(t.value for t in MarkerType if t in newly_kept),
            )
        if changed:
            logger.info(
                "Plex {}: item {} now shows {} marker(s) of ours", self._config.name, rating_key, len(plan.ours)
            )
        self.last_write_changed = changed
        self.last_kept_types = plan.kept_types
        return plan.ours

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
        if not self._local_checks(deadline=time.monotonic() + BUSY_TIMEOUT_S).ready:
            return None
        try:
            with self._database(read_only=True, deadline=time.monotonic() + BUSY_TIMEOUT_S) as conn:
                return not self._item_exists(conn, rating_key)
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
            shared or read at all (not local, Plex stopped, schema changed, the lock busy past its deadline).
        """
        out: dict[str, Shown | None] = {}
        self._live_files = {}
        if not items:
            return out
        local = self._local_checks(deadline=time.monotonic() + BUSY_TIMEOUT_S)
        if not local.ready:
            logger.debug("Plex {}: couldn't read {} item(s) back: {}", self._config.name, len(items), local.state.value)
            return {item_id: None for item_id, *_ in items}
        tag_id: int | None = None
        opened = False
        for position, (item_id, ours, kept_types, item_files) in enumerate(items):
            if cancel_check and cancel_check():
                break
            try:
                rating_key = _rating_key(item_id)
            except ItemNotFoundError:
                out[item_id] = None
                continue
            if opened:
                time.sleep(READ_BACK_PAUSE_S)  # the lock was just released: a waiting write takes it now
            opened = True
            try:
                with self._database(read_only=True, deadline=time.monotonic() + BUSY_TIMEOUT_S) as conn:
                    if tag_id is None:
                        self._check_schema(conn)
                        tag_id = self._marker_tag_id(conn)
                    shown, parts = self._shown_in(conn, tag_id, rating_key, ours, kept_types, item_files)
                out[item_id] = shown
                self._live_files[item_id] = self._local_version_files(parts)
            except PublishError as exc:
                logger.debug("Plex {}: couldn't read {} item(s) back: {}", self._config.name, len(items) - position,
                             type(exc).__name__)  # fmt: skip
                out.update({rest_id: None for rest_id, *_ in items[position:]})
                break
            except Exception as exc:
                # One unreadable item (SQLite busy, a row with a NULL offset) mustn't stop the others.
                logger.debug("Plex {}: couldn't read item {} back: {}", self._config.name, item_id, type(exc).__name__)
                out[item_id] = None
        return out

    def _shown_in(
        self,
        conn: sqlite3.Connection,
        tag_id: int,
        rating_key: int,
        ours: list[Marker],
        kept_types: frozenset[MarkerType],
        item_files: tuple[str, ...] | None,
    ) -> tuple[Shown, list[_Part]]:
        """One item's rows compared with ``ours`` (see ``shows``) on an open connection, and the item's live parts."""
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
        return compare_shown(self.project(ours), served, others_alongside=False), parts
