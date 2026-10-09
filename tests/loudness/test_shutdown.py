"""A stop kills FFmpeg along with the app; the loudness job must stay RUNNING so startup revival picks it up."""

import io
from types import SimpleNamespace

from media_preview_generator import shutdown
from media_preview_generator.loudness import job
from media_preview_generator.web.jobs import JobManager, JobStatus

from .conftest import Lifecycle

SIGNALLED_STDERR = b"size=N/A time=01:15:30.49 speed=39.9x\nExiting normally, received signal 15.\n"


def _killed_by_the_stop(lifecycle: Lifecycle, monkeypatch) -> None:
    """The stop signal reaches the app, and the file's FFmpeg exits 255 as it handles the same signal."""

    def popen(command, **kwargs):
        shutdown.request_shutdown()
        return SimpleNamespace(
            returncode=255, stdout=io.BytesIO(b""), stderr=io.BytesIO(SIGNALLED_STDERR), wait=lambda **kw: 255
        )

    monkeypatch.setattr(job.analyze.subprocess, "Popen", popen)


def test_file_killed_by_the_stop_is_not_failed_and_its_job_is_revived(lifecycle: Lifecycle, monkeypatch) -> None:
    path = lifecycle.add_file("stopped.mkv")
    _killed_by_the_stop(lifecycle, monkeypatch)

    parent = lifecycle.start([path])

    assert lifecycle.manager.get_file_results(parent.id) == []
    assert parent.status is JobStatus.RUNNING
    assert parent.completed_at is None
    assert parent.error is None

    lifecycle.manager.close()
    shutdown._shutting_down = False
    restored = JobManager(config_dir=lifecycle.manager.config_dir)
    try:
        assert [entry.id for entry in restored.requeue_interrupted_jobs()] == [parent.id]
    finally:
        restored.close()


def test_failed_file_is_recorded_and_job_fails_when_no_stop_is_under_way(lifecycle: Lifecycle, monkeypatch) -> None:
    path = lifecycle.add_file("broken.mkv")
    lifecycle.corrupt.add((path, 1))

    parent = lifecycle.start([path])

    assert lifecycle.rows(parent)[path]["outcome"] == job.FAILED
    assert parent.status is JobStatus.FAILED
