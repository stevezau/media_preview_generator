"""Pytest fixtures for the multi-server integration suite.

Loads ``servers.env`` (written by :mod:`setup_servers`) into module-level
fixtures so each test gets the live container's URL, server-id, and
admin token without re-parsing the file.

Why a separate ``tests/integration/`` directory
-----------------------------------------------

Files in this directory drive **live** Emby / Jellyfin / Plex containers
brought up by ``docker-compose.test.yml``. They are NOT mocked. They:

* Require ``./generate_test_media.sh`` to have produced ``./media/``.
* Require ``setup_servers.py`` to have written ``./servers.env`` with
  per-vendor admin tokens captured from a live boot.
* Take 5–60s each (real FFmpeg on real video, real HTTP to containers).

To keep the default ``pytest`` run fast with no Docker dependency, every
test file here is decorated with
``@pytest.mark.integration`` (file-level ``pytestmark`` or per-class).
The default ``pyproject.toml`` ``addopts`` includes
``-m "not gpu and not e2e and not integration"`` which deselects the
whole directory when nothing was asked for.

Explicit invocations:

* ``pytest -m integration --no-cov tests/integration/`` — full suite
  against the live containers (boot the stack first).
* ``pytest --no-cov tests/integration/`` — collects the whole directory but
  selects 0 (default ``-m`` filter still applies).  Confirms no ImportError.

If you ever see ``no tests collected`` from an explicit
``-m integration`` invocation, the most likely cause is a missing
``servers.env`` triggering a session-scoped ``pytest.skip`` in this
file (see ``servers_env`` fixture below) — bring the docker stack up.

Note for tooling (mutmut, etc.): this directory should be excluded by
default; mutating live-container code paths is meaningless and slow.
The ``tool.pytest.ini_options.markers`` block in ``pyproject.toml``
documents the ``integration`` marker explicitly.
"""

from __future__ import annotations

import struct
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
SERVERS_ENV = HERE / "servers.env"

BIF_MAGIC = bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])
JPEG_SOI = bytes([0xFF, 0xD8, 0xFF])


def assert_webhook_queued(response, kind: str, canonical: str) -> None:
    """Assert a webhook was accepted, classified as ``kind`` and queued as one Job for ``canonical``.

    Webhooks only queue a Job (run later by the job runner), so the response
    cannot say anything about published output.
    """
    from media_preview_generator.web.jobs import get_job_manager

    assert response.status_code == 202, response.get_data(as_text=True)
    body = response.get_json()
    assert body["status"] == "queued", body
    assert body["kind"] == kind, body
    assert body["canonical_path"] == canonical, body
    job = get_job_manager().get_job(body["job_id"])
    assert job is not None, body
    assert job.config["webhook_paths"] == [canonical], job.config


def decode_bif_count(path: Path) -> int:
    """Return the image count from a BIF header after checking its magic."""
    raw = path.read_bytes()
    assert raw[:8] == BIF_MAGIC
    return struct.unpack("<I", raw[12:16])[0]


def decode_bif(path: Path) -> dict:
    """Validate a BIF's magic and first-frame JPEG marker; return basic metadata."""
    raw = path.read_bytes()
    assert len(raw) >= 64
    assert raw[:8] == BIF_MAGIC
    image_count = struct.unpack("<I", raw[12:16])[0]
    interval_ms = struct.unpack("<I", raw[16:20])[0]
    assert image_count > 0
    first_offset = struct.unpack("<I", raw[64 + 4 : 64 + 8])[0]
    assert raw[first_offset : first_offset + 3] == JPEG_SOI
    return {"image_count": image_count, "interval_ms": interval_ms, "size_bytes": len(raw)}


def _parse_env(path: Path) -> dict[str, str]:
    """Read a tiny KEY=VALUE file (no quoting) into a dict."""
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


@pytest.fixture(scope="session")
def servers_env() -> dict[str, str]:
    """All keys from ``tests/integration/servers.env``."""
    if not SERVERS_ENV.exists():
        pytest.skip(f"{SERVERS_ENV} not found — run `python tests/integration/setup_servers.py --server emby` first")
    return _parse_env(SERVERS_ENV)


@pytest.fixture(scope="session")
def emby_credentials(servers_env: dict[str, str]) -> dict[str, str]:
    """Captured Emby credentials, or skip if Emby wasn't configured."""
    needed = ("EMBY_URL", "EMBY_SERVER_ID", "EMBY_ACCESS_TOKEN", "EMBY_USER_ID")
    missing = [k for k in needed if not servers_env.get(k)]
    if missing:
        pytest.skip(f"Emby credentials missing: {missing}")
    return {k: servers_env[k] for k in needed}


@pytest.fixture(scope="session")
def plex_credentials(servers_env: dict[str, str]) -> dict[str, str]:
    """Captured Plex credentials, or skip if Plex wasn't configured."""
    needed = ("PLEX_URL", "PLEX_SERVER_ID", "PLEX_ACCESS_TOKEN")
    missing = [k for k in needed if not servers_env.get(k)]
    if missing:
        pytest.skip(f"Plex credentials missing: {missing}")
    return {k: servers_env[k] for k in needed}


@pytest.fixture(scope="session")
def jellyfin_credentials(servers_env: dict[str, str]) -> dict[str, str]:
    """Captured Jellyfin credentials, or skip if Jellyfin wasn't configured."""
    needed = ("JELLYFIN_URL", "JELLYFIN_SERVER_ID", "JELLYFIN_ACCESS_TOKEN")
    missing = [k for k in needed if not servers_env.get(k)]
    if missing:
        pytest.skip(f"Jellyfin credentials missing: {missing}")
    return {k: servers_env[k] for k in needed}


@pytest.fixture(scope="session")
def jellyfin_config_dir(servers_env: dict[str, str]) -> str:
    """Host path of Jellyfin's ProgramDataPath (off-media jellyfin_config_folder).

    Written by setup_servers.py as ``JELLYFIN_CONFIG_DIR`` (the bind-mount's
    ``data/`` subdir). The off-media test writes trickplay into its
    ``data/trickplay/`` and reads the plugin-installed Jellyfin from it, so the
    dir must exist and be writable by this process. Skips cleanly otherwise."""
    import os as _os

    path = servers_env.get("JELLYFIN_CONFIG_DIR", "")
    if not path:
        pytest.skip("JELLYFIN_CONFIG_DIR not in servers.env (needs the 10.11 bind-mount harness)")
    if not (_os.path.isdir(path) and _os.access(path, _os.W_OK)):
        pytest.skip(f"Jellyfin config dir not writable by this process: {path}")
    return path


@pytest.fixture
def media_root() -> Path:
    """Local path to the synthetic test fixtures (mounted into containers)."""
    media_dir = HERE / "media"
    if not media_dir.exists():
        pytest.skip(f"{media_dir} missing — run ./tests/integration/generate_test_media.sh")
    return media_dir


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Reset the frame-cache and webhook de-duplication state between tests in this directory.

    Without the webhook reset, a second test posting the same file within the
    dedup window is silently dropped as a duplicate of the first.
    """
    from media_preview_generator.processing.frame_cache import reset_frame_cache
    from media_preview_generator.web.webhooks import reset_webhook_debounce

    reset_frame_cache()
    reset_webhook_debounce()
    yield
    reset_frame_cache()
    reset_webhook_debounce()


@pytest.fixture
def live_config(tmp_path: Path) -> MagicMock:
    """Config-shaped MagicMock for the live-stack tests: no Plex, CPU-only FFmpeg.

    Config is a frozen dataclass at runtime and the pipeline reads only a few
    attributes, so a MagicMock with explicit values avoids building the full
    schema while still hitting real FFmpeg. Tests that need Plex or a plex
    config folder override the relevant attributes in their own fixture.
    """
    config = MagicMock()
    config.plex_url = ""
    config.plex_token = ""
    config.plex_timeout = 60
    config.plex_libraries = []
    config.plex_config_folder = ""
    config.plex_local_videos_path_mapping = ""
    config.plex_videos_path_mapping = ""
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
    config.worker_pool_timeout = 60
    config.plex_library_ids = None
    config.plex_verify_ssl = True
    return config
