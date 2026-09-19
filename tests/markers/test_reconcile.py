"""Check servers (reconcile): which published items drifted, the files to run again, and queueing the job."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import reconcile
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.pipeline import RECHECK_AFTER
from media_preview_generator.markers.publishers.base import (
    UNREADABLE_IN_A_ROW,
    Capability,
    CapabilityReport,
    MarkerPublisher,
    PublishError,
    Shown,
    stopped_unreadable,
)
from media_preview_generator.markers.publishers.emby import EmbyMarkerPublisher
from media_preview_generator.markers.settings import load_server
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers import EmbyServer
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import FakeRegistry, ready_publisher, server_config

T = MarkerType
INTRO = Marker(T.INTRO, 10_000, 40_000, ("chapters",))
NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


def _published(store, server_id, item_id, path, *, markers=(INTRO,), kept=None, files=None):
    rec = store.upsert_file(FileIdentity(path, 1, 1), duration_ms=120_000, season_key=None, is_movie=False)
    store.set_publish_state(rec.id, server_id, item_id=item_id, markers=list(markers), status="written")
    store.set_item_publish_state(server_id, item_id, list(markers), "written", kept_types=kept, item_files=files)


def _registry(*configs):
    return FakeRegistry({c.id: c for c in configs})


@pytest.fixture
def media(tmp_path):
    """``media(name)``: a file on disk under ``media.root``, the folder the test servers' library holds."""
    root = tmp_path / "media"
    root.mkdir()

    def make(name):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return str(path)

    make.root = str(root)  # type: ignore[attr-defined]
    return make


class TestFindDrift:
    def test_only_drifted_items_are_returned_with_their_files(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        _published(store, "plex-1", "1", media("a.mkv"), files=(media("a.mkv"),))
        _published(store, "plex-1", "2", media("b.mkv"))
        _published(store, "plex-1", "3", media("c.mkv"))
        pub = ready_publisher()
        pub.shows_many.return_value = {"1": Shown.OURS, "2": Shown.REPLACED, "3": Shown.VERSIONS_CHANGED}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert [(d.server_id, d.item_id, d.shown, d.files) for d in drifts] == [
            ("plex-1", "2", Shown.REPLACED, (media("b.mkv"),)),
            ("plex-1", "3", Shown.VERSIONS_CHANGED, (media("c.mkv"),)),
        ]
        assert warnings == []
        (items,), kwargs = pub.shows_many.call_args
        assert [(i[0], i[1], i[2], i[3]) for i in items] == [
            ("1", [INTRO], frozenset(), (media("a.mkv"),)),
            ("2", [INTRO], frozenset(), None),
            ("3", [INTRO], frozenset(), None),
        ]
        assert kwargs["cancel_check"] is None

    @pytest.mark.parametrize(
        ("shown", "listed"),
        [
            (Shown.VERSIONS_CHANGED, "both"),
            (Shown.REPLACED, "both"),
            (Shown.MISSING, "both"),
            (Shown.OURS, "none"),
            (Shown.GONE, "recorded"),
        ],
    )
    def test_the_current_files_of_a_drifted_item_run_with_the_files_recorded_there(self, store, media, shown, listed):
        # Publishers audit MED-2: a Sonarr upgrade replaced the version this app ran; the new file had never run.
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        recorded, replacement = media("Show - S01E01 - 720p.mkv"), media("Show - S01E01 - 1080p.mkv")
        _published(store, "plex-1", "7", recorded, files=(recorded,))
        outside = "/elsewhere/Show - S01E01 - 1080p.mkv"  # a path mapping's other candidate: not on disk here
        pub = ready_publisher()
        pub.shows_many.return_value = {"7": shown}
        pub.live_files.side_effect = lambda item_id: {"7": (outside, recorded, replacement)}[item_id]
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        expected = {"both": [(shown, (recorded, replacement))], "recorded": [(shown, (recorded,))], "none": []}[listed]
        assert [(d.shown, d.files) for d in drifts] == expected and warnings == []

    def test_a_recorded_file_gone_with_its_replacement_on_disk_lists_the_replacement(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        recorded, replacement = media("Show - S01E01 - 720p.mkv"), media("Show - S01E01 - 1080p.mkv")
        _published(store, "plex-1", "7", recorded, files=(recorded,))
        os.remove(recorded)
        pub = ready_publisher()
        pub.shows_many.return_value = {"7": Shown.VERSIONS_CHANGED}
        pub.live_files.side_effect = lambda item_id: {"7": (replacement,)}[item_id]
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert [(d.item_id, d.shown, d.files) for d in drifts] == [("7", Shown.VERSIONS_CHANGED, (replacement,))]
        assert warnings == [] and store.get_item_publish_state("plex-1", "7").status == "written"

    def test_the_publisher_is_built_for_that_servers_own_client_and_config(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        registry = _registry(cfg)
        _published(store, "plex-1", "1", media("a.mkv"))
        pub = ready_publisher()
        pub.shows_many.return_value = {"1": Shown.OURS}
        with patch.object(reconcile, "publisher_for", return_value=pub) as factory:
            reconcile.find_drift(registry=registry, store=store)
        assert factory.call_args.args == (registry.get("plex-1"), cfg)

    def test_unreadable_items_are_counted_in_a_warning_not_rewritten(self, store, media):
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        _published(store, "jf-1", "x", media("a.mkv"))
        _published(store, "jf-1", "y", media("b.mkv"))
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": None, "y": None}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert drifts == [] and warnings == ["Couldn't read what 2 item(s) show on JF-1"]

    @pytest.mark.parametrize(
        ("stype", "block", "drift"),
        [
            (ServerType.PLEX, {"plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00",
                                        "on_plex_redetect": "restore"}}, True),
            (ServerType.PLEX, {"plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00",
                                        "on_plex_redetect": "keep_plex"}}, False),
            (ServerType.EMBY, {"emby": {"on_emby_redetect": "restore"}}, True),
            (ServerType.EMBY, {"emby": {"on_emby_redetect": "keep_emby"}}, False),
        ],
        ids=["plex-released", "plex-still-kept", "emby-released", "emby-still-kept"],
    )  # fmt: skip
    def test_kept_types_are_released_when_the_server_is_set_to_use_ours(self, store, media, stype, block, drift):
        cfg = server_config("srv-1", stype, root=media.root, markers={"enabled": True, "library_ids": None, **block})
        _published(store, "srv-1", "1", media("a.mkv"), kept={T.CREDITS})
        pub = ready_publisher("plex_db" if stype is ServerType.PLEX else "emby_bridge")
        pub.shows_many.return_value = {"1": Shown.OURS}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, _ = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert bool(drifts) is drift
        # A kept type goes along to the read-back either way (one Plex no longer shows at all comes back MISSING).
        assert pub.shows_many.call_args.args[0][0][2] == frozenset({T.CREDITS})

    @pytest.mark.parametrize(
        "switch",
        [{"markers": {"enabled": False, "library_ids": None}}, {"enabled": False}],
        ids=["markers-off", "server-off"],
    )
    def test_servers_with_intro_and_credits_off_are_not_read(self, store, media, switch):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root, **switch)
        _published(store, "plex-1", "1", media("a.mkv"))
        with patch.object(reconcile, "publisher_for") as factory:
            assert reconcile.find_drift(registry=_registry(cfg), store=store) == ([], [])
        factory.assert_not_called()

    def test_a_server_nothing_was_published_to_is_not_contacted(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        _published(store, "jf-1", "1", media("a.mkv"))
        with patch.object(reconcile, "publisher_for") as factory:
            assert reconcile.find_drift(registry=_registry(cfg), store=store) == ([], [])
        factory.assert_not_called()

    def test_a_server_that_cant_take_markers_is_skipped_with_its_reason(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        _published(store, "plex-1", "1", media("a.mkv"))
        pub = ready_publisher()
        pub.capability.return_value = CapabilityReport(Capability.NEEDS_LOCAL_DB, "Plex's database isn't local")
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert drifts == [] and warnings == ["Skipped PLEX-1: Plex's database isn't local"]
        pub.shows_many.assert_not_called()

    def test_a_failing_capability_check_skips_that_server_only(self, store, media):
        plex = server_config("plex-1", ServerType.PLEX, root=media.root)
        jf = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        _published(store, "plex-1", "1", media("a.mkv"))
        _published(store, "jf-1", "x", media("b.mkv"))
        broken, working = ready_publisher(), ready_publisher("jellyfin_bridge")
        broken.capability.side_effect = RuntimeError("boom")
        working.shows_many.return_value = {"x": Shown.MISSING}
        by_id = {"plex-1": broken, "jf-1": working}
        with patch.object(reconcile, "publisher_for", side_effect=lambda server, cfg, **kw: by_id[cfg.id]):
            drifts, warnings = reconcile.find_drift(registry=_registry(plex, jf), store=store)
        assert [(d.server_id, d.item_id) for d in drifts] == [("jf-1", "x")]
        assert warnings == ["Couldn't check PLEX-1"]

    def test_a_server_whose_read_back_raises_is_named_and_the_next_server_is_still_checked(self, store, media):
        plex = server_config("plex-1", ServerType.PLEX, root=media.root)
        jf = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        _published(store, "plex-1", "1", media("a.mkv"))
        _published(store, "jf-1", "x", media("b.mkv"))
        broken, working = ready_publisher(), ready_publisher("jellyfin_bridge")
        broken.shows_many.side_effect = TypeError("unsupported operand type(s) for +: 'NoneType' and 'int'")
        working.shows_many.return_value = {"x": Shown.MISSING}
        by_id = {"plex-1": broken, "jf-1": working}
        with patch.object(reconcile, "publisher_for", side_effect=lambda server, cfg, **kw: by_id[cfg.id]):
            drifts, warnings = reconcile.find_drift(registry=_registry(plex, jf), store=store)
        assert [(d.server_id, d.item_id, d.files) for d in drifts] == [("jf-1", "x", (media("b.mkv"),))]
        assert warnings == ["Couldn't check PLEX-1"]

    def test_a_server_that_went_down_is_read_twenty_times_then_skipped_and_the_other_server_is_still_checked(
        self, store, media
    ):
        from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
        from media_preview_generator.servers import JellyfinServer

        down = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        up = server_config("jf-2", ServerType.JELLYFIN, root=media.root)
        for n in range(30):
            _published(store, "jf-1", f"a{n:02d}", media(f"a{n:02d}.mkv"))
        _published(store, "jf-2", "b", media("b.mkv"))
        registry = _registry(down, up)
        publishers = {}
        for cfg in (down, up):
            server = create_autospec(JellyfinServer, instance=True)
            registry.servers_by_id[cfg.id] = server
            publishers[cfg.id] = JellyfinMarkerPublisher(server, cfg, load_server(cfg.markers, "jellyfin"))
        registry.get("jf-1").get_media_segments.return_value = None  # every request times out
        registry.get("jf-1").item_missing.return_value = None
        registry.get("jf-2").get_media_segments.return_value = []
        with (
            patch.object(reconcile, "publisher_for", side_effect=lambda server, cfg, **kw: publishers[cfg.id]),
            patch.object(JellyfinMarkerPublisher, "capability", return_value=CapabilityReport(Capability.READY, "ok")),
        ):
            drifts, warnings = reconcile.find_drift(registry=registry, store=store)
        assert registry.get("jf-1").get_media_segments.call_count == UNREADABLE_IN_A_ROW
        assert registry.get("jf-1").item_missing.call_count == UNREADABLE_IN_A_ROW
        assert warnings == ["Couldn't check JF-1"]  # not also "couldn't read what 20 item(s) show"
        assert [(d.server_id, d.item_id, d.shown) for d in drifts] == [("jf-2", "b", Shown.MISSING)]

    def test_fewer_failed_reads_in_a_row_than_the_stop_are_counted_as_unreadable(self, store, media):
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        for n in range(45):
            _published(store, "jf-1", f"a{n:02d}", media(f"a{n:02d}.mkv"))
        pub = ready_publisher("jellyfin_bridge")
        answers = iter([*([None] * 19), Shown.OURS] * 3)
        pub.shows.side_effect = lambda *a, **kw: next(answers, Shown.OURS)
        pub.shows_many.side_effect = lambda items, cancel_check=None: MarkerPublisher.shows_many(
            pub, items, cancel_check=cancel_check
        )
        with patch.object(reconcile, "publisher_for", return_value=pub):
            assert reconcile.find_drift(registry=_registry(cfg), store=store) == (
                [], ["Couldn't read what 43 item(s) show on JF-1"],
            )  # fmt: skip
        assert pub.shows.call_count == 45

    def test_the_jobs_capability_check_is_used_and_no_edit_dialog_details_are_asked(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        registry = _registry(cfg)
        _published(store, "plex-1", "1", media("a.mkv"))
        pub = ready_publisher()
        pub.shows_many.return_value = {"1": Shown.OURS}
        cached = MagicMock(return_value=CapabilityReport(Capability.READY, "ok"))
        with patch.object(reconcile, "publisher_for", return_value=pub) as factory:
            assert reconcile.find_drift(registry=registry, store=store, capability=cached) == ([], [])
        cached.assert_called_once_with(cfg, pub)
        pub.capability.assert_not_called()
        assert factory.call_args.kwargs["ui_details"] is False
        sibling = factory.call_args.kwargs["sibling_markers"]
        assert sibling(media("a.mkv")) is None  # decided markers of another version, from the store

    def test_items_a_cancel_left_unread_arent_counted_as_unreadable(self, store, media):
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        _published(store, "jf-1", "x", media("a.mkv"))
        _published(store, "jf-1", "y", media("b.mkv"))
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": Shown.OURS}  # cancelled before y was read
        with patch.object(reconcile, "publisher_for", return_value=pub):
            assert reconcile.find_drift(registry=_registry(cfg), store=store) == ([], [])

    def test_a_file_only_another_servers_intro_and_credits_library_holds_is_left_out(self, store, media):
        # Plex still owns the file for Intro & Credits; Jellyfin's library for it was deselected. Running the file
        # would publish to Plex but skip Jellyfin: Jellyfin's drift is not something this run can fix.
        plex = server_config("plex-1", ServerType.PLEX, root=media.root)
        jf = server_config(
            "jf-1", ServerType.JELLYFIN, root=media.root, markers={"enabled": True, "library_ids": ["2"]}
        )
        _published(store, "jf-1", "x", media("a.mkv"))
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": Shown.MISSING}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            assert reconcile.find_drift(registry=_registry(plex, jf), store=store) == ([], [])

    @pytest.mark.parametrize("file_here", [True, False], ids=["file-still-here", "file-gone-too"])
    def test_an_item_the_server_no_longer_has(self, store, media, file_here):
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        path = media("a.mkv")
        _published(store, "jf-1", "x", path)
        if not file_here:
            os.remove(path)
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": Shown.GONE}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, warnings = reconcile.find_drift(registry=_registry(cfg), store=store)
        assert warnings == []
        if file_here:
            # The file runs: the pipeline looks its item up again and publishes to the new one.
            assert [(d.item_id, d.shown, d.files) for d in drifts] == [("x", Shown.GONE, (path,))]
            assert store.get_item_publish_state("jf-1", "x").status == "written"
        else:
            assert drifts == []
            assert store.get_item_publish_state("jf-1", "x").status == "gone"
            assert store.published_items("jf-1") == []

    def test_a_server_without_a_client_or_publisher_is_named_in_a_warning(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        _published(store, "plex-1", "1", media("a.mkv"))
        with patch.object(reconcile, "publisher_for", return_value=None):
            assert reconcile.find_drift(registry=_registry(cfg), store=store) == (
                [],
                ["Couldn't check PLEX-1: no connection to it"],
            )

    def test_a_drifted_item_with_no_file_here_is_left_out(self, store, media):
        cfg = server_config("plex-1", ServerType.PLEX, root=media.root)
        store.set_item_publish_state("plex-1", "9", [INTRO], "written")  # no file row points at item 9
        pub = ready_publisher()
        pub.shows_many.return_value = {"9": Shown.MISSING}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            assert reconcile.find_drift(registry=_registry(cfg), store=store) == ([], [])

    def test_items_are_read_in_batches_with_progress_and_a_cancel_stops_between_them(self, store, media, monkeypatch):
        monkeypatch.setattr(reconcile, "READ_BACK_BATCH", 2)
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        for n in range(5):
            _published(store, "jf-1", f"i{n}", media(f"{n}.mkv"))
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.side_effect = lambda items, cancel_check=None: {i[0]: Shown.MISSING for i in items}
        progress = []
        cancelled = {"now": False}

        def on_progress(current, total, message):
            progress.append((current, total, message))
            cancelled["now"] = current >= 4

        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, _ = reconcile.find_drift(
                registry=_registry(cfg),
                store=store,
                cancel_check=lambda: cancelled["now"],
                progress_callback=on_progress,
            )
        assert [[i[0] for i in c.args[0]] for c in pub.shows_many.call_args_list] == [["i0", "i1"], ["i2", "i3"]]
        assert progress == [(2, 5, "Checking JF-1…"), (4, 5, "Checking JF-1…")]
        assert [d.item_id for d in drifts] == ["i0", "i1", "i2", "i3"]

    @pytest.mark.parametrize("change", ["file-deleted", "library-deselected", "path-excluded"])
    def test_files_intro_and_credits_no_longer_goes_to_are_left_out(self, store, media, change):
        # Each would run to a "not found" or "skipped" row on every Check servers run, ahead of files it can fix.
        kwargs = {}
        if change == "library-deselected":
            kwargs["markers"] = {"enabled": True, "library_ids": ["2"]}
        elif change == "path-excluded":
            kwargs["exclude_paths"] = [{"type": "path", "value": media.root + "/old"}]
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root, **kwargs)
        kept, left = media("new/S01E01.mkv"), media("old/S01E01.mkv")
        _published(store, "jf-1", "x", kept)
        _published(store, "jf-1", "x", left)
        if change == "file-deleted":
            os.remove(left)
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": Shown.MISSING}
        with patch.object(reconcile, "publisher_for", return_value=pub):
            drifts, _ = reconcile.find_drift(registry=_registry(cfg), store=store)
        expected = [] if change == "library-deselected" else [("x", (kept,))]
        assert [(d.item_id, d.files) for d in drifts] == expected

    @pytest.mark.parametrize("vendor", ["jellyfin", "emby"])
    @pytest.mark.parametrize(
        ("read", "missing", "expected"),
        [("none", True, "gone"), ("none", None, "unreadable"), ("none", False, "unreadable"), ("rows", None, "drift")],
        ids=["deleted", "read-and-lookup-failed", "read-failed-item-there", "read-fine"],
    )
    def test_real_publishers_tell_a_deleted_item_from_a_failed_read(
        self, store, media, vendor, read, missing, expected
    ):
        from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
        from media_preview_generator.servers import JellyfinServer

        stype = ServerType.JELLYFIN if vendor == "jellyfin" else ServerType.EMBY
        cfg = server_config("srv-1", stype, root=media.root)
        _published(store, "srv-1", "x", media("a.mkv"))
        server = create_autospec(JellyfinServer if vendor == "jellyfin" else EmbyServer, instance=True)
        reader = server.get_media_segments if vendor == "jellyfin" else server.get_chapter_markers
        reader.return_value = None if read == "none" else []
        server.item_missing.return_value = missing
        registry = _registry(cfg)
        registry.servers_by_id["srv-1"] = server
        settings = load_server(cfg.markers, stype.value)
        real = (JellyfinMarkerPublisher if vendor == "jellyfin" else EmbyMarkerPublisher)(server, cfg, settings)
        with (
            patch.object(reconcile, "publisher_for", return_value=real),
            patch.object(real, "capability", return_value=CapabilityReport(Capability.READY, "ok")),
        ):
            drifts, warnings = reconcile.find_drift(registry=registry, store=store)
        if expected == "gone":
            assert ([d.shown for d in drifts], warnings) == ([Shown.GONE], [])
        elif expected == "unreadable":
            assert (drifts, warnings) == ([], ["Couldn't read what 1 item(s) show on SRV-1"])
        else:
            assert ([d.shown for d in drifts], warnings) == ([Shown.MISSING], [])  # no rows of ours left
            server.item_missing.assert_not_called()  # asked only when the read failed

    @pytest.mark.parametrize("vendor", ["jellyfin", "emby"])
    @pytest.mark.parametrize(
        ("failure", "requests_per_item", "shown"),
        [("timeout", 1, None), ("refused", 1, None), ("http-404", 2, Shown.GONE), ("http-500", 2, None)],
    )
    def test_a_read_with_no_http_answer_doesnt_also_ask_whether_the_item_is_missing(
        self, monkeypatch, vendor, failure, requests_per_item, shown
    ):
        # Publishers audit LOW-5: a hung server cost two request timeouts per item before the 20-failure stop.
        import requests

        from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
        from media_preview_generator.servers import JellyfinServer

        stype = ServerType.JELLYFIN if vendor == "jellyfin" else ServerType.EMBY
        cfg = server_config("srv-1", stype, root="/")
        cfg.auth["user_id"] = "u1"
        server = (JellyfinServer if vendor == "jellyfin" else EmbyServer)(cfg)
        calls = []

        def request(method, path, **kwargs):
            calls.append(path)
            if failure == "timeout":
                raise requests.Timeout("read timed out")
            if failure == "refused":
                raise requests.ConnectionError("connection refused")
            status = int(failure.removeprefix("http-"))
            return MagicMock(status_code=status, **{"raise_for_status.side_effect": requests.HTTPError(str(status))})

        monkeypatch.setattr(server, "_request", request)
        publisher_cls = JellyfinMarkerPublisher if vendor == "jellyfin" else EmbyMarkerPublisher
        publisher = publisher_cls(server, cfg, load_server(cfg.markers, stype.value))
        items = [(str(1000 + n), [INTRO], frozenset(), None) for n in range(30)]

        answers = publisher.shows_many(items)

        read = UNREADABLE_IN_A_ROW if shown is None else len(items)
        assert list(answers.values()) == [shown] * read
        assert len(calls) == read * requests_per_item

    @pytest.mark.parametrize("vendor", ["jellyfin", "emby"])
    @pytest.mark.parametrize("answer", [True, False, None])
    def test_vendor_publishers_ask_their_server_whether_an_item_is_missing(self, vendor, answer):
        from media_preview_generator.markers.publishers.jellyfin import JellyfinMarkerPublisher
        from media_preview_generator.servers import JellyfinServer

        stype = ServerType.JELLYFIN if vendor == "jellyfin" else ServerType.EMBY
        cfg = server_config("srv-1", stype)
        server = create_autospec(JellyfinServer if vendor == "jellyfin" else EmbyServer, instance=True)
        server.item_missing.return_value = answer
        publisher_cls = JellyfinMarkerPublisher if vendor == "jellyfin" else EmbyMarkerPublisher
        assert publisher_cls(server, cfg, load_server(cfg.markers, stype.value)).item_missing("x") is answer
        server.item_missing.assert_called_once_with("x")
        assert MarkerPublisher.item_missing(ready_publisher(), "x") is None  # a publisher that can't tell

    def test_emby_versions_are_read_back_each_as_its_own_item(self, store, media):
        # Emby keeps each version as its own item with its own chapters (Task 10): a version whose chapters lost our
        # markers drifts alone, with only its own file.
        cfg = server_config("emby-1", ServerType.EMBY, root=media.root)
        intro_b = Marker(T.INTRO, 20_000, 50_000, ("chapters",))
        _published(store, "emby-1", "53", media("S01E01.mkv"))
        _published(store, "emby-1", "55", media("S01E01 - Extended.mkv"), markers=(intro_b,))
        server = create_autospec(EmbyServer, instance=True)
        chapters = {
            "53": [
                {"marker_type": "IntroStart", "start_ms": 10_000, "name": "Intro"},
                {"marker_type": "IntroEnd", "start_ms": 40_000, "name": "Intro End"},
            ],
            "55": [{"marker_type": "Chapter", "start_ms": 0, "name": "Chapter 1"}],  # a refresh dropped ours
        }
        server.get_chapter_markers.side_effect = lambda item_id, **_kw: chapters[item_id]
        server.get_bridge_info.return_value = {"installed": True, "version": "1.0.0.0", "features": ["markers"]}
        server.get_bridge_markers_access.return_value = "ok"
        registry = _registry(cfg)
        registry.servers_by_id["emby-1"] = server
        real = EmbyMarkerPublisher(server, cfg, load_server(cfg.markers, "emby"))
        with patch.object(reconcile, "publisher_for", return_value=real):
            drifts, warnings = reconcile.find_drift(registry=registry, store=store)
        assert [(d.item_id, d.shown, d.files) for d in drifts] == [
            ("55", Shown.MISSING, (media("S01E01 - Extended.mkv"),))
        ]
        assert [c.args for c in server.get_chapter_markers.call_args_list] == [("53",), ("55",)]
        assert warnings == []


class TestDefaultShowsMany:
    def test_reads_each_item_and_stops_on_cancel(self):
        pub = ready_publisher()
        calls = iter([False, True])
        out = MarkerPublisher.shows_many(pub, [("1", [INTRO], frozenset(), None), ("2", [INTRO], frozenset(), None)],
                                         cancel_check=lambda: next(calls))  # fmt: skip
        assert out == {"1": Shown.OURS}
        assert pub.shows.call_args.args == ("1", [INTRO])
        assert pub.shows.call_args.kwargs == {"kept_types": frozenset(), "item_files": None}

    def test_an_item_whose_read_raises_answers_none_and_the_rest_are_still_read(self):
        pub = ready_publisher("jellyfin_bridge")
        pub.shows.side_effect = [RuntimeError("socket closed"), Shown.MISSING]
        items = [("1", [INTRO], frozenset(), None), ("2", [INTRO], frozenset({T.CREDITS}), ("/m/a.mkv",))]
        assert MarkerPublisher.shows_many(pub, items) == {"1": None, "2": Shown.MISSING}
        assert pub.shows.call_args.kwargs == {"kept_types": frozenset({T.CREDITS}), "item_files": ("/m/a.mkv",)}

    def test_it_stops_after_twenty_failed_reads_in_a_row(self):
        pub = ready_publisher("jellyfin_bridge")
        pub.shows.side_effect = [None, Shown.OURS, *([None] * 19), RuntimeError("timed out"), Shown.OURS]
        items = [(str(n), [INTRO], frozenset(), None) for n in range(40)]
        out = MarkerPublisher.shows_many(pub, items)
        assert list(out) == [str(n) for n in range(22)]  # item 0 failed alone; 2-21 failed in a row
        assert pub.shows.call_count == 22
        assert stopped_unreadable(items, out) is True

    @pytest.mark.parametrize(
        ("answers", "asked", "stopped"),
        [
            ([None] * 20, 20, False),  # every item read: nothing left unread
            ([None] * 19, 25, False),  # a cancel after 19
            ([Shown.OURS] + [None] * 19, 25, False),
            ([None] * 20, 25, True),
            ([Shown.MISSING] + [None] * 20, 25, True),
        ],
        ids=["all-read", "cancel-after-19", "cancel-after-a-read", "stopped", "stopped-after-a-read"],
    )
    def test_stopped_unreadable(self, answers, asked, stopped):
        items = [(str(n), [INTRO], frozenset(), None) for n in range(asked)]
        assert stopped_unreadable(items, {str(n): a for n, a in enumerate(answers)}) is stopped


def _decided_credits(store, path, *, status=DecisionStatus.DECIDED, mtype=T.CREDITS):
    rec = store.upsert_file(FileIdentity(path, 1, 1), duration_ms=1_000_000, season_key=None, is_movie=True)
    marker = Marker(mtype, 900_000, 1_000_000, ("introdb", "skipdb")) if status is DecisionStatus.DECIDED else None
    store.save_decisions(
        rec.id, {mtype: TypeDecision(mtype, status, marker, None, "introdb")}, settings_fingerprint="f"
    )
    return rec


class TestServersToAskAgain:
    """Decided files whose server had no markers of its own a day ago are run again (rule 7 shortening, S1)."""

    def test_enabled_servers_are_asked_whatever_their_intro_and_credits_switch(self, tmp_path, media):
        clock = {"t": NOW - timedelta(days=2)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        paths = [media(f"{n}.mkv") for n in range(3)]
        for path, sid in zip(paths, ("plex-1", "jf-1", "emby-1"), strict=True):
            rec = _decided_credits(store, path)
            store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin=sid)
        clock["t"] = NOW
        off = {"enabled": False, "library_ids": None}
        configs = [
            server_config("plex-1", ServerType.PLEX, root=media.root, markers=off),  # lends evidence only
            server_config("jf-1", ServerType.JELLYFIN, root=media.root, enabled=False),
            server_config("emby-1", ServerType.EMBY, root=media.root),
        ]
        assert reconcile.files_to_ask_servers_again(registry=_registry(*configs), store=store, limit=10) == {
            paths[0]: frozenset({"plex-1"}),
            paths[2]: frozenset({"emby-1"}),
        }
        store.close()

    @pytest.mark.parametrize("change", ["file-deleted", "no-server-with-intro-and-credits-on-holds-it"])
    def test_files_the_pipeline_couldnt_run_are_taken_at_once_but_not_listed(self, tmp_path, media, change):
        clock = {"t": NOW - timedelta(days=2)}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        path = media("gone.mkv")
        rec = _decided_credits(store, path)
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="plex-1")
        clock["t"] = NOW
        markers = None
        if change == "file-deleted":
            os.remove(path)
        else:
            markers = {"enabled": False, "library_ids": None}
        registry = _registry(server_config("plex-1", ServerType.PLEX, root=media.root, markers=markers))
        assert reconcile.files_to_ask_servers_again(registry=registry, store=store, limit=10) == {}
        # Taken all the same (no run can check it), so it takes its turn behind files the job can run instead of
        # coming first each time.
        assert store.server_rechecks_due(["plex-1"], now=NOW, after=RECHECK_AFTER, limit=10) == []
        store.close()

    def test_nothing_is_taken_for_a_limit_of_zero(self, store):
        with patch.object(store, "server_rechecks_due") as due:
            assert (
                reconcile.files_to_ask_servers_again(
                    registry=_registry(server_config("plex-1", ServerType.PLEX)), store=store, limit=0
                )
                == {}
            )
        due.assert_not_called()

    def test_the_store_lists_them_on_the_backoff_steps(self, store):
        assert [step.days for step in RECHECK_AFTER] == [1, 2, 4, 8, 16]
        with (
            patch.object(store, "server_rechecks_due", return_value=[]) as due,
            patch.object(reconcile, "_utcnow", return_value=NOW),
        ):
            reconcile.files_to_ask_servers_again(
                registry=_registry(server_config("plex-1", ServerType.PLEX)), store=store, limit=7
            )
        due.assert_called_once_with(["plex-1"], now=NOW, after=RECHECK_AFTER, limit=7)

    def test_a_server_with_no_detection_of_its_own_is_asked_five_times_then_the_run_has_nothing_to_do(
        self, tmp_path, media
    ):
        # Plex on (the file is published there and still shows ours); Jellyfin off with no segment provider, so its
        # answer stays empty for good.
        clock = {"t": NOW}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        path = media("Movie (2020).mkv")
        rec = _decided_credits(store, path)
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")
        _published(store, "plex-1", "7", path, markers=(Marker(T.CREDITS, 900_000, 1_000_000, ("introdb",)),))
        plex = server_config("plex-1", ServerType.PLEX, root=media.root)
        jf = server_config(
            "jf-1", ServerType.JELLYFIN, root=media.root, markers={"enabled": False, "library_ids": None}
        )
        pub = ready_publisher()
        pub.shows_many.side_effect = lambda items, cancel_check=None: {i[0]: Shown.OURS for i in items}
        runs_with_files = []
        with (
            patch.object(reconcile, "publisher_for", return_value=pub),
            patch.object(reconcile, "_utcnow", side_effect=lambda: clock["t"]),
        ):
            for day in range(1, 60):
                clock["t"] = NOW + timedelta(days=day, minutes=day)  # a daily run, a little later each day
                listing = reconcile.check_servers_listing(registry=_registry(plex, jf), store=store, max_files=500)
                if listing.items:
                    runs_with_files.append(day)
                    assert [i.canonical_path for i in listing.items] == [path]
                    store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")  # the run reads it again
                    listing.count_checked(store, path)
            assert runs_with_files == [1, 3, 7, 15, 31]
            clock["t"] = NOW + timedelta(days=400)
            final = reconcile.check_servers_listing(registry=_registry(plex, jf), store=store, max_files=500)
        assert (final.items, final.warnings) == ([], [])  # so the job completes at once
        store.close()


class TestCheckServersListing:
    def _listing(self, store, drifts, *, rechecks=(), failed=(), warnings=(), max_files=500, **kwargs):
        """``rechecks``: files asked of jf-1 again; ``failed``: files of Plex item "9", whose last publish failed."""
        with (
            patch.object(reconcile, "find_drift", return_value=(list(drifts), list(warnings))) as find,
            patch.object(
                reconcile, "files_of_failed_items", return_value={p: frozenset({("plex-1", "9")}) for p in failed}
            ) as retry,
            patch.object(
                reconcile, "files_to_ask_servers_again", return_value={p: frozenset({"jf-1"}) for p in rechecks}
            ) as ask,
            patch.object(reconcile, "_utcnow", return_value=NOW),
        ):
            listing = reconcile.check_servers_listing(registry="reg", store=store, max_files=max_files, **kwargs)
        return listing, find, retry, ask

    def test_items_are_the_drifted_and_asked_again_files_without_hints_a_season_together(self, store):
        drifts = [reconcile.Drift("plex-1", "2", Shown.REPLACED, ("/tv/S/e2.mkv", "/tv/S/e1.mkv")),
                  reconcile.Drift("jf-1", "x", Shown.MISSING, ("/tv/S/e1.mkv",))]  # fmt: skip
        capability, progress = MagicMock(), MagicMock()
        listing, find, retry, ask = self._listing(
            store,
            drifts,
            rechecks=["/tv/S 1/b.mkv", "/tv/S/e2.mkv"],  # "/tv/S 1" sorts before "/tv/S/" by path, after it by folder
            failed=["/tv/S/e3.mkv", "/tv/S/e1.mkv"],
            warnings=["Couldn't read what 1 item(s) show on X"],
            capability=capability,
            progress_callback=progress,
        )
        assert [(i.canonical_path, i.item_id_by_server, i.server_id, i.title) for i in listing.items] == [
            ("/tv/S/e1.mkv", {}, "", "e1.mkv"),
            ("/tv/S/e2.mkv", {}, "", "e2.mkv"),
            ("/tv/S/e3.mkv", {}, "", "e3.mkv"),
            ("/tv/S 1/b.mkv", {}, "", "b.mkv"),
        ]
        assert listing.warnings == ["Couldn't read what 1 item(s) show on X"]
        assert listing.drifted == {
            "/tv/S/e1.mkv": frozenset({("plex-1", "2"), ("jf-1", "x")}),
            "/tv/S/e2.mkv": frozenset({("plex-1", "2")}),
        }
        assert set(find.call_args.kwargs) == {"registry", "store", "capability", "cancel_check", "progress_callback"}
        assert (find.call_args.kwargs["registry"], find.call_args.kwargs["store"]) == ("reg", store)
        assert find.call_args.kwargs["capability"] is capability
        assert find.call_args.kwargs["progress_callback"] is progress
        assert find.call_args.kwargs["cancel_check"]() is False
        # Failed items' files count against the run's files (drifted e1, e2), then e3 does.
        retry.assert_called_once_with(registry="reg", store=store, limit=500 - 2, now=NOW)
        ask.assert_called_once_with(registry="reg", store=store, limit=500 - 3, now=NOW)
        # What each file is checked for, used once it ran (CheckServersListing.count_checked).
        assert listing.rechecks == {"/tv/S 1/b.mkv": frozenset({"jf-1"}), "/tv/S/e2.mkv": frozenset({"jf-1"})}
        assert listing.retries == {path: frozenset({("plex-1", "9")}) for path in ("/tv/S/e3.mkv", "/tv/S/e1.mkv")}
        assert listing.listed_at == NOW.isoformat()

    def test_the_listing_survives_a_round_trip_through_the_jobs_config(self, store):
        drifts = [reconcile.Drift("plex-1", "2", Shown.REPLACED, ("/tv/S/e2.mkv", "/tv/S/e1.mkv")),
                  reconcile.Drift("jf-1", "x", Shown.MISSING, ("/tv/S/e1.mkv",))]  # fmt: skip
        listing, *_ = self._listing(
            store, drifts, rechecks=["/tv/S 1/b.mkv"], failed=["/tv/S/e3.mkv"], warnings=["Couldn't check X"]
        )
        stored = json.loads(json.dumps(listing.to_config()))  # what jobs.db keeps
        again = reconcile.CheckServersListing.from_config(stored)
        assert again == listing
        assert (again.rechecks, again.retries, again.listed_at) == (
            {"/tv/S 1/b.mkv": frozenset({"jf-1"})},
            {"/tv/S/e3.mkv": frozenset({("plex-1", "9")})},
            NOW.isoformat(),
        )
        assert [(i.canonical_path, i.item_id_by_server, i.server_id, i.title) for i in again.items] == [
            (i.canonical_path, i.item_id_by_server, i.server_id, i.title) for i in listing.items
        ]

    def test_a_listing_kept_by_an_older_build_reads_with_nothing_to_count(self):
        again = reconcile.CheckServersListing.from_config({"files": ["/m/a.mkv"], "warnings": [], "drifted": {}})
        assert (again.rechecks, again.retries, again.listed_at) == ({}, {}, None)

    @pytest.mark.parametrize(
        "stored",
        [None, "files", [], {}, {"files": "x"}, {"files": [1]}, {"files": [], "drifted": {"/a": [["x"]]}},
         {"files": [], "warnings": [None]}, {"files": [], "rechecks": {"/a": "jf-1"}},
         {"files": [], "rechecks": {"/a": [1]}}, {"files": [], "retries": {"/a": [["plex-1"]]}},
         {"files": [], "retries": []}, {"files": [], "listed_at": 5}],
    )  # fmt: skip
    def test_a_missing_or_unusable_stored_listing_reads_as_none(self, stored):
        assert reconcile.CheckServersListing.from_config(stored) is None

    def test_a_cancel_during_the_read_back_takes_no_server_rechecks_or_failed_items(self, store):
        _listing, _find, retry, ask = self._listing(store, [], cancel_check=lambda: True)
        retry.assert_not_called()
        ask.assert_not_called()

    def test_more_unfixable_drift_than_a_run_takes_neither_hides_later_drift_nor_starves_rechecks(self, tmp_path):
        clock = {"t": NOW}
        store = MarkerStore(str(tmp_path / "turns.db"), clock=lambda: clock["t"])
        stuck = [reconcile.Drift("jf-1", f"s{n:03d}", Shown.MISSING, (f"/m/stuck/{n:03d}.mkv",)) for n in range(600)]
        later = reconcile.Drift("jf-1", "later", Shown.MISSING, ("/m/later.mkv",))
        stuck_files = [d.files[0] for d in stuck]
        listed = []
        for run in range(3):
            clock["t"] = NOW + timedelta(days=run)
            drifts = stuck if run == 0 else [*stuck, later]  # "later" drifts after the first run
            listing, _find, _retry, ask = self._listing(store, drifts, rechecks=[f"/m/recheck/{run}.mkv"])
            assert ask.call_args.kwargs["limit"] == reconcile.RECHECK_SHARE  # rechecks keep their share
            assert f"/m/recheck/{run}.mkv" in [i.canonical_path for i in listing.items]
            assert len(listing.drifted) == 500 - reconcile.RECHECK_SHARE
            assert listing.warnings == [
                f"{len(drifts) - len(listing.drifted)} more changed file(s) are checked on a later run"
            ]
            listed.append(set(listing.drifted))
        assert listed[0] == set(stuck_files[:400])
        # Never listed first (the new drift and the 200 left over), then the least recently listed.
        assert listed[1] == {"/m/later.mkv", *stuck_files[400:], *stuck_files[:199]}
        assert listed[2] == set(stuck_files[199:400]) | {"/m/later.mkv", *stuck_files[:198]}
        assert set().union(*listed) == {*stuck_files, "/m/later.mkv"}
        store.close()

    def test_a_file_whose_server_dropped_the_item_stops_being_listed_on_the_next_run(self, store, media):
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        registry = _registry(cfg)
        path = media("a.mkv")
        _published(store, "jf-1", "x", path)
        _published(store, "plex-1", "7", path)
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": Shown.MISSING}
        pub.item_missing.return_value = True
        with patch.object(reconcile, "publisher_for", return_value=pub) as factory:
            listing = reconcile.check_servers_listing(registry=registry, store=store, max_files=500)
            assert [i.canonical_path for i in listing.items] == [path]
            # The file ran: Jellyfin doesn't have it any more (no item for its path); Plex was fine.
            rows = [
                {"server_id": "jf-1", "status": "markers_waiting", "reason_code": "not_in_library"},
                {"server_id": "plex-1", "status": "markers_waiting", "reason_code": "not_in_library"},
                {"server_id": "jf-1", "status": "markers_up_to_date"},
            ]
            factory.reset_mock()
            assert listing.confirmed_gone_items(registry, path, rows) == {("jf-1", "x")}
            assert store.get_item_publish_state("jf-1", "x").status == "written"  # the job marks it, once retried
            store.mark_item_gone("jf-1", "x")
            assert store.get_item_publish_state("plex-1", "7").status == "written"  # plex-1 wasn't listed for it
            factory.assert_called_once_with(registry.get("jf-1"), cfg, ui_details=False)
            pub.item_missing.assert_called_once_with("x")
            again = reconcile.check_servers_listing(registry=registry, store=store, max_files=500)
        assert again.items == []

    @pytest.mark.parametrize(
        "lookup",
        [
            {"return_value": None},
            {"return_value": False},
            {"side_effect": TimeoutError("read timed out")},
            {"factory": None},
            {"no-server": True},
        ],
        ids=["couldnt-ask", "item-still-there", "lookup-raises", "no-publisher", "server-not-in-registry"],
    )
    def test_an_item_the_server_doesnt_confirm_missing_stays_and_is_listed_again(self, store, media, lookup):
        # "Not in this server's library" can come from a lookup that failed: no retry, the item is read back again.
        cfg = server_config("jf-1", ServerType.JELLYFIN, root=media.root)
        registry = _registry(cfg)
        path = media("a.mkv")
        _published(store, "jf-1", "x", path)
        pub = ready_publisher("jellyfin_bridge")
        pub.shows_many.return_value = {"x": Shown.MISSING}
        if "return_value" in lookup:
            pub.item_missing.return_value = lookup["return_value"]
        elif "side_effect" in lookup:
            pub.item_missing.side_effect = lookup["side_effect"]
        with patch.object(reconcile, "publisher_for", return_value=pub) as factory:
            listing = reconcile.check_servers_listing(registry=registry, store=store, max_files=500)
            if "factory" in lookup:
                factory.return_value = None
            if "no-server" in lookup:
                registry.configs_by_id.pop("jf-1")
            gone = listing.confirmed_gone_items(
                registry,
                path,
                [{"server_id": "jf-1", "status": "markers_waiting", "reason_code": "not_in_library"}],
            )
            assert gone == set()
            assert store.get_item_publish_state("jf-1", "x").status == "written"
            registry.configs_by_id["jf-1"] = cfg
            factory.return_value = pub
            again = reconcile.check_servers_listing(registry=registry, store=store, max_files=500)
        assert [i.canonical_path for i in again.items] == [path]

    @pytest.mark.parametrize(
        "row",
        [
            {"server_id": "jf-1", "status": "markers_waiting", "reason_code": "plex_pass_unknown"},
            {"server_id": "jf-1", "status": "failed", "reason_code": "not_in_library"},
            {"server_id": "emby-1", "status": "markers_waiting", "reason_code": "not_in_library"},
            "not a row",
        ],
        ids=["other-reason", "not-waiting", "other-server", "junk"],
    )
    def test_other_rows_forget_nothing(self, store, row):
        store.set_item_publish_state("jf-1", "x", [INTRO], "written")
        registry = _registry(server_config("jf-1", ServerType.JELLYFIN))
        pub = ready_publisher("jellyfin_bridge")
        pub.item_missing.return_value = True
        listing = reconcile.CheckServersListing([], [], {"/m/a.mkv": frozenset({("jf-1", "x")})})
        with patch.object(reconcile, "publisher_for", return_value=pub):
            assert listing.confirmed_gone_items(registry, "/m/a.mkv", [row]) == set()
            assert listing.confirmed_gone_items(registry, "/m/other.mkv", [{"server_id": "jf-1", "status": "markers_waiting",
                                                                            "reason_code": "not_in_library"}]) == set()  # fmt: skip
        assert store.get_item_publish_state("jf-1", "x").status == "written"
        pub.item_missing.assert_not_called()


class TestFailedItems:
    """Items whose last publish failed are run again on the RECHECK_AFTER backoff (publishers audit LOW-4)."""

    @staticmethod
    def _failed(store, server_id, item_id, path):
        _published(store, server_id, item_id, path)
        store.set_item_publish_state(server_id, item_id, None, "failed")

    def test_a_transient_failure_is_retried_a_day_later_and_then_read_back(self, tmp_path, media):
        from tests.markers.test_pipeline import CHAPTERS_BOTH, _ctx, _probe, _run
        from tests.markers.test_pipeline import _registry as pipeline_registry

        clock = {"t": NOW}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        path = media("tv/Show (2020) {tvdb-1}/Season 01/Show (2020) - S01E01.mkv")
        registry = pipeline_registry(path, ServerType.PLEX)
        pub = ready_publisher()
        _run(_ctx(store, registry), path, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        pub.write.side_effect = PublishError("Plex is busy writing its database", state=Capability.UNREACHABLE)
        _run(_ctx(store, registry, force=True), path, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        assert store.get_item_publish_state("plex-1", "item-plex-1").status == "failed"
        pub.shows_many.side_effect = lambda items, cancel_check=None: {i[0]: Shown.OURS for i in items}

        def check_servers(days):
            clock["t"] = NOW + timedelta(days=days)
            with (
                patch.object(reconcile, "publisher_for", return_value=pub),
                patch.object(reconcile, "_utcnow", return_value=clock["t"]),
            ):
                listing = reconcile.check_servers_listing(registry=registry, store=store, max_files=500)
            return [i.canonical_path for i in listing.items]

        assert check_servers(0.5) == []  # not due yet
        assert check_servers(1.1) == [path] and not pub.shows_many.called
        pub.write.side_effect = pub.succeed
        out, _ = _run(_ctx(store, registry), path, {"plex-1": pub}, probe=_probe(CHAPTERS_BOTH))
        assert out.publisher_rows[0]["status"] == "markers_up_to_date"
        assert store.get_item_publish_state("plex-1", "item-plex-1").status == "written"
        assert check_servers(3.2) == []
        assert [[i[0] for i in c.args[0]] for c in pub.shows_many.call_args_list] == [["item-plex-1"]]
        store.close()

    def test_a_failure_that_stays_is_retried_on_the_backoff_five_times_then_no_more(self, tmp_path, media):
        clock = {"t": NOW}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        path = media("Movie (2020).mkv")
        self._failed(store, "plex-1", "7", path)
        registry = _registry(server_config("plex-1", ServerType.PLEX, root=media.root))
        runs_with_files = []
        with patch.object(reconcile, "_utcnow", side_effect=lambda: clock["t"]):
            for day in range(1, 60):
                clock["t"] = NOW + timedelta(days=day, minutes=day)  # a daily run, a little later each day
                listing = reconcile.check_servers_listing(registry=registry, store=store, max_files=500)
                if listing.items:
                    runs_with_files.append(day)
                    assert [i.canonical_path for i in listing.items] == [path]
                    store.set_item_publish_state("plex-1", "7", None, "failed")  # the run failed again
                    listing.count_checked(store, path)
            clock["t"] = NOW + timedelta(days=400)
            final = reconcile.check_servers_listing(registry=registry, store=store, max_files=500)
        assert runs_with_files == [1, 3, 7, 15, 31]
        assert (final.items, final.warnings) == ([], [])
        store.close()

    def test_a_new_failure_after_a_success_starts_the_backoff_over(self, tmp_path, media):
        clock = {"t": NOW}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        self._failed(store, "plex-1", "7", media("a.mkv"))
        for day in (2, 4, 8, 16, 32):
            store.record_failed_item_retries([("plex-1", "7")])
            clock["t"] = NOW + timedelta(days=day)
        assert store.failed_items_due(["plex-1"], now=NOW + timedelta(days=400), after=RECHECK_AFTER) == []
        store.set_item_publish_state("plex-1", "7", [INTRO], "written")
        store.set_item_publish_state("plex-1", "7", None, "failed")
        later = clock["t"] + timedelta(days=1, minutes=1)
        assert store.failed_items_due(["plex-1"], now=later, after=RECHECK_AFTER) == [("plex-1", "7")]
        store.close()

    def test_a_retry_is_counted_once_per_listing(self, tmp_path, media):
        # Each of an item's files counts it once it ran, and a revived run counts again what it runs again.
        clock = {"t": NOW}
        store = MarkerStore(str(tmp_path / "clocked.db"), clock=lambda: clock["t"])
        self._failed(store, "plex-1", "7", media("a.mkv"))

        def counted():
            row = store._conn.execute("SELECT retries, retried_at FROM failed_item_retries").fetchone()
            return tuple(row)

        for minutes in (5, 9, 30):
            clock["t"] = NOW + timedelta(minutes=minutes)
            store.record_failed_item_retries([("plex-1", "7")], listed_at=NOW.isoformat())
        assert counted() == (1, (NOW + timedelta(minutes=5)).isoformat())
        next_run = NOW + timedelta(days=1, minutes=1)
        clock["t"] = next_run + timedelta(minutes=2)
        store.record_failed_item_retries([("plex-1", "7")], listed_at=next_run.isoformat())
        assert counted() == (2, clock["t"].isoformat())
        store.record_failed_item_retries([("plex-1", "7")])  # no listing: always counts
        assert counted()[0] == 3
        store.close()

    @pytest.mark.parametrize(
        ("change", "listed", "counted_now"),
        [
            ("none", True, False),  # counted once one of its files ran
            ("file-deleted", False, True),  # no file of it can run
            ("server-off", False, False),
            ("intro-and-credits-off", False, False),
            ("other-server", False, False),
        ],
    )
    def test_which_failed_items_are_listed_and_which_counted_at_once(self, store, media, change, listed, counted_now):
        path = media("a.mkv")
        self._failed(store, "jf-1" if change == "other-server" else "plex-1", "7", path)
        kwargs = {}
        if change == "server-off":
            kwargs["enabled"] = False
        elif change == "intro-and-credits-off":
            kwargs["markers"] = {"enabled": False, "library_ids": None}
        registry = _registry(server_config("plex-1", ServerType.PLEX, root=media.root, **kwargs))
        if change == "file-deleted":
            os.remove(path)
        later = NOW + timedelta(days=400)
        with patch.object(store, "record_failed_item_retries", wraps=store.record_failed_item_retries) as record:
            files = reconcile.files_of_failed_items(registry=registry, store=store, limit=10, now=later)
        sid = "jf-1" if change == "other-server" else "plex-1"
        assert files == ({path: frozenset({(sid, "7")})} if listed else {})
        record.assert_called_once_with([(sid, "7")] if counted_now else [])

    def test_items_whose_files_dont_fit_the_limit_wait_uncounted(self, store, media):
        # Versions of one item share a record; each item's files are listed whole or not at all.
        self._failed(store, "plex-1", "1", media("a/1.mkv"))
        _published(store, "plex-1", "1", media("a/2.mkv"))
        store.set_item_publish_state("plex-1", "1", None, "failed")
        self._failed(store, "plex-1", "2", media("b/1.mkv"))
        registry = _registry(server_config("plex-1", ServerType.PLEX, root=media.root))
        later = NOW + timedelta(days=400)
        assert reconcile.files_of_failed_items(registry=registry, store=store, limit=1, now=later) == {}
        assert reconcile.files_of_failed_items(registry=registry, store=store, limit=0, now=later) == {}
        first = reconcile.files_of_failed_items(registry=registry, store=store, limit=2, now=later)
        assert first == {media("a/1.mkv"): frozenset({("plex-1", "1")}), media("a/2.mkv"): frozenset({("plex-1", "1")})}
        assert reconcile.files_of_failed_items(registry=registry, store=store, limit=2, now=later) == first  # not run
        store.record_failed_item_retries([("plex-1", "1")])  # item 1's files ran
        assert reconcile.files_of_failed_items(registry=registry, store=store, limit=2, now=later) == {
            media("b/1.mkv"): frozenset({("plex-1", "2")})
        }

    def test_a_file_of_two_failed_items_is_listed_for_both(self, store, media):
        # One file, published to Plex and Jellyfin, both of whose writes failed: it runs once for both retries.
        path = media("a.mkv")
        self._failed(store, "plex-1", "7", path)
        self._failed(store, "jf-1", "x", path)
        registry = _registry(
            server_config("plex-1", ServerType.PLEX, root=media.root),
            server_config("jf-1", ServerType.JELLYFIN, root=media.root),
        )
        files = reconcile.files_of_failed_items(registry=registry, store=store, limit=1, now=NOW + timedelta(days=400))
        assert files == {path: frozenset({("plex-1", "7"), ("jf-1", "x")})}


class TestChecksAreUsedOnlyOnceTheirFileRan:
    """A recheck of a server's empty answer and a failed item's retry are used once a file listed for them ran
    (``CheckServersListing.count_checked``, which the job calls per file result), not when the run lists them: a run
    cancelled, or ended by a restart, before a file leaves that file's checks due for the next run."""

    @pytest.fixture
    def world(self, tmp_path, media):
        clock = {"t": NOW - timedelta(days=2)}
        db = str(tmp_path / "checks.db")
        store = MarkerStore(db, clock=lambda: clock["t"])
        recheck = media("recheck.mkv")  # decided credits; Jellyfin had no markers of its own two days ago
        rec = _decided_credits(store, recheck)
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")
        failed = [media("item7/1.mkv"), media("item7/2.mkv")]  # two versions of Plex item 7, whose last publish failed
        for path in failed:
            _published(store, "plex-1", "7", path)
        store.set_item_publish_state("plex-1", "7", None, "failed")
        clock["t"] = NOW
        registry = _registry(
            server_config("plex-1", ServerType.PLEX, root=media.root),
            server_config("jf-1", ServerType.JELLYFIN, root=media.root),
        )
        world = SimpleNamespace(clock=clock, db=db, store=store, rec=rec, recheck=recheck, failed=failed)

        def listing(on=None):
            with patch.object(reconcile, "_utcnow", side_effect=lambda: clock["t"]):
                return reconcile.check_servers_listing(registry=registry, store=on or world.store, max_files=500)

        def counters(on=None):
            """(rereads, taken_at) of the recheck, (retries, retried_at) of item 7; None where nothing is stored."""
            conn = (on or world.store)._conn
            reread = conn.execute(
                "SELECT rereads, taken_at FROM server_marker_rereads WHERE file_id=? AND server_id='jf-1'", (rec.id,)
            ).fetchone()
            retry = conn.execute(
                "SELECT retries, retried_at FROM failed_item_retries WHERE server_id='plex-1' AND item_id='7'"
            ).fetchone()
            return (tuple(reread) if reread else None), (tuple(retry) if retry else None)

        def reopen():
            """The process died: a new one opens the same markers.db."""
            world.store.close()
            world.store = MarkerStore(db, clock=lambda: clock["t"])
            return world.store

        world.listing, world.counters, world.reopen = listing, counters, reopen
        yield world
        world.store.close()

    @staticmethod
    def _paths(listing):
        return {item.canonical_path for item in listing.items}

    def test_the_listing_says_what_each_file_is_checked_for(self, world):
        listing = world.listing()
        assert self._paths(listing) == {world.recheck, *world.failed}
        assert listing.rechecks == {world.recheck: frozenset({"jf-1"})}
        assert listing.retries == {path: frozenset({("plex-1", "7")}) for path in world.failed}
        assert listing.listed_at == NOW.isoformat()
        assert world.counters() == (None, None)  # listing uses nothing

    def test_a_run_cancelled_before_any_file_ran_uses_nothing(self, world):
        assert self._paths(world.listing()) == {world.recheck, *world.failed}
        world.clock["t"] = NOW + timedelta(hours=1)
        assert world.counters() == (None, None)
        assert self._paths(world.listing()) == {world.recheck, *world.failed}

    def test_a_run_cancelled_midway_uses_only_the_checks_of_the_files_that_ran(self, world):
        listing = world.listing()
        listing.count_checked(world.store, world.recheck)  # then the cancel, before item 7's files
        assert world.counters() == ((0, NOW.isoformat()), None)
        world.clock["t"] = NOW + timedelta(hours=1)
        # Item 7 is still due; the recheck waits a day like any file checked.
        assert self._paths(world.listing()) == set(world.failed)

    def test_a_run_that_completes_uses_each_check_once(self, world):
        listing = world.listing()
        for path in (world.recheck, *world.failed):
            listing.count_checked(world.store, path)
        assert world.counters() == ((0, NOW.isoformat()), (1, NOW.isoformat()))  # item 7: one retry for its two files
        world.clock["t"] = NOW + timedelta(hours=1)
        assert world.listing().items == []
        world.clock["t"] = NOW + timedelta(days=2, minutes=1)  # item 7's second step; nothing re-read the answer here
        assert self._paths(world.listing()) == {world.recheck, *world.failed}

    def test_a_run_revived_after_a_restart_counts_each_check_once(self, world):
        listing = world.listing()
        saved = json.loads(json.dumps(listing.to_config()))  # what the job's config keeps
        listing.count_checked(world.store, world.recheck)  # finished before the restart
        listing.count_checked(world.store, world.failed[0])  # counted, then the process died before its row was kept
        store = world.reopen()
        world.clock["t"] = NOW + timedelta(minutes=10)
        revived = reconcile.CheckServersListing.from_config(saved)
        for path in world.failed:  # the recheck file's row was kept, so the revived run skips it
            revived.count_checked(store, path)
        assert world.counters() == ((0, NOW.isoformat()), (1, NOW.isoformat()))

    def test_a_run_the_process_died_in_leaves_the_files_that_didnt_run_due(self, world):
        # Not revived (a running job is marked failed at the next start): the next run lists what the first didn't check.
        world.listing().count_checked(world.store, world.recheck)
        store = world.reopen()
        world.clock["t"] = NOW + timedelta(hours=1)
        assert self._paths(world.listing(store)) == set(world.failed)
        assert world.counters() == ((0, NOW.isoformat()), None)

    def test_a_listing_kept_by_an_older_build_counts_nothing(self, world):
        # That build used the checks up when it listed them; its revived run mustn't count them again.
        listing = world.listing()
        old = {"files": [i.canonical_path for i in listing.items], "warnings": [], "drifted": {}}
        revived = reconcile.CheckServersListing.from_config(old)
        for path in (world.recheck, *world.failed):
            revived.count_checked(world.store, path)
        assert world.counters() == (None, None)

    @pytest.mark.parametrize("failing", ["mark_server_rechecks_taken", "record_failed_item_retries"])
    def test_a_store_error_while_counting_is_logged_not_raised_and_the_other_count_still_happens(self, world, failing):
        from loguru import logger

        path = world.recheck  # Jellyfin's empty answer for it is stored, so its mark shows in the counters
        # A file listed both to ask Jellyfin again and for item 7's retry.
        listing = reconcile.CheckServersListing(
            [], [], rechecks={path: frozenset({"jf-1"})}, retries={path: frozenset({("plex-1", "7")})},
            listed_at=NOW.isoformat(),
        )  # fmt: skip
        lines: list[str] = []
        handler = logger.add(lambda m: lines.append(m), level="WARNING", format="{level} {message}")
        try:
            with patch.object(world.store, failing, side_effect=sqlite3.OperationalError("locked")) as broken:
                listing.count_checked(world.store, path)  # the job goes on to record the file's row
        finally:
            logger.remove(handler)
        if failing == "mark_server_rechecks_taken":
            broken.assert_called_once_with([(path, "jf-1")])
            assert world.counters() == (None, (1, NOW.isoformat()))
        else:
            broken.assert_called_once_with([("plex-1", "7")], listed_at=NOW.isoformat())
            assert world.counters() == ((0, NOW.isoformat()), None)
        assert [line.strip() for line in lines] == [
            "WARNING Check servers couldn't count the checks of recheck.mkv: OperationalError"
        ]


class TestQueueing:
    @pytest.fixture
    def jm(self, monkeypatch):
        jm = MagicMock()
        jm.get_pending_jobs.return_value = []
        jm.get_running_jobs.return_value = []
        monkeypatch.setattr(reconcile, "get_job_manager", lambda: jm)
        return jm

    @pytest.mark.parametrize(
        ("kwargs", "priority", "schedule_id"),
        [({}, 3, ""), ({"priority": 1, "parent_schedule_id": "sch-1"}, 1, "sch-1")],
        ids=["on-demand", "schedule"],
    )
    def test_run_queues_one_job_when_markers_are_on_somewhere(self, jm, kwargs, priority, schedule_id):
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=True),
            patch(
                "media_preview_generator.markers.triggers.create_intro_credits_job", return_value=MagicMock(id="r1")
            ) as create,
        ):
            assert reconcile.run_markers_reconcile(**kwargs) == reconcile.ReconcileQueued("r1", created=True)
        assert create.call_args.kwargs == {
            "library_name": "Intro & Credits · Check servers", "priority": priority, "source": "reconcile",
            "reconcile": True, "parent_schedule_id": schedule_id,
        }  # fmt: skip

    @pytest.mark.parametrize("state", ["pending", "running"])
    def test_run_reuses_a_queued_or_running_reconcile_job(self, jm, state):
        existing = MagicMock(id="r0", kind=JOB_KIND_INTRO_CREDITS, config={"reconcile": True})
        getattr(jm, f"get_{state}_jobs").return_value = [existing]
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=True),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create,
        ):
            assert reconcile.run_markers_reconcile(priority=1) == reconcile.ReconcileQueued("r0", created=False)
            assert reconcile.unfinished_reconcile_job() is existing
        create.assert_not_called()

    @pytest.mark.parametrize(
        "other",
        [
            MagicMock(id="ic", kind=JOB_KIND_INTRO_CREDITS, config={"libraries": []}),
            MagicMock(id="pv", kind="previews", config={"reconcile": True}),
        ],
        ids=["library-job", "preview-job"],
    )
    def test_other_jobs_dont_count_as_a_reconcile_job(self, jm, other):
        jm.get_running_jobs.return_value = [other]
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=True),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job", return_value=MagicMock(id="r1")),
        ):
            assert reconcile.run_markers_reconcile() == ("r1", True)

    def test_two_calls_at_once_queue_one_job(self, jm):
        created = []
        entered = threading.Event()

        def create(**kwargs):
            entered.set()
            job = MagicMock(id=f"r{len(created)}", kind=JOB_KIND_INTRO_CREDITS, config={"reconcile": True})
            threading.Event().wait(0.1)  # the second call arrives while the first job is being created
            created.append(job)
            jm.get_pending_jobs.return_value = list(created)
            return job

        results = []
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=True),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job", side_effect=create),
        ):
            first = threading.Thread(target=lambda: results.append(reconcile.run_markers_reconcile()))
            first.start()
            assert entered.wait(5)
            results.append(reconcile.run_markers_reconcile())
            first.join(5)
        assert len(created) == 1 and sorted(results, key=lambda r: not r.created) == [("r0", True), ("r0", False)]

    def test_run_does_nothing_when_markers_are_off_everywhere(self, jm):
        with (
            patch("media_preview_generator.markers.triggers.markers_enabled_anywhere", return_value=False),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create,
        ):
            assert reconcile.run_markers_reconcile() == (None, False)
        create.assert_not_called()
