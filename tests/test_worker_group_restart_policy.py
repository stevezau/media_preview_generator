"""Real disk recovery separates durable capacity waits from interrupted work."""

import inspect
import json
import sys
import threading
from unittest.mock import patch

import pytest

from media_preview_generator.jobs.checkpoints import read_checkpoint, write_checkpoint
from media_preview_generator.web import jobs as jobs_mod
from media_preview_generator.web import settings_manager as settings_mod
from media_preview_generator.web.app import _requeue_interrupted_on_startup
from media_preview_generator.web.jobs import JobManager, JobStatus


def restart(tmp_path, monkeypatch, before, enabled, global_hold):
    before.close()
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "schema_version": 22,
                "setup_complete": True,
                "auto_requeue_on_restart": enabled,
                "requeue_max_age_minutes": 5,
                "processing_pause_reasons": [global_hold] if global_hold else [],
                "worker_groups": [],
            }
        )
    )
    monkeypatch.setattr(settings_mod, "_settings_manager", None)
    after = JobManager(str(tmp_path))
    monkeypatch.setattr(jobs_mod, "_job_manager", after)
    with patch("media_preview_generator.web.routes._start_job_async") as start:
        _requeue_interrupted_on_startup(str(tmp_path))
    return after, start


def park(manager, job):
    manager.start_job(job.id)
    manager.update_progress(job.id, processed_items=2, total_items=5, percent=40)
    manager.set_job_outcome(job.id, {"generated": 2})
    snapshot = {
        "kind": job.kind,
        "items": [
            {"canonical_path": f"/media/{i}.mkv", "server_id": "plex", "item_id_by_server": {"plex": str(i)}}
            for i in range(3)
        ],
        "state": {"successful": 2, "failed": 0, "total_items": 5, "outcome_counts": {"generated": 2}},
    }
    reference = write_checkpoint(manager.config_dir, job.id, snapshot, bookkeeping={"retry_attempt": 1})
    assert manager.park_job(job.id, reference, "off_hours", "2030-01-01T00:00:00+00:00")
    return reference


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
@pytest.mark.parametrize("state", ["parked", "never_started_wait", "interrupted_running"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("global_hold", [None, "manual", "quiet_hours"])
def test_startup_keeps_intentional_waits_under_both_recovery_preferences(
    tmp_path, monkeypatch, kind, state, enabled, global_hold
):
    before = JobManager(str(tmp_path))
    job = before.create_job(kind=kind, config={"retry_attempt": 1, "retry_not_before": "2030-01-01T00:00:00+00:00"})
    reference = None
    if state == "parked":
        reference = park(before, job)
    elif state == "never_started_wait":
        before.mark_resource_wait(job.id, "configuration")
    else:
        before.start_job(job.id)
    before.request_pause(job.id)
    before.request_pause(job.id, by_schedule=True)
    original_started = job.started_at
    original_config = dict(job.config)
    after, start = restart(tmp_path, monkeypatch, before, enabled, global_hold)
    try:
        restored = after.get_job(job.id)
        if state == "interrupted_running" and not enabled:
            assert restored.status is JobStatus.FAILED
            start.assert_not_called()
        else:
            assert restored.status is JobStatus.PENDING
            assert restored.config == original_config
            assert restored.started_at == original_started
            assert restored.paused and after.is_pause_requested(job.id)
            start.assert_called_once_with(job.id, restored.config)
            if reference:
                assert restored.progress.processed_items == 2 and restored.progress.total_items == 5
                assert restored.progress.outcome == {"generated": 2}
                assert read_checkpoint(tmp_path, job.id, reference)["state"]["successful"] == 2
        settings = settings_mod.get_settings_manager(str(tmp_path))
        assert settings.processing_pause_reasons == ([global_hold] if global_hold else [])
        assert settings.get("auto_requeue_on_restart") is enabled
    finally:
        after.close()


@pytest.mark.parametrize("dependency", ["single", "multiple", "nested", "missing", "interrupted"])
def test_disabled_crash_recovery_keeps_only_waiting_followers_of_durable_waits(tmp_path, monkeypatch, dependency):
    before = JobManager(str(tmp_path))
    parent = before.create_job()
    before.mark_resource_wait(parent.id, "configuration")
    first = before.create_job(kind="intro_credits", config={"follows_job_id": parent.id})
    cfg = (
        {"follows_job_ids": [parent.id, first.id]}
        if dependency == "multiple"
        else {
            "follows_job_id": first.id
            if dependency == "nested"
            else "missing"
            if dependency == "missing"
            else parent.id
        }
    )
    follower = before.create_job(kind="loudness", config=cfg)
    if dependency == "interrupted":
        before.start_job(follower.id)
    before.request_pause(first.id, by_schedule=True)
    after, start = restart(tmp_path, monkeypatch, before, False, "manual")
    try:
        expected = {parent.id, first.id}
        if dependency not in {"missing", "interrupted"}:
            expected.add(follower.id)
        assert {call.args[0] for call in start.call_args_list} == expected
        for call in start.call_args_list:
            saved = after.get_job(call.args[0])
            assert call.args == (saved.id, saved.config)
            assert saved.status is JobStatus.PENDING
        assert after.get_job(first.id).config["pause_reasons"] == ["schedule"]
        if follower.id not in expected:
            assert after.get_job(follower.id).status is JobStatus.FAILED
    finally:
        after.close()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("global_hold", [None, "manual", "quiet_hours"])
@pytest.mark.parametrize(
    "child_state", ["parked", "waiting", "resumed", "successor", "finished", "missing", "terminal", "ordinary"]
)
def test_weekly_capacity_wait_keeps_retry_head_through_both_boot_phases(
    tmp_path, monkeypatch, enabled, global_hold, child_state
):
    from media_preview_generator.web.app import _resume_interrupted_retry_chains_on_startup

    before = JobManager(str(tmp_path))
    head = before.create_job(
        kind="loudness",
        config={
            "is_retry_chain": True,
            "last_outcome": "running",
            "retry_attempt": 1,
            "retry_started_at": "2020-01-01T00:00:00+00:00",
        },
    )
    child = None
    if child_state != "missing":
        child = before.create_job(
            kind="loudness",
            config={
                "is_retry": True,
                "parent_job_id": head.id,
                "retry_attempt": 1,
            },
        )
        if child_state == "parked":
            park(before, child)
        elif child_state in {"waiting", "resumed", "finished", "successor"}:
            before.mark_resource_wait(child.id, "off_hours", "2030-01-01T00:00:00+00:00")
        elif child_state == "terminal":
            before.complete_job(child.id)
    after, start = restart(tmp_path, monkeypatch, before, enabled, global_hold)
    try:
        if child_state == "resumed":
            # A live watcher can clear its wait marker before the later boot
            # phase reconciles the visible chain; saved startup identity wins.
            after.resume_parked_job(child.id)
            assert "resource_wait" not in after.get_job(child.id).config
        elif child_state == "finished":
            after.complete_job(child.id)
            after.upsert_retry_chain_job(
                canonical_path="",
                basename="",
                originating_job_id=head.id,
                attempt=1,
                max_attempts=3,
                next_run_at=None,
                wait_seconds=None,
                outcome="completed",
            )
        elif child_state == "successor":
            from media_preview_generator.loudness.job import create_loudness_job

            with patch("media_preview_generator.loudness.job.start_loudness_job_async") as launch:
                successor = create_loudness_job(
                    library_name="Retry",
                    priority=2,
                    source="manual",
                    file_paths=["/media/1.mkv"],
                    parent_job_id=head.id,
                    retry_attempt=2,
                    max_retries=3,
                )
            launch.assert_called_once_with(successor.id)
            after.complete_job(child.id)
            assert after.delete_job(child.id)  # Completed history may be removed before this boot phase.
        _resume_interrupted_retry_chains_on_startup(str(tmp_path))
        restored_head = after.get_job(head.id)
        if child_state in {"parked", "waiting", "resumed"} or (child_state == "ordinary" and enabled and global_hold):
            assert restored_head.status is JobStatus.PENDING
            assert restored_head.error is None
            assert restored_head.config["retry_attempt"] == 1
            assert after.get_job(child.id).config["retry_attempt"] == 1
            assert [call.args[0] for call in start.call_args_list] == [child.id]
        elif child_state == "finished":
            assert restored_head.status is JobStatus.COMPLETED
            assert restored_head.error is None
        elif child_state == "successor":
            assert restored_head.status is JobStatus.PENDING and restored_head.error is None
            assert after.get_job(successor.id).status is JobStatus.PENDING
            assert after.get_job(successor.id).config["retry_attempt"] == 2
        else:
            assert restored_head.status is JobStatus.FAILED
            assert ">24h" in restored_head.error
        assert after.consume_restored_capacity_chain_ids() == frozenset()
        assert settings_mod.get_settings_manager(str(tmp_path)).processing_pause_reasons == (
            [global_hold] if global_hold else []
        )
    finally:
        after.close()


def test_retry_head_completion_cannot_race_boot_age_decision(tmp_path, monkeypatch):
    from media_preview_generator.web import app as app_mod

    before = JobManager(str(tmp_path))
    head = before.create_job(
        kind="loudness",
        config={
            "is_retry_chain": True,
            "last_outcome": "running",
            "retry_started_at": "2020-01-01T00:00:00+00:00",
        },
    )
    child = before.create_job(kind="loudness", config={"is_retry": True, "parent_job_id": head.id})
    before.mark_resource_wait(child.id, "off_hours")
    after, _ = restart(tmp_path, monkeypatch, before, False, None)
    attempted, completed = threading.Event(), threading.Event()
    errors = []

    def finish():
        attempted.set()
        try:
            after.complete_job(child.id)
            after.upsert_retry_chain_job(
                canonical_path="",
                basename="",
                originating_job_id=head.id,
                attempt=1,
                max_attempts=3,
                next_run_at=None,
                wait_seconds=None,
                outcome="completed",
            )
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    reconcile = app_mod._resume_interrupted_retry_chains_on_startup
    source, first_line = inspect.getsourcelines(reconcile)
    boundary = first_line + next(i for i, line in enumerate(source) if "living_children = [" in line)
    worker = threading.Thread(target=finish)
    interleaved = False

    def trace(frame, event, arg):
        nonlocal interleaved
        if event == "line" and frame.f_code is reconcile.__code__ and frame.f_lineno == boundary and not interleaved:
            interleaved = True
            worker.start()
            assert attempted.wait(2)
            # Without the shared decision lock, the actual completion runs
            # between the earlier status read and the stale-age write.
            completed.wait(0.2)
        return trace

    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        reconcile(str(tmp_path))
    finally:
        sys.settrace(previous_trace)
        if interleaved:
            worker.join(3)
    try:
        assert interleaved and completed.is_set() and not errors
        assert after.get_job(head.id).status is JobStatus.COMPLETED
        assert after.get_job(head.id).error is None
        assert after.get_job(child.id).status is JobStatus.COMPLETED
    finally:
        after.close()


@pytest.mark.parametrize("kind", ["previews", "intro_credits", "loudness"])
@pytest.mark.parametrize("state", ["pending", "running"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("hold", [None, "manual", "quiet_hours", "job_manual", "job_schedule"])
def test_old_held_jobs_recover_without_changing_restart_preference(tmp_path, monkeypatch, kind, state, enabled, hold):
    """An owner pause is intentional waiting, even without a capacity checkpoint or heartbeat."""
    before = JobManager(str(tmp_path))
    job = before.create_job(kind=kind, server_id="selected", config={"file_paths": ["/media/a.mkv"]})
    if state == "running":
        before.start_job(job.id)
    if hold in {"job_manual", "job_schedule"}:
        before.request_pause(job.id, by_schedule=hold == "job_schedule")
    job.created_at = "2020-01-01T00:00:00+00:00"
    if state == "running":
        job.started_at = job.created_at
        job.config["last_active_at"] = job.created_at
    before._persist_job(job)
    before._storage._conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (job.created_at, job.id))
    before._storage._conn.commit()
    old_config = dict(job.config)
    after, start = restart(tmp_path, monkeypatch, before, enabled, hold if hold in {"manual", "quiet_hours"} else None)
    try:
        restored = after.get_job(job.id)
        if enabled and hold:
            assert restored.status is JobStatus.PENDING
            start.assert_called_once_with(job.id, restored.config)
            assert restored.config == old_config
            assert restored.server_id == "selected"
            assert restored.created_at == "2020-01-01T00:00:00+00:00"
            assert restored.paused == (hold in {"job_manual", "job_schedule"})
        else:
            assert restored.status is JobStatus.FAILED
            start.assert_not_called()
        settings = settings_mod.get_settings_manager(str(tmp_path))
        assert settings.get("auto_requeue_on_restart") is enabled
        assert settings.get("requeue_max_age_minutes") == 5
        assert settings.processing_pause_reasons == ([hold] if hold in {"manual", "quiet_hours"} else [])
    finally:
        after.close()


@pytest.mark.parametrize("global_hold", [False, True])
@pytest.mark.parametrize("job_hold", [False, True])
def test_hold_does_not_make_malformed_recovery_date_valid(tmp_path, global_hold, job_hold):
    manager = JobManager(str(tmp_path))
    job = manager.create_job(kind="loudness")
    if job_hold:
        manager.request_pause(job.id)
    job.created_at = "not-a-date"
    manager._interrupted_jobs = [job]
    try:
        assert manager.requeue_interrupted_jobs(max_age_minutes=60, processing_paused=global_hold) == []
        assert manager.unrevived_interrupted_jobs() == [job]
    finally:
        manager.close()


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("hold", [None, "manual", "quiet_hours", "job_manual", "job_schedule"])
@pytest.mark.parametrize("child_state", ["living", "missing", "terminal"])
def test_old_held_retry_chain_requires_recovery_permission_and_living_child(
    tmp_path, monkeypatch, enabled, hold, child_state
):
    from media_preview_generator.web.app import _resume_interrupted_retry_chains_on_startup

    before = JobManager(str(tmp_path))
    head = before.create_job(
        kind="loudness",
        config={"is_retry_chain": True, "last_outcome": "running", "retry_started_at": "2020-01-01T00:00:00+00:00"},
    )
    child = None
    if child_state != "missing":
        child = before.create_job(kind="loudness", config={"is_retry": True, "parent_job_id": head.id})
        if hold in {"job_manual", "job_schedule"}:
            before.request_pause(child.id, by_schedule=hold == "job_schedule")
        child.created_at = "2020-01-01T00:00:00+00:00"
        before._persist_job(child)
        before._storage._conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (child.created_at, child.id))
        before._storage._conn.commit()
        if child_state == "terminal":
            before.complete_job(child.id)
    after, start = restart(tmp_path, monkeypatch, before, enabled, hold if hold in {"manual", "quiet_hours"} else None)
    try:
        _resume_interrupted_retry_chains_on_startup(str(tmp_path))
        kept = enabled and hold is not None and child_state == "living"
        if kept:
            assert after.get_job(head.id).status is JobStatus.PENDING
            assert after.get_job(child.id).status is JobStatus.PENDING
            start.assert_called_once_with(child.id, after.get_job(child.id).config)
        else:
            assert after.get_job(head.id).status is JobStatus.FAILED
            start.assert_not_called()
        assert after.consume_restored_capacity_chain_ids() == frozenset()
    finally:
        after.close()
