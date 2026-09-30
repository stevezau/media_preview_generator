"""loudness.undo restores only tracks still holding what the job wrote."""

from __future__ import annotations

import sqlite3

import pytest

from media_preview_generator.loudness import plex_db
from media_preview_generator.loudness.undo import main as undo
from media_preview_generator.loudness.undo import restored
from media_preview_generator.markers.publishers.plex_db import decode_extra_data, encode_extra_data

from .test_plex_db import BEFORE, FIELDS, PLEX_ANALYSED, db  # noqa: F401 - the fixture


def _extra(path, stream_id):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT extra_data FROM media_streams WHERE id = ?", (stream_id,)).fetchone()[0]
    finally:
        conn.close()


def test_restores_what_it_wrote_and_leaves_what_changed_since(db, tmp_path):  # noqa: F811
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    plex_db.write_stream(db, 12, FIELDS, deadline=1e12)
    conn = sqlite3.connect(db._path())
    conn.execute("UPDATE media_streams SET extra_data = 'changed by Plex' WHERE id = 12")
    conn.commit()
    conn.close()
    log = str(tmp_path / "loudness-writes.jsonl")

    assert undo(["--db", db._path(), "--log", log, "--dry-run"]) == 0
    assert _extra(db._path(), 11) == PLEX_ANALYSED
    assert undo(["--db", db._path(), "--log", log]) == 0
    assert _extra(db._path(), 11) == BEFORE
    assert _extra(db._path(), 12) == "changed by Plex"


def test_the_items_mark_is_restored_with_its_tracks(db, tmp_path):  # noqa: F811
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    assert plex_db.mark_item(db, 1, deadline=1e12) is True
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 0
    conn = sqlite3.connect(db._path())
    try:
        assert conn.execute("SELECT extra_data FROM metadata_items WHERE id = 1").fetchone()[0] is None
    finally:
        conn.close()
    assert _extra(db._path(), 11) == BEFORE


def test_a_torn_last_line_is_skipped_and_the_rest_restored(db, tmp_path):  # noqa: F811
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    log = tmp_path / "loudness-writes.jsonl"
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"stream_id": 12, "bef')
    assert undo(["--db", db._path(), "--log", str(log)]) == 0
    assert _extra(db._path(), 11) == BEFORE


def test_a_mistyped_db_path_fails_and_creates_nothing(tmp_path):
    log = tmp_path / "loudness-writes.jsonl"
    log.write_text("")
    missing = tmp_path / "no-such.db"
    with pytest.raises(sqlite3.OperationalError):
        undo(["--db", str(missing), "--log", str(log)])
    assert not missing.exists()


def test_a_row_plex_changed_since_loses_only_the_fields_the_job_added(db, tmp_path):  # noqa: F811
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    assert plex_db.mark_item(db, 1, deadline=1e12) is True
    conn = sqlite3.connect(db._path())
    marked = conn.execute("SELECT extra_data FROM metadata_items WHERE id = 1").fetchone()[0]
    newer = encode_extra_data({**decode_extra_data(marked)[0], "pv:thumbBlurHash": "new poster"})
    conn.execute("UPDATE metadata_items SET extra_data = ? WHERE id = 1", (newer,))
    conn.commit()
    conn.close()
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 0
    conn = sqlite3.connect(db._path())
    try:
        item = conn.execute("SELECT extra_data FROM metadata_items WHERE id = 1").fetchone()[0]
    finally:
        conn.close()
    assert item == encode_extra_data({"pv:thumbBlurHash": "new poster"})  # the mark gone, Plex's change kept
    assert _extra(db._path(), 11) == BEFORE


def test_restored_leaves_a_row_whose_added_fields_changed():
    before = encode_extra_data({"ma:a": "1"})
    after = encode_extra_data({"ma:a": "1", "ln:loudness": "-20.00"})
    assert restored(before, after, after) == before
    assert restored(before, after, encode_extra_data({"ma:a": "2", "ln:loudness": "-20.00"})) == encode_extra_data(
        {"ma:a": "2"}
    )
    assert restored(before, after, encode_extra_data({"ma:a": "1", "ln:loudness": "-19.00"})) is False


def test_restored_leaves_a_row_it_cant_read():
    assert restored(None, encode_extra_data({"ln:loudness": "-20.00"}), "[1]") is False


def test_a_database_error_rolls_the_whole_undo_back(tmp_path):
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    log = tmp_path / "loudness-writes.jsonl"
    log.write_text('{"stream_id": 1, "before": null, "after": "x"}\n')
    with pytest.raises(sqlite3.OperationalError):
        undo(["--db", str(empty), "--log", str(log)])


def test_undo_refuses_while_plex_has_the_database_open(db, tmp_path, monkeypatch):  # noqa: F811
    from media_preview_generator.loudness import undo as undo_module

    monkeypatch.setattr(undo_module, "shm_lock_held_elsewhere", lambda path: True)
    assert undo(["--db", db._path(), "--log", str(tmp_path / "loudness-writes.jsonl")]) == 2
