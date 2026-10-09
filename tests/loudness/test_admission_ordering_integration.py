"""Persisted job priority must reach real admission and balance the granted slot."""

import threading
import time

import pytest
from flask import Flask

from media_preview_generator.jobs import dispatcher as dispatcher_module
from media_preview_generator.loudness import job
from media_preview_generator.loudness.job import start_loudness_job_async
from media_preview_generator.web import job_gate
from media_preview_generator.web.jobs import JobStatus
from media_preview_generator.web.routes import api_jobs

from .test_job_lifecycle import Lifecycle
from .test_job_lifecycle import lifecycle as lifecycle


def _wait_until(predicate, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert predicate(), "expected lifecycle boundary was not reached"


@pytest.mark.parametrize(("initial_priority", "admitted_priority"), [(3, 1), (1, 3)])
def test_priority_api_updates_waiting_runner_start_up_order(
    lifecycle: Lifecycle,  # noqa: F811
    make_gate,
    monkeypatch,
    initial_priority: int,
    admitted_priority: int,
):
    """Both priority directions change who starts up next while a job waits at the gate."""
    lifecycle.api_ready = True
    gate = make_gate(1, lambda kind: 1)
    gate._POLL_SECONDS = 0.01
    monkeypatch.setattr(job_gate, "_gate", gate)
    real_release = gate.release
    order: list[str] = []

    def recording_release():
        order.append("loudness-started-up")
        real_release()

    monkeypatch.setattr(gate, "release", recording_release)
    monkeypatch.setattr(job, "get_job_gate", lambda: gate)
    monkeypatch.setattr(api_jobs, "get_job_manager", lambda: lifecycle.manager)
    monkeypatch.setattr(dispatcher_module, "get_dispatcher", lambda: lifecycle.dispatcher)
    assert gate.acquire(priority=2, kind="previews", cancel_check=lambda: False)
    finish = threading.Event()
    original = lifecycle.popen

    def blocked_analysis(command, **kwargs):
        process = original(command, **kwargs)
        wait_for_exit = process.wait

        def wait(**options):
            assert finish.wait(20), "test did not release simulated FFmpeg"
            return wait_for_exit(**options)

        process.wait = wait
        return process

    monkeypatch.setattr(job.analyze.subprocess, "Popen", blocked_analysis)
    source = lifecycle.add_file("priority-wait.mkv")
    entry = lifecycle.manager.create_job(
        kind="loudness", priority=initial_priority, config={"source": "manual", "file_paths": [source]}
    )
    rival_cancel = threading.Event()

    def rival() -> None:
        if gate.acquire(2, rival_cancel.is_set, kind="previews"):
            order.append("preview-started-up")

    rival_thread = None
    thread = None
    app = Flask(__name__)
    try:
        start_loudness_job_async(entry.id)
        thread = next(t for t in threading.enumerate() if t.name == f"run_job_loudness_{entry.id}")
        _wait_until(lambda: gate.snapshot() == (1, 1, 1), timeout=15)
        rival_thread = threading.Thread(target=rival, daemon=True)
        rival_thread.start()
        _wait_until(lambda: gate.snapshot() == (1, 2, 1), timeout=15)
        assert entry.status is JobStatus.PENDING
        with app.test_request_context(json={"priority": admitted_priority}):
            response = api_jobs.set_job_priority.__wrapped__(entry.id)
        assert response.json["id"] == entry.id and response.json["priority"] == admitted_priority

        real_release()  # the preview job that held the only slot has listed its files
        if admitted_priority == 1:
            _wait_until(lambda: len(order) == 2, timeout=15)
            assert order == ["loudness-started-up", "preview-started-up"]
        else:
            _wait_until(lambda: order == ["preview-started-up"], timeout=15)
            assert entry.status is not JobStatus.RUNNING
            real_release()  # the rival has listed its files; now the demoted job may start up
            _wait_until(lambda: order == ["preview-started-up", "loudness-started-up"], timeout=15)
        finish.set()
        thread.join(15)
        assert not thread.is_alive()
        assert entry.status is JobStatus.COMPLETED
        assert not gate.has_request(entry.id)
        assert lifecycle.analyses == [(source, 1)]
    finally:
        lifecycle.manager.request_cancellation(entry.id)
        lifecycle.manager.cancel_job(entry.id)
        finish.set()
        rival_cancel.set()
        if thread is not None:
            thread.join(15)
        if rival_thread is not None:
            rival_thread.join(15)


def _recovery_scenario(
    lifecycle,  # noqa: F811
    monkeypatch,
    trigger: str,
    *,
    blocked_old: str | None = None,
    failed_old: bool = False,
):
    """Run real recovery orchestration; deliberately delay the older runner's thread."""
    import importlib
    from types import SimpleNamespace

    from media_preview_generator.jobs.group_runtime import admission_options
    from media_preview_generator.web import routes, settings_manager
    from media_preview_generator.web.routes import job_runner

    app_module = importlib.import_module("media_preview_generator.web.app")
    manager = lifecycle.manager
    old = manager.create_job(kind="loudness", priority=3, config={"source": "manual", "file_paths": ["/old"]})
    new = manager.create_job(kind="loudness", priority=3, config={"source": "manual", "file_paths": ["/new"]})
    old.created_at, new.created_at = "2026-10-05T00:00:00+00:00", "2026-10-07T00:00:00+00:00"
    if blocked_old == "dependency":
        parent = manager.create_job(kind="previews", config={})
        old.config["follows_job_id"] = parent.id
    elif blocked_old == "future_retry":
        old.config["retry_not_before"] = "2099-01-01T00:00:00+00:00"
    elif blocked_old == "manual_pause":
        manager.request_pause(old.id)
    settings = SimpleNamespace(processing_paused=False, get=lambda key, default=None: default)
    monkeypatch.setattr(settings_manager, "get_settings_manager", lambda *args: settings)
    monkeypatch.setattr(app_module, "get_job_manager", lambda: manager)
    monkeypatch.setattr(app_module, "_fail_unrevived_own_runner_jobs", lambda: None)
    monkeypatch.setattr(app_module, "_fail_unrevived_preview_jobs", lambda: None)
    monkeypatch.setattr(manager, "restore_capacity_waits", lambda: [new])
    monkeypatch.setattr(manager, "requeue_interrupted_jobs", lambda **kwargs: [old])
    monkeypatch.setattr(manager, "requeue_interrupted_followers", lambda kept: [])
    # Keep the parent outside this recovery batch: it is an external prerequisite.
    monkeypatch.setattr(manager, "get_pending_jobs", lambda: [new, old])
    monkeypatch.setattr(job_gate, "STARTUP_SLOTS", 1)
    gate = job_gate.JobGate(kind_capacity_provider=lambda kind: 1)
    gate._POLL_SECONDS = 0.01
    monkeypatch.setattr(job_gate, "_gate", gate)
    allow_old, release_old, cancel, new_entered = (threading.Event() for _ in range(4))
    admitted = {old.id: threading.Event(), new.id: threading.Event()}
    calls, threads = [], []

    def start(job_id, config):
        entry = manager.get_job(job_id)
        calls.append((job_id, dict(config)))
        if failed_old and job_id == old.id:
            raise RuntimeError("simulated runner launch failure")

        def run():
            if entry.id == old.id:
                allow_old.wait(3)
                if cancel.is_set():
                    return
            else:
                new_entered.set()
            if gate.acquire(
                priority=entry.priority,
                cancel_check=cancel.is_set,
                **admission_options(manager, entry.id, entry.kind),
            ):
                admitted[entry.id].set()
                if entry.id == old.id:
                    release_old.wait(3)
                gate.release()

        thread = threading.Thread(target=run, daemon=True)
        threads.append(thread)
        thread.start()

    monkeypatch.setattr(routes, "_start_job_async", start)
    monkeypatch.setattr(job_runner, "_start_job_async", start)
    if trigger == "startup":
        app_module._requeue_interrupted_on_startup(str(manager.config_dir))
    else:
        job_runner.resume_running_and_drain_pending()
    return old, new, calls, threads, allow_old, release_old, cancel, new_entered, admitted


def test_startup_older_job_keeps_place_when_new_capacity_wait_thread_runs_first(lifecycle: Lifecycle, monkeypatch):  # noqa: F811
    _assert_delayed_older_keeps_place(lifecycle, monkeypatch, "startup")


def test_global_resume_older_job_keeps_place_when_its_thread_starts_late(lifecycle: Lifecycle, monkeypatch):  # noqa: F811
    _assert_delayed_older_keeps_place(lifecycle, monkeypatch, "resume")


def _assert_delayed_older_keeps_place(lifecycle, monkeypatch, trigger: str) -> None:  # noqa: F811
    old, new, calls, threads, allow_old, release_old, cancel, new_entered, admitted = _recovery_scenario(
        lifecycle, monkeypatch, trigger
    )
    try:
        assert sorted(calls) == sorted([(old.id, old.config), (new.id, new.config)])
        assert new_entered.wait(1)
        assert not admitted[new.id].wait(0.1), "newer thread bypassed the older ready job"
        allow_old.set()
        assert admitted[old.id].wait(2)
        assert not admitted[new.id].is_set()
        release_old.set()
        assert admitted[new.id].wait(2)
    finally:
        cancel.set()
        allow_old.set()
        release_old.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()


@pytest.mark.parametrize("blocked_old", ["dependency", "future_retry", "manual_pause"])
def test_deferred_older_job_does_not_block_ready_recovered_job(lifecycle: Lifecycle, monkeypatch, blocked_old):  # noqa: F811
    old, new, calls, threads, allow_old, release_old, cancel, new_entered, admitted = _recovery_scenario(
        lifecycle, monkeypatch, "startup", blocked_old=blocked_old
    )
    try:
        assert sorted(calls) == sorted([(old.id, old.config), (new.id, new.config)])
        assert new_entered.wait(1)
        assert admitted[new.id].wait(2), "deferred work must not reserve capacity ahead of ready work"
        assert not admitted[old.id].is_set()
    finally:
        cancel.set()
        allow_old.set()
        release_old.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()


def test_recovery_failed_older_launcher_does_not_leave_a_reservation(lifecycle: Lifecycle, monkeypatch):  # noqa: F811
    old, new, calls, threads, allow_old, release_old, cancel, new_entered, admitted = _recovery_scenario(
        lifecycle, monkeypatch, "startup", failed_old=True
    )
    try:
        assert {job_id for job_id, _ in calls} == {old.id, new.id}
        assert new_entered.wait(1)
        assert admitted[new.id].wait(2), "failed launcher left a phantom admission ahead of runnable work"
        assert not job_gate.get_job_gate().has_request(old.id)
    finally:
        cancel.set()
        allow_old.set()
        release_old.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()


def test_failed_async_thread_start_releases_ownership_and_allows_retry(lifecycle: Lifecycle, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    lifecycle.api_ready = True
    gate = job_gate.JobGate()
    monkeypatch.setattr(job_gate, "_gate", gate)
    monkeypatch.setattr(job, "get_job_gate", lambda: gate)
    source = lifecycle.add_file("launch-retry.mkv")
    entry = lifecycle.manager.create_job(kind="loudness", config={"source": "manual", "file_paths": [source]})

    def fail_start():
        raise RuntimeError("simulated thread start failure")

    with monkeypatch.context() as local:
        local.setattr(job.threading, "Thread", lambda **kwargs: SimpleNamespace(start=fail_start))
        with pytest.raises(RuntimeError, match="simulated thread start failure"):
            start_loudness_job_async(entry.id)
    assert entry.id not in job._inflight_jobs
    assert not gate.has_request(entry.id)
    assert gate.snapshot() == (0, 0, job_gate.STARTUP_SLOTS)
    start_loudness_job_async(entry.id)
    _wait_until(lambda: entry.id not in job._inflight_jobs)
    assert entry.status is JobStatus.COMPLETED
    assert lifecycle.analyses == [(source, 1)]
    assert gate.snapshot() == (0, 0, job_gate.STARTUP_SLOTS)
    assert not gate.has_request(entry.id)
