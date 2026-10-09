"""Tests for POST /api/jobs/cancel-bulk (cancel several queued jobs at once)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.jobs import JobManager, JobStatus, get_job_manager
from media_preview_generator.web.routes.api_jobs import _BULK_CANCEL_MAX

URL = "/api/jobs/cancel-bulk"


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


def _headers():
    return {"Authorization": "Bearer test-token-12345678"}


def _post(client, body):
    return client.post(URL, json=body, headers=_headers())


class TestCancelBulk:
    def test_cancels_pending_jobs_when_all_pending(self, client):
        jm = get_job_manager()
        a, b = jm.create_job(library_name="A"), jm.create_job(library_name="B")

        resp = _post(client, {"job_ids": [a.id, b.id]})

        assert resp.status_code == 200
        assert resp.get_json() == {"cancelled": [a.id, b.id], "skipped": []}
        assert jm.get_job(a.id).status == JobStatus.CANCELLED
        assert jm.get_job(b.id).status == JobStatus.CANCELLED
        assert jm.is_cancellation_requested(a.id)

    def test_skips_running_job_when_mixed(self, client):
        jm = get_job_manager()
        pending, running = jm.create_job(library_name="P"), jm.create_job(library_name="R")
        jm.start_job(running.id)

        data = _post(client, {"job_ids": [pending.id, running.id]}).get_json()

        assert data["cancelled"] == [pending.id]
        assert data["skipped"] == [{"id": running.id, "reason": "not_pending (running)"}]
        assert jm.get_job(running.id).status == JobStatus.RUNNING
        assert not jm.is_cancellation_requested(running.id)

    def test_skips_unknown_id(self, client):
        data = _post(client, {"job_ids": ["no-such-job"]}).get_json()

        assert data == {"cancelled": [], "skipped": [{"id": "no-such-job", "reason": "not_found"}]}

    def test_skips_already_cancelled_job(self, client):
        jm = get_job_manager()
        job = jm.create_job(library_name="X")
        jm.cancel_job(job.id)

        data = _post(client, {"job_ids": [job.id]}).get_json()

        assert data["cancelled"] == []
        assert data["skipped"][0]["reason"] == "not_pending (cancelled)"

    def test_duplicate_ids_cancelled_once(self, client):
        jm = get_job_manager()
        job = jm.create_job(library_name="D")

        data = _post(client, {"job_ids": [job.id, job.id]}).get_json()

        assert data == {"cancelled": [job.id], "skipped": []}

    def test_retry_chain_head_cancels_pending_children_when_pending(self, client):
        jm = get_job_manager()
        head = jm.create_job(library_name="chain", config={"is_retry_chain": True})
        child = jm.create_job(library_name="child", config={"is_retry": True, "parent_job_id": head.id})

        data = _post(client, {"job_ids": [head.id]}).get_json()

        assert data["cancelled"] == [head.id]
        assert jm.get_job(head.id).status == JobStatus.CANCELLED
        assert jm.get_job(child.id).status == JobStatus.CANCELLED

    def test_cancels_running_job_held_by_its_own_pause(self, client):
        jm = get_job_manager()
        held, active = jm.create_job(library_name="H"), jm.create_job(library_name="A")
        jm.start_job(held.id)
        jm.start_job(active.id)
        jm.get_job(held.id).paused = True

        data = _post(client, {"job_ids": [held.id, active.id]}).get_json()

        assert data["cancelled"] == [held.id]
        assert data["skipped"] == [{"id": active.id, "reason": "not_pending (running)"}]
        assert jm.get_job(held.id).status == JobStatus.CANCELLED
        assert jm.get_job(active.id).status == JobStatus.RUNNING

    def test_cancels_running_job_when_pause_all_is_on(self, client):
        from media_preview_generator.web.settings_manager import get_settings_manager

        jm = get_job_manager()
        job = jm.create_job(library_name="G")
        jm.start_job(job.id)
        get_settings_manager().processing_paused = True

        data = _post(client, {"job_ids": [job.id]}).get_json()

        assert data == {"cancelled": [job.id], "skipped": []}
        assert jm.get_job(job.id).status == JobStatus.CANCELLED

    def test_uses_atomic_pending_cancel_when_cancelling(self, client):
        jm = get_job_manager()
        job = jm.create_job(library_name="S")

        with patch.object(JobManager, "cancel_job_if_pending", autospec=True, return_value=(job, None)) as mock_cancel:
            data = _post(client, {"job_ids": [job.id]}).get_json()

        assert mock_cancel.call_args.args == (jm, job.id)
        assert mock_cancel.call_args.kwargs == {"global_pause": False}
        assert data == {"cancelled": [job.id], "skipped": []}

    def test_job_started_between_listing_and_cancel_is_skipped_not_cancelled(self, client):
        jm = get_job_manager()
        job = jm.create_job(library_name="Race")
        real = JobManager.cancel_job_if_pending

        def flip_then_cancel(self_, job_id, **kwargs):
            self_.start_job(job_id)
            return real(self_, job_id, **kwargs)

        with patch.object(JobManager, "cancel_job_if_pending", autospec=True, side_effect=flip_then_cancel):
            data = _post(client, {"job_ids": [job.id]}).get_json()

        assert data == {"cancelled": [], "skipped": [{"id": job.id, "reason": "not_pending (running)"}]}
        assert jm.get_job(job.id).status == JobStatus.RUNNING
        assert not jm.is_cancellation_requested(job.id)
        assert not any("Cancellation requested by user" in line for line in jm.get_logs(job.id))

    def test_flag_and_log_applied_only_to_pending_jobs_when_mixed(self, client):
        jm = get_job_manager()
        pending, running = jm.create_job(library_name="P"), jm.create_job(library_name="R")
        jm.start_job(running.id)

        _post(client, {"job_ids": [pending.id, running.id]})

        assert jm.is_cancellation_requested(pending.id)
        assert any("Cancellation requested by user" in line for line in jm.get_logs(pending.id))
        assert not jm.is_cancellation_requested(running.id)
        assert not any("Cancellation requested by user" in line for line in jm.get_logs(running.id))

    def test_paused_pending_job_cancels(self, client):
        jm = get_job_manager()
        job = jm.create_job(library_name="Paused")
        job.paused = True

        data = _post(client, {"job_ids": [job.id]}).get_json()

        assert data == {"cancelled": [job.id], "skipped": []}
        assert jm.get_job(job.id).status == JobStatus.CANCELLED
        assert jm.get_job(job.id).paused is False

    def test_live_retry_chain_head_with_running_child_is_skipped(self, client):
        jm = get_job_manager()
        head = jm.create_job(library_name="chain", config={"is_retry_chain": True, "last_outcome": "running"})
        child = jm.create_job(library_name="child", config={"is_retry": True, "parent_job_id": head.id})
        jm.start_job(child.id)

        data = _post(client, {"job_ids": [head.id]}).get_json()

        assert data == {"cancelled": [], "skipped": [{"id": head.id, "reason": "retry_running"}]}
        assert jm.get_job(head.id).status == JobStatus.PENDING
        assert jm.get_job(child.id).status == JobStatus.RUNNING
        assert not jm.is_cancellation_requested(head.id)
        assert not jm.is_cancellation_requested(child.id)

    def test_live_retry_chain_head_with_pending_child_cascades(self, client):
        jm = get_job_manager()
        head = jm.create_job(library_name="chain", config={"is_retry_chain": True, "last_outcome": "scheduled"})
        child = jm.create_job(library_name="child", config={"is_retry": True, "parent_job_id": head.id})

        data = _post(client, {"job_ids": [head.id]}).get_json()

        assert data == {"cancelled": [head.id], "skipped": []}
        assert jm.get_job(head.id).status == JobStatus.CANCELLED
        assert jm.get_job(child.id).status == JobStatus.CANCELLED
        assert jm.is_cancellation_requested(child.id)

    @pytest.mark.parametrize("body", [{}, {"job_ids": []}, {"job_ids": "abc"}, {"job_ids": [1, 2]}])
    def test_rejects_bad_body(self, client, body):
        assert _post(client, body).status_code == 400

    def test_rejects_oversize_list(self, client):
        ids = [f"id-{i}" for i in range(_BULK_CANCEL_MAX + 1)]

        assert _post(client, {"job_ids": ids}).status_code == 400

    def test_accepts_max_size_list(self, client):
        ids = [f"id-{i}" for i in range(_BULK_CANCEL_MAX)]

        assert _post(client, {"job_ids": ids}).status_code == 200

    def test_requires_auth(self, client):
        resp = client.post(URL, json={"job_ids": ["x"]})

        assert resp.status_code in (401, 403)
