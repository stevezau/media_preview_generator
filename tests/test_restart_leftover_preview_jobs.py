"""Preview jobs a restart doesn't revive are settled, so a later resume can't start them.

A PENDING preview job older than the requeue window (or any, with auto-requeue
off) used to stay PENDING with no thread behind it. The next resume — Resume
all, a quiet-hours window ending, a Reprocess while paused — drains every
PENDING job, so it started days-old work the restart had decided not to revive.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

import media_preview_generator.web.jobs as jobs_mod
from media_preview_generator.web.app import _requeue_interrupted_on_startup
from media_preview_generator.web.jobs import JobManager, JobStatus
from media_preview_generator.web.routes.job_runner import resume_running_and_drain_pending


@pytest.fixture(autouse=True)
def _reset_job_manager():
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


def _restart_with(config_dir: str, *, stale_hours: float) -> tuple[JobManager, str, str]:
    """Persist one stale and one fresh PENDING preview job, then load them as a restarted process would."""
    os.makedirs(config_dir, exist_ok=True)
    before = JobManager(config_dir=config_dir)
    stale = before.create_job(library_name="old scan")
    fresh = before.create_job(library_name="new scan")
    stale.created_at = (datetime.now(UTC) - timedelta(hours=stale_hours)).isoformat()
    # An upsert keeps a row's created_at, so re-insert it.
    before._storage.delete(stale.id)
    before._storage.upsert(stale)

    after = JobManager(config_dir=config_dir)
    with jobs_mod._job_lock:
        jobs_mod._job_manager = after
    return after, stale.id, fresh.id


def _boot(auto_requeue: bool, *, paused: bool = False) -> list[str]:
    """Run the startup requeue; return the ids it started."""
    settings = {"auto_requeue_on_restart": auto_requeue, "requeue_max_age_minutes": 720}
    with (
        patch("media_preview_generator.web.settings_manager.get_settings_manager") as mock_sm,
        patch("media_preview_generator.web.routes._start_job_async") as boot_start,
    ):
        mock_sm.return_value.get.side_effect = lambda key, default=None: settings.get(key, default)
        mock_sm.return_value.processing_paused = paused
        _requeue_interrupted_on_startup("/unused")
    return [c.args[0] for c in boot_start.call_args_list]


def _resume() -> list[str]:
    """Run the shared resume body; return the ids it started."""
    with patch("media_preview_generator.web.routes.job_runner._start_job_async") as resume_start:
        resume_running_and_drain_pending()
    return [c.args[0] for c in resume_start.call_args_list]


class TestLeftoverPreviewJobs:
    def test_resume_does_not_start_job_too_old_to_revive(self, tmp_path):
        jm, stale_id, fresh_id = _restart_with(str(tmp_path / "config"), stale_hours=20)

        assert _boot(auto_requeue=True) == [fresh_id]
        started_by_resume = _resume()

        assert stale_id not in started_by_resume
        stale = jm.get_job(stale_id)
        assert stale.status is JobStatus.FAILED
        assert stale.error == "Interrupted by a restart and not resumed"

    def test_resume_does_not_start_jobs_when_auto_requeue_is_off(self, tmp_path):
        jm, stale_id, fresh_id = _restart_with(str(tmp_path / "config"), stale_hours=20)

        assert _boot(auto_requeue=False) == []
        started_by_resume = _resume()

        assert started_by_resume == []
        assert jm.get_job(stale_id).status is JobStatus.FAILED
        assert jm.get_job(fresh_id).status is JobStatus.FAILED

    def test_revived_job_still_starts_and_stays_pending(self, tmp_path):
        jm, _stale_id, fresh_id = _restart_with(str(tmp_path / "config"), stale_hours=20)

        _boot(auto_requeue=True)

        assert jm.get_job(fresh_id).status is JobStatus.PENDING
        assert fresh_id in _resume()

    def test_settled_state_survives_the_next_restart(self, tmp_path):
        config_dir = str(tmp_path / "config")
        _jm, stale_id, _fresh_id = _restart_with(config_dir, stale_hours=20)

        _boot(auto_requeue=True)

        assert JobManager(config_dir=config_dir).get_job(stale_id).status is JobStatus.FAILED

    def test_job_held_by_pause_waits_for_resume_however_old(self, tmp_path):
        """Pause survives a restart, and so do the jobs it was holding: Resume starts them, as the pause promised
        (a webhook queued during a weekend pause isn't re-sent)."""
        jm, stale_id, fresh_id = _restart_with(str(tmp_path / "config"), stale_hours=60)

        _boot(auto_requeue=True, paused=True)

        assert jm.get_job(stale_id).status is JobStatus.PENDING
        assert set(_resume()) == {stale_id, fresh_id}

    def test_auto_requeue_off_settles_jobs_even_while_paused(self, tmp_path):
        jm, stale_id, fresh_id = _restart_with(str(tmp_path / "config"), stale_hours=60)

        _boot(auto_requeue=False, paused=True)

        assert _resume() == []
        assert jm.get_job(stale_id).status is JobStatus.FAILED
        assert jm.get_job(fresh_id).status is JobStatus.FAILED
