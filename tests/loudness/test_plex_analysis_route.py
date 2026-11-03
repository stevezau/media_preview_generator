"""Setup Health's Set to Never for Plex's own loudness analysis: POST /api/servers/<id>/plex-loudness-analysis."""

from __future__ import annotations

# The shared route harness exports pytest fixtures with these names.
# ruff: noqa: F811
from unittest.mock import MagicMock

import pytest

from media_preview_generator.loudness import guard
from media_preview_generator.markers.publishers.base import Capability, CapabilityReport

from ..markers.test_plex_detection_route import JELLYFIN, _plex, conn, seed  # noqa: F401 - fixtures

URL = "/api/servers/plex-1/plex-loudness-analysis"
_REAL_CAPABILITY = guard.loudness_capability


@pytest.fixture(autouse=True)
def ready_writer(monkeypatch):
    probe = MagicMock(return_value=CapabilityReport(Capability.READY, "Ready"))
    monkeypatch.setattr(guard, "loudness_capability", probe)
    return probe


def _loudness_plex(enabled: bool = True) -> dict:
    return {**_plex(enabled=False), "loudness": {"enabled": enabled, "library_ids": None}}


def test_needs_authentication(app, seed, conn):  # noqa: F811
    seed(_loudness_plex())
    assert app.test_client().post(URL).status_code == 401
    conn.query.assert_not_called()


def test_no_cpu_capacity_refuses_native_disable(client, seed, conn):
    from media_preview_generator.web.settings_manager import get_settings_manager

    seed(_loudness_plex())
    get_settings_manager().update_worker_groups([])
    response = client.post(URL)
    assert response.status_code == 409
    assert "CPU worker group" in response.get_json()["error"]
    conn.query.assert_not_called()


def test_cpu_hours_fully_blocked_by_global_quiet_hours_refuse_native_disable(client, seed, conn):
    from media_preview_generator.web.settings_manager import get_settings_manager

    from ..test_worker_group_policy import cpu_group, window

    seed(_loudness_plex())
    settings = get_settings_manager()
    settings.update_worker_groups([cpu_group(availability=window([0]))])
    settings.set(
        "quiet_hours",
        {"enabled": True, "day_basis": "start", "windows": [{"days": ["mon"], "start": "23:00", "end": "07:00"}]},
    )
    assert client.post(URL).status_code == 409
    conn.query.assert_not_called()


@pytest.mark.parametrize(
    "library_ids", [None, ["1"], ["1", "2"]], ids=["default-libraries", "partial-selection", "all-video"]
)
def test_sets_only_the_loudness_pref_to_never(client, seed, conn, library_ids, ready_writer):  # noqa: F811
    seed({**_loudness_plex(), "loudness": {"enabled": True, "library_ids": library_ids}})
    resp = client.post(URL)
    assert resp.get_json() == {"ok": True, "error": ""}
    conn.query.assert_called_once_with("/:/prefs?LoudnessAnalysisBehavior=never", method=conn._session.put)
    ready_writer.assert_called_once()
    live, cfg = ready_writer.call_args.args
    assert live.id == cfg.id == "plex-1"
    assert cfg.loudness["library_ids"] == library_ids


@pytest.mark.parametrize(
    ("server", "url", "status", "error"),
    [
        (JELLYFIN, URL, 400, "Plex's marker settings are Plex settings"),
        ({**_loudness_plex(), "enabled": False}, URL, 409, "is disabled"),
        (_loudness_plex(enabled=False), URL, 400, "Loudness is off for this server"),
        (_loudness_plex(), "/api/servers/nope/plex-loudness-analysis", 404, "not found"),
    ],
    ids=["not-plex", "disabled-server", "loudness-off", "unknown-server"],
)
def test_refuses_a_server_the_row_could_not_show_for(client, seed, conn, server, url, status, error):  # noqa: F811
    seed(server)
    resp = client.post(url)
    assert resp.status_code == status and error in resp.get_json()["error"]
    conn.query.assert_not_called()


def test_plexs_refusal_is_reported_not_raised(client, seed, conn):  # noqa: F811
    seed(_loudness_plex())
    conn.query.side_effect = RuntimeError("(401) unauthorized")
    assert client.post(URL).get_json() == {"ok": False, "error": "(401) unauthorized"}


@pytest.mark.parametrize("library_ids", [[], ["missing"]])
def test_no_actual_chosen_library_refuses_without_probing_or_writing(client, seed, conn, ready_writer, library_ids):
    seed({**_loudness_plex(), "loudness": {"enabled": True, "library_ids": library_ids}})
    response = client.post(URL)
    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "error": "Choose at least one movie or TV library for this app's loudness analysis first",
    }
    ready_writer.assert_not_called()
    conn.query.assert_not_called()


@pytest.mark.parametrize("state", [state for state in Capability if state is not Capability.READY])
def test_writer_must_still_be_ready_when_action_runs(client, seed, conn, ready_writer, state):
    seed(_loudness_plex())
    ready_writer.return_value = CapabilityReport(state, "Writer is unavailable now")
    response = client.post(URL)
    assert response.status_code == 409
    assert response.get_json() == {"ok": False, "error": "Writer is unavailable now"}
    ready_writer.assert_called_once()
    live, cfg = ready_writer.call_args.args
    assert live.id == cfg.id == "plex-1"
    assert cfg.loudness == {"enabled": True, "library_ids": None}
    assert ready_writer.call_args.kwargs == {}
    conn.query.assert_not_called()


def test_missing_local_database_really_refuses_global_native_disable(client, seed, conn, monkeypatch):
    seed({**_loudness_plex(), "output": {}})
    monkeypatch.setattr(guard, "loudness_capability", _REAL_CAPABILITY)
    response = client.post(URL)
    assert response.status_code == 409
    assert response.get_json()["ok"] is False
    assert "local Plex config folder" in response.get_json()["error"]
    conn.query.assert_not_called()


def test_probe_failure_refuses_without_leaking_connection_details(client, seed, conn, ready_writer):
    seed(_loudness_plex())
    ready_writer.side_effect = RuntimeError("unavailable ?X-Plex-Token=test-private-token")
    response = client.post(URL)
    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "error": "Could not check loudness analysis. Check the Plex connection and try Setup Health again.",
    }
    conn.query.assert_not_called()


def test_plex_error_masks_credentials_in_response_and_log(client, seed, conn):
    from loguru import logger

    seed(_loudness_plex())
    conn.query.side_effect = RuntimeError("Failed /:/prefs?X-Plex-Token=private-review-token")
    messages = []
    sink = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        response = client.post(URL)
    finally:
        logger.remove(sink)
    assert response.status_code == 200
    assert response.get_json() == {"ok": False, "error": "Failed /:/prefs?X-Plex-Token=****"}
    assert "private-review-token" not in "\n".join(messages)
    conn.query.assert_called_once_with("/:/prefs?LoudnessAnalysisBehavior=never", method=conn._session.put)


LIB_URL = "/api/servers/plex-1/plex-loudness-library-analysis"


@pytest.fixture
def live_off(monkeypatch):
    from media_preview_generator.web.routes import api_servers

    live = MagicMock()
    live.set_library_loudness_analysis_off.return_value = None
    monkeypatch.setattr(api_servers, "_instantiate_for_probe", lambda cfg: live)
    return live


def test_library_off_turns_off_the_chosen_libraries(client, seed, live_off):  # noqa: F811
    seed(_loudness_plex())
    resp = client.post(LIB_URL, json={"library_ids": ["1"]})
    assert resp.get_json() == {"ok": True, "error": ""}
    live_off.set_library_loudness_analysis_off.assert_called_once_with(["1"])


@pytest.mark.parametrize("body", [{}, {"library_ids": []}, {"library_ids": "1"}, {"library_ids": [1]}])
def test_library_off_needs_a_nonempty_list_of_ids(client, seed, live_off, body):  # noqa: F811
    seed(_loudness_plex())
    assert client.post(LIB_URL, json=body).status_code == 400
    live_off.set_library_loudness_analysis_off.assert_not_called()


def test_library_off_refuses_ids_outside_the_selection(client, seed, live_off):  # noqa: F811
    seed({**_loudness_plex(), "loudness": {"enabled": True, "library_ids": ["1"]}})
    resp = client.post(LIB_URL, json={"library_ids": ["1", "2"]})
    assert resp.status_code == 400
    live_off.set_library_loudness_analysis_off.assert_not_called()


def test_library_off_refuses_when_loudness_is_off(client, seed, live_off):  # noqa: F811
    seed(_loudness_plex(enabled=False))
    resp = client.post(LIB_URL, json={"library_ids": ["1"]})
    assert resp.status_code == 400 and "Loudness is off" in resp.get_json()["error"]
    live_off.set_library_loudness_analysis_off.assert_not_called()


def test_library_off_reports_plexs_refusal(client, seed, live_off):  # noqa: F811
    seed(_loudness_plex())
    live_off.set_library_loudness_analysis_off.return_value = "Filmer: (401) unauthorized"
    assert client.post(LIB_URL, json={"library_ids": ["1"]}).get_json() == {
        "ok": False,
        "error": "Filmer: (401) unauthorized",
    }


def test_library_off_needs_a_ready_writer(client, seed, live_off, ready_writer):  # noqa: F811
    seed(_loudness_plex())
    ready_writer.return_value = CapabilityReport(next(c for c in Capability if c is not Capability.READY), "Not ready")
    assert client.post(LIB_URL, json={"library_ids": ["1"]}).status_code == 409
    live_off.set_library_loudness_analysis_off.assert_not_called()


@pytest.mark.parametrize(
    ("server", "url", "status"),
    [
        ({**JELLYFIN, "type": "emby"}, LIB_URL, 400),
        (JELLYFIN, LIB_URL, 400),
        (_loudness_plex(), "/api/servers/nope/plex-loudness-library-analysis", 404),
    ],
    ids=["emby", "jellyfin", "unknown-id"],
)
def test_library_off_refuses_non_plex_and_unknown_servers(client, seed, live_off, server, url, status):  # noqa: F811
    seed(server)
    assert client.post(url, json={"library_ids": ["1"]}).status_code == status
    live_off.set_library_loudness_analysis_off.assert_not_called()
