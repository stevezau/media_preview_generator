"""CSRF protection: a browser session's state-changing requests need the page's token; scripts and webhooks don't."""

import json
import os
import re
from unittest.mock import patch

import pytest

API_TOKEN = "csrf-test-token-123"
WEBHOOK_SECRET = "csrf-webhook-secret-456"
REFUSED = "security token"

# One route per mutating method, each with a body the route accepts and the status it answers when it runs.
MUTATING_ROUTES = [
    pytest.param("POST", "/api/processing/pause", None, 200, id="POST"),
    pytest.param("PUT", "/api/settings/log-level", {"log_level": "DEBUG"}, 200, id="PUT"),
    pytest.param("PATCH", "/api/servers/no-such-server/enabled", {"enabled": False}, 404, id="PATCH"),
    pytest.param("DELETE", "/api/webhooks/history", None, 200, id="DELETE"),
]

WEBHOOK_RECEIVERS = {
    "webhooks_bp.radarr_webhook": "/api/webhooks/radarr",
    "webhooks_bp.sonarr_webhook": "/api/webhooks/sonarr",
    "webhooks_bp.sportarr_webhook": "/api/webhooks/sportarr",
    "webhooks_bp.custom_webhook": "/api/webhooks/custom",
    "webhooks_bp.plex_webhook": "/api/webhooks/plex",
    "webhooks_bp.webhook_incoming": "/api/webhooks/incoming",
    "webhooks_bp.webhook_per_server": "/api/webhooks/server/no-such-server",
}
# What each receiver answers a test call once its own auth let it through: the handler's answer, not a refusal.
WEBHOOK_RAN = {
    "webhooks_bp.radarr_webhook": (200, "configured successfully"),
    "webhooks_bp.sonarr_webhook": (200, "configured successfully"),
    "webhooks_bp.sportarr_webhook": (200, "configured successfully"),
    "webhooks_bp.custom_webhook": (200, "configured successfully"),
    "webhooks_bp.plex_webhook": (200, "endpoint reachable"),
    "webhooks_bp.webhook_incoming": (400, "no recognised vendor signature"),
    "webhooks_bp.webhook_per_server": (404, "not configured"),
}


def _make_app(config_dir, settings: dict, extra_env: dict | None = None):
    from media_preview_generator.web import settings_manager as sm_mod
    from media_preview_generator.web.app import create_app

    sm_mod.reset_settings_manager()
    (config_dir / "settings.json").write_text(json.dumps(settings))
    env = {"CONFIG_DIR": str(config_dir), "WEB_AUTH_TOKEN": API_TOKEN, **(extra_env or {})}
    with patch.dict(os.environ, env):
        flask_app = create_app(config_dir=str(config_dir))
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def app(tmp_path):
    from media_preview_generator.web import settings_manager as sm_mod

    with patch.dict(os.environ, {"WEB_AUTH_TOKEN": API_TOKEN}):
        yield _make_app(tmp_path, {"setup_complete": True, "webhook_secret": WEBHOOK_SECRET})
    sm_mod.reset_settings_manager()


@pytest.fixture
def signed_in(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    return client


def _page_token(client, path: str = "/logs") -> str:
    """The token a page of this app hands its scripts (base.html's meta tag)."""
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match, f"{path} has no csrf-token meta tag"
    return match.group(1)


def _send(client, method: str, url: str, body: dict | None, headers: dict | None = None):
    return client.open(url, method=method, json=body, headers=headers or {})


class TestSignedInBrowserSession:
    @pytest.mark.parametrize(("method", "url", "body", "ran_status"), MUTATING_ROUTES)
    def test_refused_when_the_request_has_no_csrf_token(self, signed_in, method, url, body, ran_status):
        resp = _send(signed_in, method, url, body)

        assert resp.status_code == 400
        assert REFUSED in resp.get_json()["error"]

    @pytest.mark.parametrize(("method", "url", "body", "ran_status"), MUTATING_ROUTES)
    def test_runs_when_the_request_sends_the_page_token(self, signed_in, method, url, body, ran_status):
        resp = _send(signed_in, method, url, body, {"X-CSRFToken": _page_token(signed_in)})

        assert resp.status_code == ran_status, resp.get_data(as_text=True)[:200]

    @pytest.mark.parametrize(("method", "url", "body", "ran_status"), MUTATING_ROUTES)
    def test_refused_when_the_token_is_wrong(self, signed_in, method, url, body, ran_status):
        _page_token(signed_in)  # the session has a real token; the request sends another
        resp = _send(signed_in, method, url, body, {"X-CSRFToken": "not-the-token"})

        assert resp.status_code == 400
        assert REFUSED in resp.get_json()["error"]

    def test_token_from_another_session_is_refused(self, app, signed_in):
        other = app.test_client()
        with other.session_transaction() as sess:
            sess["authenticated"] = True

        resp = signed_in.post("/api/processing/pause", headers={"X-CSRFToken": _page_token(other)})

        assert resp.status_code == 400

    def test_refused_request_changes_nothing(self, signed_in):
        from media_preview_generator.web.settings_manager import get_settings_manager

        signed_in.post("/api/processing/pause")

        assert get_settings_manager().processing_paused is False

    def test_page_token_still_works_hours_later(self, signed_in, monkeypatch):
        # A dashboard tab stays open for days; the token must not expire after Flask-WTF's default hour.
        import time

        from itsdangerous.timed import TimestampSigner

        token = _page_token(signed_in)
        later = int(time.time()) + 3 * 3600
        monkeypatch.setattr(TimestampSigner, "get_timestamp", lambda self: later)

        resp = signed_in.post("/api/processing/pause", headers={"X-CSRFToken": token})

        assert resp.status_code == 200

    def test_https_behind_a_proxy_needs_no_matching_referer(self, signed_in):
        # A proxy that doesn't pass the original Host on would otherwise refuse every button.
        token = _page_token(signed_in)

        resp = signed_in.post(
            "/api/processing/pause",
            headers={"X-CSRFToken": token, "X-Forwarded-Proto": "https", "Referer": "https://previews.example/"},
        )

        assert resp.status_code == 200

    @pytest.mark.parametrize("path", ["/", "/logs", "/servers", "/api/system/status", "/api/jobs"])
    def test_get_requests_need_no_token(self, signed_in, path):
        resp = signed_in.get(path)

        assert resp.status_code == 200


class TestApiTokenScripts:
    @pytest.mark.parametrize("header", ["X-Auth-Token", "Authorization"])
    @pytest.mark.parametrize(("method", "url", "body", "ran_status"), MUTATING_ROUTES)
    def test_run_without_a_csrf_token(self, app, header, method, url, body, ran_status):
        value = API_TOKEN if header == "X-Auth-Token" else f"Bearer {API_TOKEN}"

        resp = _send(app.test_client(), method, url, body, {header: value})

        assert resp.status_code == ran_status, resp.get_data(as_text=True)[:200]

    @pytest.mark.parametrize("header", ["X-Auth-Token", "Authorization"])
    def test_wrong_api_token_is_answered_as_unauthenticated(self, app, header):
        value = "wrong-token" if header == "X-Auth-Token" else "Bearer wrong-token"

        resp = app.test_client().post("/api/processing/pause", headers={header: value})

        assert resp.status_code == 401
        assert resp.get_json() == {"error": "Authentication required"}

    def test_valid_api_token_runs_even_alongside_a_session(self, signed_in):
        resp = signed_in.post("/api/processing/pause", headers={"X-Auth-Token": API_TOKEN})

        assert resp.status_code == 200


class TestWebhookReceivers:
    def test_exactly_the_webhook_receivers_are_exempt(self, app):
        exempt = {
            endpoint for endpoint, view in app.view_functions.items() if getattr(view, "is_webhook_receiver", False)
        }

        assert exempt == set(WEBHOOK_RECEIVERS)

    def test_receiver_runs_whatever_sec_fetch_site_says(self, app):
        resp = app.test_client().post(
            "/api/webhooks/radarr",
            json={"eventType": "Test"},
            headers={"X-Auth-Token": WEBHOOK_SECRET, "Sec-Fetch-Site": "cross-site"},
        )

        assert resp.status_code == 200 and "configured successfully" in resp.get_data(as_text=True)

    @pytest.mark.parametrize(("endpoint", "url"), WEBHOOK_RECEIVERS.items())
    def test_receiver_runs_without_a_csrf_token(self, app, endpoint, url):
        # The webhook secret isn't the API token, so this request isn't let through as a script.
        client = app.test_client()
        if endpoint == "webhooks_bp.plex_webhook":
            resp = client.post(
                url,
                data={"payload": json.dumps({"event": "test.ping"})},
                headers={"X-Auth-Token": WEBHOOK_SECRET},
            )
        else:
            resp = client.post(url, json={"eventType": "Test"}, headers={"X-Auth-Token": WEBHOOK_SECRET})

        status, text = WEBHOOK_RAN[endpoint]
        body = resp.get_data(as_text=True)
        assert resp.status_code == status and text in body, (resp.status_code, body[:200])

    @pytest.mark.parametrize(("endpoint", "url"), WEBHOOK_RECEIVERS.items())
    def test_receiver_still_refuses_a_call_without_its_secret(self, app, endpoint, url):
        resp = app.test_client().post(url, json={"eventType": "Test"})

        assert resp.status_code == 401


def _form_token(client) -> str:
    html = client.get("/login").get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


class TestSignInStartsAFreshSession:
    """Another app on this host can plant a session cookie whose token it read from /login (session fixation)."""

    def test_a_token_from_before_the_form_sign_in_is_refused_after_it(self, app):
        client = app.test_client()
        planted = _form_token(client)
        client.post("/login", data={"token": API_TOKEN, "csrf_token": planted})

        stale = client.post("/api/processing/pause", headers={"X-CSRFToken": planted})
        fresh = client.post("/api/processing/pause", headers={"X-CSRFToken": _page_token(client)})

        assert stale.status_code == 400
        assert fresh.status_code == 200

    def test_a_token_from_before_the_api_sign_in_is_refused_after_it(self, app):
        client = app.test_client()
        planted = _form_token(client)
        client.post("/api/auth/login", json={"token": API_TOKEN}, headers={"X-CSRFToken": planted})

        stale = client.post("/api/processing/pause", headers={"X-CSRFToken": planted})

        assert stale.status_code == 400
        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True

    def test_a_failed_sign_in_keeps_the_pages_token(self, app):
        # Nothing was signed in, so the form on the page stays usable for the next try.
        client = app.test_client()
        token = _form_token(client)
        client.post("/login", data={"token": "wrong-token", "csrf_token": token})

        resp = client.post("/login", data={"token": API_TOKEN, "csrf_token": token})

        assert resp.status_code == 302


class TestSecFetchSite:
    """Browsers say where a request came from; a signed-in request from another origin is refused even with a token."""

    @pytest.mark.parametrize(
        ("fetch_site", "status"),
        [
            ("cross-site", 400),
            ("same-site", 400),  # another app on this host: a different port is the same site
            ("same-origin", 200),
            ("none", 200),  # typed or bookmarked
            (None, 200),  # older browsers and scripts send no such header
        ],
    )
    def test_signed_in_request_by_origin(self, signed_in, fetch_site, status):
        headers = {"X-CSRFToken": _page_token(signed_in)}
        if fetch_site is not None:
            headers["Sec-Fetch-Site"] = fetch_site

        resp = signed_in.post("/api/processing/pause", headers=headers)

        assert resp.status_code == status, resp.get_data(as_text=True)[:200]
        if status == 400:
            assert REFUSED in resp.get_json()["error"]

    @pytest.mark.parametrize("header", ["X-Auth-Token", "Authorization"])
    def test_api_token_requests_are_unaffected(self, app, header):
        value = API_TOKEN if header == "X-Auth-Token" else f"Bearer {API_TOKEN}"

        resp = app.test_client().post("/api/processing/pause", headers={header: value, "Sec-Fetch-Site": "cross-site"})

        assert resp.status_code == 200

    def test_sign_in_from_another_origin_is_refused(self, app):
        client = app.test_client()
        token = _form_token(client)

        resp = client.post(
            "/login", data={"token": API_TOKEN, "csrf_token": token}, headers={"Sec-Fetch-Site": "same-site"}
        )

        assert resp.status_code == 400
        with client.session_transaction() as sess:
            assert not sess.get("authenticated")


class TestLogout:
    def test_the_nav_button_signs_out(self, signed_in):
        resp = signed_in.post("/logout", data={"csrf_token": _page_token(signed_in, "/")})

        assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
        with signed_in.session_transaction() as sess:
            assert not sess.get("authenticated")

    def test_the_nav_carries_a_logout_form_with_the_page_token(self, signed_in):
        html = signed_in.get("/").get_data(as_text=True)
        form = re.search(r'<form method="post" action="/logout"[^>]*>(.*?)</form>', html, re.S)

        assert form, "the nav has no Logout form"
        assert 'name="csrf_token"' in form.group(1) and 'id="navLogoutBtn"' in form.group(1)

    @pytest.mark.parametrize("headers", [{}, {"Sec-Fetch-Site": "same-site"}], ids=["no-token", "other-origin"])
    def test_a_forged_sign_out_leaves_the_user_signed_in(self, signed_in, headers):
        if headers:
            headers = {**headers, "X-CSRFToken": _page_token(signed_in)}

        resp = signed_in.post("/logout", headers=headers)

        # The confirm page, with a fresh token, rather than a JSON error in the browser.
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/logout")
        with signed_in.session_transaction() as sess:
            assert sess.get("authenticated") is True

    def test_a_get_only_asks(self, signed_in):
        resp = signed_in.get("/logout")

        assert resp.status_code == 200
        assert "Sign out?" in resp.get_data(as_text=True)
        assert "You'll need your token to sign in again." in resp.get_data(as_text=True)
        with signed_in.session_transaction() as sess:
            assert sess.get("authenticated") is True


class TestLoginForm:
    def _form_token(self, client) -> str:
        return _form_token(client)

    def test_signs_in_with_the_form_token(self, app):
        client = app.test_client()

        resp = client.post("/login", data={"token": API_TOKEN, "csrf_token": self._form_token(client)})

        assert resp.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("authenticated") is True

    def test_without_the_form_token_shows_the_page_expired_and_stays_signed_out(self, app):
        client = app.test_client()

        resp = client.post("/login", data={"token": API_TOKEN})

        assert resp.status_code == 400
        assert "This sign-in page expired." in resp.get_data(as_text=True)
        with client.session_transaction() as sess:
            assert not sess.get("authenticated")


class TestSetupWizard:
    @pytest.fixture
    def fresh_app(self, tmp_path):
        from media_preview_generator.web import settings_manager as sm_mod

        with patch.dict(os.environ, {"WEB_AUTH_TOKEN": API_TOKEN}):
            yield _make_app(tmp_path, {})
        sm_mod.reset_settings_manager()

    def test_wizard_step_saves_with_the_page_token(self, fresh_app):
        client = fresh_app.test_client()
        token = _page_token(client, "/setup")

        resp = client.post("/api/setup/state", json={"current_step": 2}, headers={"X-CSRFToken": token})

        assert resp.status_code == 200

    def test_another_site_cannot_drive_the_wizard_before_setup(self, fresh_app):
        # Before setup the wizard's routes need no sign-in, so the CSRF token is the only thing stopping a page on
        # another site from choosing this app's token for the user.
        resp = fresh_app.test_client().post(
            "/api/setup/set-token", json={"token": "attacker-token", "confirm_token": "attacker-token"}
        )

        assert resp.status_code == 400
        assert REFUSED in resp.get_json()["error"]


class TestExternalAuth:
    @pytest.fixture
    def external_app(self, tmp_path):
        from media_preview_generator.web import settings_manager as sm_mod

        env = {"WEB_AUTH_TOKEN": API_TOKEN, "AUTH_METHOD": "external"}
        with patch.dict(os.environ, env):
            yield _make_app(tmp_path, {"setup_complete": True}, env)
        sm_mod.reset_settings_manager()

    def test_the_sign_out_page_says_to_sign_out_at_the_proxy(self, external_app):
        text = external_app.test_client().get("/logout").get_data(as_text=True)

        assert "Sign-in is handled by your proxy or VPN" in text
        assert "You'll need your token" not in text

    def test_browser_request_still_needs_the_page_token(self, external_app):
        # The proxy's own sign-in cookie rides along on a forged request just like ours would.
        client = external_app.test_client()

        refused = client.post("/api/processing/pause")
        allowed = client.post("/api/processing/pause", headers={"X-CSRFToken": _page_token(client)})

        assert refused.status_code == 400
        assert allowed.status_code == 200
