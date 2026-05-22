"""Tests for ``process_canonical_path(check_only=True)``.

``check_only`` runs every pre-FFmpeg step a normal call would (publisher
resolution, source-missing probe, the all-fresh short-circuit and its side
effects, the regenerate meta-clear) and then stops at the FFmpeg boundary,
returning :attr:`MultiServerStatus.NEEDS_GENERATION` instead of extracting
frames. Terminal outcomes (SKIPPED, PUBLISHED pending-registration, NO_OWNERS,
SKIPPED_FILE_NOT_FOUND) return exactly as they do without the flag.

The orchestrator's high-concurrency scan phase relies on this so the heavy
FFmpeg path can only run once a (capped) generation permit is held — see
``jobs/orchestrator.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from media_preview_generator.processing.frame_cache import reset_frame_cache
from media_preview_generator.processing.multi_server import (
    MultiServerStatus,
    process_canonical_path,
)
from media_preview_generator.servers import (
    ServerRegistry,
    ServerType,
)


@pytest.fixture(autouse=True)
def _reset_frame_cache_singleton():
    reset_frame_cache()
    yield
    reset_frame_cache()


@pytest.fixture
def mock_config_for_processing(mock_config, tmp_path):
    mock_config.working_tmp_folder = str(tmp_path / "tmp")
    mock_config.tmp_folder = str(tmp_path / "tmp")
    return mock_config


def _emby_registry(tmp_path) -> ServerRegistry:
    """A single Emby server owning ``tmp_path/movies``.

    Emby uses the filename-derived sidecar adapter (no item-id lookup, no
    network), which keeps these tests deterministic and offline.
    """
    return ServerRegistry.from_settings(
        [
            {
                "id": "emby-1",
                "type": ServerType.EMBY.value,
                "name": "emby-1",
                "enabled": True,
                "url": "http://x",
                "auth": {"token": "t", "method": "api_key", "api_key": "k"},
                "libraries": [
                    {
                        "id": "1",
                        "name": "Movies",
                        "remote_paths": [str(tmp_path / "movies")],
                        "enabled": True,
                    }
                ],
                "exclude_paths": [],
                "output": {
                    "adapter": "emby_sidecar",
                    "plex_config_folder": "/cfg",
                    "width": 320,
                    "frame_interval": 10,
                },
            }
        ]
    )


def _seed_media(tmp_path, name: str = "Foo (2024).mkv"):
    media_dir = tmp_path / "movies"
    media_dir.mkdir(parents=True, exist_ok=True)
    media_file = media_dir / name
    media_file.write_bytes(b"placeholder")
    return media_file


class TestCheckOnlyTerminalOutcomes:
    """Terminal (non-generation) outcomes return as-is under check_only."""

    def test_no_owners_returns_terminal_not_needs_generation(self, mock_config_for_processing):
        result = process_canonical_path(
            canonical_path="/nope/Foo.mkv",
            registry=ServerRegistry(),
            config=mock_config_for_processing,
            check_only=True,
        )
        assert result.status is MultiServerStatus.NO_OWNERS

    def test_source_missing_returns_file_not_found(self, mock_config_for_processing, tmp_path):
        # Registry owns the folder, but no file exists on disk there.
        registry = _emby_registry(tmp_path)
        missing = str(tmp_path / "movies" / "Ghost (2024).mkv")
        result = process_canonical_path(
            canonical_path=missing,
            registry=registry,
            config=mock_config_for_processing,
            check_only=True,
        )
        assert result.status is MultiServerStatus.SKIPPED_FILE_NOT_FOUND

    def test_all_fresh_returns_skipped_without_ffmpeg(self, mock_config_for_processing, tmp_path):
        registry = _emby_registry(tmp_path)
        media_file = _seed_media(tmp_path)
        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=True,
            ),
            patch("media_preview_generator.processing.multi_server.generate_images") as mock_gen,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                check_only=True,
            )
        assert result.status is MultiServerStatus.SKIPPED
        mock_gen.assert_not_called()

    def test_pending_registration_returns_published_not_needs_generation(self, mock_config_for_processing, tmp_path):
        registry = _emby_registry(tmp_path)
        media_file = _seed_media(tmp_path)
        # Force the "outputs fresh but server still needs item registration"
        # branch: all paths fresh, and the server is classified as needing
        # registration with an unresolved id. That yields a PUBLISHED
        # (pending) aggregate — terminal, NOT NEEDS_GENERATION.
        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=True,
            ),
            patch(
                "media_preview_generator.processing.multi_server._server_needs_item_registration",
                return_value=True,
            ),
            patch("media_preview_generator.processing.multi_server.generate_images") as mock_gen,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                check_only=True,
            )
        assert result.status is MultiServerStatus.PUBLISHED
        mock_gen.assert_not_called()


class TestCheckOnlyNeedsGeneration:
    """Items that genuinely need FFmpeg report NEEDS_GENERATION and run none."""

    def test_stale_output_returns_needs_generation_without_ffmpeg(self, mock_config_for_processing, tmp_path):
        registry = _emby_registry(tmp_path)
        media_file = _seed_media(tmp_path)
        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=False,
            ),
            patch("media_preview_generator.processing.multi_server.generate_images") as mock_gen,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                check_only=True,
            )
        assert result.status is MultiServerStatus.NEEDS_GENERATION
        mock_gen.assert_not_called()

    def test_regenerate_returns_needs_generation_without_ffmpeg(self, mock_config_for_processing, tmp_path):
        registry = _emby_registry(tmp_path)
        media_file = _seed_media(tmp_path)
        with patch("media_preview_generator.processing.multi_server.generate_images") as mock_gen:
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                regenerate=True,
                check_only=True,
            )
        assert result.status is MultiServerStatus.NEEDS_GENERATION
        mock_gen.assert_not_called()


class TestCheckOnlyDoesNotChangeNormalPath:
    """Sanity: default check_only=False still extracts frames for stale items.

    Guards against the boundary insertion accidentally short-circuiting the
    real generation path.
    """

    def test_normal_call_still_generates_when_stale(self, mock_config_for_processing, tmp_path):
        registry = _emby_registry(tmp_path)
        media_file = _seed_media(tmp_path)

        def fake_generate_images(video_file, output_folder, *args, **kwargs):
            from pathlib import Path as _P

            _P(output_folder).mkdir(parents=True, exist_ok=True)
            (_P(output_folder) / "00000.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 64)
            return (True, 1, "h264", 1.0, 30.0, None)

        with (
            patch(
                "media_preview_generator.processing.multi_server.outputs_fresh_for_source",
                return_value=False,
            ),
            patch(
                "media_preview_generator.processing.multi_server.generate_images",
                side_effect=fake_generate_images,
            ) as mock_gen,
        ):
            result = process_canonical_path(
                canonical_path=str(media_file),
                registry=registry,
                config=mock_config_for_processing,
                # check_only defaults to False
            )
        mock_gen.assert_called()
        assert result.status is not MultiServerStatus.NEEDS_GENERATION
