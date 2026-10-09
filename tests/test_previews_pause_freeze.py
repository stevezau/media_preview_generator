"""A Previews job paused by hand lets its running files finish; global and schedule pauses freeze them."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs.dispatcher import JobDispatcher, JobTracker
from media_preview_generator.web.jobs import PAUSED_BY_SCHEDULE
from media_preview_generator.web.routes.job_runner import job_freeze_check

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
    with (
        patch("media_preview_generator.web.settings_manager.get_settings_manager", return_value=settings),
        patch("media_preview_generator.markers.job_runner.get_settings_manager", return_value=settings),
    ):
        yield SimpleNamespace(settings=settings)


class TestFreezeVersusDispatchPause:
    def test_manual_pause_does_not_freeze_ffmpeg(self, env):
        assert job_freeze_check(FakeJobManager(paused=True), JOB_ID)() is False

    def test_global_pause_freezes_ffmpeg(self, env):
        env.settings.processing_paused = True

        assert job_freeze_check(FakeJobManager(paused=False), JOB_ID)() is True

    def test_schedule_pause_freezes_ffmpeg(self, env):
        assert job_freeze_check(FakeJobManager(paused=True, by_schedule=True), JOB_ID)() is True


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


class TestPausedJobGetsNoNewFiles:
    def test_dispatcher_skips_a_job_paused_by_hand(self):
        pool = MagicMock()
        pool._workers_lock = threading.RLock()
        dispatcher = JobDispatcher(pool)
        paused = {"value": True}
        tracker = JobTracker(
            job_id=JOB_ID,
            items=[],
            config=MagicMock(),
            registry=MagicMock(),
            callbacks={"pause_check": lambda: paused["value"]},
        )
        tracker.check_queue.append(MagicMock())
        dispatcher._trackers[JOB_ID] = tracker

        assert dispatcher._get_next_check_item() is None

        paused["value"] = False
        picked = dispatcher._get_next_check_item()
        assert picked is not None and picked[0] is tracker
