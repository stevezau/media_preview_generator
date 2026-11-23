"""Tests for /api/settings/{emby,jellyfin}_webhook/info routes.

Pre-fix the per-server Edit modal had a "Webhook & Scanner" tab for
Plex only. Emby and Jellyfin users had no place to find the webhook
URL their plugin should POST to.

These routes return the per-server webhook URL + plugin install
instructions for Emby and Jellyfin. The webhook secret is never
returned; the UI renders a [YOUR WEBHOOK SECRET] placeholder and points
the user at Automation → Triggers → Settings, where it is set (#432:
the old text pointed at Settings → Authentication, which never shows
the sign-in token, so users pasted the wrong value).

A companion ``/test`` endpoint that loopback-POSTed synthetic payloads
was removed — it only proved auth + route worked locally, which is
trivially true when the user is already authenticated to the UI. It
did NOT test reachability from the external media server. See
``TestVendorWebhookTestEndpointRemoved`` for the regression guard.

Matrix coverage per .claude/rules/testing.md:
  * vendor (emby / jellyfin)
  * info request shape (per-server URL, plugin info, token-not-leaked)
  * security (server_id required, type-mismatched server rejected)
  * test endpoint removal (404 for both vendors)
"""

from __future__ import annotations

import pytest

from media_preview_generator.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client with a configured Emby + Jellyfin server."""
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-123")
    app = create_app(config_dir=str(tmp_path))
    app.config["TESTING"] = True

    # Seed two servers so info requests have something to resolve.
    from media_preview_generator.web.settings_manager import get_settings_manager

    sm = get_settings_manager()
    sm.set("webhook_secret", "the-webhook-secret-456")
    sm.set(
        "media_servers",
        [
            {
                "id": "emby-1",
                "type": "emby",
                "name": "Test Emby",
                "enabled": True,
                "url": "http://emby:8096",
                "auth": {"method": "api_key", "api_key": "k"},
            },
            {
                "id": "jelly-1",
                "type": "jellyfin",
                "name": "Test Jellyfin",
                "enabled": True,
                "url": "http://jelly:8096",
                "auth": {"method": "api_key", "api_key": "k"},
            },
            {
                "id": "plex-1",
                "type": "plex",
                "name": "Test Plex",
                "enabled": True,
                "url": "http://plex:32400",
                "auth": {"token": "t"},
            },
        ],
    )

    with app.test_client() as c:
        yield c


def _hdrs():
    return {"X-Auth-Token": "test-token-123"}


class TestVendorWebhookInfo:
    @pytest.mark.parametrize(
        "vendor,server_id,expected_plugin_substring",
        [
            ("emby", "emby-1", "Emby Webhooks"),
            ("jellyfin", "jelly-1", "Jellyfin Webhook"),
        ],
    )
    def test_info_returns_url_plus_plugin_instructions(self, client, vendor, server_id, expected_plugin_substring):
        resp = client.get(f"/api/settings/{vendor}_webhook/info?server_id={server_id}", headers=_hdrs())
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["vendor"] == vendor
        assert data["server_id"] == server_id
        # Per-server URL pins dispatch to this server's id so multi-server
        # installs (two Jellyfins) don't rely on payload-ServerId matching
        # the probed server_identity. webhook_url is the per-server form.
        assert data["webhook_url"].endswith(f"/api/webhooks/server/{server_id}"), (
            f"webhook_url should be the per-server pinned endpoint; got {data['webhook_url']!r}"
        )
        assert data["webhook_url_per_server"] == data["webhook_url"]
        # Header name is fine to expose (it's a constant). Value MUST NOT
        # be exposed: it would land in JS state, screen-shares, and any
        # screenshot of the modal. The user copies the secret from
        # Automation → Triggers → Settings. Keep this assertion as a tripwire —
        # any future field that smuggles the secret back in will fail it.
        assert data["auth_header_name"] == "X-Auth-Token"
        assert data["auth_token_placeholder"] == "[YOUR WEBHOOK SECRET]", (
            "UI renders the placeholder in place of the real secret so the value never appears on screen"
        )
        assert data["webhook_secret_set"] is True, "webhook_secret_set is a boolean — never the secret itself"
        assert "auth_header_value" not in data, "auth_header_value MUST NOT be returned (token would leak via JS state)"
        assert "webhook_url_with_token" not in data, (
            "a ready-made ?token= URL would put the secret on screen; the UI only names the ?token= form"
        )
        # Hard fail if the actual token value appears anywhere in the
        # JSON response. Defensive: catches any future field whose
        # value collides with the configured token.
        import json as _json

        body_text = _json.dumps(data)
        assert "test-token-123" not in body_text, (
            "The actual auth token must NOT appear anywhere in /info; only a placeholder + header name are surfaced"
        )
        assert "the-webhook-secret-456" not in body_text, "The webhook secret must NOT appear anywhere in /info"

        assert expected_plugin_substring in data["plugin"]["plugin_name"], (
            f"plugin info should reference {expected_plugin_substring}"
        )
        assert isinstance(data["plugin"]["config_steps"], list)
        assert len(data["plugin"]["config_steps"]) >= 2, (
            "Plugin instructions should be a multi-step walk-through, not a single line."
        )
        assert any("X-Auth-Token" in step for step in data["plugin"]["config_steps"]), (
            "Plugin steps MUST instruct the user to add the X-Auth-Token header"
        )
        assert any(
            "[YOUR WEBHOOK SECRET]" in step and "Automation → Triggers → Settings" in step
            for step in data["plugin"]["config_steps"]
        ), (
            "Plugin steps must name the webhook secret and where to find it — Settings → Authentication "
            "never displays the sign-in token, so pointing there left users with no copyable value (#432)"
        )
        assert not any("Settings → Authentication" in step for step in data["plugin"]["config_steps"])
        # Auto-register is NOT yet supported (deferred follow-up).
        # Surface it in the response so the UI doesn't render a dead button.
        assert data["auto_register_supported"] is False

    def test_info_reports_webhook_secret_unset_when_empty(self, client):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("webhook_secret", "")
        resp = client.get("/api/settings/emby_webhook/info?server_id=emby-1", headers=_hdrs())
        assert resp.status_code == 200
        assert resp.get_json()["webhook_secret_set"] is False

    def test_info_requires_server_id(self, client):
        resp = client.get("/api/settings/emby_webhook/info", headers=_hdrs())
        assert resp.status_code == 400
        assert "server_id" in resp.get_json()["error"]

    def test_info_rejects_unknown_server_id(self, client):
        resp = client.get("/api/settings/emby_webhook/info?server_id=does-not-exist", headers=_hdrs())
        assert resp.status_code == 404

    def test_info_rejects_type_mismatched_server(self, client):
        # Asking about Plex via the Emby endpoint is a programming error;
        # surface 400 so the UI can report it instead of silently returning
        # Emby plugin info for a Plex server.
        resp = client.get("/api/settings/emby_webhook/info?server_id=plex-1", headers=_hdrs())
        assert resp.status_code == 400
        assert "expected" in resp.get_json()["error"]


class TestVendorWebhookTestEndpointRemoved:
    """The ``/api/settings/{vendor}_webhook/test`` endpoint was removed.

    It only verified loopback + auth token round-trip — both trivially
    true when the user is already authenticated to the UI. It did NOT
    test whether the external media server could reach this app, which
    is the only thing worth knowing. The button was misleading: users
    saw "Round-trip OK" and assumed real webhooks would work, then
    spent hours debugging why they didn't.

    This test class is a regression guard so the endpoint stays gone.
    """

    def test_emby_test_endpoint_404s(self, client):
        resp = client.post("/api/settings/emby_webhook/test?server_id=emby-1", headers=_hdrs())
        assert resp.status_code == 404, (
            "The Emby self-test endpoint was removed because it only proved loopback worked. "
            "Re-introducing it would re-introduce the 'webhook test passes but real events fail' "
            "support pattern."
        )

    def test_jellyfin_test_endpoint_404s(self, client):
        resp = client.post("/api/settings/jellyfin_webhook/test?server_id=jelly-1", headers=_hdrs())
        assert resp.status_code == 404


class TestWebhookContactStatus:
    """The info route reports the latest request each server's own webhook URL received (#447).

    Emby and Jellyfin users had no way to tell whether their webhook reached the app: nothing showed up until
    a real new item arrived, Emby's Test button left no trace, and a wrong secret only reached the log.
    """

    @pytest.fixture(autouse=True)
    def _fresh_contact(self):
        from media_preview_generator.web import webhooks

        webhooks._server_contact.clear()
        yield
        webhooks._server_contact.clear()

    @staticmethod
    def _contact(client, vendor: str, server_id: str) -> dict:
        resp = client.get(f"/api/settings/{vendor}_webhook/info?server_id={server_id}", headers=_hdrs())
        assert resp.status_code == 200
        return resp.get_json()["contact"]

    _EMBY_TEST = {"Title": "Test Notification", "Event": "system.webhooktest", "Server": {"Name": "Emby", "Id": "x"}}

    def test_nothing_received_yet(self, client):
        assert self._contact(client, "emby", "emby-1") == {"accepted": None, "rejected": None}

    def test_emby_test_button_counts_as_working_and_lands_in_history(self, client):
        from media_preview_generator.web import webhooks

        resp = client.post(
            "/api/webhooks/server/emby-1", json=self._EMBY_TEST, headers={"X-Auth-Token": "the-webhook-secret-456"}
        )
        assert resp.status_code == 202

        contact = self._contact(client, "emby", "emby-1")
        assert contact["accepted"]["detail"] == "test ping"
        assert contact["rejected"] is None
        latest = list(webhooks._webhook_history)[-1]
        assert (latest["event_type"], latest["status"], latest["server_id"]) == ("Test", "test", "emby-1")

    def test_secret_in_url_is_accepted(self, client):
        # Emby before 4.9 has no header option; the card tells those users to append ?token=.
        resp = client.post("/api/webhooks/server/emby-1?token=the-webhook-secret-456", json=self._EMBY_TEST)
        assert resp.status_code == 202
        assert self._contact(client, "emby", "emby-1")["accepted"]["detail"] == "test ping"

    def test_any_event_proves_the_connection(self, client):
        payload = {"NotificationType": "PlaybackStart", "ItemId": "abc", "ServerId": "s"}
        client.post("/api/webhooks/server/jelly-1", json=payload, headers={"X-Auth-Token": "the-webhook-secret-456"})
        assert self._contact(client, "jellyfin", "jelly-1")["accepted"]["detail"] == "PlaybackStart"

    @pytest.mark.parametrize(
        "headers,reason",
        [
            ({"X-Auth-Token": "wrong"}, "the webhook secret didn't match"),
            ({}, "no webhook secret was sent"),
        ],
    )
    def test_rejected_request_says_why(self, client, headers, reason):
        resp = client.post("/api/webhooks/server/emby-1", json=self._EMBY_TEST, headers=headers)
        assert resp.status_code == 401
        contact = self._contact(client, "emby", "emby-1")
        assert contact["rejected"]["detail"] == reason
        assert contact["accepted"] is None

    def test_unreadable_body_is_reported(self, client):
        resp = client.post(
            "/api/webhooks/server/emby-1",
            data="not json",
            content_type="text/plain",
            headers={"X-Auth-Token": "the-webhook-secret-456"},
        )
        assert resp.status_code == 400
        assert self._contact(client, "emby", "emby-1")["rejected"]["detail"].startswith("the request couldn't be read")

    def test_unknown_server_id_records_nothing(self, client):
        from media_preview_generator.web import webhooks

        client.post("/api/webhooks/server/not-a-server", json=self._EMBY_TEST, headers={"X-Auth-Token": "wrong"})
        assert webhooks._server_contact == {}, "requests to ids that aren't configured servers must not add entries"

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(_EMBY_TEST, id="emby-test-button"),
            pytest.param({"eventType": "Test"}, id="sonarr-style-test"),
        ],
    )
    def test_test_ping_is_recorded_without_creating_a_job(self, client, payload, monkeypatch):
        from media_preview_generator.web import webhook_router, webhooks

        def _must_not_resolve(**_kwargs):
            raise AssertionError("a test ping must not be resolved into a job")

        monkeypatch.setattr(webhook_router, "_resolve_to_canonical_paths", _must_not_resolve)
        resp = client.post(
            "/api/webhooks/server/emby-1", json=payload, headers={"X-Auth-Token": "the-webhook-secret-456"}
        )
        assert resp.status_code == 202
        assert self._contact(client, "emby", "emby-1")["accepted"]["detail"] == "test ping"
        latest = list(webhooks._webhook_history)[-1]
        assert (latest["source"], latest["server_type"], latest["status"]) == ("emby", "emby", "test")

    def test_ignored_request_still_shows_it_arrived_and_why(self, client):
        from media_preview_generator.web.settings_manager import get_settings_manager

        get_settings_manager().set("webhook_enabled", False)
        client.post(
            "/api/webhooks/server/emby-1", json=self._EMBY_TEST, headers={"X-Auth-Token": "the-webhook-secret-456"}
        )
        detail = self._contact(client, "emby", "emby-1")["accepted"]["detail"]
        assert detail == "test ping, ignored: webhook processing is turned off"

    def test_disabled_server_still_shows_it_arrived_and_why(self, client):
        from media_preview_generator.web.settings_manager import get_settings_manager

        sm = get_settings_manager()
        sm.set(
            "media_servers", [{**s, "enabled": False} if s["id"] == "emby-1" else s for s in sm.get("media_servers")]
        )
        client.post(
            "/api/webhooks/server/emby-1", json=self._EMBY_TEST, headers={"X-Auth-Token": "the-webhook-secret-456"}
        )
        detail = self._contact(client, "emby", "emby-1")["accepted"]["detail"]
        assert detail == "test ping, ignored: this server is turned off"

    def test_contact_is_per_server(self, client):
        client.post(
            "/api/webhooks/server/emby-1", json=self._EMBY_TEST, headers={"X-Auth-Token": "the-webhook-secret-456"}
        )
        assert self._contact(client, "jellyfin", "jelly-1") == {"accepted": None, "rejected": None}
