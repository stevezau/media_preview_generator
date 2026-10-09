"""
Tests for job queue priority feature.

Covers: Job model priority field, backward compatibility with old jobs.json,
priority-aware dispatcher scheduling, and the priority update API.
"""

import os
import threading
import time
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

    def test_check_pick_rotates_between_same_priority_jobs(self):
        """A later one-file job is checked 2nd, not after every file of an earlier big job."""
        dispatcher = self._make_dispatcher()
        big = self._add_tracker(
            dispatcher, "big-scan", [(f"k{i}", f"Item {i}", "movie") for i in range(5)], PRIORITY_NORMAL
        )
        self._add_tracker(dispatcher, "webhook", [("w1", "Webhook item", "movie")], PRIORITY_NORMAL)

        order = []
        while (picked := dispatcher._get_next_check_item()) is not None:
            order.append(picked[0].job_id)

        assert order == ["big-scan", "webhook", "big-scan", "big-scan", "big-scan", "big-scan"]
        assert big.last_picked > 0

    def test_check_pick_still_lets_higher_priority_win_outright(self):
        dispatcher = self._make_dispatcher()
        self._add_tracker(dispatcher, "big-scan", [(f"k{i}", f"Item {i}", "movie") for i in range(3)], PRIORITY_NORMAL)
        self._add_tracker(dispatcher, "urgent", [(f"u{i}", f"Urgent {i}", "movie") for i in range(2)], PRIORITY_HIGH)

        order = []
        while (picked := dispatcher._get_next_check_item()) is not None:
            order.append(picked[0].job_id)

        assert order == ["urgent", "urgent", "big-scan", "big-scan", "big-scan"]

    def _assign_with_one_worker(self, dispatcher, picks):
        """Run ``_assign_tasks`` against a pool that has one free worker for ``picks`` assignments."""
        worker = MagicMock()
        remaining = [picks]

        def find_available_worker(claim, kind):
            if remaining[0] == 0:
                return None
            remaining[0] -= 1
            return worker

        dispatcher.worker_pool._workers_lock = threading.RLock()
        dispatcher.worker_pool._find_available_worker.side_effect = find_available_worker
        dispatcher._assign_tasks()
        return [call.kwargs["job_id"] for call in worker.assign_task.call_args_list]

    def test_worker_pick_rotates_between_same_priority_jobs(self):
        """With one worker, a later one-file job gets the 2nd file handed out, not the 6th."""
        dispatcher = self._make_dispatcher()
        big = self._add_tracker(dispatcher, "big-scan", [], PRIORITY_NORMAL)
        big.item_queue.extend(MagicMock() for _ in range(5))
        webhook = self._add_tracker(dispatcher, "webhook", [], PRIORITY_NORMAL)
        webhook.item_queue.append(MagicMock())

        assert self._assign_with_one_worker(dispatcher, picks=3) == ["big-scan", "webhook", "big-scan"]

    def test_worker_pick_still_lets_higher_priority_win_outright(self):
        dispatcher = self._make_dispatcher()
        big = self._add_tracker(dispatcher, "big-scan", [], PRIORITY_NORMAL)
        big.item_queue.extend(MagicMock() for _ in range(3))
        urgent = self._add_tracker(dispatcher, "urgent", [], PRIORITY_HIGH)
        urgent.item_queue.extend([MagicMock(), MagicMock()])

        assert self._assign_with_one_worker(dispatcher, picks=3) == ["urgent", "urgent", "big-scan"]

    def test_progress_says_waiting_for_a_free_worker_until_the_first_file_is_picked(self):
        dispatcher = self._make_dispatcher()
        tracker = self._add_tracker(dispatcher, "job", [("k1", "Item", "movie")], PRIORITY_NORMAL)

        assert tracker.progress_message() == "Waiting for a free worker"
        dispatcher._get_next_check_item()
        assert tracker.progress_message() == "Checking existing previews… 0/1"


class TestJobGateOnWait:
    """``on_wait`` writes the job's queued state (a database upsert and a SocketIO emit), so it runs with the gate
    lock released: every other acquire and release would otherwise queue behind that I/O."""

    @staticmethod
    def _gate(make_gate, slots, poll_s):
        gate = make_gate(slots)
        gate._POLL_SECONDS = poll_s
        return gate

    @staticmethod
    def _another_thread_can_take(lock) -> bool:
        took: list[bool] = []

        def probe():
            got = lock.acquire(blocking=False)
            took.append(got)
            if got:
                lock.release()

        # The gate's Condition wraps an RLock, which the waiting thread itself could always re-enter: only another
        # thread shows whether the lock is really free.
        prober = threading.Thread(target=probe)
        prober.start()
        prober.join(timeout=5)
        return took == [True]

    def test_on_wait_runs_with_the_gate_lock_released(self, make_gate):
        gate = self._gate(make_gate, 1, poll_s=0.05)
        assert gate.acquire(PRIORITY_NORMAL, cancel_check=lambda: False) is True
        free_during_on_wait: list[bool] = []

        def on_wait(active):
            free_during_on_wait.append(self._another_thread_can_take(gate._cond))

        admitted = gate.acquire(PRIORITY_NORMAL, cancel_check=lambda: bool(free_during_on_wait), on_wait=on_wait)

        assert admitted is False
        assert free_during_on_wait == [True]
        assert gate.snapshot() == (1, 0, 1), "the cancelled waiter left the heap and took no slot"

    def test_a_slot_freed_while_on_wait_runs_is_taken_without_waiting_out_the_poll(self, make_gate):
        gate = self._gate(make_gate, 1, poll_s=5.0)
        assert gate.acquire(PRIORITY_NORMAL, cancel_check=lambda: False) is True
        calls: list[int] = []

        def on_wait(active):
            calls.append(active)
            gate.release()  # the running job ends while the queued state is being written

        started = time.monotonic()
        admitted = gate.acquire(PRIORITY_NORMAL, cancel_check=lambda: False, on_wait=on_wait)

        assert admitted is True
        assert time.monotonic() - started < 2.0, "the freed slot's notify came before the wait; look again first"
        assert calls == [1]
        assert gate.snapshot() == (1, 0, 1)

    def test_waiters_are_admitted_in_priority_then_submission_order(self, make_gate):
        gate = self._gate(make_gate, 1, poll_s=0.05)
        assert gate.acquire(PRIORITY_NORMAL, cancel_check=lambda: False) is True
        admitted: list[str] = []
        lock = threading.Lock()

        def on_wait(active):
            time.sleep(0.01)  # widen the window in which the gate lock is down

        def wait_for_slot(name, priority):
            if gate.acquire(priority, cancel_check=lambda: False, on_wait=on_wait):
                with lock:
                    admitted.append(name)

        threads = []
        for name, priority in [("low", PRIORITY_LOW), ("normal-1", PRIORITY_NORMAL), ("high", PRIORITY_HIGH)]:
            threads.append(threading.Thread(target=wait_for_slot, args=(name, priority)))
            threads[-1].start()
            _wait_until(lambda n=len(threads): gate.snapshot()[1] == n)
        threads.append(threading.Thread(target=wait_for_slot, args=("normal-2", PRIORITY_NORMAL)))
        threads[-1].start()
        _wait_until(lambda: gate.snapshot()[1] == 4)

        # Each release settles the slot of the job admitted before it: the first holder, then each waiter in turn.
        for count in range(1, 5):
            gate.release()
            _wait_until(lambda n=count: len(admitted) == n)
        for thread in threads:
            thread.join(timeout=5)

        assert admitted == ["high", "normal-1", "normal-2", "low"]

    def test_a_cancelled_waiter_leaves_and_the_next_one_takes_the_slot(self, make_gate):
        gate = self._gate(make_gate, 1, poll_s=0.05)
        assert gate.acquire(PRIORITY_NORMAL, cancel_check=lambda: False) is True
        cancel_first = threading.Event()
        results: dict[str, bool] = {}

        def wait_for_slot(name, cancel_check):
            results[name] = gate.acquire(PRIORITY_NORMAL, cancel_check=cancel_check, on_wait=lambda *_: None)

        first = threading.Thread(target=wait_for_slot, args=("first", cancel_first.is_set))
        first.start()
        _wait_until(lambda: gate.snapshot()[1] == 1)
        second = threading.Thread(target=wait_for_slot, args=("second", lambda: False))
        second.start()
        _wait_until(lambda: gate.snapshot()[1] == 2)

        cancel_first.set()
        first.join(timeout=5)
        gate.release()
        second.join(timeout=5)

        assert results == {"first": False, "second": True}
        assert gate.snapshot() == (1, 0, 1)


def _wait_until(condition, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the gate"
        time.sleep(0.005)


class TestFormatWaitMessage:
    """The queued-job status line says how many start-up places are taken."""

    def test_message_names_the_start_up_slots(self):
        from media_preview_generator.web.job_gate import format_wait_message

        assert format_wait_message(3) == "Queued — waiting to start (3 of 3 jobs starting up)"


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
