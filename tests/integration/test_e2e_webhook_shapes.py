"""Integration tests covering real webhook payload shapes.

Fires representative payloads from each source through the universal
``/api/webhooks/incoming`` endpoint and verifies each one is classified
and queued as a Job for the right file. The Job itself is not run here
(the full-pipeline tests cover publishing).

Sources covered:
* Sonarr ``Download`` event (episodeFile.path nested in series envelope)
* Radarr ``Download`` event (movieFile.path nested in movie envelope)
* Plex multipart envelope for a server that isn't configured (ignored)
* Generic ``{"path": "..."}`` payload
* Concurrent duplicate webhooks (de-duplicated into one Job)
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.integration.conftest import assert_webhook_queued


@pytest.fixture
def webhook_app(emby_credentials, media_root, tmp_path, monkeypatch, live_config):
    """Live Flask app wired to the live Emby container, ready for webhook tests."""
    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.settings_manager import (
        get_settings_manager,
        reset_settings_manager,
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    reset_settings_manager()
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "integration-test-token")

    app = create_app(config_dir=str(config_dir))
    app.config["TESTING"] = True

    settings = get_settings_manager()
    settings.set("webhook_secret", "integration-secret")
    settings.set(
        "media_servers",
        [
            {
                "id": "emby-int-1",
                "type": "emby",
                "name": "Test Emby",
                "enabled": True,
                "url": emby_credentials["EMBY_URL"],
                "auth": {
                    "method": "password",
                    "access_token": emby_credentials["EMBY_ACCESS_TOKEN"],
                    "user_id": emby_credentials["EMBY_USER_ID"],
                },
                "server_identity": emby_credentials["EMBY_SERVER_ID"],
                "libraries": [
                    {
                        "id": "movies",
                        "name": "Movies",
                        "remote_paths": ["/em-media/Movies"],
                        "enabled": True,
                    }
                ],
                "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
                "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
            }
        ],
    )
    settings.complete_setup()

    return app


def _post(app, payload, headers=None, content_type="application/json"):
    """Helper: post payload to /api/webhooks/incoming."""
    base_headers = {"X-Auth-Token": "integration-secret"}
    if headers:
        base_headers.update(headers)
    if content_type == "application/json":
        base_headers["Content-Type"] = content_type
        return app.test_client().post(
            "/api/webhooks/incoming",
            headers=base_headers,
            data=json.dumps(payload),
        )
    return app.test_client().post(
        "/api/webhooks/incoming",
        headers=base_headers,
        data=payload,
        content_type=content_type,
    )


@pytest.mark.integration
class TestSonarrWebhook:
    def test_sonarr_download_event_dispatches(self, webhook_app, media_root):
        """Sonarr's Download event has episodeFile.path nested in the series envelope."""
        canonical = str(media_root / "TV Shows" / "Test Show" / "Season 01" / "Test Show - S01E01 - Pilot.mkv")
        sonarr_payload = {
            "eventType": "Download",
            "instanceName": "Sonarr",
            "applicationUrl": "http://sonarr:8989",
            "series": {
                "id": 1,
                "title": "Test Show",
                "path": str(media_root / "TV Shows" / "Test Show"),
            },
            "episodes": [
                {
                    "id": 1,
                    "episodeNumber": 1,
                    "seasonNumber": 1,
                    "title": "Pilot",
                }
            ],
            "episodeFile": {
                "id": 1,
                "relativePath": "Season 01/Test Show - S01E01 - Pilot.mkv",
                "path": canonical,
            },
            "release": {"releaseTitle": "Test.Show.S01E01.Pilot.x264"},
        }
        assert_webhook_queued(_post(webhook_app, sonarr_payload), "sonarr", canonical)


@pytest.mark.integration
class TestRadarrWebhook:
    def test_radarr_download_event_dispatches(self, webhook_app, media_root):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        radarr_payload = {
            "eventType": "Download",
            "instanceName": "Radarr",
            "applicationUrl": "http://radarr:7878",
            "movie": {
                "id": 1,
                "title": "Test Movie H264",
                "year": 2024,
                "folderPath": str(media_root / "Movies" / "Test Movie H264 (2024)"),
            },
            "movieFile": {
                "id": 1,
                "relativePath": "Test Movie H264 (2024).mkv",
                "path": canonical,
            },
            "release": {"releaseTitle": "Test.Movie.H264.2024.1080p.x264"},
        }
        assert_webhook_queued(_post(webhook_app, radarr_payload), "radarr", canonical)


@pytest.mark.integration
class TestGenericPathWebhook:
    def test_path_only_payload_dispatches(self, webhook_app, media_root):
        """The simplest custom webhook shape: ``{"path": ...}``."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        response = _post(webhook_app, {"path": canonical, "trigger": "manual"})
        assert_webhook_queued(response, "path", canonical)

    def test_unknown_payload_returns_400(self, webhook_app):
        """Random JSON should be rejected, not dispatched."""
        response = _post(webhook_app, {"random": "noise", "no_path": True})
        assert response.status_code == 400


@pytest.mark.integration
class TestPlexWebhookMultipart:
    def test_plex_event_for_unconfigured_server_is_ignored(self, webhook_app):
        """A Plex multipart payload is parsed and classified as plex, then ignored.

        The configured registry has no Plex server, so the payload can't be
        matched to one and nothing is queued.
        """
        plex_payload = {
            "event": "media.play",
            "user": True,
            "owner": True,
            "Account": {"id": 1, "title": "test"},
            "Server": {"title": "Some Plex", "uuid": "some-other-uuid"},
            "Player": {"title": "Browser"},
            "Metadata": {"ratingKey": "999", "type": "movie", "title": "X"},
        }
        response = _post(
            webhook_app,
            {"payload": json.dumps(plex_payload)},
            content_type="multipart/form-data",
        )
        assert response.status_code == 202, response.get_data(as_text=True)
        body = response.get_json()
        assert body["status"] == "ignored", body
        assert body["kind"] == "plex", body


@pytest.mark.integration
class TestConcurrentWebhookCoalescing:
    def test_five_concurrent_webhooks_queue_one_job(self, webhook_app, media_root):
        """Five simultaneous webhooks for one file create a single Job; the rest are de-duplicated."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        client = webhook_app.test_client()

        def _fire(_):
            return client.post(
                "/api/webhooks/incoming",
                headers={
                    "X-Auth-Token": "integration-secret",
                    "Content-Type": "application/json",
                },
                data=json.dumps({"path": canonical, "trigger": "concurrent"}),
            )

        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(pool.map(_fire, range(5)))

        assert [r.status_code for r in responses] == [202] * 5
        statuses = sorted(r.get_json()["status"] for r in responses)
        assert statuses == ["ignored_duplicate"] * 4 + ["queued"], statuses
