"""A settings save keeps the pause state; only the zero-workers auto-pause is undone by one.

Saving settings with no workers pauses processing, and the save that adds
workers back resumes it. Any other save used to resume too, so a Pause all or
a quiet-hours pause ended the moment the user saved an unrelated setting.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_preview_generator.jobs.dispatcher import reset_dispatcher
from media_preview_generator.web.app import create_app
from media_preview_generator.web.settings_manager import get_settings_manager, reset_settings_manager

TOKEN = "test-token-12345678"
DRAIN = "media_preview_generator.web.routes.job_runner.resume_running_and_drain_pending"
NO_GPU = [{"device": "cuda:0", "name": "GPU", "enabled": False, "workers": 0, "ffmpeg_threads": 2}]


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    reset_dispatcher()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    reset_dispatcher()
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
    app = create_app(config_dir=str(tmp_path))
    with app.app_context():
        sm = get_settings_manager()
        sm.update({"gpu_config": NO_GPU, "cpu_threads": 2})
        sm.processing_paused = False
    return app


def _post(app, url: str, body: dict | None = None):
    response = app.test_client().post(url, json=body or {}, headers={"X-Auth-Token": TOKEN})
    assert response.status_code == 200, response.get_data(as_text=True)
    return response


def _paused(app) -> bool:
    with app.app_context():
        return get_settings_manager().processing_paused


class TestSettingsSaveKeepsPause:
    def test_manual_pause_kept_when_unrelated_setting_saved(self, app):
        _post(app, "/api/processing/pause")

        with patch(DRAIN) as drain:
            _post(app, "/api/settings", {"thumbnail_quality": 3, "cpu_threads": 2})

        assert _paused(app) is True
        drain.assert_not_called()

    def test_quiet_hours_pause_kept_when_unrelated_setting_saved(self, app):
        with app.app_context():
            get_settings_manager().processing_paused = True  # what the quiet-hours cron does

        with patch(DRAIN) as drain:
            _post(app, "/api/settings", {"thumbnail_quality": 3, "cpu_threads": 2})

        assert _paused(app) is True
        drain.assert_not_called()

    def test_zero_workers_save_pauses_and_restoring_save_resumes(self, app):
        _post(app, "/api/settings", {"cpu_threads": 0})
        assert _paused(app) is True

        with patch(DRAIN) as drain:
            _post(app, "/api/settings", {"cpu_threads": 2})

        assert _paused(app) is False
        drain.assert_called_once_with()

    def test_auto_pause_not_undone_by_save_that_keeps_zero_workers(self, app):
        _post(app, "/api/settings", {"cpu_threads": 0})

        with patch(DRAIN) as drain:
            _post(app, "/api/settings", {"thumbnail_quality": 3, "cpu_threads": 0})

        assert _paused(app) is True
        drain.assert_not_called()

    def test_manual_pause_kept_when_workers_go_to_zero_and_back(self, app):
        _post(app, "/api/processing/pause")
        _post(app, "/api/settings", {"cpu_threads": 0})

        with patch(DRAIN) as drain:
            _post(app, "/api/settings", {"cpu_threads": 2})

        assert _paused(app) is True
        drain.assert_not_called()

    def test_manual_pause_after_auto_pause_kept_when_workers_restored(self, app):
        _post(app, "/api/settings", {"cpu_threads": 0})
        _post(app, "/api/processing/pause")

        with patch(DRAIN) as drain:
            _post(app, "/api/settings", {"cpu_threads": 2})

        assert _paused(app) is True
        drain.assert_not_called()

    def test_auto_pause_stays_while_quiet_hours_active_when_workers_restored(self, app):
        _post(app, "/api/settings", {"cpu_threads": 0})

        with (
            patch("media_preview_generator.web.scheduler.is_now_in_any_quiet_window", return_value=True),
            patch(DRAIN) as drain,
        ):
            _post(app, "/api/settings", {"cpu_threads": 2})

        assert _paused(app) is True
        drain.assert_not_called()

    def test_auto_pause_survives_restart_and_restoring_save_resumes(self, app, tmp_path):
        _post(app, "/api/settings", {"cpu_threads": 0})
        reset_settings_manager()
        restarted = create_app(config_dir=str(tmp_path))

        with patch(DRAIN) as drain:
            _post(restarted, "/api/settings", {"cpu_threads": 2})

        assert _paused(restarted) is False
        drain.assert_called_once_with()
