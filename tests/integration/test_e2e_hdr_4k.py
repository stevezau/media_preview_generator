"""End-to-end coverage for non-trivial real video formats.

The basic synthetic clips in ``tests/integration/media/`` are 720p
H.264 / 1080p HEVC, both 8-bit. Real users push 4K HDR (BT.2020,
10-bit, PQ transfer) through the pipeline. This file verifies the
pipeline handles such content without crashing — the BIF lands and
parses cleanly.
"""

from __future__ import annotations

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry
from tests.integration.conftest import decode_bif


@pytest.fixture
def hdr_emby_registry(emby_credentials, media_root):
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
            "libraries": [{"id": "movies", "name": "Movies", "remote_paths": ["/em-media/Movies"], "enabled": True}],
            "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(media_root)}],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
        }
    ]
    return ServerRegistry.from_settings(raw_servers, legacy_config=None)


@pytest.mark.integration
@pytest.mark.slow
class TestHdr4kRealVideo:
    """Real 4K HDR clip → tonemap → real BIF.

    Skipped when the test fixture isn't generated.
    """

    def test_4k_hdr_clip_produces_valid_bif(self, hdr_emby_registry, media_root, live_config):
        canonical_path = media_root / "Movies" / "Test 4K HDR (2024)" / "Test 4K HDR (2024).mkv"
        if not canonical_path.exists():
            pytest.skip(
                f"4K HDR fixture not generated. Run: "
                f"ffmpeg -f lavfi -i testsrc2=size=3840x2160:rate=24:duration=15 "
                f"-c:v libx265 -pix_fmt yuv420p10le "
                f"-x265-params 'colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc' "
                f'"{canonical_path}"'
            )

        sidecar = canonical_path.parent / "Test 4K HDR (2024)-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=str(canonical_path),
                registry=hdr_emby_registry,
                config=live_config,
                gpu=None,
                gpu_device_path=None,
            )
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            published = next(p for p in result.publishers if p.status is PublisherStatus.PUBLISHED)
            assert published.adapter_name == "emby_sidecar"

            assert sidecar.exists()
            decoded = decode_bif(sidecar)
            # 15-second clip @ every 5s = ~3 frames.
            assert decoded["image_count"] >= 2, decoded
            assert decoded["interval_ms"] == 5000

            # The first frame should be a real JPEG of size > 0.
            assert decoded["size_bytes"] > 1024
        finally:
            if sidecar.exists():
                sidecar.unlink()
