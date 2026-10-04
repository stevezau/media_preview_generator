"""Register complete external chapter images using Plex's existing chapter rows.

The local writer and Plex-side agent use the same guarded transaction. No chapters
are created: chapterless, unindexed, multipart and multi-version items stay distinct.
"""

from __future__ import annotations

import hashlib
import io
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from PIL import Image

from ..settings import load_server
from .base import Capability, CapabilityReport, PublishError
from .plex_db import LocalPlexDb, plex_db_path, publish_error_from_sqlite
from .plex_remote import AgentClient, report_from_json

CHAPTER_CAPABILITY = "chapters_v1"
TESTED_PMS_VERSION = "1.43.4"
MAX_CHAPTERS = 1000
MAX_JPEG_BYTES = 16 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _valid_bundle_hash(bundle_hash: str, source_size: int) -> bool:
    """Plex prefixes small-file hashes with their decimal size (output.plex_hash)."""
    if source_size < 65_536:
        prefix = str(source_size)
        return bundle_hash.startswith(prefix) and bool(_HASH.fullmatch(bundle_hash[len(prefix) :]))
    return bool(_HASH.fullmatch(bundle_hash))


class ChapterError(PublishError):
    """A chapter refusal with a stable retry classification."""

    def __init__(self, message: str, *, code: str = "registration", state: Capability | None = None) -> None:
        super().__init__(message, state=state)
        self.code = code


@dataclass(frozen=True)
class Chapter:
    """One existing Plex chapter, including the optimistic-concurrency identity."""

    index: int
    start_ms: int
    end_ms: int
    thumb_url: str
    row_id: int
    tag_id: int


@dataclass(frozen=True)
class ChapterTarget:
    """Exact indexed source and chapter rows read before generating any images."""

    rating_key: int
    media_id: int
    part_id: int
    bundle_hash: str
    source_path: str
    source_size: int
    source_updated_at: int | None
    chapters: tuple[Chapter, ...]
    machine_identifier: str


def target_to_json(target: ChapterTarget) -> dict:
    """Serialize a snapshot for the fixed agent protocol."""
    return asdict(target)


def _integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError("Expected a nonnegative whole number")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def target_from_json(raw: Any) -> ChapterTarget:
    """Reject malformed wire snapshots before opening the database."""
    if not isinstance(raw, dict) or not isinstance(raw.get("chapters"), list | tuple):
        raise ValueError("Expected a chapter snapshot")
    if len(raw["chapters"]) > MAX_CHAPTERS:
        raise ValueError("Too many chapters")
    chapters = tuple(
        Chapter(
            _integer(c["index"], minimum=1),
            _integer(c["start_ms"]),
            _integer(c["end_ms"]),
            _text(c["thumb_url"]),
            _integer(c["row_id"], minimum=1),
            _integer(c["tag_id"], minimum=1),
        )
        for c in raw["chapters"]
    )
    target = ChapterTarget(
        _integer(raw["rating_key"], minimum=1),
        _integer(raw["media_id"], minimum=1),
        _integer(raw["part_id"], minimum=1),
        _text(raw["bundle_hash"]),
        _text(raw["source_path"]),
        _integer(raw["source_size"]),
        None if raw["source_updated_at"] is None else _integer(raw["source_updated_at"]),
        chapters,
        _text(raw["machine_identifier"]),
    )
    if (
        not _valid_bundle_hash(target.bundle_hash, target.source_size)
        or not target.machine_identifier
        or not target.source_path
    ):
        raise ValueError("Invalid source identity")
    if [c.index for c in chapters] != list(range(1, len(chapters) + 1)):
        raise ValueError("Chapter indexes must be consecutive and start at one")
    if any(c.end_ms <= c.start_ms for c in chapters):
        raise ValueError("Invalid chapter timing")
    if any(left.end_ms > right.start_ms for left, right in pairwise(chapters)):
        raise ValueError("Chapter timings overlap or are out of order")
    return target


def chapter_url(media_id: int, index: int, revision: str) -> str:
    """Native image route with a content revision for Plex's image cache."""
    return f"/library/media/{media_id}/chapterImages/{index}?mpgChapter={revision}"


class LocalChapters:
    """Chapter operations sharing the marker writer's filesystem and SQLite locks."""

    def __init__(self, config_dir: str, *, mountinfo_path: str = "/proc/self/mountinfo") -> None:
        self.folder = Path(config_dir)
        self.database = LocalPlexDb(lambda: plex_db_path(config_dir), mountinfo_path=mountinfo_path)

    def _identity(self, expected: str, version: str) -> None:
        try:
            actual = (
                ET.parse(self.folder / "Preferences.xml", forbid_dtd=True, forbid_entities=True, forbid_external=True)
                .getroot()
                .get("ProcessedMachineIdentifier")
            )
        except (OSError, ET.ParseError, DefusedXmlException):
            actual = None
        if not expected or not actual or expected != actual:
            raise ChapterError(
                "Cannot prove this database belongs to the connected Plex server",
                code="unsupported",
                state=Capability.MISCONFIGURED,
            )
        if not isinstance(version, str) or not re.fullmatch(r"1\.43\.4\.[0-9]+(?:-[A-Za-z0-9]+)?", version):
            raise ChapterError(
                f"Chapter registration has only been verified with Plex {TESTED_PMS_VERSION}",
                code="unsupported",
                state=Capability.UNSUPPORTED_SCHEMA,
            )

    @staticmethod
    def _schema(conn: sqlite3.Connection) -> None:
        required = {
            "tags": {"id", "tag_type"},
            "taggings": {"id", "metadata_item_id", "tag_id", "index", "time_offset", "end_time_offset", "thumb_url"},
            "media_items": {"id", "metadata_item_id", "deleted_at", "proxy_type"},
            "media_parts": {"id", "media_item_id", "file", "hash", "size", "updated_at", "deleted_at"},
        }
        for table, columns in required.items():
            present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}  # noqa: S608 -- fixed names
            if columns - present:
                raise ChapterError(
                    "Plex chapter database schema differs from the tested schema",
                    code="unsupported",
                    state=Capability.UNSUPPORTED_SCHEMA,
                )
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='taggings'").fetchone():
            raise ChapterError(
                "Plex chapter table has unsupported triggers", code="unsupported", state=Capability.UNSUPPORTED_SCHEMA
            )
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            raise ChapterError(
                "Plex chapter writes require its live WAL database", code="unsupported", state=Capability.NEEDS_LOCAL_DB
            )

    def _guard(self, machine_identifier: str, version: str, deadline: float) -> None:
        self._identity(machine_identifier, version)
        self._media_root()
        report = self.database.file_checks(deadline=deadline)
        if not report.ready:
            raise ChapterError(report.message, code="registration", state=report.state)

    def _media_root(self) -> Path:
        """Trust Plex's configured Media relocation, then confine images to that root."""
        media = self.folder / "Media"
        if media.is_symlink() and not media.is_dir():
            raise ChapterError(
                "Plex's Media link target is missing or inaccessible. Mount its target on the chapter writer.",
                state=Capability.MISCONFIGURED,
            )
        return media.resolve()

    def capability(self, machine_identifier: str, version: str, *, deadline: float) -> CapabilityReport:
        """Check readiness without requiring a chapter or marker tag to exist."""
        try:
            self._guard(machine_identifier, version, deadline)
            with self.database._database(read_only=True, deadline=deadline) as conn:
                self._schema(conn)
        except PublishError as exc:
            return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc))
        except sqlite3.Error as exc:
            error = publish_error_from_sqlite(exc)
            return CapabilityReport(error.state or Capability.UNREACHABLE, str(error))
        return CapabilityReport(
            Capability.READY,
            "Plex chapter registration is ready",
            {"capability": CHAPTER_CAPABILITY, "supported_pms_versions": [TESTED_PMS_VERSION + ".x"]},
        )

    @staticmethod
    def _read(
        conn: sqlite3.Connection, source_path: str, machine_identifier: str, item_id_hint: int | None
    ) -> ChapterTarget:
        rows = conn.execute(
            "SELECT mi.metadata_item_id, mi.id, mp.id, mp.hash, mp.file, mp.size, mp.updated_at "
            "FROM media_parts mp JOIN media_items mi ON mi.id=mp.media_item_id "
            "WHERE mp.file=? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL "
            "AND (mi.proxy_type IS NULL OR mi.proxy_type=0)",
            (source_path,),
        ).fetchall()
        if item_id_hint is not None:
            rows = [row for row in rows if row[0] == item_id_hint]
        if not rows:
            raise ChapterError("Plex has not indexed this source yet", code="pending_index")
        if len(rows) != 1:
            raise ChapterError("Plex has more than one matching source", code="unsupported")
        row = rows[0]
        count = conn.execute(
            "SELECT COUNT(*) FROM media_parts mp JOIN media_items mi ON mi.id=mp.media_item_id "
            "WHERE mi.metadata_item_id=? AND mp.deleted_at IS NULL AND mi.deleted_at IS NULL "
            "AND (mi.proxy_type IS NULL OR mi.proxy_type=0)",
            (row[0],),
        ).fetchone()[0]
        if count != 1:
            raise ChapterError(
                "Chapter registration does not yet support multiple versions or multipart items", code="unsupported"
            )
        chapters = conn.execute(
            "SELECT t.\"index\", t.time_offset, t.end_time_offset, COALESCE(t.thumb_url, ''), t.id, t.tag_id "
            "FROM taggings t JOIN tags ON tags.id=t.tag_id WHERE t.metadata_item_id=? AND tags.tag_type=9 "
            'ORDER BY t."index", t.id',
            (row[0],),
        ).fetchall()
        try:
            target = ChapterTarget(*row, tuple(Chapter(*c) for c in chapters), machine_identifier)
            return target_from_json(target_to_json(target))
        except (KeyError, TypeError, ValueError) as exc:
            raise ChapterError(
                "Plex source or chapter metadata is incomplete or unsupported", code="unsupported"
            ) from exc

    def read(
        self,
        source_path: str,
        machine_identifier: str,
        version: str,
        *,
        item_id_hint: int | None = None,
        deadline: float,
    ) -> ChapterTarget:
        """Read one exact source under the common database lock."""
        self._guard(machine_identifier, version, deadline)
        try:
            with self.database._database(read_only=True, deadline=deadline) as conn:
                self._schema(conn)
                return self._read(conn, source_path, machine_identifier, item_id_hint)
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc

    @staticmethod
    def _within_deadline(deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise ChapterError("Timed out validating chapter registration; no references changed", code="registration")

    def _images(self, target: ChapterTarget, revisions: dict[int, str], *, deadline: float) -> None:
        if set(revisions) != {chapter.index for chapter in target.chapters}:
            raise ChapterError("Registration requires every chapter image", code="registration")
        bundle = target.bundle_hash
        root = self._media_root()
        folder = self.folder / "Media" / "localhost" / bundle[0] / f"{bundle[1:]}.bundle" / "Contents" / "Chapters"
        for index, revision in revisions.items():
            self._within_deadline(deadline)
            if type(index) is not int or not isinstance(revision, str) or not _SHA256.fullmatch(revision):
                raise ChapterError("Invalid chapter image revision", code="registration")
            path = folder / f"chapter{index}.jpg"
            try:
                if not path.resolve().is_relative_to(root) or path.is_symlink():
                    raise ValueError("Image escapes the Plex Media folder")
                with path.open("rb") as stream:
                    data = stream.read(MAX_JPEG_BYTES + 1)
                if len(data) > MAX_JPEG_BYTES or hashlib.sha256(data).hexdigest() != revision:
                    raise ValueError("Image changed or exceeds the image limit")
                with Image.open(io.BytesIO(data)) as image:
                    if (
                        image.format != "JPEG"
                        or image.width > 8192
                        or image.height > 8192
                        or image.width * image.height > 16_000_000
                    ):
                        raise ValueError("Not a supported JPEG")
                    image.load()
            except (OSError, ValueError, Image.DecompressionBombError) as exc:
                raise ChapterError(
                    f"Chapter {index} image is missing, invalid, or changed", code="registration"
                ) from exc

    def register(self, target: ChapterTarget, revisions: dict[int, str], version: str, *, deadline: float) -> None:
        """Compare the whole snapshot and update only thumb_url, all or nothing."""
        target = target_from_json(target_to_json(target))
        self._guard(target.machine_identifier, version, deadline)
        try:
            with self.database._database(read_only=False, deadline=deadline) as conn:
                self.database._begin_write(conn, deadline)
                try:
                    self._identity(target.machine_identifier, version)
                    self._schema(conn)
                    current = self._read(conn, target.source_path, target.machine_identifier, target.rating_key)
                    if current != target:
                        raise ChapterError(
                            "Plex source or chapters changed while images were generated", code="source_changed"
                        )
                    self._images(target, revisions, deadline=deadline)
                    for chapter in target.chapters:
                        self._within_deadline(deadline)
                        desired = chapter_url(target.media_id, chapter.index, revisions[chapter.index])
                        if chapter.thumb_url != desired:
                            conn.execute("UPDATE taggings SET thumb_url=? WHERE id=?", (desired, chapter.row_id))
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
        except sqlite3.Error as exc:
            raise publish_error_from_sqlite(exc) from exc


def _context(server: Any, server_config: Any = None) -> tuple[Any, str, str, Any]:
    try:
        plex = server._connect()
        identity = plex.query("/identity")
        machine = identity.get("machineIdentifier") or ""
        version = identity.get("version") or ""
    except Exception as exc:
        raise ChapterError(
            "Cannot verify Plex server identity for chapter registration",
            code="registration",
            state=Capability.UNREACHABLE,
        ) from exc
    config = server_config if server_config is not None else server._server_config
    settings = load_server(getattr(config, "markers", None), "plex")
    if settings.agent_enabled:
        if not settings.agent_url or not settings.agent_token:
            raise ChapterError("Configure the Plex-side helper address and key", state=Capability.AGENT_UNAVAILABLE)
        backend = AgentClient(settings.agent_url, settings.agent_token)
    else:
        folder = str((getattr(config, "output", None) or {}).get("plex_config_folder") or "")
        if not folder:
            raise ChapterError(
                "Set a local Plex config folder or configure the Plex-side helper", state=Capability.NEEDS_LOCAL_DB
            )
        backend = LocalChapters(folder)
    return backend, machine, version, plex


def _remote(client: AgentClient, operation: str, machine: str, version: str, **body: Any) -> dict:
    identity = client.ping(timeout=5)
    if CHAPTER_CAPABILITY not in client.capabilities:
        raise ChapterError(
            "Update the Plex-side helper to version 1.1.0 or newer for chapter registration",
            state=Capability.PLUGIN_OUTDATED,
        )
    if not machine or identity.get("machine_identifier") != machine:
        raise ChapterError(
            "The Plex-side helper belongs to a different or unverifiable Plex server", state=Capability.MISCONFIGURED
        )
    result = client.post(
        f"/v1/chapters/{operation}",
        {
            "machine_identifier": machine,
            "pms_version": version,
            "deadline_s": 10,
            **body,
        },
        timeout=13,
    )
    if result.get("capability") != CHAPTER_CAPABILITY:
        raise ChapterError("Update the Plex-side helper for chapter registration", state=Capability.PLUGIN_OUTDATED)
    return result


def chapter_capability(server: Any, server_config: Any = None) -> CapabilityReport:
    """Readiness independently of marker settings, consent, or Plex Pass."""
    try:
        backend, machine, version, _ = _context(server, server_config)
        if isinstance(backend, AgentClient):
            return report_from_json(_remote(backend, "check", machine, version)["report"])
        return backend.capability(machine, version, deadline=time.monotonic() + 10)
    except PublishError as exc:
        return CapabilityReport(exc.state or Capability.UNREACHABLE, str(exc))
    except (KeyError, TypeError, ValueError):
        return CapabilityReport(Capability.AGENT_UNAVAILABLE, "Plex-side helper returned an invalid chapter response")


def resolve_chapter_target(server: Any, canonical_path: str, *, item_id_hint: int | None = None) -> ChapterTarget:
    """Resolve Plex's original source path to a guarded chapter snapshot."""
    backend, machine, version, _ = _context(server)
    if isinstance(backend, AgentClient):
        try:
            result = _remote(backend, "read", machine, version, source_path=canonical_path, item_id_hint=item_id_hint)
            target = target_from_json(result["target"])
            if target.machine_identifier != machine or target.source_path != canonical_path:
                raise ValueError("Wrong source identity")
            return target
        except (KeyError, TypeError, ValueError) as exc:
            raise ChapterError("Plex-side helper returned an invalid chapter snapshot") from exc
    return backend.read(canonical_path, machine, version, item_id_hint=item_id_hint, deadline=time.monotonic() + 10)


def register_chapters(server: Any, target: ChapterTarget, revisions: dict[int, str]) -> None:
    """Register all images, then require Plex's live API to expose the exact references."""
    backend, machine, version, plex = _context(server)
    if machine != target.machine_identifier:
        raise ChapterError("Connected Plex server changed during chapter generation", code="source_changed")
    if isinstance(backend, AgentClient):
        _remote(
            backend,
            "register",
            machine,
            version,
            target=target_to_json(target),
            revisions=[{"index": index, "sha256": sha} for index, sha in revisions.items()],
        )
    else:
        backend.register(target, revisions, version, deadline=time.monotonic() + 10)
    verify_chapters(server, target, revisions)


def verify_chapters(server: Any, target: ChapterTarget, revisions: dict[int, str]) -> None:
    """Verify references even when an earlier commit succeeded but API read-back failed."""
    try:
        plex = server._connect()
        if plex.query("/identity").get("machineIdentifier") != target.machine_identifier:
            raise ValueError("Connected Plex server changed")
        result = plex.query(f"/library/metadata/{target.rating_key}?includeChapters=1")
        observed = {int(c.get("index")): c.get("thumb") for c in result.findall(".//Chapter")}
        expected = {c.index: chapter_url(target.media_id, c.index, revisions[c.index]) for c in target.chapters}
        if observed != expected:
            raise ValueError("Plex has not exposed the registered chapters")
    except Exception as exc:
        raise ChapterError("Chapter images await verification through Plex's API", code="registration") from exc
