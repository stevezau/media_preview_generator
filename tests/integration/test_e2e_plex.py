"""End-to-end integration tests against a live Plex Media Server container.

Verifies the multi-server stack against a real Plex (started by
``docker-compose.test.yml``):

* :class:`PlexServer` connects, lists libraries, lists items.
* The bundle-hash lookup against ``/library/metadata/{id}/tree`` works
  for a real Plex item.
* :class:`PlexBundleAdapter` writes a real, structurally valid BIF at
  the per-item bundle path.
* The Plex native multipart webhook payload routes through the
  universal endpoint correctly.
* Identity matching via the captured ``server_identity`` works against
  the live Plex's ``machineIdentifier``.

Run with::

    pytest -m integration --no-cov tests/integration/test_e2e_plex.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests as _requests

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry
from tests.integration.conftest import assert_webhook_queued, decode_bif


@pytest.fixture
def plex_legacy_config(live_config, plex_credentials, tmp_path):
    live_config.plex_url = plex_credentials["PLEX_URL"]
    live_config.plex_token = plex_credentials["PLEX_ACCESS_TOKEN"]
    live_config.plex_libraries = ["Movies"]
    live_config.plex_config_folder = str(tmp_path / "plex_config")
    Path(live_config.plex_config_folder).mkdir(parents=True, exist_ok=True)
    return live_config


@pytest.fixture
def plex_registry(plex_credentials, plex_legacy_config, media_root):
    """Registry with the live Plex container as the only entry."""
    raw_servers = [
        {
            "id": "plex-int-1",
            "type": "plex",
            "name": "Test Plex",
            "enabled": True,
            "url": plex_credentials["PLEX_URL"],
            "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
            "server_identity": plex_credentials["PLEX_SERVER_ID"],
            "libraries": [
                {
                    "id": "1",
                    "name": "Movies",
                    "remote_paths": ["/media/Movies"],
                    "enabled": True,
                }
            ],
            "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
            "output": {
                "adapter": "plex_bundle",
                "plex_config_folder": str(plex_legacy_config.plex_config_folder),
                "frame_interval": 5,
            },
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=plex_legacy_config)


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestLivePlexConnection:
    def test_connects_and_identifies(self, plex_registry, plex_credentials):
        server = plex_registry.get("plex-int-1")
        result = server.test_connection()
        assert result.ok, result.message
        # /identity gives back the same machineIdentifier we captured at setup.
        assert result.server_id == plex_credentials["PLEX_SERVER_ID"]

    def test_list_libraries_returns_movies(self, plex_registry):
        server = plex_registry.get("plex-int-1")
        libraries = server.list_libraries()
        names = [lib.name for lib in libraries]
        assert "Movies" in names, names

    def test_list_items_returns_test_movies(self, plex_registry):
        server = plex_registry.get("plex-int-1")
        libraries = server.list_libraries()
        movies = next(lib for lib in libraries if lib.name == "Movies")
        items = list(server.list_items(movies.id))
        assert len(items) >= 1, [i.title for i in items]


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.real_plex_server
class TestLivePlexBundleBif:
    """Real FFmpeg → real Plex bundle BIF at the hash-keyed bundle path."""

    def test_bundle_bif_lands_at_per_item_bundle_path(self, plex_registry, plex_legacy_config, media_root):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")

        result = process_canonical_path(
            canonical_path=canonical,
            registry=plex_registry,
            config=plex_legacy_config,
            gpu=None,
            gpu_device_path=None,
        )

        assert result.status is MultiServerStatus.PUBLISHED, result.message
        published = next(p for p in result.publishers if p.status is PublisherStatus.PUBLISHED)
        assert published.adapter_name == "plex_bundle"
        # Plex bundle BIF lives at:
        #   {plex_config_folder}/Media/localhost/{h0}/{h[1:]}.bundle/Contents/Indexes/index-sd.bif
        bif_path = published.output_paths[0]
        assert bif_path.name == "index-sd.bif"
        assert "Indexes" in str(bif_path)
        assert ".bundle" in str(bif_path)
        # And the bytes are a real BIF.
        try:
            decoded = decode_bif(bif_path)
            assert decoded["interval_ms"] == 5000
            assert decoded["image_count"] >= 4
        finally:
            # Clean up so re-runs start fresh.
            if bif_path.exists():
                bif_path.unlink()


@pytest.mark.integration
@pytest.mark.real_plex_server
class TestPlexNativeMultipartWebhook:
    """Plex's webhook is multipart form data with a JSON ``payload`` field.

    Verify the universal /api/webhooks/incoming endpoint correctly
    classifies it, extracts Server.uuid, and routes via server_identity.
    """

    def test_multipart_payload_classified_as_plex(
        self,
        plex_credentials,
        media_root,
        tmp_path,
        monkeypatch,
        plex_legacy_config,
    ):
        """A real Plex-shape multipart webhook resolves to the file and is queued as a Job."""
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
                    "id": "plex-int-1",
                    "type": "plex",
                    "name": "Test Plex",
                    "enabled": True,
                    "url": plex_credentials["PLEX_URL"],
                    "auth": {"method": "token", "token": plex_credentials["PLEX_ACCESS_TOKEN"]},
                    "server_identity": plex_credentials["PLEX_SERVER_ID"],
                    "libraries": [
                        {
                            "id": "1",
                            "name": "Movies",
                            "remote_paths": ["/media/Movies"],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [{"remote_prefix": "/media", "local_prefix": str(media_root)}],
                    "output": {
                        "adapter": "plex_bundle",
                        "plex_config_folder": str(plex_legacy_config.plex_config_folder),
                        "frame_interval": 5,
                    },
                }
            ],
        )
        settings.complete_setup()

        # Patch load_config (used by _get_registry to build the legacy Plex
        # client) to point at our test Plex container, otherwise .env
        # leakage takes us to a real Plex on the dev machine.
        monkeypatch.setattr(
            "media_preview_generator.config.load_config",
            lambda *a, **kw: plex_legacy_config,
        )

        # Look up the real ratingKey (= Plex's item id) for the H264 fixture.
        sections_resp = _requests.get(
            f"{plex_credentials['PLEX_URL']}/library/sections/1/all",
            headers={"X-Plex-Token": plex_credentials["PLEX_ACCESS_TOKEN"], "Accept": "application/json"},
            timeout=10,
        )
        sections_resp.raise_for_status()
        videos = sections_resp.json()["MediaContainer"]["Metadata"]
        h264_item = next(v for v in videos if "H264" in v["Media"][0]["Part"][0]["file"])
        rating_key = str(h264_item["ratingKey"])

        # Build the Plex multipart payload — payload field is JSON.
        plex_payload = {
            "event": "library.new",
            "user": False,
            "owner": True,
            "Account": {"id": 1, "title": "test"},
            "Server": {
                "title": "Test Plex",
                "uuid": plex_credentials["PLEX_SERVER_ID"],
            },
            "Player": {},
            "Metadata": {
                "ratingKey": rating_key,
                "title": "Test Movie",
                "type": "movie",
                "librarySectionID": 1,
            },
        }

        client = app.test_client()
        # The Plex webhook is multipart/form-data with a "payload" field.
        response = client.post(
            "/api/webhooks/incoming",
            headers={"X-Auth-Token": "integration-secret"},
            data={"payload": json.dumps(plex_payload)},
            content_type="multipart/form-data",
        )

        assert_webhook_queued(
            response,
            "plex",
            str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"),
        )
