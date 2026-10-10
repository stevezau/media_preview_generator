"""Finished chapter warnings remain discoverable beside the unfinished queue."""

import pytest

from media_preview_generator.web.job_details import job_has_chapter_warning
from media_preview_generator.web.jobs import Job, JobManager, JobStatus, get_job_manager
from tests.test_job_details import _HEADERS
from tests.test_job_details import app as _app_fixture

app = _app_fixture


@pytest.mark.parametrize(
    "publisher,expected",
    [
        ({"chapter_counts": {"failed": 1}}, True),
        ({"chapter_counts": {"waiting": 1}}, True),
        ({"chapter_counts": {"incomplete": 1}}, True),
        ({"counts": {"published_chapters_failed": 1}}, True),
        ({"counts": {"published_pending_chapters": 1}}, True),
        ({"chapter_counts": {"ready": 1}, "counts": {"published_chapters_failed": 1}}, False),
        ({"chapter_counts": {"failed": 0}, "counts": {"published_chapters_failed": 1}}, False),
        ({"chapter_counts": {}, "counts": {"published_chapters_failed": 1}}, True),
        ({"chapter_counts": {"failed": "1"}}, False),
        ({"chapter_counts": {"failed": True}}, False),
        ({"chapter_counts": {"failed": -1}}, False),
        ({"chapter_counts": {"failed": None}}, False),
        ({"counts": "malformed"}, False),
        (None, False),
    ],
)
def test_chapter_warning_requires_positive_saved_artifact_evidence(publisher, expected):
    entry = Job(id="warning", status=JobStatus.COMPLETED, publishers=[publisher])
    assert job_has_chapter_warning(entry) is expected


@pytest.mark.parametrize(
    "status,kind,config,expected",
    [
        (JobStatus.COMPLETED, "previews", {}, True),
        (JobStatus.FAILED, "previews", {}, True),
        (JobStatus.CANCELLED, "previews", {}, False),
        (JobStatus.RUNNING, "previews", {}, False),
        (JobStatus.PENDING, "previews", {}, False),
        (JobStatus.COMPLETED, "loudness", {}, False),
        (JobStatus.COMPLETED, "intro_credits", {}, False),
        (JobStatus.COMPLETED, "previews", {"is_retry": True, "parent_job_id": "head"}, False),
        (JobStatus.COMPLETED, "previews", {"is_retry": True, "is_retry_chain": True}, True),
    ],
)
def test_only_terminal_preview_heads_contribute_to_warning_notice(status, kind, config, expected):
    entry = Job(id="warning", status=status, kind=kind, config=config, publishers=[{"chapter_counts": {"failed": 1}}])
    assert job_has_chapter_warning(entry) is expected


def test_warning_filter_paginates_heads_while_notice_ignores_filters(app):
    manager = get_job_manager()
    heads = []
    for i in range(3):
        job = manager.create_job(library_name=f"Warning {i}")
        job.status = JobStatus.COMPLETED
        job.publishers = [{"chapter_counts": {"failed": 1}}]
        heads.append(job)
    child = manager.create_job(config={"is_retry": True, "parent_job_id": heads[0].id})
    child.status = JobStatus.FAILED
    child.publishers = heads[0].publishers
    active = manager.create_job(library_name="Still running", kind="loudness")
    client = app.test_client()
    page = client.get("/api/jobs?status=chapter_warnings&page=2&per_page=2", headers=_HEADERS)
    assert page.status_code == 200
    assert page.json["total"] == 3 and page.json["pages"] == 2
    assert [j["id"] for j in page.json["jobs"]] == [heads[0].id]
    assert page.json["chapter_warning_count"] == 3
    for query in (
        "status=active&kind=loudness",
        "status=chapter_warnings&q=nonexistent",
        "status=all&include_retry_attempts=1",
    ):
        response = client.get("/api/jobs?" + query, headers=_HEADERS)
        assert response.status_code == 200
        assert response.json["chapter_warning_count"] == 3
        if query.startswith("status=active"):
            assert [j["id"] for j in response.json["jobs"]] == [active.id]
        elif "nonexistent" in query:
            assert response.json["jobs"] == []
    assert client.get("/api/jobs?status=chapter_warnings", headers=_HEADERS).json["chapter_warning_count"] == 3
    assert client.get("/api/jobs?status=chapter_warnings").status_code == 401


def test_empty_history_has_zero_notice_in_paged_and_unpaged_responses(app):
    client = app.test_client()
    for query in ("", "?page=1&per_page=50", "?status=chapter_warnings"):
        response = client.get("/api/jobs" + query, headers=_HEADERS)
        assert response.status_code == 200
        assert response.json["chapter_warning_count"] == 0
        assert response.json["jobs"] == []


def _chapter_row(manager, job_id, name, status, outcome="skipped_bif_exists"):
    servers = [{"status": "skipped_output_exists", "artifacts": {"chapters": {"status": status, "message": "m"}}}]
    manager.record_file_result(job_id, f"/media/{name}.mkv", outcome, servers=servers)


def _files(client, job_id, outcome):
    response = client.get(f"/api/jobs/{job_id}/files?outcome={outcome}", headers=_HEADERS)
    assert response.status_code == 200
    return sorted(row["file"] for row in response.json["files"])


def test_files_chapter_filter_matches_artifact_status_across_outcomes(app):
    manager = get_job_manager()
    job = manager.create_job(library_name="Chapters")
    _chapter_row(manager, job.id, "failed", "failed")
    _chapter_row(manager, job.id, "waiting", "waiting", outcome="generated")
    _chapter_row(manager, job.id, "legacy", "pending")  # counted as "incomplete": no filter link
    _chapter_row(manager, job.id, "skipped", "skipped")
    _chapter_row(manager, job.id, "ready", "ready")
    manager.record_file_result(job.id, "/media/plain.mkv", "skipped_bif_exists")
    client = app.test_client()
    assert _files(client, job.id, "chapters_failed") == ["/media/failed.mkv"]
    assert _files(client, job.id, "chapters_waiting") == ["/media/waiting.mkv"]
    assert _files(client, job.id, "chapters_skipped") == ["/media/skipped.mkv"]
    assert len(_files(client, job.id, "skipped_bif_exists")) == 5


def test_chapter_problem_rows_survive_a_full_routine_outcome_bucket(app, monkeypatch):
    monkeypatch.setattr(JobManager, "_FILE_RESULTS_PER_OUTCOME_CAP", 2)
    manager = get_job_manager()
    job = manager.create_job(library_name="Capped")
    for index in range(4):
        _chapter_row(manager, job.id, f"ok{index}", "ready")
    _chapter_row(manager, job.id, "bad", "failed")
    _chapter_row(manager, job.id, "wait", "waiting")
    _chapter_row(manager, job.id, "skip", "skipped")
    client = app.test_client()
    assert _files(client, job.id, "chapters_failed") == ["/media/bad.mkv"]
    assert _files(client, job.id, "chapters_waiting") == ["/media/wait.mkv"]
    assert _files(client, job.id, "chapters_skipped") == ["/media/skip.mkv"]


def _warning_job(manager, name):
    job = manager.create_job(library_name=name)
    job.status = JobStatus.COMPLETED
    job.completed_at = "2026-10-10T09:00:00+00:00"
    job.publishers = [{"chapter_counts": {"failed": 1}}]
    return job


def test_dismiss_requires_auth(app):
    assert app.test_client().post("/api/jobs/chapter-warnings/dismiss").status_code == 401


def test_dismiss_marks_only_warning_jobs_and_a_later_warning_raises_the_banner(app):
    manager = get_job_manager()
    first = _warning_job(manager, "First")
    second = _warning_job(manager, "Second")
    clean = manager.create_job(library_name="Clean")
    clean.status = JobStatus.COMPLETED
    client = app.test_client()

    response = client.post("/api/jobs/chapter-warnings/dismiss", headers=_HEADERS)
    assert response.status_code == 200 and response.json == {"dismissed": 2}
    assert first.config["chapter_warning_dismissed"] and second.config["chapter_warning_dismissed"]
    assert "chapter_warning_dismissed" not in (clean.config or {})
    assert client.get("/api/jobs?status=all", headers=_HEADERS).json["chapter_warning_count"] == 0
    assert client.post("/api/jobs/chapter-warnings/dismiss", headers=_HEADERS).json == {"dismissed": 0}

    _warning_job(manager, "Later")
    assert client.get("/api/jobs?status=all", headers=_HEADERS).json["chapter_warning_count"] == 1


def test_dismissal_is_persisted_to_the_jobs_database(app):
    manager = get_job_manager()
    job = _warning_job(manager, "Persisted")
    app.test_client().post("/api/jobs/chapter-warnings/dismiss", headers=_HEADERS)
    reloaded = JobManager(config_dir=manager.config_dir)
    assert reloaded.get_job(job.id).config["chapter_warning_dismissed"] == job.completed_at


def test_dismissal_applies_only_to_the_completion_it_was_made_at(app):
    manager = get_job_manager()
    job = _warning_job(manager, "Re-armed")
    job.completed_at = "2026-10-10T10:00:00+00:00"
    client = app.test_client()
    assert client.post("/api/jobs/chapter-warnings/dismiss", headers=_HEADERS).json == {"dismissed": 1}
    assert job_has_chapter_warning(job) is False

    # A retry re-arms the same chain head; completing again stamps a new time.
    job.completed_at = "2026-10-10T11:00:00+00:00"
    assert job_has_chapter_warning(job) is True
    assert client.get("/api/jobs?status=all", headers=_HEADERS).json["chapter_warning_count"] == 1


def test_boolean_dismissal_flag_does_not_dismiss(app):
    job = _warning_job(get_job_manager(), "Old flag")
    job.config = {"chapter_warning_dismissed": True}
    assert job_has_chapter_warning(job) is True


def test_rerun_of_a_dismissed_job_does_not_inherit_the_dismissal(app, monkeypatch):
    import media_preview_generator.web.routes.job_runner as job_runner

    monkeypatch.setattr(job_runner, "_start_job_async", lambda *args, **kwargs: None)
    manager = get_job_manager()
    job = _warning_job(manager, "Dismissed")
    job.completed_at = "2026-10-10T10:00:00+00:00"
    client = app.test_client()
    client.post("/api/jobs/chapter-warnings/dismiss", headers=_HEADERS)

    response = client.post(f"/api/jobs/{job.id}/reprocess", headers=_HEADERS)
    assert response.status_code == 201, response.get_data(as_text=True)
    rerun = manager.get_job(response.json["id"])
    assert "chapter_warning_dismissed" not in (rerun.config or {})
    rerun.status = JobStatus.COMPLETED
    rerun.completed_at = job.completed_at
    rerun.publishers = [{"chapter_counts": {"failed": 1}}]
    assert job_has_chapter_warning(rerun) is True
