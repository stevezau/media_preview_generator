"""Per-output ``.meta`` sidecar journal for source-aware skip-if-exists.

Background — what problem this solves:

The frame cache (process-wide, 10-min TTL) coalesces *concurrent* webhooks
for the same file. But when webhooks for the same file arrive *minutes
apart* (Sonarr fires immediately on import; Plex's own webhook fires
after its periodic library scan, often 30+ minutes later), the second
webhook arrives after the cache has expired. Today we re-extract frames
even though all publishers' outputs already exist on disk — wasted work.

Worse, if a user *replaces* the source file (Sonarr "upgrade" pulls a
higher-quality copy), the existing outputs are now stale. Plain
``output_paths.exists()`` skip-if-exists would happily reuse the old
BIF for a different source.

The ``.meta`` JSON sidecar records each source file's identity, size and
nanosecond modification/change times. New journals detect replacement even
when an importer preserves the original mtime and size. Older journals
retain their mtime/size checks for existing-output compatibility.
Cross-publisher reuse requires a matching strong fingerprint so old frames
cannot seed a newly calculated Plex bundle after a source replacement.

One output can serve several source files. Plex names a bundle after a
hash of the file's *content*, so two copies of the same video (a
re-grab next to the original, one download filed under two events) get
the same ``index-sd.bif``. The journal keeps a fingerprint per source in
``sources``; with only the last publisher's fingerprint, each copy found
the other's stamp every night and rebuilt the shared BIF.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from loguru import logger

from .plex_hash import SourceFingerprint, get_source_fingerprint

#: Bumped whenever the on-disk schema changes. A sidecar in another schema
#: proves nothing either way, so its output is judged like one with no
#: sidecar (fresh); the next publish rewrites it in the current schema.
JOURNAL_SCHEMA_VERSION = 1

#: Serialises the read-merge-write of ``.meta`` files. Copies sharing an output
#: can publish at the same moment from different workers; without this, both
#: read the old sources and the last write drops the other's entry.
_WRITE_LOCK = threading.Lock()


def _meta_path_for(output: Path) -> Path:
    """Return the ``.meta`` sidecar path for an output file.

    We append (not replace) the suffix so multiple outputs in the same
    directory don't collide and users can ``ls`` them next to the real
    files.
    """
    return output.with_suffix(output.suffix + ".meta")


def _has_content(output: Path) -> bool:
    """Whether an output is on disk with data in it.

    A 0-byte file is what a power loss leaves when the name reached the disk
    before the data did; it holds no preview, so it counts as missing.
    """
    try:
        return output.stat().st_size > 0
    except OSError:
        return False


def _fingerprint(source: dict) -> tuple[int, int] | None:
    """Return a recorded source's ``(mtime, size)``, or ``None`` when the entry is unusable."""
    try:
        return int(source.get("mtime", -1)), int(source.get("size", -1))
    except (TypeError, ValueError):
        return None


def _read_meta(meta_path: Path) -> dict | None:
    """Read a current-schema journal, treating missing or malformed data as unknown."""
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        if int(data.get("schema", 0)) != JOURNAL_SCHEMA_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    return data


def _sources_from_meta(data: dict) -> list[dict]:
    sources = data.get("sources")
    if isinstance(sources, list):
        return [s for s in sources if isinstance(s, dict)]
    return [
        {
            "path": data.get("source_path", ""),
            "mtime": data.get("source_mtime", -1),
            "size": data.get("source_size", -1),
            **({"source_fingerprint": data["source_fingerprint"]} if "source_fingerprint" in data else {}),
        }
    ]


def _read_sources(meta_path: Path) -> list[dict] | None:
    """Read source records, including the legacy single-source journal shape."""
    data = _read_meta(meta_path)
    return _sources_from_meta(data) if data is not None else None


def _write_meta_document(meta_path: Path, payload: dict) -> None:
    """Replace a journal atomically; callers hold ``_WRITE_LOCK``."""
    tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
    try:
        with open(tmp_path, "w") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")))
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError as exc:
                logger.debug("fsync failed for {}: {}", tmp_path, exc)
        os.replace(tmp_path, meta_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _pending_refreshes(data: dict) -> list[dict]:
    entries = data.get("plex_refresh_pending")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def write_meta(
    output_paths: list[Path],
    canonical_path: str,
    *,
    publisher: str | None = None,
    source_fingerprint: SourceFingerprint | None = None,
) -> None:
    """Stamp every output with the source file's freshness fingerprint.

    Called by ``_publish_one`` immediately after a successful publish.
    Writes one ``.meta`` per output so any single output that survives
    (e.g. user manually deleted the others) still carries the
    fingerprint and the dispatcher can make a correct freshness call.

    Other sources already recorded on an output are kept when they are the
    same size as this one: they share the output because their content
    matches (see the module docstring), so the rebuilt file still serves
    them. A different size means one of them has changed since, so its
    entry is dropped and it regenerates on its next dispatch.

    Failures here never bubble — at worst we miss a future short-circuit
    and re-run FFmpeg.
    """
    try:
        fingerprint = source_fingerprint or get_source_fingerprint(canonical_path)
    except OSError:
        # Source vanished between publish and meta-write — leave the
        # outputs un-stamped so a future webhook re-publishes if the
        # source comes back.
        return

    this_source = {
        "path": canonical_path,
        "mtime": fingerprint[3] // 1_000_000_000,
        "size": fingerprint[2],
        "source_fingerprint": list(fingerprint),
    }

    for output in output_paths:
        meta_path = _meta_path_for(output)
        try:
            with _WRITE_LOCK:
                copies = []
                for source in _read_sources(meta_path) or []:
                    recorded = _fingerprint(source)
                    if source.get("path") != canonical_path and recorded and recorded[1] == this_source["size"]:
                        copies.append(source)
                payload = {
                    "schema": JOURNAL_SCHEMA_VERSION,
                    "source_path": canonical_path,
                    "source_mtime": this_source["mtime"],
                    "source_size": this_source["size"],
                    "source_fingerprint": this_source["source_fingerprint"],
                    "publisher": publisher or "",
                    "sources": [*copies, this_source],
                }
                # A publication for another source/server must not erase an
                # outstanding notification on this shared Plex bundle.
                pending = _pending_refreshes(_read_meta(meta_path) or {})
                if pending:
                    payload["plex_refresh_pending"] = pending
                _write_meta_document(meta_path, payload)
        except OSError as exc:
            # Don't let a read or write failure here mask a successful publish.
            logger.debug("Could not write journal meta for {}: {}", output, exc)


def mark_plex_refresh_pending(
    output_paths: list[Path],
    canonical_path: str,
    server_id: str,
    *,
    source_fingerprint: SourceFingerprint | None = None,
) -> str | None:
    """Record one publication's notification; return its token if persisted.

    A new token distinguishes regenerations of the same unchanged source, so
    an older in-flight Analyze cannot acknowledge the newer publication.
    Only a source already stamped on the output can acquire a marker.
    """
    try:
        fingerprint = source_fingerprint or get_source_fingerprint(canonical_path)
    except OSError:
        return None
    token = uuid.uuid4().hex
    marker = {
        "path": canonical_path,
        "server_id": server_id,
        "source_fingerprint": list(fingerprint),
        "token": token,
    }
    persisted = False
    for output in output_paths:
        meta_path = _meta_path_for(output)
        try:
            with _WRITE_LOCK:
                data = _read_meta(meta_path)
                if data is None or not any(
                    source.get("path") == canonical_path and source.get("source_fingerprint") == list(fingerprint)
                    for source in _sources_from_meta(data)
                ):
                    continue
                pending = [
                    entry
                    for entry in _pending_refreshes(data)
                    if (entry.get("path"), entry.get("server_id")) != (canonical_path, server_id)
                ]
                data["plex_refresh_pending"] = [*pending, marker]
                _write_meta_document(meta_path, data)
                persisted = True
        except OSError as exc:
            logger.warning("Could not retain pending Plex notification for {}: {}", output, exc)
    return token if persisted else None


def get_plex_refresh_pending(
    output_paths: list[Path],
    canonical_path: str,
    server_id: str,
    *,
    source_fingerprint: SourceFingerprint | None = None,
) -> str | None:
    """Return a matching pending token; legacy/unmarked outputs need no Analyze."""
    try:
        fingerprint = source_fingerprint or get_source_fingerprint(canonical_path)
    except OSError:
        return None
    with _WRITE_LOCK:
        for output in output_paths:
            data = _read_meta(_meta_path_for(output))
            for entry in _pending_refreshes(data or {}):
                if (
                    entry.get("path") == canonical_path
                    and entry.get("server_id") == server_id
                    and entry.get("source_fingerprint") == list(fingerprint)
                    and isinstance(entry.get("token"), str)
                    and entry["token"]
                ):
                    return entry["token"]
    return None


def clear_plex_refresh_pending(
    output_paths: list[Path],
    canonical_path: str,
    server_id: str,
    token: str,
    *,
    source_fingerprint: SourceFingerprint | None = None,
) -> None:
    """Acknowledge only the exact publication that an accepted Analyze covers."""
    try:
        fingerprint = source_fingerprint or get_source_fingerprint(canonical_path)
    except OSError:
        return
    for output in output_paths:
        meta_path = _meta_path_for(output)
        try:
            with _WRITE_LOCK:
                data = _read_meta(meta_path)
                if data is None:
                    continue
                pending = _pending_refreshes(data)
                remaining = [
                    entry
                    for entry in pending
                    if not (
                        entry.get("path") == canonical_path
                        and entry.get("server_id") == server_id
                        and entry.get("source_fingerprint") == list(fingerprint)
                        and entry.get("token") == token
                    )
                ]
                if remaining == pending:
                    continue
                if remaining:
                    data["plex_refresh_pending"] = remaining
                else:
                    data.pop("plex_refresh_pending", None)
                _write_meta_document(meta_path, data)
        except OSError as exc:
            logger.debug("Could not acknowledge Plex notification for {}: {}", output, exc)


def outputs_fresh_for_source(
    output_paths: list[Path], canonical_path: str, *, require_source_fingerprint: bool = False
) -> bool:
    """Return True iff outputs exist and, where journals exist, the source matches.

    "Fresh" semantics:

    * Every entry in ``output_paths`` must exist on disk and hold data; a
      0-byte output counts as missing whatever its ``.meta`` says.
    * If **no** ``.meta`` sidecars exist at all (legacy outputs from
      before the journal feature shipped), assume fresh — preserves the
      pre-journal skip-if-exists behavior so upgrading the tool doesn't
      force a regeneration storm. The next successful publish stamps a
      ``.meta`` so subsequent calls go through the strict path.
    * If **any** ``.meta`` sidecar exists, at least one must record
      a fingerprint matching the current source among its
      ``sources``. Conversely, if no recorded fingerprint matches,
      fresh is False — the source has been replaced (Sonarr quality
      upgrade, manual swap) or a copy sharing the output published it,
      so skip-if-exists should not fire.

    Schema mismatch and I/O errors on a particular ``.meta`` are
    ignored; they neither prove nor disprove freshness. The ``stat``
    of the source itself failing returns False — better to regenerate
    than gamble.

    New records compare the full source fingerprint. Legacy records retain
    mtime/size matching unless ``require_source_fingerprint`` is set; use
    that stricter check before transferring frames between publishers.
    """
    if not output_paths:
        return False

    if not all(_has_content(p) for p in output_paths):
        return False

    try:
        fingerprint = get_source_fingerprint(canonical_path)
    except OSError:
        return False
    src_mtime = fingerprint[3] // 1_000_000_000
    src_size = fingerprint[2]

    saw_match = False
    saw_mismatch = False
    for output in output_paths:
        sources = _read_sources(_meta_path_for(output))
        if sources is None:
            continue
        if any(
            source["source_fingerprint"] == list(fingerprint)
            if "source_fingerprint" in source
            else not require_source_fingerprint and _fingerprint(source) == (src_mtime, src_size)
            for source in sources
        ):
            saw_match = True
        else:
            saw_mismatch = True

    if saw_match:
        return True
    if saw_mismatch:
        return False
    # No usable meta on any output — legacy outputs from before the
    # journal feature; preserve the pre-journal skip-if-exists semantic
    # so upgrades don't force a regeneration storm.
    return not require_source_fingerprint


def clear_meta(output_paths: list[Path], *, preserve_plex_refresh_pending: bool = False) -> None:
    """Remove ``.meta`` sidecars for the given outputs.

    Used by force-regenerate flows so a stale fingerprint can't shortcut
    a freshly-requested run. Regeneration can preserve pending notifications
    belonging to other sources/servers sharing the output. Best-effort.
    """
    for output in output_paths:
        try:
            with _WRITE_LOCK:
                meta_path = _meta_path_for(output)
                pending = _pending_refreshes(_read_meta(meta_path) or {}) if preserve_plex_refresh_pending else []
                if pending:
                    _write_meta_document(
                        meta_path,
                        {"schema": JOURNAL_SCHEMA_VERSION, "sources": [], "plex_refresh_pending": pending},
                    )
                else:
                    meta_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not remove stale meta for {}: {}", output, exc)
