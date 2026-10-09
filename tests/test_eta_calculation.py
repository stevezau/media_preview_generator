"""
Tests for the worker ETA (from ffmpeg) exposed via WorkerStatus.eta.

This module tests the data model and formatting contract for worker ETA.
"""

from media_preview_generator.web.jobs import WorkerStatus
from media_preview_generator.web.routes.job_runner import _format_eta


class TestWorkerStatusEta:
    """WorkerStatus retains eta for ffmpeg-based per-worker ETA."""

    def test_worker_status_has_eta(self):
        w = WorkerStatus(eta="2m 5s")
        assert w.eta == "2m 5s"
        assert w.to_dict()["eta"] == "2m 5s"

    def test_worker_status_eta_default_empty(self):
        w = WorkerStatus()
        assert w.eta == ""


class TestFormatEtaWorkerDisplay:
    """Format used for worker ETA display (seconds -> human-readable)."""

    def test_seconds_only(self):
        assert _format_eta(45) == "45s"

    def test_minutes_and_seconds(self):
        assert _format_eta(125) == "2m 5s"

    def test_hours_and_minutes(self):
        assert _format_eta(3700) == "1h 1m"

    def test_zero(self):
        assert _format_eta(0) == "0s"
