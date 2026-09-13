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


class TestJobGateReservation:
    """Issue #285 — the gate holds one slot back for high-priority work.

    A full-library regeneration runs for hours, so "first in the waiting
    line" buys an incoming webhook nothing once every slot is held by a
    scan. These tests drive :class:`JobGate` directly (no threads, no
    jobs) so the admission arithmetic is pinned independently of the
    slower end-to-end journey tests.
    """

    @staticmethod
    def _gate(cap):
        from media_preview_generator.web.job_gate import JobGate

        return JobGate(lambda: cap)

    @staticmethod
    def _admit(gate, priority):
        """acquire() that never blocks — cancel immediately if not admitted."""
        return gate.acquire(priority, cancel_check=lambda: True)

    @pytest.mark.parametrize(
        ("cap", "priority", "expected"),
        [
            # cap=1 is the escape hatch: reserving there would starve
            # normal work completely, so the reservation is skipped.
            (1, PRIORITY_HIGH, 1),
            (1, PRIORITY_NORMAL, 1),
            (1, PRIORITY_LOW, 1),
            (2, PRIORITY_HIGH, 2),
            (2, PRIORITY_NORMAL, 1),
            (2, PRIORITY_LOW, 1),
            (3, PRIORITY_HIGH, 3),
            (3, PRIORITY_NORMAL, 2),
            (3, PRIORITY_LOW, 2),
            (10, PRIORITY_HIGH, 10),
            (10, PRIORITY_NORMAL, 9),
            (10, PRIORITY_LOW, 9),
        ],
    )
    def test_effective_cap_matrix(self, cap, priority, expected):
        """Every (cap, priority) cell — high sees the whole cap, everyone
        else sees one fewer, floored at 1."""
        assert self._gate(cap).effective_cap(priority) == expected

    def test_normal_jobs_stop_one_short_of_the_cap(self):
        gate = self._gate(3)
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is False, "third normal job must hit the reservation"

    def test_high_priority_takes_the_reserved_slot(self):
        gate = self._gate(3)
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_HIGH) is True, "the held-back slot exists for exactly this"

    def test_reservation_never_pushes_past_the_cap(self):
        """The reservation redistributes slots; it must not add one.

        Exempting high priority from the cap instead would reinstate the
        webhook-burst stampede the gate was built to stop.
        """
        gate = self._gate(2)
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_HIGH) is True
        assert self._admit(gate, PRIORITY_HIGH) is False, "cap=2 means 2 in flight, high priority included"

    def test_cap_of_one_still_admits_normal_work(self):
        gate = self._gate(1)
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_HIGH) is False

    def test_release_settles_against_the_priority_it_admitted(self):
        """A finished high job must free the HIGH slot, not a normal one.

        Decrementing only the total would leave the gate believing a
        surviving normal job was the high one, so the next normal waiter
        would slide into the reserved slot — the reservation silently
        evaporating after the session's first webhook.
        """
        gate = self._gate(3)
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_HIGH) is True

        gate.release(PRIORITY_HIGH)

        assert self._admit(gate, PRIORITY_NORMAL) is False, (
            "two normal jobs are still active — that is the whole normal budget at cap=3"
        )
        assert self._admit(gate, PRIORITY_HIGH) is True, "the freed slot is the reserved one"

    def test_a_running_high_job_does_not_shrink_the_normal_budget(self):
        """High-priority slots are counted separately.

        If the gate compared total-active against ``cap - 1``, one running
        webhook job would cost a scan its slot: at cap=3 the legal steady
        state is one high plus two normal.
        """
        gate = self._gate(3)
        assert self._admit(gate, PRIORITY_HIGH) is True
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is False

    def test_release_does_not_underflow_below_zero(self):
        gate = self._gate(3)
        gate.release(PRIORITY_HIGH)
        gate.release(PRIORITY_NORMAL)
        assert gate.snapshot()[0] == 0
        # A spurious release must not hand out a bonus slot.
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is True
        assert self._admit(gate, PRIORITY_NORMAL) is False


class TestFormatWaitMessage:
    """The queued-job status line has to explain a non-obvious wait."""

    def test_plain_message_for_a_high_priority_waiter_at_a_full_gate(self):
        from media_preview_generator.web.job_gate import format_wait_message

        assert format_wait_message(3, 3, 3) == "Queued — waiting for active slot (3 of 3 busy)"

    def test_plain_message_for_a_normal_waiter_at_a_genuinely_full_gate(self):
        """A normal waiter always has ``effective_cap < cap``, but once the
        gate is actually full the reservation is not what's blocking it.

        Keying the clause on priority alone would tell a user staring at a
        saturated queue to go looking at the reservation, when the fix is
        to raise the cap or wait.
        """
        from media_preview_generator.web.job_gate import format_wait_message

        assert format_wait_message(3, 3, 2) == "Queued — waiting for active slot (3 of 3 busy)"

    def test_message_names_the_reservation_when_that_is_the_blocker(self):
        """Without this clause the dashboard reads "2 of 3 busy" next to a
        job that refuses to start, which looks like a bug in the gate."""
        from media_preview_generator.web.job_gate import format_wait_message

        assert format_wait_message(2, 3, 2) == (
            "Queued — waiting for active slot (2 of 3 busy, 1 reserved for high priority)"
        )


class TestIncomingJobPriority:
    """The Settings → Jobs knob that decides webhook / Recently Added priority."""

    @pytest.fixture
    def settings(self, tmp_path):
        import media_preview_generator.web.settings_manager as sm

        sm.reset_settings_manager()
        manager = sm.get_settings_manager(str(tmp_path))
        yield manager
        sm.reset_settings_manager()

    def test_defaults_to_high_when_unset(self, settings):
        from media_preview_generator.web.jobs import incoming_job_priority

        assert incoming_job_priority() == PRIORITY_HIGH

    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            (1, PRIORITY_HIGH),
            (2, PRIORITY_NORMAL),
            (3, PRIORITY_LOW),
            ("high", PRIORITY_HIGH),
            ("low", PRIORITY_LOW),
            # A hand-edited settings.json with junk in it falls back to
            # Normal rather than raising mid-webhook.
            ("bogus", PRIORITY_NORMAL),
        ],
    )
    def test_reads_the_configured_value(self, settings, stored, expected):
        from media_preview_generator.web.jobs import incoming_job_priority

        settings.set("incoming_job_priority", stored)
        assert incoming_job_priority() == expected
