"""The Intro & Credits tab re-reads its file when a job finishes (``markers_inspector.js`` ``watchJobs``).

The tab listens on the ``/jobs`` SocketIO namespace and decides whether to re-read from ``job.kind``. Both halves of
that contract live in different languages, and the e2e test drives the handler the JS registered — so it can't see a
namespace or a payload key that has moved. These assertions are what stops a rename on the Python side from quietly
killing the refresh while both suites stay green.
"""

from __future__ import annotations

from pathlib import Path

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.web.jobs import Job

_ROOT = Path(__file__).resolve().parent.parent
_INSPECTOR_JS = (_ROOT / "media_preview_generator/web/static/js/markers_inspector.js").read_text()
_HANDLERS = (_ROOT / "media_preview_generator/web/routes/socketio_handlers.py").read_text()
_JOBS_PY = (_ROOT / "media_preview_generator/web/jobs.py").read_text()


def test_the_tab_filters_on_the_kind_the_backend_sends() -> None:
    assert JOB_KIND_INTRO_CREDITS == "intro_credits"
    assert f"const JOB_KIND_MARKERS = '{JOB_KIND_INTRO_CREDITS}';" in _INSPECTOR_JS


def test_a_finished_job_carries_its_kind_to_the_browser() -> None:
    assert Job(id="job-1", kind=JOB_KIND_INTRO_CREDITS).to_dict()["kind"] == JOB_KIND_INTRO_CREDITS


def test_the_tab_connects_to_the_namespace_the_events_are_emitted_on() -> None:
    assert 'namespace="/jobs"' in _HANDLERS
    assert "window.io('/jobs'" in _INSPECTOR_JS


def test_the_tab_listens_for_every_event_that_ends_a_job() -> None:
    for event in ("job_completed", "job_failed"):
        assert f'_emit_event("{event}", job.to_dict())' in _JOBS_PY
        assert f"jobsSocket.on('{event}', jobFinished);" in _INSPECTOR_JS
