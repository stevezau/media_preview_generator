"""Pause causes survive independent controls and nonterminal job transitions."""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web import scheduler
from media_preview_generator.web.jobs import JobManager, JobStatus


@pytest.fixture
def manager(tmp_path):
    jm = JobManager(str(tmp_path))
    yield jm
    jm.close()


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
@pytest.mark.parametrize("started", [False, True])
def test_manual_and_schedule_pause_are_independent(manager, kind, started):
    job = manager.create_job(kind=kind)
    if started:
        manager.start_job(job.id)
    assert manager.request_pause(job.id)
    assert manager.request_pause(job.id, by_schedule=True)
    assert manager.request_resume(job.id, only_paused_by_schedule=True)
    assert job.paused and manager.is_pause_requested(job.id)
    assert manager.request_resume(job.id)
    assert not job.paused


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
def test_manual_resume_cannot_remove_schedule_hold(manager, kind):
    job = manager.create_job(kind=kind)
    manager.start_job(job.id)
    manager.request_pause(job.id, by_schedule=True)
    manager.request_resume(job.id)
    assert job.paused and manager.is_pause_requested(job.id)


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
def test_restart_keeps_pause_owners_and_same_job(manager, kind):
    job = manager.create_job(kind=kind)
    manager.start_job(job.id)
    manager.request_pause(job.id)
    manager.request_pause(job.id, by_schedule=True)
    manager._revive_interrupted(job)
    assert job.status is JobStatus.PENDING and job.paused
    manager.request_resume(job.id, only_paused_by_schedule=True)
    assert job.paused
    manager.request_resume(job.id)
    manager.start_job(job.id)
    assert not job.paused


def test_stop_time_holds_pending_jobs_and_does_not_replace_manual_pause(manager):
    pending = manager.create_job(parent_schedule_id="s")
    running = manager.create_job(parent_schedule_id="s")
    manager.start_job(running.id)
    manager.request_pause(running.id)
    unrelated = manager.create_job(parent_schedule_id="other")
    with (
        patch("media_preview_generator.web.jobs.get_job_manager", return_value=manager),
        patch.object(scheduler, "get_schedule_manager", return_value=MagicMock()),
    ):
        scheduler.execute_schedule_stop("s")
    assert pending.paused and running.paused and not unrelated.paused
    manager.request_resume(running.id)
    assert running.paused
    manager.request_resume(pending.id, only_paused_by_schedule=True)
    assert not pending.paused


def test_park_commit_preserves_counters_pause_and_start_across_restart(tmp_path):
    jm = JobManager(str(tmp_path))
    job = jm.create_job(kind="loudness")
    jm.start_job(job.id)
    started = job.started_at
    jm.update_progress(job.id, processed_items=8, total_items=12, percent=8 / 12 * 100)
    jm.request_pause(job.id)
    assert jm.park_job(job.id, "unfinished.json", "off_hours", "2026-10-12T23:00:00+11:00")
    assert jm.get_running_jobs() == []
    jm.close()
    reopened = JobManager(str(tmp_path))
    try:
        revived = reopened.requeue_interrupted_jobs()
        assert [j.id for j in revived] == [job.id]
        same = reopened.resume_parked_job(job.id)
        assert same.started_at == started and same.paused
        assert same.progress.processed_items == 8 and same.progress.total_items == 12
        assert same.config["parked_checkpoint"] == "unfinished.json"
        assert "resource_wait" not in same.config
    finally:
        reopened.close()


def test_failed_park_commit_keeps_running_job_and_original_config(manager):
    import sqlite3

    job = manager.create_job()
    manager.start_job(job.id)
    before = dict(job.config)
    with patch.object(manager._storage, "upsert", side_effect=sqlite3.OperationalError("disk full")):
        with pytest.raises(sqlite3.OperationalError):
            manager.park_job(job.id, "unfinished.json", "off_hours")
    assert job.status is JobStatus.RUNNING and job.config == before
    assert manager.get_running_jobs() == [job]


def test_weekend_resource_wait_is_not_expired_as_stale(tmp_path):
    jm = JobManager(str(tmp_path))
    job = jm.create_job()
    job.created_at = "2020-01-01T00:00:00+00:00"
    jm.mark_resource_wait(job.id, "configuration")
    jm.close()
    reopened = JobManager(str(tmp_path))
    try:
        assert [j.id for j in reopened.requeue_interrupted_jobs(max_age_minutes=5)] == [job.id]
        assert not reopened.unrevived_interrupted_jobs()
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "change",
    [
        None,
        "server",
        "libraries",
        "force",
        "priority",
        "parked",
        "running",
        "revived",
        "paused",
        "retry",
        "completed",
        "failed",
        "cancelled",
    ],
)
def test_full_library_ticks_coalesce_same_unfinished_scope(manager, monkeypatch, change):
    from types import SimpleNamespace

    schedule = SimpleNamespace(_update_last_run=MagicMock())

    def create(**kwargs):
        return manager.create_job(**kwargs)

    schedule.run_job_callback = create
    monkeypatch.setattr(scheduler, "get_schedule_manager", lambda: schedule)
    monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: manager)
    monkeypatch.setattr(
        "media_preview_generator.web.settings_manager.get_settings_manager",
        lambda: SimpleNamespace(processing_paused=False),
    )
    scheduler.execute_scheduled_job("night", ["1", "2"], "Movies", {"force": True}, 2, "plex")
    first = manager.get_all_jobs()[0]
    assert first.parent_schedule_id == "night" and first.server_id == "plex"
    if change in {"parked", "running", "revived", "paused", "retry", "completed", "failed", "cancelled"}:
        manager.start_job(first.id)
        if change == "parked":
            manager.park_job(first.id, "saved.json", "off_hours")
        elif change == "revived":
            manager._revive_interrupted(first)
        elif change == "paused":
            manager.request_pause(first.id)
        elif change == "retry":
            manager.merge_job_config(first.id, {"is_retry_chain": True})
            first.status = JobStatus.PENDING
        elif change == "completed":
            manager.complete_job(first.id)
        elif change == "failed":
            manager.complete_job(first.id, error="failed")
        elif change == "cancelled":
            manager.cancel_job(first.id)
    scheduler.execute_scheduled_job(
        "night",
        ["3"] if change == "libraries" else ["2", "1"],
        "Movies",
        {"force": change != "force"},
        1 if change == "priority" else 2,
        "other" if change == "server" else "plex",
    )
    assert len(manager.get_all_jobs()) == (
        1 if change in {None, "parked", "running", "revived", "paused", "retry"} else 2
    )
    if change == "paused":
        assert first.paused and manager.is_pause_requested(first.id)
    assert first.config["force"] is True


@pytest.mark.parametrize("started", [False, True])
def test_recently_added_schedule_hold_is_cleared_without_new_request(manager, monkeypatch, started):
    from types import SimpleNamespace

    from media_preview_generator.web.routes import job_runner

    monkeypatch.setattr(job_runner, "get_job_manager", lambda: manager)
    monkeypatch.setattr(
        scheduler,
        "get_schedule_manager",
        lambda: SimpleNamespace(_update_last_run=MagicMock(), get_schedule=lambda _: {}),
    )
    monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda: manager)
    monkeypatch.setattr(
        "media_preview_generator.web.settings_manager.get_settings_manager",
        lambda: SimpleNamespace(processing_paused=False),
    )
    with patch.object(job_runner, "_start_job_async"):
        job_id = job_runner._start_recently_added_job_async(
            schedule_id="night",
            server_id="plex",
            library_ids=["1"],
            lookback_hours=1.0,
            library_name="Recently added",
            priority=2,
        )
    job = manager.get_job(job_id)
    if started:
        manager.start_job(job_id)
    scheduler.execute_schedule_stop("night")
    assert job.paused and job.config["pause_reasons"] == ["schedule"]
    with (
        patch("media_preview_generator.web.routes.job_runner._start_job_async") as start,
        patch("media_preview_generator.web.routes.job_runner._start_recently_added_job_async") as new,
    ):
        scheduler.execute_scheduled_job("night", config={"job_type": "recently_added"})
    assert not job.paused
    if started:
        start.assert_not_called()
    else:
        start.assert_called_once_with(job.id, job.config)
    new.assert_not_called()
    assert len(manager.get_all_jobs()) == 1


def test_hidden_retry_wait_is_visible_without_consuming_an_attempt(manager):
    parent = manager.create_job(
        kind="loudness", config={"is_retry_chain": True, "last_outcome": "running", "retry_attempt": 1}
    )
    manager.start_job(parent.id)
    child = manager.create_job(
        kind="loudness", config={"is_retry": True, "parent_job_id": parent.id, "retry_attempt": 1}
    )
    manager.mark_resource_wait(child.id, "Waiting for worker configuration")
    assert parent.status is JobStatus.PENDING
    assert parent.config["resource_wait"] == child.config["resource_wait"]
    assert parent.progress.current_item == "Waiting for worker configuration"
    assert parent.config["retry_attempt"] == child.config["retry_attempt"] == 1
