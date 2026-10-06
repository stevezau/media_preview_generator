"""Loudness queue states through actual Flask, SocketIO, JobManager and SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import requests
from playwright.sync_api import expect

_HEADERS = {"X-Auth-Token": "e2e-test-token"}


@pytest.mark.e2e
@pytest.mark.parametrize("viewport", [{"width": 1280, "height": 720}, {"width": 390, "height": 844}])
def test_paused_loudness_job_is_queued_and_cancellable_without_review(
    backend_real_app, backend_real_page, viewport, tmp_path
):
    """Create through the real API; neither queue data nor browser events are stubbed."""
    url, config_dir = backend_real_app
    page = backend_real_page
    page.set_viewport_size(viewport)
    response = requests.post(f"{url}/api/processing/pause", headers=_HEADERS, timeout=15)
    assert response.ok, response.text
    page.goto(url + "/")
    expect(page.locator("#globalPauseResumeQueue")).to_contain_text("Resume Processing")
    response = requests.post(
        f"{url}/api/loudness/jobs",
        headers=_HEADERS,
        json={"library_name": "Loudness queue proof"},
        timeout=15,
    )
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    row = page.locator(f"#job-row-{job_id}")
    expect(row).to_be_visible(timeout=10000)
    expect(row.locator(".job-kind-badge")).to_have_text("Plex loudness")
    expect(row.locator(".status-dot")).to_have_text("Paused")
    expect(row).not_to_contain_text("Review")
    expect(row.locator('[aria-label="Cancel job"]')).to_be_enabled()
    row.screenshot(path=str(tmp_path / "loudness-paused.png"))
    current = requests.get(f"{url}/api/jobs/{job_id}", headers=_HEADERS, timeout=15).json()
    assert current["status"] == "pending" and current["started_at"] is None
    with sqlite3.connect(Path(config_dir) / "jobs.db") as db:
        assert db.execute("SELECT status, kind FROM jobs WHERE id = ?", (job_id,)).fetchone() == (
            "pending",
            "loudness",
        )

    response = requests.post(f"{url}/api/jobs/{job_id}/cancel", headers=_HEADERS, timeout=15)
    assert response.ok, response.text
    expect(row).to_have_count(0, timeout=10000)
    page.get_by_label("Status", exact=True).select_option(label="All jobs and history")
    expect(row.locator(".status-dot")).to_have_text("Cancelled", timeout=10000)
    expect(row.locator('[aria-label="Re-run job"]')).to_be_enabled()
    page.reload()
    expect(row).to_have_count(0)
    page.get_by_label("Status", exact=True).select_option(label="All jobs and history")
    expect(row.locator(".status-dot")).to_have_text("Cancelled")
    with sqlite3.connect(Path(config_dir) / "jobs.db") as db:
        assert db.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone() == ("cancelled",)
