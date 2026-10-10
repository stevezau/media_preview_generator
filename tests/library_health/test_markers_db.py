"""Tests for library_health.markers_db."""

import sqlite3

from media_preview_generator.library_health.markers_db import markers_db_path, nothing_found
from media_preview_generator.library_health.models import Feature
from media_preview_generator.markers.store import MarkerStore


def _add(conn, file_id, path, decisions):
    conn.execute(
        "INSERT INTO files (id, canonical_path, size, mtime_ns, updated_at) VALUES (?, ?, 1, 1, 'now')",
        (file_id, path),
    )
    for kind, status in decisions:
        conn.execute(
            "INSERT INTO decisions (file_id, type, status, reason, settings_fingerprint, decided_at) "
            "VALUES (?, ?, ?, '', 'fp', 'now')",
            (file_id, kind, status),
        )


def test_nothing_found_reads_real_schema(tmp_path):
    db = str(tmp_path / "markers.db")
    store = MarkerStore(db)
    store.close()
    conn = sqlite3.connect(db)
    _add(conn, 1, "/a.mkv", [("intro", "no_evidence"), ("credits", "decided")])
    _add(conn, 2, "/b.mkv", [("credits", "needs_review")])
    conn.commit()
    conn.close()

    assert nothing_found(db) == {"/a.mkv": {Feature.INTRO}, "/b.mkv": {Feature.CREDITS}}


def test_missing_file_returns_empty(tmp_path):
    assert nothing_found(str(tmp_path / "nope.db")) == {}


def test_unreadable_file_returns_empty(tmp_path):
    bad = tmp_path / "markers.db"
    bad.write_bytes(b"not a database at all" * 100)
    assert nothing_found(str(bad)) == {}


def test_markers_db_path_uses_config_dir(monkeypatch):
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: "/cfg")
    assert markers_db_path() == "/cfg/markers.db"


def test_path_with_url_special_characters_is_read(tmp_path):
    folder = tmp_path / "a#b?c%d"
    folder.mkdir()
    db = str(folder / "markers.db")
    MarkerStore(db).close()
    conn = sqlite3.connect(db)
    _add(conn, 1, "/a.mkv", [("intro", "no_evidence")])
    conn.commit()
    conn.close()

    assert nothing_found(db) == {"/a.mkv": {Feature.INTRO}}
