"""The Inspector follows a job on the open file and reads the file again when it ends (``inspector.js`` ``watchJobs``).

The page listens on the ``/jobs`` SocketIO namespace, matches a job to the open file from the job's ``config``
(``file_paths`` / ``webhook_paths``) or its workers' ``current_file``, and tells an Intro & Credits job from a preview
job by ``kind``. Both halves of that contract live in different languages and the e2e tests can't emit socket events,
so these assertions are what stops a rename on the Python side from quietly killing the live banner and the re-read.
"""

from __future__ import annotations

import re
from pathlib import Path

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.web.jobs import Job, JobProgress, WorkerStatus

_ROOT = Path(__file__).resolve().parent.parent
_JS = (_ROOT / "media_preview_generator/web/static/js/inspector.js").read_text()
_HANDLERS = (_ROOT / "media_preview_generator/web/routes/socketio_handlers.py").read_text()
_JOBS_PY = (_ROOT / "media_preview_generator/web/jobs.py").read_text()


def _listed(group: str) -> list[str]:
    match = re.search(
        r"\[([^\]]*)\]\.forEach\(function \(name\) \{\s*jobsSocket\.on\(name, function \(job\) \{ onJobEvent\(job, "
        + group,
        _JS,
    )
    assert match, f"no {group} listener list in inspector.js"
    return re.findall(r"'(\w+)'", match.group(1))


def test_the_page_tells_markers_jobs_by_the_kind_the_backend_sends() -> None:
    assert JOB_KIND_INTRO_CREDITS == "intro_credits"
    assert f"const MARKERS_JOB = '{JOB_KIND_INTRO_CREDITS}';" in _JS
    assert Job(id="job-1", kind=JOB_KIND_INTRO_CREDITS).to_dict()["kind"] == JOB_KIND_INTRO_CREDITS


def test_the_page_connects_to_the_namespace_the_events_are_emitted_on() -> None:
    assert 'namespace="/jobs"' in _HANDLERS
    assert "window.io('/jobs'" in _JS


def test_every_event_the_page_reads_a_job_from_carries_the_whole_job() -> None:
    running = _listed("false")
    ended = _listed("true")
    assert set(running) == {"job_created", "job_started", "job_updated"}
    assert set(ended) == {"job_completed", "job_failed", "job_cancelled"}
    for event in running + ended:
        assert f'_emit_event("{event}", job.to_dict())' in _JOBS_PY


def test_a_job_says_which_files_it_works_on_where_the_page_looks() -> None:
    job = Job(
        id="job-2",
        config={"file_paths": ["/m/a.mkv"], "webhook_paths": ["/m/b.mkv"]},
        progress=JobProgress(current_file="/m/c.mkv", workers=[WorkerStatus(current_file="/m/d.mkv")]),
    ).to_dict()
    assert job["config"]["file_paths"] == ["/m/a.mkv"]
    assert job["config"]["webhook_paths"] == ["/m/b.mkv"]
    assert job["progress"]["current_file"] == "/m/c.mkv"
    assert job["progress"]["workers"][0]["current_file"] == "/m/d.mkv"
    for key in ("cfg.file_paths", "cfg.webhook_paths", "progress.current_file", "w.current_file"):
        assert key in _JS


def test_progress_events_carry_the_job_id_and_progress_the_page_reads() -> None:
    assert '"job_progress",' in _JOBS_PY
    assert '"job_id": job_id,' in _JOBS_PY and '"progress": job.progress.to_dict(),' in _JOBS_PY
    assert "jobsSocket.on('job_progress', onJobProgress);" in _JS
    assert "data.job_id" in _JS and "data.progress" in _JS
