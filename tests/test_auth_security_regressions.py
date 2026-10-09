"""Regression tests for auth and token-handling security fixes."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web.app import create_app
from media_preview_generator.web.notifications import reset_session
from media_preview_generator.web.settings_manager import MAX_DISMISSED_NOTIFICATIONS, SettingsManager

TOKEN = "test-token-12345678"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _make_app(tmp_path, settings: dict | str):
    config_dir = str(tmp_path / "config")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "auth.json"), "w") as fh:
        json.dump({"token": TOKEN}, fh)
    with open(os.path.join(config_dir, "settings.json"), "w") as fh:
        fh.write(settings if isinstance(settings, str) else json.dumps(settings))
    return config_dir


@pytest.fixture(autouse=True)
def _clean_session_dismissals():
    reset_session()
    yield
    reset_session()


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    """Build a test client against a settings.json with the given content."""

    def _build(settings):
        config_dir = _make_app(tmp_path, settings)
        monkeypatch.setenv("CONFIG_DIR", config_dir)
        monkeypatch.setenv("WEB_AUTH_TOKEN", TOKEN)
        monkeypatch.setenv("WEB_PORT", "8099")
        app = create_app(config_dir=config_dir)
        app.config["TESTING"] = True
        app.config["WTF_CSRF_ENABLED"] = False
        return app.test_client()

    return _build


class TestDismissRoutesAuth:
    @pytest.mark.parametrize("suffix", ["dismiss", "dismiss-permanent"])
    def test_unauthenticated_dismiss_is_rejected_after_setup(self, make_client, suffix):
        client = make_client({"setup_complete": True})
        resp = client.post(f"/api/system/notifications/vulkan_software_fallback/{suffix}")
        assert resp.status_code == 401

    @pytest.mark.parametrize("suffix", ["dismiss", "dismiss-permanent"])
    def test_unknown_id_is_refused_and_not_stored(self, make_client, suffix):
        client = make_client({"setup_complete": True})
        resp = client.post(f"/api/system/notifications/not-a-real-card/{suffix}", headers=AUTH)
        assert resp.status_code == 400
        assert SettingsManager.dismissed_notifications.fget(_reload(client)) == []

    def test_known_id_is_stored_when_authenticated(self, make_client):
        client = make_client({"setup_complete": True})
        resp = client.post("/api/system/notifications/vulkan_software_fallback/dismiss-permanent", headers=AUTH)
        assert resp.status_code == 200
        assert _reload(client).dismissed_notifications == ["vulkan_software_fallback"]

    def test_dismissed_list_is_capped(self, tmp_path):
        manager = SettingsManager(config_dir=str(tmp_path))
        for i in range(MAX_DISMISSED_NOTIFICATIONS + 25):
            manager.dismiss_notification_permanent(f"id-{i}")
        stored = manager.dismissed_notifications
        assert len(stored) == MAX_DISMISSED_NOTIFICATIONS
        assert stored[-1] == f"id-{MAX_DISMISSED_NOTIFICATIONS + 24}"


def _reload(client):
    from media_preview_generator.web.settings_manager import get_settings_manager

    return get_settings_manager()


class TestTokenComparison:
    def test_non_ascii_and_non_str_tokens_are_rejected_not_errors(self, make_client):
        from media_preview_generator.web.auth import validate_token

        make_client({"setup_complete": True})
        assert validate_token("café-token") is False
        assert validate_token(5) is False
        assert validate_token(TOKEN) is True

    def test_non_ascii_header_gives_401(self, make_client):
        client = make_client({"setup_complete": True})
        resp = client.get("/api/settings", headers={"X-Auth-Token": "café"})
        assert resp.status_code == 401

    def test_login_with_non_string_token_gives_401(self, make_client):
        client = make_client({"setup_complete": True})
        resp = client.post("/api/auth/login", json={"token": 5})
        assert resp.status_code in (400, 401)


class TestFailClosedOnUnreadableSettings:
    def test_corrupt_settings_json_keeps_api_locked(self, make_client):
        client = make_client("{not valid json")
        resp = client.get("/api/settings")
        assert resp.status_code in (401, 302)
        assert _reload(client).is_setup_complete() is True

    def test_non_object_settings_json_counts_as_unreadable(self, tmp_path):
        (tmp_path / "settings.json").write_text("[1, 2]")
        manager = SettingsManager(config_dir=str(tmp_path))
        assert manager.is_setup_complete() is True

    def test_missing_settings_json_is_a_fresh_install(self, tmp_path):
        manager = SettingsManager(config_dir=str(tmp_path))
        assert manager.is_setup_complete() is False


class TestPlexTokenOnlyGoesToStoredUrl:
    STORED = {"setup_complete": True, "plex_url": "http://plex:32400", "plex_token": "SECRET-STORED"}

    def _plex_response(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"MediaContainer": {"friendlyName": "Plex", "Directory": []}}
        return resp

    def test_test_endpoint_refuses_stored_token_for_other_url(self, make_client):
        client = make_client(self.STORED)
        with patch("requests.get") as get:
            resp = client.post("/api/plex/test", headers=AUTH, json={"url": "http://attacker.example:32400"})
        assert resp.status_code == 400
        get.assert_not_called()

    def test_test_endpoint_uses_stored_token_for_stored_url(self, make_client):
        client = make_client(self.STORED)
        with patch("requests.get", return_value=self._plex_response()) as get:
            resp = client.post("/api/plex/test", headers=AUTH, json={"url": "http://plex:32400/"})
        assert resp.status_code == 200
        assert get.call_args.kwargs["headers"]["X-Plex-Token"] == "SECRET-STORED"

    def test_test_endpoint_sends_caller_token_to_caller_url(self, make_client):
        client = make_client(self.STORED)
        with patch("requests.get", return_value=self._plex_response()) as get:
            client.post("/api/plex/test", headers=AUTH, json={"url": "http://other:32400", "token": "mine"})
        assert get.call_args.kwargs["headers"]["X-Plex-Token"] == "mine"

    def test_libraries_endpoint_refuses_stored_token_for_other_url(self, make_client):
        client = make_client(self.STORED)
        with patch("requests.get") as get:
            resp = client.get("/api/plex/libraries?url=http://attacker.example:32400", headers=AUTH)
        assert resp.status_code == 400
        get.assert_not_called()

    def test_libraries_endpoint_accepts_post_body(self, make_client):
        client = make_client(self.STORED)
        with patch("requests.get", return_value=self._plex_response()) as get:
            resp = client.post("/api/plex/libraries", headers=AUTH, json={"url": "http://other:32400", "token": "mine"})
        assert resp.status_code == 200
        assert get.call_args.kwargs["headers"]["X-Plex-Token"] == "mine"

    def test_unauthenticated_pre_setup_caller_cannot_exfiltrate_stored_token(self, make_client):
        client = make_client({"plex_token": "SECRET-STORED"})
        with patch("requests.get") as get:
            resp = client.post("/api/plex/test", json={"url": "http://attacker.example"})
        assert resp.status_code == 400
        get.assert_not_called()

    def test_api_libraries_does_not_send_stored_token_to_other_url(self, make_client):
        client = make_client(self.STORED)
        with patch("requests.get") as get:
            client.get("/api/libraries?url=http://attacker.example:32400", headers=AUTH)
        for call in get.call_args_list:
            assert "SECRET-STORED" not in json.dumps(call.kwargs.get("headers", {}))
            assert "attacker.example" not in str(call.args)
