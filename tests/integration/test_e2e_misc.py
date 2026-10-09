"""Misc integration tests against the live Emby container.

* Per-server fallback webhook URL (``/api/webhooks/server/<id>``) —
  the explicit-routing alternative to the universal endpoint.
* Frame cache TTL expiration — set a tiny TTL, verify a second
  dispatch after the TTL window does re-run FFmpeg.
* Webhook with bad token rejected (auth integration).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.integration.conftest import assert_webhook_queued


@pytest.fixture
def webhook_app(emby_credentials, media_root, tmp_path, monkeypatch, live_config):
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


@pytest.mark.integration
class TestWebhookAuth:
    def test_missing_token_rejected(self, webhook_app):
        response = webhook_app.test_client().post(
            "/api/webhooks/incoming",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code == 401, response.get_data(as_text=True)

    def test_bad_token_rejected(self, webhook_app):
        response = webhook_app.test_client().post(
            "/api/webhooks/incoming",
            headers={"X-Auth-Token": "wrong-token", "Content-Type": "application/json"},
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code == 401

    def test_token_via_query_param(self, webhook_app):
        """Plex's webhook UI doesn't support custom headers — token via ?token= must work."""
        response = webhook_app.test_client().post(
            "/api/webhooks/incoming?token=integration-secret",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"path": "/notindexed.mkv", "trigger": "x"}),
        )
        assert response.status_code == 202, response.get_data(as_text=True)


@pytest.mark.integration
class TestPerServerFallbackWebhook:
    def test_explicit_per_server_url(self, webhook_app, media_root):
        """``/api/webhooks/server/<id>`` skips vendor classification."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        response = webhook_app.test_client().post(
            "/api/webhooks/server/emby-int-1",
            headers={
                "X-Auth-Token": "integration-secret",
                "Content-Type": "application/json",
            },
            data=json.dumps({"path": canonical}),
        )
        assert_webhook_queued(response, "path", canonical)

    def test_unknown_server_id_returns_404(self, webhook_app):
        response = webhook_app.test_client().post(
            "/api/webhooks/server/does-not-exist",
            headers={
                "X-Auth-Token": "integration-secret",
                "Content-Type": "application/json",
            },
            data=json.dumps({"path": "/x.mkv"}),
        )
        assert response.status_code == 404


@pytest.mark.integration
@pytest.mark.slow
class TestFrameCacheTtlExpiry:
    def test_cache_entry_evicts_after_ttl(self, emby_credentials, media_root, live_config, tmp_path, monkeypatch):
        """Set a 1-second TTL, dispatch twice with a sleep between → FFmpeg runs twice.

        This is the inverse of the concurrent-coalescing test: that
        proves "second-and-later within TTL hit cache"; this proves
        "after TTL, cache is gone and FFmpeg re-runs".

        Uses ``monkeypatch`` on ``_read_frame_reuse_setting`` — ``get_frame_cache()``
        now live-reads the settings on every call, so an explicit
        ``ttl_seconds=1`` kwarg at construction gets overwritten on the
        next call. Patching the settings reader is the only way to
        simulate a 1-second TTL end-to-end.
        """
        from media_preview_generator.processing import frame_cache as fc
        from media_preview_generator.processing import multi_server as ms_module
        from media_preview_generator.processing.frame_cache import (
            get_frame_cache,
            reset_frame_cache,
        )
        from media_preview_generator.processing.multi_server import MultiServerStatus, process_canonical_path
        from media_preview_generator.servers import ServerRegistry

        # Force a 1-second TTL via the settings-read path.
        monkeypatch.setattr(fc, "_read_frame_reuse_setting", lambda: (1, 2048))
        reset_frame_cache()
        cache_base = tmp_path / "cache_base"
        get_frame_cache(base_dir=str(cache_base))

        raw_servers = [
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
        ]
        registry = ServerRegistry.from_settings(raw_servers, legacy_config=None)

        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / "Test Movie H264 (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        # The dispatcher anchors the cache at config.tmp_folder. Set it
        # explicitly so the singleton's base_dir lookup matches what we
        # pre-seed below.
        live_config.tmp_folder = str(tmp_path)
        live_config.working_tmp_folder = str(tmp_path)
        reset_frame_cache()
        get_frame_cache(base_dir=str(Path(live_config.tmp_folder) / "frame_cache"))

        original_generate = ms_module.generate_images
        ffmpeg_calls = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        monkeypatch.setattr(ms_module, "generate_images", _spy)

        try:
            # First dispatch: real run.
            first = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=live_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert first.status is MultiServerStatus.PUBLISHED, first.message

            # Wait for TTL to expire.
            time.sleep(2)
            # Force-rebuild the publisher's already-published BIF removal
            # so the second dispatch isn't short-circuited by skip-if-exists.
            if sidecar.exists():
                sidecar.unlink()

            # Second dispatch: cache should be expired → FFmpeg runs again.
            second = process_canonical_path(
                canonical_path=canonical,
                registry=registry,
                config=live_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert second.status is MultiServerStatus.PUBLISHED, second.message

            # FFmpeg ran twice (once per dispatch since TTL expired
            # between them).
            assert len(ffmpeg_calls) == 2, f"expected 2 FFmpeg calls (TTL expired), got {len(ffmpeg_calls)}"
        finally:
            if sidecar.exists():
                sidecar.unlink()
            reset_frame_cache()
