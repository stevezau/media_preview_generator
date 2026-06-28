"""Live e2e test for OFF-MEDIA Jellyfin trickplay (#264).

The full user-visible loop against the real Jellyfin 10.11 container brought up
by docker-compose.test.yml, with the Media Preview Bridge plugin installed by
setup_servers.py and the config dir bind-mounted read-write:

1. ``process_canonical_path`` (the real dispatcher) runs FFmpeg once and the
   JellyfinTrickplayAdapter publishes tile sheets into the Jellyfin **data
   folder** (``<programdata>/data/trickplay/<id[:2]>/<id>/<W> - 10x10/``),
   NOT next to the media.
2. The dispatcher's ``trigger_refresh`` calls the plugin's ``saveWithMedia=false``
   endpoint, which registers the tiles with the correct ``ThumbnailCount``.
3. With the library option ``SaveTrickplayWithMedia=false``, Jellyfin **serves**
   the tile sheet from the data folder over HTTP — byte-identical to what we
   wrote.

Distinct from the on-media chain (test_e2e_jellyfin_trickplay_fix.py); the only
test exercising the off-media GUID layout end-to-end. Skips cleanly when the
10.11 + plugin + bind-mount harness isn't up (see the jellyfin_config_dir fixture).
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.processing.multi_server import process_canonical_path
from media_preview_generator.servers import ServerRegistry

_JF_REMOTE_PREFIX = "/jf-media"
_MEDIA_REL = ("Movies", "Test Movie H264 (2024)", "Test Movie H264 (2024).mkv")


@pytest.fixture
def offmedia_config(tmp_path):
    config = MagicMock()
    config.plex_url = ""
    config.plex_token = ""
    config.plex_config_folder = ""
    config.path_mappings = []
    config.plex_bif_frame_interval = 5
    config.thumbnail_quality = 4
    config.regenerate_thumbnails = False
    config.gpu_threads = 0
    config.cpu_threads = 2
    config.gpu_config = []
    config.tmp_folder = str(tmp_path / "tmp")
    config.working_tmp_folder = str(tmp_path / "tmp")
    Path(config.working_tmp_folder).mkdir(parents=True, exist_ok=True)
    config.tmp_folder_created_by_us = False
    config.ffmpeg_path = "/usr/bin/ffmpeg"
    config.ffmpeg_threads = 2
    config.tonemap_algorithm = "hable"
    config.log_level = "INFO"
    config.worker_pool_timeout = 120
    config.plex_verify_ssl = True
    return config


def _jf_headers(token: str) -> dict[str, str]:
    return {"X-Emby-Token": token}


def _resolve_item(jf_url: str, token: str, remote_path: str) -> str:
    """Resolve the media file to its Jellyfin item id via the plugin (with retry
    for the post-library-create scan to finish indexing)."""
    for _ in range(30):
        r = requests.get(
            f"{jf_url}/MediaPreviewBridge/ResolvePath",
            headers=_jf_headers(token),
            params={"path": remote_path},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()["itemId"]
        time.sleep(2)
    raise AssertionError(f"Jellyfin never indexed {remote_path} (plugin ResolvePath kept 404ing)")


def _movies_library_id(jf_url: str, token: str) -> str:
    folders = requests.get(f"{jf_url}/Library/VirtualFolders", headers=_jf_headers(token), timeout=15).json()
    for f in folders:
        if any(loc.startswith(_JF_REMOTE_PREFIX) for loc in (f.get("Locations") or [])):
            return f.get("ItemId") or f.get("Id") or ""
    raise AssertionError("no Movies library found on the test Jellyfin")


def _offmedia_sheet_dir(config_dir: str, item_id: str, width: int = 320) -> Path:
    guid = str(uuid.UUID(item_id))  # dashless API id -> dashed "D" form Jellyfin uses on disk
    return Path(config_dir) / "data" / "trickplay" / guid[:2] / guid / f"{width} - 10x10"


@pytest.mark.integration
@pytest.mark.slow
class TestJellyfinOffMediaEndToEnd:
    def test_off_media_publish_register_and_serve(
        self, jellyfin_credentials, jellyfin_config_dir, media_root, offmedia_config
    ):
        jf_url = jellyfin_credentials["JELLYFIN_URL"].rstrip("/")
        token = jellyfin_credentials["JELLYFIN_ACCESS_TOKEN"]
        config_dir = jellyfin_config_dir

        local_media = str(media_root.joinpath(*_MEDIA_REL))
        remote_media = f"{_JF_REMOTE_PREFIX}/" + "/".join(_MEDIA_REL)
        assert Path(local_media).is_file(), f"synthetic media missing: {local_media}"

        item_id = _resolve_item(jf_url, token, remote_media)
        lib_id = _movies_library_id(jf_url, token)

        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "jf-offmedia",
                    "type": "jellyfin",
                    "name": "Off-media Jellyfin",
                    "enabled": True,
                    "url": jf_url,
                    "auth": {"method": "api_key", "api_key": token},
                    "server_identity": jellyfin_credentials["JELLYFIN_SERVER_ID"],
                    "libraries": [
                        {
                            "id": lib_id,
                            "name": "Movies",
                            "remote_paths": [remote_media.rsplit("/", 2)[0]],
                            "enabled": True,
                        }
                    ],
                    "path_mappings": [{"remote_prefix": _JF_REMOTE_PREFIX, "local_prefix": str(media_root)}],
                    "output": {
                        "adapter": "jellyfin_trickplay",
                        "width": 320,
                        "frame_interval": 10,
                        "save_with_media": False,
                        "jellyfin_config_folder": config_dir,
                    },
                }
            ],
            legacy_config=None,
        )

        sheet_dir = _offmedia_sheet_dir(config_dir, item_id)
        folders = requests.get(f"{jf_url}/Library/VirtualFolders", headers=_jf_headers(token), timeout=15).json()
        original_opts = next(
            (f.get("LibraryOptions") or {} for f in folders if (f.get("ItemId") or f.get("Id")) == lib_id), {}
        )

        try:
            # 1. Off-media library option must be false for Jellyfin to read the data folder.
            opts = dict(original_opts)
            opts["SaveTrickplayWithMedia"] = False
            opts["EnableTrickplayImageExtraction"] = True
            requests.post(
                f"{jf_url}/Library/VirtualFolders/LibraryOptions",
                headers={**_jf_headers(token), "Content-Type": "application/json"},
                json={"Id": lib_id, "LibraryOptions": opts},
                timeout=15,
            ).raise_for_status()

            if sheet_dir.parent.exists():
                shutil.rmtree(sheet_dir.parent, ignore_errors=True)

            # 2. Run the REAL dispatcher: FFmpeg once -> off-media publish -> plugin register.
            result = process_canonical_path(
                canonical_path=local_media,
                registry=registry,
                config=offmedia_config,
                gpu=None,
                gpu_device_path=None,
            )
            # Assert the publish SUCCEEDED for our off-media server (publishers is
            # non-empty even on failure — published_count counts real writes).
            assert result.published_count == 1, (
                f"expected 1 published, got {[(p.server_id, p.status) for p in result.publishers]}"
            )
            assert result.publishers[0].server_id == "jf-offmedia"

            # 3. Tiles landed in the data folder (sheet_dir is, by construction,
            #    <config_dir>/data/trickplay/<id>/... so this proves the target).
            tiles = sorted(sheet_dir.glob("*.jpg")) if sheet_dir.is_dir() else []
            assert tiles, f"no off-media tiles written under {sheet_dir}"
            assert tiles[0].name == "0.jpg"

            # 4. Jellyfin registered the trickplay with the correct geometry.
            seen = None
            for _ in range(20):
                body = requests.get(
                    f"{jf_url}/Items",
                    headers=_jf_headers(token),
                    params={"Ids": item_id, "Fields": "Trickplay"},
                    timeout=15,
                ).json()
                tp = (body.get("Items") or [{}])[0].get("Trickplay")
                if tp:
                    by_width = next(iter(tp.values()))
                    info = by_width.get("320") if isinstance(by_width, dict) else None
                    if isinstance(info, dict) and "TileWidth" in info:
                        seen = info
                        break
                time.sleep(1)
            assert seen, "Jellyfin never registered the off-media trickplay metadata"
            assert seen["TileWidth"] == 10 and seen["TileHeight"] == 10
            assert seen["ThumbnailCount"] > 1, "ThumbnailCount looks like a sheet count (the #12887 bug)"

            # 5. Decisive proof: Jellyfin serves the off-media tile, byte-identical.
            served = None
            for _ in range(15):
                served = requests.get(
                    f"{jf_url}/Videos/{item_id}/Trickplay/320/0.jpg", headers=_jf_headers(token), timeout=15
                )
                if served.status_code == 200:
                    break
                time.sleep(2)
            assert served is not None and served.status_code == 200, "Jellyfin 404s the off-media tile"
            assert served.content[:2] == b"\xff\xd8", "served bytes are not a JPEG"
            assert served.content == tiles[0].read_bytes(), (
                "Jellyfin served a DIFFERENT tile than the one we wrote off-media — "
                "the SaveTrickplayWithMedia=false serve path is not reading our data folder"
            )
        finally:
            shutil.rmtree(sheet_dir.parent, ignore_errors=True)
            requests.post(
                f"{jf_url}/Library/VirtualFolders/LibraryOptions",
                headers={**_jf_headers(token), "Content-Type": "application/json"},
                json={"Id": lib_id, "LibraryOptions": original_opts},
                timeout=15,
            )
