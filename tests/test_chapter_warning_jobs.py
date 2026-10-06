"""Finished chapter warnings remain discoverable beside the unfinished queue."""

import pytest

from media_preview_generator.web.job_details import job_has_chapter_warning
from media_preview_generator.web.jobs import Job, JobStatus, get_job_manager
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
