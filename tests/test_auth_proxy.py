"""AUTH_METHOD=proxy: the proxy's user header signs a request in only when it also carries the proxy's secret (#454)."""

import json
import os
import re
from unittest.mock import patch

import pytest

from media_preview_generator.web.auth import (
    AUTH_METHOD_PROXY,
    ProxyAuthConfigError,
    get_auth_method,
    validate_proxy_auth_config,
)

API_TOKEN = "proxy-test-api-token-123"
SECRET = "s" * 40
PROXY_HEADERS = {"X-MPG-Proxy-Secret": SECRET, "Remote-User": "alice"}


def _make_app(config_dir, extra_env: dict):
    from media_preview_generator.web import settings_manager as sm_mod
    from media_preview_generator.web.app import create_app

    sm_mod.reset_settings_manager()
    (config_dir / "settings.json").write_text(json.dumps({"setup_complete": True}))
    env = {"CONFIG_DIR": str(config_dir), "WEB_AUTH_TOKEN": API_TOKEN, **extra_env}
    with patch.dict(os.environ, env):
        flask_app = create_app(config_dir=str(config_dir))
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def proxy_env(monkeypatch):
    env = {"WEB_AUTH_TOKEN": API_TOKEN, "AUTH_METHOD": "proxy", "AUTH_PROXY_SECRET": SECRET}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTH_PROXY_SECRET_HEADER", raising=False)
    monkeypatch.delenv("AUTH_PROXY_USER_HEADER", raising=False)
    return env


@pytest.fixture
def proxy_app(tmp_path, proxy_env):
    from media_preview_generator.web import settings_manager as sm_mod

    yield _make_app(tmp_path, proxy_env)
    sm_mod.reset_settings_manager()


def _page_token(client, headers: dict) -> str:
    html = client.get("/logs", headers=headers).get_data(as_text=True)
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match, "/logs has no csrf-token meta tag"
    return match.group(1)


class TestAuthMethodParsing:
    def test_proxy_is_recognised(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", " Proxy ")
        assert get_auth_method() == AUTH_METHOD_PROXY


class TestStartupRefusal:
    def test_missing_secret_is_refused(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "proxy")
        monkeypatch.delenv("AUTH_PROXY_SECRET", raising=False)
        with pytest.raises(ProxyAuthConfigError, match="AUTH_PROXY_SECRET"):
            validate_proxy_auth_config()

    def test_short_secret_is_refused(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "proxy")
        monkeypatch.setenv("AUTH_PROXY_SECRET", "x" * 31)
        with pytest.raises(ProxyAuthConfigError, match="32"):
            validate_proxy_auth_config()

    def test_whitespace_does_not_count_towards_the_length(self, monkeypatch):
        monkeypatch.setenv("AUTH_METHOD", "proxy")
        monkeypatch.setenv("AUTH_PROXY_SECRET", "  " + "x" * 30 + "\n")
        with pytest.raises(ProxyAuthConfigError):
            validate_proxy_auth_config()

    def test_other_methods_need_no_secret(self, monkeypatch):
        monkeypatch.delenv("AUTH_PROXY_SECRET", raising=False)
        for method in ("internal", "external"):
            monkeypatch.setenv("AUTH_METHOD", method)
            validate_proxy_auth_config()

    def test_create_app_refuses_to_start(self, tmp_path, proxy_env, monkeypatch):
        monkeypatch.delenv("AUTH_PROXY_SECRET")
        env = {k: v for k, v in proxy_env.items() if k != "AUTH_PROXY_SECRET"}
        with pytest.raises(ProxyAuthConfigError):
            _make_app(tmp_path, env)

    def test_the_secret_is_not_in_the_startup_log(self, tmp_path, proxy_env):
        from loguru import logger

        messages: list[str] = []
        sink_id = logger.add(messages.append, format="{message}", level="DEBUG")
        try:
            _make_app(tmp_path, proxy_env)
        finally:
            logger.remove(sink_id)

        log = "\n".join(str(m) for m in messages)
        assert "AUTH_METHOD=proxy" in log
        assert SECRET not in log


class TestWhoIsSignedIn:
    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="direct-hit"),
            pytest.param({"Remote-User": "alice"}, id="user-header-without-secret"),
            pytest.param({"X-MPG-Proxy-Secret": "wrong" * 10, "Remote-User": "alice"}, id="wrong-secret"),
            pytest.param({"X-MPG-Proxy-Secret": SECRET}, id="secret-without-user"),
            pytest.param({"X-MPG-Proxy-Secret": SECRET, "Remote-User": "   "}, id="blank-user"),
            pytest.param({"X-MPG-Proxy-Secret": SECRET[:-1] + "é", "Remote-User": "alice"}, id="non-ascii-secret"),
        ],
    )
    def test_api_refuses(self, proxy_app, headers):
        resp = proxy_app.test_client().get("/api/jobs", headers=headers)
        assert resp.status_code == 401

    def test_direct_page_hit_gets_the_login_page(self, proxy_app):
        resp = proxy_app.test_client().get("/", headers={"Remote-User": "alice"})
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_secret_and_user_sign_in_api_and_pages(self, proxy_app):
        client = proxy_app.test_client()
        assert client.get("/api/jobs", headers=PROXY_HEADERS).status_code == 200
        assert client.get("/", headers=PROXY_HEADERS).status_code == 200

    def test_auth_status_reports_proxy(self, proxy_app):
        data = proxy_app.test_client().get("/api/auth/status", headers=PROXY_HEADERS).get_json()
        assert data["authenticated"] is True
        assert data["auth_method"] == "proxy"

    def test_proxy_sign_in_does_not_stick_to_the_session(self, proxy_app):
        client = proxy_app.test_client()
        client.get("/", headers=PROXY_HEADERS)
        assert client.get("/api/jobs").status_code == 401

    @pytest.mark.parametrize("method", ["internal", "external"])
    def test_proxy_headers_mean_nothing_in_other_modes(self, proxy_app, monkeypatch, method):
        # A leftover AUTH_PROXY_SECRET must not sign anyone in once AUTH_METHOD is changed away from proxy.
        monkeypatch.setenv("AUTH_METHOD", method)
        from media_preview_generator.web.auth import proxy_user

        with proxy_app.test_request_context("/api/jobs", headers=PROXY_HEADERS):
            assert proxy_user() is None
        if method == "internal":
            assert proxy_app.test_client().get("/api/jobs", headers=PROXY_HEADERS).status_code == 401

    def test_token_login_still_works_on_direct_access(self, proxy_app):
        client = proxy_app.test_client()
        assert client.get("/api/jobs", headers={"X-Auth-Token": API_TOKEN}).status_code == 200

    def test_header_names_can_be_changed(self, tmp_path, proxy_env, monkeypatch):
        from media_preview_generator.web import settings_manager as sm_mod

        renamed = {**proxy_env, "AUTH_PROXY_SECRET_HEADER": "X-Shared", "AUTH_PROXY_USER_HEADER": "X-Forwarded-User"}
        for key, value in renamed.items():
            monkeypatch.setenv(key, value)
        app = _make_app(tmp_path, renamed)
        try:
            client = app.test_client()
            assert client.get("/api/jobs", headers=PROXY_HEADERS).status_code == 401
            ok = client.get("/api/jobs", headers={"X-Shared": SECRET, "X-Forwarded-User": "alice"})
            assert ok.status_code == 200
        finally:
            sm_mod.reset_settings_manager()


class TestCsrfStillApplies:
    def test_proxy_signed_in_post_needs_the_page_token(self, proxy_app):
        # The proxy adds its secret to a forged cross-site request too, so the secret is no CSRF defence.
        client = proxy_app.test_client()

        refused = client.post("/api/processing/pause", headers=PROXY_HEADERS)
        allowed = client.post(
            "/api/processing/pause", headers={**PROXY_HEADERS, "X-CSRFToken": _page_token(client, PROXY_HEADERS)}
        )

        assert refused.status_code == 400
        assert allowed.status_code == 200


class TestWebhooksUnchanged:
    def test_proxy_headers_do_not_open_a_webhook(self, proxy_app):
        resp = proxy_app.test_client().post("/api/webhooks/radarr", json={"eventType": "Test"}, headers=PROXY_HEADERS)
        assert resp.status_code == 401

    def test_webhook_with_its_token_still_runs(self, proxy_app):
        resp = proxy_app.test_client().post(
            "/api/webhooks/radarr", json={"eventType": "Test"}, headers={"X-Auth-Token": API_TOKEN}
        )
        assert resp.status_code == 200


class TestSignOutPage:
    def test_proxy_signed_in_user_is_told_to_sign_out_at_the_proxy(self, proxy_app):
        text = proxy_app.test_client().get("/logout", headers=PROXY_HEADERS).get_data(as_text=True)
        assert "Sign-in is handled by your proxy or VPN" in text

    def test_token_signed_in_user_gets_the_normal_page(self, proxy_app):
        client = proxy_app.test_client()
        with client.session_transaction() as sess:
            sess["authenticated"] = True
        text = client.get("/logout").get_data(as_text=True)
        assert "You'll need your token" in text


NAMESPACES = pytest.mark.parametrize("namespace", ["/jobs", "/logs"])


class TestSocketIO:
    def _connect(self, app, namespace: str, headers: dict):
        from media_preview_generator.web.app import socketio

        return socketio.test_client(app, namespace=namespace, headers=headers, flask_test_client=app.test_client())

    @NAMESPACES
    def test_proxy_signed_in_client_connects(self, proxy_app, namespace):
        sio = self._connect(proxy_app, namespace, {**PROXY_HEADERS, "Sec-Fetch-Site": "same-origin"})
        try:
            assert sio.is_connected(namespace=namespace)
        finally:
            if sio.is_connected(namespace=namespace):
                sio.disconnect(namespace=namespace)

    @NAMESPACES
    def test_user_header_alone_is_rejected(self, proxy_app, namespace):
        sio = self._connect(proxy_app, namespace, {"Remote-User": "alice"})
        assert not sio.is_connected(namespace=namespace)

    @NAMESPACES
    def test_another_site_cannot_connect_even_with_the_proxy_login(self, proxy_app, namespace):
        # The proxy adds its secret to a socket another site's page opens too; the browser's Sec-Fetch-Site gives it away.
        headers = {**PROXY_HEADERS, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
        sio = self._connect(proxy_app, namespace, headers)
        assert not sio.is_connected(namespace=namespace)
