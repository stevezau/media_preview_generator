"""Preview retry backoff survives restarts without renewing its saved deadline."""

import threading
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from media_preview_generator.web.jobs import get_job_manager
from media_preview_generator.web.routes import job_runner

from .journeys.test_journey_startup_gate import _reset_singletons, app  # noqa: F401

NOW = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("saved", "seconds"),
    [
        ("2026-10-06T00:59:00+00:00", -60),
        ("2026-10-06T01:00:00+00:00", 0),
        ("2026-10-06T01:00:03.200000+00:00", 3.2),
        ("2026-10-06T12:00:03.200000+11:00", 3.2),
        ("2026-10-06T01:00:03", 3),
        (None, 300),
        ("", 300),
        ("invalid", 300),
        (123, 300),
    ],
)
def test_saved_deadline_is_utc_and_does_not_restart_the_original_delay(saved, seconds):
    due = job_runner._preview_retry_deadline({"scheduled_at": saved, "retry_delay": 300}, NOW)
    assert due == NOW + timedelta(seconds=seconds)
    assert due.tzinfo is UTC


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("saved", "expected_sleeps", "expected_wait"),
    [
        ("2026-10-06T00:59:00+00:00", [], 0),
        ("2026-10-06T01:00:00+00:00", [], 0),
        ("2026-10-06T01:00:03.200000+00:00", [2, 2], 4),
        (None, [2, 2, 1], 5),
        ("invalid", [2, 2, 1], 5),
    ],
)
def test_real_preview_runner_waits_only_remaining_time_and_persists_deadline(
    app,  # noqa: F811
    monkeypatch,
    saved,
    expected_sleeps,
    expected_wait,  # noqa: F811
):
    manager = get_job_manager()
    entry = manager.create_job(
        config={
            "is_retry": True,
            "retry_delay": 5,
            "scheduled_at": saved,
            "server_id": "plex-1",
            "force_generate": True,
        }
    )
    sleeps = []
    test_thread = threading.get_ident()
    actual_sleep = time.sleep
    persisted = []
    progress = []
    original_progress = manager.update_progress

    def update(job_id, **kwargs):
        progress.append(kwargs)
        return original_progress(job_id, **kwargs)

    def sleep(seconds):
        if threading.get_ident() != test_thread or seconds < 0.5:
            return actual_sleep(seconds)
        sleeps.append(seconds)
        persisted.append(dict(manager.get_job(entry.id).config))

    monkeypatch.setattr(job_runner, "datetime", FrozenDateTime)
    monkeypatch.setattr(manager, "update_progress", update)
    with (
        app.app_context(),
        patch("time.sleep", side_effect=sleep),
        patch(
            "media_preview_generator.jobs.orchestrator.run_processing", return_value={"outcome": {"generated": 1}}
        ) as run,
    ):
        job_runner._start_job_async(entry.id, entry.config)
    assert run.call_count == 1, entry.error
    assert run.call_args.kwargs["job_id"] == entry.id
    assert run.call_args.kwargs["priority"] == entry.priority
    assert run.call_args.args[0].server_id_filter == "plex-1"
    assert run.call_args.args[0].regenerate_thumbnails is True
    assert sleeps == expected_sleeps
    assert next(row["retry_wait_total"] for row in progress if row.get("retry_wait_total") is not None) == expected_wait
    due = job_runner._preview_retry_deadline(entry.config, NOW)
    assert entry.config["scheduled_at"] == due.isoformat()
    assert all(row["scheduled_at"] == due.isoformat() for row in persisted)
    assert job_runner._preview_retry_deadline(entry.config, NOW + timedelta(seconds=2)) == due


@pytest.mark.parametrize("action", ["cancel", "fire-now", "pause"])
def test_retry_countdown_preserves_cancel_fire_now_and_global_pause(app, monkeypatch, action):  # noqa: F811
    from media_preview_generator.web.jobs import JobStatus
    from media_preview_generator.web.settings_manager import get_settings_manager

    manager = get_job_manager()
    settings = get_settings_manager()
    entry = manager.create_job(
        config={"is_retry": True, "retry_delay": 300, "scheduled_at": (NOW + timedelta(seconds=6)).isoformat()}
    )
    sleeps = []
    test_thread = threading.get_ident()
    actual_sleep = time.sleep

    def sleep(seconds):
        if threading.get_ident() != test_thread or seconds < 0.5:
            return actual_sleep(seconds)
        sleeps.append(seconds)
        if len(sleeps) == 1:
            if action == "cancel":
                manager.request_cancellation(entry.id)
                manager.cancel_job(entry.id)
            elif action == "fire-now":
                manager.merge_job_config(entry.id, {"force_fire_now": True})
            else:
                settings.processing_paused = True
        elif action == "pause" and seconds == 0.5:
            settings.processing_paused = False

    monkeypatch.setattr(job_runner, "datetime", FrozenDateTime)
    with (
        app.app_context(),
        patch("time.sleep", side_effect=sleep),
        patch(
            "media_preview_generator.jobs.orchestrator.run_processing", return_value={"outcome": {"generated": 1}}
        ) as run,
    ):
        job_runner._start_job_async(entry.id, entry.config)
    assert sleeps == ([2, 0.5, 2, 2] if action == "pause" else [2])
    assert run.call_count == (0 if action == "cancel" else 1)
    if action != "cancel":
        assert run.call_args.kwargs["job_id"] == entry.id
        assert run.call_args.kwargs["priority"] == entry.priority
    if action == "cancel":
        assert entry.status is JobStatus.CANCELLED
    assert entry.config["scheduled_at"] == (NOW + timedelta(seconds=6)).isoformat()
    assert not settings.processing_paused
