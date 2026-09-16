"""Plex's own markers for the eval files, read-only from the prod Plex database (the "Plex baseline").

Two exports have the same content:

- The lab scale run's dump (``evidence/lab/results/scale/prod_plex_markers.json`` and ``prod_plex_parts.json`` next to
  it): every intro/credits marker and every movie/episode part of the prod library. The default.
- A fresh export of the eval's season folders: ``python -m tools.markers_eval plex-sql`` piped over ssh into
  ``sqlite3 -separator '|' 'file:<prod db>?mode=ro'`` on ``plex`` (README has the command), saved as
  ``evidence/lab/prod_plex_baseline.txt``. Rows: file|type|start_ms|end_ms|extra_data|duration_ms|hash; a file without
  markers has empty marker fields. A path holding ``|`` would split wrongly; the prod library has none.

Plex keeps one marker set per item. None of the eval's files belong to an item with a second version, so the app's
"another cut" rule (spec §5.5 rule 7) never drops Plex's markers here.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from media_preview_generator.markers.models import Candidate, MarkerType, Source

MARKER_TAG_TYPE = 12
PARTS_FILE = "prod_plex_parts.json"
_TYPES = {"intro": MarkerType.INTRO, "credits": MarkerType.CREDITS}


@dataclass(frozen=True)
class PlexMarker:
    """One of Plex's markers on a file (``final``: credits that run to the end, Plex's ``pv:final``)."""

    type: str
    start_ms: int
    end_ms: int
    final: bool


def _like(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
    return f"mp.file LIKE '{escaped}/%' ESCAPE '\\'"


def export_sql(folders: list[str]) -> str:
    """A read-only query for every part under the given folders, with its intro/credits markers when it has any.

    Args:
        folders: Season (or movie) folders.

    Returns:
        One SELECT statement (the marker join is the one the lab scale run's dump used).
    """
    where = " OR ".join(_like(f) for f in sorted(set(folders)))
    return (
        "SELECT mp.file, t.text, t.time_offset, t.end_time_offset, t.extra_data, mi.duration, mp.hash\n"
        "FROM media_parts mp JOIN media_items mi ON mi.id = mp.media_item_id\n"
        "LEFT JOIN taggings t ON t.metadata_item_id = mi.metadata_item_id\n"
        f"  AND t.tag_id IN (SELECT id FROM tags WHERE tag_type = {MARKER_TAG_TYPE}) AND t.text IN ('intro', 'credits')\n"
        f"WHERE {where}\nORDER BY mp.file, t.time_offset;\n"
    )


def _final(extra: str | None) -> bool:
    return '"pv:final":"1"' in (extra or "")


def parse_export(lines: Iterable[str]) -> dict[str, list[PlexMarker]]:
    """Markers by file path from an ``export_sql`` result.

    Args:
        lines: The export's rows.

    Returns:
        Markers by path (``[]`` for a file Plex has no markers for).
    """
    out: dict[str, list[PlexMarker]] = {}
    for line in lines:
        parts = line.rstrip("\n").split("|")
        if len(parts) < 7:
            continue
        path, mtype, start, end, extra = parts[:5]
        markers = out.setdefault(path, [])
        if mtype and start and end:
            markers.append(PlexMarker(mtype, int(start), int(end), _final(extra)))
    return out


def parse_scale_dump(markers: list[dict], parts: list[dict]) -> dict[str, list[PlexMarker]]:
    """Markers by file path from the lab scale run's dump.

    Args:
        markers: ``prod_plex_markers.json`` rows (file, type, start, end, extra).
        parts: ``prod_plex_parts.json`` rows (file): every movie and episode part.

    Returns:
        Markers by path for every part (``[]`` for a file Plex has no markers for), in start order.
    """
    out: dict[str, list[PlexMarker]] = {row["file"]: [] for row in parts}
    for row in markers:
        if row.get("start") is None or row.get("end") is None:
            continue
        out.setdefault(row["file"], []).append(PlexMarker(row["type"], row["start"], row["end"], _final(row["extra"])))
    for found in out.values():
        found.sort(key=lambda m: (m.start_ms, m.type))
    return out


def load_baseline(path: Path) -> dict[str, list[PlexMarker]]:
    """Plex's markers by file path from either export.

    Args:
        path: ``prod_plex_markers.json`` (its ``prod_plex_parts.json`` sibling is read too) or a ``plex-sql`` export.

    Returns:
        Markers by path.
    """
    if path.suffix == ".json":
        parts = json.loads((path.parent / PARTS_FILE).read_text())
        return parse_scale_dump(json.loads(path.read_text()), parts)
    with path.open(encoding="utf-8") as fh:
        return parse_export(fh)


def server_candidates(markers: list[PlexMarker], mtype: MarkerType | None = None) -> list[Candidate]:
    """Plex's markers as the app reads them for evidence (``sources/server_markers.py``: final credits have no end).

    Args:
        markers: One file's markers.
        mtype: Only this type (default: intros and credits).

    Returns:
        ``server_markers`` candidates with origin ``plex``.
    """
    out = []
    for m in markers:
        found = _TYPES.get(m.type)
        if found is None or (mtype is not None and found is not mtype):
            continue
        end = None if found is MarkerType.CREDITS and m.final else m.end_ms
        out.append(Candidate(found, m.start_ms, end, Source.SERVER_MARKERS, origin="plex"))
    return out


def first_marker(markers: list[PlexMarker], mtype: MarkerType) -> PlexMarker | None:
    """Plex's earliest marker of a type on a file: the first skip it offers."""
    return min((m for m in markers if m.type == mtype.value), key=lambda m: m.start_ms, default=None)
