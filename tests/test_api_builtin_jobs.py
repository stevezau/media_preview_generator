"""Built-in jobs API: GET shape, PUT validation and effect on the scheduler, run now."""

from unittest.mock import MagicMock

import pytest

from media_preview_generator.library_health.models import CheckProgress
from media_preview_generator.markers.reconcile import ReconcileQueued
from media_preview_generator.web.routes import api_schedules as mod
from media_preview_generator.web.scheduler import ScheduleManager

ROUTES = [
    ("get", "/api/schedules/builtin"),
    ("put", "/api/schedules/builtin/library_health"),
    ("post", "/api/schedules/builtin/library_health/run"),
]


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    manager = ScheduleManager(config_dir=str(tmp_path / "sched"), run_job_callback=None)
    monkeypatch.setattr("media_preview_generator.web.scheduler._schedule_manager", manager)
    manager.start()
    yield manager
    manager.stop()


@pytest.fixture
def runner(monkeypatch):
    fake = MagicMock()
    fake.start_check.return_value = (True, CheckProgress(kind="check", started_at=1.0, reason="nightly"))
    monkeypatch.setattr(mod, "get_runner", lambda: fake)
    return fake


@pytest.mark.parametrize(("method", "url"), ROUTES)
def test_auth_required(client, method, url):
    assert getattr(client, method)(url, headers={"X-Auth-Token": "wrong"}).status_code == 401


def test_get_lists_both_jobs_with_defaults(client, auth_headers, scheduler, monkeypatch):
    monkeypatch.setattr("media_preview_generator.markers.triggers.markers_enabled_anywhere", lambda: False)

    jobs = client.get("/api/schedules/builtin", headers=auth_headers).get_json()["jobs"]

    health, check = jobs
    assert (health["id"], health["name"]) == ("library_health", "Library Health check")
    assert (health["enabled"], health["time"], health["catch_up"], health["catch_up_max_files"]) == (
        True,
        "03:00",
        True,
        500,
    )
    assert health["next_run"] is not None
    assert (check["id"], check["name"], check["time"]) == ("check_servers", "Check servers", "04:30")
    assert check["intro_credits_on"] is False
    assert check["next_run"] is not None
    assert "enabled" not in check


def test_put_changes_the_time_and_the_next_run(client, auth_headers, scheduler):
    response = client.put("/api/schedules/builtin/library_health", headers=auth_headers, json={"time": "02:15"})

    body = response.get_json()
    assert response.status_code == 200
    assert body["time"] == "02:15"
    assert "T02:15:00" in body["next_run"]
    saved = client.get("/api/schedules/builtin", headers=auth_headers).get_json()["jobs"][0]
    assert saved["time"] == "02:15"


def test_put_off_switch_removes_the_job_and_on_brings_it_back(client, auth_headers, scheduler):
    client.put("/api/schedules/builtin/library_health", headers=auth_headers, json={"enabled": False})
    assert scheduler.scheduler.get_job("__library_health_nightly") is None
    body = client.get("/api/schedules/builtin", headers=auth_headers).get_json()["jobs"][0]
    assert (body["enabled"], body["next_run"]) == (False, None)

    client.put("/api/schedules/builtin/library_health", headers=auth_headers, json={"enabled": True})
    assert scheduler.scheduler.get_job("__library_health_nightly") is not None


def test_put_check_servers_time_moves_its_cron(client, auth_headers, scheduler):
    response = client.put("/api/schedules/builtin/check_servers", headers=auth_headers, json={"time": "05:10"})

    assert response.status_code == 200
    assert "hour='5'" in str(scheduler.scheduler.get_job("__markers_check_servers_daily").trigger)


def test_put_catch_up_switch_persists(client, auth_headers, scheduler):
    client.put("/api/schedules/builtin/library_health", headers=auth_headers, json={"catch_up": False})

    assert client.get("/api/schedules/builtin", headers=auth_headers).get_json()["jobs"][0]["catch_up"] is False


@pytest.mark.parametrize(
    "body",
    [{"time": "25:00"}, {"time": "3pm"}, {"enabled": "yes"}, {"nope": True}, {}, [1]],
)
def test_put_rejects_bad_input_with_a_plain_message(client, auth_headers, scheduler, body):
    response = client.put("/api/schedules/builtin/library_health", headers=auth_headers, json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]


def test_put_check_servers_has_no_off_switch(client, auth_headers, scheduler):
    response = client.put("/api/schedules/builtin/check_servers", headers=auth_headers, json={"enabled": False})

    assert response.status_code == 400


def test_put_unknown_job_is_404(client, auth_headers, scheduler):
    assert client.put("/api/schedules/builtin/nope", headers=auth_headers, json={"time": "01:00"}).status_code == 404


def test_the_generic_settings_route_cannot_write_builtin_jobs(client, auth_headers, scheduler):
    client.post("/api/settings", headers=auth_headers, json={"builtin_jobs": {"library_health": {"time": "bad"}}})

    assert client.get("/api/schedules/builtin", headers=auth_headers).get_json()["jobs"][0]["time"] == "03:00"


def test_run_library_health_starts_a_nightly_check(client, auth_headers, runner):
    response = client.post("/api/schedules/builtin/library_health/run", headers=auth_headers)

    assert response.get_json() == {"ok": True}
    runner.start_check.assert_called_once_with("nightly")


def test_run_library_health_is_409_while_a_check_is_running(client, auth_headers, runner):
    runner.start_check.return_value = (False, CheckProgress(kind="check", started_at=1.0, reason="manual"))

    response = client.post("/api/schedules/builtin/library_health/run", headers=auth_headers)

    assert response.status_code == 409
    assert "already running" in response.get_json()["error"]


def test_run_check_servers_queues_the_check(client, auth_headers, monkeypatch):
    run = MagicMock(return_value=ReconcileQueued("job-1", True))
    monkeypatch.setattr("media_preview_generator.markers.reconcile.run_markers_reconcile", run)

    response = client.post("/api/schedules/builtin/check_servers/run", headers=auth_headers)

    assert response.get_json() == {"ok": True, "job_id": "job-1", "reused": False}
    run.assert_called_once_with()


def test_run_check_servers_reports_a_joined_job(client, auth_headers, monkeypatch):
    run = MagicMock(return_value=ReconcileQueued("job-1", False))
    monkeypatch.setattr("media_preview_generator.markers.reconcile.run_markers_reconcile", run)

    body = client.post("/api/schedules/builtin/check_servers/run", headers=auth_headers).get_json()

    assert body["reused"] is True


def test_run_check_servers_is_409_when_intro_and_credits_is_off_everywhere(client, auth_headers, monkeypatch):
    monkeypatch.setattr("media_preview_generator.markers.triggers.markers_enabled_anywhere", lambda: False)

    response = client.post("/api/schedules/builtin/check_servers/run", headers=auth_headers)

    assert response.status_code == 409
    assert response.get_json() == {"error": "Intro & Credits is off on every server, so there is nothing to check"}


def test_restoring_settings_with_the_health_check_off_removes_its_job(client, auth_headers, scheduler, tmp_path):
    from media_preview_generator.web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    sm.config_dir = tmp_path
    sm.settings_file = tmp_path / "settings.json"
    sm.settings_file.write_text("{}")
    (tmp_path / "settings.json.20260101-100000.bak").write_text(
        '{"builtin_jobs": {"library_health": {"enabled": false}}}'
    )
    assert scheduler.scheduler.get_job("__library_health_nightly") is not None

    response = client.post(
        "/api/settings/backups/restore",
        headers=auth_headers,
        json={"file": "settings.json", "backup": "settings.json.20260101-100000.bak"},
    )

    assert response.status_code == 200
    assert scheduler.scheduler.get_job("__library_health_nightly") is None


def test_run_unknown_job_is_404(client, auth_headers):
    assert client.post("/api/schedules/builtin/nope/run", headers=auth_headers).status_code == 404
