"""End-to-end integration tests against a live Jellyfin container.

Mirrors the Emby + Plex live tests for completeness:

* :class:`JellyfinServer` connects, lists libraries, lists items,
  resolves item ids → paths.
* :class:`JellyfinTrickplayAdapter` writes valid 10×10 tile sheets
  + manifest.json against a real path.
* The Jellyfin webhook plugin's ``ItemAdded`` shape routes through
  the universal endpoint and dispatches.
"""

from __future__ import annotations

import json
import shutil

import pytest

from media_preview_generator.output.jellyfin_trickplay import JellyfinTrickplayAdapter
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry
from tests.integration.conftest import assert_webhook_queued


@pytest.fixture
def jf_registry(jellyfin_credentials, media_root):
    raw_servers = [
        {
            "id": "jf-int-1",
            "type": "jellyfin",
            "name": "Test Jellyfin",
            "enabled": True,
            "url": jellyfin_credentials["JELLYFIN_URL"],
            "auth": {
                "method": "api_key",
                "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"],
            },
            "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
            "libraries": [
                {
                    "id": "movies",
                    "name": "Movies",
                    "remote_paths": ["/jf-media/Movies"],
                    "enabled": True,
                }
            ],
            "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
        }
    ]
    return ServerRegistry.from_settings(raw_servers)


@pytest.mark.integration
class TestLiveJellyfinConnection:
    def test_connects_and_identifies(self, jf_registry, jellyfin_credentials):
        server = jf_registry.get("jf-int-1")
        result = server.test_connection()
        assert result.ok, result.message
        assert result.server_id == jellyfin_credentials["JELLYFIN_SERVER_ID"]

    def test_list_libraries_returns_movies(self, jf_registry):
        server = jf_registry.get("jf-int-1")
        libraries = server.list_libraries()
        names = [lib.name for lib in libraries]
        assert "Movies" in names, names

    def test_list_items_returns_test_movies(self, jf_registry):
        server = jf_registry.get("jf-int-1")
        libraries = server.list_libraries()
        movies = next(lib for lib in libraries if lib.name == "Movies")
        items = list(server.list_items(movies.id))
        assert len(items) >= 1, [i.title for i in items]

    def test_resolve_remote_path_to_item_id(self, jf_registry, media_root):
        """The reverse-lookup helper finds the right Jellyfin item id."""
        server = jf_registry.get("jf-int-1")
        # Use the fixture's known path; the helper searches by basename
        # and verifies via parent-dir tail.
        target_path = "/jf-media/Movies/Test Movie H264 (2024)/Test Movie H264 (2024).mkv"
        item_id = server.resolve_remote_path_to_item_id(target_path)
        assert item_id, f"no item id found for {target_path}"

        # Verify by round-tripping through resolve_item_to_remote_path.
        roundtrip = server.resolve_item_to_remote_path(item_id)
        assert roundtrip == target_path, roundtrip


@pytest.mark.integration
@pytest.mark.slow
class TestLiveJellyfinTrickplay:
    """Real FFmpeg → real trickplay tile sheets in Jellyfin's ``<name>.trickplay/`` layout."""

    def test_trickplay_lands_with_tile_sheets(self, jf_registry, live_config, media_root):
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        trickplay_dir = JellyfinTrickplayAdapter.trickplay_dir(canonical)

        # Clean up any leftovers so we can assert the tests created them.
        if trickplay_dir.exists():
            shutil.rmtree(trickplay_dir)

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=jf_registry,
                config=live_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            published = next(p for p in result.publishers if p.status is PublisherStatus.PUBLISHED)
            assert published.adapter_name == "jellyfin_trickplay"

            # Jellyfin 10.10+ builds its own TrickplayInfo from the sheets; no manifest is written.
            assert not list(trickplay_dir.parent.glob("*-320.json"))
            # Fewer than 100 frames → a single 10x10 sheet, 0.jpg.
            sheets_dir = trickplay_dir / "320 - 10x10"
            assert sheets_dir.is_dir()
            sheets = sorted(f.name for f in sheets_dir.iterdir() if f.suffix == ".jpg")
            assert sheets == ["0.jpg"]
            assert (sheets_dir / "0.jpg").read_bytes()[:2] == b"\xff\xd8"
        finally:
            if trickplay_dir.exists():
                shutil.rmtree(trickplay_dir)


@pytest.mark.integration
@pytest.mark.slow
class TestJellyfinNativeWebhook:
    def test_jellyfin_itemadded_payload_queues_a_job(self, jellyfin_credentials, media_root, tmp_path, monkeypatch):
        """jellyfin-plugin-webhook stock ItemAdded → universal router → queued Job."""
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
                    "id": "jf-int-1",
                    "type": "jellyfin",
                    "name": "Test Jellyfin",
                    "enabled": True,
                    "url": jellyfin_credentials["JELLYFIN_URL"],
                    "auth": {
                        "method": "api_key",
                        "api_key": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"],
                    },
                    "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
                    "libraries": [
                        {
                            "id": "movies",
                            "name": "Movies",
                            "remote_paths": ["/jf-media/Movies"],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [{"remote_prefix": "/jf-media", "local_prefix": str(media_root)}],
                    "output": {"adapter": "jellyfin_trickplay", "width": 320, "frame_interval": 5},
                }
            ],
        )
        settings.complete_setup()

        # Find a real Jellyfin item id.
        import requests

        items_resp = requests.get(
            f"{jellyfin_credentials['JELLYFIN_URL']}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path",
                "Limit": 50,
            },
            headers={"X-Emby-Token": jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]},
            timeout=10,
        )
        items_resp.raise_for_status()
        target = next(i for i in items_resp.json()["Items"] if "H264" in (i.get("Path") or ""))
        target_id = target["Id"]
        canonical = target["Path"].replace("/jf-media", str(media_root), 1)

        response = app.test_client().post(
            "/api/webhooks/incoming",
            headers={"X-Auth-Token": "integration-secret", "Content-Type": "application/json"},
            data=json.dumps(
                {
                    "NotificationType": "ItemAdded",
                    "ItemId": target_id,
                    "ItemType": "Movie",
                    "ServerId": jellyfin_credentials["JELLYFIN_SERVER_ID"],
                    "ServerName": "Test Jellyfin",
                }
            ),
        )
        assert_webhook_queued(response, "jellyfin", canonical)
