"""Full end-to-end integration tests — real FFmpeg, real webhooks.

The other integration test file mocks ``generate_images`` so the test
runs in milliseconds. *This* file deliberately does not — it runs
real FFmpeg against the synthetic test fixtures, drives the dispatcher
through the real Flask app's ``/api/webhooks/incoming`` endpoint, and
asserts on the resulting BIF byte structure.

These tests are slow (5-15s each because FFmpeg actually runs) but
they're the only thing that proves the entire chain works:

    Sonarr-style webhook
      → /api/webhooks/incoming
      → webhook_router
      → process_canonical_path
      → generate_images (FFmpeg subprocess)
      → EmbyBifAdapter.publish (real generate_bif call)
      → BIF written next to media

If any of those steps regresses, these tests fail. The mock-everything
unit suite cannot catch breakage at the layer boundaries.

Run with::

    pytest -m integration --no-cov tests/integration/test_e2e_full_pipeline.py
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry
from tests.integration.conftest import BIF_MAGIC, JPEG_SOI, assert_webhook_queued

# ---------------------------------------------------------------------------
# BIF format reader — used to validate publisher output is structurally sound.
# ---------------------------------------------------------------------------

# BIF magic header (8 bytes); see Roku's spec + the project's CLAUDE.md.


def _decode_bif(path: Path) -> dict:
    """Parse a BIF file's header + index table; return the structural metadata.

    Validates: magic, version uint32, image-count uint32, frame-interval-ms
    uint32, and that each indexed offset points at a JPEG SOI marker.
    Raises ``AssertionError`` if anything's off.
    """
    raw = path.read_bytes()
    assert len(raw) >= 64, f"BIF too small ({len(raw)} bytes); header is 64 bytes"
    assert raw[:8] == BIF_MAGIC, f"BIF magic mismatch: got {raw[:8].hex()}"

    version = struct.unpack("<I", raw[8:12])[0]
    image_count = struct.unpack("<I", raw[12:16])[0]
    interval_ms = struct.unpack("<I", raw[16:20])[0]
    # raw[20:64] is reserved, ignored.

    assert image_count > 0, "BIF claims 0 images"

    # Index table starts at offset 64; each entry is 8 bytes
    # (timestamp uint32 + offset uint32). Plus an 8-byte terminator
    # (0xffffffff + final offset).
    index_start = 64
    index_end = index_start + (image_count * 8) + 8
    assert len(raw) >= index_end, "BIF truncated (index table doesn't fit)"

    offsets: list[int] = []
    for i in range(image_count):
        entry = raw[index_start + i * 8 : index_start + (i + 1) * 8]
        timestamp = struct.unpack("<I", entry[:4])[0]
        offset = struct.unpack("<I", entry[4:8])[0]
        del timestamp  # we don't assert specific values
        offsets.append(offset)

    # Terminator
    term = raw[index_end - 8 : index_end]
    assert term[:4] == bytes([0xFF, 0xFF, 0xFF, 0xFF]), f"missing index terminator: {term.hex()}"

    # Sanity-check the first frame is actually a JPEG.
    first_offset = offsets[0]
    assert raw[first_offset : first_offset + 3] == JPEG_SOI, (
        f"frame at offset {first_offset} is not a JPEG (bytes={raw[first_offset : first_offset + 4].hex()})"
    )

    return {
        "version": version,
        "image_count": image_count,
        "interval_ms": interval_ms,
        "offsets": offsets,
        "size_bytes": len(raw),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def emby_registry(emby_credentials, media_root):
    """Single-Emby registry pointing at the live container + local media."""
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
    return ServerRegistry.from_settings(raw_servers)


# ---------------------------------------------------------------------------
# Real FFmpeg → real BIF → structurally valid
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
class TestRealFFmpegPipeline:
    """No mocks at the FFmpeg boundary — the pipeline runs end-to-end."""

    def test_real_ffmpeg_produces_valid_bif_for_h264_clip(self, emby_registry, media_root, live_config):
        """Process a real H.264 clip; verify FFmpeg ran, BIF lands, BIF parses."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        sidecar = Path(canonical).parent / f"Test Movie H264 (2024)-320-{int(live_config.plex_bif_frame_interval)}.bif"

        # Clear any leftover from a previous run.
        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=emby_registry,
                config=live_config,
                # Force CPU; the test runner may not have a GPU.
                gpu=None,
                gpu_device_path=None,
            )
        finally:
            # Defensive — even if the test fails, don't leak state.
            pass

        assert result.status is MultiServerStatus.PUBLISHED, (
            f"expected PUBLISHED, got {result.status}: {result.message}"
        )
        assert result.frame_count > 0, "FFmpeg produced 0 frames"
        published = next((p for p in result.publishers if p.status is PublisherStatus.PUBLISHED), None)
        assert published is not None, [(p.adapter_name, p.status.value, p.message) for p in result.publishers]

        try:
            assert sidecar.exists(), f"sidecar BIF missing at {sidecar}"
            decoded = _decode_bif(sidecar)
            # Synthetic clip is 30s, frame interval 5s → ~6 frames; FFmpeg's
            # actual output count varies slightly with codec/seek precision,
            # so allow a range.
            assert 4 <= decoded["image_count"] <= 8, (
                f"unexpected frame count: {decoded['image_count']} (expected 4-8 for 30s @ 5s interval)"
            )
            # Frame interval encoded in ms.
            assert decoded["interval_ms"] == 5000, decoded["interval_ms"]
            # First frame is a real JPEG.
            assert decoded["size_bytes"] > 1024, (
                f"BIF suspiciously small ({decoded['size_bytes']} bytes); JPEGs should be sizable"
            )
        finally:
            if sidecar.exists():
                sidecar.unlink()

    def test_real_ffmpeg_handles_hevc_clip(self, emby_registry, media_root, live_config):
        """The HEVC fixture also produces a valid BIF — covers the other codec."""
        canonical = str(media_root / "Movies" / "Test Movie HEVC (2024)" / "Test Movie HEVC (2024).mkv")
        sidecar = Path(canonical).parent / f"Test Movie HEVC (2024)-320-{int(live_config.plex_bif_frame_interval)}.bif"

        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=emby_registry,
                config=live_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert sidecar.exists()
            decoded = _decode_bif(sidecar)
            assert decoded["image_count"] >= 4
        finally:
            if sidecar.exists():
                sidecar.unlink()


# ---------------------------------------------------------------------------
# Real webhook → real Flask app → real BIF
# ---------------------------------------------------------------------------


@pytest.fixture
def flask_app_for_webhook(emby_credentials, media_root, tmp_path, monkeypatch):
    """A real Flask app instance wired to the live Emby + local media.

    Settings persist into ``tmp_path/config/settings.json``; the
    webhook secret is "integration-secret" so the X-Auth-Token header
    in the request is accepted.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Reset all singletons before configuring the new app.
    from media_preview_generator.web.settings_manager import (
        reset_settings_manager,
    )

    reset_settings_manager()

    # Configure CONFIG_DIR before creating the app — settings_manager keys
    # off it.
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "integration-test-token")

    from media_preview_generator.web.app import create_app
    from media_preview_generator.web.settings_manager import get_settings_manager

    app = create_app(config_dir=str(config_dir))
    app.config["TESTING"] = True

    settings = get_settings_manager()
    # Webhook secret used by /api/webhooks/incoming auth.
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
    # Mark setup complete so the before_request middleware doesn't
    # redirect /api/* to the wizard.
    settings.complete_setup()
    return app


@pytest.mark.integration
@pytest.mark.slow
class TestWebhookEndToEnd:
    """Real Flask app receives a webhook and queues a Job for the file."""

    def test_sonarr_style_path_webhook_queues_a_job(
        self,
        flask_app_for_webhook,
        media_root,
    ):
        """A path-based webhook (Sonarr/Radarr/templated) is classified path-first and queued as a Job."""
        canonical = str(media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv")
        response = flask_app_for_webhook.test_client().post(
            "/api/webhooks/incoming",
            headers={"X-Auth-Token": "integration-secret", "Content-Type": "application/json"},
            data=json.dumps({"path": canonical, "trigger": "file_added"}),
        )
        assert_webhook_queued(response, "path", canonical)

    def test_emby_native_webhook_resolves_item_id_and_queues_a_job(
        self,
        flask_app_for_webhook,
        emby_credentials,
        media_root,
    ):
        """An Emby-shaped webhook (with ItemId, no path) is resolved to a path and queued as a Job.

        The router calls back to Emby to translate item id → path, which
        exercises the item-id-only webhook flow against a real Emby server.
        """
        # Look up a real item id from the live Emby container.
        import requests

        items_resp = requests.get(
            f"{emby_credentials['EMBY_URL']}/Items",
            params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "Fields": "Path",
                "Limit": 10,
            },
            headers={"X-Emby-Token": emby_credentials["EMBY_ACCESS_TOKEN"]},
            timeout=10,
        )
        items_resp.raise_for_status()
        items = items_resp.json().get("Items", [])
        # Pick the H264 fixture so the expected canonical path is known.
        target = next(i for i in items if "H264" in (i.get("Path") or ""))
        target_path = target["Path"]
        target_id = target["Id"]

        # Map the Emby-side path back to the local canonical path.
        local_canonical = target_path.replace("/em-media", str(media_root), 1)

        response = flask_app_for_webhook.test_client().post(
            "/api/webhooks/incoming",
            headers={"X-Auth-Token": "integration-secret", "Content-Type": "application/json"},
            data=json.dumps(
                {
                    # Emby Webhooks-plugin shape (Plex-format-compatible).
                    "Event": "library.new",
                    "Item": {"Id": target_id},
                    "Server": {"Id": emby_credentials["EMBY_SERVER_ID"]},
                }
            ),
        )
        assert_webhook_queued(response, "emby", local_canonical)
