"""End-to-end test for Unicode path mapping.

Verifies a canonical path containing non-ASCII characters (Japanese
title, accented characters, emoji) flows through the dispatcher and
publishes correctly. Regression guard for the NFC normalisation
fix in ``servers/ownership.py::_normalize`` and ``config/paths.py::
_path_matches_prefix``.

Path NFD-vs-NFC differences come up when the source filesystem is
HFS+ (macOS) — out of scope for this Linux-only test container, but
the in-memory NFC normalisation also defends against it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    PublisherStatus,
    process_canonical_path,
)
from media_preview_generator.servers import ServerRegistry
from tests.integration.conftest import BIF_MAGIC

# Unicode title with: Japanese, accented latin, emoji. Real-world worst
# case — a user with a multi-language library.
UNICODE_TITLE = "メディア café 🎬 (2024)"


@pytest.fixture
def unicode_media(media_root: Path) -> Path:
    """Generate a small test video at a canonical path with Unicode characters."""
    parent = media_root / "Movies" / UNICODE_TITLE
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / f"{UNICODE_TITLE}.mkv"
    if not target.exists():
        # Use ffmpeg lavfi to create a deterministic 5-second clip. Tiny
        # fixture; takes ~0.2s. We don't reuse generate_test_media.sh
        # because that script's filenames are fixed.
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=size=320x180:rate=30:duration=5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(target),
            ],
            check=True,
        )
    yield target
    # Best-effort cleanup of unicode dir if we created it.
    if parent.exists():
        try:
            shutil.rmtree(parent)
        except OSError:
            pass


@pytest.fixture
def unicode_registry(emby_credentials, media_root):
    raw_servers = [
        {
            "id": "emby-unicode",
            "type": "emby",
            "name": "Test Emby (unicode)",
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


@pytest.mark.integration
class TestUnicodePathPublish:
    def test_publish_works_for_unicode_canonical_path(self, unicode_media: Path, unicode_registry, live_config):
        """Canonical path contains Japanese + accented + emoji chars; publish anyway."""
        canonical = str(unicode_media)
        sidecar = unicode_media.parent / f"{UNICODE_TITLE}-320-5.bif"
        if sidecar.exists():
            sidecar.unlink()

        try:
            result = process_canonical_path(
                canonical_path=canonical,
                registry=unicode_registry,
                config=live_config,
                gpu=None,
                gpu_device_path=None,
            )
            # Ownership matched (NFC normalisation + library remote_paths
            # all-ASCII so this just exercises the unicode-canonical path
            # arriving at an ASCII-prefix library).
            assert result.status is MultiServerStatus.PUBLISHED, result.message
            assert all(p.status is PublisherStatus.PUBLISHED for p in result.publishers)
            assert sidecar.exists()
            # File on disk is the same Unicode title byte-for-byte.
            assert UNICODE_TITLE in sidecar.name
            # Valid BIF header.
            head = sidecar.read_bytes()[:8]
            assert head == BIF_MAGIC
        finally:
            if sidecar.exists():
                sidecar.unlink()
            for f in unicode_media.parent.glob("*.bif.meta"):
                f.unlink()
