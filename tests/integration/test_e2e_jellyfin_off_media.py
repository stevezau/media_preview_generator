"""Live e2e test for OFF-MEDIA Jellyfin trickplay (#264).

The full user-visible loop against a real Jellyfin **10.11+** with the
Media Preview Bridge plugin installed and its config dir mounted
read-write into the test runner:

1. ``process_canonical_path`` (the real dispatcher) runs FFmpeg once and
   the JellyfinTrickplayAdapter publishes tile sheets into the Jellyfin
   **data folder** (``<config>/data/trickplay/<id[:2]>/<id>/<W> - 10x10/``),
   NOT next to the media.
2. The dispatcher's ``trigger_refresh`` calls the plugin's
   ``saveWithMedia=false`` endpoint, which registers the tiles with the
   correct ``ThumbnailCount``.
3. With the library option ``SaveTrickplayWithMedia=false``, Jellyfin
   **serves** the tile sheet from the data folder over HTTP — the byte
   stream the web client fetches when scrubbing.

This is distinct from the on-media chain (test_e2e_jellyfin_trickplay_fix.py)
and is the only test that exercises the off-media GUID layout end-to-end.

Gated on a dedicated env set (the standard integration Jellyfin is 10.9.11
without the plugin, so it can't run this). Provide, e.g. against the
``mpg_test`` canary rig::

    MPG_OFFMEDIA_JELLYFIN_URL=http://localhost:8197 \
    MPG_OFFMEDIA_JELLYFIN_TOKEN=<admin token> \
    MPG_OFFMEDIA_JELLYFIN_CONFIG_DIR=<this runner's RW mount of JF /config> \
    MPG_OFFMEDIA_MEDIA_FILE=<abs path to a media file indexed in JF & readable here> \
    pytest -m integration --no-cov tests/integration/test_e2e_jellyfin_off_media.py

The config dir MUST be writable by this process (the real deployment
mounts it RW with a matching PUID); the test skips with a clear message
if it isn't, rather than failing spuriously.
"""

from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from media_preview_generator.processing.multi_server import process_canonical_path
from media_preview_generator.servers import ServerRegistry

_ENV = (
    "MPG_OFFMEDIA_JELLYFIN_URL",
    "MPG_OFFMEDIA_JELLYFIN_TOKEN",
    "MPG_OFFMEDIA_JELLYFIN_CONFIG_DIR",
    "MPG_OFFMEDIA_MEDIA_FILE",
)


@pytest.fixture(scope="session")
def offmedia_env() -> dict[str, str]:
    missing = [k for k in _ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(f"off-media Jellyfin env not set: {missing} (needs JF 10.11 + plugin + RW config mount)")
    env = {k: os.environ[k] for k in _ENV}
    if not Path(env["MPG_OFFMEDIA_MEDIA_FILE"]).is_file():
        pytest.skip(f"media file not readable here: {env['MPG_OFFMEDIA_MEDIA_FILE']}")
    # The publisher must be able to write into the config dir (real deploys
    # mount it RW with a matching PUID). Skip clearly rather than fail.
    cfg = env["MPG_OFFMEDIA_JELLYFIN_CONFIG_DIR"]
    if not (os.path.isdir(cfg) and os.access(cfg, os.W_OK)):
        pytest.skip(f"Jellyfin config dir not writable by this process: {cfg}")
    return env


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


def _resolve_item(jf_url: str, token: str, media_path: str) -> tuple[str, str]:
    """Return (item_id, library_id) for the media file, via the plugin ResolvePath."""
    r = requests.get(
        f"{jf_url}/MediaPreviewBridge/ResolvePath",
        headers=_jf_headers(token),
        params={"path": media_path},
        timeout=15,
    )
    assert r.status_code == 200, f"plugin ResolvePath failed ({r.status_code}); is the plugin installed? {r.text[:200]}"
    item_id = r.json()["itemId"]
    # Find which library owns it (for the registry + the SaveTrickplayWithMedia flip).
    folders = requests.get(f"{jf_url}/Library/VirtualFolders", headers=_jf_headers(token), timeout=15).json()
    lib_id = ""
    best = 0
    for f in folders:
        for loc in f.get("Locations") or []:
            # Prefer the longest matching location prefix (the folder that
            # actually owns the item when virtual folders share a prefix).
            if media_path.startswith(loc) and len(loc) > best:
                best = len(loc)
                lib_id = f.get("ItemId") or f.get("Id") or ""
    return item_id, lib_id


def _offmedia_sheet_dir(config_dir: str, item_id: str, width: int = 320) -> Path:
    guid = str(uuid.UUID(item_id))  # dashless API id -> dashed "D" form Jellyfin uses on disk
    return Path(config_dir) / "data" / "trickplay" / guid[:2] / guid / f"{width} - 10x10"


@pytest.mark.integration
@pytest.mark.slow
class TestJellyfinOffMediaEndToEnd:
    def test_off_media_publish_register_and_serve(self, offmedia_env, offmedia_config):
        jf_url = offmedia_env["MPG_OFFMEDIA_JELLYFIN_URL"].rstrip("/")
        token = offmedia_env["MPG_OFFMEDIA_JELLYFIN_TOKEN"]
        config_dir = offmedia_env["MPG_OFFMEDIA_JELLYFIN_CONFIG_DIR"]
        media = offmedia_env["MPG_OFFMEDIA_MEDIA_FILE"]

        item_id, lib_id = _resolve_item(jf_url, token, media)
        assert item_id, "could not resolve the media file to a Jellyfin item id"
        assert lib_id, "could not find the owning Jellyfin library"

        media_dir = str(Path(media).parent)
        registry = ServerRegistry.from_settings(
            [
                {
                    "id": "jf-offmedia",
                    "type": "jellyfin",
                    "name": "Off-media Jellyfin",
                    "enabled": True,
                    "url": jf_url,
                    "auth": {"method": "api_key", "api_key": token},
                    "libraries": [{"id": lib_id, "name": "lib", "remote_paths": [media_dir], "enabled": True}],
                    "path_mappings": [],
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
        # Snapshot the library option so we can restore it.
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
                canonical_path=media,
                registry=registry,
                config=offmedia_config,
                gpu=None,
                gpu_device_path=None,
            )
            # Assert the publish SUCCEEDED for our off-media server (publishers
            # is non-empty even on failure — published_count counts real writes).
            assert result.published_count == 1, (
                f"expected 1 published, got {[(p.server_id, p.status) for p in result.publishers]}"
            )
            assert result.publishers[0].server_id == "jf-offmedia"

            # 3. Tiles landed in the Jellyfin data folder. (We don't assert the
            #    media-adjacent dir is absent: on a shared rig it may pre-exist
            #    from earlier media-adjacent runs. The adapter unit tests prove
            #    off-media writes ONLY to the config dir; here we prove the data
            #    folder got the tiles and Jellyfin serves them from there.)
            # sheet_dir is, by construction, <config_dir>/data/trickplay/<id>/...
            # so tiles existing there proves the publish targeted the config dir.
            tiles = sorted(sheet_dir.glob("*.jpg")) if sheet_dir.is_dir() else []
            assert tiles, f"no off-media tiles written under {sheet_dir}"
            assert tiles[0].name == "0.jpg"

            # 4. Jellyfin registered the trickplay (correct geometry).
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

            # 5. The decisive proof: Jellyfin SERVES the off-media tile, and it's
            #    byte-identical to what we wrote into the data folder.
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
            # Restore the original library options.
            requests.post(
                f"{jf_url}/Library/VirtualFolders/LibraryOptions",
                headers={**_jf_headers(token), "Content-Type": "application/json"},
                json={"Id": lib_id, "LibraryOptions": original_opts},
                timeout=15,
            )
