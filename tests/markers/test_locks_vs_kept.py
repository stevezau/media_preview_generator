"""A marker the user adjusted or locked beats "Keep Plex's" / "Keep Emby's" (spec §5.5 rule 1, §14 2026-09-20).

The matrix: locked type × a type the server's setting keeps × vendor (Plex / Jellyfin / Emby) × the server has its own
markers of that type or doesn't × published now from the Inspector or by a later job. Every group carries its unlocked
cell too, which pins the rule that hasn't changed: a decision the user never touched still loses to a kept type.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from media_preview_generator.markers import inspect, pipeline
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import REPLACED_OWN, ServerStatus
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.base import PublishError, Shown
from media_preview_generator.markers.publishers.plex_db import PlexMarkerPublisher
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import FakeRegistry, ready_publisher, server_config
from tests.markers.test_emby_publisher import EMBY_INTRO, FakeEmby
from tests.markers.test_emby_publisher import _publisher as _emby_publisher
from tests.markers.test_plex_db_publisher import (
    CREDITS_FINAL_ROW_EXTRA,
    CREDITS_ROW_EXTRA,
    INTRO_ROW_EXTRA,
    _insert_taggings,
    _make_db,
    _part_markers,
    _rows,
    _served,
    _write_one,
    _writes,
    encode_extra_data,
)
from tests.markers.test_plex_db_publisher import _publisher as _plex_publisher

T = MarkerType
PLEX_PATH = "/data/tv/S01E01.mkv"
DUR = 1_320_000
INTRO = Marker(T.INTRO, 11_000, 37_000, ("chapters",))
CREDITS_FINAL = Marker(T.CREDITS, 1_299_000, DUR, ("chapters",))
LOCKED_INTRO = replace(INTRO, decided_by=(Source.USER.value,), locked=True)
LOCKED_CREDITS = replace(CREDITS_FINAL, decided_by=(Source.USER.value,), locked=True)
# What Plex's own detection leaves on the item, in served times.
PLEX_OWN_INTRO_ROW = (990, 29_306)
PLEX_OWN_INTRO_SERVED = (T.INTRO, 990, 29_306)


@pytest.fixture
def sql_log(monkeypatch):
    """Every statement the Plex publisher runs, so a no-op write can be shown to take no write lock."""
    log: list[str] = []
    original = PlexMarkerPublisher._connect

    def connect(self, *, read_only, **kwargs):
        conn = original(self, read_only=read_only, **kwargs)
        conn.set_trace_callback(log.append)
        return conn

    monkeypatch.setattr(PlexMarkerPublisher, "_connect", connect)
    return log


@pytest.fixture(autouse=True)
def plex_holds_the_database(monkeypatch):
    """Stand in for Plex having its database open, which every write proves before it opens the file."""
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)


def _plex_item(tmp_path, *, plex_own_intro=True, redetect="keep_plex"):
    folder = tmp_path / "Plex Media Server"
    db = _make_db(folder, journal_mode="delete")
    if plex_own_intro:
        _insert_taggings(db, (7, 563, 0, "intro", *PLEX_OWN_INTRO_ROW, INTRO_ROW_EXTRA))
    return db, _plex_publisher(tmp_path, folder, redetect=redetect)


class TestPlexPublisher:
    """``PlexMarkerPublisher.write`` against a real Plex 1.43 database."""

    @pytest.mark.parametrize(
        ("redetect", "locked", "plex_own_intro", "ours", "kept", "replaced_own", "served_intro"),
        [
            ("keep_plex", True, True, [LOCKED_INTRO], frozenset(), {T.INTRO}, (T.INTRO, 11_000, 37_000)),
            ("keep_plex", False, True, [], {T.INTRO}, frozenset(), PLEX_OWN_INTRO_SERVED),
            ("keep_plex", True, False, [LOCKED_INTRO], frozenset(), frozenset(), (T.INTRO, 11_000, 37_000)),
            ("keep_plex", False, False, [INTRO], frozenset(), frozenset(), (T.INTRO, 11_000, 37_000)),
            ("restore", True, True, [LOCKED_INTRO], frozenset(), frozenset(), (T.INTRO, 11_000, 37_000)),
            ("restore", False, True, [INTRO], frozenset(), frozenset(), (T.INTRO, 11_000, 37_000)),
        ],
        ids=[
            "keep-locked-plex-has-own",
            "keep-unlocked-plex-has-own",
            "keep-locked-plex-has-none",
            "keep-unlocked-plex-has-none",
            "restore-locked-plex-has-own",
            "restore-unlocked-plex-has-own",
        ],
    )
    def test_the_setting_keeps_plexs_rows_only_for_a_type_the_user_didnt_lock(
        self, tmp_path, redetect, locked, plex_own_intro, ours, kept, replaced_own, served_intro
    ):
        db, pub = _plex_item(tmp_path, plex_own_intro=plex_own_intro, redetect=redetect)
        wanted = LOCKED_INTRO if locked else INTRO

        assert _write_one(pub, [wanted], path=PLEX_PATH) == ours
        assert pub.last_kept_types == kept
        assert pub.last_replaced_own_types == replaced_own
        assert _served(db) == [served_intro]

    def test_a_type_kept_on_an_earlier_run_is_taken_back_once_the_user_locks_it(self, tmp_path):
        db, pub = _plex_item(tmp_path)
        assert _write_one(pub, [INTRO], path=PLEX_PATH) == []  # run 1: Plex's intro is kept
        assert pub.last_kept_types == {T.INTRO}

        ours = pub.write(
            "7",
            [LOCKED_INTRO],
            previous=[],
            duration_ms=DUR,
            canonical_path=PLEX_PATH,
            kept_types=frozenset({T.INTRO}),
        )

        assert ours == [LOCKED_INTRO]
        assert (pub.last_kept_types, pub.last_replaced_own_types) == (frozenset(), {T.INTRO})
        assert _served(db) == [(T.INTRO, 11_000, 37_000)]

    def test_plexs_rows_already_serving_the_locked_times_take_no_write_and_report_nothing_replaced(
        self, tmp_path, sql_log
    ):
        # The override must name only what it really took off Plex: rows that already serve the locked times are ours.
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="delete")
        _insert_taggings(db, (7, 563, 0, "intro", 11_000, 37_000, INTRO_ROW_EXTRA))
        _set_intro_key(db)
        pub = _plex_publisher(tmp_path, folder, redetect="keep_plex")
        sql_log.clear()

        assert _write_one(pub, [LOCKED_INTRO], path=PLEX_PATH) == [LOCKED_INTRO]

        assert pub.last_replaced_own_types == frozenset()
        assert (pub.last_kept_types, pub.last_write_changed) == (frozenset(), False)
        assert _writes(sql_log) == []

    def test_only_the_locked_type_is_taken_back_while_the_other_stays_plexs(self, tmp_path):
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, journal_mode="delete")
        _insert_taggings(
            db,
            (7, 563, 0, "intro", *PLEX_OWN_INTRO_ROW, INTRO_ROW_EXTRA),
            (7, 563, 1, "credits", 1_182_721, 1_212_721, CREDITS_ROW_EXTRA),
        )
        pub = _plex_publisher(tmp_path, folder, redetect="keep_plex")

        ours = _write_one(pub, [LOCKED_INTRO, CREDITS_FINAL], path=PLEX_PATH)

        assert ours == [LOCKED_INTRO]
        assert (pub.last_kept_types, pub.last_replaced_own_types) == ({T.CREDITS}, {T.INTRO})
        assert _served(db) == [(T.INTRO, 11_000, 37_000), (T.CREDITS, 1_184_721, 1_210_721)]

    def test_a_locked_credits_gets_its_final_flag_put_right_although_keep_plexs_otherwise_wouldnt(self, tmp_path):
        # `_refresh_final_types` is off under "Keep Plex's" because rows serving our times can still be Plex's own.
        # A locked type's rows are ours whatever the setting, so the stale flag is rewritten like under "Use ours".
        folder = tmp_path / "Plex Media Server"
        stale_key = (
            '{"MediaPartMarkersArray":{"attributeName":"credits","version":4,'
            '"MediaPartMarker":[{"startTimeOffset":1297000,"endTimeOffset":1322000}]}}'
        )
        db = _make_db(folder, parts=((PLEX_PATH, encode_extra_data({"pv:credits": stale_key})),), journal_mode="delete")
        _insert_taggings(db, (7, 563, 0, "credits", 1_297_000, DUR + 2_000, CREDITS_ROW_EXTRA))
        unlocked = _plex_publisher(tmp_path, folder, redetect="keep_plex")
        before = _rows(db, "SELECT time_offset, end_time_offset, extra_data FROM taggings")

        assert _write_one(unlocked, [CREDITS_FINAL], previous=[CREDITS_FINAL], path=PLEX_PATH) == [CREDITS_FINAL]
        assert _rows(db, "SELECT time_offset, end_time_offset, extra_data FROM taggings") == before
        assert unlocked.last_write_changed is False

        locked = _plex_publisher(tmp_path, folder, redetect="keep_plex")
        # The item keeps the times it already shows (they agree); ``decided_by`` is the record's, the lock the user's.
        assert _write_one(locked, [LOCKED_CREDITS], previous=[CREDITS_FINAL], path=PLEX_PATH) == [
            replace(CREDITS_FINAL, locked=True)
        ]
        assert _rows(db, "SELECT time_offset, end_time_offset, extra_data FROM taggings") == [
            (1_297_000, DUR, CREDITS_FINAL_ROW_EXTRA)
        ]
        assert _part_markers(db, 1, "pv:credits") == [
            {"startTimeOffset": 1_297_000, "endTimeOffset": DUR, "final": True}
        ]

    def test_a_lock_survives_the_item_keeping_another_versions_agreeing_times(self, tmp_path):
        # A multi-version item keeps the times it already shows when every version agrees with them. The times are the
        # item's, the lock is the calling file's -- and it decides whether Plex may keep its own rows of the type.
        other = "/data/tv/S01E01 - 2160p.mkv"
        folder = tmp_path / "Plex Media Server"
        db = _make_db(folder, parts=((PLEX_PATH, None), (other, None)), journal_mode="delete")
        nearly = Marker(T.INTRO, 11_900, 37_900, ("chapters",))
        pub = _plex_publisher(
            tmp_path, folder, redetect="keep_plex", sibling_markers=lambda p: {T.INTRO: nearly} if p == other else None
        )
        item_row = _write_one(pub, [INTRO], path=PLEX_PATH)
        assert item_row == [INTRO]
        _plex_redetects_the_intro(db)
        locked_a_little_later = replace(INTRO, start_ms=11_500, end_ms=37_500, locked=True)

        ours = _write_one(pub, [locked_a_little_later], previous=item_row, path=PLEX_PATH)

        # The item's own times win (every version agrees with them), and the calling file's lock rides along.
        assert [(m.start_ms, m.end_ms, m.locked) for m in ours] == [(11_000, 37_000, True)]
        assert (pub.last_kept_types, pub.last_replaced_own_types) == (frozenset(), {T.INTRO})
        assert _served(db) == [(T.INTRO, 11_000, 37_000)]


def _set_intro_key(db):
    payload = (
        '{"MediaPartMarkersArray":{"attributeName":"intros","version":5,'
        '"MediaPartMarker":[{"startTimeOffset":11000,"endTimeOffset":37000}]}}'
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE media_parts SET extra_data=? WHERE id=1", (encode_extra_data({"pv:intros": payload}),))
        conn.commit()
    finally:
        conn.close()


def _plex_redetects_the_intro(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM taggings WHERE text='intro'")
        conn.commit()
    finally:
        conn.close()
    _insert_taggings(db, (7, 563, 1, "intro", *PLEX_OWN_INTRO_ROW, INTRO_ROW_EXTRA))


@pytest.fixture
def emby(tmp_path):
    media = tmp_path / "S01E01.mkv"
    media.write_bytes(b"x" * 321)
    return FakeEmby(str(media))


EMBY_INTRO_M = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
EMBY_CREDITS = Marker(T.CREDITS, 1_295_324, 1_321_472, ("chapters",))
EMBY_LOCKED_INTRO = replace(EMBY_INTRO_M, decided_by=(Source.USER.value,), locked=True)
EMBY_LOCKED_CREDITS = replace(EMBY_CREDITS, decided_by=(Source.USER.value,), locked=True)


def _emby_write(emby, markers, publisher, *, previous=(), kept_types=()):
    return publisher.write(
        "42",
        list(markers),
        previous=None if previous is None else list(previous),
        duration_ms=1_321_472,
        canonical_path=emby.path,
        kept_types=frozenset(kept_types),
    )


class TestEmbyPublisher:
    """``EmbyMarkerPublisher.write`` against a model of the Bridge plugin's ``ReplaceOwn`` behaviour."""

    @pytest.mark.parametrize(
        ("setting", "locked", "emby_own_intro", "ours", "kept", "replaced_own", "replace_own_flags"),
        [
            ("keep_emby", True, True, [EMBY_LOCKED_INTRO], frozenset(), {T.INTRO}, [True]),
            ("keep_emby", False, True, [], {T.INTRO}, frozenset(), [False]),
            ("keep_emby", True, False, [EMBY_LOCKED_INTRO], frozenset(), frozenset(), [True]),
            ("keep_emby", False, False, [EMBY_INTRO_M], frozenset(), frozenset(), [False]),
            ("restore", True, True, [EMBY_LOCKED_INTRO], frozenset(), frozenset(), [True]),
            ("restore", False, True, [EMBY_INTRO_M], frozenset(), frozenset(), [True]),
        ],
        ids=[
            "keep-locked-emby-has-own",
            "keep-unlocked-emby-has-own",
            "keep-locked-emby-has-none",
            "keep-unlocked-emby-has-none",
            "restore-locked-emby-has-own",
            "restore-unlocked-emby-has-own",
        ],
    )
    def test_the_setting_keeps_embys_rows_only_for_a_type_the_user_didnt_lock(
        self, emby, setting, locked, emby_own_intro, ours, kept, replaced_own, replace_own_flags
    ):
        if emby_own_intro:
            emby.rows += EMBY_INTRO
        publisher = _emby_publisher(emby, setting)

        written = _emby_write(emby, [EMBY_LOCKED_INTRO if locked else EMBY_INTRO_M], publisher)

        assert written == ours
        assert publisher.last_kept_types == kept
        assert publisher.last_replaced_own_types == replaced_own
        # ReplaceOwn is per POST: every wanted type locked means the one POST carries it.
        assert emby.replace_own == replace_own_flags
        shown = ("IntroStart", 60_000) if ours == [] else ("IntroStart", EMBY_INTRO_M.start_ms)
        assert shown in emby.markers_shown()

    def test_a_locked_type_beside_a_kept_one_is_replaced_by_its_own_post(self, emby):
        # ReplaceOwn is per POST, not per type: the locked intro goes alone with it, then the whole set without it, so
        # Emby keeps its own credits.
        emby.rows += [*EMBY_INTRO, ("CreditsStart", 11_000_000_000, "Credits")]
        publisher = _emby_publisher(emby, "keep_emby")

        ours = _emby_write(emby, [EMBY_LOCKED_INTRO, EMBY_CREDITS], publisher)

        assert ours == [EMBY_LOCKED_INTRO]
        assert (publisher.last_kept_types, publisher.last_replaced_own_types) == ({T.CREDITS}, {T.INTRO})
        assert emby.replace_own == [False, True, False]
        replacing = emby.server.put_emby_markers.call_args_list[1].kwargs
        assert replacing["replace_own"] is True
        assert (replacing["intro_start_ticks"], replacing["credits_start_ticks"]) == (1_267_710_000, None)
        assert emby.markers_shown() == [
            ("IntroStart", EMBY_INTRO_M.start_ms),
            ("IntroEnd", EMBY_INTRO_M.end_ms),
            ("CreditsStart", 1_100_000),
        ]

    def test_the_replace_own_post_leaves_the_item_complete_if_the_post_after_it_never_lands(self, emby):
        # The ReplaceOwn POST replaces the plugin's whole stored set, so it must carry every type Emby doesn't own:
        # carrying only the locked intro would take the credits row off the item, and a failure before the POST that
        # puts it back would leave the item short a marker it already showed (the row then waits on the retry backoff).
        publisher = _emby_publisher(emby, "keep_emby")
        emby.rows += EMBY_INTRO  # Emby owns the intro; nothing owns the credits

        posts = []
        real_put = emby.server.put_emby_markers.side_effect

        def put(item_id, **kwargs):
            posts.append(kwargs)
            if len(posts) == 3:  # the POST that only re-stores the whole set
                raise requests.ConnectionError("Emby went away")
            return real_put(item_id, **kwargs)

        emby.server.put_emby_markers.side_effect = put

        with pytest.raises(PublishError):
            _emby_write(emby, [EMBY_LOCKED_INTRO, EMBY_CREDITS], publisher)

        assert emby.markers_shown() == [
            ("IntroStart", EMBY_INTRO_M.start_ms),
            ("IntroEnd", EMBY_INTRO_M.end_ms),
            ("CreditsStart", EMBY_CREDITS.start_ms),
        ]

    def test_a_type_kept_on_an_earlier_run_is_taken_back_once_the_user_locks_it(self, emby):
        emby.rows += EMBY_INTRO
        publisher = _emby_publisher(emby, "keep_emby")
        assert _emby_write(emby, [EMBY_INTRO_M, EMBY_CREDITS], publisher) == [EMBY_CREDITS]
        assert publisher.last_kept_types == {T.INTRO}

        ours = _emby_write(
            emby,
            [EMBY_LOCKED_INTRO, EMBY_CREDITS],
            publisher,
            previous=[EMBY_CREDITS],
            kept_types={T.INTRO},
        )

        assert ours == [EMBY_LOCKED_INTRO, EMBY_CREDITS]
        assert (publisher.last_kept_types, publisher.last_replaced_own_types) == (frozenset(), {T.INTRO})
        assert ("IntroStart", EMBY_INTRO_M.start_ms) in emby.markers_shown()

    def test_a_lock_on_a_kept_type_is_never_short_circuited_as_nothing_to_send(self, emby):
        # Without the lock this very call sends nothing (the plugin already stores exactly this set).
        emby.rows += EMBY_INTRO
        publisher = _emby_publisher(emby, "keep_emby")
        _emby_write(emby, [EMBY_INTRO_M, EMBY_CREDITS], publisher)
        emby.server.put_emby_markers.reset_mock()
        emby.replace_own.clear()

        unlocked = _emby_write(
            emby, [EMBY_INTRO_M, EMBY_CREDITS], publisher, previous=[EMBY_CREDITS], kept_types={T.INTRO}
        )
        assert unlocked == [EMBY_CREDITS]
        emby.server.put_emby_markers.assert_not_called()

        locked = _emby_write(
            emby, [EMBY_LOCKED_INTRO, EMBY_CREDITS], publisher, previous=[EMBY_CREDITS], kept_types={T.INTRO}
        )
        assert locked == [EMBY_LOCKED_INTRO, EMBY_CREDITS]
        assert publisher.last_replaced_own_types == {T.INTRO}
        assert emby.replace_own == [False, True, False]

    def test_every_locked_type_means_one_post_with_replace_own(self, emby):
        emby.rows += [*EMBY_INTRO, ("CreditsStart", 11_000_000_000, "Credits")]
        publisher = _emby_publisher(emby, "keep_emby")

        ours = _emby_write(emby, [EMBY_LOCKED_INTRO, EMBY_LOCKED_CREDITS], publisher)

        assert ours == [EMBY_LOCKED_INTRO, EMBY_LOCKED_CREDITS]
        assert publisher.last_replaced_own_types == {T.INTRO, T.CREDITS}
        assert emby.replace_own == [True]
        assert emby.markers_shown() == [
            ("IntroStart", EMBY_INTRO_M.start_ms),
            ("IntroEnd", EMBY_INTRO_M.end_ms),
            ("CreditsStart", EMBY_CREDITS.start_ms),
        ]

    def test_rows_this_app_left_before_are_not_counted_as_embys_own(self, emby):
        # The plugin lost its store, so our own earlier rows look like somebody else's. They are replaced (that rule
        # predates the lock), but nothing of Emby's was taken off it, so the row must not say so.
        publisher = _emby_publisher(emby, "keep_emby")
        old = Marker(T.INTRO, 5_000, 35_000, ("chapters",))
        _emby_write(emby, [old], publisher)
        emby.stored, emby.size = None, None
        emby.replace_own.clear()

        ours = _emby_write(emby, [EMBY_LOCKED_INTRO], publisher, previous=[old])

        assert ours == [EMBY_LOCKED_INTRO] and publisher.last_replaced_own_types == frozenset()
        assert emby.replace_own == [True]  # every wanted type is locked, so the one POST carries ReplaceOwn
        assert emby.markers_shown() == [
            ("IntroStart", EMBY_INTRO_M.start_ms),
            ("IntroEnd", EMBY_INTRO_M.end_ms),
        ]


class TestJellyfin:
    def test_jellyfin_has_no_keep_setting_so_a_lock_changes_nothing_there(self, tmp_path):
        """Jellyfin's core `/MediaSegments` serves every provider's segments side by side, so there is nothing to keep
        and nothing for a lock to override: the publisher has no `on_*_redetect` setting at all."""
        from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
        from media_preview_generator.markers.settings import load_server

        cfg = server_config("jf-1", ServerType.JELLYFIN, root="/")
        publisher = JellyfinMarkerPublisher(MagicMock(), cfg, load_server(cfg.markers, "jellyfin"))
        settings = load_server(cfg.markers, "jellyfin")

        assert settings.keeps_server_markers is False
        assert not hasattr(settings, "on_jellyfin_redetect")
        assert publisher.last_kept_types == frozenset()
        assert publisher.last_replaced_own_types == frozenset()


MEDIA_DUR = 1_320_000
JOB_INTRO = Marker(T.INTRO, 126_771, 157_068, ("chapters",))
JOB_CREDITS = Marker(T.CREDITS, 1_295_324, MEDIA_DUR, ("chapters",))


@pytest.fixture
def media(tmp_path):
    folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    f = folder / "Show - S01E01.mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"), clock=lambda: datetime(2026, 9, 14, tzinfo=UTC))
    yield s
    s.close()


def _registry(media, stype, *, setting):
    root = media[: media.index("/media/") + len("/media")]
    sid = f"{stype.value}-1"
    cfg = server_config(sid, stype, root=root)
    if stype is ServerType.PLEX:
        cfg.markers["plex"]["on_plex_redetect"] = setting
    else:
        cfg.markers.setdefault("emby", {})["on_emby_redetect"] = setting
    return FakeRegistry({sid: cfg})


def _known(store, media, markers, *, locked=()):
    st = os.stat(media)
    rec = store.upsert_file(
        FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=MEDIA_DUR, season_key=None, is_movie=False
    )
    store.save_decisions(
        rec.id,
        {
            m.type: TypeDecision(m.type, DecisionStatus.DECIDED, m, None, "chapters")
            for m in markers
            if m.type not in {x.type for x in locked}
        }
        | {m.type: TypeDecision(m.type, DecisionStatus.DECIDED, m, None, "locked by user") for m in locked},
        settings_fingerprint="f",
    )
    if locked:
        store.save_user_markers(rec.id, list(locked), settings_fingerprint="f")
    return rec


def _settings():
    return load_global(validate_global({"sources": [{"id": "chapters", "enabled": True}]}, None)[0])


def _publish_now(monkeypatch, store, media, registry, publishers):
    monkeypatch.setattr(pipeline, "get_marker_store", lambda: store)
    monkeypatch.setattr(pipeline, "get_global_settings", _settings)
    with patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: publishers.get(cfg.id)):
        return pipeline.publish_now(media, registry=registry, live_config=registry.get_config)


def _keeping(pub, kept, shown, *, changed, replaced_own=frozenset()):
    """A publisher that keeps ``kept`` as the server's own and shows ``shown`` as ours."""

    def write(item_id, markers, **kwargs):
        pub.last_write_changed = changed
        pub.last_kept_types = frozenset(kept)
        pub.last_replaced_own_types = frozenset(replaced_own)
        return [m for m in markers if m.type in shown]

    pub.write.side_effect = write


class TestTheDecisionPath:
    """``_publish_to``: a type the item record holds as the server's own is published again once the user locks it."""

    @pytest.mark.parametrize(
        ("stype", "publisher_name", "setting", "vendor"),
        [
            (ServerType.PLEX, "plex_db", "keep_plex", "Plex"),
            (ServerType.EMBY, "emby_bridge", "keep_emby", "Emby"),
        ],
    )
    def test_locking_a_kept_type_republishes_it_although_the_times_did_not_change(
        self, store, media, monkeypatch, stype, publisher_name, setting, vendor
    ):
        # A lock alone changes no time, so the publish basis still matches and the item still reads as ours. Without
        # the lock rule the request would answer "Up to date" and leave the server showing its own markers.
        sid = f"{stype.value}-1"
        reg = _registry(media, stype, setting=setting)
        rec = _known(store, media, [JOB_INTRO, JOB_CREDITS])
        pub = ready_publisher(publisher_name, types=("intro", "credits"))
        _keeping(pub, {T.INTRO}, {T.CREDITS}, changed=True)
        pub.shows.return_value = Shown.OURS
        _publish_now(monkeypatch, store, media, reg, {sid: pub})
        assert store.get_item_publish_state(sid, pub.write.call_args.args[0]).kept_types == {T.INTRO}
        writes_before = pub.write.call_count

        _known(store, media, [JOB_CREDITS], locked=[replace(JOB_INTRO, locked=True)])
        _keeping(pub, frozenset(), {T.INTRO, T.CREDITS}, changed=True, replaced_own={T.INTRO})
        rows = _publish_now(monkeypatch, store, media, reg, {sid: pub})

        assert pub.write.call_count == writes_before + 1
        assert pub.write.call_args.kwargs["kept_types"] == {T.INTRO}
        assert [m.locked for m in pub.write.call_args.args[1] if m.type is T.INTRO] == [True]
        assert rows[0]["status"] == ServerStatus.WRITTEN.value
        assert rows[0][REPLACED_OWN] == ["intro"]
        assert rows[0]["message"] == (
            f"2 marker(s). Replaced {vendor}'s own marker. This server is set to keep {vendor}'s, "
            "but a marker you adjust always wins."
        )
        assert store.get_item_publish_state(sid, pub.write.call_args.args[0]).kept_types == frozenset()
        assert store.get_markers(rec.id)[T.INTRO].locked is True

    def test_a_row_still_waiting_on_the_items_other_versions_says_it_too(self, store, media, monkeypatch):
        # A Plex item shows a type only once every version agrees on it, so the same write can both take a locked type
        # off Plex and hold another back. The override sentence and the row key are composed again on that lane.
        reg = _registry(media, ServerType.PLEX, setting="keep_plex")
        _known(store, media, [JOB_CREDITS], locked=[replace(JOB_INTRO, locked=True)])
        pub = ready_publisher("plex_db")
        _keeping(pub, frozenset(), {T.INTRO}, changed=True, replaced_own={T.INTRO})

        rows = _publish_now(monkeypatch, store, media, reg, {"plex-1": pub})

        assert rows[0]["status"] == ServerStatus.WAITING.value
        assert rows[0][REPLACED_OWN] == ["intro"]
        assert rows[0]["message"] == (
            "Waiting for this item's other versions to agree on: credits. Replaced Plex's own marker. "
            "This server is set to keep Plex's, but a marker you adjust always wins."
        )

    def test_an_unlocked_decision_on_a_kept_type_still_sends_nothing(self, store, media, monkeypatch):
        sid = "plex-1"
        reg = _registry(media, ServerType.PLEX, setting="keep_plex")
        _known(store, media, [JOB_INTRO, JOB_CREDITS])
        pub = ready_publisher("plex_db")
        _keeping(pub, {T.INTRO}, {T.CREDITS}, changed=True)
        pub.shows.return_value = Shown.OURS
        _publish_now(monkeypatch, store, media, reg, {sid: pub})
        writes_before = pub.write.call_count

        rows = _publish_now(monkeypatch, store, media, reg, {sid: pub})

        assert pub.write.call_count == writes_before  # no second write: the record already says it is Plex's
        assert (rows[0]["status"], rows[0]["message"]) == (ServerStatus.UP_TO_DATE.value, "Keeping Plex's intro")
        assert REPLACED_OWN not in rows[0]

    def test_a_later_job_publishes_the_lock_over_a_kept_type_the_same_way(self, store, media, monkeypatch):
        # Not only the editor's bounded request: a normal Intro & Credits run takes the same path.
        from media_preview_generator.markers.pipeline import PipelineContext, check_item
        from media_preview_generator.markers.probe import MediaProbe
        from media_preview_generator.processing.types import ProcessableItem

        reg = _registry(media, ServerType.PLEX, setting="keep_plex")
        _known(store, media, [JOB_CREDITS], locked=[replace(JOB_INTRO, locked=True)])
        pub = ready_publisher("plex_db")
        _keeping(pub, frozenset(), {T.INTRO, T.CREDITS}, changed=True, replaced_own={T.INTRO})
        ctx = PipelineContext(
            registry=reg,
            config=MagicMock(),
            settings=_settings(),
            store=store,
            priority=lambda: 2,
            ffprobe="ffprobe",
            clients={},
            live_config=reg.get_config,
        )
        item = ProcessableItem(canonical_path=media, server_id="plex-1", item_id_by_server={})

        with (
            patch.object(pipeline, "probe_media", return_value=MediaProbe(MEDIA_DUR, ())),
            patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: pub),
        ):
            out = check_item(item, ctx=ctx)

        row = out.publisher_rows[0]
        assert [m.locked for m in pub.write.call_args.args[1] if m.type is T.INTRO] == [True]
        assert row[REPLACED_OWN] == ["intro"]
        assert row["message"].endswith("but a marker you adjust always wins.")


class TestTheInspectorSaysItBeforeTheSave:
    PATH = "/media/tv/Show/Season 01/Show - S01E01.mkv"

    def _file(self, store, *, locked):
        rec = store.upsert_file(
            FileIdentity(self.PATH, 100, 200), duration_ms=DUR, season_key="/media/tv/Show", is_movie=False
        )
        store.replace_evidence(
            rec.id, Source.CHAPTERS, [Candidate(T.INTRO, 11_000, 37_000, Source.CHAPTERS, origin="Opening")]
        )
        store.save_decisions(
            rec.id,
            {
                T.INTRO: TypeDecision(T.INTRO, DecisionStatus.DECIDED, INTRO, None, "chapters"),
                T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NO_EVIDENCE, None, None, "nothing found"),
                T.RECAP: TypeDecision(T.RECAP, DecisionStatus.NO_EVIDENCE, None, None, "nothing found"),
                T.PREVIEW: TypeDecision(T.PREVIEW, DecisionStatus.DISABLED, None, None, "not detected"),
            },
            settings_fingerprint="f",
        )
        if locked:
            store.save_user_markers(rec.id, [INTRO], settings_fingerprint="f")
        return rec

    @pytest.mark.parametrize(
        ("locked", "plan", "reason"),
        [
            (
                True,
                "will_replace",
                "This server is set to keep Plex's own markers. Your locked marker replaces them anyway.",
            ),
            (False, "keeps_plex", "Keeping Plex's intro"),
        ],
        ids=["locked", "unlocked"],
    )
    def test_the_plan_lane_says_what_a_save_will_do_to_plexs_own_markers(
        self, store, monkeypatch, locked, plan, reason
    ):
        monkeypatch.setattr(inspect, "publisher_for", lambda *a, **kw: None)
        self._file(store, locked=locked)
        markers = {
            "enabled": True,
            "library_ids": None,
            "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00", "on_plex_redetect": "keep_plex"},
        }
        cfg = server_config("plex", ServerType.PLEX, root="/media/tv", markers=markers)
        registry = FakeRegistry({"plex": cfg})
        registry.get("plex").get_markers.return_value = [
            {"type": "intro", "start_ms": 990, "end_ms": 29_306, "final": False}
        ]

        payload = inspect.item_payload(self.PATH, registry=registry, store=store)

        row = next(r for r in payload["servers"] if r["server_id"] == "plex")
        assert (row["plan"], row["plan_reason"]) == (plan, reason)

    @pytest.mark.parametrize(
        ("server_type", "vendor"), [(ServerType.PLEX, "Plex"), (ServerType.EMBY, "Emby")], ids=["plex", "emby"]
    )
    @pytest.mark.parametrize("locked", [True, False], ids=["locked", "unlocked"])
    @pytest.mark.parametrize("waiting_on_versions", [False, True], ids=["now", "waiting-on-versions"])
    def test_every_vendor_and_lane_says_it(self, server_type, vendor, locked, waiting_on_versions):
        """The sentence is composed in two places (the plan lane and the versions-waiting lane) and reads the vendor,
        so every cell of vendor × locked × lane gets a row. Emby also folds its credits note into the same string."""
        wanted = [replace(INTRO, locked=locked)]
        embys_own = [{"type": "intro", "start_ms": 990, "end_ms": 29_306}]

        plan, reason = inspect._plan(
            server_type=server_type,
            off_reason="",
            wanted=wanted,
            ours=(),
            current=embys_own,
            waiting_on_versions=waiting_on_versions,
            duration_ms=DUR,
            keep_own=True,
        )

        override = f"This server is set to keep {vendor}'s own markers. Your locked marker replaces them anyway."
        if waiting_on_versions:
            # The versions-waiting lane keeps its own reason and the override follows it as its own sentence.
            assert plan == "waiting"
            assert reason == (
                f"versions don't agree yet. {override}"
                if locked
                else f"versions don't agree yet; keeping {vendor}'s intro"
            )
        elif locked:
            assert (plan, reason) == ("will_replace", override)
        else:
            assert (plan, reason) == (f"keeps_{server_type.value}", f"Keeping {vendor}'s intro")


class TestPublishNowBoundsPlexsDatabaseWait:
    def test_the_plex_publisher_is_built_with_the_requests_own_database_deadline(self, store, media, monkeypatch):
        # plex_db.BUSY_TIMEOUT_S is a job's 30 s wait on the database locks -- longer than the whole bounded request.
        reg = _registry(media, ServerType.PLEX, setting="restore")
        _known(store, media, [JOB_INTRO])
        built = {}

        def factory(server, cfg, **kwargs):
            built.update(kwargs)
            return ready_publisher("plex_db")

        monkeypatch.setattr(pipeline, "get_marker_store", lambda: store)
        monkeypatch.setattr(pipeline, "get_global_settings", _settings)
        with patch.object(pipeline, "publisher_for", side_effect=factory):
            pipeline.publish_now(media, registry=reg, live_config=reg.get_config)

        assert built["db_timeout_s"] == pipeline.PUBLISH_NOW_DB_WAIT_S
        assert pipeline.PUBLISH_NOW_DB_WAIT_S < plex_db.BUSY_TIMEOUT_S

    def test_a_job_leaves_the_database_wait_where_it_was(self, tmp_path):
        from media_preview_generator.markers.publishers.factory import publisher_for

        folder = tmp_path / "Plex Media Server"
        _make_db(folder, journal_mode="delete")
        cfg = server_config("plex-1", ServerType.PLEX, root="/")
        cfg.output = {"plex_config_folder": str(folder)}

        pub = publisher_for(MagicMock(), cfg, ui_details=False)
        shortened = publisher_for(MagicMock(), cfg, ui_details=False, db_timeout_s=3.0)

        assert pub._db_deadline() - shortened._db_deadline() == pytest.approx(plex_db.BUSY_TIMEOUT_S - 3.0, abs=0.05)
