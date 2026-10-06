"""The one write loudness makes: ln:* fields in media_streams.extra_data, byte for byte as Plex writes them."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.loudness import plex_db
from media_preview_generator.markers.publishers.base import DatabaseBusyError, PublishError
from media_preview_generator.markers.publishers.plex_db import LocalPlexDb, encode_extra_data, plex_db_path

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "markers"
# A stream Plex analysed itself (production 1.43.4, Puffin Rock S01E34-E36 stream 1, 2026-09-29).
PLEX_ANALYSED = (
    '{"ln:gainOffset":"0.21","ln:loudness":"-23.23","ln:loudnessAnalysisVersion":"0.02","ln:lra":"7.30",'
    '"ln:peak":"-8.57","ln:threshold":"-33.93","ma:audioChannelLayout":"stereo","ma:profile":"he-aac",'
    '"ma:samplingRate":"48000","ma:streamIdentifier":"2","url":"ln%3AgainOffset=0%2E21&ln%3Aloudness=-23%2E23&'
    "ln%3AloudnessAnalysisVersion=0%2E02&ln%3Alra=7%2E30&ln%3Apeak=-8%2E57&ln%3Athreshold=-33%2E93&"
    'ma%3AaudioChannelLayout=stereo&ma%3Aprofile=he-aac&ma%3AsamplingRate=48000&ma%3AstreamIdentifier=2"}'
)
FIELDS = {k: v for k, v in json.loads(PLEX_ANALYSED).items() if k.startswith("ln:")}
BEFORE = encode_extra_data({k: v for k, v in json.loads(PLEX_ANALYSED).items() if k.startswith("ma:")})
FILE = "/data/kids/Puffin Rock S01E34.mp4"
# metadata_items' title-update search triggers, from production 1.43.4 (2026-09-30). Their FTS table needs Plex's own
# tokenizer and is left out, so a write that fired one would fail with "no such table".
PLEX_ITEM_TRIGGERS = """
CREATE TRIGGER fts4_metadata_titles_before_update_icu BEFORE UPDATE OF title, title_sort, original_title ON
metadata_items BEGIN DELETE FROM fts4_metadata_titles_icu WHERE docid=old.rowid; END;
CREATE TRIGGER fts4_metadata_titles_after_update_icu AFTER UPDATE OF title, title_sort, original_title ON
metadata_items BEGIN INSERT INTO fts4_metadata_titles_icu(docid, title, title_sort, original_title)
VALUES(new.rowid, new.title, new.title_sort, new.original_title); END;
"""


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = Path(plex_db_path(str(tmp_path / "Plex Media Server")))
    path.parent.mkdir(parents=True)
    conn = sqlite3.connect(path)
    conn.executescript("BEGIN;\n" + (FIX / "plex_schema_1_43.sql").read_text() + PLEX_ITEM_TRIGGERS + "\nCOMMIT;")
    conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (1, 4, 'Ep')")
    conn.execute("INSERT INTO media_items (id, metadata_item_id, duration) VALUES (1, 1, 424000)")
    conn.execute("INSERT INTO media_parts (id, media_item_id, file) VALUES (1, 1, ?)", (FILE,))
    streams = [(10, 1, 0, "h264", None), (11, 2, 1, "aac", BEFORE), (12, 2, 2, "aac", None)]
    conn.executemany(
        'INSERT INTO media_streams (id, stream_type_id, "index", codec, extra_data, media_part_id, media_item_id) '
        "VALUES (?, ?, ?, ?, ?, 1, 1)",
        streams,
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    return LocalPlexDb(lambda: str(path))


def _extra(db, stream_id):
    conn = sqlite3.connect(db._path())
    try:
        return conn.execute("SELECT extra_data FROM media_streams WHERE id = ?", (stream_id,)).fetchone()[0]
    finally:
        conn.close()


def test_reads_only_audio_streams_of_the_live_part(db):
    streams, in_plex = plex_db.read_streams(db, [FILE, "/other.mkv"], deadline=1e12)
    assert in_plex is True
    assert plex_db.read_streams(db, ["/other.mkv"], deadline=1e12) == ([], False)
    assert [(s.id, s.index, s.codec, s.duration_ms) for s in streams] == [
        (11, 1, "aac", 424000),
        (12, 2, "aac", 424000),
    ]
    assert all(plex_db.needs_analysis(s) for s in streams)


def test_write_is_byte_identical_to_plexs_own(db):
    assert plex_db.write_stream(db, 11, FIELDS, deadline=1e12) is True
    assert _extra(db, 11) == PLEX_ANALYSED


@pytest.mark.parametrize("abort", [False, True], ids=["commit", "rollback"])
def test_native_title_control_is_preserved_by_atomic_loudness_write(db, tmp_path, monkeypatch, abort):
    original = {"ma:title": "Commentary with Person A \x16and Person B", "ma:profile": "he-aac"}
    canonical = encode_extra_data(original)
    raw = canonical.replace("\\u0016", "\x16")
    with sqlite3.connect(db._path()) as conn:
        conn.execute("UPDATE media_streams SET extra_data=? WHERE id=11", (raw,))
    if abort:

        def fail_receipt(*args, **kwargs):
            raise OSError("Receipt disk unavailable")

        monkeypatch.setattr(plex_db, "_log_write", fail_receipt)
        with pytest.raises(OSError, match="Receipt disk unavailable"):
            plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
        assert _extra(db, 11) == raw
        return

    assert plex_db.write_stream(db, 11, FIELDS, deadline=1e12) is True
    after = _extra(db, 11)
    expected = json.loads(encode_extra_data({**original, **FIELDS}))
    assert json.loads(after) == expected
    assert "\x16" not in after and "\\u0016" in after
    record, receipt = [json.loads(line) for line in (tmp_path / "loudness-writes.jsonl").read_text().splitlines()]
    assert record["before"] == raw and record["after"] == after
    assert receipt["transaction_id"] == record["transaction_id"]


def test_write_over_null_extra_data(db):
    assert plex_db.write_stream(db, 12, FIELDS, deadline=1e12) is True
    assert json.loads(_extra(db, 12))["ln:loudness"] == "-23.23"


def test_second_write_changes_nothing(db):
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    other = {**FIELDS, "ln:loudness": "-99.00"}
    assert plex_db.write_stream(db, 11, other, deadline=1e12) is False
    assert _extra(db, 11) == PLEX_ANALYSED


def test_each_write_is_logged_for_undo(db, tmp_path):
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    record, receipt = [json.loads(line) for line in (tmp_path / "loudness-writes.jsonl").read_text().splitlines()]
    assert (record["stream_id"], record["before"], record["after"]) == (11, BEFORE, PLEX_ANALYSED)
    assert (record["kind"], receipt["kind"]) == ("intent", "commit")
    assert record["transaction_id"] == receipt["transaction_id"]


def test_trigger_on_media_streams_refuses_the_write(db):
    conn = sqlite3.connect(db._path())
    conn.execute("CREATE TRIGGER t AFTER UPDATE ON media_streams BEGIN SELECT 1; END")
    conn.commit()
    conn.close()
    with pytest.raises(PublishError, match="trigger on media_streams"):
        plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    assert _extra(db, 11) == BEFORE


def test_missing_stream_is_an_error(db):
    with pytest.raises(PublishError, match="gone"):
        plex_db.write_stream(db, 999, FIELDS, deadline=1e12)


def test_a_database_another_program_keeps_locked_is_busy_and_nothing_is_written(db):  # noqa: F811
    holder = sqlite3.connect(db._path(), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(DatabaseBusyError):
            plex_db.write_stream(db, 11, FIELDS, deadline=time.monotonic() + 1.5)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert _extra(db, 11) == BEFORE


def test_a_url_form_row_stays_in_url_form(db):  # noqa: F811
    url_form = encode_extra_data(json.loads(BEFORE), url_form=True)
    conn = sqlite3.connect(db._path())
    conn.execute("UPDATE media_streams SET extra_data = ? WHERE id = 11", (url_form,))
    conn.commit()
    conn.close()
    assert plex_db.write_stream(db, 11, FIELDS, deadline=1e12) is True
    expected = {k: v for k, v in json.loads(PLEX_ANALYSED).items() if k != "url"}
    assert _extra(db, 11) == encode_extra_data(expected, url_form=True)


def _item_extra(db):
    conn = sqlite3.connect(db._path())
    try:
        return conn.execute("SELECT extra_data FROM metadata_items WHERE id = 1").fetchone()[0]
    finally:
        conn.close()


def test_streams_carry_their_item_and_whether_plex_counts_it_analysed(db):
    streams, _ = plex_db.read_streams(db, [FILE], deadline=1e12)
    assert {(s.metadata_item_id, s.item_marked) for s in streams} == {(1, False)}


def test_the_item_is_marked_once_every_audio_stream_is_done_keeping_its_other_fields(db, tmp_path):
    conn = sqlite3.connect(db._path())
    conn.execute("""UPDATE metadata_items SET extra_data = '{"pv:thumbBlurHash":"abc"}' WHERE id = 1""")
    conn.commit()
    conn.close()
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    assert plex_db.mark_item(db, 1, deadline=1e12) is False  # stream 12 still lacks loudness
    plex_db.write_stream(db, 12, FIELDS, deadline=1e12)
    assert plex_db.mark_item(db, 1, deadline=1e12) is True  # Plex's title triggers didn't fire (no FTS table here)
    # As Plex writes its own items' extra_data: sorted keys and the trailing url field.
    assert _item_extra(db) == encode_extra_data({"ln:loudnessAnalysisVersion": "0.02", "pv:thumbBlurHash": "abc"})
    assert plex_db.read_streams(db, [FILE], deadline=1e12)[0][0].item_marked is True
    assert plex_db.mark_item(db, 1, deadline=1e12) is False
    record = json.loads((tmp_path / "loudness-writes.jsonl").read_text().splitlines()[-2])
    assert (record["metadata_item_id"], record["before"]) == (1, '{"pv:thumbBlurHash":"abc"}')


@pytest.mark.parametrize(
    "trigger",
    [
        "CREATE TRIGGER t AFTER UPDATE ON metadata_items BEGIN SELECT 1; END",
        "CREATE TRIGGER t AFTER UPDATE OF title, extra_data ON metadata_items BEGIN SELECT 1; END",
        "CREATE TRIGGER t AFTER UPDATE OF EXTRA_DATA ON metadata_items BEGIN SELECT 1; END",
    ],
)
def test_a_trigger_that_could_fire_on_the_items_extra_data_refuses_every_write(db, trigger):
    conn = sqlite3.connect(db._path())
    conn.execute(trigger)
    conn.commit()
    conn.close()
    with pytest.raises(PublishError, match="trigger on metadata_items"):
        plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    assert _extra(db, 11) == BEFORE and _item_extra(db) is None


def _add_version(db, part_id, file, *, proxy_type=None, deleted=False, index=1):
    conn = sqlite3.connect(db._path())
    conn.execute(
        "INSERT INTO media_items (id, metadata_item_id, duration, proxy_type, deleted_at) VALUES (?, 1, 1, ?, ?)",
        (part_id, proxy_type, 1 if deleted else None),
    )
    conn.execute("INSERT INTO media_parts (id, media_item_id, file) VALUES (?, ?, ?)", (part_id, part_id, file))
    conn.execute(
        'INSERT INTO media_streams (id, stream_type_id, "index", codec, media_part_id, media_item_id) '
        "VALUES (?, 2, ?, 'aac', ?, ?)",
        (part_id * 10, index, part_id, part_id),
    )
    conn.commit()
    conn.close()


def test_a_file_plex_has_without_audio_is_in_plex_with_no_streams(db):
    conn = sqlite3.connect(db._path())
    conn.execute("DELETE FROM media_streams WHERE stream_type_id = 2")
    conn.commit()
    conn.close()
    assert plex_db.read_streams(db, [FILE], deadline=1e12) == ([], True)


def test_the_item_waits_for_every_version_but_not_deleted_optimized_or_indexless_ones(db):
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    _add_version(db, 2, "/data/kids/Puffin Rock S01E34 4K.mkv")
    _add_version(db, 3, "/data/kids/Plex Versions/Optimized for TV/Puffin Rock S01E34.mp4", proxy_type=42)
    _add_version(db, 4, "/data/kids/Puffin Rock S01E34 old.mkv", deleted=True)
    _add_version(db, 5, "/data/kids/Puffin Rock S01E34 odd.mkv", index=None)
    assert plex_db.mark_item(db, 1, deadline=1e12) is False  # the 4K version is still to do
    plex_db.write_stream(db, 20, FIELDS, deadline=1e12)
    assert plex_db.mark_item(db, 1, deadline=1e12) is True


def test_a_database_lacking_the_tested_tables_is_refused():
    with pytest.raises(PublishError, match="looks different from the tested version"):
        plex_db.check_schema(sqlite3.connect(":memory:"))


def test_no_paths_is_nothing_in_plex(db):
    assert plex_db.read_streams(db, [], deadline=1e12) == ([], False)


def test_a_database_error_while_reading_or_writing_is_a_publish_error(db, monkeypatch):
    monkeypatch.setattr(plex_db, "check_schema", MagicMock(side_effect=sqlite3.OperationalError("disk I/O error")))
    with pytest.raises(PublishError):
        plex_db.read_streams(db, [FILE], deadline=1e12)
    with pytest.raises(PublishError):
        plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    with pytest.raises(PublishError):
        plex_db.mark_item(db, 1, deadline=1e12)


def test_a_write_the_read_back_doesnt_show_is_an_error(db, monkeypatch):
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    monkeypatch.setattr(plex_db, "_same_extra_data", lambda stored, written: False)
    conn = sqlite3.connect(db._path())
    conn.execute("UPDATE media_streams SET extra_data = ? WHERE id = 12", (BEFORE,))
    conn.commit()
    conn.close()
    with pytest.raises(PublishError, match="doesn't show the loudness just written"):
        plex_db.write_stream(db, 12, FIELDS, deadline=1e12)
    with pytest.raises(PublishError, match="doesn't show the loudness mark"):
        plex_db.mark_item(db, 1, deadline=1e12)


def test_a_missing_item_is_an_error_and_a_failed_mark_writes_nothing(db, monkeypatch):
    with pytest.raises(PublishError, match="gone"):
        plex_db.mark_item(db, 999, deadline=1e12)
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    monkeypatch.setattr(plex_db, "encode_extra_data", MagicMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        plex_db.mark_item(db, 1, deadline=1e12)
    assert _item_extra(db) is None


def test_a_write_whose_undo_record_cant_be_kept_rolls_back(db, tmp_path, monkeypatch):
    monkeypatch.setattr(plex_db, "write_log_path", lambda: str(tmp_path))  # a folder: open() fails
    with pytest.raises(PublishError, match="undo record"):
        plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    assert _extra(db, 11) == BEFORE


@pytest.mark.parametrize(
    "extra",
    [
        {"ln:loudness": "-20"},
        {**FIELDS, "ln:loudnessAnalysisVersion": "99"},
        {**FIELDS, "ln:peak": "nan"},
    ],
)
def test_partial_or_unsupported_native_metadata_is_preserved(db, extra):
    native = encode_extra_data(extra)
    with sqlite3.connect(db._path()) as conn:
        conn.execute("UPDATE media_streams SET extra_data = ? WHERE id = 11", (native,))
    conn.close()
    with pytest.raises(PublishError, match="incomplete or unsupported"):
        plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    assert _extra(db, 11) == native


@pytest.mark.parametrize("peak", ["-inf", "-16.32"])
def test_native_silent_or_short_measurements_are_complete(db, peak):
    fields = {**FIELDS, "ln:loudness": "-inf", "ln:gainOffset": "inf", "ln:peak": peak}
    assert plex_db.write_stream(db, 11, fields, deadline=1e12)
    assert plex_db.has_analysis(_extra(db, 11))
    assert not plex_db.write_stream(db, 11, FIELDS, deadline=1e12)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE media_parts SET hash = 'replacement' WHERE id = 1",
        "UPDATE media_parts SET file = '/new/location.mkv' WHERE id = 1",
        "UPDATE media_streams SET codec = 'eac3' WHERE id = 11",
        'UPDATE media_streams SET "index" = 7 WHERE id = 11',
        "UPDATE media_streams SET created_at = 1234 WHERE id = 11",
        "UPDATE media_parts SET deleted_at = 1234 WHERE id = 1",
    ],
)
def test_write_refuses_a_changed_indexed_audio_target(db, mutation):
    stream = plex_db.read_streams(db, [FILE], deadline=1e12)[0][0]
    with sqlite3.connect(db._path()) as conn:
        conn.execute(mutation)
    conn.close()
    with pytest.raises(plex_db.SourceChangedError, match="source changed"):
        plex_db.write_stream(db, 11, FIELDS, deadline=1e12, expected=stream)
    assert _extra(db, 11) == BEFORE


def test_source_bytes_changed_during_analysis_are_not_written(db, tmp_path):
    source = tmp_path / "local.mkv"
    source.write_bytes(b"old")
    fingerprint = plex_db.SourceFingerprint.read(str(source))
    source.write_bytes(b"new")
    with pytest.raises(plex_db.SourceChangedError, match="source changed"):
        plex_db.write_stream(db, 11, FIELDS, deadline=1e12, source=fingerprint)
    assert _extra(db, 11) == BEFORE


def test_item_mark_refuses_a_version_added_after_analysis_started(db):
    snapshot = plex_db.read_item_snapshot(db, 1, deadline=1e12)
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    _add_version(db, 2, "/data/kids/new-version.mkv")
    plex_db.write_stream(db, 20, FIELDS, deadline=1e12)
    with pytest.raises(plex_db.SourceChangedError, match="versions changed"):
        plex_db.mark_item(db, 1, deadline=1e12, expected=snapshot)
    assert _item_extra(db) is None
    assert plex_db.mark_item(db, 1, deadline=1e12, expected=plex_db.read_item_snapshot(db, 1, deadline=1e12))


def test_item_mark_rolls_back_when_journal_cannot_be_saved(db, tmp_path, monkeypatch):
    for stream_id in (11, 12):
        plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    monkeypatch.setattr(plex_db, "write_log_path", lambda: str(tmp_path))
    with pytest.raises(PublishError, match="undo record"):
        plex_db.mark_item(db, 1, deadline=1e12)
    assert _item_extra(db) is None


def test_journal_contains_database_and_row_identity(db, tmp_path):
    plex_db.write_stream(db, 11, FIELDS, deadline=1e12)
    record = json.loads((tmp_path / "loudness-writes.jsonl").read_text().splitlines()[0])
    assert record["format"] == 2
    assert record["database"] == plex_db.database_identity(db._path())
    with sqlite3.connect(db._path()) as conn:
        assert record["target"] == plex_db.undo_target(conn, {"stream_id": 11})
    conn.close()


def test_music_items_are_refused_without_mutation(db):
    with sqlite3.connect(db._path()) as conn:
        conn.execute("UPDATE metadata_items SET metadata_type = 10 WHERE id = 1")
    conn.close()
    for operation in (
        lambda: plex_db.read_streams(db, [FILE], deadline=1e12),
        lambda: plex_db.write_stream(db, 11, FIELDS, deadline=1e12),
        lambda: plex_db.mark_item(db, 1, deadline=1e12),
    ):
        with pytest.raises(PublishError):
            operation()
    assert _extra(db, 11) == BEFORE
    assert _item_extra(db) is None


@pytest.mark.parametrize("operation", ["stream", "item"])
def test_cancellation_after_transaction_admission_prevents_mutation(db, tmp_path, monkeypatch, operation):
    if operation == "item":
        for stream_id in (11, 12):
            plex_db.write_stream(db, stream_id, FIELDS, deadline=1e12)
    log = tmp_path / "loudness-writes.jsonl"
    before_log = log.read_text() if log.exists() else ""
    cancelled = False
    original_connect = db._connect

    class CancelOnBegin:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, *args):
            nonlocal cancelled
            result = self.connection.execute(statement, *args)
            if statement == "BEGIN IMMEDIATE":
                cancelled = True
            return result

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(db, "_connect", lambda **kwargs: CancelOnBegin(original_connect(**kwargs)))
    with pytest.raises(PublishError, match="cancelled"):
        if operation == "stream":
            plex_db.write_stream(db, 11, FIELDS, deadline=1e12, cancel_check=lambda: cancelled)
        else:
            plex_db.mark_item(db, 1, deadline=1e12, cancel_check=lambda: cancelled)
    assert cancelled
    assert _item_extra(db) is None
    assert _extra(db, 11) == (BEFORE if operation == "stream" else PLEX_ANALYSED)
    assert (log.read_text() if log.exists() else "") == before_log
