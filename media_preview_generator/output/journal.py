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

The fix is a ``.meta`` JSON sidecar written next to every published
output recording the source file's ``(mtime, size)`` at publish time.
Subsequent webhooks compare current source ``(mtime, size)`` against
the journal: match -> safely skip, mismatch -> force regenerate.

mtime + size is what Plex/Emby/Jellyfin themselves use for "changed"
detection; full hashes are overkill for this. The journal is portable
JSON (not xattrs), survives copies, and is readable by humans for
debugging.

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
from pathlib import Path

from loguru import logger

#: Bumped whenever the on-disk schema changes. Older schemas are treated
#: as a miss (forces regen, then writes the new schema). Cheap to bump.
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


def _fingerprint(source: dict) -> tuple[int, int] | None:
    """Return a recorded source's ``(mtime, size)``, or ``None`` when the entry is unusable."""
    try:
        return int(source.get("mtime", -1)), int(source.get("size", -1))
    except (TypeError, ValueError):
        return None


def _read_sources(meta_path: Path) -> list[dict] | None:
    """Return the sources one ``.meta`` records, or ``None`` when it proves nothing.

    ``None`` covers a missing, unreadable or other-schema sidecar. A sidecar
    written before ``sources`` existed records its single source in the
    top-level ``source_*`` fields.
    """
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
    sources = data.get("sources")
    if isinstance(sources, list):
        return [s for s in sources if isinstance(s, dict)]
    return [
        {
            "path": data.get("source_path", ""),
            "mtime": data.get("source_mtime", -1),
            "size": data.get("source_size", -1),
        }
    ]


def write_meta(output_paths: list[Path], canonical_path: str, *, publisher: str | None = None) -> None:
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
        st = os.stat(canonical_path)
    except OSError:
        # Source vanished between publish and meta-write — leave the
        # outputs un-stamped so a future webhook re-publishes if the
        # source comes back.
        return

    this_source = {"path": canonical_path, "mtime": int(st.st_mtime), "size": int(st.st_size)}

    for output in output_paths:
        meta_path = _meta_path_for(output)
        try:
            with _WRITE_LOCK:
                copies = []
                for source in _read_sources(meta_path) or []:
                    fingerprint = _fingerprint(source)
                    if source.get("path") != canonical_path and fingerprint and fingerprint[1] == this_source["size"]:
                        copies.append(source)
                payload = {
                    "schema": JOURNAL_SCHEMA_VERSION,
                    "source_path": canonical_path,
                    "source_mtime": this_source["mtime"],
                    "source_size": this_source["size"],
                    "publisher": publisher or "",
                    "sources": [*copies, this_source],
                }
                # Atomic write: a crash mid-write would otherwise leave a
                # truncated .meta that outputs_fresh_for_source() falls back
                # on the legacy "no .meta = fresh" branch — which would treat
                # stale outputs as fresh on the next dispatch.
                tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
                tmp_path.write_text(json.dumps(payload, separators=(",", ":")))
                os.replace(tmp_path, meta_path)
        except OSError as exc:
            # Don't let a read or write failure here mask a successful publish.
            logger.debug("Could not write journal meta for {}: {}", output, exc)


def outputs_fresh_for_source(output_paths: list[Path], canonical_path: str) -> bool:
    """Return True iff outputs exist and, where journals exist, the source matches.

    "Fresh" semantics:

    * Every entry in ``output_paths`` must exist on disk.
    * If **no** ``.meta`` sidecars exist at all (legacy outputs from
      before the journal feature shipped), assume fresh — preserves the
      pre-journal skip-if-exists behavior so upgrading the tool doesn't
      force a regeneration storm. The next successful publish stamps a
      ``.meta`` so subsequent calls go through the strict path.
    * If **any** ``.meta`` sidecar exists, at least one must record
      ``(mtime, size)`` matching the current source among its
      ``sources``. Conversely, if no recorded fingerprint matches,
      fresh is False — the source has been replaced (Sonarr quality
      upgrade, manual swap) or a copy sharing the output published it,
      so skip-if-exists should not fire.

    Schema mismatch and I/O errors on a particular ``.meta`` are
    ignored; they neither prove nor disprove freshness. The ``stat``
    of the source itself failing returns False — better to regenerate
    than gamble.
    """
    if not output_paths:
        return False

    if not all(p.exists() for p in output_paths):
        return False

    try:
        st = os.stat(canonical_path)
    except OSError:
        return False
    src_mtime = int(st.st_mtime)
    src_size = int(st.st_size)

    saw_match = False
    saw_mismatch = False
    for output in output_paths:
        sources = _read_sources(_meta_path_for(output))
        if sources is None:
            continue
        if any(_fingerprint(s) == (src_mtime, src_size) for s in sources):
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
    return True


def clear_meta(output_paths: list[Path]) -> None:
    """Remove ``.meta`` sidecars for the given outputs.

    Used by force-regenerate flows so a stale fingerprint can't shortcut
    a freshly-requested run. Best-effort; missing files are fine.
    """
    for output in output_paths:
        try:
            _meta_path_for(output).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.debug("Could not remove stale meta for {}: {}", output, exc)
