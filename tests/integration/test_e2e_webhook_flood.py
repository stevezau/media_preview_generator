"""Flood test: 50 distinct files dispatched in parallel.

Different from the same-file coalescing test: this fires many dispatches
for *different* files and asserts each ran its own FFmpeg pass without
serializing on a global lock.

The key invariant: ``FrameCache.generation_lock`` is per-canonical-path,
so two different files should produce two FFmpeg invocations executing
in parallel (limited only by the worker pool's CPU thread count).
A regression that introduced a global lock — say, a misplaced module-
level threading.Lock — would coalesce all 50 to 1 FFmpeg call, which
this test catches.

Files are tiny synthetic clips so 50 of them generate fast even on a
slow runner.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from media_preview_generator.processing.frame_cache import reset_frame_cache
from media_preview_generator.processing.multi_server import MultiServerStatus, process_canonical_path
from media_preview_generator.servers import ServerRegistry
from tests.integration.conftest import BIF_MAGIC

_FLOOD_COUNT = 50


@pytest.fixture
def flood_media_dir(media_root, tmp_path):
    """Generate ``_FLOOD_COUNT`` distinct test clips under tmp_path/Movies."""
    src = media_root / "Movies" / "Test Movie H264 (2024)" / "Test Movie H264 (2024).mkv"
    movies = tmp_path / "Movies"
    movies.mkdir(parents=True)
    paths = []
    for i in range(_FLOOD_COUNT):
        # Cheap "different file" — copy + append a unique tag so size and
        # content differ per file and journals can't short-circuit.
        sub = movies / f"Flood Test {i:02d} (2024)"
        sub.mkdir()
        dst = sub / f"Flood Test {i:02d} (2024).mkv"
        shutil.copyfile(src, dst)
        with dst.open("ab") as f:
            f.write(b"FLOOD" + str(i).zfill(4).encode())
        paths.append(dst)
    yield paths
    shutil.rmtree(movies, ignore_errors=True)


@pytest.fixture
def flood_registry(emby_credentials, tmp_path):
    """Single-Emby registry whose library covers the per-test tmp media dir."""
    raw_servers = [
        {
            "id": "emby-flood",
            "type": "emby",
            "name": "Test Emby (flood)",
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
            "path_mappings": [{"remote_prefix": "/em-media", "local_prefix": str(tmp_path)}],
            "output": {"adapter": "emby_sidecar", "width": 320, "frame_interval": 5},
        }
    ]
    return ServerRegistry.from_settings(raw_servers)


@pytest.mark.integration
@pytest.mark.slow
class TestFloodAcrossDistinctFiles:
    """50 different files dispatched simultaneously — verify per-path lock isn't global."""

    def test_fifty_parallel_dispatches_each_run_ffmpeg(self, flood_registry, flood_media_dir, live_config, monkeypatch):
        from media_preview_generator.processing import multi_server as ms_module

        # Reset frame cache so previous tests can't shortcut us.
        reset_frame_cache()

        original_generate = ms_module.generate_images
        ffmpeg_calls: list[str] = []

        def _spy(*args, **kwargs):
            ffmpeg_calls.append(args[0])
            return original_generate(*args, **kwargs)

        monkeypatch.setattr(ms_module, "generate_images", _spy)

        def _dispatch(canonical_path: Path):
            return process_canonical_path(
                canonical_path=str(canonical_path),
                registry=flood_registry,
                config=live_config,
                gpu=None,
                gpu_device_path=None,
            )

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(_dispatch, flood_media_dir))

        failed = [r.message for r in results if r.status is not MultiServerStatus.PUBLISHED]
        assert not failed, failed

        # 50 different files → 50 distinct FFmpeg invocations. If a
        # global lock crept in, this would be 1.
        unique_ffmpeg_inputs = set(ffmpeg_calls)
        assert len(unique_ffmpeg_inputs) == _FLOOD_COUNT, (
            f"expected {_FLOOD_COUNT} distinct FFmpeg invocations, got {len(unique_ffmpeg_inputs)}; "
            f"this means a global lock serialized unrelated work"
        )

        for p in flood_media_dir:
            sidecar = p.parent / f"{p.stem}-320-5.bif"
            assert sidecar.exists(), f"sidecar missing for {p.name}"
            assert sidecar.read_bytes()[:8] == BIF_MAGIC, f"{sidecar} has bad BIF magic"
