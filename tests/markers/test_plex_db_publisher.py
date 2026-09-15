"""PlexMarkerPublisher against a real SQLite file with Plex 1.43's schema (spec §3.1)."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.models import Marker, MarkerType
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.base import (
    Capability,
    ItemNotFoundError,
    MarkerPublisher,
    PublishError,
    Shown,
)
from media_preview_generator.markers.publishers.plex_db import (
    PlexMarkerPublisher,
    encode_extra_data,
    merge_part_extra_data,
    plex_db_path,
)
from media_preview_generator.markers.settings import ServerMarkersSettings
from media_preview_generator.servers.base import ServerConfig, ServerType

T = MarkerType
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "markers"
DUR = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))
CREDITS_FINAL = Marker(T.CREDITS, 1_299_000, DUR, ("chapters",))
CREDITS_NONFINAL = Marker(T.CREDITS, 1_200_000, 1_250_000, ("theintrodb", "skipdb"))
NATIVE_INTROS = (
    '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
    '"MediaPartMarker":[{"startTimeOffset":990,"endTimeOffset":29306}]}}'
)
NATIVE_CREDITS = (
    '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
    '"MediaPartMarker":[{"startTimeOffset":1154521,"endTimeOffset":1188521}]}}'
)


def _plex_json(d: dict) -> str:
    # Plex stores compact JSON; the capability sample's LIKE '%"pv:intros":"{%' only matches that form.
    return json.dumps(d, separators=(",", ":"))


BOTH_NATIVE = _plex_json({"pv:credits": NATIVE_CREDITS, "pv:intros": NATIVE_INTROS, "url": "z"})
INTROS_V6 = _plex_json({"pv:intros": NATIVE_INTROS.replace('"version":5', '"version":6')})
# What we store for INTRO / CREDITS_FINAL (credits start = served - 2000 ms).
INTRO_PAYLOAD = (
    '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
    '"MediaPartMarker":[{"startTimeOffset":11000,"endTimeOffset":37000}]}}'
)
CREDITS_FINAL_PAYLOAD = (
    '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
    '"MediaPartMarker":[{"startTimeOffset":1297000,"endTimeOffset":1320000,"final":true}]}}'
)
INTRO_ROW_EXTRA = '{"pv:version":"5","url":"pv%3Aversion=5"}'
CREDITS_ROW_EXTRA = '{"pv:version":"4","url":"pv%3Aversion=4"}'
CREDITS_FINAL_ROW_EXTRA = '{"pv:final":"1","pv:version":"4","url":"pv%3Afinal=1&pv%3Aversion=4"}'
_JOURNAL_MODE = "delete"
PLEX_VERSION = "1.43.4.10903-e5521bd8c"


@pytest.fixture(autouse=True, params=["delete", "wal"])
def journal_mode(request, monkeypatch):
    """Every DB test runs on both journal modes: SQLite's default and WAL, which Plex uses."""
    monkeypatch.setitem(globals(), "_JOURNAL_MODE", request.param)
    return request.param


@pytest.fixture(autouse=True)
def plex_holds_the_database(monkeypatch):
    """Stand in for Plex having its DB open; TestLockDomain overrides this and uses the real probe."""
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)


@pytest.fixture
def sql_log(monkeypatch):
    """Every statement the publisher runs, with bound values expanded."""
    log: list[str] = []
    original = PlexMarkerPublisher._connect

    def connect(self, *, read_only, **kwargs):
        conn = original(self, read_only=read_only, **kwargs)
        conn.set_trace_callback(log.append)
        return conn

    monkeypatch.setattr(PlexMarkerPublisher, "_connect", connect)
    return log


def _writes(log: list[str]) -> list[str]:
    return [sql for sql in log if sql.split(None, 1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}]


def _make_db(
    folder: Path,
    *,
    tag_row: str | None = "''",
    parts=(("/data/tv/S01E01.mkv", None),),
    metadata_item_id=7,
    journal_mode: str | None = None,
):
    db = Path(plex_db_path(str(folder)))
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode={journal_mode or _JOURNAL_MODE}")
    conn.executescript((FIX / "plex_schema_1_43.sql").read_text())
    conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (?, 4, 'Ep')", (metadata_item_id,))
    if tag_row is not None:
        conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (562, NULL, 12)")
        if tag_row == "''":
            conn.execute("INSERT INTO tags (id, tag, tag_type) VALUES (563, '', 12)")
    for n, (file, extra) in enumerate(parts, start=1):
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (?, ?)", (n, metadata_item_id))
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (?, ?, ?, ?)", (n, n, file, extra)
        )
    conn.commit()
    conn.close()
    return db


def _mountinfo(tmp_path, fs="ext4", extra_lines=""):
    p = tmp_path / "mountinfo"
    p.write_text(f"22 1 259:2 / / rw - {fs} /dev/root rw\n{extra_lines}")
    return str(p)


def _publisher(
    tmp_path,
    folder,
    *,
    enabled=True,
    confirmed="2026-09-13T00:00:00+00:00",
    fs="ext4",
    plex_pass=True,
    sibling_markers=None,
    mappings=None,
    mountinfo_lines="",
    mountinfo_path=None,
    settings_provider=None,
    redetect="restore",
):
    server = MagicMock()
    server.get_server_status.return_value = (
        None if plex_pass is None else {"plex_pass": plex_pass, "version": PLEX_VERSION}
    )
    server.get_marker_detection_prefs.return_value = {"intro": "never", "credits": "never"}
    cfg = ServerConfig(
        id="plex-1",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://p",
        auth={},
        output={"plex_config_folder": str(folder)},
        path_mappings=mappings or [],
    )
    settings = ServerMarkersSettings(enabled, None, confirmed, redetect)
    return PlexMarkerPublisher(
        server,
        cfg,
        settings,
        sibling_markers=sibling_markers,
        settings_provider=settings_provider,
        mountinfo_path=mountinfo_path or _mountinfo(tmp_path, fs, mountinfo_lines),
    )


def _rows(db, sql, *params):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _served(db, item=7):
    """What Plex serves for the item: its intro/credits rows on the marker tag, in served times (start order)."""
    rows = _rows(
        db,
        "SELECT text, time_offset, end_time_offset, t.extra_data FROM taggings t JOIN tags g ON g.id = t.tag_id "
        "WHERE t.metadata_item_id=? AND g.tag_type=12 AND g.tag='' AND t.text IN ('intro','credits') "
        "ORDER BY t.time_offset",
        item,
    )
    out = []
    for text, start, end, extra in rows:
        mtype = MarkerType.INTRO if text == "intro" else MarkerType.CREDITS
        out.append((mtype, *plex_db._served_times(mtype, int(start), int(end), plex_db._row_is_final(extra))))
    return out


class TestExtraDataEncoding:
    def test_rebuilding_native_extra_data_is_byte_identical(self):
        native = (FIX / "plex_part_extra_data_native.json").read_text().strip()
        d = json.loads(native)
        d.pop("url")
        assert encode_extra_data(d) == native

    def test_merge_writes_both_keys_and_rebuilds_url(self):
        native = (FIX / "plex_part_extra_data_native.json").read_text().strip()
        out = json.loads(merge_part_extra_data(native, [INTRO, CREDITS_FINAL], {T.INTRO, T.CREDITS}, DUR))
        assert out["pv:intros"] == (
            '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
            '"MediaPartMarker":[{"startTimeOffset":11000,"endTimeOffset":37000}]}}'
        )
        assert out["pv:credits"] == (
            '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
            '"MediaPartMarker":[{"startTimeOffset":1297000,"endTimeOffset":1320000,"final":true}]}}'
        )
        assert out["ma:container"] == "mkv" and out["pv:deepAnalysisDate"] == "1651985592"
        assert list(out) == sorted(k for k in out if k != "url") + ["url"]
        rebuilt = dict(out)
        rebuilt.pop("url")
        assert json.loads(encode_extra_data(rebuilt))["url"] == out["url"]
        assert "pv%3Aintros=%7B%22MediaPartMarkersArray" in out["url"]

    @pytest.mark.parametrize(
        ("existing", "wanted", "managed", "previous", "intros", "credits"),
        [
            (None, [INTRO], {T.INTRO}, [], "set", "absent"),
            ("", [CREDITS_FINAL], {T.CREDITS}, [], "absent", "set"),
            (BOTH_NATIVE, [INTRO], {T.INTRO}, [], "set", "keep"),
            # We removed our intro: the key goes, so Plex's own detection may analyse the part again.
            (_plex_json({"pv:intros": INTRO_PAYLOAD, "url": "z"}), [], {T.INTRO}, [INTRO], "absent", "absent"),
            # Plex re-detected since we published: its intro stays.
            (BOTH_NATIVE, [], {T.INTRO}, [INTRO], "native", "keep"),
            (_plex_json({"pv:intros": "", "url": "z"}), [INTRO], {T.INTRO}, [], "set", "absent"),  # cleared earlier
            (BOTH_NATIVE, [INTRO, CREDITS_NONFINAL], {T.INTRO, T.CREDITS}, [], "set", "set"),
        ],
        ids=["add-intro", "add-credits", "keep-other-type", "remove-ours", "keep-redetected", "over-empty", "both"],
    )
    def test_merge_matrix(self, existing, wanted, managed, previous, intros, credits):
        out = json.loads(merge_part_extra_data(existing, wanted, managed, DUR, previous=previous))
        expect = {
            "set": lambda v: v.startswith('{"MediaPartMarkersArray"') and v not in (NATIVE_INTROS, NATIVE_CREDITS),
            "absent": lambda v: v is None,
            "keep": lambda v: v == NATIVE_CREDITS,
            "native": lambda v: v == NATIVE_INTROS,
        }
        assert expect[intros](out.get("pv:intros"))
        assert expect[credits](out.get("pv:credits"))
        assert "url" in out

    def test_non_final_credits_shifts_end_and_omits_final(self):
        out = json.loads(merge_part_extra_data(None, [CREDITS_NONFINAL], {T.CREDITS}, DUR))
        marker = json.loads(out["pv:credits"])["MediaPartMarkersArray"]["MediaPartMarker"]
        assert marker == [{"startTimeOffset": 1_198_000, "endTimeOffset": 1_252_000}]

    @pytest.mark.parametrize(
        "existing",
        [
            "{not json",
            "[1, 2]",
            INTROS_V6,  # the type we write
            _plex_json({"pv:credits": NATIVE_CREDITS.replace('"version":4', '"version":5')}),  # a type we only keep
            _plex_json({"pv:credits": NATIVE_CREDITS.replace('"credits","version"', '"outros","version"')}),
            _plex_json({"pv:intros": "not json"}),
        ],
        ids=["not-json", "not-object", "intro-v6", "credits-v5-kept", "wrong-attribute", "marker-value-not-json"],
    )
    def test_unknown_existing_data_raises_unsupported_schema(self, existing):
        with pytest.raises(PublishError) as ei:
            merge_part_extra_data(existing, [INTRO], {T.INTRO}, DUR)
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA


class TestWrite:
    def test_writes_taggings_and_parts_and_never_touches_tags(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        tags_before = _rows(db, "SELECT * FROM tags ORDER BY id")
        pub = _publisher(tmp_path, folder)
        ours = pub.write(
            "7", [CREDITS_FINAL, INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
        )
        assert ours == [INTRO, CREDITS_FINAL]  # sorted by start
        rows = _rows(
            db,
            "SELECT metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
            "typeof(created_at), extra_data FROM taggings ORDER BY [index]",
        )
        # Plex's own numbering (lab DB, spec §3.1): by text, then start, so credits come before intro.
        assert rows == [
            (7, 563, 0, "credits", 1_297_000, DUR, "", "integer", CREDITS_FINAL_ROW_EXTRA),
            (7, 563, 1, "intro", 11_000, 37_000, "", "integer", INTRO_ROW_EXTRA),
        ]
        assert _rows(db, "SELECT * FROM tags ORDER BY id") == tags_before
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert json.loads(extra["pv:intros"])["MediaPartMarkersArray"]["MediaPartMarker"] == [
            {"startTimeOffset": 11_000, "endTimeOffset": 37_000}
        ]

    def test_replaces_native_rows_of_managed_types_only(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, "
            "thumb_url, created_at, extra_data) VALUES (7, 563, 0, 'intro', 76508, 112748, '', 1, 'n'), "
            "(7, 563, 1, 'credits', 1264953, 1296953, '', 1, 'n'), (7, 999, 0, 'bookmark', 5, 6, '', 1, 'b'), "
            "(7, 999, 1, 'intro', 7, 8, '', 1, 'other tag'), (8, 563, 0, 'intro', 1, 2, '', 1, 'other item')"
        )
        conn.commit()
        conn.close()
        _publisher(tmp_path, folder).write(
            "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
        )
        rows = _rows(
            db,
            "SELECT metadata_item_id, tag_id, text, time_offset, extra_data FROM taggings ORDER BY metadata_item_id, tag_id, text",
        )
        assert rows == [
            (7, 563, "credits", 1264953, "n"),  # Plex's own credits stay: we have no credits decision
            (7, 563, "intro", 11_000, '{"pv:version":"5","url":"pv%3Aversion=5"}'),
            (7, 999, "bookmark", 5, "b"),
            (7, 999, "intro", 7, "other tag"),  # same text on another tag: not a marker row
            (8, 563, "intro", 1, "other item"),
        ]

    def test_previously_published_type_now_undecided_is_removed(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        pub.write("7", [INTRO, CREDITS_FINAL], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        pub.write("7", [INTRO], previous=[INTRO, CREDITS_FINAL], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv")
        assert [r[0] for r in _rows(db, "SELECT text FROM taggings")] == ["intro"]
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert "pv:credits" not in extra and extra["pv:intros"] == INTRO_PAYLOAD

    @pytest.mark.parametrize(
        ("tag_row", "state"),
        [(None, Capability.NEEDS_PLEX_DETECTION_ONCE), ("null-only", Capability.NEEDS_PLEX_DETECTION_ONCE)],
    )
    def test_missing_marker_tag_row_never_creates_one(self, tmp_path, tag_row, state):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, tag_row=tag_row)
        before = _rows(db, "SELECT COUNT(*) FROM tags")[0][0]
        with pytest.raises(PublishError) as ei:
            _publisher(tmp_path, folder).write(
                "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
            )
        assert ei.value.state is state
        assert _rows(db, "SELECT COUNT(*) FROM tags")[0][0] == before
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_unknown_item_raises_not_found(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        with pytest.raises(PublishError, match="not found"):
            _publisher(tmp_path, folder).write("999", [INTRO], previous=[], duration_ms=DUR, canonical_path="/x.mkv")

    def test_missing_column_is_unsupported_schema_and_writes_nothing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE taggings RENAME COLUMN end_time_offset TO end_offset_ms")
        conn.commit()
        conn.close()
        with pytest.raises(PublishError) as ei:
            _publisher(tmp_path, folder).write(
                "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
            )
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA

    def test_unknown_native_marker_version_on_the_item_is_unsupported_schema(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", INTROS_V6),))
        with pytest.raises(PublishError) as ei:
            _publisher(tmp_path, folder).write(
                "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
            )
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(INTROS_V6,)]

    def test_failure_mid_transaction_rolls_back(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, "
            "thumb_url, created_at, extra_data) VALUES (7, 563, 0, 'intro', 76508, 112748, '', 1, 'n')"
        )
        conn.commit()
        conn.close()
        executed = []
        original = PlexMarkerPublisher._connect

        def connect(self, *, read_only, **kwargs):
            # Refuse the media_parts UPDATE, which runs after the DELETE and INSERT: both must be rolled back.
            c = original(self, read_only=read_only, **kwargs)
            c.set_trace_callback(executed.append)
            c.set_authorizer(
                lambda action, table, *_: (
                    sqlite3.SQLITE_DENY
                    if table == "media_parts" and action == sqlite3.SQLITE_UPDATE
                    else sqlite3.SQLITE_OK
                )
            )
            return c

        monkeypatch.setattr(PlexMarkerPublisher, "_connect", connect)
        with pytest.raises(PublishError):
            _publisher(tmp_path, folder).write(
                "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
            )
        assert any(sql.startswith("DELETE FROM taggings") for sql in executed)
        assert any(sql.startswith("INSERT INTO taggings") for sql in executed)
        assert executed[-1] == "ROLLBACK"
        assert _rows(db, "SELECT text, time_offset FROM taggings") == [("intro", 76508)]
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(None,)]

    def test_merges_extra_data_plex_wrote_while_we_waited_for_the_lock(self, tmp_path):
        # Plex analyses a new file right when the webhook follow-up runs; its fresh ma:* keys must survive our write.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", '{"ma:container":"mkv","url":"ma%3Acontainer=mkv"}'),))
        locker = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        locker.execute(
            "UPDATE media_parts SET extra_data=? WHERE id=1",
            ('{"ma:container":"mkv","ma:x":"1","url":"ma%3Acontainer=mkv&ma%3Ax=1"}',),
        )
        threading.Timer(0.5, lambda: (locker.execute("COMMIT"), locker.close())).start()
        start = time.monotonic()
        _publisher(tmp_path, folder).write(
            "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
        )
        assert time.monotonic() - start >= 0.4
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 1
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert extra["ma:x"] == "1" and extra["ma:container"] == "mkv"
        assert json.loads(extra["pv:intros"])["MediaPartMarkersArray"]["MediaPartMarker"] == [
            {"startTimeOffset": 11_000, "endTimeOffset": 37_000}
        ]

    def test_parts_changed_while_waiting_for_the_lock_writes_nothing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        locker = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("UPDATE media_parts SET file='/data/tv/S01E01.PROPER.mkv' WHERE id=1")
        threading.Timer(0.3, lambda: (locker.execute("COMMIT"), locker.close())).start()
        with pytest.raises(PublishError, match="changed") as ei:
            _publisher(tmp_path, folder).write(
                "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
            )
        assert ei.value.state is None
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(None,)]


class TestMultiVersion:
    PARTS = (("/data/tv/S01E01 - 1080p.mkv", None), ("/data/tv/S01E01 - 2160p.mkv", None))

    def _write(self, tmp_path, sibling):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=self.PARTS)
        pub = _publisher(tmp_path, folder, sibling_markers=lambda path: sibling if path.endswith("2160p.mkv") else None)
        return db, pub

    def test_all_versions_agree_within_2s_writes_every_part(self, tmp_path):
        sibling = {T.INTRO: Marker(T.INTRO, 12_500, 38_900, ("chapters",))}
        db, pub = self._write(tmp_path, sibling)
        ours = pub.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01 - 1080p.mkv")
        assert ours == [INTRO]  # the calling file's times
        assert [json.loads(r[0])["pv:intros"] for r in _rows(db, "SELECT extra_data FROM media_parts ORDER BY id")] == [
            INTRO_PAYLOAD,
            INTRO_PAYLOAD,
        ]
        assert _rows(db, "SELECT text, time_offset, end_time_offset FROM taggings") == [("intro", 11_000, 37_000)]

    @pytest.mark.parametrize(
        "sibling",
        [None, {}, {T.INTRO: Marker(T.INTRO, 14_000, 40_000, ("chapters",))}],
        ids=["undecided", "decided-without-intro", "disagrees"],
    )
    def test_a_type_not_every_version_agrees_on_is_not_written(self, tmp_path, sibling):
        db, pub = self._write(tmp_path, sibling)
        assert pub.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01 - 1080p.mkv") == []
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_stacked_parts_in_one_version_are_refused(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO media_parts (id, media_item_id, file) VALUES (2, 1, '/data/tv/S01E01-cd2.mkv')")
        conn.commit()
        conn.close()
        with pytest.raises(PublishError, match="stacked"):
            _publisher(tmp_path, folder).write(
                "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv"
            )

    def test_part_paths_are_path_mapped_before_comparing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/plexmedia/tv/A.mkv", None), ("/plexmedia/tv/B.mkv", None)))
        seen = []
        sibling = {T.INTRO: INTRO}
        pub = _publisher(
            tmp_path,
            folder,
            mappings=[{"plex_prefix": "/plexmedia", "local_prefix": "/data"}],
            sibling_markers=lambda p: (seen.append(p), sibling)[1],
        )
        assert pub.write("7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/A.mkv") == [INTRO]
        assert seen == ["/data/tv/B.mkv"]
        assert [json.loads(r[0])["pv:intros"] for r in _rows(db, "SELECT extra_data FROM media_parts ORDER BY id")] == [
            INTRO_PAYLOAD,
            INTRO_PAYLOAD,
        ]


class TestSavedSettingsBeforeEveryWrite:
    """With a ``settings_provider`` the switch and the confirmation are read at each check, not when the job started
    (audit C MED-2)."""

    @pytest.mark.parametrize(
        ("saved", "state"),
        [
            (ServerMarkersSettings(False, None, "2026-09-13T00:00:00+00:00", "restore"), Capability.DISABLED),
            (ServerMarkersSettings(True, None, None, "restore"), Capability.NEEDS_CONFIRMATION),
        ],
        ids=["turned-off", "confirmation-cleared"],
    )
    def test_a_job_started_before_the_change_writes_nothing(self, tmp_path, saved, state):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        live = {"settings": ServerMarkersSettings(True, None, "2026-09-13T00:00:00+00:00", "restore")}
        pub = _publisher(tmp_path, folder, settings_provider=lambda: live["settings"])
        assert _write_one(pub, [INTRO]) == [INTRO]
        live["settings"] = saved
        with pytest.raises(PublishError) as ei:
            _write_one(pub, [INTRO, CREDITS_FINAL], previous=[INTRO])
        assert ei.value.state is state
        assert pub.capability().state is state
        assert _served(db) == [(T.INTRO, 11_000, 37_000)]

    def test_without_a_provider_the_settings_given_at_creation_count(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        assert _publisher(tmp_path, folder, enabled=False).capability().state is Capability.DISABLED
        on = _publisher(
            tmp_path, folder, enabled=False, settings_provider=lambda: ServerMarkersSettings(True, None, "t", "restore")
        )
        assert on.capability().state is Capability.READY


class TestCapability:
    @pytest.mark.parametrize(
        ("kwargs", "db", "state"),
        [
            ({"enabled": False}, True, Capability.DISABLED),
            ({"confirmed": None}, True, Capability.NEEDS_CONFIRMATION),
            ({}, False, Capability.MISCONFIGURED),
            ({"fs": "nfs4"}, True, Capability.NEEDS_LOCAL_DB),
            ({"plex_pass": False}, True, Capability.NEEDS_PASS),
            ({}, "no-tag", Capability.NEEDS_PLEX_DETECTION_ONCE),
            ({}, True, Capability.READY),
            ({"plex_pass": None}, True, Capability.READY),  # Plex unreachable right now: DB checks still pass
        ],
    )
    def test_matrix(self, tmp_path, kwargs, db, state):
        folder = tmp_path / "Plex Media Server"
        if db:
            _make_db(folder, tag_row=None if db == "no-tag" else "''")
        report = _publisher(tmp_path, folder, **kwargs).capability()
        assert report.state is state, report.message
        if state is Capability.READY:
            assert report.details["fs_type"] == "ext4" and report.details["db_path"].endswith(
                "com.plexapp.plugins.library.db"
            )
            # Plex unreachable: prefs aren't fetched (no second connection attempt just for UI details).
            unreachable = kwargs.get("plex_pass", True) is None
            expected = {"intro": None, "credits": None} if unreachable else {"intro": "never", "credits": "never"}
            assert report.details["detection"] == expected

    def test_empty_config_folder_is_misconfigured(self, tmp_path):
        pub = _publisher(tmp_path, "")
        assert pub.capability().state is Capability.MISCONFIGURED

    def test_unknown_marker_version_anywhere_in_the_library_blocks_writes(self, tmp_path):
        # The library-wide sample runs here (cached per job), not on every write.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO metadata_items (id, metadata_type, title) VALUES (8, 4, 'Other')")
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (50, 8)")
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (50, 50, '/data/tv/o.mkv', ?)",
            (INTROS_V6,),
        )
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 6" in report.message


def _insert_taggings(db, *rows):
    """Insert (metadata_item_id, tag_id, index, text, start, end, extra_data) rows; returns their ids."""
    return [
        _exec(
            db,
            "INSERT INTO taggings (metadata_item_id, tag_id, [index], text, time_offset, end_time_offset, thumb_url, "
            "created_at, extra_data) VALUES (?, ?, ?, ?, ?, ?, '', 1, ?)",
            *row,
        )
        for row in rows
    ]


def _part_markers(db, part_id, key):
    value = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=?", part_id)[0][0]).get(key)
    return value if not value else json.loads(value)["MediaPartMarkersArray"]["MediaPartMarker"]


def _part_payload_for(marker):
    return json.loads(merge_part_extra_data(None, [marker], {marker.type}, DUR))[plex_db._PART_KEY[marker.type]]


def _set_part_extra(db, part_id, extra):
    _exec(db, "UPDATE media_parts SET extra_data=? WHERE id=?", extra, part_id)


def _exec(db, sql, *params):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _write_one(
    pub, markers, *, previous=(), duration_ms=DUR, path="/data/tv/S01E01.mkv", item_id="7", own_previous=None
):
    prior = None if previous is None else list(previous)
    kwargs = {} if own_previous is None else {"own_previous": list(own_previous)}
    return pub.write(item_id, list(markers), previous=prior, duration_ms=duration_ms, canonical_path=path, **kwargs)


class TestCreditsTimes:
    def test_several_credits_are_stored_in_start_order_whatever_the_input_order(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _write_one(_publisher(tmp_path, folder), [CREDITS_FINAL, CREDITS_NONFINAL])
        assert _part_markers(db, 1, "pv:credits") == [
            {"startTimeOffset": 1_198_000, "endTimeOffset": 1_252_000},
            {"startTimeOffset": 1_297_000, "endTimeOffset": DUR, "final": True},
        ]

    def test_non_final_credits_row_is_shifted_on_both_edges(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _write_one(_publisher(tmp_path, folder), [CREDITS_NONFINAL])
        assert _rows(db, "SELECT [index], text, time_offset, end_time_offset, extra_data FROM taggings") == [
            (0, "credits", 1_198_000, 1_252_000, '{"pv:version":"4","url":"pv%3Aversion=4"}')
        ]
        assert _part_markers(db, 1, "pv:credits") == [{"startTimeOffset": 1_198_000, "endTimeOffset": 1_252_000}]

    @pytest.mark.parametrize(
        ("end_ms", "stored_end", "final"),
        [(DUR - 2_000, DUR - 2_000, True), (DUR - 2_001, DUR - 1, False)],
        ids=["within-2s-of-end-is-final", "just-outside-is-not"],
    )
    def test_final_means_ending_within_2s_of_the_file_end(self, tmp_path, end_ms, stored_end, final):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _write_one(_publisher(tmp_path, folder), [Marker(T.CREDITS, 1_290_000, end_ms, ("chapters",))])
        [(end, extra)] = _rows(db, "SELECT end_time_offset, extra_data FROM taggings")
        assert end == stored_end
        assert ('"pv:final":"1"' in extra) is final
        expected = {"startTimeOffset": 1_288_000, "endTimeOffset": stored_end} | ({"final": True} if final else {})
        assert _part_markers(db, 1, "pv:credits") == [expected]

    @pytest.mark.parametrize("duration_ms", [None, 0, -5])
    def test_credits_with_unknown_duration_write_nothing(self, tmp_path, duration_ms):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        with pytest.raises(PublishError, match="duration"):
            _write_one(_publisher(tmp_path, folder), [INTRO, CREDITS_FINAL], duration_ms=duration_ms)
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(None,)]
        with pytest.raises(PublishError, match="duration"):
            merge_part_extra_data(None, [CREDITS_FINAL], {T.CREDITS}, duration_ms)

    def test_intro_needs_no_duration(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _write_one(_publisher(tmp_path, folder), [INTRO], duration_ms=0)
        assert _rows(db, "SELECT text, time_offset, end_time_offset FROM taggings") == [("intro", 11_000, 37_000)]


class TestWriteGuards:
    @pytest.mark.parametrize(
        ("kwargs", "state"),
        [
            ({"enabled": False}, Capability.DISABLED),
            ({"confirmed": None}, Capability.NEEDS_CONFIRMATION),
            ({"fs": "nfs4"}, Capability.NEEDS_LOCAL_DB),
            ({"fs": "fakeowner"}, Capability.NEEDS_LOCAL_DB),
            ({"fs": "vboxsf"}, Capability.NEEDS_LOCAL_DB),  # unknown type
            ({"mountinfo_path": "/nonexistent/mountinfo"}, Capability.NEEDS_LOCAL_DB),  # type can't be told
            ({"stacked_nfs": True}, Capability.NEEDS_LOCAL_DB),
        ],
        ids=["disabled", "unconfirmed", "nfs4", "fakeowner", "unknown-fs", "no-mountinfo", "nfs-stacked-on-ext4"],
    )
    def test_write_rechecks_settings_and_filesystem(self, tmp_path, kwargs, state):
        # capability() is cached per job; a caller that skips it must still never write.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        kwargs = dict(kwargs)  # the parametrize dict is shared by both journal modes
        if kwargs.pop("stacked_nfs", False):
            top = os.path.realpath(folder).replace(" ", "\\040")  # mountinfo escapes spaces
            kwargs["mountinfo_lines"] = (
                f"100 22 0:50 / {top} rw - ext4 /dev/sda1 rw\n101 100 0:51 / {top} rw - nfs4 nas:/p rw\n"
            )
        pub = _publisher(tmp_path, folder, **kwargs)
        with pytest.raises(PublishError) as ei:
            _write_one(pub, [INTRO])
        assert ei.value.state is state
        assert pub.capability().state is state
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(None,)]

    def test_write_without_database_is_misconfigured_and_creates_no_file(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        with pytest.raises(PublishError) as ei:
            _write_one(_publisher(tmp_path, folder), [INTRO])
        assert ei.value.state is Capability.MISCONFIGURED
        assert not os.path.exists(plex_db_path(str(folder)))

    def test_symlinked_config_folder_is_judged_by_its_real_mount(self, tmp_path):
        nas = tmp_path / "nas"
        _make_db(nas / "Plex Media Server")
        (tmp_path / "config").symlink_to(nas)
        folder = tmp_path / "config" / "Plex Media Server"
        pub = _publisher(
            tmp_path, folder, mountinfo_lines=f"90 22 0:77 / {os.path.realpath(nas)} rw - nfs4 nas:/x rw\n"
        )
        report = pub.capability()
        assert report.state is Capability.NEEDS_LOCAL_DB and report.details["fs_type"] == "nfs4"
        with pytest.raises(PublishError) as ei:
            _write_one(pub, [INTRO])
        assert ei.value.state is Capability.NEEDS_LOCAL_DB

    def test_config_folder_with_uri_special_characters(self, tmp_path):
        folder = tmp_path / "Plex #2 ?x=1 100%" / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        assert pub.capability().state is Capability.READY
        _write_one(pub, [INTRO])
        assert _rows(db, "SELECT text FROM taggings") == [("intro",)]

    @pytest.mark.parametrize(
        ("markers", "previous"),
        [([], []), ([Marker(T.RECAP, 1_000, 9_000, ("chapters",))], [Marker(T.PREVIEW, 5, 3_005, ("chapters",))])],
        ids=["nothing", "unsupported-types-only"],
    )
    def test_nothing_to_manage_never_opens_the_database(self, tmp_path, markers, previous):
        pub = _publisher(tmp_path, tmp_path / "missing" / "Plex Media Server", enabled=False)
        assert _write_one(pub, markers, previous=previous) == []

    def test_item_id_may_be_a_metadata_key(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO], item_id="/library/metadata/7")
        assert _rows(db, "SELECT metadata_item_id FROM taggings") == [(7,)]
        assert _served(db) == [(T.INTRO, 11_000, 37_000)]

    @pytest.mark.parametrize("item_id", ["", "abc", "/library/metadata/", "7a"])
    def test_non_numeric_item_id_is_not_found(self, tmp_path, item_id):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        with pytest.raises(ItemNotFoundError):
            _write_one(pub, [INTRO], item_id=item_id)

    def test_deleted_parts_are_neither_versions_nor_rewritten(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", None), ("/data/tv/S01E01.old.mkv", None)))
        conn = sqlite3.connect(db)
        conn.execute("UPDATE media_parts SET deleted_at=1700000000 WHERE id=2")
        conn.commit()
        conn.close()
        # No sibling lookup: counted as a version, never decided, it would make intro not desired.
        assert _write_one(_publisher(tmp_path, folder), [INTRO]) == [INTRO]
        assert _part_markers(db, 1, "pv:intros") == [{"startTimeOffset": 11_000, "endTimeOffset": 37_000}]
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=2") == [(None,)]


class TestLockWaitRechecks:
    def test_a_version_becoming_an_optimized_copy_while_waiting_writes_nothing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", None), ("/data/tv/S01E01 - 4K.mkv", None)))
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: {T.INTRO: INTRO})
        self._locked_change(db, "UPDATE media_items SET proxy_type=42 WHERE id=2")
        with pytest.raises(PublishError, match="changed"):
            _write_one(pub, [INTRO])
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def _locked_change(self, db, sql):
        locker = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        locker.execute(sql)
        threading.Timer(0.3, lambda: (locker.execute("COMMIT"), locker.close())).start()

    @pytest.mark.parametrize(
        ("sql", "state"),
        [
            ("DELETE FROM tags WHERE id=563", Capability.NEEDS_PLEX_DETECTION_ONCE),
            ("ALTER TABLE taggings RENAME COLUMN end_time_offset TO end_offset_ms", Capability.UNSUPPORTED_SCHEMA),
            ("CREATE TRIGGER added AFTER INSERT ON taggings BEGIN SELECT 1; END", Capability.UNSUPPORTED_SCHEMA),
        ],
        ids=["tag-row-removed", "schema-changed", "trigger-added"],
    )
    def test_tag_row_and_schema_are_rechecked_under_the_lock(self, tmp_path, sql, state):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        self._locked_change(db, sql)
        with pytest.raises(PublishError) as ei:
            _write_one(_publisher(tmp_path, folder), [INTRO])
        assert ei.value.state is state
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=1") == [(None,)]


class TestMultiVersionMatrix:
    A, B, C = "/data/tv/E - 1080p.mkv", "/data/tv/E - 2160p.mkv", "/data/tv/E - 720p.mkv"

    def _db(self, tmp_path, files):
        folder = tmp_path / "Plex Media Server"
        return folder, _make_db(folder, parts=tuple((f, None) for f in files))

    @pytest.mark.parametrize(
        ("d_start", "d_end", "agrees"),
        [
            (2_000, 0, True),
            (0, 2_000, True),
            (-2_000, -2_000, True),
            (2_001, 0, False),
            (-2_001, 0, False),
            (0, 2_001, False),
            (0, -2_001, False),
        ],
    )
    def test_agreement_is_within_2s_on_both_edges(self, tmp_path, d_start, d_end, agrees):
        folder, db = self._db(tmp_path, (self.A, self.B))
        sibling = {T.INTRO: Marker(T.INTRO, INTRO.start_ms + d_start, INTRO.end_ms + d_end, ("chapters",))}
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: sibling if p == self.B else None)
        ours = _write_one(pub, [INTRO], path=self.A)
        if agrees:
            assert ours == [INTRO]
            assert _rows(db, "SELECT time_offset, end_time_offset FROM taggings") == [(11_000, 37_000)]
            assert _part_markers(db, 2, "pv:intros") == [{"startTimeOffset": 11_000, "endTimeOffset": 37_000}]
        else:
            assert ours == []
            assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    @pytest.mark.parametrize(
        "sibling",
        [
            {T.INTRO: INTRO, T.CREDITS: CREDITS_FINAL},  # sibling has credits we don't: credits just aren't desired
            {T.INTRO: INTRO, T.RECAP: Marker(T.RECAP, 0, 9_000, ("chapters",))},  # Plex can't show recaps
        ],
        ids=["extra-supported-type", "extra-unsupported-type"],
    )
    def test_each_type_is_judged_on_its_own(self, tmp_path, sibling):
        folder, db = self._db(tmp_path, (self.A, self.B))
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: sibling if p == self.B else None)
        assert _write_one(pub, [INTRO], path=self.A) == [INTRO]
        assert _rows(db, "SELECT text FROM taggings") == [("intro",)]

    def test_every_type_must_agree(self, tmp_path):
        folder, db = self._db(tmp_path, (self.A, self.B))
        late_credits = Marker(T.CREDITS, CREDITS_FINAL.start_ms + 3_000, DUR, ("chapters",))
        sibling = {T.INTRO: INTRO, T.CREDITS: late_credits}
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: sibling if p == self.B else None)
        assert _write_one(pub, [INTRO, CREDITS_FINAL], path=self.A) == [INTRO]
        assert _rows(db, "SELECT text FROM taggings") == [("intro",)]

    def test_every_other_version_must_agree(self, tmp_path):
        folder, db = self._db(tmp_path, (self.A, self.B, self.C))
        decided = {self.B: {T.INTRO: INTRO}, self.C: {T.INTRO: Marker(T.INTRO, 20_000, 46_000, ("chapters",))}}
        pub = _publisher(tmp_path, folder, sibling_markers=decided.get)
        assert _write_one(pub, [INTRO], path=self.A) == []
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_removal_publishes_when_the_other_version_is_decided_empty(self, tmp_path):
        folder, db = self._db(tmp_path, (self.A, self.B))
        _insert_taggings(db, (7, 563, 0, "intro", 11_000, 37_000, INTRO_ROW_EXTRA))
        _set_part_extra(db, 1, _plex_json({"pv:intros": INTRO_PAYLOAD}))
        _set_part_extra(db, 2, _plex_json({"pv:intros": INTRO_PAYLOAD}))
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: {} if p == self.B else None)
        assert _write_one(pub, [], previous=[INTRO], path=self.A) == []
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _part_markers(db, 1, "pv:intros") is None and _part_markers(db, 2, "pv:intros") is None

    def test_removal_never_waits_for_an_undecided_version(self, tmp_path):
        folder, db = self._db(tmp_path, (self.A, self.B))
        _insert_taggings(db, (7, 563, 0, "intro", 11_000, 37_000, INTRO_ROW_EXTRA))
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: None)
        assert _write_one(pub, [], previous=[INTRO], path=self.A) == []
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_item_without_our_file_is_not_found_even_if_its_file_agrees(self, tmp_path):
        folder, db = self._db(tmp_path, (self.B,))
        seen = []
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: (seen.append(p), {T.INTRO: INTRO})[1])
        with pytest.raises(ItemNotFoundError, match="path mappings"):
            _write_one(pub, [INTRO], path=self.A)
        assert seen == []
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _rows(db, "SELECT extra_data FROM media_parts") == [(None,)]

    def test_multi_disk_mapping_looks_up_every_local_candidate(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/plexmedia/tv/A.mkv", None), ("/plexmedia/tv/B.mkv", None)))
        seen = []
        decided = {"/disk2/tv/B.mkv": {T.INTRO: INTRO}}
        pub = _publisher(
            tmp_path,
            folder,
            mappings=[
                {"plex_prefix": "/plexmedia", "local_prefix": "/disk1"},
                {"plex_prefix": "/plexmedia", "local_prefix": "/disk2"},
            ],
            sibling_markers=lambda p: (seen.append(p), decided.get(p))[1],
        )
        assert _write_one(pub, [INTRO], path="/disk2/tv/A.mkv") == [INTRO]
        assert seen == ["/disk1/tv/B.mkv", "/disk2/tv/B.mkv"]
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 1


class TestCapabilityDetails:
    def _db_with_other_part(self, tmp_path, extra):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (50, 8)")
        conn.execute(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (50, 50, '/o.mkv', ?)", (extra,)
        )
        conn.commit()
        conn.close()
        return folder

    def test_unknown_credits_version_in_the_library_blocks_writes(self, tmp_path):
        folder = self._db_with_other_part(
            tmp_path, _plex_json({"pv:credits": NATIVE_CREDITS.replace('"version":4', '"version":5')})
        )
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 5" in report.message

    @pytest.mark.parametrize(
        "extra", [BOTH_NATIVE, _plex_json({"pv:intros": "", "pv:credits": ""}), 'not json but mentions "pv:intros":"{']
    )
    def test_tested_or_cleared_library_data_is_ready(self, tmp_path, extra):
        folder = self._db_with_other_part(tmp_path, extra)
        assert _publisher(tmp_path, folder).capability().state is Capability.READY

    def test_missing_column_is_unsupported_schema(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        conn = sqlite3.connect(db)
        conn.execute("ALTER TABLE media_parts RENAME COLUMN deleted_at TO removed_at")
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "media_parts" in report.message

    def test_unreadable_database_is_misconfigured(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = Path(plex_db_path(str(folder)))
        db.parent.mkdir(parents=True)
        db.write_bytes(b"this is not a sqlite database" * 10)
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.MISCONFIGURED and "not a database" in report.message

    def test_ready_details_survive_detection_prefs_failing(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder, plex_pass=None)
        pub._server.get_marker_detection_prefs.side_effect = RuntimeError("down")
        report = pub.capability()
        assert report.state is Capability.READY
        assert report.details["detection"] == {"intro": None, "credits": None}
        assert report.details["plex_pass"] is None

    def test_network_share_report_names_the_filesystem(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder, fs="cifs")
        report = pub.capability()
        assert report.state is Capability.NEEDS_LOCAL_DB and "network share (cifs)" in report.message
        assert report.details["fs_type"] == "cifs"
        pub._server.get_server_status.assert_not_called()


_HOLDER = (
    "import sqlite3, sys\n"
    "conn = sqlite3.connect(sys.argv[1])\n"
    "conn.execute('SELECT COUNT(*) FROM taggings').fetchall()\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


@contextlib.contextmanager
def _another_process_holding(db):
    """A separate process with the DB open, like Plex: it holds SQLite's read lock on byte 128 of <db>-shm."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


class TestLockDomain:
    """The real lock probe: writes need proof that Plex's SQLite and ours share locks on the same file."""

    @pytest.fixture(autouse=True)
    def plex_holds_the_database(self):
        """Overrides the module fixture: no stand-in, the real probe runs."""

    @pytest.fixture(autouse=True)
    def journal_mode(self):
        """Overrides the module fixture: each test picks its journal mode explicitly."""

    def _db(self, tmp_path, journal="wal"):
        folder = tmp_path / "Plex Media Server"
        return folder, _make_db(folder, journal_mode=journal)

    def test_ready_and_writes_while_another_process_holds_the_database(self, tmp_path):
        folder, db = self._db(tmp_path)
        pub = _publisher(tmp_path, folder)
        with _another_process_holding(db):
            report = pub.capability()
            assert report.state is Capability.READY, report.message
            _write_one(pub, [INTRO])
        assert _rows(db, "SELECT text FROM taggings") == [("intro",)]

    @pytest.mark.parametrize(
        ("plex_pass", "state"),
        [(True, Capability.NEEDS_LOCAL_DB), (False, Capability.NEEDS_LOCAL_DB), (None, Capability.UNREACHABLE)],
        ids=["plex-reachable", "reachable-without-pass", "plex-unreachable"],
    )
    def test_no_holder_is_not_ready(self, tmp_path, plex_pass, state):
        folder, _db = self._db(tmp_path)
        report = _publisher(tmp_path, folder, plex_pass=plex_pass).capability()
        assert report.state is state, report.message
        assert report.details["lock_holder"] is False

    def test_write_without_a_holder_writes_nothing(self, tmp_path):
        folder, db = self._db(tmp_path)
        with pytest.raises(PublishError) as ei:
            _write_one(_publisher(tmp_path, folder), [INTRO])
        assert ei.value.state is Capability.UNREACHABLE
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        assert _rows(db, "SELECT extra_data FROM media_parts") == [(None,)]

    def test_a_rollback_journal_database_never_counts_as_held(self, tmp_path):
        # Plex runs WAL; without a -shm file there is nothing to prove the lock domain with.
        folder, db = self._db(tmp_path, journal="delete")
        with _another_process_holding(db):
            assert _publisher(tmp_path, folder).capability().state is Capability.NEEDS_LOCAL_DB

    def test_closing_any_descriptor_on_the_shm_drops_our_connection_s_lock(self, tmp_path):
        # Why every connection and the probe share one per-path lock: this is the kernel's POSIX-lock rule.
        _folder, db = self._db(tmp_path)
        checker = (
            "import fcntl, os, struct, sys\n"
            "fd = os.open(sys.argv[1], os.O_RDONLY)\n"
            "req = struct.pack('hhqqi', fcntl.F_WRLCK, 0, 128, 1, 0)\n"
            "print(struct.unpack('hhqqi', fcntl.fcntl(fd, fcntl.F_GETLK, req))[0] != fcntl.F_UNLCK)\n"
        )

        def held() -> bool:
            out = subprocess.run(
                [sys.executable, "-c", checker, f"{db}-shm"], capture_output=True, text=True, check=True
            )
            return out.stdout.strip() == "True"

        conn = sqlite3.connect(db)
        try:
            conn.execute("SELECT COUNT(*) FROM taggings").fetchall()
            assert held()
            os.close(os.open(f"{db}-shm", os.O_RDONLY))
            assert not held()
        finally:
            conn.close()


class TestRemoval:
    """Removing a type we published removes only what is still ours (spec §14; ruling: delete the key)."""

    def _published(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO, CREDITS_FINAL])
        return db, pub

    @pytest.mark.parametrize(
        ("rows_redetected", "key_redetected"),
        [(False, False), (True, False), (False, True), (True, True)],
        ids=["all-ours", "rows-redetected", "key-redetected", "both-redetected"],
    )
    def test_only_rows_and_key_still_equal_to_ours_are_removed(self, tmp_path, rows_redetected, key_redetected):
        db, pub = self._published(tmp_path)
        if rows_redetected:
            _exec(
                db,
                "UPDATE taggings SET time_offset=1264953, end_time_offset=1296953, extra_data=? WHERE text='credits'",
                CREDITS_ROW_EXTRA,
            )
        if key_redetected:
            extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
            extra["pv:credits"] = NATIVE_CREDITS
            _set_part_extra(db, 1, encode_extra_data(extra))
        _write_one(pub, [INTRO], previous=[INTRO, CREDITS_FINAL])
        credits_rows = _rows(db, "SELECT time_offset, end_time_offset, extra_data FROM taggings WHERE text='credits'")
        assert credits_rows == ([(1264953, 1296953, CREDITS_ROW_EXTRA)] if rows_redetected else [])
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert extra.get("pv:credits") == (NATIVE_CREDITS if key_redetected else None)
        assert extra["pv:intros"] == INTRO_PAYLOAD

    @pytest.mark.parametrize(
        ("published", "duration_now"),
        [
            (CREDITS_FINAL, DUR + 60_000),  # was final; the new duration would call it not final
            (Marker(T.CREDITS, 1_200_000, DUR - 60_000, ("chapters",)), DUR - 60_000),  # the other way round
            (CREDITS_FINAL, None),
        ],
        ids=["file-60s-longer", "file-60s-shorter", "duration-unknown"],
    )
    def test_removal_compares_served_times_whatever_the_duration_is_now(self, tmp_path, published, duration_now):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO, published])
        _write_one(pub, [INTRO], previous=[INTRO, published], duration_ms=duration_now)
        assert _rows(db, "SELECT text FROM taggings") == [("intro",)]
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert "pv:credits" not in extra and extra["pv:intros"] == INTRO_PAYLOAD

    @pytest.mark.parametrize("duration_now", [DUR + 60_000, DUR - 60_000, None])
    def test_merge_removes_our_key_whatever_the_duration_is_now(self, duration_now):
        existing = _plex_json({"pv:credits": CREDITS_FINAL_PAYLOAD, "ma:container": "mkv"})
        out = json.loads(merge_part_extra_data(existing, [], {T.CREDITS}, duration_now, previous=[CREDITS_FINAL]))
        assert out == {"ma:container": "mkv", "url": "ma%3Acontainer=mkv"}

    def test_redetected_row_with_our_times_but_other_flags_stays(self, tmp_path):
        db, pub = self._published(tmp_path)
        _exec(db, "UPDATE taggings SET extra_data=? WHERE text='credits'", CREDITS_ROW_EXTRA)  # Plex: not final
        _write_one(pub, [INTRO], previous=[INTRO, CREDITS_FINAL])
        assert _rows(db, "SELECT time_offset, end_time_offset, extra_data FROM taggings WHERE text='credits'") == [
            (1_297_000, DUR, CREDITS_ROW_EXTRA)
        ]

    def test_removing_everything_leaves_only_the_url_field(self, tmp_path):
        db, pub = self._published(tmp_path)
        _write_one(pub, [], previous=[INTRO, CREDITS_FINAL])
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert extra == {"url": ""}


class TestIndex:
    """[index] follows Plex's rule over every marker row of the item: by text, then start."""

    def test_publishing_intro_beside_native_credits_keeps_indexes_unique(self, tmp_path, sql_log):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        c0, c1, _i2 = _insert_taggings(
            db,
            (7, 563, 0, "credits", 1_182_721, 1_212_721, CREDITS_ROW_EXTRA),
            (7, 563, 1, "credits", 1_246_721, 1_318_496, CREDITS_FINAL_ROW_EXTRA),
            (7, 563, 2, "intro", 1_237, 29_554, INTRO_ROW_EXTRA),
        )
        _write_one(_publisher(tmp_path, folder), [INTRO])
        assert _rows(db, "SELECT id, [index], text, time_offset FROM taggings WHERE tag_id=563 ORDER BY [index]")[
            :2
        ] == [
            (c0, 0, "credits", 1_182_721),
            (c1, 1, "credits", 1_246_721),
        ]
        assert _rows(db, "SELECT [index], text, time_offset FROM taggings WHERE text='intro'") == [(2, "intro", 11_000)]
        assert not [sql for sql in _writes(sql_log) if sql.startswith("UPDATE taggings")]

    def test_kept_rows_are_renumbered_only_when_their_index_changes(self, tmp_path, sql_log):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        credits_id, intro_id = _insert_taggings(
            db,
            (7, 563, 0, "credits", 1_182_721, 1_212_721, CREDITS_ROW_EXTRA),
            (7, 563, 1, "intro", 1_237, 29_554, INTRO_ROW_EXTRA),
        )
        _write_one(_publisher(tmp_path, folder), [CREDITS_NONFINAL, CREDITS_FINAL])
        assert _rows(db, "SELECT [index], text, time_offset FROM taggings ORDER BY [index]") == [
            (0, "credits", 1_198_000),
            (1, "credits", 1_297_000),
            (2, "intro", 1_237),
        ]
        assert _rows(db, "SELECT id FROM taggings WHERE text='intro'") == [(intro_id,)]
        updates = [sql for sql in _writes(sql_log) if sql.startswith("UPDATE taggings")]
        assert updates == [f"UPDATE taggings SET [index]=2 WHERE id={intro_id}"]
        assert credits_id not in [r[0] for r in _rows(db, "SELECT id FROM taggings")]


class TestNoOp:
    def test_a_part_key_holding_plex_s_value_is_replaced(self, tmp_path):
        # Same keys before and after, different value: still a change to write.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", encode_extra_data({"pv:intros": NATIVE_INTROS})),))
        _insert_taggings(db, (7, 563, 0, "intro", 11_000, 37_000, INTRO_ROW_EXTRA))
        _write_one(_publisher(tmp_path, folder), [INTRO])
        assert _rows(db, "SELECT extra_data FROM media_parts") == [(encode_extra_data({"pv:intros": INTRO_PAYLOAD}),)]

    def test_only_a_kept_row_index_changing_is_still_written(self, tmp_path, sql_log):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        credits_id, _intro_id = _insert_taggings(
            db,
            (7, 563, 5, "credits", 1_182_721, 1_212_721, CREDITS_ROW_EXTRA),
            (7, 563, 1, "intro", 11_000, 37_000, INTRO_ROW_EXTRA),
        )
        _set_part_extra(db, 1, encode_extra_data({"pv:intros": INTRO_PAYLOAD}))
        _write_one(_publisher(tmp_path, folder), [INTRO])
        assert _writes(sql_log) == [f"UPDATE taggings SET [index]=0 WHERE id={credits_id}"]
        assert _rows(db, "SELECT text, [index] FROM taggings ORDER BY [index]") == [("credits", 0), ("intro", 1)]

    def test_unchanged_publish_writes_nothing(self, tmp_path, sql_log):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO, CREDITS_FINAL])
        before = _rows(db, "SELECT id, created_at, [index] FROM taggings ORDER BY id")
        parts_before = _rows(db, "SELECT extra_data FROM media_parts")
        sql_log.clear()
        assert _write_one(pub, [INTRO, CREDITS_FINAL], previous=[INTRO, CREDITS_FINAL]) == [INTRO, CREDITS_FINAL]
        assert _writes(sql_log) == []
        assert not [sql for sql in sql_log if sql.startswith(("BEGIN", "COMMIT"))]  # not even the write lock
        assert _rows(db, "SELECT id, created_at, [index] FROM taggings ORDER BY id") == before
        assert _rows(db, "SELECT extra_data FROM media_parts") == parts_before

    @pytest.mark.parametrize("difference", ["index", "part-key"])
    def test_same_rows_with_a_stale_index_or_part_are_rewritten(self, tmp_path, difference):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        indexes = (1, 0) if difference == "index" else (0, 1)
        _insert_taggings(
            db,
            (7, 563, indexes[0], "credits", 1_297_000, DUR, CREDITS_FINAL_ROW_EXTRA),
            (7, 563, indexes[1], "intro", 11_000, 37_000, INTRO_ROW_EXTRA),
        )
        payloads = {"pv:credits": CREDITS_FINAL_PAYLOAD, "pv:intros": INTRO_PAYLOAD}
        if difference == "index":
            _set_part_extra(db, 1, encode_extra_data(payloads))
        _write_one(_publisher(tmp_path, folder), [INTRO, CREDITS_FINAL])
        assert _rows(db, "SELECT text, [index] FROM taggings ORDER BY [index]") == [("credits", 0), ("intro", 1)]
        assert _rows(db, "SELECT extra_data FROM media_parts") == [(encode_extra_data(payloads),)]


class TestKeepPlexs:
    """``on_plex_redetect`` per type: a type Plex re-detected over ours stays Plex's while the server keeps them."""

    NATIVE_INTRO = (T.INTRO, 990, 29_306)

    @staticmethod
    def _plex_redetects_the_intro(db) -> None:
        conn = sqlite3.connect(db)
        try:
            conn.execute("DELETE FROM taggings WHERE text='intro'")
            conn.commit()
        finally:
            conn.close()
        _insert_taggings(db, (7, 563, 1, "intro", 990, 29_306, INTRO_ROW_EXTRA))
        _set_part_extra(db, 1, encode_extra_data({"pv:credits": CREDITS_FINAL_PAYLOAD, "pv:intros": NATIVE_INTROS}))

    @pytest.mark.parametrize("redetect", ["keep_plex", "restore"])
    def test_a_type_plex_replaced_is_kept_only_when_set_to_keep(self, tmp_path, sql_log, redetect):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder, redetect=redetect)
        _write_one(pub, [INTRO, CREDITS_FINAL])
        self._plex_redetects_the_intro(db)
        sql_log.clear()

        ours = _write_one(pub, [INTRO, CREDITS_FINAL], previous=[INTRO, CREDITS_FINAL])

        if redetect == "keep_plex":
            assert (ours, pub.last_kept_types, pub.last_write_changed) == ([CREDITS_FINAL], {T.INTRO}, False)
            assert _served(db) == [self.NATIVE_INTRO, (T.CREDITS, 1_299_000, DUR)]
            assert not [sql for sql in sql_log if sql.startswith("BEGIN")]  # not even the write lock
            assert json.loads(_rows(db, "SELECT extra_data FROM media_parts")[0][0])["pv:intros"] == NATIVE_INTROS
        else:
            assert (ours, pub.last_kept_types, pub.last_write_changed) == ([INTRO, CREDITS_FINAL], frozenset(), True)
            assert _served(db) == [(T.INTRO, 11_000, 37_000), (T.CREDITS, 1_299_000, DUR)]

    @pytest.mark.parametrize("redetect", ["keep_plex", "restore"])
    def test_a_kept_type_stays_plexs_without_our_record_of_it(self, tmp_path, redetect):
        # Later runs: the item record no longer lists the kept intro, only kept_types says it is Plex's.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder, redetect=redetect)
        _write_one(pub, [CREDITS_FINAL])
        self._plex_redetects_the_intro(db)

        ours = pub.write(
            "7",
            [INTRO, CREDITS_FINAL],
            previous=[CREDITS_FINAL],
            duration_ms=DUR,
            canonical_path="/data/tv/S01E01.mkv",
            kept_types=frozenset({T.INTRO}),
        )

        kept = redetect == "keep_plex"
        assert ours == ([CREDITS_FINAL] if kept else [INTRO, CREDITS_FINAL])
        assert pub.last_kept_types == ({T.INTRO} if kept else frozenset())
        assert _served(db)[0] == (self.NATIVE_INTRO if kept else (T.INTRO, 11_000, 37_000))

    def test_kept_ends_once_plex_has_no_rows_of_the_type(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        ours = pub.write(
            "7", [INTRO], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv", kept_types={T.INTRO}
        )
        assert (ours, pub.last_kept_types) == ([INTRO], frozenset())
        assert _served(db) == [(T.INTRO, 11_000, 37_000)]

    @pytest.mark.parametrize(
        ("redetect", "native", "own_previous", "ours", "kept", "served_intro"),
        [
            # Nothing of ours recorded: Plex's intro is kept (it may be Plex's own, or ours before markers.db was reset).
            ("keep_plex", (990, 29_306), None, [], {T.INTRO}, (990, 29_306)),
            ("restore", (990, 29_306), None, [INTRO], frozenset(), (11_000, 37_000)),
            # Rows that already show the decision are ours.
            ("keep_plex", (11_000, 37_000), None, [INTRO], frozenset(), (11_000, 37_000)),
            # Rows that show what this file published on its item before a merge are ours too.
            (
                "keep_plex",
                (5_000, 9_000),
                [Marker(T.INTRO, 5_000, 9_000, ("a",))],
                [INTRO],
                frozenset(),
                (11_000, 37_000),
            ),
        ],
        ids=["keep-plexs", "restore-plexs", "keep-already-ours", "keep-ours-before-a-merge"],
    )
    def test_a_first_publish_onto_plexs_rows_follows_the_setting(
        self, tmp_path, redetect, native, own_previous, ours, kept, served_intro
    ):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _insert_taggings(db, (7, 563, 0, "intro", *native, INTRO_ROW_EXTRA))
        pub = _publisher(tmp_path, folder, redetect=redetect)
        assert (_write_one(pub, [INTRO], own_previous=own_previous), pub.last_kept_types) == (ours, kept)
        assert _served(db) == [(T.INTRO, *served_intro)]

    def test_plexs_rows_of_a_type_we_dont_show_are_left_alone_and_not_recorded_as_kept(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _insert_taggings(db, (7, 563, 0, "credits", 1_182_721, 1_212_721, CREDITS_ROW_EXTRA))
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        assert (_write_one(pub, [INTRO]), pub.last_kept_types) == ([INTRO], frozenset())
        assert _served(db) == [(T.INTRO, 11_000, 37_000), (T.CREDITS, 1_184_721, 1_210_721)]

    def test_a_plex_row_matching_our_decision_isnt_newly_kept_when_the_record_is_stale(self, tmp_path):
        # The record says the old intro while Plex already shows the new one (recording failed after the COMMIT).
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        _write_one(pub, [INTRO])
        newer = Marker(T.INTRO, 12_000, 40_000, ("a",))
        _write_one(pub, [newer], previous=[INTRO])
        assert (_write_one(pub, [newer], previous=[INTRO]), pub.last_kept_types) == ([newer], frozenset())
        assert _served(db) == [(T.INTRO, 12_000, 40_000)]

    @pytest.mark.parametrize(
        ("redetect", "rows", "kept"),
        [("keep_plex", True, {T.INTRO}), ("keep_plex", False, frozenset()), ("restore", True, frozenset())],
        ids=["keep-rows", "keep-no-rows", "restore"],
    )
    def test_nothing_to_write_still_reads_the_rows_of_a_kept_type(self, tmp_path, redetect, rows, kept):
        # keepplex re-review LOW-1: no early return while a type is kept, so one Plex no longer shows is released.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        if rows:
            _insert_taggings(db, (7, 563, 0, "intro", 990, 29_306, INTRO_ROW_EXTRA))
        pub = _publisher(tmp_path, folder, redetect=redetect)
        ours = pub.write(
            "7", [], previous=[], duration_ms=DUR, canonical_path="/data/tv/S01E01.mkv", kept_types={T.INTRO}
        )
        assert (ours, pub.last_kept_types, pub.last_write_changed) == ([], kept, False)

    def test_read_back_reports_a_kept_type_plex_no_longer_shows(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder, redetect="keep_plex")
        _write_one(pub, [CREDITS_FINAL])
        assert pub.shows("7", [CREDITS_FINAL], kept_types=frozenset({T.INTRO})) is Shown.MISSING
        assert pub.shows("7", [], kept_types=frozenset({T.INTRO})) is Shown.MISSING
        self._plex_redetects_the_intro(db)
        assert pub.shows("7", [CREDITS_FINAL], kept_types=frozenset({T.INTRO})) is Shown.OURS
        assert pub.shows("7", [], kept_types=frozenset({T.INTRO})) is Shown.OURS
        assert pub.shows("7", [INTRO, CREDITS_FINAL]) is Shown.REPLACED


class TestOptimizedVersions:
    OPTIMIZED = "/data/tv/Plex Versions/Optimized for TV/S01E01.mp4"

    @pytest.mark.parametrize(
        ("proxy_type", "path", "waits"),
        [
            (42, OPTIMIZED, False),
            (0, OPTIMIZED, True),
            (42, "/data/tv/S01E01 - 4K.mkv", True),
            (42, "/data/tv/My Plex Versions Backup/S01E01.mkv", True),  # a path component, not a substring
        ],
        ids=["optimized", "proxy-type-0", "proxy-type-outside-plex-versions", "plex-versions-inside-a-folder-name"],
    )
    def test_optimized_copies_are_rewritten_but_never_block(self, tmp_path, proxy_type, path, waits):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", None), (path, None)))
        _exec(db, "UPDATE media_items SET proxy_type=? WHERE id=2", proxy_type)
        seen = []
        pub = _publisher(tmp_path, folder, sibling_markers=lambda p: seen.append(p))
        if waits:  # an ordinary version that was never decided: intro isn't desired
            assert _write_one(pub, [INTRO]) == []
            assert seen == [path]
            assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0
            return
        assert _write_one(pub, [INTRO]) == [INTRO]
        assert seen == []
        assert [json.loads(r[0])["pv:intros"] for r in _rows(db, "SELECT extra_data FROM media_parts ORDER BY id")] == [
            INTRO_PAYLOAD,
            INTRO_PAYLOAD,
        ]

    def test_an_optimized_copy_alone_does_not_hold_our_file(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.OPTIMIZED, None),))
        _exec(db, "UPDATE media_items SET proxy_type=42 WHERE id=1")
        with pytest.raises(ItemNotFoundError):
            _write_one(_publisher(tmp_path, folder), [INTRO], path=self.OPTIMIZED)


class TestVersionFiles:
    """The item's versions a write records (``last_item_files``) and the read-back compares with Plex's parts now."""

    A = "/data/tv/S01E01 - 1080p.mkv"
    B = "/data/tv/S01E01 - 2160p.mkv"
    OPTIMIZED = TestOptimizedVersions.OPTIMIZED

    @staticmethod
    def _add_part(db, part_id: int, path: str, *, proxy_type: int | None = None) -> None:
        _exec(db, "INSERT INTO media_items (id, metadata_item_id, proxy_type) VALUES (?, 7, ?)", part_id, proxy_type)
        _exec(db, "INSERT INTO media_parts (id, media_item_id, file) VALUES (?, ?, ?)", part_id, part_id, path)

    @pytest.mark.parametrize(
        ("change", "shown"),
        [
            ("none", Shown.OURS),
            ("version-added", Shown.VERSIONS_CHANGED),
            ("version-deleted", Shown.VERSIONS_CHANGED),
            ("optimized-copy-added", Shown.OURS),  # a transcode Plex made takes no part in the agreement
        ],
    )
    def test_read_back_compares_the_versions_the_write_recorded(self, tmp_path, change, shown):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.B, None), (self.A, None)))  # Plex numbered the 2160p part first
        pub = _publisher(tmp_path, folder, sibling_markers=lambda _path: {T.INTRO: INTRO})
        assert _write_one(pub, [INTRO], path=self.A) == [INTRO]
        assert pub.last_item_files == (self.A, self.B)  # sorted, as markers.db stores them
        if change == "version-added":
            self._add_part(db, 3, "/data/tv/S01E01 - 720p.mkv")
        elif change == "version-deleted":
            _exec(db, "UPDATE media_parts SET deleted_at=1 WHERE id=1")
        elif change == "optimized-copy-added":
            self._add_part(db, 3, self.OPTIMIZED, proxy_type=42)
        assert pub.shows("7", [INTRO], item_files=(self.A, self.B)) is shown
        assert pub.shows("7", [INTRO], item_files=None) is Shown.OURS

    def test_an_optimized_copy_is_not_recorded_as_a_version(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.A, None), (self.OPTIMIZED, None)))
        _exec(db, "UPDATE media_items SET proxy_type=42 WHERE id=2")
        pub = _publisher(tmp_path, folder)
        assert _write_one(pub, [INTRO], path=self.A) == [INTRO]
        assert pub.last_item_files == (self.A,)
        assert pub.shows("7", [INTRO], item_files=(self.A,)) is Shown.OURS

    def test_different_versions_are_reported_whatever_the_rows_show(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder, parts=((self.A, None),))
        pub = _publisher(tmp_path, folder)
        assert pub.shows("7", [INTRO], item_files=(self.A,)) is Shown.MISSING  # no rows at all
        assert pub.shows("7", [INTRO], item_files=(self.A, self.B)) is Shown.VERSIONS_CHANGED
        assert pub.shows("7", [], kept_types=frozenset({T.INTRO}), item_files=()) is Shown.VERSIONS_CHANGED

    def test_an_item_without_live_files_is_told_apart_from_an_unknown_item(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.A, None), (self.B, None)))
        pub = _publisher(tmp_path, folder)
        _exec(db, "UPDATE media_parts SET deleted_at=1 WHERE id=1")
        _exec(db, "UPDATE media_items SET deleted_at=1 WHERE id=2")
        with pytest.raises(ItemNotFoundError, match="Plex has no live files for this item"):
            _write_one(pub, [INTRO], path=self.A)
        with pytest.raises(ItemNotFoundError, match="Plex item 8 not found in the database"):
            _write_one(pub, [INTRO], path=self.A, item_id="8")

    def test_a_write_that_did_not_read_the_item_records_no_versions(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder, parts=((self.A, None),))
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO], path=self.A)
        assert _write_one(pub, [], previous=None, path=self.A) == []  # nothing to show or remove: Plex isn't read
        assert pub.last_item_files is None
        _write_one(pub, [INTRO], path=self.A)
        with pytest.raises(ItemNotFoundError):
            _write_one(pub, [INTRO], path=self.A, item_id="8")
        assert pub.last_item_files is None


class TestSqliteErrors:
    def test_plex_holding_the_write_lock_past_the_timeout_is_unreachable(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.2)
        locker = sqlite3.connect(db, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(PublishError) as ei:
                _write_one(_publisher(tmp_path, folder), [INTRO])
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert ei.value.state is Capability.UNREACHABLE
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    @pytest.mark.parametrize("target", ["db", "folder", "shm"])
    def test_files_this_app_cannot_write_are_misconfigured(self, tmp_path, target):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        path = {"db": db, "folder": db.parent, "shm": Path(f"{db}-shm")}[target]
        if target == "shm":
            path.write_bytes(b"")
        mode = path.stat().st_mode
        path.chmod(0o555 if target == "folder" else 0o444)
        try:
            pub = _publisher(tmp_path, folder)
            report = pub.capability()
            assert report.state is Capability.MISCONFIGURED and "can't write" in report.message
            with pytest.raises(PublishError) as ei:
                _write_one(pub, [INTRO])
            assert ei.value.state is Capability.MISCONFIGURED
        finally:
            path.chmod(mode)
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    @pytest.mark.parametrize(
        ("code", "message", "state"),
        [
            (5, "database is locked", Capability.UNREACHABLE),
            (6, "database table is locked", Capability.UNREACHABLE),
            (517, "database is locked", Capability.UNREACHABLE),
            (8, "attempt to write a readonly database", Capability.MISCONFIGURED),
            (14, "unable to open database file", Capability.MISCONFIGURED),
            (3, "access permission denied", Capability.MISCONFIGURED),
            (10, "disk I/O error", Capability.MISCONFIGURED),
            (26, "file is not a database", Capability.MISCONFIGURED),
            (15, "locking protocol", Capability.UNREACHABLE),
            (13, "database or disk is full", Capability.MISCONFIGURED),
            (11, "database disk image is malformed", Capability.MISCONFIGURED),
            (None, "database or disk is full", Capability.MISCONFIGURED),
            (None, "database disk image is malformed", Capability.MISCONFIGURED),
            (None, "locking protocol", Capability.UNREACHABLE),
            (1, "no such column: x", Capability.UNSUPPORTED_SCHEMA),
            (None, "database is locked", Capability.UNREACHABLE),
            (None, "attempt to write a readonly database", Capability.MISCONFIGURED),
            (None, "something else", Capability.UNSUPPORTED_SCHEMA),
        ],
    )
    def test_sqlite_errors_map_to_capability_states(self, code, message, state):
        labels = {"database or disk is full": "Disk full", "database disk image is malformed": "Database damaged"}
        exc = sqlite3.OperationalError(message)
        if code is not None:
            exc.sqlite_errorcode = code
        err = plex_db.publish_error_from_sqlite(exc)
        assert isinstance(err, PublishError) and err.state is state and message in str(err)
        assert labels.get(message, "") in str(err)


class TestSchemaGuards:
    def test_newest_parts_are_sampled_first(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        good = _plex_json({"pv:intros": NATIVE_INTROS})
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO media_items (id, metadata_item_id) VALUES (100, 8)")
        conn.executemany(
            "INSERT INTO media_parts (id, media_item_id, file, extra_data) VALUES (?, 100, ?, ?)",
            [(100 + n, f"/o{n}.mkv", good) for n in range(60)] + [(200, "/newest.mkv", INTROS_V6)],
        )
        conn.commit()
        conn.close()
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and "version 6" in report.message

    def test_two_marker_tag_rows_are_unsupported(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _exec(db, "INSERT INTO tags (id, tag, tag_type) VALUES (900, '', 12)")
        pub = _publisher(tmp_path, folder)
        assert pub.capability().state is Capability.UNSUPPORTED_SCHEMA
        with pytest.raises(PublishError) as ei:
            _write_one(pub, [INTRO])
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    @pytest.mark.parametrize("table", ["taggings", "media_parts"])
    def test_a_trigger_on_a_table_we_write_is_unsupported(self, tmp_path, table):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _exec(db, f"CREATE TRIGGER surprise AFTER UPDATE ON {table} BEGIN SELECT 1; END")
        pub = _publisher(tmp_path, folder)
        report = pub.capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and table in report.message
        with pytest.raises(PublishError) as ei:
            _write_one(pub, [INTRO])
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA

    def test_plex_s_own_triggers_on_other_tables_are_fine(self, tmp_path):
        # Lab PMS 1.43.4 has fts4 triggers on metadata_items and tags only (review PROOF P5).
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _exec(db, "CREATE TRIGGER fts4_like AFTER UPDATE ON metadata_items BEGIN SELECT 1; END")
        _exec(db, "CREATE TRIGGER fts4_tags_like AFTER UPDATE ON tags BEGIN SELECT 1; END")
        assert _publisher(tmp_path, folder).capability().state is Capability.READY

    @pytest.mark.parametrize(
        "existing",
        [_plex_json({"ma:x": 1}), _plex_json({"ma:y": True}), _plex_json({"pv:intros": None}), _plex_json({"url": 5})],
        ids=["int", "bool", "null-marker", "non-string-url"],
    )
    def test_non_string_extra_data_values_are_unsupported(self, existing):
        with pytest.raises(PublishError) as ei:
            merge_part_extra_data(existing, [INTRO], {T.INTRO}, DUR)
        assert ei.value.state is Capability.UNSUPPORTED_SCHEMA

    def test_deleted_media_item_is_ignored_even_if_its_part_is_not(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", None), ("/data/tv/S01E01.old.mkv", None)))
        _exec(db, "UPDATE media_items SET deleted_at=1700000000 WHERE id=2")
        _write_one(_publisher(tmp_path, folder), [INTRO])
        assert _rows(db, "SELECT extra_data FROM media_parts WHERE id=2") == [(None,)]

    @pytest.mark.parametrize(("table", "column"), [("media_items", "deleted_at"), ("media_items", "proxy_type")])
    def test_media_items_columns_we_read_are_required(self, tmp_path, table, column):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _exec(db, f"ALTER TABLE {table} RENAME COLUMN {column} TO renamed_{column}")
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.UNSUPPORTED_SCHEMA and column in report.message

    def test_detection_prefs_are_not_fetched_when_plex_is_unreachable(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder, plex_pass=None)
        report = pub.capability()
        assert report.state is Capability.READY
        assert report.details["detection"] == {"intro": None, "credits": None}
        pub._server.get_marker_detection_prefs.assert_not_called()


class TestUnknownPrevious:
    """``previous=None``: our last publish failed, so nothing on the item is provably ours."""

    def _native(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", _plex_json({"pv:credits": NATIVE_CREDITS})),))
        _insert_taggings(db, (7, 563, 0, "credits", 1_297_000, DUR, CREDITS_FINAL_ROW_EXTRA))
        return folder, db

    def test_wanted_types_are_written_and_others_left_alone(self, tmp_path):
        folder, db = self._native(tmp_path)
        assert _write_one(_publisher(tmp_path, folder), [INTRO], previous=None) == [INTRO]
        assert _rows(db, "SELECT [index], text, time_offset FROM taggings ORDER BY [index]") == [
            (0, "credits", 1_297_000),
            (1, "intro", 11_000),
        ]
        extra = json.loads(_rows(db, "SELECT extra_data FROM media_parts WHERE id=1")[0][0])
        assert extra["pv:credits"] == NATIVE_CREDITS and extra["pv:intros"] == INTRO_PAYLOAD

    def test_nothing_wanted_is_a_no_op(self, tmp_path, sql_log):
        folder, db = self._native(tmp_path)
        before = _rows(db, "SELECT * FROM taggings"), _rows(db, "SELECT extra_data FROM media_parts")
        assert _write_one(_publisher(tmp_path, folder), [], previous=None) == []
        assert sql_log == []
        assert (_rows(db, "SELECT * FROM taggings"), _rows(db, "SELECT extra_data FROM media_parts")) == before


class TestLockTimeouts:
    def _paused_writer(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        inside, release = threading.Event(), threading.Event()
        real_extra = plex_db._tagging_extra

        def paused_extra(marker, final):
            inside.set()
            release.wait(10)
            return real_extra(marker, final)

        monkeypatch.setattr(plex_db, "_tagging_extra", paused_extra)
        writer = threading.Thread(target=_write_one, args=(pub, [INTRO]))
        writer.start()
        assert inside.wait(5)
        return db, pub, writer, release

    def test_calls_waiting_on_the_database_lock_give_up_after_the_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.5)
        db, pub, writer, release = self._paused_writer(tmp_path, monkeypatch)
        try:
            start = time.monotonic()
            with pytest.raises(PublishError) as ei:
                _write_one(pub, [INTRO])
            assert ei.value.state is Capability.UNREACHABLE
            assert time.monotonic() - start < 0.5 + 1.0
            start = time.monotonic()
            assert pub.capability().state is Capability.UNREACHABLE
            assert time.monotonic() - start < 0.5 + 1.0
        finally:
            release.set()
            writer.join(10)
        assert _rows(db, "SELECT text FROM taggings") == [("intro",)]

    def test_no_convoy_behind_plex_s_write_lock(self, tmp_path, monkeypatch):
        # Every call waits at most BUSY_TIMEOUT_S in total, however many threads queue on the same database.
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 2.0)
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        results = []

        def timed(name, call):
            start = time.monotonic()
            try:
                outcome = call()
            except PublishError as exc:
                outcome = exc.state
            results.append((name, time.monotonic() - start, outcome))

        locker = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
        locker.execute("BEGIN IMMEDIATE")  # Plex writing
        try:
            threads = [
                threading.Thread(target=timed, args=(f"w{n}", lambda: _write_one(pub, [INTRO]))) for n in range(4)
            ]
            threads.append(threading.Thread(target=timed, args=("capability", lambda: pub.capability().state)))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert len(results) == 5
        assert all(elapsed < 2.0 + 1.0 for _name, elapsed, _outcome in results), results
        assert {outcome for name, _e, outcome in results if name.startswith("w")} == {Capability.UNREACHABLE}

    def test_one_deadline_covers_both_the_lock_wait_and_plex_s_write_lock(self, tmp_path, monkeypatch):
        # A write that gets this process's lock late must not then wait a full BUSY_TIMEOUT_S on Plex.
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 2.0)
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        locker = sqlite3.connect(db, isolation_level=None, check_same_thread=False)
        locker.execute("BEGIN IMMEDIATE")  # Plex writing for longer than we wait
        lock = plex_db._db_lock(str(db))
        lock.acquire()
        threading.Timer(1.5, lock.release).start()  # another task of ours finishing
        start = time.monotonic()
        try:
            with pytest.raises(PublishError) as ei:
                _write_one(pub, [INTRO])
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert ei.value.state is Capability.UNREACHABLE
        assert time.monotonic() - start < 2.0 + 0.8

    def test_version_check_runs_with_no_plex_connection_or_lock_held(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/A.mkv", None), ("/data/tv/B.mkv", None)))
        seen = []

        def sibling(path):
            seen.append((plex_db._db_lock(str(db)).locked(), set(getattr(plex_db._thread_state, "open", set()))))
            return {T.INTRO: INTRO}

        _write_one(_publisher(tmp_path, folder, sibling_markers=sibling), [INTRO], path="/data/tv/A.mkv")
        assert seen == [(False, set())]
        assert _rows(db, "SELECT text FROM taggings") == [("intro",)]

    def test_a_failing_sibling_lookup_is_not_a_plex_error(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/A.mkv", None), ("/data/tv/B.mkv", None)))

        def sibling(path):
            raise sqlite3.OperationalError("database is locked")  # markers.db, not Plex

        with pytest.raises(PublishError, match="other version") as ei:
            _write_one(_publisher(tmp_path, folder, sibling_markers=sibling), [INTRO], path="/data/tv/A.mkv")
        assert ei.value.state is None
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_database_vanishing_before_the_ownership_message_is_still_a_report(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        monkeypatch.setattr(
            PlexMarkerPublisher, "_unwritable_paths", staticmethod(lambda path: (os.remove(path), [path])[1])
        )
        report = _publisher(tmp_path, folder).capability()
        assert report.state is Capability.MISCONFIGURED and "can't write" in report.message


class TestLockSerialisation:
    """The real probe against every kind of open connection (read, capability, write)."""

    @pytest.fixture(autouse=True)
    def plex_holds_the_database(self):
        """Overrides the module fixture: the real probe runs."""

    @pytest.fixture(autouse=True)
    def journal_mode(self):
        """Overrides the module fixture: WAL only, the probe needs a -shm file."""

    @pytest.mark.parametrize("holder", ["capability", "write"])
    def test_probe_waits_for_every_open_connection(self, tmp_path, monkeypatch, holder):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        pub = _publisher(tmp_path, folder)
        inside, release, probed = threading.Event(), threading.Event(), threading.Event()
        pause_in = {"capability": "_check_library_marker_versions", "write": "_plan"}[holder]
        real_pause, real_probe = getattr(PlexMarkerPublisher, pause_in), plex_db._shm_dms_locked_elsewhere
        paused = []

        def pausing(*args, **kwargs):
            if not paused:
                paused.append(True)
                inside.set()
                release.wait(10)
            return real_pause(*args, **kwargs)

        monkeypatch.setattr(PlexMarkerPublisher, pause_in, staticmethod(pausing) if pause_in != "_plan" else pausing)
        monkeypatch.setattr(plex_db, "_shm_dms_locked_elsewhere", lambda path: (probed.set(), real_probe(path))[1])
        operation = {
            "capability": pub.capability,
            "write": lambda: _write_one(pub, [INTRO]),
        }
        with _another_process_holding(db):
            first = threading.Thread(target=operation[holder])
            first.start()
            assert inside.wait(5)
            probed.clear()
            reports = []
            checker = threading.Thread(target=lambda: reports.append(pub.capability()))
            checker.start()
            try:
                assert not probed.wait(0.5), f"probe ran while the {holder} connection was open"
            finally:
                release.set()
            assert probed.wait(5)
            first.join(10)
            checker.join(10)
        assert reports[0].state is Capability.READY

    def test_leftover_shm_without_a_holder_is_not_ready(self, tmp_path):
        # A Plex killed with SIGKILL (or the unRAID split path) leaves a -shm with nobody locking it.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(db)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        assert proc.stdout.readline().strip() == "ready"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(10)
        assert os.path.exists(f"{db}-shm")
        pub = _publisher(tmp_path, folder)
        assert pub.capability().state is Capability.NEEDS_LOCAL_DB
        with pytest.raises(PublishError) as ei:
            _write_one(pub, [INTRO])
        assert ei.value.state is Capability.UNREACHABLE
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 0

    def test_a_failing_lock_query_is_not_a_holder(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")

        def failing_fcntl(*_args):
            raise OSError("no locks here")

        with _another_process_holding(db):
            monkeypatch.setattr(plex_db.fcntl, "fcntl", failing_fcntl)
            assert _publisher(tmp_path, folder).capability().state is Capability.NEEDS_LOCAL_DB

    def test_probe_takes_the_database_lock_itself(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        lock = plex_db._db_lock(str(db))
        lock.acquire()
        try:
            start = time.monotonic()
            with pytest.raises(PublishError) as ei:
                plex_db.shm_lock_held_elsewhere(str(db), deadline=time.monotonic() + 0.2)
            assert ei.value.state is Capability.UNREACHABLE and time.monotonic() - start < 2.0
        finally:
            lock.release()

    def test_capability_reports_a_busy_database_lock_instead_of_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.3)
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        lock = plex_db._db_lock(str(db))
        lock.acquire()
        try:
            report = _publisher(tmp_path, folder).capability()
        finally:
            lock.release()
        assert report.state is Capability.UNREACHABLE and "still using this Plex database" in report.message

    def test_a_failed_connect_leaves_no_open_connection_behind(self, tmp_path, monkeypatch):
        # Otherwise every later probe on this thread would refuse to run ("inside an open connection").
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        pub = _publisher(tmp_path, folder)
        original, calls = PlexMarkerPublisher._connect, []

        def failing_once(self, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError("unable to open database file")
            return original(self, **kwargs)

        monkeypatch.setattr(PlexMarkerPublisher, "_connect", failing_once)
        with pytest.raises(sqlite3.OperationalError), pub._database(read_only=True, deadline=time.monotonic() + 5):
            pass
        assert plex_db.shm_lock_held_elsewhere(str(db)) is False  # runs; no holder in this test

    def test_probe_inside_our_own_open_connection_is_refused(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        pub = _publisher(tmp_path, folder)
        with pub._database(read_only=True, deadline=time.monotonic() + 5):
            with pytest.raises(RuntimeError, match="open connection"):
                plex_db.shm_lock_held_elsewhere(str(db))

    def test_one_lock_for_every_path_to_the_same_database(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="wal")
        dotted = f"{folder}/../{folder.name}/Plug-in Support/Databases/{db.name}"
        (tmp_path / "symlinked").symlink_to(folder)
        via_symlink = tmp_path / "symlinked" / "Plug-in Support" / "Databases" / db.name
        hardlink = tmp_path / "second-mount.db"  # same inode, like a second bind mount of the folder
        os.link(db, hardlink)
        lock = plex_db._db_lock(str(db))
        assert all(plex_db._db_lock(str(p)) is lock for p in (dotted, via_symlink, hardlink))
        missing = tmp_path / "missing" / "x.db"
        assert plex_db._db_lock(f"{tmp_path}/missing/../missing/x.db") is plex_db._db_lock(str(missing))
        assert plex_db._db_lock(str(missing)) is not lock


class TestPlexVersionDetail:
    @pytest.mark.parametrize(("plex_pass", "version"), [(True, PLEX_VERSION), (None, None)])
    def test_capability_reports_the_plex_version(self, tmp_path, plex_pass, version):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        report = _publisher(tmp_path, folder, plex_pass=plex_pass).capability()
        assert report.state is Capability.READY and report.details["plex_version"] == version


class TestItemDesiredSet:
    """Plex serves one marker set per item: write() computes it across every version (plex-item-publish-design.md)."""

    A, B = "/data/tv/E - 1080p.mkv", "/data/tv/E - 2160p.mkv"
    EARLY_CREDITS = Marker(T.CREDITS, 1_280_000, DUR, ("chapters",))  # 19 s earlier than CREDITS_FINAL

    def _item(self, tmp_path, files, decided, *, native_credits=False):
        folder = tmp_path / "Plex Media Server"
        extra = _plex_json({"pv:credits": NATIVE_CREDITS}) if native_credits else None
        db = _make_db(folder, parts=tuple((f, extra) for f in files))
        pub = _publisher(tmp_path, folder, sibling_markers=decided.get)
        return db, pub

    def _rows_and_parts(self, db):
        rows = _rows(
            db, "SELECT id, [index], text, time_offset, end_time_offset, extra_data FROM taggings ORDER BY [index]"
        )
        parts = [json.loads(r[0]) if r[0] else {} for r in _rows(db, "SELECT extra_data FROM media_parts ORDER BY id")]
        return rows, parts

    def test_a_version_added_later_that_disagrees_removes_the_first_version_s_credits(self, tmp_path):
        # t8rr2/added_version.py: A publishes alone; B is added to the item with different credits.
        decided = {self.A: {T.INTRO: INTRO, T.CREDITS: CREDITS_FINAL}}
        db, pub = self._item(tmp_path, (self.A,), decided)
        item_row = _write_one(pub, [INTRO, CREDITS_FINAL], path=self.A)
        assert item_row == [INTRO, CREDITS_FINAL]
        _exec(db, "INSERT INTO media_items (id, metadata_item_id) VALUES (2, 7)")
        _exec(db, "INSERT INTO media_parts (id, media_item_id, file) VALUES (2, 2, ?)", self.B)
        decided[self.B] = {T.INTRO: INTRO, T.CREDITS: self.EARLY_CREDITS}
        # B's run: previous is the item row (what this app last left on the item, from any file).
        item_row = _write_one(pub, [INTRO, self.EARLY_CREDITS], previous=item_row, path=self.B)
        assert item_row == [INTRO]
        rows, parts = self._rows_and_parts(db)
        assert [(r[2], r[3]) for r in rows] == [("intro", 11_000)]
        assert all("pv:credits" not in extra and extra["pv:intros"] == INTRO_PAYLOAD for extra in parts)
        # A's next run sees the same item and returns the same set, writing nothing.
        before = self._rows_and_parts(db)
        assert _write_one(pub, [INTRO, CREDITS_FINAL], previous=item_row, path=self.A) == [INTRO]
        assert self._rows_and_parts(db) == before

    def test_partial_agreement_then_the_other_type(self, tmp_path):
        # t8rr2/partial.py: agree on intro, disagree on credits; later intro goes to review and credits agree.
        decided = {self.B: {T.INTRO: INTRO, T.CREDITS: CREDITS_FINAL}}
        db, pub = self._item(tmp_path, (self.A, self.B), decided)
        item_row = _write_one(pub, [INTRO, CREDITS_FINAL], path=self.A)
        assert item_row == [INTRO, CREDITS_FINAL]
        decided[self.B] = {T.INTRO: INTRO, T.CREDITS: self.EARLY_CREDITS}
        item_row = _write_one(pub, [INTRO, CREDITS_FINAL], previous=item_row, path=self.A)
        assert item_row == [INTRO]
        assert [r[2] for r in self._rows_and_parts(db)[0]] == ["intro"]
        decided[self.B] = {T.CREDITS: CREDITS_FINAL}  # both versions' intros in review, credits agree again
        item_row = _write_one(pub, [CREDITS_FINAL], previous=item_row, path=self.A)
        assert item_row == [CREDITS_FINAL]
        rows, parts = self._rows_and_parts(db)
        assert [(r[2], r[3]) for r in rows] == [("credits", 1_297_000)]  # our intro row is gone
        assert all("pv:intros" not in extra and extra["pv:credits"] == CREDITS_FINAL_PAYLOAD for extra in parts)

    def test_an_undecided_version_s_types_are_removed_when_they_are_provably_ours(self, tmp_path):
        decided = {self.B: {T.INTRO: INTRO}}
        db, pub = self._item(tmp_path, (self.A, self.B), decided, native_credits=True)
        plex_credits = _insert_taggings(db, (7, 563, 0, "credits", 1_154_521, 1_188_521, CREDITS_ROW_EXTRA))[0]
        item_row = _write_one(pub, [INTRO], path=self.A)
        assert item_row == [INTRO]
        decided[self.B] = None  # re-decided file became undecided (identity changed, not probed yet)
        assert _write_one(pub, [INTRO], previous=item_row, path=self.A) == []
        rows, parts = self._rows_and_parts(db)
        assert rows == [(plex_credits, 0, "credits", 1_154_521, 1_188_521, CREDITS_ROW_EXTRA)]  # Plex's own row stays
        assert all("pv:intros" not in extra and extra["pv:credits"] == NATIVE_CREDITS for extra in parts)

    def test_previous_unknown_removes_nothing_but_still_writes_what_is_desired(self, tmp_path):
        decided = {self.B: {T.INTRO: INTRO, T.CREDITS: CREDITS_FINAL}}
        db, pub = self._item(tmp_path, (self.A, self.B), decided)
        _write_one(pub, [INTRO, CREDITS_FINAL], path=self.A)
        decided[self.B] = {T.INTRO: INTRO, T.CREDITS: self.EARLY_CREDITS}
        assert _write_one(pub, [INTRO, CREDITS_FINAL], previous=None, path=self.A) == [INTRO]
        assert [r[2] for r in self._rows_and_parts(db)[0]] == ["credits", "intro"]  # nothing provably ours to remove

    def test_an_atomic_failure_keeps_the_item_row_true_so_a_later_run_removes_our_rows(self, tmp_path, monkeypatch):
        decided = {}
        db, pub = self._item(tmp_path, (self.A,), decided)
        item_row = _write_one(pub, [INTRO, CREDITS_FINAL], path=self.A)
        before = self._rows_and_parts(db)
        # A transient Plex failure on the next run: atomic, so nothing changed and the caller keeps the item row.
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.2)
        locker = sqlite3.connect(db, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(PublishError) as ei:
                _write_one(pub, [INTRO], previous=item_row, path=self.A)
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert ei.value.state is Capability.UNREACHABLE and PlexMarkerPublisher.atomic_writes is True
        assert self._rows_and_parts(db) == before
        # The next run passes the kept item row: credits are no longer wanted and our old rows go.
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 30.0)
        assert _write_one(pub, [INTRO], previous=item_row, path=self.A) == [INTRO]
        rows, parts = self._rows_and_parts(db)
        assert [r[2] for r in rows] == ["intro"] and "pv:credits" not in parts[0]

    def test_the_returned_set_is_sorted_by_start_whatever_the_type(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        early_credits = Marker(T.CREDITS, 5_000, 9_000, ("user",))  # a user-locked oddity: credits before the intro
        assert _write_one(_publisher(tmp_path, folder), [INTRO, early_credits]) == [early_credits, INTRO]

    def test_only_plex_is_atomic(self):
        assert PlexMarkerPublisher.atomic_writes is True
        assert MarkerPublisher.atomic_writes is False


class TestRoundTwoLows:
    def test_pv_final_zero_is_not_final(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        _insert_taggings(
            db,
            (
                7,
                563,
                0,
                "credits",
                1_297_000,
                DUR,
                '{"pv:final":"0","pv:version":"4","url":"pv%3Afinal=0&pv%3Aversion=4"}',
            ),
        )
        assert _served(db) == [(T.CREDITS, 1_299_000, DUR - 2_000)]


class TestAgreeingVersionsDoNotPingPong:
    """Versions that agree within 2 s keep what this app already left on the item (combined review, MED ping-pong)."""

    A, B = "/data/tv/E - 1080p.mkv", "/data/tv/E - 2160p.mkv"
    CREDITS_A = Marker(T.CREDITS, 1_299_000, DUR, ("chapters",))
    CREDITS_B = Marker(T.CREDITS, 1_299_000, DUR + 800, ("chapters",))  # same chapter, runtime 800 ms longer

    def _item(self, tmp_path, decided):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.A, None), (self.B, None)))
        return db, _publisher(tmp_path, folder, sibling_markers=decided.get)

    def test_alternating_runs_of_agreeing_versions_commit_once(self, tmp_path, sql_log):
        decided = {
            self.A: {T.INTRO: INTRO, T.CREDITS: self.CREDITS_A},
            self.B: {T.INTRO: INTRO, T.CREDITS: self.CREDITS_B},
        }
        db, pub = self._item(tmp_path, decided)
        item_row: list[Marker] = []
        for run in range(6):
            path, credits, duration = (
                (self.A, self.CREDITS_A, DUR) if run % 2 == 0 else (self.B, self.CREDITS_B, DUR + 800)
            )
            item_row = _write_one(pub, [INTRO, credits], previous=item_row, duration_ms=duration, path=path)
            assert item_row == [INTRO, self.CREDITS_A]  # the first writer's times stay
        assert sql_log.count("COMMIT") == 1
        assert _rows(db, "SELECT text, time_offset, end_time_offset FROM taggings ORDER BY [index]") == [
            ("credits", 1_297_000, DUR),
            ("intro", 11_000, 37_000),
        ]

    @pytest.mark.parametrize(
        ("mine", "sibling"),
        [
            (1_300_500, 1_301_500),  # what we left (1_299_000) is 2.5 s from the other version
            (1_301_500, 1_300_500),  # ... or 2.5 s from the calling file
        ],
        ids=["far-from-the-other-version", "far-from-the-calling-file"],
    )
    def test_the_calling_file_s_times_win_once_ours_no_longer_agree_with_every_version(self, tmp_path, mine, sibling):
        mine_credits = Marker(T.CREDITS, mine, DUR, ("chapters",))
        decided = {self.B: {T.CREDITS: Marker(T.CREDITS, sibling, DUR, ("chapters",))}}
        db, pub = self._item(tmp_path, decided)
        item_row = _write_one(pub, [mine_credits], previous=[self.CREDITS_A], path=self.A)
        assert item_row == [mine_credits]
        assert _rows(db, "SELECT time_offset FROM taggings") == [(mine - 2_000,)]

    def test_previous_of_another_type_or_unknown_is_never_kept(self, tmp_path):
        decided = {self.B: {T.INTRO: INTRO}}
        _db, pub = self._item(tmp_path, decided)
        assert _write_one(pub, [INTRO], previous=[CREDITS_FINAL], path=self.A) == [INTRO]
        assert _write_one(pub, [INTRO], previous=None, path=self.A) == [INTRO]


class TestOwnPreviousAfterTheItemChanged:
    """A file moved to another Plex item: its part's keys it left behind go (combined review, MED merge/split)."""

    A, B = "/data/tv/E - 1080p.mkv", "/data/tv/E - 2160p.mkv"
    INTRO_REAL = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
    CREDITS_A = Marker(T.CREDITS, 1_295_324, DUR, ("chapters",))
    CREDITS_B = Marker(T.CREDITS, 1_276_324, DUR, ("chapters",))

    def _keys(self, db):
        return [
            sorted(k for k in json.loads(r[0] or "{}") if k.startswith("pv:"))
            for r in _rows(db, "SELECT extra_data FROM media_parts ORDER BY id")
        ]

    def test_merge_removes_the_moved_part_s_own_credits_key(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.A, None),))
        _exec(db, "INSERT INTO metadata_items (id, metadata_type, title) VALUES (8, 4, 'Ep dup')")
        _exec(db, "INSERT INTO media_items (id, metadata_item_id) VALUES (2, 8)")
        _exec(db, "INSERT INTO media_parts (id, media_item_id, file) VALUES (2, 2, ?)", self.B)
        decided = {
            self.A: {T.INTRO: self.INTRO_REAL, T.CREDITS: self.CREDITS_A},
            self.B: {T.INTRO: self.INTRO_REAL, T.CREDITS: self.CREDITS_B},
        }
        pub = _publisher(tmp_path, folder, sibling_markers=decided.get)
        item7 = _write_one(pub, [self.INTRO_REAL, self.CREDITS_A], path=self.A, item_id="7")
        b_on_8 = _write_one(pub, [self.INTRO_REAL, self.CREDITS_B], path=self.B, item_id="8")
        assert b_on_8 == [self.INTRO_REAL, self.CREDITS_B]
        # The user merges item 8 into 7.
        _exec(db, "UPDATE media_items SET metadata_item_id=7 WHERE id=2")
        _exec(db, "DELETE FROM taggings WHERE metadata_item_id=8")
        ours = _write_one(
            pub, [self.INTRO_REAL, self.CREDITS_B], previous=item7, own_previous=b_on_8, path=self.B, item_id="7"
        )
        assert ours == [self.INTRO_REAL]
        assert _rows(db, "SELECT text FROM taggings WHERE metadata_item_id=7") == [("intro",)]
        assert self._keys(db) == [["pv:intros"], ["pv:intros"]]

    @pytest.mark.parametrize(
        ("own_previous", "key_after"),
        [
            ("ours", False),
            (None, True),  # not given: the key isn't provably ours on this item
            ("other-times", True),  # e.g. Plex re-detected on the part since
        ],
    )
    def test_split_removes_the_moved_part_s_key_only_when_it_is_exactly_ours(self, tmp_path, own_previous, key_after):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.A, None), (self.B, None)))
        decided = {
            self.A: {T.INTRO: self.INTRO_REAL, T.CREDITS: self.CREDITS_A},
            self.B: {T.INTRO: self.INTRO_REAL, T.CREDITS: self.CREDITS_A},
        }
        pub = _publisher(tmp_path, folder, sibling_markers=decided.get)
        item7 = _write_one(pub, [self.INTRO_REAL, self.CREDITS_A], path=self.A, item_id="7")
        assert item7 == [self.INTRO_REAL, self.CREDITS_A]
        rows7 = _rows(db, "SELECT * FROM taggings ORDER BY id")
        # The user splits B off into item 8; B's credits then go to review.
        _exec(db, "INSERT INTO metadata_items (id, metadata_type, title) VALUES (8, 4, 'Ep split')")
        _exec(db, "UPDATE media_items SET metadata_item_id=8 WHERE id=2")
        own = {"ours": item7, None: None, "other-times": [self.INTRO_REAL, self.CREDITS_B]}[own_previous]
        ours = _write_one(pub, [self.INTRO_REAL], previous=[], own_previous=own, path=self.B, item_id="8")
        assert ours == [self.INTRO_REAL]
        assert _rows(db, "SELECT * FROM taggings WHERE metadata_item_id=7 ORDER BY id") == rows7  # rows unaffected
        keys_a, keys_b = self._keys(db)
        assert keys_a == ["pv:credits", "pv:intros"]  # only the calling part is touched
        assert ("pv:credits" in keys_b) is key_after and "pv:intros" in keys_b

    def test_another_part_s_key_is_not_ours_to_remove_by_own_previous(self, tmp_path):
        # Both parts carry the same credits key, but only the moved (calling) part got it from us.
        folder = tmp_path / "Plex Media Server"
        payload = {"pv:credits": _part_payload_for(self.CREDITS_B)}
        db = _make_db(folder, parts=((self.A, encode_extra_data(payload)), (self.B, encode_extra_data(payload))))
        decided = {self.A: {T.INTRO: self.INTRO_REAL}}
        pub = _publisher(tmp_path, folder, sibling_markers=decided.get)
        ours = _write_one(
            pub, [self.INTRO_REAL], previous=[], own_previous=[self.INTRO_REAL, self.CREDITS_B], path=self.B
        )
        assert ours == [self.INTRO_REAL]
        assert self._keys(db) == [["pv:credits", "pv:intros"], ["pv:intros"]]

    def test_own_previous_alone_is_enough_to_open_the_database(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.B, _plex_json({"pv:credits": CREDITS_FINAL_PAYLOAD})),))
        assert (
            _write_one(_publisher(tmp_path, folder), [], previous=[], own_previous=[CREDITS_FINAL], path=self.B) == []
        )
        assert self._keys(db) == [[]]


class TestServedTimesDecideWhatIsAlreadyThere:
    """Final review LOW-1/LOW-2: no rewrite when Plex already serves the desired times; no write lock for a no-op."""

    A, B = "/data/tv/E - 1080p.mkv", "/data/tv/E - 2160p.mkv"
    CREDITS = Marker(T.CREDITS, 1_295_324, DUR - 1_000, ("chapters",))  # final for A, not final for B (1.5 s longer)

    def test_a_final_flag_flip_from_another_runtime_does_not_rewrite(self, tmp_path, sql_log):
        # rr-final/flag_pingpong.py: intros disagree, credits agree; each caller's runtime flips the credits' final flag.
        folder = tmp_path / "Plex Media Server"
        _make_db(folder, parts=((self.A, None), (self.B, None)))
        intro_a, intro_b = INTRO, Marker(T.INTRO, 61_000, 97_000, ("chapters",))
        decided = {
            self.A: {T.INTRO: intro_a, T.CREDITS: self.CREDITS},
            self.B: {T.INTRO: intro_b, T.CREDITS: self.CREDITS},
        }
        pub = _publisher(tmp_path, folder, sibling_markers=decided.get)
        item_row: list[Marker] = []
        for run in range(8):
            path, intro, duration = (self.A, intro_a, DUR) if run % 2 == 0 else (self.B, intro_b, DUR + 1_500)
            item_row = _write_one(pub, [intro, self.CREDITS], previous=item_row, duration_ms=duration, path=path)
            assert item_row == [self.CREDITS]
            assert _served(Path(plex_db_path(str(folder)))) == [(T.CREDITS, 1_295_324, DUR - 1_000)]
        assert sql_log.count("COMMIT") == 1

    def test_rows_and_key_serving_the_desired_times_are_left_as_they_are(self, tmp_path, sql_log):
        folder = tmp_path / "Plex Media Server"
        stored_as_not_final = (
            '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
            '"MediaPartMarker":[{"startTimeOffset":1293324,"endTimeOffset":1321000}]}}'
        )
        db = _make_db(folder, parts=((self.A, encode_extra_data({"pv:credits": stored_as_not_final})),))
        _insert_taggings(db, (7, 563, 0, "credits", 1_293_324, DUR + 1_000, CREDITS_ROW_EXTRA))
        before = _rows(db, "SELECT * FROM taggings"), _rows(db, "SELECT extra_data FROM media_parts")
        # Same served times (1_295_324 .. DUR-1000); this caller's duration would store them as final.
        assert _write_one(_publisher(tmp_path, folder), [self.CREDITS], path=self.A) == [self.CREDITS]
        assert (_rows(db, "SELECT * FROM taggings"), _rows(db, "SELECT extra_data FROM media_parts")) == before
        assert _writes(sql_log) == []

    def test_a_no_op_write_never_takes_plex_s_write_lock(self, tmp_path, monkeypatch, sql_log):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        item_row = _write_one(pub, [INTRO, CREDITS_FINAL])
        sql_log.clear()
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.5)
        locker = sqlite3.connect(db, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")  # Plex writing for longer than our deadline
        try:
            start = time.monotonic()
            assert _write_one(pub, [INTRO, CREDITS_FINAL], previous=item_row) == [INTRO, CREDITS_FINAL]
            assert time.monotonic() - start < 0.5
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert not [sql for sql in sql_log if sql.startswith("BEGIN")]

    def test_a_real_change_still_waits_for_and_takes_the_write_lock(self, tmp_path, monkeypatch, sql_log):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.5)
        locker = sqlite3.connect(db, isolation_level=None)
        locker.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(PublishError) as ei:
                _write_one(pub, [INTRO])
        finally:
            locker.execute("ROLLBACK")
            locker.close()
        assert ei.value.state is Capability.UNREACHABLE
        assert "BEGIN IMMEDIATE" in sql_log

    def test_a_file_renamed_after_the_first_read_is_not_taken_as_a_no_op(self, tmp_path, sql_log):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((self.A, None), (self.B, None)))
        rename = []

        def sibling(path):
            if rename:  # between the item read and the no-op snapshot: the desired set was judged on the old files
                with contextlib.closing(sqlite3.connect(db, isolation_level=None)) as plex:
                    plex.execute("UPDATE media_parts SET file='/data/tv/E - 720p.mkv' WHERE id=2")
            return {T.INTRO: INTRO}

        pub = _publisher(tmp_path, folder, sibling_markers=sibling)
        item_row = _write_one(pub, [INTRO], path=self.A)
        rename.append(True)
        with pytest.raises(PublishError, match="changed"):
            _write_one(pub, [INTRO], previous=item_row, path=self.A)

    def test_a_change_made_by_someone_else_while_waiting_for_the_lock_is_not_written_again(self, tmp_path, sql_log):
        reference = tmp_path / "reference" / "Plex Media Server"
        ref_db = _make_db(reference)
        _write_one(_publisher(tmp_path / "reference", reference), [INTRO])
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        sql_log.clear()
        locker = sqlite3.connect(db, check_same_thread=False, isolation_level=None)
        locker.execute("ATTACH DATABASE ? AS ref", (str(ref_db),))
        locker.execute("BEGIN IMMEDIATE")  # the same markers, not yet committed when our snapshot is read
        locker.execute("INSERT INTO taggings SELECT * FROM ref.taggings")
        locker.execute("UPDATE media_parts SET extra_data=(SELECT extra_data FROM ref.media_parts WHERE id=1)")
        threading.Timer(0.3, lambda: (locker.execute("COMMIT"), locker.close())).start()
        assert _write_one(_publisher(tmp_path, folder), [INTRO]) == [INTRO]
        assert "BEGIN IMMEDIATE" in sql_log
        assert "COMMIT" not in sql_log
        assert _rows(db, "SELECT COUNT(*) FROM taggings")[0][0] == 1


class TestReadBackMany:
    """Check servers reads every published Plex item back: one connection per item, the lock proof once per call."""

    def _items(self, keys, ours=(INTRO,)):
        return [(str(k), list(ours), frozenset(), None) for k in keys]

    def test_shows_many_answers_what_shows_answers(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO, CREDITS_FINAL])
        files = ("/data/tv/S01E01.mkv",)
        items = [("7", [INTRO, CREDITS_FINAL], frozenset(), files), ("not-a-key", [INTRO], frozenset(), None)]
        assert pub.shows_many(items) == {"7": Shown.OURS, "not-a-key": None}
        assert pub.shows("7", [INTRO, CREDITS_FINAL], item_files=files) is Shown.OURS
        assert pub.shows_many([("7", [INTRO], frozenset(), ("/data/tv/other.mkv",))]) == {"7": Shown.VERSIONS_CHANGED}
        other_intro = Marker(T.INTRO, 12_000, 37_000, ("chapters",))
        assert pub.shows_many([("7", [other_intro], frozenset(), None)]) == {"7": Shown.REPLACED}
        assert pub.shows_many([("7", [], frozenset({T.CREDITS}), None), ("8", [INTRO], frozenset(), None)]) == {
            "7": Shown.OURS,
            "8": Shown.GONE,  # Plex has no item 8
        }
        assert pub.shows("8", [INTRO]) is Shown.GONE

    def test_one_connection_per_item_and_the_checks_once_per_call(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        opened, local_checks, schema_checks = [], [], []
        real_database, real_local, real_schema = (
            PlexMarkerPublisher._database,
            PlexMarkerPublisher._local_checks,
            (PlexMarkerPublisher._check_schema),
        )

        def counting_database(self, **kwargs):
            opened.append(kwargs["read_only"])
            return real_database(self, **kwargs)

        monkeypatch.setattr(PlexMarkerPublisher, "_database", counting_database)
        monkeypatch.setattr(
            PlexMarkerPublisher, "_local_checks", lambda self, **kw: local_checks.append(1) or real_local(self, **kw)
        )
        monkeypatch.setattr(
            PlexMarkerPublisher,
            "_check_schema",
            staticmethod(lambda conn: schema_checks.append(1) or real_schema(conn)),
        )
        out = pub.shows_many(self._items([7, 8, "x", 9, 10]))
        assert out == {"7": Shown.MISSING, "8": Shown.GONE, "x": None, "9": Shown.GONE, "10": Shown.GONE}
        assert opened == [True, True, True, True]  # no connection for an id that isn't a rating key
        assert (len(local_checks), len(schema_checks)) == (1, 1)

    def test_a_row_this_code_cant_read_skips_only_that_item(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=(("/data/tv/S01E01.mkv", None),))
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO])
        _exec(db, "INSERT INTO metadata_items (id, metadata_type, title) VALUES (8, 4, 'Ep 2')")
        _exec(db, "INSERT INTO metadata_items (id, metadata_type, title) VALUES (9, 4, 'Ep 3')")
        _exec(db, "INSERT INTO taggings (metadata_item_id, tag_id, text, time_offset, end_time_offset) "
                  "VALUES (8, 563, 'credits', NULL, 1320000)")  # fmt: skip
        out = pub.shows_many(self._items([7, 8, 9]))
        assert out == {"7": Shown.OURS, "8": None, "9": Shown.MISSING}

    def test_an_sqlite_error_on_one_item_answers_none_for_it_and_the_next_item_is_still_read(
        self, tmp_path, monkeypatch
    ):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO])
        real_shown = PlexMarkerPublisher._shown_in
        calls = []

        def failing_second(self, *args):
            calls.append(args[2])  # the rating key
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk I/O error")
            return real_shown(self, *args)

        monkeypatch.setattr(PlexMarkerPublisher, "_shown_in", failing_second)
        assert pub.shows_many(self._items(["7", "8", "7"])) == {"7": Shown.OURS, "8": None}
        assert calls == [7, 8, 7]

    def test_once_the_database_cant_be_read_at_all_the_rest_are_none(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        _write_one(pub, [INTRO])
        real_database = PlexMarkerPublisher._database
        opened = []

        def busy_after_the_first(self, **kwargs):
            opened.append(1)
            if len(opened) == 2:
                raise PublishError("Another Intro & Credits task is still using this Plex database",
                                   state=Capability.UNREACHABLE)  # fmt: skip
            return real_database(self, **kwargs)

        monkeypatch.setattr(PlexMarkerPublisher, "_database", busy_after_the_first)
        assert pub.shows_many(self._items(["7", "8", "9"])) == {"7": Shown.OURS, "8": None, "9": None}
        assert len(opened) == 2

    @pytest.mark.parametrize(
        "problem", ["settings-off", "no-lock-holder", "schema"], ids=["off", "plex-stopped", "schema-changed"]
    )
    def test_nothing_is_read_when_the_database_cant_be_shared(self, tmp_path, monkeypatch, problem):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder, enabled=problem != "settings-off")
        if problem == "no-lock-holder":
            monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: False)
        elif problem == "schema":
            _exec(db, "CREATE TRIGGER t AFTER INSERT ON taggings BEGIN SELECT 1; END")
        connected = []
        monkeypatch.setattr(
            PlexMarkerPublisher, "_connect", lambda self, **kw: connected.append(kw) or sqlite3.connect(db)
        )
        assert pub.shows_many(self._items(["7", "8"])) == {"7": None, "8": None}
        assert len(connected) == (1 if problem == "schema" else 0)  # a changed schema stops the call after one look

    @pytest.mark.parametrize(
        ("case", "missing"),
        [("item-there", False), ("item-deleted", True), ("not-a-rating-key", None), ("plex-stopped", None),
         ("database-busy", None)],
    )  # fmt: skip
    def test_item_missing_asks_plexs_database(self, tmp_path, monkeypatch, case, missing):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)  # item 7
        pub = _publisher(tmp_path, folder)
        item_id = {"item-deleted": "8", "not-a-rating-key": "x"}.get(case, "7")
        if case == "plex-stopped":
            monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: False)
        elif case == "database-busy":

            def busy(self, **kwargs):
                raise PublishError("Another Intro & Credits task is still using this Plex database",
                                   state=Capability.UNREACHABLE)  # fmt: skip

            monkeypatch.setattr(PlexMarkerPublisher, "_database", busy)
        assert pub.item_missing(item_id) is missing

    def test_a_write_waiting_for_the_database_gets_it_between_items(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        reader, writer = _publisher(tmp_path, folder), _publisher(tmp_path, folder)
        real_shown = PlexMarkerPublisher._shown_in
        reading = threading.Event()

        def slow(self, *args):
            reading.set()
            time.sleep(0.01)
            return real_shown(self, *args)

        monkeypatch.setattr(PlexMarkerPublisher, "_shown_in", slow)
        monkeypatch.setattr(plex_db, "BUSY_TIMEOUT_S", 0.5)  # the write gives up if it never gets the lock
        finished = {}

        def read_back():
            reader.shows_many(self._items(range(1, 101)))
            finished["read"] = time.monotonic()

        thread = threading.Thread(target=read_back)
        thread.start()
        assert reading.wait(5)
        _write_one(writer, [INTRO])
        finished["write"] = time.monotonic()
        thread.join(20)
        assert finished["write"] < finished["read"] - 0.3  # the write didn't wait for the whole read-back
        assert _served(db) == [(T.INTRO, INTRO.start_ms, INTRO.end_ms)]

    def test_between_two_items_it_pauses_with_the_lock_free(self, tmp_path, monkeypatch):
        # A released threading.Lock can be taken again by the same thread before a waiting writer wakes (Python before
        # 3.13 hands it to no one in particular); the pause gives the writer the moment to take it.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder)
        pub = _publisher(tmp_path, folder)
        pauses = []
        real_sleep = time.sleep
        monkeypatch.setattr(
            plex_db.time,
            "sleep",
            lambda s: (
                pauses.append((s, plex_db._db_lock(str(db)).locked()))
                if s == plex_db.READ_BACK_PAUSE_S
                else real_sleep(s)
            ),
        )
        pub.shows_many(self._items([7, "x", 8, 9]))
        assert pauses == [(plex_db.READ_BACK_PAUSE_S, False)] * 2  # before items 8 and 9; none before the first read

    def test_a_cancel_stops_before_the_next_item(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        answers = iter([False, False, True])
        out = pub.shows_many(self._items(["1", "2", "3", "4"]), cancel_check=lambda: next(answers))
        assert list(out) == ["1", "2"]

    def test_shows_is_a_read_back_of_one_item(self, tmp_path, monkeypatch):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        many = MagicMock(return_value={"7": Shown.REPLACED})
        monkeypatch.setattr(pub, "shows_many", many)
        assert pub.shows("7", (INTRO,), kept_types={T.CREDITS}, item_files=("/a.mkv",)) is Shown.REPLACED
        assert many.call_args.args == ([("7", [INTRO], frozenset({T.CREDITS}), ("/a.mkv",))],)

    @pytest.mark.parametrize("ui_details", [True, False])
    def test_detection_settings_are_asked_only_for_the_edit_dialog(self, tmp_path, ui_details):
        folder = tmp_path / "Plex Media Server"
        _make_db(folder)
        pub = _publisher(tmp_path, folder)
        pub._ui_details = ui_details
        report = pub.capability()
        assert report.ready
        assert pub._server.get_marker_detection_prefs.called is ui_details
        assert report.details["detection"] == (
            {"intro": "never", "credits": "never"} if ui_details else {"intro": None, "credits": None}
        )
