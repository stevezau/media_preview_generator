"""
Tests for job queue priority feature.

Covers: Job model priority field, backward compatibility with old jobs.json,
priority-aware dispatcher scheduling, and the priority update API.
"""

import os
from unittest.mock import MagicMock

import pytest

from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker
from media_preview_generator.jobs.worker import WorkerPool
from media_preview_generator.web.jobs import (
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    Job,
    JobManager,
    parse_priority,
)


@pytest.fixture(autouse=True)
def _reset_job_manager():
    """Reset global job manager so tests can create their own."""
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


@pytest.fixture
def config_dir(tmp_path):
    return str(tmp_path / "config")


def _make_config():
    config = MagicMock()
    config.cpu_threads = 1
    config.gpu_threads = 0
    config.worker_pool_timeout = 5
    return config


# ---------------------------------------------------------------------------
# parse_priority
# ---------------------------------------------------------------------------


class TestParsePriority:
    def test_int_values(self):
        assert parse_priority(1) == PRIORITY_HIGH
        assert parse_priority(2) == PRIORITY_NORMAL
        assert parse_priority(3) == PRIORITY_LOW

    def test_string_labels(self):
        assert parse_priority("high") == PRIORITY_HIGH
        assert parse_priority("Normal") == PRIORITY_NORMAL
        assert parse_priority("LOW") == PRIORITY_LOW

    def test_invalid_defaults_to_normal(self):
        assert parse_priority(99) == PRIORITY_NORMAL
        assert parse_priority("bogus") == PRIORITY_NORMAL
        assert parse_priority(None) == PRIORITY_NORMAL


# ---------------------------------------------------------------------------
# Job dataclass
# ---------------------------------------------------------------------------


class TestJobPriority:
    def test_default_priority(self):
        job = Job(id="test-1")
        assert job.priority == PRIORITY_NORMAL

    def test_explicit_priority(self):
        job = Job(id="test-2", priority=PRIORITY_HIGH)
        assert job.priority == PRIORITY_HIGH

    def test_priority_in_to_dict(self):
        job = Job(id="test-3", priority=PRIORITY_LOW)
        d = job.to_dict()
        assert d["priority"] == PRIORITY_LOW

    def test_backward_compat_missing_priority(self):
        """Old jobs.json entries without priority should default to normal."""
        data = {
            "id": "old-job",
            "status": "completed",
            "created_at": "2025-01-01T00:00:00+00:00",
            "library_name": "Movies",
            "config": {},
        }
        job = Job(**data)
        assert job.priority == PRIORITY_NORMAL

    def test_priority_from_string_in_constructor(self):
        """Priority should accept string labels when loaded from JSON."""
        job = Job(id="str-pri", priority="high")
        assert job.priority == PRIORITY_HIGH


# ---------------------------------------------------------------------------
# JobManager.create_job with priority
# ---------------------------------------------------------------------------


class TestJobManagerPriority:
    def test_create_job_default_priority(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Movies")
        assert job.priority == PRIORITY_NORMAL

    def test_create_job_with_priority(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Movies", priority=PRIORITY_HIGH)
        assert job.priority == PRIORITY_HIGH

    def test_update_job_priority(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="Movies", priority=PRIORITY_NORMAL)
        updated = jm.update_job_priority(job.id, PRIORITY_LOW)
        assert updated is not None
        assert updated.priority == PRIORITY_LOW

    def test_update_job_priority_not_found(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        result = jm.update_job_priority("nonexistent", PRIORITY_HIGH)
        assert result is None

    def test_priority_persists_across_reload(self, config_dir):
        os.makedirs(config_dir, exist_ok=True)
        jm = JobManager(config_dir=config_dir)
        job = jm.create_job(library_name="TV", priority=PRIORITY_HIGH)
        jm.complete_job(job.id)

        jm2 = JobManager(config_dir=config_dir)
        reloaded = jm2.get_job(job.id)
        assert reloaded is not None
        assert reloaded.priority == PRIORITY_HIGH


# ---------------------------------------------------------------------------
# JobTracker priority
# ---------------------------------------------------------------------------


class TestJobTrackerPriority:
    def test_default_priority(self):
        tracker = JobTracker(
            job_id="j1",
            items=[("k1", "t1", "movie")],
            config=_make_config(),
            registry=MagicMock(),
        )
        assert tracker.priority == PRIORITY_NORMAL

    def test_explicit_priority(self):
        tracker = JobTracker(
            job_id="j2",
            items=[("k1", "t1", "movie")],
            config=_make_config(),
            registry=MagicMock(),
            priority=PRIORITY_HIGH,
        )
        assert tracker.priority == PRIORITY_HIGH

    def test_submission_order_increases(self):
        t1 = JobTracker(
            job_id="j1",
            items=[("k1", "t1", "movie")],
            config=_make_config(),
            registry=MagicMock(),
        )
        t2 = JobTracker(
            job_id="j2",
            items=[("k1", "t1", "movie")],
            config=_make_config(),
            registry=MagicMock(),
        )
        assert t2.submission_order > t1.submission_order


# ---------------------------------------------------------------------------
# Dispatcher priority-aware scheduling
# ---------------------------------------------------------------------------


class TestDispatcherPriority:
    def _make_dispatcher(self):
        pool = MagicMock(spec=WorkerPool)
        dispatcher = JobDispatcher(pool)
        # Prevent background dispatch thread from consuming items
        dispatcher._ensure_dispatch_running = lambda: None
        return dispatcher

    def _add_tracker(self, dispatcher, job_id, items, priority):
        """Register a tracker directly (no submit_items → no background
        checking threads), so the priority picker can be exercised
        synchronously and deterministically.
        """
        tracker = JobTracker(
            job_id=job_id,
            items=items,
            config=_make_config(),
            registry=MagicMock(),
            priority=priority,
        )
        with dispatcher._trackers_lock:
            dispatcher._trackers[job_id] = tracker
        return tracker

    def test_high_priority_dispatched_first(self):
        """Items from a high-priority job should be checked before normal.

        The priority-aware entry picker is ``_get_next_check_item`` now
        (items enter the checking queue first); it shares the same
        (priority, submission_order) sort the processing picker uses.
        """
        dispatcher = self._make_dispatcher()
        self._add_tracker(dispatcher, "low-job", [("k1", "Low Item", "movie")], PRIORITY_LOW)
        self._add_tracker(dispatcher, "high-job", [("k2", "High Item", "movie")], PRIORITY_HIGH)

        picked = dispatcher._get_next_check_item()
        assert picked is not None
        assert picked[0].job_id == "high-job"

        picked2 = dispatcher._get_next_check_item()
        assert picked2 is not None
        assert picked2[0].job_id == "low-job"

    def test_same_priority_fifo(self):
        """Within the same priority, earlier submissions should come first."""
        dispatcher = self._make_dispatcher()
        self._add_tracker(dispatcher, "first", [("k1", "First", "movie")], PRIORITY_NORMAL)
        self._add_tracker(dispatcher, "second", [("k2", "Second", "movie")], PRIORITY_NORMAL)

        picked = dispatcher._get_next_check_item()
        assert picked is not None
        assert picked[0].job_id == "first"

    def test_update_job_priority_reorders(self):
        """Changing a job's priority should affect subsequent dispatch order."""
        dispatcher = self._make_dispatcher()
        self._add_tracker(dispatcher, "job-a", [("k1", "A1", "movie"), ("k2", "A2", "movie")], PRIORITY_NORMAL)
        self._add_tracker(dispatcher, "job-b", [("k3", "B1", "movie")], PRIORITY_NORMAL)

        dispatcher.update_job_priority("job-b", PRIORITY_HIGH)

        picked = dispatcher._get_next_check_item()
        assert picked is not None
        assert picked[0].job_id == "job-b"

    def test_empty_queue_returns_none(self):
        dispatcher = self._make_dispatcher()
        assert dispatcher._get_next_check_item() is None
        assert dispatcher._get_next_item() is None
