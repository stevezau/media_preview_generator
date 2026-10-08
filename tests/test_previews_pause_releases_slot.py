"""A Previews job paused by hand hands its JobGate slot back; global and schedule pauses keep it."""

import threading
from collections import deque
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_PREVIEWS
from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker
from media_preview_generator.web.jobs import PAUSED_BY_SCHEDULE, PRIORITY_HIGH, PRIORITY_NORMAL
from media_preview_generator.web.routes.job_runner import (
    _paused_by_hand,
    _wait_releasing_slot_while_paused,
    job_freeze_check,
)

JOB_ID = "job-1"


class FakeJobManager:
    def __init__(self, paused=False, by_schedule=False):
        self.paused = paused
        self.by_schedule = by_schedule
        self.logs = []

    def is_pause_requested(self, job_id):
        return self.paused

    def is_cancellation_requested(self, job_id):
        return False

    def get_job(self, job_id):
        return SimpleNamespace(config={PAUSED_BY_SCHEDULE: True} if self.by_schedule else {})

    def add_log(self, job_id, message):
        self.logs.append(message)


@pytest.fixture
def env():
    settings = SimpleNamespace(processing_paused=False)
    gate = MagicMock()
    gate.has_request.return_value = False
    gate.acquire.return_value = True
    with (
        patch("media_preview_generator.web.job_gate.get_job_gate", return_value=gate),
        patch("media_preview_generator.web.settings_manager.get_settings_manager", return_value=settings),
        patch("media_preview_generator.markers.job_runner.get_settings_manager", return_value=settings),
    ):
        yield SimpleNamespace(settings=settings, gate=gate)


def _tracker(active_processing=0, active_checks=0):
    return SimpleNamespace(
        active_processing=active_processing,
        active_checks=active_checks,
        done_event=MagicMock(is_set=lambda: False),
        slot_released=False,
    )


def _dispatcher(worker=None):
    pool = MagicMock()
    pool._snapshot_workers.return_value = []
    pool._find_available_worker.return_value = worker
    return JobDispatcher(pool)


def _tick(jm, slot, tracker=None, priority=PRIORITY_NORMAL, park=None, on_wait=None, dispatcher=None):
    _wait_releasing_slot_while_paused(
        dispatcher or _dispatcher(),
        tracker or _tracker(),
        jm,
        JOB_ID,
        slot,
        park_check=park or (lambda: None),
        live_priority=lambda: priority,
        on_wait=on_wait or (lambda *a: None),
    )


def _slot(held=True, priority=PRIORITY_HIGH):
    return {"held": held, "priority": priority, "kind": JOB_KIND_PREVIEWS}


class TestSlotRelease:
    def test_releases_with_admitted_priority_and_kind_when_paused_by_hand_and_idle(self, env):
        jm, slot = FakeJobManager(paused=True), _slot(priority=PRIORITY_HIGH)

        _tick(jm, slot, priority=PRIORITY_NORMAL)

        env.gate.release.assert_called_once_with(PRIORITY_HIGH, kind=JOB_KIND_PREVIEWS)
        assert slot["held"] is False
        assert jm.logs == ["INFO - Paused; active slot handed back until resume"]

    def test_keeps_slot_when_a_file_is_in_flight(self, env):
        slot = _slot()

        _tick(FakeJobManager(paused=True), slot, tracker=_tracker(active_processing=1))

        env.gate.release.assert_not_called()
        assert slot["held"] is True

    def test_keeps_slot_when_a_check_is_in_flight(self, env):
        slot = _slot()

        _tick(FakeJobManager(paused=True), slot, tracker=_tracker(active_checks=1))

        env.gate.release.assert_not_called()
        assert slot["held"] is True

    def test_releases_once_the_in_flight_file_finishes(self, env):
        jm, slot, tracker = FakeJobManager(paused=True), _slot(), _tracker(active_processing=1)

        _tick(jm, slot, tracker=tracker)
        tracker.active_processing = 0
        _tick(jm, slot, tracker=tracker)

        env.gate.release.assert_called_once_with(PRIORITY_HIGH, kind=JOB_KIND_PREVIEWS)

    def test_keeps_slot_when_all_processing_is_paused(self, env):
        env.settings.processing_paused = True
        slot = _slot()

        _tick(FakeJobManager(paused=True), slot)

        env.gate.release.assert_not_called()
        assert slot["held"] is True

    def test_keeps_slot_when_only_global_pause_is_set(self, env):
        env.settings.processing_paused = True
        slot = _slot()

        _tick(FakeJobManager(paused=False), slot)

        env.gate.release.assert_not_called()
        env.gate.acquire.assert_not_called()

    def test_keeps_slot_when_paused_by_schedule(self, env):
        slot = _slot()

        _tick(FakeJobManager(paused=True, by_schedule=True), slot)

        env.gate.release.assert_not_called()
        assert slot["held"] is True

    def test_does_nothing_when_not_paused_and_slot_held(self, env):
        _tick(FakeJobManager(paused=False), _slot())

        env.gate.release.assert_not_called()
        env.gate.acquire.assert_not_called()

    def test_does_not_release_twice(self, env):
        jm, slot = FakeJobManager(paused=True), _slot()

        _tick(jm, slot)
        _tick(jm, slot)

        env.gate.release.assert_called_once()

    def test_park_check_runs_before_the_slot_decision(self, env):
        order = []
        env.gate.release.side_effect = lambda *a, **k: order.append("release")

        _tick(FakeJobManager(paused=True), _slot(), park=lambda: order.append("park"))

        assert order == ["park", "release"]


class TestSlotReacquire:
    def test_reacquires_at_live_priority_with_kind_on_resume(self, env):
        slot = _slot(held=False, priority=PRIORITY_HIGH)
        on_wait = MagicMock()

        _tick(FakeJobManager(paused=False), slot, priority=PRIORITY_NORMAL, on_wait=on_wait)

        call = env.gate.acquire.call_args
        assert call.kwargs["priority"] == PRIORITY_NORMAL
        assert call.kwargs["kind"] == JOB_KIND_PREVIEWS
        assert call.kwargs["on_wait"] is on_wait
        assert slot["held"] is True
        assert slot["priority"] == PRIORITY_NORMAL

    def test_stays_unslotted_while_the_gate_is_full_and_acquire_gives_up(self, env):
        env.gate.acquire.return_value = False
        slot = _slot(held=False)

        _tick(FakeJobManager(paused=False), slot)

        env.gate.acquire.assert_called_once()
        assert slot["held"] is False

    def test_takes_the_slot_after_a_full_gate_admits(self, env):
        results = iter([False, True])
        env.gate.acquire.side_effect = lambda **kwargs: next(results)
        slot = _slot(held=False)
        jm = FakeJobManager(paused=False)

        _tick(jm, slot)
        assert slot["held"] is False
        _tick(jm, slot)

        assert slot["held"] is True
        assert env.gate.acquire.call_count == 2

    def test_acquire_cancel_check_stops_on_repause_cancel_or_done(self, env):
        jm, tracker, slot = FakeJobManager(paused=False), _tracker(), _slot(held=False)
        _tick(jm, slot, tracker=tracker)
        cancel_check = env.gate.acquire.call_args.kwargs["cancel_check"]

        assert cancel_check() is False
        jm.paused = True
        assert cancel_check() is True
        jm.paused = False
        tracker.done_event = MagicMock(is_set=lambda: True)
        assert cancel_check() is True

    def test_does_not_acquire_while_still_paused_by_hand(self, env):
        slot = _slot(held=False)

        _tick(FakeJobManager(paused=True), slot)

        env.gate.acquire.assert_not_called()
        env.gate.release.assert_not_called()

    def test_does_not_acquire_while_manually_and_globally_paused(self, env):
        env.settings.processing_paused = True
        slot = _slot(held=False)

        _tick(FakeJobManager(paused=True), slot)

        env.gate.acquire.assert_not_called()
        assert slot["held"] is False


class TestFreezeVersusDispatchPause:
    def test_manual_pause_does_not_freeze_ffmpeg(self, env):
        assert job_freeze_check(FakeJobManager(paused=True), JOB_ID)() is False

    def test_global_pause_freezes_ffmpeg(self, env):
        env.settings.processing_paused = True

        assert job_freeze_check(FakeJobManager(paused=False), JOB_ID)() is True

    def test_schedule_pause_freezes_ffmpeg(self, env):
        assert job_freeze_check(FakeJobManager(paused=True, by_schedule=True), JOB_ID)() is True

    @pytest.mark.parametrize(
        ("processing_paused", "paused", "by_schedule", "expected"),
        [
            (False, False, False, False),
            (False, True, False, True),
            (True, True, False, False),
            (False, True, True, False),
            (True, False, False, False),
        ],
    )
    def test_paused_by_hand_matrix(self, env, processing_paused, paused, by_schedule, expected):
        env.settings.processing_paused = processing_paused

        assert _paused_by_hand(FakeJobManager(paused=paused, by_schedule=by_schedule), JOB_ID) is expected


class TestTrackerFreezeCheck:
    def _make(self, callbacks):
        return JobTracker(job_id=JOB_ID, items=[], config=MagicMock(), registry=MagicMock(), callbacks=callbacks)

    def test_freeze_check_is_separate_from_dispatch_pause(self):
        dispatch, freeze = MagicMock(return_value=True), MagicMock(return_value=False)

        tracker = self._make({"pause_check": dispatch, "freeze_check": freeze})

        assert tracker.pause_check is dispatch
        assert tracker.freeze_check is freeze
        assert tracker.is_paused() is True

    def test_freeze_check_defaults_to_pause_check_for_other_runners(self):
        dispatch = MagicMock()

        tracker = self._make({"pause_check": dispatch})

        assert tracker.freeze_check is dispatch


class TestReleaseVersusDispatch:
    """The idle check + release is atomic against the dispatcher's pick + increment (real threads, real tracker)."""

    def _tracker(self, slot, pause_check):
        tracker = JobTracker(
            job_id=JOB_ID,
            items=[],
            config=MagicMock(),
            registry=MagicMock(),
            callbacks={"pause_check": pause_check},
        )
        tracker.item_queue = deque(["item-1"])
        return tracker

    def _register(self, dispatcher, tracker):
        dispatcher._trackers[tracker.job_id] = tracker

    def test_item_not_started_when_slot_released_between_pause_check_and_pick(self, env):
        worker = MagicMock()
        dispatcher = _dispatcher(worker)
        slot = _slot(priority=PRIORITY_HIGH)
        checked, go, first = threading.Event(), threading.Event(), [True]

        def pause_check():
            value = not slot["held"]
            if first[0]:
                first[0] = False
                checked.set()
                assert go.wait(5)
            return value

        tracker = self._tracker(slot, pause_check)
        self._register(dispatcher, tracker)
        assign = threading.Thread(target=dispatcher._assign_tasks)
        assign.start()
        assert checked.wait(5)

        _tick(FakeJobManager(paused=True), slot, tracker=tracker, dispatcher=dispatcher)
        go.set()
        assign.join(5)

        env.gate.release.assert_called_once_with(PRIORITY_HIGH, kind=JOB_KIND_PREVIEWS)
        assert slot["held"] is False
        worker.assign_task.assert_not_called()
        assert tracker.active_processing == 0
        assert list(tracker.item_queue) == ["item-1"]

    def test_release_refused_when_a_file_got_in_flight_first(self, env):
        entered, go = threading.Event(), threading.Event()
        worker = MagicMock()
        worker.assign_task.side_effect = lambda *a, **k: (entered.set(), go.wait(5))
        dispatcher = _dispatcher(worker)
        slot = _slot()
        tracker = self._tracker(slot, lambda: not slot["held"])
        self._register(dispatcher, tracker)
        assign = threading.Thread(target=dispatcher._assign_tasks)
        assign.start()
        assert entered.wait(5)
        releaser = threading.Thread(
            target=_tick,
            args=(FakeJobManager(paused=True), slot),
            kwargs={"tracker": tracker, "dispatcher": dispatcher},
        )
        releaser.start()
        releaser.join(0.3)
        blocked_while_pick_in_progress = releaser.is_alive()
        go.set()
        assign.join(5)
        releaser.join(5)

        assert blocked_while_pick_in_progress
        assert tracker.active_processing == 1
        assert slot["held"] is True
        env.gate.release.assert_not_called()

    def test_check_not_started_when_slot_released_first(self, env):
        dispatcher = _dispatcher()
        slot = _slot()
        tracker = self._tracker(slot, lambda: not slot["held"])
        tracker.check_queue = deque(["item-1"])
        self._register(dispatcher, tracker)

        _tick(FakeJobManager(paused=True), slot, tracker=tracker, dispatcher=dispatcher)

        assert dispatcher._get_next_check_item() is None
        assert tracker.active_checks == 0
        assert list(tracker.check_queue) == ["item-1"]

    def test_pause_check_never_runs_under_trackers_lock_in_assign_tasks(self, env):
        dispatcher = _dispatcher(MagicMock())
        slot = _slot()
        owned_during_call = []

        def pause_check():
            owned_during_call.append(dispatcher._trackers_lock._is_owned())
            return not slot["held"]

        tracker = self._tracker(slot, pause_check)
        tracker.item_queue = deque([SimpleNamespace(canonical_path="/data/x.mkv")])
        self._register(dispatcher, tracker)

        dispatcher._assign_tasks()

        assert owned_during_call
        assert not any(owned_during_call)

    def test_pause_check_never_runs_under_trackers_lock_when_a_pick_is_skipped(self, env):
        dispatcher = _dispatcher(MagicMock())
        slot = _slot()
        tracker = self._tracker(slot, lambda: not slot["held"])
        self._register(dispatcher, tracker)
        _tick(FakeJobManager(paused=True), slot, tracker=tracker, dispatcher=dispatcher)
        owned_during_call = []
        tracker.pause_check = lambda: owned_during_call.append(dispatcher._trackers_lock._is_owned()) or True

        dispatcher._assign_tasks()

        assert owned_during_call == [False]

    def test_release_sets_slot_released_and_reacquire_clears_it(self, env):
        dispatcher = _dispatcher()
        slot = _slot()
        tracker = self._tracker(slot, lambda: not slot["held"])
        jm = FakeJobManager(paused=True)
        assert tracker.slot_released is False

        _tick(jm, slot, tracker=tracker, dispatcher=dispatcher)
        assert tracker.slot_released is True

        _tick(jm, slot, tracker=tracker, dispatcher=dispatcher)
        assert tracker.slot_released is True

        jm.paused = False
        _tick(jm, slot, tracker=tracker, dispatcher=dispatcher)
        assert slot["held"] is True
        assert tracker.slot_released is False

    def test_slot_released_stays_set_when_reacquire_gives_up(self, env):
        env.gate.acquire.return_value = False
        dispatcher = _dispatcher()
        slot = _slot(held=False)
        tracker = self._tracker(slot, lambda: not slot["held"])
        tracker.slot_released = True

        _tick(FakeJobManager(paused=False), slot, tracker=tracker, dispatcher=dispatcher)

        assert tracker.slot_released is True

    def test_slot_released_not_set_when_a_file_is_in_flight(self, env):
        dispatcher = _dispatcher()
        slot = _slot()
        tracker = self._tracker(slot, lambda: False)
        tracker.active_processing = 1

        _tick(FakeJobManager(paused=True), slot, tracker=tracker, dispatcher=dispatcher)

        assert tracker.slot_released is False

    def test_gate_release_failure_restores_held_and_leaves_flag_clear(self, env):
        env.gate.release.side_effect = RuntimeError("boom")
        dispatcher = _dispatcher()
        slot = _slot()
        tracker = self._tracker(slot, lambda: False)

        with pytest.raises(RuntimeError):
            _tick(FakeJobManager(paused=True), slot, tracker=tracker, dispatcher=dispatcher)

        assert slot["held"] is True
        assert tracker.slot_released is False

    def test_all_slots_released_only_when_every_live_job_gave_its_slot_back(self):
        dispatcher = _dispatcher()
        assert dispatcher._all_slots_released() is False
        first, second = self._tracker(_slot(), lambda: False), self._tracker(_slot(), lambda: False)
        second.job_id = "job-2"
        self._register(dispatcher, first)
        self._register(dispatcher, second)
        first.slot_released = True
        assert dispatcher._all_slots_released() is False
        second.slot_released = True
        assert dispatcher._all_slots_released() is True

    def test_release_when_idle_runs_release_fn_only_when_idle(self):
        dispatcher = _dispatcher()
        tracker = self._tracker(_slot(), lambda: False)
        release = MagicMock()

        tracker.active_checks = 1
        assert dispatcher.release_when_idle(tracker, release) is False
        tracker.active_checks, tracker.active_processing = 0, 1
        assert dispatcher.release_when_idle(tracker, release) is False
        release.assert_not_called()
        tracker.active_processing = 0
        assert dispatcher.release_when_idle(tracker, release) is True
        release.assert_called_once_with()
