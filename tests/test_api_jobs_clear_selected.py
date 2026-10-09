"""Tests for POST /api/jobs/clear with job_ids (delete only the selected finished jobs)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.jobs import get_job_manager

URL = "/api/jobs/clear"


@pytest.fixture()
def client(tmp_path):
    config_dir = str(tmp_path / "cfg")
    os.makedirs(config_dir, exist_ok=True)
    with patch.dict(
        os.environ,
        {"CONFIG_DIR": config_dir, "WEB_AUTH_TOKEN": "test-token-12345678", "WEB_PORT": "8099"},
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app.test_client()


def _post(client, body):
    return client.post(URL, json=body, headers={"Authorization": "Bearer test-token-12345678"})


def _finished(jm, status: str):
    job = jm.create_job(library_name=status)
    jm.start_job(job.id)
    if status == "completed":
        jm.complete_job(job.id)
    elif status == "failed":
        jm.complete_job(job.id, error="boom")
    else:
        jm.cancel_job(job.id)
    return job


class TestClearSelected:
    def test_deletes_only_listed_finished_jobs(self, client):
        jm = get_job_manager()
        done, failed, keep = _finished(jm, "completed"), _finished(jm, "failed"), _finished(jm, "completed")

        data = _post(client, {"job_ids": [done.id, failed.id]}).get_json()

        assert data["cleared"] == 2
        assert sorted(data["removed"]) == sorted([done.id, failed.id])
        assert data["skipped"] == []
        assert jm.get_job(done.id) is None
        assert jm.get_job(failed.id) is None
        assert jm.get_job(keep.id) is not None

    def test_skips_queued_and_running_with_reasons(self, client):
        jm = get_job_manager()
        queued = jm.create_job(library_name="Q")
        running = jm.create_job(library_name="R")
        jm.start_job(running.id)
        done = _finished(jm, "cancelled")

        data = _post(client, {"job_ids": [queued.id, running.id, done.id, "nope"]}).get_json()

        assert data["removed"] == [done.id]
        assert {s["id"]: s["reason"] for s in data["skipped"]} == {
            queued.id: "not_finished (pending)",
            running.id: "not_finished (running)",
            "nope": "not_found",
        }
        assert jm.get_job(queued.id) is not None
        assert jm.get_job(running.id) is not None

    def test_rejects_non_list_job_ids(self, client):
        assert _post(client, {"job_ids": "abc"}).status_code == 400

    def test_rejects_job_ids_with_statuses(self, client):
        assert _post(client, {"job_ids": [], "statuses": ["completed"]}).status_code == 400

    def test_empty_job_ids_deletes_nothing(self, client):
        jm = get_job_manager()
        keep = _finished(jm, "completed")

        data = _post(client, {"job_ids": []}).get_json()

        assert data["cleared"] == 0
        assert jm.get_job(keep.id) is not None

    def test_statuses_form_still_clears_by_status(self, client):
        jm = get_job_manager()
        done, failed = _finished(jm, "completed"), _finished(jm, "failed")

        data = _post(client, {"statuses": ["failed"]}).get_json()

        assert data == {"success": True, "cleared": 1}
        assert jm.get_job(done.id) is not None
        assert jm.get_job(failed.id) is None
