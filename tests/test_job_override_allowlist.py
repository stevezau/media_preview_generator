"""Job config overrides go through the allow-list on every start path.

``POST /api/jobs`` used to persist its raw ``config`` block. Only the first
start filtered it; restart revival, a resume drain, Reprocess and scheduled
runs all hand the persisted config to the override loop, which set any
``Config`` attribute it named, so an API-token holder could point
``ffmpeg_path`` at any binary on disk.
"""

from __future__ import annotations

import json
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.jobs import JobStatus, get_job_manager
from media_preview_generator.web.settings_manager import reset_settings_manager

TOKEN = "test-token-12345678"
REAL_FFMPEG = "/usr/bin/ffmpeg"
MALICIOUS = {"ffmpeg_path": "/tmp/evil", "plex_token": "stolen", "sort_by": "oldest"}


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.scheduler as sched_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    reset_settings_manager()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    with sched_mod._schedule_lock:
        if sched_mod._schedule_manager is not None:
            try:
                sched_mod._schedule_manager.stop()
            except Exception:
                pass
            sched_mod._schedule_manager = None


@pytest.fixture()
def app(tmp_path, monkeypatch):
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    auth_file = os.path.join(config_dir, "auth.json")
    with open(auth_file, "w") as f:
        json.dump({"token": TOKEN}, f)
    with open(os.path.join(config_dir, "settings.json"), "w") as f:
        json.dump({"setup_complete": True}, f)
    monkeypatch.setattr("media_preview_generator.web.auth.AUTH_FILE", auth_file)
    monkeypatch.setattr("media_preview_generator.web.auth.CONFIG_DIR", config_dir)
    monkeypatch.setattr("media_preview_generator.web.auth.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("CONFIG_DIR", config_dir)
    monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
    flask_app = create_app(config_dir=config_dir)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    return flask_app


@pytest.fixture()
def captured_run(tmp_path):
    """Run the real job thread up to ``run_processing`` and capture the Config it was given."""
    captured: list = []
    done = threading.Event()

    def _capture(config, *args, **kwargs):
        captured.append(config)
        done.set()
        return {"outcome": {}}

    config = MagicMock()
    config.path_mappings = []
    config.tmp_folder = str(tmp_path)
    config.ffmpeg_path = REAL_FFMPEG
    config.sort_by = "newest"
    config.plex_token = "real-token"
    with (
        patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=_capture),
        patch("media_preview_generator.config.load_config", return_value=config),
        patch("media_preview_generator.processing.generator._verify_tmp_folder_health", return_value=(True, [])),
        patch("media_preview_generator.utils.setup_working_directory", return_value=str(tmp_path / "work")),
        patch("media_preview_generator.gpu.detect.detect_all_gpus", return_value=[]),
    ):
        yield captured, done


def _assert_only_allowed_keys_applied(captured_run) -> None:
    captured, done = captured_run
    assert done.wait(timeout=5), "run_processing was not called"
    config = captured[0]
    assert config.ffmpeg_path == REAL_FFMPEG
    assert config.plex_token == "real-token"
    assert config.sort_by == "oldest", "an allowed override must still apply"


class TestCreateJobSavesOnlyAllowedKeys:
    def test_saved_config_drops_disallowed_keys(self, app):
        with patch("media_preview_generator.web.routes.api_jobs._start_job_async") as mock_start:
            resp = app.test_client().post(
                "/api/jobs", headers=_headers(), json={"library_name": "Movies", "config": MALICIOUS}
            )

        assert resp.status_code == 201
        with app.app_context():
            saved = get_job_manager().get_job(resp.get_json()["id"]).config
        assert "ffmpeg_path" not in saved
        assert "plex_token" not in saved
        assert saved["sort_by"] == "oldest"
        overrides = mock_start.call_args.args[1]
        assert "ffmpeg_path" not in overrides and "plex_token" not in overrides

    def test_saved_config_keeps_the_library_scope(self, app):
        """Revival, resume and Reprocess replay the saved config, so it must scope the run like the first start."""
        with patch("media_preview_generator.web.routes.api_jobs._start_job_async") as mock_start:
            resp = app.test_client().post(
                "/api/jobs", headers=_headers(), json={"library_ids": ["7"], "config": {"sort_by": "oldest"}}
            )

        with app.app_context():
            saved = get_job_manager().get_job(resp.get_json()["id"]).config
        assert saved == mock_start.call_args.args[1]
        assert saved["selected_library_ids"] == ["7"]

    def test_dropped_keys_are_logged(self, app):
        from loguru import logger

        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="WARNING", format="{message}")
        try:
            with patch("media_preview_generator.web.routes.api_jobs._start_job_async"):
                app.test_client().post("/api/jobs", headers=_headers(), json={"config": MALICIOUS})
        finally:
            logger.remove(sink)

        text = "".join(lines)
        assert "ffmpeg_path" in text and "plex_token" in text
        assert "stolen" not in text, "values must never be logged"


class TestEveryStartPathIgnoresDisallowedKeys:
    """A job persisted with a disallowed key (as before the fix) never applies it."""

    def _persisted_job(self, app, status=JobStatus.PENDING):
        with app.app_context():
            jm = get_job_manager()
            job = jm.create_job(library_name="Movies", config=dict(MALICIOUS))
            if status is not JobStatus.PENDING:
                jm.complete_job(job.id)
            return jm, job

    def test_first_start_ignores_disallowed_keys(self, app, captured_run):
        with app.app_context():
            from media_preview_generator.web.routes.job_runner import _start_job_async

            job = get_job_manager().create_job(library_name="Movies")
            _start_job_async(job.id, dict(MALICIOUS))

        _assert_only_allowed_keys_applied(captured_run)

    def test_restart_revival_ignores_disallowed_keys(self, app, captured_run):
        from media_preview_generator.web.app import _requeue_interrupted_on_startup

        jm, job = self._persisted_job(app)
        jm._interrupted_jobs = [job]

        with app.app_context():
            _requeue_interrupted_on_startup(app.config.get("CONFIG_DIR") or os.environ["CONFIG_DIR"])

        _assert_only_allowed_keys_applied(captured_run)

    def test_resume_drain_ignores_disallowed_keys(self, app, captured_run):
        self._persisted_job(app)

        resp = app.test_client().post("/api/processing/resume", headers=_headers())

        assert resp.status_code == 200
        _assert_only_allowed_keys_applied(captured_run)

    def test_reprocess_ignores_disallowed_keys(self, app, captured_run):
        _jm, job = self._persisted_job(app, status=JobStatus.COMPLETED)

        resp = app.test_client().post(f"/api/jobs/{job.id}/reprocess", headers=_headers())

        assert resp.status_code == 201, resp.get_data(as_text=True)
        _assert_only_allowed_keys_applied(captured_run)

    def test_scheduled_run_ignores_disallowed_keys(self, app, captured_run):
        from media_preview_generator.web.app import run_scheduled_job

        with app.app_context():
            run_scheduled_job(library_name="Movies", config=dict(MALICIOUS))

        _assert_only_allowed_keys_applied(captured_run)
