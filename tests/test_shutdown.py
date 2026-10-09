"""A stop kills FFmpeg along with the app: previews interrupted by it stay RUNNING for startup revival."""

import json
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator import shutdown
from media_preview_generator.jobs.worker import Worker
from media_preview_generator.processing import set_file_result_callback
from media_preview_generator.processing.generator import ProcessingResult, _notify_file_result, record_failure
from media_preview_generator.web.app import create_app
from media_preview_generator.web.jobs import JobManager, JobStatus
from media_preview_generator.web.settings_manager import reset_settings_manager
from tests.conftest import _ms, _pi


@pytest.fixture
def file_results():
    """Collects the rows the scope-less file-result callback is given."""
    rows = []
    set_file_result_callback(lambda path, outcome, reason, worker, servers: rows.append((path, outcome)))
    yield rows
    set_file_result_callback(None)


def _run_preview_worker(process_side_effect) -> Worker:
    worker = Worker(0, "CPU")
    with patch(
        "media_preview_generator.processing.multi_server.process_canonical_path", side_effect=process_side_effect
    ):
        worker.assign_task(_pi("/m/a.mkv", title="A", media_type="movie"), MagicMock(), MagicMock())
        worker.current_thread.join(timeout=5)
    return worker


class TestPreviewFileKilledByTheStop:
    def test_failed_file_is_not_recorded_while_shutting_down(self, file_results):
        def ffmpeg_killed(*args, **kwargs):
            shutdown.request_shutdown()
            return _ms("failed")

        _run_preview_worker(ffmpeg_killed)

        assert file_results == []

    def test_failed_file_is_recorded_when_not_shutting_down(self, file_results):
        _run_preview_worker(lambda *args, **kwargs: _ms("failed"))

        assert [outcome for _path, outcome in file_results] == ["failed"]

    def test_other_outcomes_are_still_recorded_while_shutting_down(self, file_results):
        shutdown.request_shutdown()

        _run_preview_worker(lambda *args, **kwargs: _ms("generated"))

        assert [outcome for _path, outcome in file_results] == ["generated"]


class TestCompletionWhileShuttingDown:
    @pytest.mark.parametrize("kwargs", [{"error": "1 file(s) failed"}, {"warning": "1 file(s) failed"}])
    def test_failed_or_warned_job_stays_running_and_is_revived(self, tmp_path, kwargs):
        jm = JobManager(config_dir=str(tmp_path))
        job = jm.create_job(library_name="Movies", config={})
        jm.start_job(job.id)
        shutdown.request_shutdown()

        jm.complete_job(job.id, **kwargs)

        assert (job.status, job.completed_at, job.error) == (JobStatus.RUNNING, None, None)
        jm.close()
        shutdown._shutting_down = False
        restored = JobManager(config_dir=str(tmp_path))
        try:
            assert [entry.id for entry in restored.requeue_interrupted_jobs()] == [job.id]
        finally:
            restored.close()

    def test_job_that_finished_cleanly_is_completed_while_shutting_down(self, tmp_path):
        jm = JobManager(config_dir=str(tmp_path))
        job = jm.create_job(library_name="Movies", config={})
        jm.start_job(job.id)
        shutdown.request_shutdown()

        jm.complete_job(job.id)

        assert job.status is JobStatus.COMPLETED
        jm.close()

    def test_failed_job_is_failed_when_not_shutting_down(self, tmp_path):
        jm = JobManager(config_dir=str(tmp_path))
        job = jm.create_job(library_name="Movies", config={})
        jm.start_job(job.id)

        jm.complete_job(job.id, error="1 file(s) failed")

        assert (job.status, job.error) == (JobStatus.FAILED, "1 file(s) failed")
        jm.close()


@pytest.fixture
def app(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plex_cfg = tmp_path / "plex_cfg"
    (plex_cfg / "Media" / "localhost").mkdir(parents=True)
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "setup_complete": True,
                "media_servers": [
                    {
                        "id": "plex-1",
                        "type": "plex",
                        "name": "Plex Main",
                        "enabled": True,
                        "url": "http://plex:32400",
                        "auth": {"token": "tok"},
                        "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                        "output": {"adapter": "plex_bundle", "plex_config_folder": str(plex_cfg)},
                    }
                ],
            }
        )
    )
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    reset_settings_manager()
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield create_app(config_dir=str(config_dir))
    reset_settings_manager()
    with jobs_mod._job_lock:
        if jobs_mod._job_manager is not None:
            jobs_mod._job_manager.close()
        jobs_mod._job_manager = None


class TestPreviewJobRunnerWhileShuttingDown:
    @pytest.mark.real_job_async
    def test_job_whose_files_were_killed_by_the_stop_stays_running_and_is_revived(self, app):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        ran = []

        def stopped_mid_file(config, selected_gpus, **kwargs):
            shutdown.request_shutdown()  # the stop signal reaches the app; the file's FFmpeg exits with it
            record_failure("/m/a.mkv", 255, "ffmpeg exited 255: received signal 15", "CPU")
            _notify_file_result("/m/a.mkv", ProcessingResult.FAILED, "ffmpeg exited 255", "CPU")
            ran.append(True)
            return {"outcome": {"failed": 1}}

        with (
            app.app_context(),
            patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=stopped_mid_file),
        ):
            jm = get_job_manager()
            job = jm.create_job(library_name="Movies", config={"webhook_retry_count": 2})
            _start_job_async(job.id, None)
            deadline = time.time() + 5
            while not ran and time.time() < deadline:
                time.sleep(0.02)
            time.sleep(0.5)  # let the runner finish handling the result

            assert ran
            assert jm.get_file_results(job.id) == []
            assert (job.status, job.completed_at, job.error) == (JobStatus.RUNNING, None, None)
            assert [other.id for other in jm.get_all_jobs()] == [job.id]  # no retry job spawned
            jm.close()
            shutdown._shutting_down = False
            restored = JobManager(config_dir=jm.config_dir)
            try:
                assert [revived.id for revived in restored.requeue_interrupted_jobs()] == [job.id]
            finally:
                restored.close()


class TestShutdownHooks:
    def test_sigterm_sets_the_flag_then_runs_the_previous_handler(self):
        calls = []
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        signal.signal(signal.SIGTERM, lambda signum, frame: calls.append(signum))
        try:
            with patch("media_preview_generator.shutdown.atexit.register"):
                shutdown.install_shutdown_hooks()
            assert not shutdown.is_shutting_down()

            signal.raise_signal(signal.SIGTERM)

            assert shutdown.is_shutting_down()
            assert calls == [signal.SIGTERM]
        finally:
            for sig, handler in saved.items():
                signal.signal(sig, handler)

    def test_sigterm_keeps_restarting_interrupted_syscalls(self):
        saved = signal.getsignal(signal.SIGTERM)
        try:
            with (
                patch("media_preview_generator.shutdown.atexit.register"),
                patch.object(shutdown.signal, "siginterrupt") as siginterrupt,
            ):
                shutdown.install_shutdown_hooks()
            siginterrupt.assert_any_call(signal.SIGTERM, False)
        finally:
            signal.signal(signal.SIGTERM, saved)

    def test_sigterm_with_the_default_action_still_ends_the_process(self):
        code = (
            "import signal, sys\n"
            "from media_preview_generator import shutdown\n"
            "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
            "shutdown.install_shutdown_hooks()\n"
            "signal.raise_signal(signal.SIGTERM)\n"
            "sys.exit(0)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)

        assert result.returncode == -signal.SIGTERM
