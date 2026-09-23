"""Intro & Credits job runner: file selection, the job thread, delegation from the preview runner and restarts."""

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_PREVIEWS
from media_preview_generator.markers import job_runner
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType
from media_preview_generator.markers.source_counts import DecidedByTally
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library, ServerType
from tests.markers.fakes import FakeRegistry, server_config


class TestBuildItems:
    def test_file_paths_are_resolved_expanded_deduped_and_season_sorted(self, tmp_path):
        season = tmp_path / "tv" / "Show" / "Season 01"
        season.mkdir(parents=True)
        for name in ("S01E02.mkv", "S01E01.mkv"):
            (season / name).write_bytes(b"x")
        other = tmp_path / "tv" / "Another" / "Season 01"
        other.mkdir(parents=True)
        (other / "S01E01.mkv").write_bytes(b"x")
        reg = FakeRegistry(
            {
                "jf-1": server_config(
                    "jf-1", ServerType.JELLYFIN, libraries=[Library("1", "TV", (str(tmp_path / "tv"),), enabled=False)]
                )
            }
        )
        cfg = {
            "file_paths": [str(season), str(season / "S01E01.mkv"), str(other / "S01E01.mkv")],
            "webhook_item_id_hints": {str(other / "S01E01.mkv"): {"jf-1": "abc"}},
        }
        with patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            side_effect=lambda p, configs, log_resolution=True: (p, []),
        ) as resolve:
            items, warnings, _sent = job_runner.build_items(cfg, registry=reg)
        assert [i.canonical_path for i in items] == [
            str(other / "S01E01.mkv"),
            str(season / "S01E01.mkv"),
            str(season / "S01E02.mkv"),
        ]
        assert items[0].item_id_by_server == {"jf-1": "abc"} and items[1].item_id_by_server == {}
        assert [i.title for i in items] == ["S01E01.mkv", "S01E01.mkv", "S01E02.mkv"]
        assert warnings == []
        assert all(c.kwargs.get("log_resolution") is False for c in resolve.call_args_list)
        # Resolved against every library: the preview opt-in plays no part in Intro & Credits ownership.
        for c in resolve.call_args_list:
            assert [(cfg.id, [(lib.id, lib.enabled) for lib in cfg.libraries]) for cfg in c.args[1]] == [
                ("jf-1", [("1", True)])
            ]

    def test_two_sender_paths_of_one_file_give_one_item_with_both_senders_item_ids(self):
        # A Sonarr and a Plex webhook for the same episode joined one follow-up: each sender's path maps to one file.
        reg = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        cfg = {
            "file_paths": ["/sonarr/tv/Show/S01E01.mkv", "/plex/tv/Show/S01E01.mkv"],
            "webhook_item_id_hints": {
                "/sonarr/tv/Show/S01E01.mkv": {"jf-1": "jf-item"},
                "/plex/tv/Show/S01E01.mkv": {"plex-1": "4242", "jf-1": "other"},
            },
        }
        with patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            return_value=("/media/tv/Show/S01E01.mkv", []),
        ):
            items, _, sent = job_runner.build_items(cfg, registry=reg)
        assert [(i.canonical_path, i.item_id_by_server) for i in items] == [
            ("/media/tv/Show/S01E01.mkv", {"jf-1": "jf-item", "plex-1": "4242"})  # the first sender's id wins a clash
        ]
        assert sent == {"/media/tv/Show/S01E01.mkv": "/sonarr/tv/Show/S01E01.mkv"}

    def test_file_paths_are_resolved_to_the_canonical_local_path(self):
        reg = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        with patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            return_value=("/media/tv/Show/S01E01.mkv", []),
        ):
            items, _, _sent = job_runner.build_items(
                {"file_paths": ["/data/tv/Show/S01E01.mkv"], "webhook_item_id_hints": {}}, registry=reg
            )
        assert [(i.canonical_path, i.server_id) for i in items] == [("/media/tv/Show/S01E01.mkv", "")]

    def test_explicit_libraries_are_enumerated_per_server_with_their_ids(self):
        reg = FakeRegistry(
            {
                "plex-1": server_config(
                    "plex-1",
                    ServerType.PLEX,
                    libraries=[Library("1", "TV", ("/media/tv",), enabled=False), Library("2", "Films", ("/m",))],
                ),
                "jf-1": server_config("jf-1", ServerType.JELLYFIN),
            }
        )
        seen = {}

        def fake_enumerate(candidates, *, enumerate_one, cancel_check=None, label, progress_callback=None):
            processor = MagicMock()

            def list_paths(cfg, **kw):
                seen[cfg.id] = kw
                return [ProcessableItem(f"/media/{cfg.id}/a.mkv", cfg.id)]

            processor.list_canonical_paths.side_effect = list_paths
            pairs = [(cfg, item) for cfg in candidates for item in enumerate_one(processor, cfg)]
            assert all(lib.enabled for cfg in candidates for lib in cfg.libraries), "preview opt-in must not filter"
            return pairs, [("JF-1", "TimeoutError: slow")]

        cancel, progress = MagicMock(return_value=False), MagicMock()
        cfg = {"libraries": [{"server_id": "plex-1", "library_id": "1"}, {"server_id": "plex-1", "library_id": "2"}]}
        with patch(
            "media_preview_generator.jobs.orchestrator._enumerate_items_for_servers", side_effect=fake_enumerate
        ) as enum:
            items, warnings, _sent = job_runner.build_items(
                cfg, registry=reg, cancel_check=cancel, progress_callback=progress
            )
        assert list(seen) == ["plex-1"]
        assert seen["plex-1"] == {"library_ids": ["1", "2"], "cancel_check": cancel, "progress_callback": progress}
        assert (
            enum.call_args.kwargs["cancel_check"] is cancel and enum.call_args.kwargs["progress_callback"] is progress
        )
        assert [i.canonical_path for i in items] == ["/media/plex-1/a.mkv"]
        assert warnings == ["Couldn't list JF-1: TimeoutError: slow"]

    def test_selected_libraries_outside_the_markers_selection_are_skipped_with_a_warning(self):
        reg = FakeRegistry(
            {
                "jf-1": server_config(
                    "jf-1",
                    ServerType.JELLYFIN,
                    libraries=[Library("tv", "TV Shows", ("/media/tv",)), Library("sp", "Sports", ("/media/sp",))],
                ),
                "jf-2": server_config(
                    "jf-2",
                    ServerType.JELLYFIN,
                    markers={"enabled": True, "library_ids": ["kids"]},
                    libraries=[Library("films", "Films", ("/media/films",)), Library("kids", "Kids", ("/m/k",))],
                ),
            }
        )
        seen = {}

        def fake_enumerate(candidates, *, enumerate_one, cancel_check=None, label, progress_callback=None):
            processor = MagicMock()
            processor.list_canonical_paths.side_effect = lambda cfg, **kw: seen.setdefault(cfg.id, kw["library_ids"])
            for cfg in candidates:
                enumerate_one(processor, cfg)
            return [], []

        cfg = {
            "libraries": [
                {"server_id": "jf-1", "library_id": "tv"},
                {"server_id": "jf-1", "library_id": "sp"},
                {"server_id": "jf-1", "library_id": "gone"},
                {"server_id": "jf-2", "library_id": "films"},
            ]
        }
        with patch(
            "media_preview_generator.jobs.orchestrator._enumerate_items_for_servers", side_effect=fake_enumerate
        ):
            _items, warnings, _sent = job_runner.build_items(cfg, registry=reg)
        assert seen == {"jf-1": ["tv"]}
        assert warnings == [
            "Skipped Sports: Intro & Credits isn't on for it",
            "Skipped gone: Intro & Credits isn't on for it",
            "Skipped Films: Intro & Credits isn't on for it",
        ]

    def test_no_selection_enumerates_only_marker_libraries_on_servers_with_markers_on(self):
        reg = FakeRegistry(
            {
                "plex-1": server_config(
                    "plex-1",
                    ServerType.PLEX,
                    libraries=[
                        Library("1", "TV Shows", ("/media/tv",), enabled=False),
                        Library("2", "Sports", ("/media/sports",)),
                    ],
                ),
                "jf-1": server_config("jf-1", ServerType.JELLYFIN, markers={"enabled": False}),
                "emby-1": server_config("emby-1", ServerType.EMBY, enabled=False),
                "emby-2": server_config(
                    "emby-2", ServerType.EMBY, libraries=[Library("9", "Sports", ("/media/sports",))]
                ),
            }
        )
        seen = {}

        def fake_enumerate(candidates, *, enumerate_one, cancel_check=None, label, progress_callback=None):
            processor = MagicMock()
            processor.list_canonical_paths.side_effect = lambda cfg, **kw: seen.setdefault(cfg.id, kw["library_ids"])
            for cfg in candidates:
                enumerate_one(processor, cfg)
            return [], []

        with patch(
            "media_preview_generator.jobs.orchestrator._enumerate_items_for_servers", side_effect=fake_enumerate
        ) as enum:
            items, warnings, _sent = job_runner.build_items({}, registry=reg)
        assert [c.id for c in enum.call_args.args[0]] == ["plex-1"]
        assert seen == {"plex-1": ["1"]}
        assert items == [] and warnings == []

    @pytest.mark.parametrize(
        ("servers", "expected_warning"),
        [
            ({"jf-1": server_config("jf-1", ServerType.JELLYFIN, markers={"enabled": False})}, "turned off on JF-1"),
            ({"jf-1": server_config("jf-1", ServerType.JELLYFIN, enabled=False)}, "JF-1 is disabled"),
            ({}, "jf-1 is no longer configured"),
        ],
    )
    def test_selected_library_on_a_server_that_cant_take_markers_is_skipped_with_a_warning(
        self, servers, expected_warning
    ):
        with patch(
            "media_preview_generator.jobs.orchestrator._enumerate_items_for_servers", return_value=([], [])
        ) as enum:
            items, warnings, _sent = job_runner.build_items(
                {"libraries": [{"server_id": "jf-1", "library_id": "1"}]}, registry=FakeRegistry(servers)
            )
        assert enum.call_args.args[0] == []
        assert items == []
        assert len(warnings) == 1 and expected_warning in warnings[0]

    @pytest.mark.parametrize("previews_on", [True, False], ids=["previews-on", "previews-off"])
    @pytest.mark.parametrize("sender_view", [True, False], ids=["sender-path", "local-path"])
    def test_file_paths_resolve_to_the_local_file_whatever_the_library_preview_setting(
        self, tmp_path, previews_on, sender_view
    ):
        # Jellyfin "Anime": Intro & Credits on, previews on or off; Sonarr sends /data/..., the app reads <tmp>/media.
        from media_preview_generator.markers.ownership import marker_matches
        from media_preview_generator.servers.base import ServerConfig

        episode = tmp_path / "media" / "anime" / "Show" / "Show - S01E01.mkv"
        episode.parent.mkdir(parents=True)
        episode.write_bytes(b"x")
        cfg = ServerConfig(
            id="jf-1",
            type=ServerType.JELLYFIN,
            name="JF",
            enabled=True,
            url="http://jf",
            auth={},
            libraries=[Library("a", "Anime", ("/jfmedia/anime",), enabled=previews_on)],
            path_mappings=[
                {"remote_prefix": "/jfmedia", "local_prefix": str(tmp_path / "media"), "webhook_prefixes": ["/data"]}
            ],
            markers={"enabled": True, "library_ids": None},
        )
        reg = FakeRegistry({"jf-1": cfg})
        raw = "/data/anime/Show/Show - S01E01.mkv" if sender_view else str(episode)

        items, warnings, sent = job_runner.build_items({"file_paths": [raw]}, registry=reg)

        assert [i.canonical_path for i in items] == [str(episode)]
        assert sent == {str(episode): raw}
        assert list(marker_matches(items[0].canonical_path, reg.configs())) == ["jf-1"]
        assert warnings == []

    def test_enumerated_duplicates_keep_the_first_item(self):
        reg = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        first = ProcessableItem("/media/tv/S/a.mkv", "jf-1", {"jf-1": "x"})
        with patch(
            "media_preview_generator.jobs.orchestrator._enumerate_items_for_servers",
            return_value=([(None, first), (None, ProcessableItem("/media/tv/S/a.mkv", "jf-1"))], []),
        ):
            items, _, _sent = job_runner.build_items({}, registry=reg)
        assert items == [first]


def _item(path="/m/a.mkv"):
    return ProcessableItem(canonical_path=path, server_id="")


@pytest.fixture
def env(monkeypatch):
    jm = MagicMock()
    job = MagicMock(id="j1", kind=JOB_KIND_INTRO_CREDITS, priority=3, paused=False, config={"libraries": []})
    jm.get_job.return_value = job
    jm.is_cancellation_requested.return_value = False
    jm.is_pause_requested.return_value = False
    jm.get_file_results.return_value = []
    jm.get_running_jobs.return_value = []
    monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
    sm = MagicMock(processing_paused=False)
    sm.get.return_value = "INFO"
    monkeypatch.setattr(job_runner, "get_settings_manager", lambda: sm)
    gate = MagicMock()
    gate.acquire.return_value = True
    monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
    config = MagicMock(ffmpeg_path="/usr/bin/ffmpeg")
    monkeypatch.setattr(job_runner, "load_config", lambda: config)
    registry = MagicMock()
    monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda cfg: registry)
    gpus = [("nvidia", "/dev/nvidia0", {"workers": 1})]
    monkeypatch.setattr(job_runner, "_build_selected_gpus", lambda settings: gpus)
    ctx = MagicMock()
    ctx.take_budget_rechecks.return_value = ([], None)
    monkeypatch.setattr(job_runner, "build_context", MagicMock(return_value=ctx))
    handlers = MagicMock()
    kind_handlers = MagicMock(return_value=handlers)
    monkeypatch.setattr(job_runner, "kind_handlers", kind_handlers)
    dispatcher = MagicMock()
    tracker = MagicMock(priority=3)
    tracker.done_event = threading.Event()  # a MagicMock's is_set() is truthy and would fake "job finished"
    tracker.wait.return_value = True
    tracker.get_result.return_value = {
        "completed": 1,
        "failed": 0,
        "total": 1,
        "cancelled": False,
        "outcome": {"markers_published": 1},
    }
    dispatcher.submit_items.return_value = tracker
    get_dispatcher = MagicMock(return_value=dispatcher)
    monkeypatch.setattr(job_runner, "get_or_create_dispatcher", get_dispatcher)
    monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=MagicMock(side_effect=AssertionError("slept"))))
    return SimpleNamespace(
        jm=jm,
        job=job,
        sm=sm,
        gate=gate,
        config=config,
        registry=registry,
        gpus=gpus,
        ctx=ctx,
        handlers=handlers,
        kind_handlers=kind_handlers,
        dispatcher=dispatcher,
        tracker=tracker,
        get_dispatcher=get_dispatcher,
        build_context=job_runner.build_context,
    )


class TestRun:
    def _run(self, items=None, warnings=None):
        from media_preview_generator.markers import reconcile

        items = [_item()] if items is None else items
        listing = reconcile.CheckServersListing(list(items), list(warnings or []))
        with (
            patch.object(job_runner, "build_items", return_value=(items, warnings or [], {})) as build,
            patch.object(reconcile, "check_servers_listing", return_value=listing) as self.listing,
        ):
            job_runner.run_intro_credits_job("j1")
        return build

    def test_submits_items_with_kind_handlers_and_job_priority_then_completes(self, env):
        items = [_item("/m/a.mkv"), _item("/m/b.mkv")]
        build = self._run(items, ["Couldn't list X"])

        assert build.call_args.args == (env.job.config,)
        assert build.call_args.kwargs["registry"] is env.registry
        env.get_dispatcher.assert_called_once_with(env.config, env.gpus)
        kwargs = env.dispatcher.submit_items.call_args.kwargs
        assert kwargs["job_id"] == "j1" and kwargs["items"] == items
        assert kwargs["config"] is env.config and kwargs["registry"] is env.registry
        assert kwargs["kind"] == JOB_KIND_INTRO_CREDITS and kwargs["handlers"] is env.handlers
        assert kwargs["priority"] == 3
        assert kwargs["carried_outcome"] == {}
        assert set(kwargs["callbacks"]) == {"progress_callback", "worker_callback", "cancel_check", "pause_check"}
        env.kind_handlers.assert_called_once_with(env.ctx)
        ctx_kwargs = env.build_context.call_args.kwargs
        assert ctx_kwargs["registry"] is env.registry and ctx_kwargs["config"] is env.config
        assert ctx_kwargs["force"] is False
        env.gate.acquire.assert_called_once()
        assert env.gate.acquire.call_args.kwargs["priority"] == 3
        env.jm.start_job.assert_called_once_with("j1")
        env.jm.set_job_outcome.assert_called_once_with("j1", {"markers_published": 1})
        env.jm.complete_job.assert_called_once_with("j1", warning="Couldn't list X")
        env.gate.release.assert_called_once_with(3)

    def test_the_shared_worker_pool_is_the_running_jobs_pool(self, env):
        # POST /api/jobs/<id>/workers/add and /remove act on the running job's pool; with only Intro & Credits jobs
        # running nothing else registers one, and those routes would answer 409.
        order = []
        env.jm.set_active_worker_pool.side_effect = lambda job_id, pool: order.append(("pool", job_id, pool))
        env.dispatcher.submit_items.side_effect = lambda **kw: order.append("submit") or env.tracker
        self._run()
        assert order == [("pool", "j1", env.dispatcher.worker_pool), "submit"]

    @pytest.mark.parametrize(("force", "expected"), [(True, True), (False, False), (None, False), ("", False)])
    def test_force_from_job_config_reaches_the_pipeline_context(self, env, force, expected):
        env.job.config = {"libraries": [], "force": force}
        self._run()
        assert env.build_context.call_args.kwargs["force"] is expected

    def test_no_warnings_completes_cleanly(self, env):
        self._run()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize(
        ("config", "items", "swept"),
        [
            ({}, None, True),
            ({"reconcile": True}, None, True),
            ({}, [], True),
            ({"source": "season", "file_paths": ["/m/a.mkv"]}, None, False),
            ({"retry_attempt": 1, "file_paths": ["/m/a.mkv"]}, None, False),
            ({"verify": True, "file_paths": ["/m/a.mkv"]}, None, False),
        ],
        ids=["library", "check-servers", "no-files", "season", "retry", "verify"],
    )
    def test_a_job_sweeps_the_fingerprint_cache_when_it_ends_unless_it_follows_another(self, env, config, items, swept):
        env.job.config = {"libraries": [], **config}
        with patch.object(job_runner, "start_fingerprint_sweep", return_value=True) as sweep:
            self._run(items)
        assert sweep.call_args_list == ([call(env.ctx.store)] if swept else [])
        assert env.jm.complete_job.call_count == 1

    def test_the_cleanup_starts_after_the_slot_is_back_and_outside_the_jobs_log(self, env):
        from media_preview_generator.jobs.worker import is_job_thread_for

        seen = []
        sweep = MagicMock(
            side_effect=lambda store: seen.append(
                (env.gate.release.called, is_job_thread_for(threading.get_ident(), "j1"))
            )
        )
        with patch.object(job_runner, "start_fingerprint_sweep", sweep):
            self._run()
        assert seen == [(True, False)]

    def test_a_cancelled_job_starts_no_sweep(self, env):
        env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        with patch.object(job_runner, "start_fingerprint_sweep") as sweep:
            self._run()
        env.jm.cancel_job.assert_called_once_with("j1")
        sweep.assert_not_called()

    def test_a_sweep_that_cant_start_leaves_the_completed_job_alone(self, env):
        with patch.object(job_runner, "start_fingerprint_sweep", side_effect=RuntimeError("can't start new thread")):
            self._run()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)
        env.gate.release.assert_called_once_with(3)

    def test_a_sweep_blocked_on_the_file_system_holds_neither_the_job_nor_its_slot(self, env, tmp_path, monkeypatch):
        from media_preview_generator.markers.audio import fingerprint as fpmod
        from media_preview_generator.markers.models import FileIdentity
        from media_preview_generator.markers.store import MarkerStore

        monkeypatch.setattr(job_runner, "start_fingerprint_sweep", fpmod.start_fingerprint_sweep)
        monkeypatch.setattr(fpmod, "_sweep_started_at", None)
        monkeypatch.setattr(fpmod, "SWEEP_MIN_GAP_S", 0.0)  # only the running sweep can keep a second one out
        store = MarkerStore(str(tmp_path / "markers.db"))
        env.ctx.store = store
        media = tmp_path / "Season 01" / "S01E01.mkv"
        media.parent.mkdir()
        media.write_bytes(b"x")
        rec = store.upsert_file(FileIdentity(str(media), 1, 1), duration_ms=300_000, season_key=None, is_movie=False)
        store.set_fingerprint(rec.id, size=1, mtime_ns=1, window="intro", start_s=0.0, length_s=105.0, algorithm=1,
                              points=b"")  # fmt: skip
        entered, unblock, real_stat = threading.Event(), threading.Event(), os.stat

        def stat(path, *args, **kwargs):  # a hard-mounted share that stalls: no error, no answer
            if str(path) == str(media):
                entered.set()
                unblock.wait(10)
            return real_stat(path, *args, **kwargs)

        sweeping_at_release = []
        env.gate.release.side_effect = lambda priority: sweeping_at_release.append(fpmod._SWEEP_LOCK.locked())
        try:
            with (
                patch.object(fpmod.os, "stat", side_effect=stat),
                patch.object(store, "fingerprint_checks", wraps=store.fingerprint_checks) as listed,
            ):
                self._run()
                assert entered.wait(5) and fpmod._SWEEP_LOCK.locked()  # the sweep is stuck in os.stat; the job returned
                env.jm.complete_job.assert_called_once_with("j1", warning=None)
                assert sweeping_at_release == [False]  # the slot was given back before the sweep started
                self._run()  # the next job completes too, and starts no second sweep
                assert env.jm.complete_job.call_count == 2 and sweeping_at_release == [False, True]
                assert listed.call_count == 1
                unblock.set()
                assert fpmod._SWEEP_LOCK.acquire(timeout=5)
                fpmod._SWEEP_LOCK.release()
        finally:
            unblock.set()
            store.close()

    @pytest.mark.parametrize(
        ("outcome", "warnings", "expected"),
        [
            ({"markers_published": 2, "failed": 0}, [], {"warning": None}),
            ({"markers_published": 2, "failed": 0}, ["Couldn't list X"], {"warning": "Couldn't list X"}),
            ({"markers_published": 2, "markers_none": 1, "failed": 1}, [], {"warning": "1 file(s) failed"}),
            (
                {"markers_up_to_date": 1, "failed": 2},
                ["Couldn't list X"],
                {"warning": "2 file(s) failed | Couldn't list X"},
            ),
            ({"markers_published": 0, "failed": 3}, [], {"error": "All 3 file(s) failed — see the Files panel"}),
            (
                {"markers_published": 0, "failed": 3},
                ["Couldn't list X"],
                {"error": "All 3 file(s) failed — see the Files panel | Couldn't list X"},
            ),
            ({"markers_published": 0, "failed": 0}, [], {"warning": None}),
        ],
        ids=[
            "none-failed",
            "none-failed-warnings",
            "some-failed",
            "some-failed-warnings",
            "all-failed",
            "all-failed-warnings",
            "nothing-counted",
        ],
    )
    def test_completion_is_red_when_every_file_failed_and_amber_when_some_did(self, env, outcome, warnings, expected):
        env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "outcome": outcome}
        self._run([_item("/m/a.mkv"), _item("/m/b.mkv"), _item("/m/c.mkv")], warnings)
        env.jm.complete_job.assert_called_once_with("j1", **expected)
        env.jm.set_job_outcome.assert_called_once_with("j1", outcome)

    def test_pipeline_priority_follows_live_priority_changes(self, env):
        self._run()
        priority = env.build_context.call_args.kwargs["priority"]
        assert callable(priority) and priority() == 3
        env.job.priority = 1  # the user raises the running job to High
        assert priority() == 1

    def test_priority_changed_while_submitting_is_applied_to_the_tracker(self, env):
        def submit(**kwargs):
            env.job.priority = 1  # route updates the job before this tracker is registered
            return env.tracker

        env.dispatcher.submit_items.side_effect = submit
        self._run()
        env.dispatcher.update_job_priority.assert_called_once_with("j1", 1)

    def test_unchanged_priority_is_not_pushed_again(self, env):
        self._run()
        env.dispatcher.update_job_priority.assert_not_called()

    def test_gate_is_acquired_at_the_priority_in_force_when_the_job_starts(self, env):
        env.job.priority = 1
        self._run()
        assert env.gate.acquire.call_args.kwargs["priority"] == 1
        env.gate.release.assert_called_once_with(1)
        assert env.dispatcher.submit_items.call_args.kwargs["priority"] == 1

    def test_pause_check_honours_slot_job_and_global_pause(self, env):
        seen = {}

        def during_wait(timeout=None):
            pause_check = env.dispatcher.submit_items.call_args.kwargs["callbacks"]["pause_check"]
            for job_flag, global_flag in ((False, False), (True, False), (False, True), (True, True)):
                env.jm.is_pause_requested.return_value = job_flag
                env.sm.processing_paused = global_flag
                seen[(job_flag, global_flag)] = pause_check()
            env.jm.is_pause_requested.return_value = False
            env.sm.processing_paused = False
            return True

        env.tracker.wait.side_effect = during_wait
        self._run()
        assert seen == {(False, False): False, (True, False): True, (False, True): True, (True, True): True}

    def test_callbacks_report_progress_and_workers_to_the_job(self, env):
        def during_wait(timeout=None):
            callbacks = env.dispatcher.submit_items.call_args.kwargs["callbacks"]
            callbacks["progress_callback"](1, 4, "Looking up markers…")
            callbacks["worker_callback"](
                [
                    {
                        "worker_id": 0,
                        "worker_type": "GPU",
                        "worker_name": "GPU 0",
                        "status": "busy",
                        "current_title": "Intro & Credits · a.mkv",
                        "remaining_time": 90,
                        "current_phase": "Reading chapters…",
                    }
                ]
            )
            assert callbacks["cancel_check"]() is False
            return True

        env.tracker.wait.side_effect = during_wait
        self._run()
        env.jm.update_progress.assert_any_call(
            "j1", percent=25.0, processed_items=1, total_items=4, current_item="Looking up markers…"
        )
        key, status = env.jm.update_worker_status.call_args.args
        assert key == "GPU_0" and status.current_phase == "Reading chapters…" and status.eta
        env.jm.prune_worker_statuses.assert_called_once_with({"GPU_0"})
        env.jm.emit_worker_statuses.assert_called_once()

    def test_file_results_are_recorded_on_this_job(self, env):
        with patch.object(job_runner, "set_file_result_callback") as set_cb:
            self._run()
        callback = set_cb.call_args_list[0].args[0]
        assert set_cb.call_args_list[0].kwargs == {"job_id": "j1"}
        callback("/m/a.mkv", "markers_published", "1 marker(s)", "Lookup", servers=[{"id": "jf-1"}])
        env.jm.record_file_result.assert_called_once_with(
            "j1",
            "/m/a.mkv",
            "markers_published",
            "1 marker(s)",
            "Lookup",
            servers=[{"id": "jf-1"}],
            server_messages=True,
        )

    def test_each_file_result_updates_the_jobs_decided_by_counts(self, env):
        env.ctx.decided_by = DecidedByTally()
        with patch.object(job_runner, "set_file_result_callback") as set_cb:
            self._run()
        callback = set_cb.call_args_list[0].args[0]
        # The pipeline counts a file before its result reaches the job.
        env.ctx.decided_by.add({MarkerType.CREDITS: "credits_text"})
        env.jm.set_marker_sources.reset_mock()

        callback("/m/a.mkv", "markers_published", "", "Lookup", servers=[])

        env.jm.set_marker_sources.assert_called_once_with("j1", {"credits": {"credits_text": 1}})

    def test_the_finished_job_stores_the_final_counts_after_every_file(self, env):
        # Worker threads store their snapshots in any order, so the last per-file one may be stale.
        env.ctx.decided_by = DecidedByTally()
        env.tracker.get_result.side_effect = lambda: (
            env.ctx.decided_by.add({MarkerType.INTRO: "chapters"}),
            {"completed": 1, "failed": 0, "total": 1, "cancelled": False, "outcome": {"markers_published": 1}},
        )[1]

        self._run()

        assert env.jm.set_marker_sources.call_args_list[-1] == call("j1", {"intro": {"chapters": 1}})
        stored_at = env.jm.method_calls.index(call.set_marker_sources("j1", {"intro": {"chapters": 1}}))
        completed_at = next(i for i, c in enumerate(env.jm.method_calls) if c[0] == "complete_job")
        assert stored_at < completed_at

    def test_paused_job_hands_back_its_slot_so_a_high_preview_job_is_admitted(self, env, monkeypatch):
        from media_preview_generator.web.job_gate import JobGate

        gate = JobGate(lambda: 1)
        monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
        state = {"paused": False}
        env.jm.is_pause_requested.side_effect = lambda jid: state["paused"]
        steps = []

        def during_wait(timeout=None):
            pause_check = env.dispatcher.submit_items.call_args.kwargs["callbacks"]["pause_check"]
            step = len(steps)
            steps.append(step)
            if step == 0:
                assert gate.snapshot()[0] == 1 and pause_check() is False
                state["paused"] = True
                return False
            if step == 1:
                # Slot handed back: a HIGH preview job gets in at cap 1, and nothing of ours is dispatched.
                assert gate.snapshot()[0] == 0 and pause_check() is True
                assert gate.acquire(1, cancel_check=lambda: True) is True
                gate.release(1)
                state["paused"] = False
                return False
            # Resumed: slot re-acquired before dispatch continues.
            assert gate.snapshot()[0] == 1 and pause_check() is False
            return True

        env.tracker.wait.side_effect = during_wait
        self._run()
        assert steps == [0, 1, 2]
        assert gate.snapshot()[0] == 0
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def test_slot_is_retaken_at_the_priority_in_force_on_resume_and_released_at_it(self, env):
        state = {"paused": False}
        env.jm.is_pause_requested.side_effect = lambda jid: state["paused"]
        calls = []
        seen_while_readmitting = []

        def during_wait(timeout=None):
            calls.append(len(calls))
            if len(calls) == 1:
                state["paused"] = True
                return False
            if len(calls) == 2:
                return False  # still paused on the next tick: the slot was already handed back
            if len(calls) == 3:
                env.job.priority = 1
                state["paused"] = False
                return False
            return True

        def acquire(priority, cancel_check, on_wait=None):
            pause_check = env.dispatcher.submit_items.call_args
            if pause_check is not None:
                # Resumed but not yet re-admitted: nothing of this job may be dispatched.
                seen_while_readmitting.append(pause_check.kwargs["callbacks"]["pause_check"]())
            return True

        env.gate.acquire.side_effect = acquire
        env.tracker.wait.side_effect = during_wait
        self._run()
        assert [c.kwargs["priority"] for c in env.gate.acquire.call_args_list] == [3, 1]
        assert [c.args for c in env.gate.release.call_args_list] == [(3,), (1,)]
        assert seen_while_readmitting == [True]

    def test_job_that_finished_while_paused_does_not_queue_for_a_slot(self, env, monkeypatch):
        from media_preview_generator.web.job_gate import JobGate

        gate = JobGate(lambda: 1)
        monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
        state = {"paused": False}
        env.jm.is_pause_requested.side_effect = lambda jid: state["paused"]
        calls = []

        def during_wait(timeout=None):
            calls.append(len(calls))
            if len(calls) == 1:
                state["paused"] = True
                return False
            if len(calls) == 2:
                # Slot handed back; a long scan takes it, the last in-flight item finishes, the user resumes.
                assert gate.acquire(2, cancel_check=lambda: True) is True
                env.tracker.done_event.set()
                state["paused"] = False
                return False
            return True

        env.tracker.wait.side_effect = during_wait
        self._run()
        assert calls == [0, 1, 2]
        assert gate.snapshot()[0] == 1  # only the scan's slot; ours was never re-taken
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def test_cancel_while_paused_waits_for_the_dispatcher_without_retaking_a_slot(self, env):
        state = {"paused": False}
        env.jm.is_pause_requested.side_effect = lambda jid: state["paused"]
        calls = []

        def during_wait(timeout=None):
            calls.append(len(calls))
            if len(calls) == 1:
                state["paused"] = True
                return False
            if len(calls) == 2:
                # Cancelling clears the job's pause flag (JobManager.cancel_job).
                env.jm.is_cancellation_requested.return_value = True
                state["paused"] = False
                return False
            env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
            return True

        env.tracker.wait.side_effect = during_wait
        self._run()
        env.gate.acquire.assert_called_once()
        env.gate.release.assert_called_once_with(3)
        env.jm.complete_job.assert_not_called()

    @pytest.mark.parametrize("preview_priority", [1, 2, 3])
    def test_follow_up_waits_for_its_preview_job_whatever_its_priority(self, env, monkeypatch, preview_priority):
        from media_preview_generator.web.jobs import JobStatus

        env.job.config = {"libraries": [], "follows_job_id": "prev-1"}
        preview = MagicMock(
            id="prev-1", priority=preview_priority, status=JobStatus.RUNNING, progress=SimpleNamespace(retry_eta=None)
        )
        env.jm.get_job.side_effect = lambda jid: preview if jid == "prev-1" else env.job
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            env.gate.acquire.assert_not_called()
            if len(sleeps) == 2:
                preview.status = JobStatus.COMPLETED

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        self._run()
        assert len(sleeps) == 2
        env.gate.acquire.assert_called_once()
        env.dispatcher.submit_items.assert_called_once()
        first_progress = env.jm.update_progress.call_args_list[0].kwargs["current_item"]
        assert "preview job" in first_progress
        assert env.jm.update_progress.call_count >= 1

    def test_priority_changed_while_waiting_for_the_preview_job_is_used_for_the_slot(self, env, monkeypatch):
        from media_preview_generator.web.jobs import JobStatus

        env.job.config = {"follows_job_id": "prev-1"}
        preview = MagicMock(status=JobStatus.RUNNING, progress=SimpleNamespace(retry_eta=None))
        env.jm.get_job.side_effect = lambda jid: preview if jid == "prev-1" else env.job

        def fake_sleep(_seconds):
            env.job.priority = 1
            preview.status = JobStatus.COMPLETED

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        self._run()
        assert env.gate.acquire.call_args.kwargs["priority"] == 1
        env.gate.release.assert_called_once_with(1)

    @pytest.mark.parametrize("finished", ["completed", "failed", "cancelled"])
    def test_follow_up_starts_once_its_preview_job_has_ended_any_way(self, env, finished):
        from media_preview_generator.web.jobs import JobStatus

        env.job.config = {"follows_job_id": "prev-1"}
        preview = MagicMock(status=JobStatus(finished), progress=SimpleNamespace(retry_eta=None))
        env.jm.get_job.side_effect = lambda jid: preview if jid == "prev-1" else env.job
        self._run()
        env.dispatcher.submit_items.assert_called_once()

    def test_follow_up_cancelled_while_waiting_never_takes_a_slot(self, env, monkeypatch):
        from media_preview_generator.web.jobs import JobStatus

        env.job.config = {"follows_job_id": "prev-1"}
        preview = MagicMock(status=JobStatus.PENDING, progress=SimpleNamespace(retry_eta=None))
        env.jm.get_job.side_effect = lambda jid: preview if jid == "prev-1" else env.job

        def fake_sleep(_seconds):
            env.jm.is_cancellation_requested.return_value = True

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        job_runner.run_intro_credits_job("j1")
        env.jm.cancel_job.assert_called_once_with("j1")
        env.gate.acquire.assert_not_called()
        env.gate.release.assert_not_called()
        env.dispatcher.submit_items.assert_not_called()

    def test_follow_up_does_not_wait_through_a_preview_retry_countdown(self, env):
        from media_preview_generator.web.jobs import JobStatus

        env.job.config = {"follows_job_id": "prev-1"}
        preview = MagicMock(status=JobStatus.PENDING, progress=SimpleNamespace(retry_eta="2026-09-13T10:00:00+00:00"))
        env.jm.get_job.side_effect = lambda jid: preview if jid == "prev-1" else env.job
        self._run()
        env.dispatcher.submit_items.assert_called_once()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    @pytest.fixture
    def pending_preview(self, env, monkeypatch):
        """A PENDING preview job the follow-up waits for; ``state`` drives its thread and the global pause per poll."""
        from media_preview_generator.web.jobs import JobStatus

        env.job.config = {"follows_job_id": "prev-1"}
        preview = MagicMock(status=JobStatus.PENDING, progress=SimpleNamespace(retry_eta=None))
        env.jm.get_job.side_effect = lambda jid: preview if jid == "prev-1" else env.job
        state = {"sleeps": 0, "script": lambda n: None}

        def fake_sleep(_seconds):
            state["sleeps"] += 1
            if state["sleeps"] > 1000:
                raise AssertionError("still waiting for the preview job")
            env.gate.acquire.assert_not_called()
            state["script"](state["sleeps"])

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        yield SimpleNamespace(preview=preview, state=state)
        with job_runner._inflight_lock:
            job_runner._inflight_jobs.discard("prev-1")

    def test_follow_up_of_a_pending_preview_job_no_thread_will_run_starts_after_a_grace_period(
        self, env, pending_preview
    ):
        # Too old to be revived after a restart: nothing will ever finish it.
        self._run()
        assert pending_preview.state["sleeps"] == job_runner._ORPHAN_GRACE_POLLS
        env.dispatcher.submit_items.assert_called_once()
        assert any("isn't queued to run" in c.args[1] for c in env.jm.add_log.call_args_list)

    def test_follow_up_keeps_waiting_for_a_pending_preview_job_that_has_a_thread(self, env, pending_preview):
        from media_preview_generator.web.jobs import JobStatus

        with job_runner._inflight_lock:
            job_runner._inflight_jobs.add("prev-1")  # waiting for a slot
        limit = job_runner._ORPHAN_GRACE_POLLS * 3

        def script(n):
            if n == limit:
                pending_preview.preview.status = JobStatus.COMPLETED

        pending_preview.state["script"] = script
        self._run()
        assert pending_preview.state["sleeps"] == limit
        assert not any("isn't queued to run" in c.args[1] for c in env.jm.add_log.call_args_list)

    def test_grace_period_restarts_when_the_preview_job_gets_a_thread_again(self, env, pending_preview):
        grace = job_runner._ORPHAN_GRACE_POLLS

        def script(n):
            with job_runner._inflight_lock:
                if n == grace - 1:
                    job_runner._inflight_jobs.add("prev-1")  # the pending drain started it
                elif n == grace:
                    job_runner._inflight_jobs.discard("prev-1")  # ...and it went back to pending

        pending_preview.state["script"] = script
        self._run()
        assert pending_preview.state["sleeps"] == 2 * grace
        env.dispatcher.submit_items.assert_called_once()

    def test_follow_up_waits_through_a_global_pause_for_a_pending_preview_job(self, env, pending_preview):
        from media_preview_generator.web.jobs import JobStatus

        # Paused processing leaves new jobs pending without a thread; resuming starts them in order.
        env.sm.processing_paused = False
        limit = job_runner._ORPHAN_GRACE_POLLS * 3

        def script(n):
            env.sm.processing_paused = n < limit
            if n == limit:
                pending_preview.preview.status = JobStatus.COMPLETED

        pending_preview.state["script"] = script
        self._run()
        assert pending_preview.state["sleeps"] == limit
        assert not any("isn't queued to run" in c.args[1] for c in env.jm.add_log.call_args_list)

    def test_follow_up_of_a_deleted_preview_job_starts_straight_away(self, env):
        env.job.config = {"follows_job_id": "gone"}
        env.jm.get_job.side_effect = lambda jid: None if jid == "gone" else env.job
        self._run()
        env.dispatcher.submit_items.assert_called_once()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize("others_running", [True, False])
    def test_teardown_clears_per_job_state(self, env, others_running):
        env.jm.get_running_jobs.return_value = [MagicMock()] if others_running else []
        with (
            patch.object(job_runner, "set_file_result_callback") as set_cb,
            patch.object(job_runner, "clear_failures") as clear_failures,
        ):
            self._run()
        env.jm.clear_pause_flag.assert_called_once_with("j1")
        env.jm.clear_cancellation_flag.assert_called_once_with("j1")
        env.jm.clear_active_worker_pool.assert_called_once_with("j1")
        assert set_cb.call_args_list[-1].args == (None,) and set_cb.call_args_list[-1].kwargs == {"job_id": "j1"}
        clear_failures.assert_called_once()
        assert env.jm.clear_worker_statuses.called is (not others_running)

    def test_job_failure_bucket_is_cleared_when_the_job_ends(self, env):
        from media_preview_generator.processing.generator import failure_scope, get_failures, record_failure

        with failure_scope("j1"):
            record_failure("/m/a.mkv", 1, "left over from this job")
        self._run()
        with failure_scope("j1"):
            assert get_failures() == []

    def test_teardown_unregisters_the_job_thread(self, env):
        with (
            patch.object(job_runner, "register_job_thread") as register,
            patch.object(job_runner, "unregister_job_thread") as unregister,
        ):
            self._run()
        register.assert_called_once_with("j1")
        unregister.assert_called_once_with()

    @pytest.mark.parametrize("warnings", [[], ["Couldn't list JF-1: TimeoutError: slow"]])
    def test_nothing_to_do_completes_with_warning_and_never_submits(self, env, warnings):
        self._run([], warnings)
        env.dispatcher.submit_items.assert_not_called()
        warning = env.jm.complete_job.call_args.kwargs["warning"]
        assert "no files" in warning.lower()
        assert all(w in warning for w in warnings)
        env.gate.release.assert_called_once_with(3)

    @pytest.mark.parametrize(
        ("warnings", "expected"),
        [([], None), (["Skipped PLEX-1: Plex is stopped", "Couldn't read what 2 item(s) show on JF-1"],
                      "Skipped PLEX-1: Plex is stopped | Couldn't read what 2 item(s) show on JF-1")],
        ids=["clean", "with-warnings"],
    )  # fmt: skip
    def test_check_servers_with_nothing_drifted_completes_at_once_with_a_log_line(self, env, warnings, expected):
        env.job.config = {"reconcile": True, "source": "reconcile"}
        self._run([], warnings)
        env.dispatcher.submit_items.assert_not_called()
        env.jm.add_log.assert_any_call("j1", "INFO - Every server checked still shows what this app published")
        env.jm.complete_job.assert_called_once_with("j1", warning=expected)

    @pytest.mark.parametrize(("config", "recheck"), [({"reconcile": True}, True), ({"libraries": []}, False)])
    def test_only_check_servers_asks_servers_again_for_their_own_markers(self, env, config, recheck):
        env.job.config = config
        build = self._run()
        assert env.build_context.call_args.kwargs["recheck_empty_server_markers"] is recheck
        assert (self.listing.called, build.called) == (recheck, not recheck)

    def test_check_servers_keeps_its_listing_on_the_job_for_a_run_revived_after_a_restart(self, env):
        from media_preview_generator.markers import reconcile

        env.job.config = {"reconcile": True, "source": "reconcile"}
        self._run([_item("/m/a.mkv"), _item("/m/b.mkv")], ["Couldn't check X"])
        # Kept whole while the job runs, so a revived run checks these files without reading every server back again.
        # Taken off again once the job has ended (only a revive needs it).
        order = [c for c in env.jm.mock_calls if c[0] in ("merge_job_config", "complete_job")]
        assert order == [
            call.merge_job_config(
                "j1",
                {
                    reconcile.LISTING_CONFIG_KEY: {
                        "files": ["/m/a.mkv", "/m/b.mkv"],
                        "warnings": ["Couldn't check X"],
                        "drifted": {},
                        "rechecks": {},
                        "retries": {},
                        "listed_at": None,
                    }
                },
            ),
            call.complete_job("j1", warning="Couldn't check X"),
            call.merge_job_config("j1", {}, remove=(reconcile.LISTING_CONFIG_KEY,)),
        ]

    def test_a_revived_check_servers_job_checks_the_files_its_first_run_listed(self, env):
        from media_preview_generator.markers import reconcile

        stored = reconcile.CheckServersListing(
            [_item("/m/a.mkv"), _item("/m/b.mkv")], ["Couldn't check X"], {"/m/a.mkv": frozenset({("jf-1", "x")})}
        ).to_config()
        env.job.config = {"reconcile": True, "source": "reconcile", reconcile.LISTING_CONFIG_KEY: stored}
        with (
            patch.object(job_runner, "build_items") as build,
            patch.object(reconcile, "check_servers_listing") as listing,
        ):
            job_runner.run_intro_credits_job("j1")
        listing.assert_not_called()
        build.assert_not_called()
        env.jm.merge_job_config.assert_called_once_with("j1", {}, remove=(reconcile.LISTING_CONFIG_KEY,))
        submitted = env.dispatcher.submit_items.call_args.kwargs["items"]
        assert [(i.canonical_path, i.server_id, i.item_id_by_server, i.title) for i in submitted] == [
            ("/m/a.mkv", "", {}, "a.mkv"),
            ("/m/b.mkv", "", {}, "b.mkv"),
        ]
        env.jm.complete_job.assert_called_once_with("j1", warning="Couldn't check X")

    @pytest.mark.parametrize("ends", ["cancelled-waiting-for-a-slot", "servers-config-unloadable"])
    def test_a_revived_check_servers_job_that_ends_before_its_listing_is_read_drops_it(self, env, monkeypatch, ends):
        from media_preview_generator.markers import reconcile

        stored = reconcile.CheckServersListing([_item("/m/a.mkv")], []).to_config()
        env.job.config = {"reconcile": True, "source": "reconcile", reconcile.LISTING_CONFIG_KEY: stored}
        if ends == "cancelled-waiting-for-a-slot":
            env.gate.acquire.return_value = False
        else:
            monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda cfg: None)
        self._run()
        env.dispatcher.submit_items.assert_not_called()
        env.jm.merge_job_config.assert_called_once_with("j1", {}, remove=(reconcile.LISTING_CONFIG_KEY,))

    def test_a_revived_check_servers_job_whose_listing_is_unusable_lists_again_and_says_so(self, env):
        from loguru import logger

        from media_preview_generator.markers import reconcile

        env.job.config = {"reconcile": True, "source": "reconcile", reconcile.LISTING_CONFIG_KEY: {"files": "?"}}
        lines: list[str] = []
        handler = logger.add(lambda m: lines.append(m), level="WARNING", format="{message}")
        try:
            self._run([_item("/m/b.mkv")])
        finally:
            logger.remove(handler)
        assert self.listing.called
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == [_item("/m/b.mkv")]
        assert any("couldn't read the files" in line.lower() and "listing them again" in line for line in lines)
        # The new listing replaces it, and goes when the job ends.
        empty = {"drifted": {}, "rechecks": {}, "retries": {}, "listed_at": None}
        assert [c for c in env.jm.mock_calls if c[0] == "merge_job_config"] == [
            call.merge_job_config(
                "j1", {reconcile.LISTING_CONFIG_KEY: {"files": ["/m/b.mkv"], "warnings": [], **empty}}
            ),
            call.merge_job_config("j1", {}, remove=(reconcile.LISTING_CONFIG_KEY,)),
        ]

    def test_a_check_servers_run_cancelled_during_its_read_back_keeps_no_listing(self, env):
        env.job.config = {"reconcile": True, "source": "reconcile"}
        env.jm.is_cancellation_requested.return_value = True
        self._run()
        assert self.listing.called  # the read-back ran and was cut short
        env.jm.merge_job_config.assert_not_called()
        env.jm.cancel_job.assert_called_once_with("j1")

    def test_check_servers_lists_its_files_with_the_jobs_store_capability_cache_and_cancel(self, env, monkeypatch):
        env.job.config = {"reconcile": True, "source": "reconcile"}
        self._run()
        kwargs = self.listing.call_args.kwargs
        assert set(kwargs) == {"registry", "store", "max_files", "capability", "cancel_check", "progress_callback"}
        assert (kwargs["registry"], kwargs["store"], kwargs["max_files"]) == (env.registry, env.ctx.store, 500)
        cached = MagicMock(return_value="report")
        monkeypatch.setattr(job_runner, "cached_capability", cached)
        assert kwargs["capability"]("cfg", "pub") == "report"
        cached.assert_called_once_with(env.ctx, "cfg", "pub")
        assert kwargs["cancel_check"]() is False
        env.jm.is_cancellation_requested.return_value = True
        assert kwargs["cancel_check"]() is True

    def test_a_pause_during_the_read_back_hands_the_slot_back_until_resume(self, env, monkeypatch):
        env.job.config = {"reconcile": True, "source": "reconcile"}
        env.job.priority = 2
        paused = iter([True, True, False])
        env.jm.is_pause_requested.side_effect = lambda job_id: next(paused, False)
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            env.job.priority = 1  # raised while paused: the slot taken on resume is a HIGH one

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=sleep))
        seen = []

        def listing(**kwargs):
            seen.append((kwargs["cancel_check"](), env.gate.release.call_args_list[:], env.gate.acquire.call_count))
            from media_preview_generator.markers import reconcile

            return reconcile.CheckServersListing([], [])

        from media_preview_generator.markers import reconcile

        with patch.object(reconcile, "check_servers_listing", side_effect=listing):
            job_runner.run_intro_credits_job("j1")
        [(cancelled, released, acquired)] = seen
        assert cancelled is False
        assert released == [call(2)]  # handed back while paused
        assert acquired == 2  # the job's first slot, then again on resume
        assert env.gate.acquire.call_args_list[1].kwargs["priority"] == 1
        assert len(sleeps) == 2
        assert env.gate.release.call_args_list == [call(2), call(1)]  # the job's end gives back the slot it holds
        env.jm.add_log.assert_any_call("j1", "INFO - Paused; active slot handed back until resume")

    def test_all_processing_paused_waits_during_the_read_back_keeping_the_slot(self, env, monkeypatch):
        env.job.config = {"reconcile": True, "source": "reconcile"}
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            env.sm.processing_paused = False  # resumed

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=sleep))
        from media_preview_generator.markers import reconcile

        answers = []

        def listing(**kwargs):
            env.sm.processing_paused = True
            answers.append((kwargs["cancel_check"](), env.gate.release.called))
            return reconcile.CheckServersListing([], [])

        with patch.object(reconcile, "check_servers_listing", side_effect=listing):
            job_runner.run_intro_credits_job("j1")
        assert answers == [(False, False)]  # waited, slot kept (every job is paused)
        assert len(sleeps) == 1

    def test_a_cancel_while_paused_during_the_read_back_ends_the_wait(self, env, monkeypatch):
        env.job.config = {"reconcile": True, "source": "reconcile"}
        env.jm.is_pause_requested.return_value = True
        cancels = iter([False, False, True])
        env.jm.is_cancellation_requested.side_effect = lambda job_id: next(cancels, True)
        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=lambda s: None))
        from media_preview_generator.markers import reconcile

        answers = []

        def listing(**kwargs):
            answers.append(kwargs["cancel_check"]())
            return reconcile.CheckServersListing([], [])

        with patch.object(reconcile, "check_servers_listing", side_effect=listing):
            job_runner.run_intro_credits_job("j1")
        assert answers == [True]
        env.jm.cancel_job.assert_called_once_with("j1")
        assert env.gate.release.call_args_list == [call(3)]  # handed back once while paused; not released again
        assert env.gate.acquire.call_count == 1  # never taken again: the cancel came first

    def test_a_pause_while_waiting_for_a_slot_after_resume_keeps_the_job_waiting_without_one(self, env, monkeypatch):
        from media_preview_generator.markers import reconcile

        env.job.config = {"reconcile": True, "source": "reconcile"}
        state = {"paused": True}
        env.jm.is_pause_requested.side_effect = lambda job_id: state["paused"]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            state["paused"] = False  # resumed

        def acquire(priority, cancel_check, on_wait=None):
            if env.gate.acquire.call_count == 2:  # the slot asked for on resume
                state["paused"] = True  # paused again while waiting for it
                assert cancel_check() is True
                return False
            return True

        env.gate.acquire.side_effect = acquire
        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=sleep))
        seen = []

        def listing(**kwargs):
            seen.append((kwargs["cancel_check"](), env.gate.release.call_args_list[:], env.gate.acquire.call_count))
            return reconcile.CheckServersListing([], [])

        with patch.object(reconcile, "check_servers_listing", side_effect=listing):
            job_runner.run_intro_credits_job("j1")
        [(cancelled, released, acquired)] = seen
        assert cancelled is False
        # Paused: handed back once. The acquire interrupted by the second pause took no slot, so there was nothing to
        # hand back again; the job waited out that pause and took a slot on the next resume.
        assert released == [call(3)]
        assert acquired == 3
        assert len(sleeps) == 2
        assert env.gate.release.call_args_list == [call(3), call(3)]

    def test_cancel_during_enumeration_cancels_without_submitting(self, env):
        def build(cfg, **kwargs):
            env.jm.is_cancellation_requested.return_value = True
            return [_item()], [], {}

        with patch.object(job_runner, "build_items", side_effect=build):
            job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.cancel_job.assert_called_once_with("j1")
        env.jm.complete_job.assert_not_called()

    def test_missing_job_does_nothing(self, env):
        env.jm.get_job.return_value = None
        job_runner.run_intro_credits_job("j1")
        env.gate.acquire.assert_not_called()
        env.jm.complete_job.assert_not_called()

    def test_paused_processing_leaves_job_pending(self, env):
        env.sm.processing_paused = True
        job_runner.run_intro_credits_job("j1")
        env.gate.acquire.assert_not_called()
        env.jm.start_job.assert_not_called()
        env.jm.clear_pause_flag.assert_not_called()

    def test_cancel_while_waiting_for_gate(self, env):
        env.gate.acquire.return_value = False
        job_runner.run_intro_credits_job("j1")
        env.jm.cancel_job.assert_called_once_with("j1")
        env.jm.start_job.assert_not_called()
        env.gate.release.assert_not_called()

    def test_gate_cancel_check_reads_this_jobs_cancel_flag(self, env):
        def acquire(priority, cancel_check, on_wait=None):
            on_wait(3, 3, 2)
            env.jm.is_cancellation_requested.side_effect = lambda jid: jid == "j1"
            return not cancel_check()

        env.gate.acquire.side_effect = acquire
        job_runner.run_intro_credits_job("j1")
        env.jm.update_progress.assert_any_call(
            "j1",
            percent=0,
            processed_items=0,
            total_items=0,
            current_item="Queued — waiting for active slot (3 of 3 busy)",
        )
        env.jm.cancel_job.assert_called_once_with("j1")

    @pytest.mark.parametrize(("tracker_cancelled", "cancel_requested"), [(True, True), (True, False), (False, True)])
    def test_cancelled_job_is_not_marked_complete(self, env, tracker_cancelled, cancel_requested):
        def during_wait(timeout=None):
            # (False, True): the user cancelled as the last item finished.
            env.jm.is_cancellation_requested.return_value = cancel_requested
            env.tracker.get_result.return_value = {
                **env.tracker.get_result.return_value,
                "cancelled": tracker_cancelled,
            }
            return True

        env.tracker.wait.side_effect = during_wait
        self._run()
        env.jm.complete_job.assert_not_called()
        env.jm.cancel_job.assert_called_once_with("j1")
        env.jm.set_job_outcome.assert_called_once_with("j1", {"markers_published": 1})
        env.gate.release.assert_called_once_with(3)

    def test_registry_unavailable_fails_the_job(self, env, monkeypatch):
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda cfg: None)
        self._run()
        assert "media servers" in env.jm.complete_job.call_args.kwargs["error"]
        env.dispatcher.submit_items.assert_not_called()
        env.gate.release.assert_called_once_with(3)

    def test_crash_marks_job_failed_and_releases_gate(self, env):
        with patch.object(job_runner, "build_items", side_effect=RuntimeError("enumeration exploded")):
            job_runner.run_intro_credits_job("j1")
        env.jm.complete_job.assert_called_once_with("j1", error="RuntimeError: enumeration exploded")
        env.gate.release.assert_called_once_with(3)
        env.jm.clear_pause_flag.assert_called_once_with("j1")
        env.dispatcher.cancel_job.assert_not_called()  # nothing was submitted

    def test_crash_after_submitting_stops_the_jobs_remaining_checks(self, env):
        # Without this the tracker keeps publishing files for a job already marked failed, with no slot held.
        order = []
        env.tracker.wait.side_effect = RuntimeError("wait exploded")
        env.dispatcher.cancel_job.side_effect = lambda jid: order.append(("cancel", jid))
        env.jm.complete_job.side_effect = lambda jid, **kw: order.append(("complete", jid, kw))
        self._run()
        assert order == [("cancel", "j1"), ("complete", "j1", {"error": "RuntimeError: wait exploded"})]
        env.gate.release.assert_called_once_with(3)

    def test_crash_while_stopping_the_tracker_still_marks_the_job_failed(self, env):
        env.tracker.wait.side_effect = RuntimeError("wait exploded")
        env.dispatcher.cancel_job.side_effect = RuntimeError("dispatcher gone")
        self._run()
        env.jm.complete_job.assert_called_once_with("j1", error="RuntimeError: wait exploded")

    def test_a_crash_whose_text_carries_a_token_leaks_it_neither_to_the_job_nor_to_the_log(self, env):
        from loguru import logger

        url = "http://plex:32400/library/sections/1/all?X-Plex-Token=s3cr3t-t0ken&type=4"
        lines: list[str] = []
        handler = logger.add(lambda m: lines.append(m), level="DEBUG", format="{message}\n{exception}")
        try:
            with patch.object(job_runner, "build_items", side_effect=ConnectionError(f"Max retries exceeded: {url}")):
                job_runner.run_intro_credits_job("j1")
        finally:
            logger.remove(handler)
        error = env.jm.complete_job.call_args.kwargs["error"]
        assert error == (
            "ConnectionError: Max retries exceeded: http://plex:32400/library/sections/1/all?X-Plex-Token=****&type=4"
        )
        logged = "".join(lines)
        assert "s3cr3t" not in logged and "s3cr3t" not in str(env.jm.add_log.call_args_list)
        # The traceback still reaches the app log (redacted), but not the job's own log.
        assert "Traceback" in logged and "X-Plex-Token=****" in logged
        job_log = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert f"ERROR - Intro & Credits job j1 failed: {error}" in job_log
        assert not any("Traceback" in line for line in job_log)


ALL_OUTCOMES = [
    ("markers_published", True),
    ("markers_up_to_date", True),
    ("markers_needs_review", True),
    ("markers_none", True),
    ("markers_no_owners", True),
    ("markers_waiting", False),  # the server may have indexed the item while the app was down
    ("markers_skipped", False),  # a plugin or setting may have been fixed meanwhile
    ("skipped_file_not_found", False),  # a stale mount often comes back with a restart
    ("failed", False),
]


class TestRestart:
    @pytest.fixture
    def finished(self, env, tmp_path):
        """Real files, with the store's identity of each and the job's rows from before the restart."""
        records = {}

        def make(name, outcome, *, analysed="same", servers=None):
            path = tmp_path / name
            path.write_bytes(b"x" * 10)
            st = os.stat(path)
            if analysed == "same":
                records[str(path)] = SimpleNamespace(id=len(records) + 1, size=st.st_size, mtime_ns=st.st_mtime_ns)
            elif analysed == "replaced":
                records[str(path)] = SimpleNamespace(id=len(records) + 1, size=st.st_size + 1, mtime_ns=st.st_mtime_ns)
            row = {"file": str(path), "outcome": outcome}
            if servers is not None:
                row["servers"] = servers
            env.jm.get_file_results.return_value.append(row)
            return _item(str(path))

        env.jm.get_file_results.return_value = []
        env.ctx.store.get_file.side_effect = records.get
        return make

    def test_a_job_whose_files_all_finished_before_the_restart_starts_the_sweep(self, env, finished):
        item = finished("a.mkv", "markers_published")
        with (
            patch.object(job_runner, "build_items", return_value=([item], [], {})),
            patch.object(job_runner, "start_fingerprint_sweep") as sweep,
        ):
            job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)
        sweep.assert_called_once_with(env.ctx.store)

    def test_resumed_job_skips_unchanged_finished_files_and_carries_their_counts(self, env, finished, tmp_path):
        done = finished("a.mkv", "markers_published")
        failed = finished("b.mkv", "failed")
        replaced = finished("c.mkv", "markers_published", analysed="replaced")
        store_reset = finished("d.mkv", "markers_up_to_date", analysed=None)
        env.jm.get_file_results.return_value.append({"file": "", "outcome": "truncated:markers_up_to_date"})
        new = _item(str(tmp_path / "e.mkv"))
        items = [done, failed, replaced, store_reset, new]
        with patch.object(job_runner, "build_items", return_value=(items, [], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.get_file_results.assert_called_once_with("j1")
        kwargs = env.dispatcher.submit_items.call_args.kwargs
        assert kwargs["items"] == [failed, replaced, store_reset, new]
        # The tracker counts them from the start (live "x/y" and breakdown); its result already includes them.
        assert kwargs["carried_outcome"] == {"markers_published": 1}
        env.jm.set_job_outcome.assert_called_once_with("j1", {"markers_published": 1})

    @pytest.fixture
    def decided_in_store(self, env, tmp_path):
        """A real markers store: each file analysed as it is on disk, with a decision per type, and this job's row."""
        store = MarkerStore(str(tmp_path / "markers.db"))
        env.ctx.store = store
        env.ctx.decided_by = DecidedByTally()
        env.jm.get_file_results.return_value = []

        def make(name, outcome, decided):
            path = tmp_path / name
            path.write_bytes(b"x" * 10)
            st = os.stat(path)
            rec = store.upsert_file(
                FileIdentity(str(path), st.st_size, st.st_mtime_ns),
                duration_ms=1_320_000,
                season_key=None,
                is_movie=True,
            )
            decisions = {
                mtype: TypeDecision(mtype, DecisionStatus.DECIDED, Marker(mtype, 0, 30_000, sources), None, "")
                if sources
                else TypeDecision(mtype, DecisionStatus.NEEDS_REVIEW, None, None, "one source")
                for mtype, sources in decided.items()
            }
            store.save_decisions(rec.id, decisions, settings_fingerprint="fp")
            env.jm.get_file_results.return_value.append({"file": str(path), "outcome": outcome})
            return _item(str(path))

        yield make
        store.close()

    def test_resumed_job_counts_the_files_it_carries_from_what_the_store_decided(self, env, decided_in_store):
        chapters, credit_text = ("chapters",), ("credits_text",)
        items = [
            decided_in_store(
                "a.mkv", "markers_published", {MarkerType.INTRO: chapters, MarkerType.CREDITS: credit_text}
            ),
            decided_in_store("b.mkv", "markers_up_to_date", {MarkerType.CREDITS: chapters}),
            # Only the decided type of a file in review counts.
            decided_in_store("c.mkv", "markers_needs_review", {MarkerType.INTRO: (), MarkerType.CREDITS: chapters}),
            decided_in_store("d.mkv", "markers_none", {MarkerType.CREDITS: ()}),
            # No server had Intro & Credits on for it, so this job decided nothing; the store's markers are old.
            decided_in_store("e.mkv", "markers_no_owners", {MarkerType.CREDITS: chapters}),
            # Run again, so it counts when its new run finishes, not from the store.
            decided_in_store("f.mkv", "failed", {MarkerType.CREDITS: chapters}),
        ]
        with patch.object(job_runner, "build_items", return_value=(items, [], {})):
            job_runner.run_intro_credits_job("j1")

        assert env.jm.set_marker_sources.call_args_list[0] == call(
            "j1", {"intro": {"chapters": 1}, "credits": {"credits_text": 1, "chapters": 2}}
        )

    def test_a_job_with_nothing_carried_reads_no_sources_from_the_store(self, env, finished, tmp_path):
        with (
            patch.object(job_runner, "build_items", return_value=([_item(str(tmp_path / "new.mkv"))], [], {})),
            patch.object(job_runner, "stored_groups") as stored,
        ):
            job_runner.run_intro_credits_job("j1")

        stored.assert_not_called()

    @pytest.mark.parametrize(("outcome", "skipped"), ALL_OUTCOMES)
    def test_only_settled_outcomes_are_skipped(self, env, finished, outcome, skipped):
        item = finished("a.mkv", outcome)
        other = finished("b.mkv", "failed")
        with patch.object(job_runner, "build_items", return_value=([item, other], [], {})):
            job_runner.run_intro_credits_job("j1")
        submitted = env.dispatcher.submit_items.call_args.kwargs["items"]
        assert submitted == ([other] if skipped else [item, other])

    @pytest.mark.parametrize(
        ("servers", "skipped"),
        [
            ([{"id": "plex-1", "status": "markers_written"}, {"id": "jf-1", "status": "markers_waiting"}], False),
            ([{"id": "plex-1", "status": "markers_written"}, {"id": "jf-1", "status": "markers_up_to_date"}], True),
            ([], True),
        ],
        ids=["one-server-waiting", "all-settled", "no-servers"],
    )
    def test_published_file_with_a_server_still_waiting_is_checked_again(self, env, finished, servers, skipped):
        # Plex was written but Jellyfin hadn't indexed the file; the restart may have lost its retry job.
        item = finished("a.mkv", "markers_published", servers=servers)
        other = finished("b.mkv", "failed")
        with patch.object(job_runner, "build_items", return_value=([item, other], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == ([other] if skipped else [item, other])

    def test_file_gone_from_disk_is_checked_again(self, env, finished, tmp_path):
        item = finished("a.mkv", "markers_published")
        os.remove(item.canonical_path)
        with patch.object(job_runner, "build_items", return_value=([item], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == [item]

    def test_resumed_job_with_every_file_finished_completes_without_submitting(self, env, finished):
        item = finished("a.mkv", "markers_needs_review")
        env.ctx.summary_lines.return_value = ["Done: 1 file · 0 need review · 0 nothing found"]
        with patch.object(job_runner, "build_items", return_value=([item], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.set_job_outcome.assert_called_once_with("j1", {"markers_needs_review": 1})
        env.jm.complete_job.assert_called_once_with("j1", warning=None)
        # Its totals count the files finished before the restart.
        env.ctx.summary_lines.assert_called_once_with({"markers_needs_review": 1})
        env.jm.add_log.assert_any_call("j1", "INFO - Done: 1 file · 0 need review · 0 nothing found")

    @pytest.mark.parametrize("results", [OSError("disk gone"), None])
    def test_unreadable_file_results_checks_every_file_again(self, env, monkeypatch, results):
        if isinstance(results, Exception):
            env.jm.get_file_results.side_effect = results
        else:
            env.jm.get_file_results.return_value = results
        with patch.object(job_runner, "build_items", return_value=([_item("/m/a.mkv")], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == [_item("/m/a.mkv")]

    @pytest.mark.parametrize("by_schedule", [False, True], ids=["paused-by-hand", "paused-by-stop-time"])
    def test_job_paused_before_the_restart_stays_paused_without_a_slot_until_resumed(
        self, env, monkeypatch, by_schedule
    ):
        env.job.paused = True
        if by_schedule:
            env.job.config["paused_by_schedule"] = True
        order = []
        env.jm.start_job.side_effect = lambda jid: order.append("start")
        env.jm.request_pause.side_effect = lambda jid, **kw: order.append("pause") or True
        state = {"paused": True}
        env.jm.is_pause_requested.side_effect = lambda jid: state["paused"]
        sleeps = []

        def fake_sleep(_seconds):
            sleeps.append(1)
            env.gate.acquire.assert_not_called()
            if len(sleeps) == 2:
                state["paused"] = False

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert order[:2] == ["start", "pause"]
        # A stop-time pause is held as one, so the schedule's next start still resumes it; a pause by hand isn't.
        env.jm.request_pause.assert_called_once_with("j1", by_schedule=by_schedule)
        assert len(sleeps) == 2
        env.gate.acquire.assert_called_once()
        env.dispatcher.submit_items.assert_called_once()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def test_job_paused_before_the_restart_cancelled_while_held(self, env, monkeypatch):
        env.job.paused = True
        env.jm.is_pause_requested.return_value = True

        def fake_sleep(_seconds):
            env.jm.is_cancellation_requested.return_value = True

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        job_runner.run_intro_credits_job("j1")
        env.gate.acquire.assert_not_called()
        env.jm.cancel_job.assert_called_once_with("j1")
        env.dispatcher.submit_items.assert_not_called()

    @pytest.mark.parametrize(("kind", "keeps_pause"), [(JOB_KIND_INTRO_CREDITS, True), (JOB_KIND_PREVIEWS, False)])
    @pytest.mark.parametrize("by_schedule", [False, True], ids=["by-hand", "by-stop-time"])
    def test_requeue_after_restart_keeps_only_an_intro_credits_jobs_own_pause(
        self, tmp_path, kind, keeps_pause, by_schedule
    ):
        from media_preview_generator.web.jobs import JobManager, JobStatus

        config_dir = str(tmp_path)
        before = JobManager(config_dir=config_dir)
        job = before.create_job(library_name="Backfill", kind=kind, config={"kind": kind, "libraries": []})
        before.start_job(job.id)
        assert before.request_pause(job.id, by_schedule=by_schedule)

        after = JobManager(config_dir=config_dir)
        revived = after.requeue_interrupted_jobs()

        assert [j.id for j in revived] == [job.id]
        assert revived[0].status is JobStatus.PENDING
        assert revived[0].paused is keeps_pause
        assert JobManager(config_dir=config_dir).get_job(job.id).paused is keeps_pause
        # Where the pause came from outlives the restart with the job's config.
        assert revived[0].config.get("paused_by_schedule", False) is by_schedule

    def test_a_job_its_stop_time_paused_is_held_paused_as_the_stop_times_after_a_restart(self, tmp_path, monkeypatch):
        # Real JobManager: the revived job is loaded paused, and the hold's stop-time pause must still take.
        from media_preview_generator.web.jobs import JobManager, JobStatus

        before = JobManager(config_dir=str(tmp_path))
        job = before.create_job(library_name="Backfill", kind=JOB_KIND_INTRO_CREDITS, config={"libraries": []})
        before.start_job(job.id)
        assert before.request_pause(job.id, by_schedule=True)
        after = JobManager(config_dir=str(tmp_path))
        (revived,) = after.requeue_interrupted_jobs()
        assert revived.paused
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: after)
        held = []

        def fake_sleep(_seconds):
            live = after.get_job(job.id)
            held.append((live.status, after.is_pause_requested(job.id), live.config.get("paused_by_schedule")))
            after.request_resume(job.id, only_paused_by_schedule=True)  # the schedule's next start

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        assert job_runner._hold_pause_from_before_restart(job.id, lambda: False) is True
        assert held == [(JobStatus.RUNNING, True, True)]
        assert not after.get_job(job.id).paused

    def test_startup_revival_runs_each_intro_credits_job_once_and_creates_no_jobs(self, tmp_path, monkeypatch):
        from media_preview_generator.web import app as app_mod
        from media_preview_generator.web.jobs import JobManager
        from media_preview_generator.web.routes import job_runner as preview_runner

        config_dir = str(tmp_path)
        before = JobManager(config_dir=config_dir)
        config = {
            "kind": JOB_KIND_INTRO_CREDITS,
            "source": "sonarr",
            "libraries": [],
            "file_paths": ["/data/tv/a.mkv"],
            "follows_job_id": "prev-1",
            "force": True,
            "webhook_item_id_hints": {"/data/tv/a.mkv": {"jf-1": "x"}},
        }
        ic = before.create_job(library_name="Intro & Credits · a", kind=JOB_KIND_INTRO_CREDITS, config=config)
        preview = before.create_job(library_name="a", config={"webhook_paths": ["/data/tv/a.mkv"]})
        before.start_job(ic.id)
        before.start_job(preview.id)

        after = JobManager(config_dir=config_dir)
        monkeypatch.setattr(app_mod, "get_job_manager", lambda: after)
        monkeypatch.setattr(preview_runner, "get_job_manager", lambda: after)
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: after)
        sm = MagicMock(processing_paused=False)
        sm.get.side_effect = lambda key, default=None: default
        monkeypatch.setattr("media_preview_generator.web.settings_manager.get_settings_manager", lambda *a: sm)
        runs = []
        with (
            patch.object(job_runner, "run_intro_credits_job", side_effect=runs.append),
            patch.object(preview_runner, "threading") as preview_threads,
        ):
            app_mod._requeue_interrupted_on_startup(config_dir)
            # A second start for the same job (e.g. the pending drain after a resume) while it is in flight.
            with job_runner._inflight_lock:
                job_runner._inflight_jobs.add(ic.id)
            try:
                preview_runner._start_job_async(ic.id, ic.config)
            finally:
                with job_runner._inflight_lock:
                    job_runner._inflight_jobs.discard(ic.id)
                preview_runner._inflight_jobs.discard(preview.id)

        assert runs == [ic.id]
        preview_threads.Thread.assert_called_once()
        assert len(after.get_all_jobs()) == 2
        assert after.get_job(ic.id).config == config
        assert after.get_job(ic.id).kind == JOB_KIND_INTRO_CREDITS


def _row(status, message, sid="jf-1", **extra):
    return {"server_id": sid, "status": status, "message": message, **extra}


NOT_IN_LIBRARY_ROW = _row("markers_waiting", "Not in this server's library yet", reason_code="not_in_library")
PLEX_PASS_UNKNOWN_ROW = _row(
    "markers_waiting", "Can't reach Plex to confirm Plex Pass", sid="plex-1", reason_code="plex_pass_unknown"
)
VERSIONS_UNCHECKED_ROW = _row(
    "markers_waiting",
    "Waiting for this item's other versions to agree on: intro, credits",
    sid="plex-1",
    reason_code="versions_unchecked",
)


class TestLibraryRetry:
    """Files a server hasn't indexed yet get a delayed retry job (Option B ruling)."""

    @pytest.fixture
    def retry_env(self, env, monkeypatch):
        from media_preview_generator.markers import triggers

        settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
        env.sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        env.job.library_name = "Rick and Morty S01 · 2 files"
        env.job.config = {"libraries": [], "file_paths": ["/data/tv/a.mkv"], "source": "sonarr"}
        create = MagicMock(return_value=MagicMock(id="retry-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        results = []

        def during_wait(timeout=None):
            callback = set_cb.call_args_list[0].args[0]
            for path, outcome, rows in results:
                callback(path, outcome, "", "Lookup", servers=rows)
            return True

        env.tracker.wait.side_effect = during_wait
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)
        return SimpleNamespace(settings=settings, create=create, results=results)

    def _run(self, paths=("/m/a.mkv", "/m/b.mkv"), listing=None):
        from media_preview_generator.markers import reconcile

        items = [_item(p) for p in paths]
        listing = listing or reconcile.CheckServersListing(items, [])
        with (
            patch.object(job_runner, "build_items", return_value=(items, [], {})),
            patch.object(reconcile, "check_servers_listing", return_value=listing),
        ):
            job_runner.run_intro_credits_job("j1")

    @pytest.mark.parametrize(
        ("attempt_done", "retry_delay", "expected_attempt", "expected_delay"),
        [
            (None, 30, 1, 60),
            (1, 30, 2, 120),
            (2, 30, 3, 300),
            (None, 60, 1, 120),  # webhook_retry_delay scales the backoff like preview retries
            (None, 10, 1, 30),  # scale never drops below half
        ],
    )
    def test_item_id_lookup_that_found_nothing_gets_a_retry_follow_up(
        self, env, retry_env, attempt_done, retry_delay, expected_attempt, expected_delay
    ):
        retry_env.settings["webhook_retry_delay"] = retry_delay
        if attempt_done is not None:
            env.job.config["retry_attempt"] = attempt_done
            env.job.library_name = "Retry: Rick and Morty S01 · 2 files"
        retry_env.results += [
            ("/m/b.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]),
            ("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW, _row("markers_waiting", "x", sid="plex-1")]),
        ]
        self._run()
        retry_env.create.assert_called_once_with(
            library_name="Retry: Rick and Morty S01 · 2 files",
            priority=3,
            source="sonarr",
            file_paths=["/m/a.mkv", "/m/b.mkv"],
            item_id_hints=None,
            retry_attempt=expected_attempt,
            retry_delay_s=expected_delay,
            verify_chain=False,
            parent_job_id="j1",
            max_retries=3,
        )
        env.jm.complete_job.assert_called_once_with("j1", warning=None)
        assert any("retry" in c.args[1].lower() for c in env.jm.add_log.call_args_list)

    def test_retry_carries_the_jobs_priority_at_the_end(self, env, retry_env):
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))

        def during_wait(timeout=None):
            job_runner.set_file_result_callback.call_args_list[0].args[0](
                "/m/a.mkv", "markers_waiting", "", "Lookup", servers=[NOT_IN_LIBRARY_ROW]
            )
            env.job.priority = 1
            return True

        env.tracker.wait.side_effect = during_wait
        self._run(["/m/a.mkv"])
        assert retry_env.create.call_args.kwargs["priority"] == 1

    @pytest.mark.parametrize(("count", "attempt_done"), [(3, 3), (0, None), (1, 1)])
    def test_retries_stop_after_webhook_retry_count(self, env, retry_env, count, attempt_done):
        retry_env.settings["webhook_retry_count"] = count
        if attempt_done is not None:
            env.job.config["retry_attempt"] = attempt_done
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run(["/m/a.mkv"])
        retry_env.create.assert_not_called()
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        if count == 0:
            expected = "WARNING - 1 file(s) not in a server's library yet; retries are off, so a later job for them tries again"
        else:
            expected = (
                f"WARNING - 1 file(s) still not in a server's library after {count} retr{'y' if count == 1 else 'ies'}; "
                "a later job for them tries again"
            )
        assert expected in logs, logs

    def test_retry_takes_at_most_500_files_and_says_how_many_wait_for_the_next_run(self, env, retry_env):
        paths = [f"/m/{i:04d}.mkv" for i in range(501)]
        retry_env.results += [(p, "markers_waiting", [NOT_IN_LIBRARY_ROW]) for p in reversed(paths)]
        self._run(paths)
        assert retry_env.create.call_args.kwargs["file_paths"] == paths[:500]
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert (
            "INFO - 1 more files not in a server's library yet get no retry; a later job for them tries again" in logs
        )

    def test_exactly_500_files_are_all_retried(self, env, retry_env):
        paths = [f"/m/{i:04d}.mkv" for i in range(500)]
        retry_env.results += [(p, "markers_waiting", [NOT_IN_LIBRARY_ROW]) for p in paths]
        self._run(paths)
        assert retry_env.create.call_args.kwargs["file_paths"] == paths
        assert not any("more files" in c.args[1] for c in env.jm.add_log.call_args_list)

    @pytest.mark.parametrize(
        ("outcome", "other_row"),
        [
            ("markers_waiting", _row("markers_written", "2 marker(s)", sid="plex-1")),
            ("markers_needs_review", _row("markers_up_to_date", "Up to date", sid="plex-1")),
            ("failed", _row("failed", "boom", sid="plex-1")),
        ],
        ids=["waiting", "needs-review", "failed"],
    )
    def test_a_server_that_hasnt_indexed_the_file_retries_whatever_the_file_outcome(
        self, env, retry_env, outcome, other_row
    ):
        # The retry reads the rows: Jellyfin would never get its markers if a file outcome decided it.
        retry_env.results.append(("/m/a.mkv", outcome, [other_row, NOT_IN_LIBRARY_ROW]))
        self._run(["/m/a.mkv"])
        retry_env.create.assert_called_once()
        assert retry_env.create.call_args.kwargs["file_paths"] == ["/m/a.mkv"]
        assert retry_env.create.call_args.kwargs["retry_attempt"] == 1

    @pytest.mark.parametrize(
        "rows",
        [
            [_row("markers_waiting", "Waiting for this item's other versions to agree on: intro")],
            [_row("markers_written", "Not in this server's library yet", reason_code="not_in_library")],
            [_row("markers_waiting", "Not in this server's library yet")],  # the code decides, not the wording
            [_row("markers_waiting", "Plugin not installed", reason_code="plugin_missing")],
            [],
        ],
        ids=["versions", "written", "message-without-code", "other-code", "no-rows"],
    )
    def test_other_rows_get_no_retry(self, env, retry_env, rows):
        retry_env.results.append(("/m/a.mkv", "markers_waiting", rows))
        self._run(["/m/a.mkv"])
        retry_env.create.assert_not_called()

    @pytest.mark.parametrize(
        ("source", "file_paths", "retried"),
        [
            ("sonarr", ["/data/tv/a.mkv"], True),  # the import is still copying over NFS: preview retries it too
            ("jellyfin", ["/data/tv/a.mkv"], True),
            ("retry", ["/data/tv/a.mkv"], True),
            ("manual", ["/data/tv/a.mkv"], False),  # the user picked a file that isn't there; say so, don't retry
            ("inspector", ["/data/tv/a.mkv"], False),
            ("inspector_season", ["/data/tv/Show/Season 01"], False),  # the Season view's Publish
            ("schedule", [], False),  # a library listing only names files the server already has
            ("manual", [], False),
        ],
    )
    def test_file_not_on_disk_yet_is_retried_only_for_webhook_paths(self, env, retry_env, source, file_paths, retried):
        env.job.config = {"libraries": [], "file_paths": file_paths, "source": source}
        retry_env.results.append(("/m/a.mkv", "skipped_file_not_found", []))
        self._run(["/m/a.mkv"])
        if retried:
            retry_env.create.assert_called_once_with(
                library_name="Retry: Rick and Morty S01 · 2 files",
                priority=3,
                source=source,
                file_paths=["/m/a.mkv"],
                item_id_hints=None,
                retry_attempt=1,
                retry_delay_s=60,
                verify_chain=False,
                parent_job_id="j1",
                max_retries=3,
            )
            logs = [c.args[1] for c in env.jm.add_log.call_args_list]
            assert any("not on disk yet" in line and "retry 1 of 3" in line for line in logs), logs
        else:
            retry_env.create.assert_not_called()

    def test_files_not_on_disk_and_not_indexed_share_one_retry(self, env, retry_env):
        retry_env.results += [
            ("/m/b.mkv", "skipped_file_not_found", []),
            ("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]),
            ("/m/c.mkv", "markers_published", [_row("markers_written", "2 marker(s)")]),
        ]
        self._run(["/m/a.mkv", "/m/b.mkv", "/m/c.mkv"])
        retry_env.create.assert_called_once()
        assert retry_env.create.call_args.kwargs["file_paths"] == ["/m/a.mkv", "/m/b.mkv"]
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert any(
            line.startswith("INFO - 2 file(s) not on disk or not in a server's library yet; retry 1") for line in logs
        )

    def test_file_still_not_on_disk_after_the_last_retry_is_left_for_the_next_run(self, env, retry_env):
        env.job.config["retry_attempt"] = 3
        retry_env.results.append(("/m/a.mkv", "skipped_file_not_found", []))
        self._run(["/m/a.mkv"])
        retry_env.create.assert_not_called()
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert any(line.startswith("WARNING - 1 file(s) still not on disk after 3 retries") for line in logs), logs

    @pytest.mark.parametrize(
        ("rows", "reason"),
        [
            ([NOT_IN_LIBRARY_ROW], "not in a server's library yet"),
            ([PLEX_PASS_UNKNOWN_ROW], "not checked on Plex yet"),  # Plex restarting: the Pass check didn't answer
            ([PLEX_PASS_UNKNOWN_ROW, NOT_IN_LIBRARY_ROW], "not in a server's library or not checked on Plex yet"),
            # Another version of the Plex item is on disk but unchecked: it may be checked, or deleted, by then.
            ([VERSIONS_UNCHECKED_ROW], "with another version not checked yet"),
        ],
        ids=["not-indexed", "plex-pass-unknown", "both", "versions-unchecked"],
    )
    def test_each_retry_reason_gets_the_retry_and_its_log_line(self, env, retry_env, rows, reason):
        retry_env.results.append(("/m/a.mkv", "markers_waiting", rows))
        self._run(["/m/a.mkv"])
        assert retry_env.create.call_args.kwargs["file_paths"] == ["/m/a.mkv"]
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert f"INFO - 1 file(s) {reason}; retry 1 of 3 in 60s (job retry-1)" in logs, logs

    def test_all_three_reasons_share_one_retry_and_one_log_line(self, env, retry_env):
        retry_env.results += [
            ("/m/a.mkv", "skipped_file_not_found", []),
            ("/m/b.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]),
            ("/m/c.mkv", "markers_waiting", [PLEX_PASS_UNKNOWN_ROW]),
        ]
        env.job.config["retry_attempt"] = 3
        self._run(["/m/a.mkv", "/m/b.mkv", "/m/c.mkv"])
        retry_env.create.assert_not_called()
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert (
            "WARNING - 3 file(s) still not on disk, not in a server's library or not checked on Plex after 3 retries; "
            "a later job for them tries again"
        ) in logs, logs

    def test_stable_reason_code_is_recognised_whatever_the_message(self, env, retry_env):
        retry_env.results.append(
            (
                "/m/a.mkv",
                "markers_waiting",
                [_row("markers_waiting", "Jellyfin hasn't scanned it", reason_code="not_in_library")],
            )
        )
        self._run(["/m/a.mkv"])
        retry_env.create.assert_called_once()

    @pytest.mark.parametrize("row", [NOT_IN_LIBRARY_ROW, PLEX_PASS_UNKNOWN_ROW], ids=["not-in-library", "plex-pass"])
    def test_check_servers_queues_no_retry_and_hands_the_rows_to_the_listing(self, env, retry_env, row):
        # A retry chain on every run would pile up; a later Check servers run lists the file again instead.
        from media_preview_generator.markers import reconcile

        env.job.config = {"reconcile": True, "source": "reconcile"}
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [row]))
        listing = MagicMock(spec=reconcile.CheckServersListing, items=[_item("/m/a.mkv")], warnings=[])
        listing.confirmed_gone_items.return_value = set()  # no item confirmed gone
        self._run(listing=listing)
        retry_env.create.assert_not_called()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)
        listing.confirmed_gone_items.assert_called_once_with(env.registry, "/m/a.mkv", [row])

    @staticmethod
    def _check_servers_file(env, confirmed_gone):
        from media_preview_generator.markers import reconcile

        env.job.config = {"reconcile": True, "source": "reconcile"}
        env.job.library_name = reconcile.RECONCILE_JOB_NAME
        listing = reconcile.CheckServersListing([_item("/m/a.mkv")], [], {"/m/a.mkv": frozenset({("jf-1", "x")})})
        return listing, patch.object(reconcile, "_confirmed_missing", return_value=confirmed_gone)

    @pytest.mark.parametrize("confirmed_gone", [True, False], ids=["item-confirmed-gone", "item-not-confirmed-gone"])
    def test_check_servers_retries_once_a_file_whose_old_item_the_server_confirmed_gone(
        self, env, retry_env, confirmed_gone
    ):
        # The server deleted the old item but may not have indexed the new one yet: the file gets the retry a normal
        # job queues, and only then leaves Check servers. An item not confirmed gone is read back next run.
        listing, confirmed_missing = self._check_servers_file(env, confirmed_gone)
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        order = []
        retry_env.create.side_effect = lambda **kw: order.append("retry") or MagicMock(id="retry-1")
        env.ctx.store.mark_item_gone.side_effect = lambda *args: order.append(("marked", *args))
        with confirmed_missing as confirmed:
            self._run(listing=listing)
        confirmed.assert_called_once_with(env.registry, "jf-1", "x")
        env.jm.record_file_result.assert_called_once_with(
            "j1", "/m/a.mkv", "markers_waiting", "", "Lookup", servers=[NOT_IN_LIBRARY_ROW], server_messages=True
        )
        if confirmed_gone:
            assert order == ["retry", ("marked", "jf-1", "x")]  # marked gone only once its retry exists
            retry_env.create.assert_called_once_with(
                library_name="Retry: Intro & Credits · Check servers",
                priority=3,
                source="reconcile",
                file_paths=["/m/a.mkv"],
                item_id_hints=None,
                retry_attempt=1,
                retry_delay_s=60,
                verify_chain=False,
                parent_job_id="j1",
                max_retries=3,
            )
        else:
            assert order == []
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize("ending", ["cancelled", "retries-off", "retry-not-created", "job-failed"])
    def test_a_confirmed_gone_item_stays_in_check_servers_when_no_retry_was_queued(self, env, retry_env, ending):
        # Marked gone without a retry, the file would never be tried again: the next run confirms it again instead.
        listing, confirmed_missing = self._check_servers_file(env, True)
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        if ending == "cancelled":
            env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        elif ending == "retries-off":
            retry_env.settings["webhook_retry_count"] = 0
        elif ending == "retry-not-created":
            retry_env.create.side_effect = RuntimeError("jobs.db locked")
        else:
            # The run fails before its retry is queued (the retry comes before completing, as for preview jobs).
            env.jm.set_job_outcome.side_effect = RuntimeError("jobs.db locked")
        with confirmed_missing:
            self._run(listing=listing)
        env.ctx.store.mark_item_gone.assert_not_called()
        assert retry_env.create.call_count == (1 if ending == "retry-not-created" else 0)
        if ending == "retries-off":
            logs = [c.args[1] for c in env.jm.add_log.call_args_list]
            assert (
                "WARNING - 1 file(s) not in a server's library yet; retries are off, so Check servers checks them "
                "again on its next run"
            ) in logs, logs

    @pytest.mark.parametrize("failing", ["server-lookup", "marking-gone"])
    def test_a_store_or_lookup_error_still_records_the_file_and_completes(self, env, retry_env, failing):
        from media_preview_generator.markers import reconcile

        listing, confirmed_missing = self._check_servers_file(env, True)
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        if failing == "server-lookup":
            confirmed_missing = patch.object(reconcile, "_confirmed_missing", side_effect=RuntimeError("boom"))
        else:
            env.ctx.store.mark_item_gone.side_effect = sqlite3.OperationalError("database is locked")
        with confirmed_missing:
            self._run(listing=listing)
        env.jm.record_file_result.assert_called_once_with(
            "j1", "/m/a.mkv", "markers_waiting", "", "Lookup", servers=[NOT_IN_LIBRARY_ROW], server_messages=True
        )
        # A lookup that raised confirms nothing (no retry, listed again); a failed mark leaves the queued retry.
        assert retry_env.create.call_count == (0 if failing == "server-lookup" else 1)
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def test_a_retry_check_servers_queued_chains_not_in_library_retries_only(self, env, retry_env):
        # Its files were on disk and nothing was just replaced: no not-on-disk retry and no verify job.
        env.job.config = {"source": "reconcile", "file_paths": ["/m/a.mkv", "/m/b.mkv"], "retry_attempt": 1}
        env.job.library_name = "Retry: Intro & Credits · Check servers"
        replaced_row = _row("markers_written", "2 marker(s)", verify_later=True)
        retry_env.results += [
            ("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]),
            ("/m/b.mkv", "skipped_file_not_found", []),
            ("/m/c.mkv", "markers_published", [replaced_row]),
        ]
        with patch.object(job_runner, "_queue_verify") as verify:
            self._run(["/m/a.mkv", "/m/b.mkv", "/m/c.mkv"])
        verify.assert_not_called()
        retry_env.create.assert_called_once()
        kwargs = retry_env.create.call_args.kwargs
        assert (kwargs["file_paths"], kwargs["retry_attempt"], kwargs["source"]) == (["/m/a.mkv"], 2, "reconcile")
        assert kwargs["library_name"] == "Retry: Intro & Credits · Check servers"

    def test_other_jobs_forget_no_items(self, env, retry_env):
        from media_preview_generator.markers import reconcile

        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        with patch.object(reconcile.CheckServersListing, "confirmed_gone_items") as forget:
            self._run()
        forget.assert_not_called()

    def test_cancelled_job_gets_no_retry(self, env, retry_env):
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        self._run(["/m/a.mkv"])
        retry_env.create.assert_not_called()

    def test_retry_that_cant_be_created_leaves_the_job_completed(self, env, retry_env):
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        retry_env.create.side_effect = RuntimeError("jobs.db locked")
        self._run(["/m/a.mkv"])
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def test_file_results_are_still_recorded(self, env, retry_env):
        retry_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run(["/m/a.mkv"])
        env.jm.record_file_result.assert_called_once_with(
            "j1", "/m/a.mkv", "markers_waiting", "", "Lookup", servers=[NOT_IN_LIBRARY_ROW], server_messages=True
        )


class TestRetryChain:
    """A job with files still waiting heads a retry chain like a preview job: its hidden retry runs, its row shows it."""

    @pytest.fixture
    def chain_env(self, env, monkeypatch):
        from media_preview_generator.markers import triggers

        settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
        env.sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        env.job.library_name = "Intro & Credits · Pilot"
        env.job.config = {"libraries": [], "file_paths": ["/m/a.mkv"], "source": "sonarr"}
        retry = MagicMock(id="retry-1", config={"retry_not_before": "2026-09-23T10:01:00+00:00"})
        create = MagicMock(return_value=retry)
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        order = []
        env.jm.upsert_retry_chain_job.side_effect = lambda **kw: order.append(("chain", kw["outcome"]))
        env.jm.complete_job.side_effect = lambda job_id, **kw: order.append(("complete", job_id))
        rows = []
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)

        def during_wait(timeout=None):
            for path, outcome, servers in rows:
                set_cb.call_args_list[0].args[0](path, outcome, "", "Lookup", servers=servers)
            return True

        env.tracker.wait.side_effect = during_wait
        return SimpleNamespace(settings=settings, create=create, order=order, rows=rows)

    def _run(self):
        with patch.object(job_runner, "build_items", return_value=([_item("/m/a.mkv")], [], {})):
            job_runner.run_intro_credits_job("j1")

    def _chain_calls(self, env):
        return [c.kwargs for c in env.jm.upsert_retry_chain_job.call_args_list]

    def test_a_job_with_a_file_waiting_is_scheduled_as_its_own_chain_head_before_it_completes(self, env, chain_env):
        chain_env.rows.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run()
        assert chain_env.create.call_args.kwargs["parent_job_id"] == "j1"
        assert chain_env.create.call_args.kwargs["max_retries"] == 3
        assert self._chain_calls(env) == [
            {
                "canonical_path": "",
                "basename": "",
                "attempt": 1,
                "max_attempts": 3,
                "next_run_at": "2026-09-23T10:01:00+00:00",
                "wait_seconds": 60,
                "outcome": "scheduled",
                "originating_job_id": "j1",
                "reason": None,
            }
        ]
        # complete_job then keeps the row pending ("chain drives lifecycle") and only settles the run.
        assert chain_env.order == [("chain", "scheduled"), ("complete", "j1")]

    def test_a_job_with_nothing_waiting_starts_no_chain(self, env, chain_env):
        chain_env.rows.append(("/m/a.mkv", "markers_published", [_row("markers_written", "2 marker(s)")]))
        self._run()
        chain_env.create.assert_not_called()
        env.jm.upsert_retry_chain_job.assert_not_called()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def _as_retry(self, env, attempt=1):
        env.job.library_name = "Retry: Intro & Credits · Pilot"
        env.job.config = {
            "libraries": [],
            "file_paths": ["/m/a.mkv"],
            "source": "sonarr",
            "retry_attempt": attempt,
            "is_retry": True,
            "parent_job_id": "head-1",
            "max_retries": 3,
        }

    def test_a_retry_shows_its_run_on_the_head_and_records_its_files_there(self, env, chain_env):
        self._as_retry(env)
        chain_env.rows.append(("/m/a.mkv", "markers_published", [_row("markers_written", "2 marker(s)")]))
        self._run()
        running, completed = self._chain_calls(env)
        assert (running["originating_job_id"], running["outcome"], running["attempt"], running["max_attempts"]) == (
            "head-1",
            "running",
            1,
            3,
        )
        env.jm.record_file_result.assert_called_once_with(
            "head-1",
            "/m/a.mkv",
            "markers_published",
            "",
            "Lookup",
            servers=[_row("markers_written", "2 marker(s)")],
            server_messages=True,
        )
        # Nothing left waiting: the chain ends completed, as a preview chain does.
        assert (completed["originating_job_id"], completed["outcome"], completed["reason"]) == (
            "head-1",
            "completed",
            None,
        )
        env.jm.complete_job.assert_called_once_with("j1", warning=None)
        chain_env.create.assert_not_called()

    def test_a_retry_with_the_file_still_waiting_schedules_the_next_retry_of_the_same_chain(self, env, chain_env):
        self._as_retry(env)
        chain_env.rows.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run()
        kwargs = chain_env.create.call_args.kwargs
        assert (kwargs["parent_job_id"], kwargs["retry_attempt"], kwargs["retry_delay_s"]) == ("head-1", 2, 120)
        assert kwargs["library_name"] == "Retry: Intro & Credits · Pilot"
        scheduled = self._chain_calls(env)[-1]
        assert (scheduled["originating_job_id"], scheduled["outcome"], scheduled["attempt"]) == (
            "head-1",
            "scheduled",
            2,
        )
        assert [c["outcome"] for c in self._chain_calls(env)] == ["running", "scheduled"]

    @pytest.mark.parametrize("count", [3, 0], ids=["past-the-count", "retries-turned-off"])
    def test_the_last_retry_ends_the_chain_exhausted_with_the_reason(self, env, chain_env, count):
        # Past the retry count, or retries turned off since the chain started: the head fails like a preview chain.
        chain_env.settings["webhook_retry_count"] = count
        self._as_retry(env, attempt=3)
        chain_env.rows.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run()
        chain_env.create.assert_not_called()
        exhausted = self._chain_calls(env)[-1]
        assert (exhausted["originating_job_id"], exhausted["outcome"], exhausted["attempt"]) == (
            "head-1",
            "exhausted",
            3,
        )
        assert exhausted["reason"] == (
            "1 file(s) still not in a server's library after 3 retries. Check the Files panel for the affected paths."
        )

    def test_a_chain_head_waiting_on_its_retry_is_not_run_again_when_the_queue_resumes(self, env, chain_env):
        env.job.config = {"file_paths": ["/m/a.mkv"], "is_retry_chain": True, "last_outcome": "scheduled"}
        self._run()
        env.gate.acquire.assert_not_called()
        env.dispatcher.submit_items.assert_not_called()
        env.jm.start_job.assert_not_called()

    @pytest.mark.parametrize("last_outcome", ["completed", "exhausted"])
    def test_a_job_whose_chain_ended_runs_when_started(self, env, chain_env, last_outcome):
        env.job.config = {"file_paths": ["/m/a.mkv"], "is_retry_chain": True, "last_outcome": last_outcome}
        self._run()
        env.dispatcher.submit_items.assert_called_once()


class TestCheckServersUsesChecksAsFilesRun:
    """Check servers uses a server recheck, or a failed item's retry, when a file listed for it has its result, not
    when it lists the file: over a real markers store and the real listing (spec §6.2 step 6)."""

    NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)

    @pytest.fixture
    def check_env(self, env, monkeypatch, tmp_path):
        from media_preview_generator.markers import reconcile
        from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
        from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType, Source
        from media_preview_generator.markers.store import MarkerStore

        clock = {"t": self.NOW - timedelta(days=2)}
        store = MarkerStore(str(tmp_path / "markers.db"), clock=lambda: clock["t"])
        root = tmp_path / "media"
        root.mkdir()

        def on_disk(name):
            path = root / name
            path.write_bytes(b"x")
            st = path.stat()
            identity = FileIdentity(str(path), st.st_size, st.st_mtime_ns)
            return str(path), store.upsert_file(identity, duration_ms=1_000_000, season_key=None, is_movie=True)

        # Credits decided; Jellyfin had no markers of its own two days ago: its answer is due to be read again.
        recheck, rec = on_disk("recheck.mkv")
        credits = Marker(MarkerType.CREDITS, 900_000, 1_000_000, ("introdb", "skipdb"))
        decided = TypeDecision(MarkerType.CREDITS, DecisionStatus.DECIDED, credits, None, "introdb")
        store.save_decisions(rec.id, {MarkerType.CREDITS: decided}, settings_fingerprint="f")
        store.replace_evidence(rec.id, Source.SERVER_MARKERS, [], origin="jf-1")
        # Plex item 7's last publish failed two days ago: its retry is due.
        failed, failed_rec = on_disk("failed.mkv")
        intro = Marker(MarkerType.INTRO, 10_000, 40_000, ("chapters",))
        store.set_publish_state(failed_rec.id, "plex-1", item_id="7", markers=[intro], status="written")
        store.set_item_publish_state("plex-1", "7", [intro], "written")
        store.set_item_publish_state("plex-1", "7", None, "failed")
        clock["t"] = self.NOW
        registry = FakeRegistry(
            {
                "plex-1": server_config("plex-1", ServerType.PLEX, root=str(root)),
                "jf-1": server_config("jf-1", ServerType.JELLYFIN, root=str(root)),
            }
        )
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda cfg: registry)
        monkeypatch.setattr(reconcile, "_utcnow", lambda: clock["t"])
        monkeypatch.setattr(job_runner, "start_fingerprint_sweep", MagicMock(return_value=True))
        env.ctx.store = store
        env.job.config = {"reconcile": True, "source": "reconcile"}
        env.job.library_name = reconcile.RECONCILE_JOB_NAME
        # While the job waits: ("cancel") or (path, outcome, reason) file results, in order.
        steps: list = []
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)

        def during_wait(timeout=None):
            callback = set_cb.call_args_list[0].args[0]
            for step in steps:
                if step == "cancel":
                    env.jm.is_cancellation_requested.return_value = True
                    env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
                else:
                    callback(*step, "Lookup", servers=[])
            return True

        env.tracker.wait.side_effect = during_wait

        def counters():
            """(rereads, taken_at) of the recheck, (retries, retried_at) of item 7; None where nothing is stored."""
            reread = store._conn.execute(
                "SELECT rereads, taken_at FROM server_marker_rereads WHERE file_id=? AND server_id='jf-1'", (rec.id,)
            ).fetchone()
            retry = store._conn.execute(
                "SELECT retries, retried_at FROM failed_item_retries WHERE server_id='plex-1' AND item_id='7'"
            ).fetchone()
            return (tuple(reread) if reread else None), (tuple(retry) if retry else None)

        yield SimpleNamespace(
            store=store, registry=registry, recheck=recheck, failed=failed, steps=steps, counters=counters, clock=clock
        )
        store.close()

    def _submitted(self, env):
        return [item.canonical_path for item in env.dispatcher.submit_items.call_args.kwargs["items"]]

    def test_a_run_that_completes_uses_each_files_checks_once_its_result_is_in(self, env, check_env):
        check_env.steps += [(check_env.recheck, "markers_up_to_date", ""), (check_env.failed, "markers_published", "")]
        job_runner.run_intro_credits_job("j1")
        assert sorted(self._submitted(env)) == sorted([check_env.recheck, check_env.failed])
        now = self.NOW.isoformat()
        assert check_env.counters() == ((0, now), (1, now))
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def test_a_run_cancelled_before_any_file_ran_uses_nothing(self, env, check_env, monkeypatch):
        from media_preview_generator.markers import reconcile

        listed = reconcile.check_servers_listing

        def cancelled_right_after(**kwargs):
            listing = listed(**kwargs)
            env.jm.is_cancellation_requested.return_value = True
            return listing

        monkeypatch.setattr(reconcile, "check_servers_listing", cancelled_right_after)
        job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.cancel_job.assert_called_once_with("j1")
        assert check_env.counters() == (None, None)

    def test_a_run_cancelled_midway_uses_only_the_checks_of_the_files_that_ran(self, env, check_env):
        # The cancel stops item 7's file part way: its result says so, and its retry stays due.
        check_env.steps += [
            (check_env.recheck, "markers_up_to_date", ""),
            "cancel",
            (check_env.failed, "failed", "cancelled by user"),
        ]
        job_runner.run_intro_credits_job("j1")
        assert check_env.counters() == ((0, self.NOW.isoformat()), None)
        env.jm.cancel_job.assert_called_once_with("j1")
        assert [c.args[1] for c in env.jm.record_file_result.call_args_list] == [check_env.recheck, check_env.failed]

    def test_a_file_that_fails_without_a_cancel_uses_its_retry(self, env, check_env):
        # It ran: a failure that stays counts toward the item's backoff like any retry (at most 5).
        check_env.steps.append((check_env.failed, "failed", "ffmpeg exited 1"))
        job_runner.run_intro_credits_job("j1")
        assert check_env.counters() == (None, (1, self.NOW.isoformat()))
        env.jm.cancel_job.assert_not_called()

    def test_a_revived_run_uses_the_checks_of_the_files_it_runs(self, env, check_env):
        from media_preview_generator.markers import reconcile

        # The first run listed both files and ran the recheck file before the restart.
        first = reconcile.check_servers_listing(registry=check_env.registry, store=check_env.store, max_files=500)
        first.count_checked(check_env.store, check_env.recheck)
        env.job.config[reconcile.LISTING_CONFIG_KEY] = json.loads(json.dumps(first.to_config()))
        env.jm.get_file_results.return_value = [{"file": check_env.recheck, "outcome": "markers_up_to_date"}]
        check_env.clock["t"] = self.NOW + timedelta(minutes=10)
        check_env.steps.append((check_env.failed, "markers_published", ""))
        job_runner.run_intro_credits_job("j1")
        assert self._submitted(env) == [check_env.failed]
        assert check_env.counters() == ((0, self.NOW.isoformat()), (1, check_env.clock["t"].isoformat()))


class TestVerifyReplacedFilesLater:
    """A replaced file is checked once more a while after its publish: servers rescan it and can drop our markers."""

    @pytest.fixture
    def verify_env(self, env, monkeypatch):
        from media_preview_generator.markers import triggers

        settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
        env.sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        env.job.library_name = "Show - S01E01.mkv"
        env.job.config = {
            "libraries": [],
            "file_paths": ["/data/tv/a.mkv", "/data/tv/b.mkv"],
            "webhook_item_id_hints": {"/data/tv/a.mkv": {"jf-1": "abc"}},
            "source": "sonarr",
        }
        create = MagicMock(side_effect=lambda **kw: MagicMock(id=f"job-{create.call_count}"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        results = []
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)

        def during_wait(timeout=None):
            for path, outcome, rows in results:
                set_cb.call_args_list[0].args[0](path, outcome, "", "Lookup", servers=rows)
            return True

        env.tracker.wait.side_effect = during_wait
        sent = {"/m/a.mkv": "/data/tv/a.mkv", "/m/b.mkv": "/data/tv/b.mkv"}
        return SimpleNamespace(settings=settings, create=create, results=results, sent=sent)

    def _run(self, verify_env, paths=("/m/a.mkv", "/m/b.mkv")):
        with patch.object(job_runner, "build_items", return_value=([_item(p) for p in paths], [], verify_env.sent)):
            job_runner.run_intro_credits_job("j1")

    WRITTEN_LATER = _row("markers_written", "2 marker(s)", verify_later=True)
    UP_TO_DATE_LATER = _row("markers_up_to_date", "Up to date", sid="plex-1", verify_later=True)

    @pytest.mark.parametrize(("retry_delay", "delay"), [(30, 600), (10, 600), (300, 1800)])
    def test_one_verify_job_for_the_replaced_files(self, env, verify_env, retry_delay, delay):
        verify_env.settings["webhook_retry_delay"] = retry_delay
        verify_env.results += [
            ("/m/b.mkv", "markers_published", [self.WRITTEN_LATER]),
            ("/m/a.mkv", "markers_up_to_date", [self.UP_TO_DATE_LATER, self.WRITTEN_LATER]),
        ]
        self._run(verify_env)
        verify_env.create.assert_called_once_with(
            library_name="Verify: Show - S01E01.mkv",
            priority=3,
            source="sonarr",
            file_paths=["/data/tv/a.mkv", "/data/tv/b.mkv"],
            item_id_hints={"/data/tv/a.mkv": {"jf-1": "abc"}},
            retry_delay_s=delay,
            verify=True,
            chain_attempt=0,
        )
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert f"INFO - 2 replaced file(s) are checked again in {delay}s (job job-1)" in logs, logs

    @pytest.mark.parametrize("later", ["verify", "retry"])
    def test_the_later_job_carries_the_item_ids_of_every_sender_of_a_file(self, env, verify_env, later):
        # build_items merged a second sender's Plex id into the first sender's ids for the one file both reported.
        merged = {"jf-1": "abc", "plex-1": "42"}
        item = ProcessableItem("/m/a.mkv", "", dict(merged), title="a.mkv")
        row = self.WRITTEN_LATER if later == "verify" else NOT_IN_LIBRARY_ROW
        verify_env.results.append(("/m/a.mkv", row["status"], [row]))
        with patch.object(job_runner, "build_items", return_value=([item], [], verify_env.sent)):
            job_runner.run_intro_credits_job("j1")
        kwargs = verify_env.create.call_args.kwargs
        assert kwargs["library_name"].startswith("Verify: " if later == "verify" else "Retry: ")
        assert (kwargs["file_paths"], kwargs["item_id_hints"]) == (["/data/tv/a.mkv"], {"/data/tv/a.mkv": merged})

    def test_rows_without_the_flag_queue_nothing(self, env, verify_env):
        verify_env.results.append(("/m/a.mkv", "markers_published", [_row("markers_written", "2 marker(s)")]))
        self._run(verify_env)
        verify_env.create.assert_not_called()

    @pytest.mark.parametrize(
        "chain", [{"verify": True}, {"verify_chain": True, "retry_attempt": 2}], ids=["verify", "its-retry"]
    )
    def test_a_verify_job_and_its_retries_never_queue_another(self, env, verify_env, chain):
        env.job.config.update(chain)
        verify_env.results.append(("/m/a.mkv", "markers_published", [self.WRITTEN_LATER]))
        self._run(verify_env)
        verify_env.create.assert_not_called()

    @pytest.mark.parametrize(
        ("source", "file_paths", "verified"),
        [
            ("sonarr", ["/data/tv/a.mkv"], True),
            ("retry", ["/data/tv/a.mkv"], True),
            # A listing's "replaced" file may have changed days ago; the servers have long since rescanned it.
            ("schedule", [], False),
            ("manual", [], False),
            ("manual", ["/data/tv/a.mkv"], False),
            ("inspector", ["/data/tv/a.mkv"], False),
            ("inspector_season", ["/data/tv/Show/Season 01"], False),
        ],
    )
    def test_only_sent_files_are_checked_again_later(self, env, verify_env, source, file_paths, verified):
        env.job.config = {"libraries": [], "file_paths": file_paths, "source": source}
        verify_env.results.append(("/m/a.mkv", "markers_published", [self.WRITTEN_LATER]))
        self._run(verify_env, ["/m/a.mkv"])
        assert verify_env.create.call_count == int(verified)

    @pytest.mark.parametrize(("attempt_done", "chain_attempt"), [(None, 0), (2, 2)])
    def test_the_verify_carries_the_retries_already_used(self, env, verify_env, attempt_done, chain_attempt):
        if attempt_done is not None:
            env.job.config["retry_attempt"] = attempt_done
        verify_env.results.append(("/m/a.mkv", "markers_published", [self.WRITTEN_LATER]))
        self._run(verify_env, ["/m/a.mkv"])
        assert verify_env.create.call_args.kwargs["chain_attempt"] == chain_attempt

    @pytest.mark.parametrize(("chain_attempt", "attempt", "delay"), [(None, 1, 60), (1, 2, 120), (2, 3, 300)])
    def test_a_retry_from_a_verify_job_goes_on_counting(self, env, verify_env, chain_attempt, attempt, delay):
        env.job.library_name = "Verify: Show - S01E01.mkv"
        env.job.config.update({"verify": True, **({"chain_attempt": chain_attempt} if chain_attempt else {})})
        verify_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run(verify_env, ["/m/a.mkv"])
        verify_env.create.assert_called_once_with(
            library_name="Retry: Show - S01E01.mkv",
            priority=3,
            source="sonarr",
            file_paths=["/data/tv/a.mkv"],
            item_id_hints={"/data/tv/a.mkv": {"jf-1": "abc"}},
            retry_attempt=attempt,
            retry_delay_s=delay,
            verify_chain=True,
            parent_job_id="j1",
            max_retries=3,
        )

    def test_a_retry_of_a_retry_in_a_verify_chain_stays_in_the_chain(self, env, verify_env):
        env.job.library_name = "Retry: Show - S01E01.mkv"
        env.job.config.update({"verify_chain": True, "retry_attempt": 1})
        verify_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run(verify_env, ["/m/a.mkv"])
        kwargs = verify_env.create.call_args.kwargs
        assert (kwargs["library_name"], kwargs["retry_attempt"], kwargs["verify_chain"]) == (
            "Retry: Show - S01E01.mkv",
            2,
            True,
        )

    def test_a_retry_from_a_verify_job_stops_at_the_retry_count(self, env, verify_env):
        env.job.config.update({"verify": True, "chain_attempt": 3})
        verify_env.results.append(("/m/a.mkv", "markers_waiting", [NOT_IN_LIBRARY_ROW]))
        self._run(verify_env, ["/m/a.mkv"])
        verify_env.create.assert_not_called()

    def test_a_verify_job_doesnt_retry_a_file_gone_from_disk(self, env, verify_env):
        env.job.config["verify"] = True
        verify_env.results.append(("/m/a.mkv", "skipped_file_not_found", []))
        self._run(verify_env, ["/m/a.mkv"])
        verify_env.create.assert_not_called()

    def test_retries_turned_off_turn_the_verify_off_too(self, env, verify_env):
        verify_env.settings["webhook_retry_count"] = 0
        verify_env.results.append(("/m/a.mkv", "markers_published", [self.WRITTEN_LATER]))
        self._run(verify_env)
        verify_env.create.assert_not_called()

    def test_a_file_waiting_on_one_server_gets_its_retry_and_its_verify(self, env, verify_env):
        verify_env.results.append(
            ("/m/a.mkv", "markers_published", [self.WRITTEN_LATER, {**NOT_IN_LIBRARY_ROW, "server_id": "plex-1"}])
        )
        self._run(verify_env, ["/m/a.mkv"])
        assert [c.kwargs.get("verify", False) for c in verify_env.create.call_args_list] == [False, True]
        assert all(c.kwargs["file_paths"] == ["/data/tv/a.mkv"] for c in verify_env.create.call_args_list)

    def test_the_verify_takes_at_most_500_files_and_says_so(self, env, verify_env):
        paths = [f"/m/{i:04d}.mkv" for i in range(501)]
        verify_env.sent = {}
        verify_env.results += [(p, "markers_published", [self.WRITTEN_LATER]) for p in paths]
        self._run(verify_env, paths)
        assert verify_env.create.call_args.kwargs["file_paths"] == paths[:500]
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert "INFO - 1 more replaced file(s) aren't checked again later; the next run for them checks them" in logs

    def test_a_cancelled_job_queues_no_verify(self, env, verify_env):
        verify_env.results.append(("/m/a.mkv", "markers_published", [self.WRITTEN_LATER]))
        env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        self._run(verify_env)
        verify_env.create.assert_not_called()

    @pytest.fixture
    def before_restart(self, env, tmp_path):
        """Files this job published before a restart revived it, with their Files-panel rows (as the JSONL keeps
        them) and the markers store's identity of each."""
        records = {}

        def make(name, *, verify_later):
            path = tmp_path / name
            path.write_bytes(b"x" * 10)
            st = os.stat(path)
            records[str(path)] = SimpleNamespace(id=len(records) + 1, size=st.st_size, mtime_ns=st.st_mtime_ns)
            server = {"id": "jf-1", "status": "markers_written", **({"verify_later": True} if verify_later else {})}
            env.jm.get_file_results.return_value.append(
                {"file": str(path), "outcome": "markers_published", "servers": [server]}
            )
            return str(path)

        env.jm.get_file_results.return_value = []
        env.ctx.store.get_file.side_effect = records.get
        return make

    @pytest.mark.parametrize("others", [False, True], ids=["every-file-carried", "some-files-left"])
    def test_a_revived_job_queues_the_verify_of_replaced_files_it_published_before_the_restart(
        self, env, verify_env, before_restart, others
    ):
        replaced = before_restart("a.mkv", verify_later=True)
        plain = before_restart("b.mkv", verify_later=False)
        paths = [replaced, plain] + (["/m/c.mkv"] if others else [])
        verify_env.sent = {replaced: "/data/tv/a.mkv", plain: "/data/tv/b.mkv"}
        self._run(verify_env, paths)
        assert env.dispatcher.submit_items.called is others
        verify_env.create.assert_called_once()
        kwargs = verify_env.create.call_args.kwargs
        assert (kwargs["library_name"], kwargs["file_paths"], kwargs["verify"]) == (
            "Verify: Show - S01E01.mkv",
            ["/data/tv/a.mkv"],
            True,
        )

    @pytest.mark.parametrize("config", [{"verify": True}, {"source": "manual"}], ids=["verify-job", "manual-job"])
    def test_a_revived_job_that_checks_nothing_later_queues_no_verify_for_carried_files(
        self, env, verify_env, before_restart, config
    ):
        env.job.config.update(config)
        path = before_restart("a.mkv", verify_later=True)
        self._run(verify_env, [path])
        verify_env.create.assert_not_called()


class TestReadBackFailures:
    """Files left "Up to date" because a server's markers couldn't be read back make the job say so (LOW-2)."""

    def test_the_job_warns_per_server_how_many_files_it_couldnt_check(self, env, monkeypatch):
        unread = {"status": "markers_up_to_date", "message": "Up to date", "read_back_failed": True}
        results = [
            ("/m/a.mkv", [{**unread, "server_id": "plex-1", "server_name": "Home Plex"}]),
            (
                "/m/b.mkv",
                [
                    {**unread, "server_id": "plex-1", "server_name": "Home Plex"},
                    {**unread, "server_id": "jf-1", "server_name": "Home Jellyfin"},
                ],
            ),
            ("/m/c.mkv", [{"server_id": "plex-1", "server_name": "Home Plex", "status": "markers_up_to_date"}]),
        ]
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)

        def during_wait(timeout=None):
            for path, rows in results:
                set_cb.call_args_list[0].args[0](path, "markers_up_to_date", "", "Lookup", servers=rows)
            return True

        env.tracker.wait.side_effect = during_wait
        items = [_item(p) for p, _rows in results]
        with patch.object(job_runner, "build_items", return_value=(items, ["Couldn't list X"], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.complete_job.assert_called_once_with(
            "j1",
            warning="Couldn't list X | Couldn't check what 1 file(s) show on Home Jellyfin | "
            "Couldn't check what 2 file(s) show on Home Plex",
        )


class TestBudgetExhaustedCompletionWarning:
    """The job's completion warning includes ``pipeline.budget_exhausted_warnings(ctx)`` (spec finding 4)."""

    _TIDB_WARNING = (
        "TheIntroDB's daily lookup limit was reached: 39 files were checked without it. "
        "It resets at 00:00 UTC; the files it left undecided are checked again automatically after that (or add a "
        "TheIntroDB API key for a higher limit)."
    )

    def test_the_jobs_warning_includes_it(self, env, monkeypatch):
        monkeypatch.setattr(job_runner, "budget_exhausted_warnings", lambda ctx: [self._TIDB_WARNING])
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.complete_job.assert_called_once_with("j1", warning=self._TIDB_WARNING)

    def test_it_is_joined_after_build_items_warnings(self, env, monkeypatch):
        monkeypatch.setattr(job_runner, "budget_exhausted_warnings", lambda ctx: [self._TIDB_WARNING])
        with patch.object(job_runner, "build_items", return_value=([_item()], ["Couldn't list X"], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.complete_job.assert_called_once_with("j1", warning=f"Couldn't list X | {self._TIDB_WARNING}")

    def test_it_is_asked_about_this_jobs_own_context(self, env, monkeypatch):
        seen = []
        monkeypatch.setattr(job_runner, "budget_exhausted_warnings", lambda ctx: seen.append(ctx) or [])
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert seen == [env.ctx]

    def test_no_warning_when_nothing_ran_out(self, env, monkeypatch):
        monkeypatch.setattr(job_runner, "budget_exhausted_warnings", lambda ctx: [])
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.complete_job.assert_called_once_with("j1", warning=None)


class TestClosingLogLines:
    """The job's log ends with the pipeline's closing lines (a Season job's per-season lines, then the totals)."""

    LINES = [
        "Season re-check, Show (2020) S01 (3 episodes): no change",
        "Done: 3 files · 0 sent to Plex · 0 need review · 3 nothing found",
    ]

    def test_they_are_logged_from_the_jobs_counts_before_the_job_completes(self, env):
        order = []
        env.ctx.summary_lines.return_value = self.LINES
        env.jm.add_log.side_effect = lambda job_id, line: order.append((job_id, line))
        env.jm.complete_job.side_effect = lambda job_id, **kwargs: order.append((job_id, "completed"))
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.ctx.summary_lines.assert_called_once_with({"markers_published": 1})
        assert order[-3:] == [("j1", f"INFO - {line}") for line in self.LINES] + [("j1", "completed")]

    def test_a_season_job_that_took_requests_while_running_still_logs_them_then_passes_the_requests_on(self, env):
        order = []
        env.job.config = {"file_paths": ["/m/a.mkv"], "source": "season", job_runner.LATE_REQUESTS: {"/m/b.mkv": 7}}
        env.ctx.summary_lines.return_value = self.LINES
        env.ctx.ran_since.return_value = False  # its own run of b came before the request, or there was none
        env.jm.add_log.side_effect = lambda job_id, line: order.append((job_id, line))
        env.jm.complete_job.side_effect = lambda job_id, **kwargs: order.append((job_id, "completed"))
        with (
            patch.object(job_runner, "build_items", return_value=([_item()], [], {})),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create,
        ):
            job_runner.run_intro_credits_job("j1")
        done = order.index(("j1", "completed"))
        assert order[done - 2 : done + 1] == [("j1", f"INFO - {line}") for line in self.LINES] + [("j1", "completed")]
        assert any("checked again with this job's results" in line for _, line in order[done + 1 :])
        env.ctx.ran_since.assert_called_once_with("/m/b.mkv", 7)
        assert create.call_args.kwargs["file_paths"] == ["/m/b.mkv"]
        assert (create.call_args.kwargs["source"], create.call_args.kwargs["priority"]) == ("season", env.job.priority)

    def test_lines_that_cant_be_made_leave_the_job_completed(self, env):
        env.ctx.summary_lines.side_effect = RuntimeError("boom")
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize(
        ("source", "season_recheck", "label"),
        [
            ("season", True, "Season re-check"),
            ("theintrodb_recheck", True, "TheIntroDB recheck"),
            ("sonarr", False, "Season re-check"),
            ("manual", False, "Season re-check"),
            (None, False, "Season re-check"),
        ],
    )
    def test_only_a_season_job_or_a_theintrodb_recheck_logs_one_line_per_season(
        self, env, source, season_recheck, label
    ):
        env.job.config = {"libraries": [], "source": source}
        env.ctx.store.get_decisions.return_value = {}  # a recheck job's file is still undecided
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.build_context.call_args.kwargs["season_recheck"] is season_recheck
        assert env.build_context.call_args.kwargs["recheck_label"] == label


class TestTheIntroDbBudgetRecheck:
    """Files TheIntroDB's used-up daily budget refused, left undecided, are checked again just after the 00:00 UTC
    reset by one waiting LOW job (production: 198 such files in 25 jobs were never asked again)."""

    REFUSED_AT = datetime(2026, 9, 24, 4, 42, tzinfo=UTC)
    NOW = datetime(2026, 9, 24, 5, 0, tzinfo=UTC)
    DUE = datetime(2026, 9, 25, 0, 5, tzinfo=UTC)

    @pytest.fixture
    def jm(self, monkeypatch):
        jm = MagicMock()
        jm.get_pending_jobs.return_value = []
        jm.update_job_config_if_pending.return_value = True
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
        monkeypatch.setattr(job_runner, "_utcnow", lambda: self.NOW)
        return jm

    @pytest.fixture
    def create(self):
        with patch(
            "media_preview_generator.markers.triggers.create_intro_credits_job", return_value=MagicMock(id="r1234567")
        ) as create:
            yield create

    def _ctx(self, paths, *, enabled=True, refused_at=REFUSED_AT):
        return SimpleNamespace(
            take_budget_rechecks=lambda: (list(paths), refused_at if paths else None),
            settings=SimpleNamespace(source_enabled=lambda source_id: enabled and source_id == "theintrodb"),
        )

    @staticmethod
    def _waiting(files, **config):
        return MagicMock(
            id="w7654321",
            kind=JOB_KIND_INTRO_CREDITS,
            config={
                "source": job_runner.BUDGET_RECHECK_SOURCE,
                "file_paths": list(files),
                "retry_not_before": "2026-09-25T00:05:00+00:00",
                **config,
            },
        )

    @pytest.mark.parametrize(
        ("refused_at", "due"),
        [
            (datetime(2026, 9, 24, 4, 42, tzinfo=UTC), datetime(2026, 9, 25, 0, 5, tzinfo=UTC)),
            (datetime(2026, 9, 24, 0, 0, tzinfo=UTC), datetime(2026, 9, 25, 0, 5, tzinfo=UTC)),
            (datetime(2026, 9, 24, 23, 59, 59, tzinfo=UTC), datetime(2026, 9, 25, 0, 5, tzinfo=UTC)),
            (datetime(2026, 9, 30, 12, 0, tzinfo=UTC), datetime(2026, 10, 1, 0, 5, tzinfo=UTC)),
        ],
    )
    def test_due_just_after_the_next_utc_day_roll(self, refused_at, due):
        assert job_runner.budget_recheck_due(refused_at) == due

    def test_queues_one_low_job_due_after_the_reset(self, jm, create):
        job_runner._queue_budget_recheck(MagicMock(id="j1"), self._ctx(["/m/a.mkv", "/m/b.mkv"]))

        kwargs = create.call_args.kwargs
        assert kwargs["source"] == job_runner.BUDGET_RECHECK_SOURCE
        assert kwargs["priority"] == job_runner.PRIORITY_LOW
        assert kwargs["file_paths"] == ["/m/a.mkv", "/m/b.mkv"]
        assert kwargs["retry_delay_s"] == int((self.DUE - self.NOW).total_seconds())
        assert kwargs["library_name"] == "TheIntroDB recheck: 2 files"
        jm.add_log.assert_any_call(
            "j1",
            "INFO - 2 file(s) checked without TheIntroDB (daily limit reached) are checked again after it resets at "
            "00:00 UTC (job r1234567)",
        )

    def test_a_job_finishing_after_the_reset_queues_a_recheck_due_now(self, jm, create, monkeypatch):
        monkeypatch.setattr(job_runner, "_utcnow", lambda: self.DUE + timedelta(hours=1))
        job_runner._queue_budget_recheck(MagicMock(id="j1"), self._ctx(["/m/a.mkv"]))
        assert create.call_args.kwargs["retry_delay_s"] == 0

    def test_files_join_the_waiting_recheck_job(self, jm, create):
        waiting = self._waiting(["/m/a.mkv"])
        jm.get_pending_jobs.return_value = [waiting]

        job_runner._queue_budget_recheck(MagicMock(id="j1"), self._ctx(["/m/a.mkv", "/m/b.mkv"]))

        create.assert_not_called()
        jm.update_job_config_if_pending.assert_called_once_with(
            "w7654321", {**waiting.config, "file_paths": ["/m/a.mkv", "/m/b.mkv"]}
        )
        jm.update_job_library_name.assert_called_once_with("w7654321", "TheIntroDB recheck: 2 files")

    @pytest.mark.parametrize(
        "waiting",
        [
            MagicMock(
                id="w1",
                kind=JOB_KIND_INTRO_CREDITS,
                config={
                    "source": "theintrodb_recheck",
                    "file_paths": ["/m/x.mkv"],
                    "files_sealed": True,
                    "retry_not_before": "2026-09-25T00:05:00+00:00",
                },
            ),
            MagicMock(
                id="w2",
                kind=JOB_KIND_INTRO_CREDITS,
                config={
                    "source": "sonarr",
                    "file_paths": ["/m/x.mkv"],
                    "retry_not_before": "2026-09-25T00:05:00+00:00",
                },
            ),
            MagicMock(
                id="w3",
                kind="previews",
                config={
                    "source": "theintrodb_recheck",
                    "file_paths": ["/m/x.mkv"],
                    "retry_not_before": "2026-09-25T00:05:00+00:00",
                },
            ),
            # Due at today's reset and only waiting for a slot: it runs before the budget resets again.
            MagicMock(
                id="w4",
                kind=JOB_KIND_INTRO_CREDITS,
                config={
                    "source": "theintrodb_recheck",
                    "file_paths": ["/m/x.mkv"],
                    "retry_not_before": "2026-09-24T00:05:00+00:00",
                },
            ),
            MagicMock(
                id="w5",
                kind=JOB_KIND_INTRO_CREDITS,
                config={"source": "theintrodb_recheck", "file_paths": ["/m/x.mkv"]},
            ),
        ],
        ids=["sealed", "another-source", "another-kind", "already-due", "no-due-time"],
    )
    def test_a_job_that_cant_take_files_gets_a_new_recheck_job(self, jm, create, waiting):
        jm.get_pending_jobs.return_value = [waiting]
        job_runner._queue_budget_recheck(MagicMock(id="j1"), self._ctx(["/m/a.mkv"]))
        jm.update_job_config_if_pending.assert_not_called()
        assert create.call_args.kwargs["file_paths"] == ["/m/a.mkv"]

    def test_a_waiting_job_cancelled_meanwhile_gets_a_new_recheck_job(self, jm, create):
        jm.get_pending_jobs.return_value = [self._waiting(["/m/x.mkv"])]
        jm.update_job_config_if_pending.return_value = False
        job_runner._queue_budget_recheck(MagicMock(id="j1"), self._ctx(["/m/a.mkv"]))
        assert create.call_args.kwargs["file_paths"] == ["/m/a.mkv"]

    def test_the_waiting_job_lists_at_most_max_retry_files(self, jm, create):
        listed = [f"/m/{n}.mkv" for n in range(job_runner.MAX_RETRY_FILES - 1)]
        jm.get_pending_jobs.return_value = [self._waiting(listed)]

        job_runner._queue_budget_recheck(MagicMock(id="j1"), self._ctx(["/m/new1.mkv", "/m/new2.mkv", "/m/new3.mkv"]))

        files = jm.update_job_config_if_pending.call_args.args[1]["file_paths"]
        assert files == [*listed, "/m/new1.mkv"]
        jm.add_log.assert_any_call(
            "j1",
            "INFO - 2 more file(s) checked without TheIntroDB aren't checked again automatically; the next run of "
            "their library checks them",
        )

    @pytest.mark.parametrize(("paths", "enabled"), [([], True), (["/m/a.mkv"], False)], ids=["no-files", "tidb-off"])
    def test_nothing_is_queued(self, jm, create, paths, enabled):
        job_runner._queue_budget_recheck(MagicMock(id="j1"), self._ctx(paths, enabled=enabled))
        create.assert_not_called()
        jm.update_job_config_if_pending.assert_not_called()

    def test_a_finished_job_hands_its_context_over(self, env, monkeypatch):
        queue = MagicMock()
        monkeypatch.setattr(job_runner, "_queue_budget_recheck", queue)
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        queue.assert_called_once_with(env.job, env.ctx)

    def test_a_cancelled_job_queues_no_recheck(self, env, monkeypatch):
        queue = MagicMock()
        monkeypatch.setattr(job_runner, "_queue_budget_recheck", queue)
        env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        queue.assert_not_called()

    def _decisions(self, env, by_path):
        env.ctx.store.get_file.side_effect = lambda path: SimpleNamespace(id=path) if path in by_path else None
        env.ctx.store.get_decisions.side_effect = lambda file_id: {
            n: SimpleNamespace(status=DecisionStatus(status)) for n, status in enumerate(by_path[file_id])
        }

    def test_the_recheck_job_runs_only_files_still_undecided(self, env):
        env.job.config = {
            "source": job_runner.BUDGET_RECHECK_SOURCE,
            "file_paths": ["/m/a.mkv", "/m/b.mkv", "/m/c.mkv"],
        }
        env.ctx.settings.source_enabled.side_effect = lambda source_id: source_id == "theintrodb"
        self._decisions(env, {"/m/a.mkv": ["decided", "needs_review"], "/m/b.mkv": ["decided", "disabled"]})
        items = [_item("/m/a.mkv"), _item("/m/b.mkv"), _item("/m/c.mkv")]
        with patch.object(job_runner, "build_items", return_value=(items, [], {})):
            job_runner.run_intro_credits_job("j1")

        submitted = [i.canonical_path for i in env.dispatcher.submit_items.call_args.kwargs["items"]]
        assert submitted == ["/m/a.mkv", "/m/c.mkv"]  # c: no decisions stored yet
        env.jm.add_log.assert_any_call("j1", "INFO - 1 file(s) were decided since; they aren't checked again")

    def test_the_recheck_job_runs_nothing_once_theintrodb_is_off(self, env):
        env.job.config = {"source": job_runner.BUDGET_RECHECK_SOURCE, "file_paths": ["/m/a.mkv"]}
        env.ctx.settings.source_enabled.return_value = False
        with patch.object(job_runner, "build_items", return_value=([_item("/m/a.mkv")], [], {})):
            job_runner.run_intro_credits_job("j1")

        env.dispatcher.submit_items.assert_not_called()
        env.jm.add_log.assert_any_call("j1", "INFO - TheIntroDB is turned off now, so these files aren't checked again")
        env.jm.complete_job.assert_called_once_with("j1", warning="No files to check.")

    def test_the_recheck_job_seals_its_files_when_it_reads_them(self, env):
        env.job.config = {"source": job_runner.BUDGET_RECHECK_SOURCE, "file_paths": ["/m/a.mkv"]}
        with patch.object(job_runner, "build_items", return_value=([], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.merge_job_config.assert_any_call("j1", {job_runner.FILES_SEALED: True})

    def test_other_jobs_are_not_filtered(self, env):
        env.job.config = {"source": "sonarr", "file_paths": ["/m/a.mkv"]}
        self._decisions(env, {"/m/a.mkv": ["decided"]})
        with patch.object(job_runner, "build_items", return_value=([_item("/m/a.mkv")], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert [i.canonical_path for i in env.dispatcher.submit_items.call_args.kwargs["items"]] == ["/m/a.mkv"]


class TestRetryCarriesTheSenderPath:
    """A retry resolves the path the sender gave again, so it finds the disk the file landed on (pre-lab MED-1)."""

    RAW = "/data/tv/Show/Season 01/Show - S01E01.mkv"
    TAIL = ("Show", "Season 01", "Show - S01E01.mkv")

    @pytest.fixture
    def disks(self, tmp_path, env, monkeypatch):
        from media_preview_generator.markers import triggers
        from media_preview_generator.servers.base import ServerConfig

        for disk in ("disk1", "disk2"):
            (tmp_path / disk / "tv").mkdir(parents=True)
        # One "TV Shows" library spread over two disks, both reached from Sonarr's /data/tv.
        cfg = ServerConfig(
            id="plex-1",
            type=ServerType.PLEX,
            name="Plex",
            enabled=True,
            url="http://plex",
            auth={},
            libraries=[Library("2", "TV Shows", ("/tv",), enabled=True)],
            path_mappings=[
                {"remote_prefix": "/tv", "local_prefix": str(tmp_path / d / "tv"), "webhook_prefixes": ["/data/tv"]}
                for d in ("disk1", "disk2")
            ],
            markers={"enabled": True, "library_ids": None, "plex": {"db_write_confirmed_at": "x"}},
        )
        reg = FakeRegistry({"plex-1": cfg})
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda config: reg)
        settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
        env.sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        env.job.library_name = "Show - S01E01.mkv"
        create = MagicMock(return_value=MagicMock(id="retry-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)
        return SimpleNamespace(tmp=tmp_path, registry=reg, create=create, set_cb=set_cb)

    def _local(self, disks, disk):
        return str(disks.tmp.joinpath(disk, "tv", *self.TAIL))

    @pytest.mark.parametrize(
        ("result", "on_disk"),
        [
            (("skipped_file_not_found", []), None),  # Sonarr's import is still copying to one of the disks
            (("markers_waiting", [NOT_IN_LIBRARY_ROW]), "disk2"),  # on disk2, the server hasn't scanned it yet
        ],
        ids=["not-on-disk", "not-indexed"],
    )
    @pytest.mark.parametrize(
        ("source", "hints"), [("sonarr", {}), ("jellyfin", {"jf-1": "abc"})], ids=["no-hints", "vendor-hints"]
    )
    def test_retry_gets_the_sender_path_and_its_hints(self, env, disks, result, on_disk, source, hints):
        if on_disk:
            Path(self._local(disks, on_disk)).parent.mkdir(parents=True)
            Path(self._local(disks, on_disk)).write_bytes(b"x")
        canonical = self._local(disks, on_disk or "disk1")
        env.job.config = {
            "libraries": [],
            "file_paths": [self.RAW],
            "webhook_item_id_hints": {self.RAW: hints} if hints else {},
            "source": source,
        }

        def during_wait(timeout=None):
            disks.set_cb.call_args_list[0].args[0](canonical, result[0], "", "Lookup", servers=result[1])
            return True

        env.tracker.wait.side_effect = during_wait
        job_runner.run_intro_credits_job("j1")

        assert [i.canonical_path for i in env.dispatcher.submit_items.call_args.kwargs["items"]] == [canonical]
        disks.create.assert_called_once_with(
            library_name="Retry: Show - S01E01.mkv",
            priority=3,
            source=source,
            file_paths=[self.RAW],
            item_id_hints={self.RAW: hints} if hints else None,
            retry_attempt=1,
            retry_delay_s=60,
            verify_chain=False,
            parent_job_id="j1",
            max_retries=3,
        )

        # Sonarr's copy lands on the second disk before the retry runs: the retry reads it there.
        landed = Path(self._local(disks, "disk2"))
        landed.parent.mkdir(parents=True, exist_ok=True)
        landed.write_bytes(b"x")
        kwargs = disks.create.call_args.kwargs
        retry_config = {"file_paths": kwargs["file_paths"], "webhook_item_id_hints": kwargs["item_id_hints"] or {}}
        items, _warnings, _sent = job_runner.build_items(retry_config, registry=disks.registry)
        assert [(i.canonical_path, i.item_id_by_server) for i in items] == [(str(landed), hints)]

    def test_files_from_a_library_listing_are_retried_by_their_local_path(self, env, disks):
        local = self._local(disks, "disk2")
        env.job.config = {"libraries": [], "file_paths": [], "source": "schedule"}

        def during_wait(timeout=None):
            disks.set_cb.call_args_list[0].args[0](local, "markers_waiting", "", "Lookup", servers=[NOT_IN_LIBRARY_ROW])
            return True

        env.tracker.wait.side_effect = during_wait
        with patch.object(job_runner, "build_items", return_value=([_item(local)], [], {})):
            job_runner.run_intro_credits_job("j1")

        assert disks.create.call_args.kwargs["file_paths"] == [local]
        assert disks.create.call_args.kwargs["item_id_hints"] is None

    def test_build_items_maps_each_local_path_to_the_path_it_was_sent_as(self, disks):
        (disks.tmp / "other.mkv").write_bytes(b"x")
        local_pick = str(disks.tmp / "other.mkv")

        items, _warnings, sent = job_runner.build_items({"file_paths": [self.RAW, local_pick]}, registry=disks.registry)

        assert sent == {self._local(disks, "disk1"): self.RAW, local_pick: local_pick}
        assert sorted(i.canonical_path for i in items) == sorted(sent)
        _items, _warnings, listed = job_runner.build_items({}, registry=disks.registry)
        assert listed == {}


class TestRetryWait:
    """A retry job waits out its delay before the gate: no slot, cancellable."""

    def _clock(self, monkeypatch, start):
        now = {"t": start}
        monkeypatch.setattr(job_runner, "_utcnow", lambda: now["t"])
        return now

    @pytest.mark.parametrize(
        ("kind", "waiting_for"),
        [
            ({"retry_attempt": 2}, "Retry starting in 120s — waiting for these files to appear on disk or on a server"),
            (
                {"verify": True},
                "Check starting in 120s — servers often rescan a replaced file after its markers are sent",
            ),
            (
                {"source": job_runner.BUDGET_RECHECK_SOURCE},
                "Check starting in 120s — after TheIntroDB's daily limit resets at 00:00 UTC",
            ),
        ],
        ids=["retry", "verify", "theintrodb-recheck"],
    )
    def test_waits_until_the_retry_is_due_without_a_slot(self, env, monkeypatch, kind, waiting_for):
        from datetime import datetime, timedelta

        start = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        now = self._clock(monkeypatch, start)
        due = start + timedelta(seconds=120)
        env.job.config = {
            "file_paths": ["/m/a.mkv"],
            **kind,
            "retry_delay": 120,
            "retry_not_before": due.isoformat(),
        }
        env.ctx.store.get_decisions.return_value = {}  # a recheck job's file is still undecided
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            env.gate.acquire.assert_not_called()
            now["t"] += timedelta(seconds=60)

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert len(sleeps) == 2
        env.gate.acquire.assert_called_once()
        first = env.jm.update_progress.call_args_list[0].kwargs
        assert first["retry_eta"] == due.isoformat() and first["retry_wait_total"] == 120
        assert first["current_item"] == waiting_for
        env.jm.update_progress.assert_any_call("j1", retry_eta=None)
        env.dispatcher.submit_items.assert_called_once()

    @pytest.mark.parametrize("not_before", ["2026-09-14T09:00:00+00:00", "not a time", None])
    def test_due_or_unreadable_retry_time_starts_straight_away(self, env, monkeypatch, not_before):
        from datetime import datetime

        self._clock(monkeypatch, datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        env.job.config = {"file_paths": ["/m/a.mkv"], "retry_attempt": 1, "retry_not_before": not_before}
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_called_once()
        assert not any("retry_eta" in c.kwargs for c in env.jm.update_progress.call_args_list)

    def test_retry_now_on_the_chain_head_skips_the_rest_of_the_wait(self, env, monkeypatch):
        # POST /api/jobs/<head>/retry-now flags the chain's pending retry, as it does a preview retry.
        from datetime import datetime, timedelta

        start = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        self._clock(monkeypatch, start)
        env.job.config = {
            "file_paths": ["/m/a.mkv"],
            "retry_attempt": 1,
            "retry_not_before": (start + timedelta(hours=1)).isoformat(),
        }
        sleeps = []

        def fake_sleep(_seconds):
            sleeps.append(1)
            env.job.config["force_fire_now"] = True

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert sleeps == [1]
        env.jm.add_log.assert_any_call("j1", "INFO - Retry backoff skipped — operator forced fire-now")
        env.dispatcher.submit_items.assert_called_once()

    def test_cancelled_while_waiting_never_takes_a_slot(self, env, monkeypatch):
        from datetime import datetime, timedelta

        start = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)
        self._clock(monkeypatch, start)
        env.job.config = {
            "file_paths": ["/m/a.mkv"],
            "retry_attempt": 1,
            "retry_not_before": (start + timedelta(hours=1)).isoformat(),
        }

        def fake_sleep(_seconds):
            env.jm.is_cancellation_requested.return_value = True

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        job_runner.run_intro_credits_job("j1")
        env.jm.cancel_job.assert_called_once_with("j1")
        env.gate.acquire.assert_not_called()
        env.dispatcher.submit_items.assert_not_called()


class TestLeftoverJobsAfterRestart:
    """Interrupted Intro & Credits jobs that no thread will run must not block their schedule or absorb webhooks."""

    SERVERS = [
        {
            "id": "jf-1",
            "type": "jellyfin",
            "name": "JF",
            "enabled": True,
            "markers": {"enabled": True},
            "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/media/tv"], "enabled": True}],
        }
    ]

    def _boot(self, tmp_path, monkeypatch, *, auto_requeue, age_hours):
        from datetime import datetime, timedelta

        from media_preview_generator.web import app as app_mod
        from media_preview_generator.web.jobs import JobManager

        config_dir = str(tmp_path)
        before = JobManager(config_dir=config_dir)
        scheduled = before.create_job(
            library_name="weekly",
            kind=JOB_KIND_INTRO_CREDITS,
            priority=3,
            parent_schedule_id="sched-1",
            config={"kind": JOB_KIND_INTRO_CREDITS, "libraries": [], "file_paths": []},
        )
        follow_up = before.create_job(
            library_name="follow-up",
            kind=JOB_KIND_INTRO_CREDITS,
            priority=2,
            config={"kind": JOB_KIND_INTRO_CREDITS, "file_paths": ["/media/tv/S/X.mkv"], "follows_job_id": "p"},
        )
        preview = before.create_job(library_name="preview", config={"webhook_paths": ["/media/tv/S/X.mkv"]})

        after = JobManager(config_dir=config_dir)
        # jobs.db never rewrites created_at, so age the loaded rows the way a long wait in the queue would.
        stamp = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
        for job in after.get_all_jobs():
            job.created_at = stamp
        monkeypatch.setattr(app_mod, "get_job_manager", lambda: after)
        settings = {
            "auto_requeue_on_restart": auto_requeue,
            "requeue_max_age_minutes": 720,
            "media_servers": self.SERVERS,
        }
        sm = MagicMock(processing_paused=False)
        sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        monkeypatch.setattr("media_preview_generator.web.settings_manager.get_settings_manager", lambda *a: sm)
        with patch("media_preview_generator.web.routes._start_job_async") as start:
            app_mod._requeue_interrupted_on_startup(config_dir)
        return after, sm, start, (scheduled.id, follow_up.id, preview.id)

    @pytest.mark.parametrize(("auto_requeue", "age_hours"), [(False, 0), (True, 13)], ids=["requeue-off", "too-old"])
    def test_unrevived_jobs_are_failed_so_schedules_and_webhooks_start_new_ones(
        self, tmp_path, monkeypatch, auto_requeue, age_hours
    ):
        from media_preview_generator.markers import triggers
        from media_preview_generator.web.jobs import JobStatus
        from media_preview_generator.web.scheduler import _start_scheduled_intro_credits_job

        after, sm, start, (scheduled_id, follow_up_id, preview_id) = self._boot(
            tmp_path, monkeypatch, auto_requeue=auto_requeue, age_hours=age_hours
        )

        start.assert_not_called()
        for job_id in (scheduled_id, follow_up_id):
            job = after.get_job(job_id)
            assert job.status is JobStatus.FAILED
            assert job.error == "Interrupted by a restart and not resumed"
            assert job.completed_at
        assert after.get_job(preview_id).status is JobStatus.PENDING  # preview jobs keep today's behaviour
        stored = type(after)(config_dir=str(tmp_path)).get_job(scheduled_id)
        assert stored.status is JobStatus.FAILED

        create = MagicMock()
        with (
            patch("media_preview_generator.web.jobs.get_job_manager", lambda *a, **k: after),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job", create),
        ):
            _start_scheduled_intro_credits_job(MagicMock(), "sched-1", [], "TV", None, None)
        create.assert_called_once()

        with (
            patch.object(triggers, "get_settings_manager", lambda: sm),
            patch.object(triggers, "get_job_manager", lambda: after),
            patch.object(triggers, "start_intro_credits_job_async"),
        ):
            new_follow_up = triggers.submit_webhook_follow_up(
                preview_job_id="p2", paths=["/media/tv/S/X.mkv"], source="sonarr"
            )
        assert new_follow_up is not None

    def test_revived_jobs_are_not_failed(self, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobStatus

        after, _sm, start, (scheduled_id, follow_up_id, preview_id) = self._boot(
            tmp_path, monkeypatch, auto_requeue=True, age_hours=0
        )
        assert {c.args[0] for c in start.call_args_list} == {scheduled_id, follow_up_id, preview_id}
        assert all(after.get_job(j).status is JobStatus.PENDING for j in (scheduled_id, follow_up_id, preview_id))


class TestStartAsync:
    def test_starts_one_daemon_thread_and_forgets_the_job_when_it_ends(self, monkeypatch):
        runs = []
        monkeypatch.setattr(job_runner, "run_intro_credits_job", runs.append)
        job_runner.start_intro_credits_job_async("start-1")
        assert runs == ["start-1"]
        assert "start-1" not in job_runner._inflight_jobs

    def test_thread_is_named_and_daemon(self, monkeypatch):
        monkeypatch.setattr(job_runner, "run_intro_credits_job", lambda jid: None)
        with patch.object(job_runner, "threading") as threading_mod:
            job_runner.start_intro_credits_job_async("start-2")
        job_runner._inflight_jobs.discard("start-2")
        kwargs = threading_mod.Thread.call_args.kwargs
        assert kwargs["daemon"] is True and "start-2" in kwargs["name"]
        threading_mod.Thread.return_value.start.assert_called_once_with()

    def test_duplicate_start_while_in_flight_is_ignored(self, monkeypatch):
        monkeypatch.setattr(job_runner, "run_intro_credits_job", MagicMock(side_effect=AssertionError("ran twice")))
        with job_runner._inflight_lock:
            job_runner._inflight_jobs.add("start-3")
        try:
            with patch.object(job_runner, "threading") as threading_mod:
                job_runner.start_intro_credits_job_async("start-3")
            threading_mod.Thread.assert_not_called()
        finally:
            job_runner._inflight_jobs.discard("start-3")

    def test_a_crashing_run_still_forgets_the_job(self, monkeypatch):
        monkeypatch.setattr(job_runner, "run_intro_credits_job", MagicMock(side_effect=RuntimeError("boom")))
        with pytest.raises(RuntimeError):
            job_runner.start_intro_credits_job_async("start-4")
        assert "start-4" not in job_runner._inflight_jobs

    @pytest.mark.parametrize(
        ("overrides", "updated"),
        [
            (None, None),
            ({}, None),
            ({"force": False}, None),
            ({"force": True, "libraries": []}, {"force": True}),
            ({"retry_not_before": "t"}, {"retry_not_before": "t"}),
        ],
    )
    def test_config_overrides_are_merged_only_when_they_change_something(self, monkeypatch, overrides, updated):
        jm = MagicMock()
        jm.get_job.return_value = MagicMock(config={"force": False, "libraries": []})
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
        monkeypatch.setattr(job_runner, "run_intro_credits_job", lambda jid: None)
        job_runner.start_intro_credits_job_async("start-5", overrides)
        jm.update_job_config.assert_not_called()
        if updated is None:
            jm.merge_job_config.assert_not_called()
        else:
            jm.merge_job_config.assert_called_once_with("start-5", updated)

    def test_a_key_another_thread_writes_while_the_overrides_merge_is_kept(self, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import PAUSED_BY_SCHEDULE, JobManager

        jm = JobManager(config_dir=str(tmp_path))
        job = jm.create_job(library_name="A", kind=JOB_KIND_INTRO_CREDITS, config={"libraries": [], "force": False})
        jm.start_job(job.id)
        # A stop tick between the resume's read of the config and its write (either writer).
        for writer in ("update_job_config", "merge_job_config"):
            real = getattr(jm, writer)

            def pause_then_write(*args, _real=real, **kwargs):
                jm.request_pause(job.id, by_schedule=True)
                return _real(*args, **kwargs)

            monkeypatch.setattr(jm, writer, pause_then_write)
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
        monkeypatch.setattr(job_runner, "run_intro_credits_job", lambda jid: None)
        job_runner.start_intro_credits_job_async(job.id, {"libraries": [], "force": True})
        assert jm.get_job(job.id).config == {"libraries": [], "force": True, PAUSED_BY_SCHEDULE: True}


@pytest.mark.parametrize(("kind", "delegated"), [(JOB_KIND_INTRO_CREDITS, True), (JOB_KIND_PREVIEWS, False)])
def test_start_job_async_delegates_by_kind(kind, delegated, monkeypatch):
    from media_preview_generator.web.routes import job_runner as preview_runner

    jm = MagicMock()
    jm.get_job.return_value = MagicMock(kind=kind, config={})
    monkeypatch.setattr(preview_runner, "get_job_manager", lambda: jm)
    job_id = f"job-delegation-{kind}"
    with (
        patch("media_preview_generator.markers.job_runner.start_intro_credits_job_async") as start,
        patch.object(preview_runner, "threading") as threading_mod,
    ):
        preview_runner._start_job_async(job_id, {"a": 1})
    preview_runner._inflight_jobs.discard(job_id)
    if delegated:
        start.assert_called_once_with(job_id, {"a": 1})
        threading_mod.Thread.assert_not_called()
    else:
        start.assert_not_called()
        threading_mod.Thread.assert_called_once()
        threading_mod.Thread.return_value.start.assert_called_once()


@pytest.mark.parametrize("lookup", [None, RuntimeError("jobs.db locked")])
def test_start_job_async_falls_back_to_the_preview_thread_when_the_job_cant_be_read(lookup, monkeypatch):
    from media_preview_generator.web.routes import job_runner as preview_runner

    jm = MagicMock()
    if isinstance(lookup, Exception):
        jm.get_job.side_effect = lookup
    else:
        jm.get_job.return_value = lookup
    monkeypatch.setattr(preview_runner, "get_job_manager", lambda: jm)
    job_id = f"job-delegation-fallback-{type(lookup).__name__}"
    with (
        patch("media_preview_generator.markers.job_runner.start_intro_credits_job_async") as start,
        patch.object(preview_runner, "threading") as threading_mod,
    ):
        preview_runner._start_job_async(job_id, None)
    preview_runner._inflight_jobs.discard(job_id)
    start.assert_not_called()
    threading_mod.Thread.assert_called_once()


class TestInReviewJob:
    """The one job after settings v16 (``triggers.submit_decide_again``): an ordinary Intro & Credits job over the files
    in Needs review, and those waiting for their item's other versions, when it runs."""

    CONFIG = {
        "kind": JOB_KIND_INTRO_CREDITS,
        "source": job_runner.DECIDE_AGAIN_SOURCE,
        "libraries": [],
        "file_paths": [],
        job_runner.DECIDE_AGAIN: True,
    }

    @staticmethod
    def _ends(env, status):
        from media_preview_generator.web.jobs import JobStatus

        env.jm.complete_job.side_effect = lambda *args, **kwargs: setattr(env.job, "status", JobStatus[status])

    def test_it_lists_the_files_in_review_and_waiting_and_runs_them_as_any_job(self, env):
        env.job.config = dict(self.CONFIG)
        env.ctx.store.files_in_review.return_value = ["/tv/B/S01/e2.mkv", "/movies/A/a.mkv", "/tv/B/S01/e1.mkv"]
        env.ctx.store.files_waiting_for_other_versions.return_value = ["/tv/B/S01/e1.mkv", "/movies/C/c - 4K.mkv"]
        with patch.object(job_runner, "build_items") as build:
            job_runner.run_intro_credits_job("j1")
        build.assert_not_called()
        ctx_kwargs = env.build_context.call_args.kwargs
        # Only its log is grouped; it asks and reads what is due, as any job does.
        assert (ctx_kwargs["decide_again"], ctx_kwargs["force"]) == (True, False)
        items = env.dispatcher.submit_items.call_args.kwargs["items"]
        # Season folder, then path, as build_items orders a job's files; no item id hints (each is looked up again).
        assert [(i.canonical_path, i.item_id_by_server) for i in items] == [
            ("/movies/A/a.mkv", {}),
            ("/movies/C/c - 4K.mkv", {}),
            ("/tv/B/S01/e1.mkv", {}),
            ("/tv/B/S01/e2.mkv", {}),
        ]
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize(("status", "cleared"), [("COMPLETED", True), ("FAILED", False)])
    def test_the_upgrades_request_is_cleared_only_once_the_job_completes(self, env, status, cleared):
        from media_preview_generator.upgrade import DECIDE_AGAIN_KEY

        env.job.config = dict(self.CONFIG)
        env.ctx.store.files_in_review.return_value = ["/m/a.mkv"]
        self._ends(env, status)
        job_runner.run_intro_credits_job("j1")
        assert env.sm.delete.call_args_list == ([call(DECIDE_AGAIN_KEY)] if cleared else [])

    def test_with_nothing_in_review_it_completes_without_a_warning_and_clears_the_request(self, env):
        from media_preview_generator.upgrade import DECIDE_AGAIN_KEY

        env.job.config = dict(self.CONFIG)
        env.ctx.store.files_in_review.return_value = []
        self._ends(env, "COMPLETED")
        job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.complete_job.assert_called_once_with("j1")
        env.sm.delete.assert_called_once_with(DECIDE_AGAIN_KEY)

    def test_any_other_job_lists_its_own_files_and_leaves_the_request(self, env):
        self._ends(env, "COMPLETED")
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.build_context.call_args.kwargs["decide_again"] is False
        env.ctx.store.files_in_review.assert_not_called()
        env.sm.delete.assert_not_called()

    def test_its_retry_is_an_ordinary_retry_and_a_file_off_disk_gets_none(self, env, monkeypatch):
        from media_preview_generator.markers import triggers

        settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
        env.sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        env.job.config = dict(self.CONFIG)
        env.job.library_name = triggers.DECIDE_AGAIN_JOB_NAME
        env.ctx.store.files_in_review.return_value = ["/m/a.mkv", "/m/b.mkv"]
        create = MagicMock(return_value=MagicMock(id="retry-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)

        def during_wait(timeout=None):
            callback = set_cb.call_args_list[0].args[0]
            callback("/m/a.mkv", "markers_waiting", "", "Lookup", servers=[NOT_IN_LIBRARY_ROW])
            callback("/m/b.mkv", "skipped_file_not_found", "", "Lookup", servers=[])
            return True

        env.tracker.wait.side_effect = during_wait
        job_runner.run_intro_credits_job("j1")
        create.assert_called_once_with(
            library_name=f"Retry: {triggers.DECIDE_AGAIN_JOB_NAME}",
            priority=3,
            source=job_runner.DECIDE_AGAIN_SOURCE,
            file_paths=["/m/a.mkv"],
            item_id_hints=None,
            retry_attempt=1,
            retry_delay_s=60,
            verify_chain=False,
            parent_job_id="j1",
            max_retries=3,
        )


class TestOnlineRecheckJob:
    """The weekly job (``triggers.submit_online_recheck``): an ordinary Intro & Credits job over the files whose "no
    entry" from an online database is due again, listed when it runs."""

    CONFIG = {
        "kind": JOB_KIND_INTRO_CREDITS,
        "source": job_runner.ONLINE_RECHECK_SOURCE,
        "libraries": [],
        "file_paths": [],
        job_runner.ONLINE_RECHECK: True,
    }

    def test_it_lists_the_due_files_and_runs_them_as_any_job(self, env, monkeypatch):
        env.job.config = dict(self.CONFIG)
        listed = MagicMock(return_value=["/tv/B/S01/e2.mkv", "/movies/A/a.mkv", "/tv/B/S01/e1.mkv"])
        monkeypatch.setattr(job_runner, "online_recheck_files", listed)
        with patch.object(job_runner, "build_items") as build:
            job_runner.run_intro_credits_job("j1")
        build.assert_not_called()
        # Listed with the job's own settings and clock, when it runs.
        listed.assert_called_once_with(env.ctx.store, env.ctx.settings, env.ctx.now.return_value)
        ctx_kwargs = env.build_context.call_args.kwargs
        # Only its log is grouped: it asks and reads only what is due, as any job does.
        assert (ctx_kwargs["online_recheck"], ctx_kwargs["force"], ctx_kwargs["decide_again"]) == (True, False, False)
        assert ctx_kwargs["season_recheck"] is False
        items = env.dispatcher.submit_items.call_args.kwargs["items"]
        assert [(i.canonical_path, i.item_id_by_server) for i in items] == [
            ("/movies/A/a.mkv", {}),
            ("/tv/B/S01/e1.mkv", {}),
            ("/tv/B/S01/e2.mkv", {}),
        ]
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    def test_a_cancel_stops_the_listing_and_the_job(self, env, monkeypatch):
        env.job.config = dict(self.CONFIG)

        def listed(store, settings, now):
            yield "/m/a.mkv"
            env.jm.is_cancellation_requested.return_value = True
            yield "/m/b.mkv"
            raise AssertionError("listed on after the cancel")

        monkeypatch.setattr(job_runner, "online_recheck_files", listed)
        job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.cancel_job.assert_called_once_with("j1")

    def test_with_nothing_due_it_completes_without_a_warning(self, env, monkeypatch):
        env.job.config = dict(self.CONFIG)
        monkeypatch.setattr(job_runner, "online_recheck_files", MagicMock(return_value=[]))
        job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.add_log.assert_any_call("j1", "INFO - No file is due to be asked again online")
        env.jm.complete_job.assert_called_once_with("j1")

    def test_any_other_job_lists_its_own_files(self, env, monkeypatch):
        listed = MagicMock()
        monkeypatch.setattr(job_runner, "online_recheck_files", listed)
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.build_context.call_args.kwargs["online_recheck"] is False
        listed.assert_not_called()

    def test_a_file_theintrodbs_budget_refused_goes_to_the_theintrodb_recheck(self, env, monkeypatch):
        from media_preview_generator.markers import triggers

        env.job.config = dict(self.CONFIG)
        monkeypatch.setattr(job_runner, "online_recheck_files", MagicMock(return_value=["/m/a.mkv", "/m/b.mkv"]))
        refused_at = datetime(2026, 9, 24, 4, 42, tzinfo=UTC)
        env.ctx.take_budget_rechecks.return_value = (["/m/b.mkv"], refused_at)
        env.jm.get_pending_jobs.return_value = []
        create = MagicMock(return_value=MagicMock(id="r1234567"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        monkeypatch.setattr(job_runner, "_utcnow", lambda: refused_at)
        job_runner.run_intro_credits_job("j1")
        create.assert_called_once_with(
            library_name="TheIntroDB recheck: 1 files",
            priority=job_runner.PRIORITY_LOW,
            source=job_runner.BUDGET_RECHECK_SOURCE,
            file_paths=["/m/b.mkv"],
            retry_delay_s=int((job_runner.budget_recheck_due(refused_at) - refused_at).total_seconds()),
        )

    def test_a_file_off_disk_gets_no_retry(self, env, monkeypatch):
        from media_preview_generator.markers import triggers

        settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
        env.sm.get.side_effect = lambda key, default=None: settings.get(key, default)
        env.job.config = dict(self.CONFIG)
        monkeypatch.setattr(job_runner, "online_recheck_files", MagicMock(return_value=["/m/a.mkv"]))
        create = MagicMock()
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)

        def during_wait(timeout=None):
            set_cb.call_args_list[0].args[0]("/m/a.mkv", "skipped_file_not_found", "", "Lookup", servers=[])
            return True

        env.tracker.wait.side_effect = during_wait
        job_runner.run_intro_credits_job("j1")
        create.assert_not_called()
