"""The durable park handoff never loses admitted work on I/O failure or cancellation."""

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from media_preview_generator.jobs import parking
from media_preview_generator.jobs.checkpoints import read_checkpoint
from media_preview_generator.web.jobs import JobManager, JobStatus


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    manager = JobManager(str(tmp_path))
    job = manager.create_job(kind="loudness")
    manager.start_job(job.id)
    dispatcher = MagicMock()
    dispatcher.worker_pool._policies = {}
    dispatcher.worker_pool.capacity_for.return_value = {"open": 0, "reason": "configuration", "next_opening": None}
    snapshot = {
        "kind": "loudness",
        "items": [
            {
                "canonical_path": "/media/next.mkv",
                "server_id": "plex",
                "item_id_by_server": {},
                "title": "next",
                "library_id": None,
            }
        ],
        "state": {
            "successful": 2,
            "failed": 1,
            "failed_paths": ["/media/failed.mkv"],
            "outcome_counts": {"loudness_written": 2, "failed": 1},
            "publishers_aggregate": {},
            "cpu_fallback_files": 1,
            "total_items": 4,
        },
    }
    dispatcher.park_snapshot.return_value = snapshot
    dispatcher.detach_parked.return_value = True
    monkeypatch.setattr(parking, "refresh_worker_groups", lambda pool: True)
    yield manager, job, dispatcher, snapshot
    manager.close()


@pytest.mark.parametrize("failure", ["checkpoint", "job-database"])
def test_failed_handoff_keeps_registered_work_running(handoff, monkeypatch, failure):
    manager, job, dispatcher, snapshot = handoff
    if failure == "checkpoint":
        monkeypatch.setattr(parking, "write_checkpoint", MagicMock(side_effect=OSError("full")))
    else:
        monkeypatch.setattr(manager._storage, "upsert", MagicMock(side_effect=sqlite3.OperationalError("full")))
    parking.park_if_unavailable(
        dispatcher, SimpleNamespace(park_requested=True), manager, job.id, "loudness", lambda: {"retry": ["x"]}
    )
    dispatcher.detach_parked.assert_not_called()
    assert job.status is JobStatus.RUNNING and "parked_checkpoint" not in job.config
    assert dispatcher.park_snapshot.return_value is snapshot
    assert manager.get_running_jobs() == [job]


def test_old_checkpoint_cleanup_failure_does_not_fail_committed_park(handoff, monkeypatch):
    manager, job, dispatcher, _ = handoff
    manager.merge_job_config(job.id, {"parked_checkpoint": "old.json"})
    cleanup = MagicMock(side_effect=OSError("permission"))
    monkeypatch.setattr(parking, "delete_checkpoint", cleanup)
    with pytest.raises(parking.JobParked):
        parking.park_if_unavailable(
            dispatcher, SimpleNamespace(park_requested=True), manager, job.id, "loudness", lambda: {"retry": ["x"]}
        )
    dispatcher.detach_parked.assert_called_once_with(job.id)
    assert job.status is JobStatus.PENDING and job.progress.processed_items == 3
    assert job.progress.cpu_fallback_files == 1
    assert read_checkpoint(manager.config_dir, job.id, job.config["parked_checkpoint"])["bookkeeping"] == {
        "retry": ["x"]
    }
    cleanup.assert_called_once_with(manager.config_dir, job.id, "old.json")


def test_removed_job_after_snapshot_cancels_work_without_a_continuation(handoff, monkeypatch):
    manager, job, dispatcher, _ = handoff
    monkeypatch.setattr(manager, "get_job", lambda job_id: None)
    parking.park_if_unavailable(
        dispatcher, SimpleNamespace(park_requested=True), manager, job.id, "loudness", lambda: {}
    )
    dispatcher.cancel_job.assert_called_once_with(job.id)
    dispatcher.detach_parked.assert_not_called()
    assert "parked_checkpoint" not in job.config


def test_cancel_after_commit_is_terminal_and_never_restarts_checkpoint(handoff, monkeypatch):
    manager, job, dispatcher, _ = handoff
    park = manager.park_job

    def commit_then_cancel(*args, **kwargs):
        result = park(*args, **kwargs)
        manager.request_cancellation(job.id)
        return result

    monkeypatch.setattr(manager, "park_job", commit_then_cancel)
    parking.park_if_unavailable(
        dispatcher, SimpleNamespace(park_requested=True), manager, job.id, "loudness", lambda: {}
    )
    assert job.status is JobStatus.CANCELLED
    assert "parked_checkpoint" not in job.config
    dispatcher.detach_parked.assert_not_called()
    dispatcher.cancel_job.assert_called_once_with(job.id)


def _loudness_group(gid, *, enabled=True):
    return {
        "id": gid,
        "name": gid,
        "enabled": enabled,
        "availability": {"mode": "always", "windows": []},
        "members": [{"id": "m1", "resource": "cpu", "device": None, "count": 1, "job_types": ["loudness"]}],
    }


@pytest.mark.parametrize(
    ("second_group_enabled", "parks"),
    [(True, False), (False, True)],
    ids=["second group's open member keeps the job running", "no open member in any group parks it"],
)
def test_job_parks_only_when_no_group_has_an_open_member_for_its_kind(monkeypatch, second_group_enabled, parks):
    from media_preview_generator.jobs.worker import WorkerPool

    pool = WorkerPool(0, 0, [])
    pool.reconcile_groups(
        [_loudness_group("closed", enabled=False), _loudness_group("open", enabled=second_group_enabled)], []
    )
    dispatcher = MagicMock()
    dispatcher.worker_pool = pool
    dispatcher.park_snapshot.return_value = None
    monkeypatch.setattr(parking, "refresh_worker_groups", lambda pool: True)
    manager = MagicMock()

    parking.park_if_unavailable(
        dispatcher, SimpleNamespace(park_requested=False), manager, "job-1", "loudness", lambda: {}
    )

    if parks:
        dispatcher.request_park.assert_called_once_with("job-1")
    else:
        dispatcher.request_park.assert_not_called()
        dispatcher.park_snapshot.assert_not_called()
