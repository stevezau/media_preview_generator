"""Config-directory health preflight (issue #278).

A read-only ``/config`` used to surface as SQLite's opaque "attempt to write
a readonly database" — flooding the log once per item, blaming cron syntax on
schedule creation, and leaving the UI looking frozen. These tests pin the
new behaviour: one actionable probe result, a 503 (not a misleading 500) on
the write paths, and a health endpoint the banner reads.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.config_health import probe_config_health


@pytest.fixture()
def app(tmp_path):
    config_dir = str(tmp_path / "cfg")
    os.makedirs(config_dir, exist_ok=True)
    with patch.dict(
        os.environ,
        {
            "CONFIG_DIR": config_dir,
            "WEB_AUTH_TOKEN": "test-token-12345678",
            "WEB_PORT": "8099",
        },
    ):
        flask_app = create_app(config_dir=config_dir)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app


@pytest.fixture()
def client(app):
    return app.test_client()


def _headers():
    return {"Authorization": "Bearer test-token-12345678"}


_UNWRITABLE = {
    "path": "/config",
    "writable": False,
    "status": "not_writable",
    "detail": "Config folder /config isn't writable by this container.",
    "hint": "On the host run `chown -R 1000:1000 <your config folder>` and restart.",
    "warnings": [],
    "read_only_mount": False,
    "network_fs": None,
    "free_bytes": 1,
    "low_space": False,
    "process_user": "1000:1000",
    "dir_owner": "0:0",
    "dir_mode": "0o755",
}


class TestProbeConfigHealth:
    def test_writable_directory_reports_ok(self, tmp_path):
        health = probe_config_health(str(tmp_path))
        assert health["writable"] is True
        assert health["status"] == "ok"
        # The probe file must be cleaned up — no litter left behind.
        assert not any(name.startswith(".config-write-probe") for name in os.listdir(tmp_path))

    def test_readonly_directory_is_not_writable_with_chown_hint(self, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        os.chmod(ro, 0o500)
        try:
            health = probe_config_health(str(ro))
        finally:
            os.chmod(ro, 0o700)  # restore so pytest can clean up
        assert health["writable"] is False
        assert health["status"] == "not_writable"
        assert "chown" in health["hint"]
        assert health["process_user"] == f"{os.getuid()}:{os.getgid()}"

    def test_cleanup_unlink_failure_does_not_flip_verdict(self, tmp_path):
        # A successful probe WRITE proves writability; a racing/failed cleanup
        # unlink must not report the dir unwritable (the gthread shared-PID
        # race the MED review finding was about).
        with patch(
            "media_preview_generator.web.config_health.os.unlink",
            side_effect=FileNotFoundError("already gone"),
        ):
            health = probe_config_health(str(tmp_path))
        assert health["writable"] is True
        assert health["status"] == "ok"

    def test_low_space_warning_when_free_below_threshold(self, tmp_path):
        with patch("media_preview_generator.web.config_health.os.statvfs") as mock_statvfs:
            mock_statvfs.return_value = type("S", (), {"f_bavail": 1, "f_frsize": 4096})()
            health = probe_config_health(str(tmp_path))
        assert health["low_space"] is True
        assert any(w["kind"] == "low_space" for w in health["warnings"])

    def test_network_fs_warning(self, tmp_path):
        with patch(
            "media_preview_generator.web.config_health._mount_for_path",
            return_value=("nfs4", {"rw"}),
        ):
            health = probe_config_health(str(tmp_path))
        assert health["network_fs"] == "nfs4"
        assert any(w["kind"] == "network_fs" for w in health["warnings"])

    def test_readonly_mount_gives_ro_specific_hint(self, tmp_path):
        ro = tmp_path / "ro2"
        ro.mkdir()
        os.chmod(ro, 0o500)
        try:
            with patch(
                "media_preview_generator.web.config_health._mount_for_path",
                return_value=("ext4", {"ro"}),
            ):
                health = probe_config_health(str(ro))
        finally:
            os.chmod(ro, 0o700)
        assert health["writable"] is False
        assert health["status"] == "read_only_mount"
        assert ":ro" in health["hint"]
        assert "chown" not in health["hint"]


class TestConfigHealthEndpoint:
    def test_returns_config_and_media_issues(self, client):
        resp = client.get("/api/system/config-health", headers=_headers())
        assert resp.status_code == 200
        body = resp.get_json()
        assert "config" in body
        assert body["config"]["writable"] is True
        assert "media_mount_issues" in body


class TestScanGuard:
    def test_create_job_blocked_503_when_config_unwritable(self, client):
        with patch(
            "media_preview_generator.web.config_health.probe_config_health",
            return_value=_UNWRITABLE,
        ):
            resp = client.post("/api/jobs", json={"library_ids": ["1"]}, headers=_headers())
        assert resp.status_code == 503
        body = resp.get_json()
        assert body["error"] == _UNWRITABLE["detail"]
        assert body["hint"] == _UNWRITABLE["hint"]

    def test_create_job_proceeds_when_writable(self, client):
        # Writable config (the fixture's real dir) — the guard must not block.
        # Stub _start_job_async so we don't spawn a real worker thread (which
        # would pollute global processing state for other tests); we only care
        # that we got *past* the 503 guard.
        with patch("media_preview_generator.web.routes.api_jobs._start_job_async"):
            resp = client.post("/api/jobs", json={"library_ids": ["1"]}, headers=_headers())
        assert resp.status_code != 503
        assert resp.status_code == 201


class TestScheduleReadonlyError:
    def test_readonly_db_returns_503_not_misleading_500(self, client):
        readonly_exc = Exception("(sqlite3.OperationalError) attempt to write a readonly database")
        with patch("media_preview_generator.web.routes.api_schedules.get_schedule_manager") as mock_gsm:
            mock_gsm.return_value.create_schedule.side_effect = readonly_exc
            resp = client.post(
                "/api/schedules",
                json={"name": "Nightly Scan", "cron_expression": "0 3 * * *"},
                headers=_headers(),
            )
        assert resp.status_code == 503
        body = resp.get_json()
        assert "hint" in body
        assert "config_health" in body

    def test_unable_to_open_database_also_returns_503(self, client):
        # The classifier routes BOTH "readonly database" and "unable to open
        # database" to the permissions 503 — cover the second cell too.
        exc = Exception("(sqlite3.OperationalError) unable to open database file")
        with patch("media_preview_generator.web.routes.api_schedules.get_schedule_manager") as mock_gsm:
            mock_gsm.return_value.create_schedule.side_effect = exc
            resp = client.post(
                "/api/schedules",
                json={"name": "Nightly Scan", "cron_expression": "0 3 * * *"},
                headers=_headers(),
            )
        assert resp.status_code == 503
        assert "hint" in resp.get_json()

    def test_non_readonly_error_still_returns_500(self, client):
        with patch("media_preview_generator.web.routes.api_schedules.get_schedule_manager") as mock_gsm:
            mock_gsm.return_value.create_schedule.side_effect = RuntimeError("boom")
            resp = client.post(
                "/api/schedules",
                json={"name": "Nightly Scan", "cron_expression": "0 3 * * *"},
                headers=_headers(),
            )
        assert resp.status_code == 500
