"""Persisted job priority must reach real admission and balance the granted slot."""

import threading
import time
from unittest.mock import patch

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
def test_priority_api_updates_waiting_runner_and_release_uses_granted_priority(
    lifecycle: Lifecycle,  # noqa: F811
    monkeypatch,
    initial_priority: int,
    admitted_priority: int,
):
    """Both priority directions affect waiting; reverting later cannot corrupt slot accounting."""
    lifecycle.api_ready = True
    gate = job_gate.JobGate(lambda: 2, kind_capacity_provider=lambda kind: 1)
    gate._POLL_SECONDS = 0.01
    monkeypatch.setattr(job_gate, "_gate", gate)
    monkeypatch.setattr(job, "get_job_gate", lambda: gate)
    monkeypatch.setattr(api_jobs, "get_job_manager", lambda: lifecycle.manager)
    monkeypatch.setattr(dispatcher_module, "get_dispatcher", lambda: lifecycle.dispatcher)
    assert gate.acquire(priority=2, kind="previews", cancel_check=lambda: False)
    normal_held = True
    high_held = initial_priority == 1
    if high_held:
        assert gate.acquire(priority=1, kind="intro_credits", cancel_check=lambda: False)
    started, finish = threading.Event(), threading.Event()
    original = lifecycle.popen

    def blocked_analysis(command, **kwargs):
        process = original(command, **kwargs)
        wait_for_exit = process.wait

        def wait(**options):
            started.set()
            assert finish.wait(5), "test did not release simulated FFmpeg"
            return wait_for_exit(**options)

        process.wait = wait
        return process

    monkeypatch.setattr(job.analyze.subprocess, "Popen", blocked_analysis)
    source = lifecycle.add_file("priority-wait.mkv")
    entry = lifecycle.manager.create_job(
        kind="loudness", priority=initial_priority, config={"source": "manual", "file_paths": [source]}
    )
    thread = None
    app = Flask(__name__)
    with patch.object(gate, "release", wraps=gate.release) as release:
        try:
            start_loudness_job_async(entry.id)
            thread = next(t for t in threading.enumerate() if t.name == f"run_job_loudness_{entry.id}")
            _wait_until(lambda: gate.snapshot() == (1 + high_held, 1, 2))
            assert entry.status is JobStatus.PENDING and not started.is_set()
            start_loudness_job_async(entry.id)
            assert gate.snapshot() == (1 + high_held, 1, 2), "duplicate launch changed the owned admission"
            with app.test_request_context(json={"priority": admitted_priority}):
                response = api_jobs.set_job_priority.__wrapped__(entry.id)
            assert response.json["id"] == entry.id and response.json["priority"] == admitted_priority
            if high_held:
                gate.release(1, kind="intro_credits")
                high_held = False
                assert not started.wait(0.1), "demoted work incorrectly used the reserved HIGH slot"
                gate.release(2, kind="previews")
                normal_held = False
            assert started.wait(2), "priority update did not reach the pending gate request"
            assert gate.snapshot() == (1 + normal_held, 0, 2)
            with app.test_request_context(json={"priority": initial_priority}):
                response = api_jobs.set_job_priority.__wrapped__(entry.id)
            assert response.json["priority"] == initial_priority
            finish.set()
            thread.join(3)
            assert not thread.is_alive()
            assert entry.status is JobStatus.COMPLETED
            assert not gate.has_request(entry.id)
            assert lifecycle.analyses == [(source, 1)]
            assert [
                (call.args, call.kwargs) for call in release.call_args_list if call.kwargs.get("kind") == "loudness"
            ] == [((admitted_priority,), {"kind": "loudness"})]
            assert gate.snapshot() == (int(normal_held), 0, 2)
            assert gate._active_high == 0
            assert all(not count for (kind, _), count in gate._kind_active.items() if kind == "loudness")
        finally:
            lifecycle.manager.request_cancellation(entry.id)
            lifecycle.manager.cancel_job(entry.id)
            finish.set()
            if high_held:
                gate.release(1, kind="intro_credits")
            if normal_held:
                gate.release(2, kind="previews")
            if thread is not None:
                thread.join(3)
                assert not thread.is_alive()


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
    gate = job_gate.JobGate(lambda: 1, kind_capacity_provider=lambda kind: 1)
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
                gate.release(entry.priority, kind=entry.kind)

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


@pytest.mark.parametrize("kind", ["loudness", "intro_credits"])
def test_named_running_job_yields_for_pause_then_reacquires_at_current_priority(
    lifecycle: Lifecycle,  # noqa: F811
    monkeypatch,
    kind,
):
    from types import SimpleNamespace

    from media_preview_generator.jobs.admission import claim_admission, finish_admission
    from media_preview_generator.jobs.group_runtime import admission_options
    from media_preview_generator.markers import job_runner

    manager = lifecycle.manager
    entry = manager.create_job(kind=kind, priority=3, config={})
    manager.start_job(entry.id)
    gate = job_gate.JobGate(lambda: 2, kind_capacity_provider=lambda _: 1)
    gate._POLL_SECONDS = 0.01
    monkeypatch.setattr(job_gate, "_gate", gate)
    monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
    owner = claim_admission(manager, entry.id)
    slot = {"held": True, "priority": 3, "kind": kind}
    assert gate.acquire(
        priority=3,
        cancel_check=lambda: False,
        **admission_options(manager, entry.id, kind, on_admitted=lambda value: slot.update(priority=value)),
    )
    manager.request_pause(entry.id)
    calls = 0

    def tracker_wait(timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert gate.snapshot() == (0, 0, 2), "paused running job retained its slot"
            assert slot["held"] is False
            manager.update_job_priority(entry.id, 1)
            manager.request_resume(entry.id)
            assert gate.acquire(priority=2, kind="previews", cancel_check=lambda: False)
        if calls == 3:
            assert slot == {"held": True, "priority": 1, "kind": kind}
            assert gate.snapshot() == (2, 0, 2)
            manager.update_job_priority(entry.id, 3)
            return True
        assert calls < 4
        return False

    tracker = SimpleNamespace(wait=tracker_wait, done_event=threading.Event())
    job_runner.wait_releasing_slot_while_paused(
        tracker,
        job_id=entry.id,
        slot=slot,
        live_priority=lambda: entry.priority,
        cancel_check=lambda: False,
        on_wait=lambda *_: None,
    )
    assert calls == 3 and entry.priority == 3
    assert gate._active_high == 1
    gate.release(slot["priority"], kind=kind)
    assert gate._active_high == 0
    assert all(not count for (held_kind, _), count in gate._kind_active.items() if held_kind == kind)
    gate.release(2, kind="previews")
    finish_admission(entry.id, owner)
    assert not gate.has_request(entry.id)
    assert gate.snapshot() == (0, 0, 2)


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
    gate = job_gate.JobGate(lambda: 2)
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
    assert gate.snapshot() == (0, 0, 2)
    start_loudness_job_async(entry.id)
    _wait_until(lambda: entry.id not in job._inflight_jobs)
    assert entry.status is JobStatus.COMPLETED
    assert lifecycle.analyses == [(source, 1)]
    assert gate.snapshot() == (0, 0, 2)
    assert not gate.has_request(entry.id)
