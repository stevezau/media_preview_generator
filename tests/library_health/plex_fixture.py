"""Builds a minimal Plex library database (only the tables and columns the health counter reads)."""

from __future__ import annotations

import sqlite3
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from media_preview_generator.output.plex_bundle import PlexBundleAdapter

_SCHEMA = """
CREATE TABLE metadata_items (id INTEGER PRIMARY KEY, library_section_id INTEGER, metadata_type INTEGER,
    parent_id INTEGER, title TEXT, "index" INTEGER, deleted_at INTEGER);
CREATE TABLE media_items (id INTEGER PRIMARY KEY, metadata_item_id INTEGER, deleted_at INTEGER, proxy_type INTEGER);
CREATE TABLE media_parts (id INTEGER PRIMARY KEY, media_item_id INTEGER, file TEXT, hash TEXT, extra_data TEXT,
    deleted_at INTEGER);
CREATE TABLE media_streams (id INTEGER PRIMARY KEY, media_part_id INTEGER, stream_type_id INTEGER, extra_data TEXT,
    "index" INTEGER);
CREATE TABLE tags (id INTEGER PRIMARY KEY, tag TEXT, tag_type INTEGER);
CREATE TABLE taggings (id INTEGER PRIMARY KEY, metadata_item_id INTEGER, tag_id INTEGER, text TEXT);
"""

SD_FLAG = '{"ma:container":"mkv","mi:indexes":"sd"}'


def build_plex_db(path: str | Path, items: list[dict[str, Any]]) -> None:
    """Write a Plex database holding ``items``.

    Args:
        path: Where to create the database file.
        items: One dict per movie/episode with ``section`` (int), ``type`` (1 movie, 4 episode), ``title`` and
            ``parts`` (list of dicts: ``file``, ``hash``, ``extra_data`` and ``audio``, a list of stream
            ``extra_data`` values). Optional: ``show``, ``season_index``, ``index`` (episodes), ``tags``
            (``"intro"`` / ``"credits"``).
    """
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (1, '', 12)")
    next_id = {"item": 1, "media": 1, "part": 1, "stream": 1, "tagging": 1}

    def take(kind: str) -> int:
        value = next_id[kind]
        next_id[kind] += 1
        return value

    shows: dict[tuple[int, str, int], int] = {}
    for item in items:
        parent_id = None
        if item["type"] == 4:
            season_key = (item["section"], item.get("show", ""), item.get("season_index", 1))
            if season_key not in shows:
                show_id, season_id = take("item"), take("item")
                conn.execute(
                    "INSERT INTO metadata_items VALUES (?, ?, 2, NULL, ?, 1, NULL)",
                    (show_id, item["section"], season_key[1]),
                )
                conn.execute(
                    "INSERT INTO metadata_items VALUES (?, ?, 3, ?, ?, ?, NULL)",
                    (season_id, item["section"], show_id, f"Season {season_key[2]}", season_key[2]),
                )
                shows[season_key] = season_id
            parent_id = shows[season_key]
        item_id = take("item")
        conn.execute(
            "INSERT INTO metadata_items VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (item_id, item["section"], item["type"], parent_id, item["title"], item.get("index", 1)),
        )
        media_id = take("media")
        conn.execute("INSERT INTO media_items VALUES (?, ?, NULL, 0)", (media_id, item_id))
        for part in item["parts"]:
            part_id = take("part")
            conn.execute(
                "INSERT INTO media_parts VALUES (?, ?, ?, ?, ?, NULL)",
                (part_id, media_id, part["file"], part.get("hash"), part.get("extra_data")),
            )
            for stream_extra in part.get("audio", []):
                conn.execute(
                    "INSERT INTO media_streams VALUES (?, ?, 2, ?, 1)", (take("stream"), part_id, stream_extra)
                )
            for stream_extra in part.get("unindexed_audio", []):
                conn.execute(
                    "INSERT INTO media_streams VALUES (?, ?, 2, ?, NULL)", (take("stream"), part_id, stream_extra)
                )
        for tag in item.get("tags", []):
            conn.execute("INSERT INTO taggings VALUES (?, ?, 1, ?)", (take("tagging"), item_id, tag))
    conn.commit()
    conn.close()


def write_bif(plex_folder: str | Path, bundle_hash: str, size: int = 10) -> None:
    """Create the BIF file Plex would have for ``bundle_hash``."""
    bif = PlexBundleAdapter.bundle_bif_path(str(plex_folder), bundle_hash)
    bif.parent.mkdir(parents=True, exist_ok=True)
    bif.write_bytes(b"x" * size)


class FakeLocalDb:
    """Stands in for ``LocalPlexDb``: opens the fixture file without the schema and lock checks."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)

    @contextmanager
    def _database(self, *, read_only: bool, deadline: float):
        assert read_only is True, "the health check must open Plex's database read-only"
        conn = sqlite3.connect(f"file:{urllib.parse.quote(self._path)}?mode=ro", uri=True)
        try:
            yield conn
        finally:
            conn.close()
