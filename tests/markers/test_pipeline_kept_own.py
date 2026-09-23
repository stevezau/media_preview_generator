"""A type every server keeps its own marker of is never read from the file (spec §6.2 step 3, §14 2026-09-23).

"Keep Plex's" / "Keep Emby's" leave the server's own markers of a type in place, so an answer of ours for that type is
never shown. When every server the file's markers go to keeps its own and shows one of the type now, no local
detector (credit text, season audio) reads the file for it, and a type that ends undecided is "kept Plex's own marker"
instead of Needs review; anything else reads and decides it exactly as before.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome, ServerStatus, kept_own_reason
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


def _settings(*, skipdb=False, recap=False):
    return {
        "detect": {"intro": True, "credits": True, "recap": recap},
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

    def __init__(self, version=1):
        self.intro = MagicMock(return_value=[AUDIO_INTRO])
        self.credits = MagicMock(return_value=[TEXT_CREDITS])
        self.specs = (
            LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), self.intro, version=version),
            LocalDetectorSpec(Source.CREDITS_TEXT, frozenset({T.CREDITS}), self.credits, version=version),
        )


def _plex(path, setting="keep_plex", rows=(PLEX_CREDITS,)):
    reg = _registry(path, ServerType.PLEX)
    reg.configs_by_id["plex-1"].markers["plex"]["on_plex_redetect"] = setting
    reg.get("plex-1").get_markers.return_value = None if rows is None else list(rows)
    reg.get("plex-1").get_part_durations.return_value = [DUR]  # one version, one part
    return reg


def _emby_config(path, setting):
    markers = {"enabled": True, "library_ids": None, "emby": {"on_emby_redetect": setting}}
    return server_config("emby-1", ServerType.EMBY, root=_media_root(path), markers=markers)


def _job(
    store,
    reg,
    path,
    detectors,
    publishers,
    *,
    stage="process",
    force=False,
    skipdb=False,
    recap=False,
    probe=None,
    hints=None,
):
    clients = _clients(skipdb=LookupResult("ok", (SKIPDB_INTRO,))) if skipdb else _clients()
    settings = _settings(skipdb=skipdb, recap=recap)
    ctx = _ctx(store, reg, settings_raw=settings, clients=clients, detectors=detectors.specs, force=force)
    out, _ = _run(ctx, path, publishers, stage=stage, probe=probe, hints=hints)
    return out


def _assert_nothing_to_send(publisher, path, item_id="item-plex-1"):
    call = publisher.write.call_args
    assert call.args == (item_id, [])
    assert (call.kwargs["previous"], call.kwargs["own_previous"], call.kwargs["kept_types"]) == ([], None, frozenset())
    assert call.kwargs["canonical_path"] == path


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
        assert reg.get("plex-1").get_part_durations.call_count == 1  # the version check reuses the read's parts
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
        # Nothing decided and nothing of ours there: the write has nothing to send (a real publisher returns before
        # touching the server), and records the file on its item so Check servers reads Plex's credits back.
        _assert_nothing_to_send(plex, movie)
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        row = out.publisher_rows[0]
        assert (row["status"], row["message"]) == (ServerStatus.UP_TO_DATE.value, "Keeping Plex's credits")
        assert out.message == "credits: kept Plex's own marker"
        state = store.get_publish_state(store.get_file(movie).id, "plex-1")
        assert (state.item_id, state.markers, state.status) == ("item-plex-1", (), "written")
        assert not store.published_to_item("plex-1", "item-plex-1")  # no evidence gating: nothing of ours there
        (item,) = store.published_items("plex-1")
        assert (item.item_id, item.markers, item.kept_types, item.own_types) == (
            "item-plex-1",
            (),
            frozenset(),
            frozenset({T.CREDITS}),
        )

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
        _assert_nothing_to_send(emby, movie, item_id="item-emby-1")
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

    @pytest.mark.parametrize(
        "failure",
        ["emby-plugins-unreadable", "no-item-id", "settings-unreadable", "plex-markers-raise", "plex-parts-unknown"],
    )
    def test_whatever_can_t_be_read_reads_the_file(self, store, movie, monkeypatch, failure):
        if failure == "emby-plugins-unreadable":  # its markers may be an importer's copy (PLUGINS_UNKNOWN_DETAIL)
            reg = _registry(movie, ServerType.EMBY)
            reg.configs_by_id["emby-1"] = _emby_config(movie, "keep_emby")
            reg.get("emby-1").get_chapter_markers.return_value = [EMBY_CREDITS]
            reg.get("emby-1").get_plugin_names.return_value = None
            pubs = {"emby-1": ready_publisher("emby_bridge")}
        else:
            reg = _plex(movie)
            pubs = {"plex-1": ready_publisher()}
        if failure == "no-item-id":
            reg.get("plex-1").resolve_remote_path_to_item_id.return_value = None
        elif failure == "settings-unreadable":

            def unreadable(ctx, cfg):
                raise OSError("settings.json")

            monkeypatch.setattr(pipeline, "_live_markers_settings", unreadable)
        elif failure == "plex-markers-raise":
            reg.get("plex-1").get_markers.side_effect = RuntimeError("Plex restarting")
        elif failure == "plex-parts-unknown":  # can't tell whether the item has one version
            reg.get("plex-1").get_part_durations.return_value = []
        detectors = _Detectors()

        _job(store, reg, movie, detectors, pubs)

        assert detectors.credits.call_count == 1
        assert _decision(store, movie, T.CREDITS).status is not DecisionStatus.DISABLED

    def test_ours_of_one_type_on_the_item_leaves_only_plexs_other_type_unread(self, store, media):
        # Plex can't tell ours from its own: its intro rows are ours (the item's record says so), its credits aren't.
        our_intro = Marker(T.INTRO, 126_500, 157_500, ("chapters",))
        store.set_item_publish_state("plex-1", "item-plex-1", [our_intro], "written")
        reg = _plex(media, rows=(PLEX_INTRO, PLEX_CREDITS))
        detectors = _Detectors()

        _job(store, reg, media, detectors, {"plex-1": ready_publisher()})

        assert (detectors.intro.call_count, detectors.credits.call_count) == (1, 0)
        assert _decision(store, media, T.CREDITS).status is DecisionStatus.DISABLED
        assert _decision(store, media, T.INTRO).status is not DecisionStatus.DISABLED

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


# A credit text answer stored before (a run before this fix): 33 s after Plex's credits start, so the two disagree.
TEXT_CREDITS_APART = Candidate(T.CREDITS, 1_190_000, None, Source.CREDITS_TEXT)
# Plex serving the credits row NATIVE_CREDITS stores (served start = stored + 2 s, a non-final end 2 s earlier).
PLEX_SERVED_CREDITS = {"type": "credits", "start_ms": 1_156_521, "end_ms": 1_186_521, "final": False}


def _stored_text_answer(store, path, candidates=(TEXT_CREDITS_APART,), *, episode=False):
    st = os.stat(path)
    rec = store.upsert_file(
        FileIdentity(path, st.st_size, st.st_mtime_ns),
        duration_ms=DUR,
        season_key=os.path.dirname(path) if episode else None,
        is_movie=not episode,
    )
    store.replace_detector_answer(rec.id, {Source.CREDITS_TEXT: list(candidates)}, version=1)
    return rec


class TestStoredAnswerInReview:
    """An answer stored earlier left the type in review, so no detector is due: the kept status applies all the same."""

    def test_keep_plexs_with_its_own_marker_is_kept_not_in_review(self, store, movie):
        _stored_text_answer(store, movie)
        reg = _plex(movie, rows=(PLEX_SERVED_CREDITS,))
        detectors = _Detectors()
        plex = ready_publisher()

        out = _job(store, reg, movie, detectors, {"plex-1": plex})

        detectors.credits.assert_not_called()
        credits = _decision(store, movie, T.CREDITS)
        assert (credits.status, credits.reason) == (DecisionStatus.DISABLED, KEPT_PLEX)
        _assert_nothing_to_send(plex, movie)
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        assert (out.publisher_rows[0]["status"], out.publisher_rows[0]["message"]) == (
            ServerStatus.UP_TO_DATE.value,
            "Keeping Plex's credits",
        )
        assert out.message == "credits: kept Plex's own marker"

    def test_use_ours_leaves_it_in_review(self, store, movie):
        _stored_text_answer(store, movie)
        detectors = _Detectors()
        out = _job(
            store, _plex(movie, "restore", rows=(PLEX_SERVED_CREDITS,)), movie, detectors, {"plex-1": ready_publisher()}
        )
        detectors.credits.assert_not_called()
        assert _decision(store, movie, T.CREDITS).status is DecisionStatus.NEEDS_REVIEW
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value

    @pytest.mark.parametrize(("version", "read"), [(1, 0), (2, 1)], ids=["answer-current", "answer-due"])
    def test_a_marker_plex_since_lost_puts_it_back_in_review_or_reads_a_due_answer(self, store, movie, version, read):
        _stored_text_answer(store, movie)
        reg = _plex(movie, rows=(PLEX_SERVED_CREDITS,))
        first = _job(store, reg, movie, _Detectors(), {"plex-1": ready_publisher()})
        assert first.outcome_key == FileOutcome.UP_TO_DATE.value

        reg.get("plex-1").get_markers.return_value = []
        detectors = _Detectors(version=version)
        out = _job(store, reg, movie, detectors, {"plex-1": ready_publisher()})

        assert detectors.credits.call_count == read
        credits = _decision(store, movie, T.CREDITS)
        assert credits.status is not DecisionStatus.DISABLED
        if not read:
            # The stored answer and Plex's credits as stored still disagree, as they did before the marker went.
            assert (credits.status, out.outcome_key) == (DecisionStatus.NEEDS_REVIEW, FileOutcome.NEEDS_REVIEW.value)

    def test_a_decided_type_stays_decided_with_the_publishers_kept_note(self, store, movie):
        _stored_text_answer(store, movie, (TEXT_CREDITS,))  # agrees with Plex's credits: decided, as before
        reg = _plex(movie)
        plex = ready_publisher()

        def keeps_plexs(item_id, markers, **kwargs):
            plex.last_write_changed, plex.last_kept_types = False, frozenset({T.CREDITS})
            return []

        plex.write.side_effect = keeps_plexs
        detectors = _Detectors()

        out = _job(store, reg, movie, detectors, {"plex-1": plex})

        detectors.credits.assert_not_called()
        credits = _decision(store, movie, T.CREDITS)
        assert credits.status is DecisionStatus.DECIDED and credits.reason != KEPT_PLEX
        assert [(m.type, m.start_ms, m.end_ms) for m in plex.write.call_args.args[1]] == [(T.CREDITS, 1_291_000, DUR)]
        assert (out.outcome_key, out.publisher_rows[0]["message"]) == (
            FileOutcome.UP_TO_DATE.value,
            "Keeping Plex's credits",
        )

    @pytest.mark.parametrize(
        ("setting", "status", "row"),
        [
            ("keep_plex", DecisionStatus.DISABLED, "Keeping Plex's credits"),
            ("restore", DecisionStatus.NO_EVIDENCE, "No markers found"),
        ],
    )
    def test_nothing_found_beside_plexs_own_marker_is_kept_too(self, store, movie, setting, status, row):
        # The credit text found nothing, and Plex's own credits start too early to pass the sanity checks: the type
        # ends with nothing found (Use ours shows it), not in review, and Plex's marker stays whatever we decide.
        _stored_text_answer(store, movie, ())
        reg = _plex(movie, setting, rows=({"type": "credits", "start_ms": 600_000, "end_ms": 650_000, "final": False},))
        detectors = _Detectors()

        out = _job(store, reg, movie, detectors, {"plex-1": ready_publisher()})

        detectors.credits.assert_not_called()
        assert _decision(store, movie, T.CREDITS).status is status
        assert out.publisher_rows[0]["message"] == row

    def test_an_undecided_recap_asks_no_server(self, store, media):
        # Intro and credits are decided by chapters and the recap has nothing: Plex can't show a recap, so the check
        # of which types it keeps its own of isn't made for it.
        reg = _plex(media)
        probe = test_pipeline._probe(test_pipeline.CHAPTERS_BOTH)
        _job(store, reg, media, _Detectors(), {"plex-1": ready_publisher()}, recap=True, probe=probe)
        before = reg.get("plex-1").get_markers.call_count

        out = _job(store, reg, media, _Detectors(), {"plex-1": ready_publisher()}, recap=True, probe=probe)

        assert reg.get("plex-1").get_markers.call_count == before
        assert _decision(store, media, T.RECAP).status is DecisionStatus.NO_EVIDENCE
        assert "recap: none" in out.message


class TestRealPlexDatabase:
    """End to end on Plex's database: the kept status sends Plex exactly what Needs review sent."""

    @pytest.mark.parametrize("kind", ["movie", "episode"])
    def test_an_answer_left_in_review_writes_the_item_byte_for_byte_as_before(
        self, tmp_path, monkeypatch, request, kind
    ):
        from media_preview_generator.markers.probe import Chapter
        from media_preview_generator.markers.publishers import plex_db
        from media_preview_generator.markers.store import MarkerStore
        from tests.markers.test_plex_db_publisher import (
            CREDITS_ROW_EXTRA,
            NATIVE_CREDITS,
            _insert_taggings,
            _make_db,
            _publisher,
            _rows,
        )

        monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)  # Plex has its DB open
        path = request.getfixturevalue("media" if kind == "episode" else "movie")
        # An episode's intro is decided by its chapter and written; the credits are left to Plex either way.
        chapters = (Chapter(0, 126_771, "Chapter 1"), Chapter(126_771, 157_068, "Intro"), Chapter(157_068, None, "B"))
        probe = test_pipeline._probe(chapters if kind == "episode" else ())

        def run(name, *, kept_status):
            store = MarkerStore(str(tmp_path / f"{name}.db"))
            _stored_text_answer(store, path, episode=kind == "episode")
            folder = tmp_path / name / "Plex Media Server"
            db = _make_db(folder, parts=((path, plex_db.encode_extra_data({"pv:credits": NATIVE_CREDITS})),))
            _insert_taggings(db, (7, 563, 0, "credits", 1_154_521, 1_188_521, CREDITS_ROW_EXTRA))
            publisher = _publisher(tmp_path / name, folder, redetect="keep_plex")
            reg = _plex(path, rows=(PLEX_SERVED_CREDITS,))
            with monkeypatch.context() as m:
                if not kept_status:  # this cell as it ran before the kept status: Needs review
                    m.setattr(pipeline, "_kept_by_every_destination", lambda *a, **k: frozenset())
                out = _job(store, reg, path, _Detectors(), {"plex-1": publisher}, probe=probe, hints={"plex-1": "7"})
            item = store.get_item_publish_state("plex-1", "7")
            taggings = sorted(_rows(db, "SELECT [index], text, time_offset, end_time_offset, extra_data FROM taggings"))
            parts = _rows(db, "SELECT id, file, extra_data FROM media_parts ORDER BY id")
            status = _decision(store, path, T.CREDITS).status
            store.close()
            return out, status, (taggings, parts), item and (item.markers, item.kept_types)

        kept_out, kept_status, kept_plex, kept_item = run("kept", kept_status=True)
        review_out, review_status, review_plex, review_item = run("review", kept_status=False)

        assert (kept_status, review_status) == (DecisionStatus.DISABLED, DecisionStatus.NEEDS_REVIEW)
        # An episode's intro is written either way, so the file counts as written; a movie has nothing else to write.
        assert review_out.outcome_key == (
            FileOutcome.PUBLISHED.value if kind == "episode" else FileOutcome.NEEDS_REVIEW.value
        )
        assert kept_out.outcome_key == (
            FileOutcome.PUBLISHED.value if kind == "episode" else FileOutcome.UP_TO_DATE.value
        )
        assert kept_plex == review_plex  # Plex's database, byte for byte
        taggings, parts = kept_plex
        assert (0, "credits", 1_154_521, 1_188_521, CREDITS_ROW_EXTRA) in taggings  # Plex's own row, untouched
        assert plex_db.decode_extra_data(parts[0][2])[0]["pv:credits"] == NATIVE_CREDITS
        if kind == "episode":
            assert [t[1] for t in taggings] == ["credits", "intro"]
            assert kept_item == review_item and kept_item[1] == frozenset()
        else:
            # Only this app's record differs: the kept file is recorded on its item (nothing of ours, nothing kept),
            # so Check servers reads Plex's credits back.
            assert [t[1] for t in taggings] == ["credits"]
            assert (kept_item, review_item) == (((), frozenset()), None)


class TestSeasonChapterFollowUps:
    """A sibling left to the servers' own intro isn't asked again because a re-decide can't say "kept" (MED 3)."""

    @pytest.mark.parametrize(
        ("chapter", "asked"), [(False, False), (True, True)], ids=["still-undecided", "now-decided-by-its-chapter"]
    )
    def test_a_kept_sibling_is_asked_again_only_when_its_intro_would_be_decided(self, store, media, chapter, asked):
        from media_preview_generator.markers.decide import TypeDecision
        from media_preview_generator.markers.sources.chapters import CHAPTER_RULES_VERSION

        st = os.stat(media)
        sibling = store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns),
            duration_ms=DUR,
            season_key=os.path.dirname(media),
            is_movie=False,
        )
        kept = TypeDecision(T.INTRO, DecisionStatus.DISABLED, None, None, kept_own_reason(["Plex"]))
        store.save_decisions(sibling.id, {T.INTRO: kept}, settings_fingerprint="x")
        intro_chapter = Candidate(T.INTRO, 126_771, 157_068, Source.CHAPTERS, origin="Intro")
        store.replace_evidence(
            sibling.id, Source.CHAPTERS, [intro_chapter] if chapter else [], version=CHAPTER_RULES_VERSION
        )
        ctx = _ctx(store, _plex(media), settings_raw=_settings(), detectors=_Detectors().specs)

        pipeline._request_season_chapter_followups(ctx, {media: None})

        assert ctx.take_followups() == ([media] if asked else [])
