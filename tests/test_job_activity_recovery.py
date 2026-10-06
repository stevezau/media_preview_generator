"""Crash recovery ages active work by liveness, not original start time."""

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest

from media_preview_generator.web import jobs


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.JobManager, "_start_activity_timer", lambda self: None)
    manager = jobs.JobManager(str(tmp_path))
    yield manager
    manager.close()


@pytest.mark.parametrize("kind", ["loudness", "previews", "intro_credits"])
def test_long_running_task_with_no_file_completion_survives_restart(manager, monkeypatch, kind):
    now = datetime.now(UTC)
    job = manager.create_job(kind=kind)
    original_start = (now - timedelta(days=2)).isoformat()
    job.started_at = original_start
    manager.start_job(job.id)
    job.config[jobs.LAST_ACTIVE_AT] = (now - timedelta(hours=2)).isoformat()
    monkeypatch.setattr(jobs, "_now", lambda: now)
    manager._activity_tick()
    manager.close()

    reopened = jobs.JobManager(manager.config_dir)
    try:
        revived = reopened.requeue_interrupted_jobs(max_age_minutes=60)
        assert [item.id for item in revived] == [job.id]
        assert revived[0].started_at == original_start
        assert revived[0].config[jobs.LAST_ACTIVE_AT] == now.isoformat()
        assert revived[0].status is jobs.JobStatus.PENDING
    finally:
        reopened.close()


def test_stale_heartbeat_and_recent_database_open_do_not_revive_job(manager):
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    job = manager.create_job(kind="loudness")
    manager.start_job(job.id)
    job.started_at = old
    job.config[jobs.LAST_ACTIVE_AT] = old
    manager._persist_job(job)
    manager.close()
    reopened = jobs.JobManager(manager.config_dir)
    try:
        assert reopened.requeue_interrupted_jobs(max_age_minutes=60) == []
        assert reopened.get_job(job.id).status is jobs.JobStatus.FAILED
    finally:
        reopened.close()


@pytest.mark.parametrize("log_age,revived", [(30, True), (7200, False), (-3600, False)])
def test_legacy_running_job_uses_only_recent_nonfuture_own_log(manager, log_age, revived):
    now = datetime.now(UTC)
    job = manager.create_job(kind="loudness")
    manager.start_job(job.id)
    job.started_at = (now - timedelta(days=2)).isoformat()
    job.config.pop(jobs.LAST_ACTIVE_AT)
    manager.add_log(job.id, "INFO - Still processing the current file")
    path = os.path.join(manager._job_logs_dir, f"{job.id}.log")
    stamp = now.timestamp() - log_age
    os.utime(path, (stamp, stamp))
    manager._persist_job(job)
    manager.close()
    reopened = jobs.JobManager(manager.config_dir)
    try:
        assert bool(reopened.requeue_interrupted_jobs(max_age_minutes=60)) is revived
    finally:
        reopened.close()


def test_heartbeat_does_not_change_pending_terminal_pause_or_retry_deadline(manager, monkeypatch):
    now = datetime.now(UTC)
    running = manager.create_job(kind="loudness")
    manager.start_job(running.id)
    running.config[jobs.LAST_ACTIVE_AT] = (now - timedelta(seconds=61)).isoformat()
    pending = manager.create_job(config={"scheduled_at": (now + timedelta(hours=2)).isoformat()})
    paused = manager.create_job(config={"pause_reasons": ["manual"]})
    paused.paused = True
    terminal = manager.create_job()
    terminal.status = jobs.JobStatus.COMPLETED
    manager._running_job_ids.add(terminal.id)  # A stale set entry must not resurrect terminal work.
    monkeypatch.setattr(jobs, "_now", lambda: now)
    persist = Mock(wraps=manager._persist_job)
    monkeypatch.setattr(manager, "_persist_job", persist)

    manager._activity_tick()
    manager._activity_tick()

    persist.assert_called_once_with(running)
    assert jobs.LAST_ACTIVE_AT not in pending.config
    assert jobs.LAST_ACTIVE_AT not in paused.config
    assert jobs.LAST_ACTIVE_AT not in terminal.config
    assert pending.config["scheduled_at"] == (now + timedelta(hours=2)).isoformat()
    assert paused.paused and paused.config["pause_reasons"] == ["manual"]
    assert terminal.status is jobs.JobStatus.COMPLETED


def test_late_tick_after_close_never_writes_or_schedules(manager, monkeypatch):
    job = manager.create_job()
    manager.start_job(job.id)
    manager.close()
    persist = Mock()
    schedule = Mock()
    monkeypatch.setattr(manager, "_persist_job", persist)
    monkeypatch.setattr(manager, "_start_activity_timer", schedule)
    manager._activity_tick()
    persist.assert_not_called()
    schedule.assert_not_called()


def test_job_history_cleanup_removes_loudness_ledger_and_sidecars(manager):
    from pathlib import Path

    job = manager.create_job(kind="loudness")
    directory = Path(manager.config_dir) / "job_loudness_resume"
    directory.mkdir()
    paths = [directory / f"{job.id}.sqlite3{suffix}" for suffix in ("", "-wal", "-shm")]
    for path in paths:
        path.write_bytes(b"private completion records")
    manager._delete_file_results(job.id)
    assert all(not path.exists() for path in paths)


def test_loudness_revival_preserves_saved_counts_and_deadline_until_validation(manager):
    job = manager.create_job(kind="loudness", config={"pause_reasons": ["manual"]})
    job.paused = True
    job.started_at = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    due = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    job.progress = jobs.JobProgress(
        percent=50,
        processed_items=1,
        total_items=2,
        outcome={"loudness_written": 1},
        current_files=["/media/working.mkv"],
        current_file="/media/working.mkv",
        speed="2.0x",
        workers=[{}],
        retry_eta=due,
        retry_wait_total=3600,
    )
    manager._interrupted_jobs = [job]
    assert manager.requeue_interrupted_jobs(max_age_minutes=5) == [job]
    assert job.progress.processed_items == 1
    assert job.progress.total_items == 2
    assert job.progress.percent == 50
    assert job.progress.outcome == {"loudness_written": 1}
    assert job.progress.current_item == "Saved loudness progress; native validation pending"
    assert job.progress.current_files == [] and job.progress.current_file == ""
    assert job.progress.workers == [] and job.progress.speed == "0.0x"
    assert job.progress.retry_eta == due and job.progress.retry_wait_total == 3600
    assert job.paused and job.config["pause_reasons"] == ["manual"]
