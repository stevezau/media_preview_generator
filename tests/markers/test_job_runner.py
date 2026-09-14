"""Intro & Credits job runner: file selection, the job thread, delegation from the preview runner and restarts."""

import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_PREVIEWS
from media_preview_generator.markers import job_runner
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
        reg = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        cfg = {
            "file_paths": [str(season), str(season / "S01E01.mkv"), str(other / "S01E01.mkv")],
            "webhook_item_id_hints": {str(other / "S01E01.mkv"): {"jf-1": "abc"}},
        }
        with patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            side_effect=lambda p, configs, log_resolution=True: (p, []),
        ) as resolve:
            items, warnings = job_runner.build_items(cfg, registry=reg)
        assert [i.canonical_path for i in items] == [
            str(other / "S01E01.mkv"),
            str(season / "S01E01.mkv"),
            str(season / "S01E02.mkv"),
        ]
        assert items[0].item_id_by_server == {"jf-1": "abc"} and items[1].item_id_by_server == {}
        assert [i.title for i in items] == ["S01E01.mkv", "S01E01.mkv", "S01E02.mkv"]
        assert warnings == []
        assert all(c.kwargs.get("log_resolution") is False for c in resolve.call_args_list)
        assert all(c.args[1] == reg.configs() for c in resolve.call_args_list)

    def test_file_paths_are_resolved_to_the_canonical_local_path(self):
        reg = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        with patch(
            "media_preview_generator.jobs.orchestrator._resolve_webhook_path_to_canonical",
            return_value=("/media/tv/Show/S01E01.mkv", []),
        ):
            items, _ = job_runner.build_items(
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
            items, warnings = job_runner.build_items(cfg, registry=reg, cancel_check=cancel, progress_callback=progress)
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
            _items, warnings = job_runner.build_items(cfg, registry=reg)
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
            items, warnings = job_runner.build_items({}, registry=reg)
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
            items, warnings = job_runner.build_items(
                {"libraries": [{"server_id": "jf-1", "library_id": "1"}]}, registry=FakeRegistry(servers)
            )
        assert enum.call_args.args[0] == []
        assert items == []
        assert len(warnings) == 1 and expected_warning in warnings[0]

    def test_enumerated_duplicates_keep_the_first_item(self):
        reg = FakeRegistry({"jf-1": server_config("jf-1", ServerType.JELLYFIN)})
        first = ProcessableItem("/media/tv/S/a.mkv", "jf-1", {"jf-1": "x"})
        with patch(
            "media_preview_generator.jobs.orchestrator._enumerate_items_for_servers",
            return_value=([(None, first), (None, ProcessableItem("/media/tv/S/a.mkv", "jf-1"))], []),
        ):
            items, _ = job_runner.build_items({}, registry=reg)
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
        with patch.object(
            job_runner, "build_items", return_value=([_item()] if items is None else items, warnings or [])
        ) as build:
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

    @pytest.mark.parametrize(("force", "expected"), [(True, True), (False, False), (None, False), ("", False)])
    def test_force_from_job_config_reaches_the_pipeline_context(self, env, force, expected):
        env.job.config = {"libraries": [], "force": force}
        self._run()
        assert env.build_context.call_args.kwargs["force"] is expected

    def test_no_warnings_completes_cleanly(self, env):
        self._run()
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

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
            "j1", "/m/a.mkv", "markers_published", "1 marker(s)", "Lookup", servers=[{"id": "jf-1"}]
        )

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

    def test_cancel_during_enumeration_cancels_without_submitting(self, env):
        def build(cfg, **kwargs):
            env.jm.is_cancellation_requested.return_value = True
            return [_item()], []

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
        assert "enumeration exploded" in env.jm.complete_job.call_args.kwargs["error"]
        env.gate.release.assert_called_once_with(3)
        env.jm.clear_pause_flag.assert_called_once_with("j1")


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
                records[str(path)] = SimpleNamespace(size=st.st_size, mtime_ns=st.st_mtime_ns)
            elif analysed == "replaced":
                records[str(path)] = SimpleNamespace(size=st.st_size + 1, mtime_ns=st.st_mtime_ns)
            row = {"file": str(path), "outcome": outcome}
            if servers is not None:
                row["servers"] = servers
            env.jm.get_file_results.return_value.append(row)
            return _item(str(path))

        env.jm.get_file_results.return_value = []
        env.ctx.store.get_file.side_effect = records.get
        return make

    def test_resumed_job_skips_unchanged_finished_files_and_carries_their_counts(self, env, finished, tmp_path):
        done = finished("a.mkv", "markers_published")
        failed = finished("b.mkv", "failed")
        replaced = finished("c.mkv", "markers_published", analysed="replaced")
        store_reset = finished("d.mkv", "markers_up_to_date", analysed=None)
        env.jm.get_file_results.return_value.append({"file": "", "outcome": "truncated:markers_up_to_date"})
        new = _item(str(tmp_path / "e.mkv"))
        items = [done, failed, replaced, store_reset, new]
        with patch.object(job_runner, "build_items", return_value=(items, [])):
            job_runner.run_intro_credits_job("j1")
        env.jm.get_file_results.assert_called_once_with("j1")
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == [failed, replaced, store_reset, new]
        env.jm.set_job_outcome.assert_called_once_with("j1", {"markers_published": 2})

    @pytest.mark.parametrize(("outcome", "skipped"), ALL_OUTCOMES)
    def test_only_settled_outcomes_are_skipped(self, env, finished, outcome, skipped):
        item = finished("a.mkv", outcome)
        other = finished("b.mkv", "failed")
        with patch.object(job_runner, "build_items", return_value=([item, other], [])):
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
        with patch.object(job_runner, "build_items", return_value=([item, other], [])):
            job_runner.run_intro_credits_job("j1")
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == ([other] if skipped else [item, other])

    def test_file_gone_from_disk_is_checked_again(self, env, finished, tmp_path):
        item = finished("a.mkv", "markers_published")
        os.remove(item.canonical_path)
        with patch.object(job_runner, "build_items", return_value=([item], [])):
            job_runner.run_intro_credits_job("j1")
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == [item]

    def test_resumed_job_with_every_file_finished_completes_without_submitting(self, env, finished):
        item = finished("a.mkv", "markers_needs_review")
        with patch.object(job_runner, "build_items", return_value=([item], [])):
            job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_not_called()
        env.jm.set_job_outcome.assert_called_once_with("j1", {"markers_needs_review": 1})
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

    @pytest.mark.parametrize("results", [OSError("disk gone"), None])
    def test_unreadable_file_results_checks_every_file_again(self, env, monkeypatch, results):
        if isinstance(results, Exception):
            env.jm.get_file_results.side_effect = results
        else:
            env.jm.get_file_results.return_value = results
        with patch.object(job_runner, "build_items", return_value=([_item("/m/a.mkv")], [])):
            job_runner.run_intro_credits_job("j1")
        assert env.dispatcher.submit_items.call_args.kwargs["items"] == [_item("/m/a.mkv")]

    def test_job_paused_before_the_restart_stays_paused_without_a_slot_until_resumed(self, env, monkeypatch):
        env.job.paused = True
        order = []
        env.jm.start_job.side_effect = lambda jid: order.append("start")
        env.jm.request_pause.side_effect = lambda jid: order.append("pause") or True
        state = {"paused": True}
        env.jm.is_pause_requested.side_effect = lambda jid: state["paused"]
        sleeps = []

        def fake_sleep(_seconds):
            sleeps.append(1)
            env.gate.acquire.assert_not_called()
            if len(sleeps) == 2:
                state["paused"] = False

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        with patch.object(job_runner, "build_items", return_value=([_item()], [])):
            job_runner.run_intro_credits_job("j1")
        assert order[:2] == ["start", "pause"]
        env.jm.request_pause.assert_called_once_with("j1")
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
    def test_requeue_after_restart_keeps_only_an_intro_credits_jobs_own_pause(self, tmp_path, kind, keeps_pause):
        from media_preview_generator.web.jobs import JobManager, JobStatus

        config_dir = str(tmp_path)
        before = JobManager(config_dir=config_dir)
        job = before.create_job(library_name="Backfill", kind=kind, config={"kind": kind, "libraries": []})
        before.start_job(job.id)
        assert before.request_pause(job.id)

        after = JobManager(config_dir=config_dir)
        revived = after.requeue_interrupted_jobs()

        assert [j.id for j in revived] == [job.id]
        assert revived[0].status is JobStatus.PENDING
        assert revived[0].paused is keeps_pause
        assert JobManager(config_dir=config_dir).get_job(job.id).paused is keeps_pause

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

    def _run(self, paths=("/m/a.mkv", "/m/b.mkv")):
        with patch.object(job_runner, "build_items", return_value=([_item(p) for p in paths], [])):
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
            retry_attempt=expected_attempt,
            retry_delay_s=expected_delay,
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
        assert any("still" in c.args[1] for c in env.jm.add_log.call_args_list)

    def test_retry_takes_at_most_500_files_and_says_how_many_wait_for_the_next_run(self, env, retry_env):
        paths = [f"/m/{i:04d}.mkv" for i in range(501)]
        retry_env.results += [(p, "markers_waiting", [NOT_IN_LIBRARY_ROW]) for p in reversed(paths)]
        self._run(paths)
        assert retry_env.create.call_args.kwargs["file_paths"] == paths[:500]
        logs = [c.args[1] for c in env.jm.add_log.call_args_list]
        assert "INFO - 1 more files the server hasn't indexed yet will be tried on the next run" in logs

    def test_exactly_500_files_are_all_retried(self, env, retry_env):
        paths = [f"/m/{i:04d}.mkv" for i in range(500)]
        retry_env.results += [(p, "markers_waiting", [NOT_IN_LIBRARY_ROW]) for p in paths]
        self._run(paths)
        assert retry_env.create.call_args.kwargs["file_paths"] == paths
        assert not any("more files" in c.args[1] for c in env.jm.add_log.call_args_list)

    def test_a_server_that_has_the_file_and_one_that_hasnt_indexed_it_yet_still_retries(self, env, retry_env):
        # The file counts as published (Plex written) but Jellyfin would never get its markers otherwise.
        retry_env.results.append(
            (
                "/m/a.mkv",
                "markers_published",
                [_row("markers_written", "2 marker(s)", sid="plex-1"), NOT_IN_LIBRARY_ROW],
            )
        )
        self._run(["/m/a.mkv"])
        assert retry_env.create.call_args.kwargs["file_paths"] == ["/m/a.mkv"]

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
            "j1", "/m/a.mkv", "markers_waiting", "", "Lookup", servers=[NOT_IN_LIBRARY_ROW]
        )


class TestRetryWait:
    """A retry job waits out its delay before the gate: no slot, cancellable."""

    def _clock(self, monkeypatch, start):
        now = {"t": start}
        monkeypatch.setattr(job_runner, "_utcnow", lambda: now["t"])
        return now

    def test_waits_until_the_retry_is_due_without_a_slot(self, env, monkeypatch):
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
        now = self._clock(monkeypatch, start)
        due = start + timedelta(seconds=120)
        env.job.config = {
            "file_paths": ["/m/a.mkv"],
            "retry_attempt": 2,
            "retry_delay": 120,
            "retry_not_before": due.isoformat(),
        }
        sleeps = []

        def fake_sleep(seconds):
            sleeps.append(seconds)
            env.gate.acquire.assert_not_called()
            now["t"] += timedelta(seconds=60)

        monkeypatch.setattr(job_runner, "time", SimpleNamespace(sleep=fake_sleep))
        with patch.object(job_runner, "build_items", return_value=([_item()], [])):
            job_runner.run_intro_credits_job("j1")
        assert len(sleeps) == 2
        env.gate.acquire.assert_called_once()
        first = env.jm.update_progress.call_args_list[0].kwargs
        assert first["retry_eta"] == due.isoformat() and first["retry_wait_total"] == 120
        assert "Retry starting in 120s" in first["current_item"]
        env.jm.update_progress.assert_any_call("j1", retry_eta=None)
        env.dispatcher.submit_items.assert_called_once()

    @pytest.mark.parametrize("not_before", ["2026-09-14T09:00:00+00:00", "not a time", None])
    def test_due_or_unreadable_retry_time_starts_straight_away(self, env, monkeypatch, not_before):
        from datetime import datetime, timezone

        self._clock(monkeypatch, datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc))
        env.job.config = {"file_paths": ["/m/a.mkv"], "retry_attempt": 1, "retry_not_before": not_before}
        with patch.object(job_runner, "build_items", return_value=([_item()], [])):
            job_runner.run_intro_credits_job("j1")
        env.dispatcher.submit_items.assert_called_once()
        assert not any("retry_eta" in c.kwargs for c in env.jm.update_progress.call_args_list)

    def test_cancelled_while_waiting_never_takes_a_slot(self, env, monkeypatch):
        from datetime import datetime, timedelta, timezone

        start = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
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
        from datetime import datetime, timedelta, timezone

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
        stamp = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
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
        [(None, None), ({}, None), ({"force": False}, None), ({"force": True}, {"force": True, "libraries": []})],
    )
    def test_config_overrides_are_merged_only_when_they_change_something(self, monkeypatch, overrides, updated):
        jm = MagicMock()
        jm.get_job.return_value = MagicMock(config={"force": False, "libraries": []})
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
        monkeypatch.setattr(job_runner, "run_intro_credits_job", lambda jid: None)
        job_runner.start_intro_credits_job_async("start-5", overrides)
        if updated is None:
            jm.update_job_config.assert_not_called()
        else:
            jm.update_job_config.assert_called_once_with("start-5", updated)


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
