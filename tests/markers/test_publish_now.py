"""The Inspector editor's publish (``pipeline.publish_now``): the same per-server write a job does, bounded.

Matrix: server type x capability x marker type x kept types, plus the two halves of ruling P-R1's bound (nothing
outlives the request; an unreachable server can't multiply into one wait per server).
"""

from __future__ import annotations

import dataclasses
import os
import threading
from unittest.mock import patch

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import PLEX_PASS_UNKNOWN, ServerStatus
from media_preview_generator.markers.publishers.base import Capability, CapabilityReport, PublishError
from media_preview_generator.markers.publishers.emby import CREDITS_BEFORE_END_NOTE
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import FakeRegistry, ready_publisher, server_config

T = MarkerType
DUR = 1_320_000
INTRO = Marker(T.INTRO, 5_000, 35_000, (Source.USER.value,))
CREDITS_TO_END = Marker(T.CREDITS, 1_290_000, DUR, (Source.USER.value,))
CREDITS_EARLY = Marker(T.CREDITS, 1_200_000, 1_250_000, (Source.USER.value,))
RECAP = Marker(T.RECAP, 1_000, 20_000, (Source.USER.value,))
PREVIEW = Marker(T.PREVIEW, 1_300_000, 1_310_000, (Source.USER.value,))


@pytest.fixture
def media(tmp_path):
    folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    f = folder / "Show - S01E01.mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _registry(media, *server_types, **markers_by_id):
    root = media[: media.index("/media/") + len("/media")]
    configs = {}
    for stype in server_types:
        sid = f"{stype.value}-1"
        configs[sid] = server_config(sid, stype, root=root, markers=markers_by_id.get(sid))
    return FakeRegistry(configs)


def _known(store, media, markers=(), *, duration=DUR):
    st = os.stat(media)
    rec = store.upsert_file(
        FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=duration, season_key=None, is_movie=False
    )
    if markers:
        store.save_user_markers(rec.id, markers, settings_fingerprint="fp")
    return rec


def _settings(raw=None):
    return load_global(validate_global(raw or {"sources": [{"id": "chapters", "enabled": True}]}, None)[0])


def _publish(monkeypatch, store, media, registry, publishers, **kwargs):
    monkeypatch.setattr(pipeline, "get_marker_store", lambda: store)
    monkeypatch.setattr(pipeline, "get_global_settings", _settings)
    with patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: publishers.get(cfg.id)):
        return pipeline.publish_now(media, registry=registry, live_config=registry.get_config, **kwargs)


def _rows(rows):
    return {r["server_id"]: r for r in rows}


class TestFileState:
    def test_a_file_that_was_never_analysed_is_refused(self, store, media, monkeypatch):
        reg = _registry(media, ServerType.PLEX)
        with pytest.raises(pipeline.FileNotAnalysedError):
            _publish(monkeypatch, store, media, reg, {"plex-1": ready_publisher()})

    def test_a_file_with_no_stored_duration_is_refused(self, store, media, monkeypatch):
        """Without a duration nothing can be checked against the file's end, and no publisher can project credits."""
        _known(store, media, duration=None)
        reg = _registry(media, ServerType.PLEX)
        with pytest.raises(pipeline.FileNotAnalysedError):
            _publish(monkeypatch, store, media, reg, {"plex-1": ready_publisher()})

    def test_a_file_that_changed_on_disk_is_refused_before_any_server_is_touched(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        with open(media, "wb") as fh:
            fh.write(b"y" * 200)
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        with pytest.raises(pipeline.FileChangedError):
            _publish(monkeypatch, store, media, reg, {"plex-1": plex})
        plex.write.assert_not_called()


class TestServerTypeMatrix:
    @pytest.mark.parametrize(
        ("server_type", "publisher_name", "types"),
        [
            (ServerType.PLEX, "plex_db", ("intro", "credits")),
            (ServerType.JELLYFIN, "jellyfin_bridge", ("intro", "credits", "recap", "preview")),
            (ServerType.EMBY, "emby_bridge", ("intro", "credits")),
        ],
    )
    def test_the_write_gets_exactly_the_arguments_a_job_passes(
        self, store, media, monkeypatch, server_type, publisher_name, types
    ):
        rec = _known(store, media, [INTRO, CREDITS_TO_END])
        sid = f"{server_type.value}-1"
        reg = _registry(media, server_type)
        pub = ready_publisher(publisher_name, types)
        rows = _publish(monkeypatch, store, media, reg, {sid: pub})

        item_id, markers = pub.write.call_args.args
        kwargs = pub.write.call_args.kwargs
        assert item_id == f"item-{sid}"
        assert [(m.type, m.start_ms, m.end_ms) for m in markers] == [
            (T.INTRO, 5_000, 35_000),
            (T.CREDITS, 1_290_000, DUR),
        ]
        assert kwargs["previous"] == []  # nothing of ours on the item yet
        assert kwargs["own_previous"] is None
        assert kwargs["duration_ms"] == DUR
        assert kwargs["canonical_path"] == media
        assert kwargs["kept_types"] == frozenset()
        assert _rows(rows)[sid]["status"] == ServerStatus.WRITTEN.value
        # Recorded exactly as a job would, so Check servers reads the same rows.
        assert store.get_publish_state(rec.id, sid).status == "written"
        assert store.get_publish_basis(rec.id, sid) is not None

    def test_every_owner_gets_its_own_row_in_registry_order(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY)
        pubs = {
            "plex-1": ready_publisher(),
            "jellyfin-1": ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview")),
            "emby-1": ready_publisher("emby_bridge"),
        }
        rows = _publish(monkeypatch, store, media, reg, pubs)
        assert [(r["server_id"], r["status"]) for r in rows] == [
            ("plex-1", ServerStatus.WRITTEN.value),
            ("jellyfin-1", ServerStatus.WRITTEN.value),
            ("emby-1", ServerStatus.WRITTEN.value),
        ]


class TestCapabilityMatrix:
    @pytest.mark.parametrize(
        ("state", "message"),
        [
            (Capability.NEEDS_PLUGIN, "Install the Media Preview Bridge plugin"),
            (Capability.NEEDS_CONFIRMATION, "Confirm the database write first"),
            (Capability.UNREACHABLE, "Can't reach this server"),
            (Capability.NEEDS_PASS, "This Plex server has no Plex Pass"),
            (Capability.NEEDS_LOCAL_DB, "Plex's database isn't on this machine"),
        ],
    )
    def test_a_server_that_cannot_take_markers_is_skipped_with_its_own_message(
        self, store, media, monkeypatch, state, message
    ):
        rec = _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX)
        pub = ready_publisher()
        pub.capability.return_value = CapabilityReport(state, message)
        rows = _publish(monkeypatch, store, media, reg, {"plex-1": pub})
        assert (rows[0]["status"], rows[0]["message"]) == (ServerStatus.SKIPPED.value, message)
        pub.write.assert_not_called()
        assert store.get_publish_state(rec.id, "plex-1").status == "skipped"

    def test_a_plex_whose_pass_check_did_not_answer_waits_instead_of_writing(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX)
        pub = ready_publisher()
        pub.capability.return_value = CapabilityReport(Capability.READY, "ok", {"plex_pass": None})
        rows = _publish(monkeypatch, store, media, reg, {"plex-1": pub})
        assert rows[0]["status"] == ServerStatus.WAITING.value
        assert rows[0]["reason_code"] == PLEX_PASS_UNKNOWN
        pub.write.assert_not_called()

    def test_a_write_that_fails_is_reported_and_left_for_the_next_run(self, store, media, monkeypatch):
        rec = _known(store, media, [INTRO])
        reg = _registry(media, ServerType.JELLYFIN)
        pub = ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        pub.write.side_effect = PublishError("The plugin refused the write")
        rows = _publish(monkeypatch, store, media, reg, {"jellyfin-1": pub})
        assert (rows[0]["status"], rows[0]["message"]) == (ServerStatus.FAILED.value, "The plugin refused the write")
        assert store.get_publish_state(rec.id, "jellyfin-1").status == "failed"
        # Check servers picks the item up from here; the save itself is untouched.
        assert store.get_item_publish_state("jellyfin-1", "item-jellyfin-1").status == "failed"
        assert store.get_locked(rec.id) == {T.INTRO: Marker(T.INTRO, 5_000, 35_000, ("user",), locked=True)}

    def test_a_server_without_a_publisher_is_skipped_not_failed(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.EMBY)
        rows = _publish(monkeypatch, store, media, reg, {})
        assert rows[0]["status"] == ServerStatus.SKIPPED.value


class TestOwners:
    def test_a_server_with_intro_and_credits_off_still_gets_a_row_saying_so(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        reg.configs_by_id["jellyfin-1"].markers.update({"enabled": False})
        jf = ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        rows = _rows(_publish(monkeypatch, store, media, reg, {"plex-1": ready_publisher(), "jellyfin-1": jf}))
        assert rows["plex-1"]["status"] == ServerStatus.WRITTEN.value
        assert rows["jellyfin-1"]["status"] == ServerStatus.SKIPPED.value
        assert rows["jellyfin-1"]["message"] == "Intro & Credits is off for this server"
        jf.write.assert_not_called()

    def test_a_library_that_is_not_selected_says_which_rule_stopped_it(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX)
        reg.configs_by_id["plex-1"].markers.update({"library_ids": ["other"]})
        rows = _publish(monkeypatch, store, media, reg, {"plex-1": ready_publisher()})
        assert rows[0]["status"] == ServerStatus.SKIPPED.value
        assert "library" in rows[0]["message"].lower()


class TestTypesAndNotes:
    @pytest.mark.parametrize(
        ("extra", "plex_types", "jf_types"),
        [
            (RECAP, [T.INTRO], [T.RECAP, T.INTRO]),
            (PREVIEW, [T.INTRO], [T.INTRO, T.PREVIEW]),
        ],
        ids=["recap", "preview"],
    )
    def test_a_type_a_server_cannot_show_is_left_out_of_that_servers_write_only(
        self, store, media, monkeypatch, extra, plex_types, jf_types
    ):
        """D8: Plex and Emby take neither recap nor preview; Jellyfin takes both, from the same save."""
        _known(store, media, [INTRO, extra])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex = ready_publisher()
        jf = ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        _publish(monkeypatch, store, media, reg, {"plex-1": plex, "jellyfin-1": jf})
        assert [m.type for m in plex.write.call_args.args[1]] == plex_types
        assert [m.type for m in jf.write.call_args.args[1]] == jf_types

    def test_an_edited_credits_end_reaches_emby_and_the_row_says_what_emby_does_with_it(
        self, store, media, monkeypatch
    ):
        """D8: the end is accepted and published start-only, never silently dropped."""
        _known(store, media, [CREDITS_EARLY])
        reg = _registry(media, ServerType.EMBY)
        emby = ready_publisher("emby_bridge")
        emby.projection_note.side_effect = lambda ms, *, duration_ms: (
            CREDITS_BEFORE_END_NOTE if any(m.end_ms < duration_ms - 2_000 for m in ms) else ""
        )
        rows = _publish(monkeypatch, store, media, reg, {"emby-1": emby})
        assert [(m.start_ms, m.end_ms) for m in emby.write.call_args.args[1]] == [(1_200_000, 1_250_000)]
        assert rows[0]["status"] == ServerStatus.WRITTEN.value
        assert CREDITS_BEFORE_END_NOTE in rows[0]["message"]

    def test_credits_that_run_to_the_end_carry_no_emby_note(self, store, media, monkeypatch):
        _known(store, media, [CREDITS_TO_END])
        reg = _registry(media, ServerType.EMBY)
        emby = ready_publisher("emby_bridge")
        emby.projection_note.side_effect = lambda ms, *, duration_ms: (
            CREDITS_BEFORE_END_NOTE if any(m.end_ms < duration_ms - 2_000 for m in ms) else ""
        )
        rows = _publish(monkeypatch, store, media, reg, {"emby-1": emby})
        assert CREDITS_BEFORE_END_NOTE not in rows[0]["message"]

    def test_the_types_the_server_keeps_as_its_own_are_forwarded_to_the_write(self, store, media, monkeypatch):
        """ "Keep Plex's": the publisher decides what happens to a kept type, so it has to see the recorded set."""
        _known(store, media, [INTRO, CREDITS_TO_END])
        reg = _registry(media, ServerType.PLEX)
        store.set_item_publish_state("plex-1", "item-plex-1", [], "written", kept_types=frozenset({T.CREDITS}))
        plex = ready_publisher()
        _publish(monkeypatch, store, media, reg, {"plex-1": plex})
        assert plex.write.call_args.kwargs["kept_types"] == frozenset({T.CREDITS})

    def test_what_this_app_left_on_the_item_is_forwarded_as_previous(self, store, media, monkeypatch):
        old = Marker(T.INTRO, 1_000, 20_000, ("chapters",))
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX)
        store.set_item_publish_state("plex-1", "item-plex-1", [old], "written")
        plex = ready_publisher()
        _publish(monkeypatch, store, media, reg, {"plex-1": plex})
        assert plex.write.call_args.kwargs["previous"] == [old]


class TestTheBound:
    def test_nothing_outlives_the_request(self, store, media, monkeypatch):
        """P-R1 / step 6: no thread, no job and no worker slot -- the fan-out finishes inside the call."""
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        pubs = {
            "plex-1": ready_publisher(),
            "jellyfin-1": ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview")),
        }
        before = threading.active_count()
        rows = _publish(monkeypatch, store, media, reg, pubs)
        assert threading.active_count() == before
        assert all(r["status"] != "" for r in rows)
        # Every publisher was called on this thread, not handed to anything -- and with this file's own arguments,
        # so a fan-out that reached the right number of servers with the wrong item or path still fails here.
        saved = dataclasses.replace(INTRO, locked=True)  # every marker the editor saved is a lock
        for sid, pub in pubs.items():
            pub.write.assert_called_once()
            call = pub.write.call_args
            assert call.args == (f"item-{sid}", [saved])
            assert call.kwargs["canonical_path"] == media
            assert call.kwargs["duration_ms"] == DUR
            assert call.kwargs["previous"] == []
            assert call.kwargs["own_previous"] is None
            assert call.kwargs["kept_types"] == frozenset()

    def test_a_server_the_deadline_caught_is_reported_and_left_for_the_next_run(self, store, media, monkeypatch):
        rec = _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        store.set_publish_basis(rec.id, "jellyfin-1", decided_hash="old", item_version=1)
        ticks = iter([0.0, 0.0, 99.0])  # start, before Plex, before Jellyfin
        rows = _rows(
            _publish(
                monkeypatch,
                store,
                media,
                reg,
                {"plex-1": plex, "jellyfin-1": jf},
                deadline_s=10.0,
                clock=lambda: next(ticks),
            )
        )
        assert rows["plex-1"]["status"] == ServerStatus.WRITTEN.value
        jf.write.assert_not_called()
        assert rows["jellyfin-1"]["status"] == ServerStatus.FAILED.value
        assert rows["jellyfin-1"]["message"] == pipeline.PUBLISH_DEADLINE_MESSAGE
        assert store.get_publish_state(rec.id, "jellyfin-1").status == "failed"
        # The basis is cleared, so the next run of this file publishes it instead of reading "unchanged".
        assert store.get_publish_basis(rec.id, "jellyfin-1") is None

    def test_a_deadline_already_past_contacts_nobody(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex, jf = ready_publisher(), ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        rows = _publish(
            monkeypatch, store, media, reg, {"plex-1": plex, "jellyfin-1": jf}, deadline_s=0.0, clock=lambda: 0.0
        )
        assert [r["status"] for r in rows] == [ServerStatus.FAILED.value] * 2
        plex.write.assert_not_called()
        jf.write.assert_not_called()

    def test_a_job_running_the_same_file_gets_the_publish_instead_of_a_blocked_request(self, store, media, monkeypatch):
        rec = _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX)
        plex = ready_publisher()
        store.set_publish_basis(rec.id, "plex-1", decided_hash="old", item_version=1)
        with pipeline._PATH_LOCKS.hold(media):
            rows = _publish(monkeypatch, store, media, reg, {"plex-1": plex}, lock_wait_s=0.01)
        assert rows[0]["status"] == ServerStatus.WAITING.value
        assert rows[0]["message"] == pipeline.PUBLISH_BUSY_MESSAGE
        plex.write.assert_not_called()
        assert store.get_publish_basis(rec.id, "plex-1") is None
        assert store.get_locked(rec.id)  # the save is untouched: only the publish waits

    def test_an_unexpected_publisher_error_becomes_one_failed_row_not_a_500(self, store, media, monkeypatch):
        _known(store, media, [INTRO])
        reg = _registry(media, ServerType.PLEX, ServerType.JELLYFIN)
        plex = ready_publisher()
        plex.project.side_effect = RuntimeError("boom")  # outside _publish_to's own try blocks
        jf = ready_publisher("jellyfin_bridge", ("intro", "credits", "recap", "preview"))
        rows = _rows(_publish(monkeypatch, store, media, reg, {"plex-1": plex, "jellyfin-1": jf}))
        assert rows["plex-1"]["status"] == ServerStatus.FAILED.value
        assert rows["jellyfin-1"]["status"] == ServerStatus.WRITTEN.value  # one server's failure stops nothing else
