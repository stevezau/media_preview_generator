"""run_loudness_job's lifecycle (slot, completion, retry of files Plex hadn't added) and the webhook follow-up."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.loudness import job
from media_preview_generator.processing.retry_queue import scaled_backoff_delay
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import Library
from media_preview_generator.web.jobs import JobStatus


@pytest.fixture
def run(monkeypatch):
    """run_loudness_job with its collaborators faked; returns what they saw."""
    jm = MagicMock()
    jm.get_job.return_value = SimpleNamespace(
        id="j1",
        priority=3,
        paused=False,
        status=JobStatus.PENDING,
        progress=SimpleNamespace(outcome={}),
        publishers=[],
        config={"source": "sonarr", "file_paths": ["/m"]},
    )
    jm.is_cancellation_requested.return_value = False
    jm.is_pause_requested.return_value = False
    jm.get_running_jobs.return_value = []
    jm.get_file_results.return_value = []
    jm.set_job_outcome.side_effect = lambda job_id, counts: setattr(jm.get_job.return_value.progress, "outcome", counts)
    gate = MagicMock()
    gate.acquire.return_value = True
    seen = {"retries": [], "outcomes": {}, "publisher_rows": {}}
    callback = {}
    settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}

    def create_retry(**kwargs):
        seen["retries"].append(kwargs)
        return SimpleNamespace(id="retry-1", config={**kwargs, "retry_not_before": "2099-01-01T00:00:00+00:00"})

    def submit_items(**kwargs):
        seen["submitted"] = kwargs["items"]
        # The dispatcher reports each file through the job's callback, then finishes.
        for path, outcome in seen["outcomes"].items():
            callback["fn"](path, outcome, "", "CPU 1", servers=seen["publisher_rows"].get(path))
        tracker = MagicMock(priority=3)
        tracker.wait.return_value = True
        counts = {}
        for outcome in seen["outcomes"].values():
            counts[outcome] = counts.get(outcome, 0) + 1
        tracker.get_result.return_value = {"outcome": counts, "cancelled": False}
        return tracker

    dispatcher = MagicMock()
    dispatcher.submit_items.side_effect = submit_items
    items = [ProcessableItem(canonical_path=p, server_id="", title=p) for p in ("/m/a.mkv", "/m/b.mkv")]
    for name, value in {
        "get_job_manager": lambda: jm,
        "get_job_gate": lambda: gate,
        "get_settings_manager": lambda: SimpleNamespace(processing_paused=False, get=settings.get),
        "load_config": lambda: MagicMock(ffmpeg_path="ffmpeg"),
        "_build_multi_server_registry": lambda config: MagicMock(),
        "build_items": lambda cfg, **kw: (items, [], {}),
        "_ensure_gpu_cache": lambda: [],
        "_build_selected_gpus": lambda *a, **kw: [],
        "get_or_create_dispatcher": lambda config, gpus: dispatcher,
        "wait_for_preceding_job": lambda *a: True,
        "wait_for_retry_time": lambda *a: True,
        "wait_releasing_slot_while_paused": lambda *a, **kw: None,
        "set_file_result_callback": lambda fn, job_id: callback.__setitem__("fn", fn),
        "create_loudness_job": create_retry,
    }.items():
        monkeypatch.setattr(job, name, value)
    seen.update(jm=jm, gate=gate, dispatcher=dispatcher, settings=settings)
    return seen


def test_completes_releases_the_slot_and_retries_files_plex_hadnt_added(run):
    run["outcomes"] = {"/m/a.mkv": job.WRITTEN, "/m/b.mkv": job.NOT_IN_LIBRARY}
    job.run_loudness_job("j1")
    run["jm"].complete_job.assert_called_once()
    run["gate"].release.assert_called_once_with(3)
    (retry,) = run["retries"]
    assert retry["file_paths"] == ["/m/b.mkv"] and retry["retry_attempt"] == 1
    assert retry["retry_delay_s"] == scaled_backoff_delay(1, 30)


def test_loudness_callback_preserves_shared_preview_chapter_progress(run, tmp_path, monkeypatch):
    from media_preview_generator.jobs.dispatcher import JobDispatcher
    from media_preview_generator.jobs.worker import WorkerPool
    from media_preview_generator.web.jobs import JobManager

    emitted = []
    delivered = threading.Event()

    def capture(event, data, namespace):
        if event == "worker_update":
            assert namespace == "/jobs"
            emitted.append(data)
            delivered.set()

    manager = JobManager(str(tmp_path), socketio=SimpleNamespace(emit=capture))
    pool = WorkerPool(cpu_workers=2, gpu_workers=0, selected_gpus=[])
    dispatcher = JobDispatcher(pool)
    preview_job = manager.create_job(kind="previews")
    audio_job = manager.create_job(kind="loudness")
    manager.start_job(preview_job.id)
    manager.start_job(audio_job.id)
    manager.request_pause(audio_job.id)
    for method in ("update_worker_status", "prune_worker_statuses", "emit_worker_statuses"):
        monkeypatch.setattr(run["jm"], method, getattr(manager, method))
    try:
        job.run_loudness_job("j1")
        callback = run["dispatcher"].submit_items.call_args.kwargs["callbacks"]["worker_callback"]
        worker = pool._snapshot_workers()[0]
        worker.is_busy = True
        worker.current_job_id = preview_job.id
        worker.media_file = "/media/preview.mkv"
        worker.current_task = worker.media_file
        audio_worker = pool._snapshot_workers()[1]
        audio_worker.is_busy = True
        audio_worker.current_job_id = audio_job.id
        audio_worker.media_file = "/media/audio.mkv"
        audio_worker.current_task = audio_worker.media_file
        audio_worker.media_title = "Audio movie"
        audio_worker.current_phase = "Loudness 1/1"
        worker.current_phase = "Chapter thumbnails for Plex…"
        for title, processed in (("Movie A", 2), ("Movie A", 3), ("Movie B", 0)):
            worker.media_title = title
            snapshot = {"stage": "extracting", "processed": processed, "total": 6, "ready": processed, "failed": 0}
            worker.chapter_progress = snapshot
            delivered.clear()
            callback(dispatcher._build_worker_statuses())
            assert delivered.wait(2)
            assert emitted[-1]["workers"][0]["current_title"] == title
            assert emitted[-1]["workers"][0]["chapter_progress"] == snapshot
            preview_row, audio_row = emitted[-1]["workers"]
            assert preview_row["job_id"] == preview_job.id and preview_row["job_kind"] == "previews"
            assert preview_row["current_file"] == "/media/preview.mkv" and not preview_row["paused"]
            assert audio_row["job_id"] == audio_job.id and audio_row["job_kind"] == "loudness"
            assert audio_row["current_file"] == "/media/audio.mkv" and audio_row["paused"]
        worker.is_busy = False
        delivered.clear()
        callback(dispatcher._build_worker_statuses())
        assert delivered.wait(2)
        assert emitted[-1]["workers"][0]["chapter_progress"] is None
        assert emitted[-1]["workers"][0]["current_title"] == ""
        assert emitted[-1]["workers"][0]["job_id"] is None
        assert emitted[-1]["workers"][0]["job_kind"] is None
        assert emitted[-1]["workers"][0]["current_file"] == ""
    finally:
        for worker in pool._snapshot_workers():
            worker.is_busy = False
        pool.shutdown()
        manager.close()


def test_a_last_retry_queues_no_more(run):
    run["jm"].get_job.return_value.config["retry_attempt"] = 3
    run["outcomes"] = {"/m/b.mkv": job.NOT_IN_LIBRARY}
    job.run_loudness_job("j1")
    assert run["retries"] == []
    run["gate"].release.assert_called_once()
    assert "1 file(s) still waiting for Plex or the disk" in run["jm"].complete_job.call_args.kwargs["error"]


def test_a_job_cancelled_waiting_for_its_slot_holds_none(run):
    run["gate"].acquire.return_value = False
    job.run_loudness_job("j1")
    run["jm"].cancel_job.assert_called_once_with("j1")
    run["gate"].release.assert_not_called()


def test_a_senders_missing_and_waiting_files_are_retried_and_dont_fail_the_job(run):
    run["outcomes"] = {"/m/a.mkv": job.FILE_NOT_FOUND, "/m/b.mkv": job.WAITING}
    job.run_loudness_job("j1")
    (retry,) = run["retries"]
    assert retry["file_paths"] == ["/m/a.mkv", "/m/b.mkv"]
    assert run["jm"].upsert_retry_chain_job.call_args.kwargs["outcome"] == "scheduled"
    assert run["jm"].upsert_retry_chain_job.call_args.kwargs["originating_job_id"] == "j1"


def test_a_manual_jobs_missing_file_is_not_retried(run):
    run["jm"].get_job.return_value.config["source"] = "manual"
    run["outcomes"] = {"/m/a.mkv": job.FILE_NOT_FOUND}
    job.run_loudness_job("j1")
    assert run["retries"] == []
    assert "failed or missing" in run["jm"].complete_job.call_args.kwargs["error"]


def test_a_revived_job_carries_the_files_it_finished_before_the_restart(run):
    run["jm"].get_file_results.return_value = [{"file": "/m/a.mkv", "outcome": job.WRITTEN}]
    run["outcomes"] = {"/m/b.mkv": job.WRITTEN}
    job.run_loudness_job("j1")
    assert [i.canonical_path for i in run["submitted"]] == ["/m/b.mkv"]
    assert run["jm"].set_job_outcome.call_args.args[1][job.WRITTEN] == 2


# --- the runner's other ways out ----------------------------------------------------------------------------------


def test_a_job_gone_before_it_starts_does_nothing(run):
    run["jm"].get_job.return_value = None
    job.run_loudness_job("j1")
    run["jm"].start_job.assert_not_called()


def test_a_job_started_while_processing_is_paused_stays_pending(run, monkeypatch):
    monkeypatch.setattr(job, "get_settings_manager", lambda: MagicMock(processing_paused=True))
    job.run_loudness_job("j1")
    run["jm"].start_job.assert_not_called()
    run["gate"].acquire.assert_not_called()


@pytest.mark.parametrize("wait", ["wait_for_preceding_job", "wait_for_retry_time", "hold_pause_from_before_restart"])
def test_a_job_cancelled_while_it_waits_to_start_holds_no_slot(run, monkeypatch, wait):
    run["jm"].get_job.return_value.paused = True
    monkeypatch.setattr(job, "hold_pause_from_before_restart", lambda *a: True)
    monkeypatch.setattr(job, wait, lambda *a: False)
    job.run_loudness_job("j1")
    run["jm"].cancel_job.assert_called_once_with("j1")
    run["gate"].acquire.assert_not_called()


def test_waiting_for_a_slot_and_listing_files_show_on_the_job(run, monkeypatch):
    def acquire(**kwargs):
        kwargs["on_wait"](1, 1, 1)
        return True

    def build_items(cfg, **kwargs):
        kwargs["progress_callback"](1, 2, "Listing")
        return [ProcessableItem(canonical_path="/m/a.mkv", server_id="", title="a")], [], {}

    run["gate"].acquire.side_effect = acquire
    monkeypatch.setattr(job, "build_items", build_items)
    run["outcomes"] = {"/m/a.mkv": job.WRITTEN}
    job.run_loudness_job("j1")
    messages = [c.kwargs.get("current_item") for c in run["jm"].update_progress.call_args_list]
    assert "Listing" in messages and len(messages) >= 2
    run["jm"].note_slot_wait.assert_called()


def test_a_missing_server_configuration_fails_the_job(run, monkeypatch):
    monkeypatch.setattr(job, "_build_multi_server_registry", lambda config: None)
    job.run_loudness_job("j1")
    assert "media servers configuration" in run["jm"].complete_job.call_args.kwargs["error"]
    run["gate"].release.assert_called_once()


def test_a_job_cancelled_while_listing_its_files_submits_nothing(run):
    run["jm"].is_cancellation_requested.return_value = True
    job.run_loudness_job("j1")
    run["jm"].cancel_job.assert_called_once_with("j1")
    assert "submitted" not in run


def test_a_job_with_no_files_completes_with_a_warning(run, monkeypatch):
    monkeypatch.setattr(job, "build_items", lambda cfg, **kw: ([], ["Skipped Barn TV"], {}))
    job.run_loudness_job("j1")
    assert run["jm"].complete_job.call_args.kwargs["warning"] == "No files to check. Skipped Barn TV"


def test_a_priority_changed_while_submitting_reaches_the_dispatcher(run):
    run["jm"].get_job.return_value.priority = 1
    run["outcomes"] = {"/m/a.mkv": job.WRITTEN}
    job.run_loudness_job("j1")
    run["dispatcher"].update_job_priority.assert_called_once_with("j1", 1)


def test_an_error_fails_the_job_stops_its_files_and_still_cleans_up(run):
    run["dispatcher"].submit_items.side_effect = RuntimeError("boom")
    run["gate"].release.side_effect = RuntimeError("gate gone")
    run["jm"].get_running_jobs.side_effect = RuntimeError("jobs unreadable")
    job.run_loudness_job("j1")
    assert run["jm"].complete_job.call_args.kwargs["error"] == "RuntimeError: boom"
    run["dispatcher"].cancel_job.assert_called_once_with("j1")
    run["jm"].clear_active_worker_pool.assert_called_once_with("j1")


def test_an_error_marking_the_job_failed_is_only_logged(run, monkeypatch):
    monkeypatch.setattr(job, "build_items", MagicMock(side_effect=RuntimeError("boom")))
    run["jm"].complete_job.side_effect = RuntimeError("store gone")
    job.run_loudness_job("j1")  # doesn't raise
    run["gate"].release.assert_called_once()


# --- starting and creating ------------------------------------------------------------------------------------------


def test_start_merges_changed_overrides_runs_once_and_skips_a_job_in_flight(monkeypatch):
    jm = MagicMock()
    jm.get_job.return_value = SimpleNamespace(config={"priority_hint": 1})
    monkeypatch.setattr(job, "get_job_manager", lambda: jm)
    started = threading.Event()
    monkeypatch.setattr(job, "run_loudness_job", lambda job_id: started.set())
    job._inflight_jobs.add("busy")
    try:
        job.start_loudness_job_async("busy")
        assert not started.wait(0.2)
    finally:
        job._inflight_jobs.discard("busy")
    job.start_loudness_job_async("j1", {"priority_hint": 1, "server_id": "plex1"})
    jm.merge_job_config.assert_called_once_with("j1", {"server_id": "plex1"})
    assert started.wait(2)


def test_create_stores_a_retrys_due_time_and_pin(monkeypatch):
    jm = MagicMock()
    monkeypatch.setattr(job, "get_job_manager", lambda: jm)
    monkeypatch.setattr(job, "start_loudness_job_async", lambda job_id: None)
    job.create_loudness_job(
        library_name="Retry", priority=3, source="sonarr", file_paths=["/m/a.mkv"], server_id="plex1",
        retry_attempt=2, retry_delay_s=60,
    )  # fmt: skip
    config = jm.create_job.call_args.kwargs["config"]
    assert config["server_id"] == "plex1" and config["retry_attempt"] == 2 and config["retry_not_before"]


def test_a_job_cancelled_during_its_files_is_cancelled_not_completed(run):
    run["outcomes"] = {"/m/a.mkv": job.WRITTEN}
    submit = run["dispatcher"].submit_items.side_effect
    run["dispatcher"].submit_items.side_effect = lambda **kw: _cancelled(submit(**kw))
    job.run_loudness_job("j1")
    run["jm"].cancel_job.assert_called_once_with("j1")
    run["jm"].complete_job.assert_not_called()


def _cancelled(tracker):
    tracker.get_result.return_value = {**tracker.get_result.return_value, "cancelled": True}
    return tracker


def test_unreadable_earlier_results_and_a_failed_retry_fail_unfinished_job(run, monkeypatch):
    run["jm"].get_file_results.side_effect = RuntimeError("jsonl unreadable")
    monkeypatch.setattr(job, "create_loudness_job", MagicMock(side_effect=RuntimeError("store full")))
    run["dispatcher"].cancel_job.side_effect = RuntimeError("gone")
    run["outcomes"] = {"/m/b.mkv": job.NOT_IN_LIBRARY}
    job.run_loudness_job("j1")
    assert "1 file(s) still waiting for Plex or the disk" in run["jm"].complete_job.call_args.kwargs["error"]


def test_a_file_no_server_owns_any_more_by_the_worker_stage(monkeypatch):
    monkeypatch.setattr(job, "owners", lambda *a: [])
    ctx = job.LoudnessContext(registry=None, ffmpeg="ffmpeg")
    item = ProcessableItem(canonical_path="/m/a.mkv", server_id="", title="a")
    assert job.process_item(item, ctx=ctx).outcome_key == job.NO_OWNERS


# --- webhook follow-up ------------------------------------------------------------------------------------------


def _follow_up_plex(sid="plex-1", mappings=None):
    return job.ServerConfig(
        id=sid,
        name=sid,
        type=job.ServerType.PLEX,
        enabled=True,
        url="http://plex.invalid",
        auth={},
        path_mappings=mappings or [],
        loudness={"enabled": True},
        libraries=[Library("1", "Videos", ("/m",), kind="movie")],
    )


@pytest.fixture
def follow_up(monkeypatch):
    from media_preview_generator.markers import triggers

    plex = _follow_up_plex()
    monkeypatch.setattr(triggers, "_server_configs", lambda: [plex])
    jm = MagicMock()
    jm.get_pending_jobs.return_value = []
    jm.get_running_jobs.return_value = []
    jm.get_job.return_value = SimpleNamespace(priority=1, library_name="Show S01E01")
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    created = []
    monkeypatch.setattr(job, "create_loudness_job", lambda **kw: created.append(kw))
    return triggers, created


def test_follow_up_is_skipped_when_loudness_is_off_everywhere(follow_up, monkeypatch):
    triggers, created = follow_up
    monkeypatch.setattr("media_preview_generator.loudness.settings.loudness_enabled_anywhere", lambda configs: False)
    triggers._submit_loudness_follow_up("p1", [], ["/m/a.mkv"], "sonarr", None)
    assert created == []


def test_follow_up_drops_a_known_non_plex_originator_pin_and_keeps_a_plex_pin(follow_up, monkeypatch):
    triggers, created = follow_up
    configs = [
        *triggers._server_configs(),
        SimpleNamespace(id="jellyfin-1", type=job.ServerType.JELLYFIN, enabled=True, path_mappings=[]),
    ]
    monkeypatch.setattr(triggers, "_server_configs", lambda: configs)
    monkeypatch.setattr(
        "media_preview_generator.loudness.settings.loudness_enabled_anywhere",
        lambda configs: any(c.id == "plex-1" for c in configs),
    )
    triggers._submit_loudness_follow_up("p1", [], ["/m/a.mkv"], "sonarr", "jellyfin-1")
    triggers._submit_loudness_follow_up("p1", [], ["/m/a.mkv"], "sonarr", "plex-1")
    assert [c["server_id"] for c in created] == [None, "plex-1"]
    assert created[0]["follows_job_id"] == "p1" and created[0]["file_paths"] == ["/m/a.mkv"]


@pytest.mark.parametrize(
    "pin,expected_pin,should_create",
    [
        ("plex-on", "plex-on", True),
        ("plex-off", None, False),
        ("plex-disabled", None, False),
        ("deleted-server", None, False),
        ("jellyfin", None, True),
        ("emby", None, True),
        (None, None, True),
    ],
)
def test_follow_up_never_broadens_an_explicit_plex_or_unknown_pin(
    follow_up, monkeypatch, pin, expected_pin, should_create
):
    triggers, created = follow_up
    configs = [
        job.ServerConfig(
            id=sid,
            name=sid,
            type=server_type,
            enabled=server_enabled,
            url="http://example.invalid",
            auth={},
            loudness={"enabled": loudness_enabled},
            libraries=[Library("1", "Videos", ("/m",), kind="movie")],
        )
        for sid, server_type, server_enabled, loudness_enabled in [
            ("plex-on", job.ServerType.PLEX, True, True),
            ("plex-off", job.ServerType.PLEX, True, False),
            ("plex-disabled", job.ServerType.PLEX, False, True),
            ("jellyfin", job.ServerType.JELLYFIN, True, False),
            ("emby", job.ServerType.EMBY, True, False),
        ]
    ]
    monkeypatch.setattr(triggers, "_server_configs", lambda: configs)
    triggers._submit_loudness_follow_up("p1", ["intro-1"], ["/m/a.mkv"], "sonarr", pin)
    if not should_create:
        assert created == []  # The enabled second Plex server cannot receive this pinned request.
    else:
        assert created == [
            {
                "library_name": "Plex loudness · Show S01E01",
                "priority": triggers.PRIORITY_NORMAL,
                "source": "sonarr",
                "file_paths": ["/m/a.mkv"],
                "follows_job_id": "intro-1",
                "follows_job_ids": ["p1", "intro-1"],
                "server_id": expected_pin,
            }
        ]


def test_a_failing_loudness_follow_up_never_costs_the_intro_follow_up(monkeypatch):
    from media_preview_generator.markers import triggers

    jm = MagicMock()
    jm.get_job.return_value = SimpleNamespace(
        config={triggers.INTRO_CREDITS_FOLLOW_UP: True, "webhook_paths": ["/m/a.mkv"], "source": "sonarr"}
    )
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    monkeypatch.setattr(triggers, "submit_follow_ups", lambda **kw: ["intro-job"])
    monkeypatch.setattr(triggers, "_server_configs", MagicMock(side_effect=RuntimeError("settings unreadable")))
    assert triggers.submit_pending_follow_up("p1") == ["intro-job"]


def test_follow_up_runs_at_normal_and_skips_paths_already_queued_for_the_preview(follow_up, monkeypatch):
    triggers, created = follow_up
    monkeypatch.setattr("media_preview_generator.loudness.settings.loudness_enabled_anywhere", lambda configs: True)
    triggers._submit_loudness_follow_up("p1", [], ["/m/a.mkv"], "sonarr", None)
    assert created[0]["priority"] == triggers.PRIORITY_NORMAL  # never ahead of its High preview, nor below Normal
    assert created[0]["library_name"] == "Plex loudness · Show S01E01"
    earlier = SimpleNamespace(
        kind=job.JOB_KIND_LOUDNESS,
        started_at=None,
        config={"follows_job_id": "p1", "file_paths": ["/m/a.mkv"], "source": "sonarr"},
    )
    triggers.get_job_manager().get_all_jobs.return_value = [earlier]
    triggers._submit_loudness_follow_up("p1", [], ["/m/a.mkv", "/m/b.mkv"], "sonarr", None)
    assert created[1]["file_paths"] == ["/m/b.mkv"]
    triggers._submit_loudness_follow_up("p1", [], ["/m/a.mkv"], "sonarr", None)
    assert len(created) == 2


def test_follow_up_waits_for_the_intro_and_credits_follow_up_when_there_is_one(follow_up, monkeypatch):
    triggers, created = follow_up
    monkeypatch.setattr("media_preview_generator.loudness.settings.loudness_enabled_anywhere", lambda configs: True)
    triggers._submit_loudness_follow_up("p1", ["intro-1"], ["/m/a.mkv"], "sonarr", None)
    triggers._submit_loudness_follow_up("p2", [], ["/m/b.mkv"], "sonarr", None)
    assert [c["follows_job_id"] for c in created] == ["intro-1", "p2"]


@pytest.mark.parametrize("existing_pin", [None, "plex-1", "plex-2"])
@pytest.mark.parametrize("incoming_pin", [None, "plex-1", "plex-2"])
@pytest.mark.parametrize("state", ["pending", "revived", "running"])
def test_different_preview_always_keeps_its_own_barrier(follow_up, monkeypatch, existing_pin, incoming_pin, state):
    triggers, created = follow_up
    configs = [_follow_up_plex(sid) for sid in ("plex-1", "plex-2")]
    monkeypatch.setattr(triggers, "_server_configs", lambda: configs)
    monkeypatch.setattr("media_preview_generator.loudness.settings.loudness_enabled_anywhere", lambda configs: True)
    earlier = SimpleNamespace(
        kind=job.JOB_KIND_LOUDNESS,
        started_at=None if state == "pending" else "2026-10-04T00:00:00Z",
        config={
            "follows_job_id": "previous",
            "file_paths": ["/m/a.mkv"],
            "source": "sonarr",
            "server_id": existing_pin,
        },
    )
    jm = triggers.get_job_manager()
    getattr(jm, "get_running_jobs" if state == "running" else "get_pending_jobs").return_value = [earlier]
    jm.get_all_jobs.return_value = [earlier]
    triggers._submit_loudness_follow_up("p1", ["intro-1"], ["/m/a.mkv"], "sonarr", incoming_pin)
    assert created == [
        {
            "library_name": "Plex loudness · Show S01E01",
            "priority": triggers.PRIORITY_NORMAL,
            "source": "sonarr",
            "file_paths": ["/m/a.mkv"],
            "follows_job_id": "intro-1",
            "follows_job_ids": ["p1", "intro-1"],
            "server_id": incoming_pin,
        }
    ]


@pytest.mark.parametrize("existing_source", ["sonarr", "recently_added"])
def test_different_preview_preserves_mapped_sender_retries(follow_up, monkeypatch, existing_source):
    triggers, created = follow_up
    configs = [_follow_up_plex(mappings=[{"remote_prefix": "/remote", "local_prefix": "/m"}])]
    monkeypatch.setattr(triggers, "_server_configs", lambda: configs)
    monkeypatch.setattr("media_preview_generator.loudness.settings.loudness_enabled_anywhere", lambda configs: True)
    triggers.get_job_manager().get_all_jobs.return_value = [
        SimpleNamespace(
            kind=job.JOB_KIND_LOUDNESS,
            started_at=None,
            config={"follows_job_id": "previous", "file_paths": ["/remote/a.mkv"], "source": existing_source},
        )
    ]
    triggers._submit_loudness_follow_up("p1", [], ["/m/a.mkv"], "sonarr", "plex-1")
    assert len(created) == 1
    assert created[0]["file_paths"] == ["/m/a.mkv"]
    assert created[0]["server_id"] == "plex-1"
    assert created[0]["source"] == "sonarr"


def test_marker_follow_up_cannot_cover_loudness(follow_up, monkeypatch):
    triggers, created = follow_up
    monkeypatch.setattr("media_preview_generator.loudness.settings.loudness_enabled_anywhere", lambda configs: True)
    triggers.get_job_manager().get_pending_jobs.return_value = [
        SimpleNamespace(
            id="intro-1",
            kind=triggers.JOB_KIND_INTRO_CREDITS,
            started_at=None,
            config={"follows_job_id": "p1", "file_paths": ["/m/a.mkv"], "source": "sonarr"},
        )
    ]
    triggers._submit_loudness_follow_up("p1", ["intro-1"], ["/m/a.mkv"], "sonarr", None)
    assert created[0]["file_paths"] == ["/m/a.mkv"]
    assert created[0]["follows_job_id"] == "intro-1"


@pytest.mark.parametrize("pin", [None, "plex-2"])
@pytest.mark.parametrize("attempt", [0, 3 - 1, 3])
@pytest.mark.parametrize("pending", [job.WAITING, job.NOT_IN_LIBRARY, "retryable_failure"])
def test_failed_file_keeps_pending_publisher_retries_scoped_and_bounded(run, pin, attempt, pending):
    config = run["jm"].get_job.return_value.config
    config.update(server_id=pin, retry_attempt=attempt, source="plex")
    rows = [
        {"server_id": "plex-1", "status": job.FAILED, "message": "permanent failure"},
        {"server_id": "plex-2", "status": pending, "message": "temporary failure"},
    ]
    if pending == "retryable_failure":
        rows = [{"server_id": "plex-2", "status": job.FAILED, "retryable": True, "message": "mixed track results"}]
    run["outcomes"] = {"/m/a.mkv": job.FAILED}
    run["publisher_rows"] = {"/m/a.mkv": rows}
    job.run_loudness_job("j1")
    assert run["jm"].set_job_outcome.call_args.args[1][job.FAILED] == 1
    run["jm"].record_file_result.assert_called_once_with(
        "j1", "/m/a.mkv", job.FAILED, "", "CPU 1", servers=rows, server_messages=True, uncapped=False
    )
    if attempt == 3:
        assert run["retries"] == []
        assert "still waiting" in run["jm"].complete_job.call_args.kwargs["error"]
    else:
        (retried,) = run["retries"]
        baseline = retried.pop("retry_baseline")
        assert baseline["outcome"] == {job.FAILED: 1}
        assert set(baseline["files"]) == {"/m/a.mkv"}
        assert baseline["files"]["/m/a.mkv"]["outcome"] == job.FAILED
        assert retried == {
            "library_name": "Retry: Plex loudness: 1 file(s)",
            "priority": 3,
            "source": "plex",
            "file_paths": ["/m/a.mkv"],
            "server_id": pin,
            "retry_attempt": attempt + 1,
            "retry_delay_s": scaled_backoff_delay(attempt + 1, 30),
            "parent_job_id": "j1",
            "max_retries": 3,
        }


def test_permanent_publisher_failures_do_not_create_retries(run):
    run["outcomes"] = {"/m/a.mkv": job.FAILED}
    run["publisher_rows"] = {"/m/a.mkv": [{"server_id": "plex-1", "status": job.FAILED}]}
    job.run_loudness_job("j1")
    assert run["retries"] == []


@pytest.mark.parametrize("blocked", [None, "preview", "marker-2"])
def test_every_predecessor_is_checked_before_acquiring_worker_slot(run, monkeypatch, blocked):
    run["jm"].get_job.return_value.config.update(
        follows_job_id="marker-1", follows_job_ids=["preview", "marker-1", "marker-2"]
    )
    seen = []

    def wait(job_id, predecessor, cancel_check):
        run["gate"].acquire.assert_not_called()
        seen.append(predecessor)
        return predecessor != blocked

    monkeypatch.setattr(job, "wait_for_preceding_job", wait)
    job.run_loudness_job("j1")
    expected = ["marker-1", "preview", "marker-2"]
    assert seen == expected[: expected.index(blocked) + 1] if blocked else seen == expected
    if blocked:
        run["gate"].acquire.assert_not_called()
        run["jm"].cancel_job.assert_called_with("j1")
    else:
        run["gate"].acquire.assert_called_once()
