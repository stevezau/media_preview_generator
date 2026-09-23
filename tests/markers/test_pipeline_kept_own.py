"""A type every server keeps its own marker of is never read from the file (spec §6.2 step 3, §14 2026-09-23).

"Keep Plex's" / "Keep Emby's" leave the server's own markers of a type in place, so an answer of ours for that type is
never shown. When every server the file's markers go to keeps its own and shows one of the type now, no local
detector (credit text, season audio) reads the file for it; anything else reads it exactly as before.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome, ServerStatus
from media_preview_generator.markers.pipeline import LocalDetectorSpec
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import ready_publisher, server_config
from tests.markers.test_pipeline import DUR, _clients, _ctx, _media_root, _registry, _run

# test_pipeline's fixtures, shared by name (an import of them reads as unused to the linter).
media = test_pipeline.media
store = test_pipeline.store

T = MarkerType
PLEX_CREDITS = {"type": "credits", "start_ms": 1_290_000, "end_ms": DUR, "final": True}
PLEX_INTRO = {"type": "intro", "start_ms": 126_500, "end_ms": 157_500, "final": False}
EMBY_CREDITS = {"marker_type": "CreditsStart", "start_ms": 1_290_000}
JELLYFIN_OUTRO = {"Type": "Outro", "StartTicks": 1_290_000 * 10_000, "EndTicks": DUR * 10_000}
AUDIO_INTRO = Candidate(T.INTRO, 126_000, 158_000, Source.SEASON_AUDIO, 1.0, "2/2")
SKIPDB_INTRO = Candidate(T.INTRO, 126_500, 157_000, Source.SKIPDB)
TEXT_CREDITS = Candidate(T.CREDITS, 1_291_000, None, Source.CREDITS_TEXT)
OUR_CREDITS = Marker(T.CREDITS, 1_291_000, DUR, ("credits_text", "server_markers"))
KEPT_PLEX = "kept Plex's own marker"


@pytest.fixture
def movie(tmp_path):
    folder = tmp_path / "media" / "movies" / "Heat (1995) {tmdb-949}"
    folder.mkdir(parents=True)
    f = folder / "Heat (1995).mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


def _settings(*, skipdb=False):
    return {
        "detect": {"intro": True, "credits": True, "recap": False},
        "publish_when": "high",
        "sources": [
            {"id": "chapters", "enabled": True},
            {"id": "theintrodb", "enabled": False},
            {"id": "introdb", "enabled": False},
            {"id": "skipdb", "enabled": skipdb},
            {"id": "season_audio", "enabled": True},
            {"id": "credits_text", "enabled": True},
            {"id": "server_markers", "enabled": True},
        ],
    }


class _Detectors:
    """Season audio (intros) and credit text (credits), each a mock that reads the file when called."""

    def __init__(self):
        self.intro = MagicMock(return_value=[AUDIO_INTRO])
        self.credits = MagicMock(return_value=[TEXT_CREDITS])
        self.specs = (
            LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), self.intro),
            LocalDetectorSpec(Source.CREDITS_TEXT, frozenset({T.CREDITS}), self.credits),
        )


def _plex(path, setting="keep_plex", rows=(PLEX_CREDITS,)):
    reg = _registry(path, ServerType.PLEX)
    reg.configs_by_id["plex-1"].markers["plex"]["on_plex_redetect"] = setting
    reg.get("plex-1").get_markers.return_value = None if rows is None else list(rows)
    return reg


def _emby_config(path, setting):
    markers = {"enabled": True, "library_ids": None, "emby": {"on_emby_redetect": setting}}
    return server_config("emby-1", ServerType.EMBY, root=_media_root(path), markers=markers)


def _job(store, reg, path, detectors, publishers, *, stage="process", force=False, skipdb=False):
    clients = _clients(skipdb=LookupResult("ok", (SKIPDB_INTRO,))) if skipdb else _clients()
    ctx = _ctx(
        store, reg, settings_raw=_settings(skipdb=skipdb), clients=clients, detectors=detectors.specs, force=force
    )
    out, _ = _run(ctx, path, publishers, stage=stage)
    return out


def _decision(store, path, mtype):
    return store.get_decisions(store.get_file(path).id)[mtype]


class TestNotRead:
    def test_an_episode_is_read_for_its_intro_but_not_for_the_credits_plex_keeps(self, store, media):
        reg = _plex(media)
        detectors = _Detectors()
        plex = ready_publisher()

        out = _job(store, reg, media, detectors, {"plex-1": plex}, skipdb=True)

        detectors.credits.assert_not_called()
        assert detectors.intro.call_count == 1
        credits = _decision(store, media, T.CREDITS)
        assert (credits.status, credits.reason) == (DecisionStatus.DISABLED, KEPT_PLEX)
        assert _decision(store, media, T.INTRO).status is DecisionStatus.DECIDED
        call = plex.write.call_args
        assert call.args[0] == "item-plex-1" and [m.type for m in call.args[1]] == [T.INTRO]
        assert (call.kwargs["previous"], call.kwargs["kept_types"]) == ([], frozenset())
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert out.publisher_rows[0]["message"] == "1 marker(s); keeping Plex's credits"
        assert "credits: kept Plex's own marker" in out.message
        # One read of Plex: the check and the evidence read share it, and Plex's credits are stored as before.
        rec = store.get_file(media)
        assert reg.get("plex-1").get_markers.call_count == 1
        assert Candidate(T.CREDITS, 1_290_000, None, Source.SERVER_MARKERS, origin="plex-1") in store.get_evidence(
            rec.id
        )

    def test_a_movie_isnt_read_takes_no_worker_and_is_up_to_date(self, store, movie):
        reg = _plex(movie)
        detectors = _Detectors()
        plex = ready_publisher()

        out = _job(store, reg, movie, detectors, {"plex-1": plex}, stage="check")

        assert out is not None  # nothing left to read, so the checking thread finishes it
        detectors.credits.assert_not_called()
        plex.write.assert_not_called()  # nothing decided, nothing of ours there: nothing to send, as before
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        row = out.publisher_rows[0]
        assert (row["status"], row["message"]) == (ServerStatus.UP_TO_DATE.value, "Keeping Plex's credits")
        assert out.message == "credits: kept Plex's own marker"
        assert store.get_publish_state(store.get_file(movie).id, "plex-1") is None

    def test_every_later_run_asks_plex_again_and_still_doesnt_read(self, store, movie):
        reg = _plex(movie)
        detectors = _Detectors()
        for _ in range(3):
            out = _job(store, reg, movie, detectors, {"plex-1": ready_publisher()})
            assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        detectors.credits.assert_not_called()
        # The stored answer isn't read again (it has markers); only the check asks what Plex shows now.
        assert reg.get("plex-1").get_markers.call_count == 3

    @pytest.mark.parametrize(
        ("rows", "intro_read", "credits_read"),
        [((PLEX_CREDITS,), 1, 0), ((PLEX_INTRO,), 0, 1), ((PLEX_INTRO, PLEX_CREDITS), 0, 0), ((), 1, 1)],
        ids=["own-credits", "own-intro", "own-both", "none"],
    )
    def test_each_type_is_decided_on_its_own(self, store, media, rows, intro_read, credits_read):
        detectors = _Detectors()
        _job(store, _plex(media, rows=rows), media, detectors, {"plex-1": ready_publisher()})
        assert (detectors.intro.call_count, detectors.credits.call_count) == (intro_read, credits_read)

    def test_emby_set_to_keep_embys_is_the_same(self, store, movie):
        reg = _registry(movie, ServerType.EMBY)
        reg.configs_by_id["emby-1"] = _emby_config(movie, "keep_emby")
        reg.get("emby-1").get_chapter_markers.return_value = [EMBY_CREDITS]
        detectors = _Detectors()
        emby = ready_publisher("emby_bridge")

        out = _job(store, reg, movie, detectors, {"emby-1": emby})

        detectors.credits.assert_not_called()
        emby.write.assert_not_called()
        assert (out.outcome_key, out.publisher_rows[0]["message"]) == (
            FileOutcome.UP_TO_DATE.value,
            "Keeping Emby's credits",
        )
        assert _decision(store, movie, T.CREDITS).reason == "kept Emby's own marker"

    def test_plex_and_emby_both_keeping_their_own_arent_read_and_both_rows_say_so(self, store, movie):
        reg = _plex(movie)
        reg.configs_by_id["emby-1"] = _emby_config(movie, "keep_emby")
        reg.get("emby-1").get_chapter_markers.return_value = [EMBY_CREDITS]
        detectors = _Detectors()

        out = _job(
            store, reg, movie, detectors, {"plex-1": ready_publisher(), "emby-1": ready_publisher("emby_bridge")}
        )

        detectors.credits.assert_not_called()
        assert [r["message"] for r in out.publisher_rows] == ["Keeping Plex's credits", "Keeping Emby's credits"]
        assert _decision(store, movie, T.CREDITS).reason == "kept Plex's and Emby's own markers"


class TestReadAsBefore:
    @pytest.mark.parametrize(
        ("setting", "rows", "durations"),
        [
            ("restore", (PLEX_CREDITS,), []),
            ("keep_plex", (), []),
            # Rule 7: an item with a version more than 2 s apart gives no evidence, so nothing says Plex has its own.
            ("keep_plex", (PLEX_CREDITS,), [DUR, DUR + 60_000]),
            ("keep_plex", None, []),
        ],
        ids=["use-ours", "no-own-marker", "another-cut", "plex-unreadable"],
    )
    def test_plex_alone(self, store, movie, setting, rows, durations):
        reg = _plex(movie, setting, rows)
        reg.get("plex-1").get_part_durations.return_value = durations
        detectors = _Detectors()
        _job(store, reg, movie, detectors, {"plex-1": ready_publisher()})
        assert detectors.credits.call_count == 1
        assert _decision(store, movie, T.CREDITS).status is not DecisionStatus.DISABLED

    @pytest.mark.parametrize("record", ["item", "file", "failed-write"])
    def test_plexs_credits_that_are_or_may_be_ours(self, store, movie, record):
        reg = _plex(movie)
        st = os.stat(movie)
        rec = store.upsert_file(
            FileIdentity(movie, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=True
        )
        if record == "item":
            store.set_item_publish_state("plex-1", "item-plex-1", [OUR_CREDITS], "written")
        elif record == "file":  # this file published them on the item it belonged to before a merge
            store.set_publish_state(rec.id, "plex-1", item_id="item-old", markers=[OUR_CREDITS], status="written")
        else:  # what is ours on the item isn't known after a failed write
            store.set_item_publish_state("plex-1", "item-plex-1", [], "failed")
        detectors = _Detectors()
        _job(store, reg, movie, detectors, {"plex-1": ready_publisher()})
        assert detectors.credits.call_count == 1

    @pytest.mark.parametrize(
        ("second", "setting", "own"),
        [
            (ServerType.JELLYFIN, None, False),
            (ServerType.JELLYFIN, None, True),  # Jellyfin has no "keep its own" setting
            (ServerType.EMBY, "restore", True),
            (ServerType.EMBY, "keep_emby", False),
        ],
        ids=["jellyfin-without-markers", "jellyfin-with-its-own", "emby-use-ours", "emby-keeps-but-has-none"],
    )
    def test_a_second_server_that_would_show_ours(self, store, movie, second, setting, own):
        reg = _plex(movie)
        if second is ServerType.EMBY:
            reg.configs_by_id["emby-1"] = _emby_config(movie, setting)
            reg.get("emby-1").get_chapter_markers.return_value = [EMBY_CREDITS] if own else []
            pubs = {"plex-1": ready_publisher(), "emby-1": ready_publisher("emby_bridge")}
        else:
            reg.configs_by_id["jellyfin-1"] = server_config("jellyfin-1", second, root=_media_root(movie))
            reg.get("jellyfin-1").get_media_segments.return_value = [JELLYFIN_OUTRO] if own else []
            pubs = {"plex-1": ready_publisher(), "jellyfin-1": ready_publisher("jellyfin_bridge")}
        detectors = _Detectors()
        _job(store, reg, movie, detectors, pubs)
        assert detectors.credits.call_count == 1

    def test_emby_markers_an_importer_plugin_wrote_arent_embys_own(self, store, movie):
        reg = _registry(movie, ServerType.EMBY)
        reg.configs_by_id["emby-1"] = _emby_config(movie, "keep_emby")
        reg.get("emby-1").get_chapter_markers.return_value = [EMBY_CREDITS]
        reg.get("emby-1").get_plugin_names.return_value = ["IntroDB Importer"]
        detectors = _Detectors()
        _job(store, reg, movie, detectors, {"emby-1": ready_publisher("emby_bridge")})
        assert detectors.credits.call_count == 1

    @pytest.mark.parametrize("setting", ["keep_plex", "restore"])
    def test_a_locked_type_is_read_on_a_forced_run_whatever_the_setting(self, store, movie, setting):
        reg = _plex(movie, setting)
        st = os.stat(movie)
        rec = store.upsert_file(
            FileIdentity(movie, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=True
        )
        locked = Marker(T.CREDITS, 1_250_000, DUR, ("user",), locked=True)
        store.lock_marker(rec.id, locked)
        detectors = _Detectors()
        plex = ready_publisher()

        _job(store, reg, movie, detectors, {"plex-1": plex}, force=True)

        assert detectors.credits.call_count == 1
        assert plex.write.call_args.args == ("item-plex-1", [locked])
        assert _decision(store, movie, T.CREDITS).status is DecisionStatus.DECIDED


class TestLaterRuns:
    @pytest.mark.parametrize("change", ["switched-to-use-ours", "plex-lost-its-marker", "new-destination"])
    def test_the_next_run_reads_the_file_once_the_answer_could_be_shown(self, store, movie, change):
        reg = _plex(movie)
        detectors = _Detectors()
        pubs = {"plex-1": ready_publisher(), "jellyfin-1": ready_publisher("jellyfin_bridge")}
        _job(store, reg, movie, detectors, pubs)
        assert detectors.credits.call_count == 0

        if change == "switched-to-use-ours":
            reg.configs_by_id["plex-1"].markers["plex"]["on_plex_redetect"] = "restore"
        elif change == "plex-lost-its-marker":
            reg.get("plex-1").get_markers.return_value = []
        else:
            reg.configs_by_id["jellyfin-1"] = server_config("jellyfin-1", ServerType.JELLYFIN, root=_media_root(movie))
        out = _job(store, reg, movie, detectors, pubs)

        assert detectors.credits.call_count == 1
        assert _decision(store, movie, T.CREDITS).status is not DecisionStatus.DISABLED
        assert "kept" not in out.message
