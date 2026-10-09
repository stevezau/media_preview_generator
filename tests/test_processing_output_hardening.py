"""Regressions for the processing/output hardening pass.

Covers: orphan sweep with video formats outside any allow-list, *arr deleted-path mapping,
Jellyfin trickplay sibling resolutions + staging cleanup, FFmpeg runner cleanup on a raising
callback, no full-frame retry after a stall, and fsync in ``atomic_json_save``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from media_preview_generator.output.base import BifBundle
from media_preview_generator.output.jellyfin_trickplay import JellyfinTrickplayAdapter
from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner
from media_preview_generator.processing.multi_server import cleanup_orphaned_outputs
from media_preview_generator.servers.base import ServerConfig, ServerType
from media_preview_generator.utils import atomic_json_save


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return path


def _registry(types: list[ServerType], path_mappings: list[dict] | None = None) -> MagicMock:
    configs = [
        ServerConfig(
            id=f"srv-{i}",
            type=t,
            name=f"{t.value}-{i}",
            enabled=True,
            url="http://localhost",
            auth={},
            output={},
            path_mappings=path_mappings or [],
        )
        for i, t in enumerate(types)
    ]
    registry = MagicMock()
    registry.configs.return_value = configs
    return registry


class TestSweepKeepsUnlistedVideoFormats:
    def test_flv_sidecars_survive_while_true_orphan_is_removed(self, tmp_path, mock_config):
        _touch(tmp_path / "a.mkv")
        _touch(tmp_path / "b.flv")
        flv_bif = _touch(tmp_path / "b-320-10.bif")
        flv_trickplay = tmp_path / "b.trickplay"
        flv_trickplay.mkdir()
        orphan = _touch(tmp_path / "gone-320-10.bif")

        removed = cleanup_orphaned_outputs(
            str(tmp_path / "a.mkv"),
            deleted_paths=None,
            registry=_registry([ServerType.EMBY, ServerType.JELLYFIN]),
            config=mock_config,
        )

        assert flv_bif.exists()
        assert flv_trickplay.exists()
        assert not orphan.exists()
        assert orphan in removed

    @pytest.mark.parametrize("ext", [".iso", ".vob", ".3gp", ".strm", ".unknownvideo"])
    def test_any_non_sidecar_extension_counts_as_live_media(self, tmp_path, mock_config, ext):
        _touch(tmp_path / "a.mkv")
        _touch(tmp_path / f"b{ext}")
        bif = _touch(tmp_path / "b-320-10.bif")

        cleanup_orphaned_outputs(
            str(tmp_path / "a.mkv"), deleted_paths=None, registry=_registry([ServerType.EMBY]), config=mock_config
        )

        assert bif.exists()


class TestTargetedCleanupMapsArrPaths:
    def test_arr_path_is_mapped_to_local_folder(self, tmp_path, mock_config):
        local = tmp_path / "local"
        old_trickplay = local / "Movie-OLD.trickplay"
        old_trickplay.mkdir(parents=True)
        _touch(local / "Movie-NEW.mkv")
        mappings = [{"remote_prefix": "/arr/media", "local_prefix": str(local)}]

        removed = cleanup_orphaned_outputs(
            str(local / "Movie-NEW.mkv"),
            deleted_paths=["/arr/media/Movie-OLD.mkv"],
            registry=_registry([ServerType.JELLYFIN], mappings),
            config=mock_config,
        )

        assert old_trickplay in removed
        assert not old_trickplay.exists()


def _bundle(media: Path, frame_dir: Path, count: int) -> BifBundle:
    frame_dir.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        Image.new("RGB", (320, 180), (i, 0, 0)).save(frame_dir / f"{i:05d}.jpg")
    return BifBundle(
        canonical_path=str(media),
        frame_dir=frame_dir,
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=count,
    )


class TestJellyfinPublishKeepsOtherResolutions:
    def test_sibling_resolution_dir_survives_republish(self, tmp_path):
        media = _touch(tmp_path / "Foo.mkv")
        sibling = tmp_path / "Foo.trickplay" / "640 - 10x10"
        sibling.mkdir(parents=True)
        (sibling / "0.jpg").write_bytes(b"keep")
        stale = tmp_path / "Foo.trickplay" / "320 - 10x10"
        stale.mkdir()
        (stale / "7.jpg").write_bytes(b"stale")

        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _bundle(media, tmp_path / "frames", 3)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]
        adapter.publish(bundle, [sheet0], item_id="x")

        assert (sibling / "0.jpg").read_bytes() == b"keep"
        assert sorted(p.name for p in stale.iterdir()) == ["0.jpg"]
        assert not any(p.name.startswith(".") for p in tmp_path.iterdir())

    def test_failed_pack_removes_staging_dir(self, tmp_path):
        media = _touch(tmp_path / "Foo.mkv")
        adapter = JellyfinTrickplayAdapter(width=320)
        bundle = _bundle(media, tmp_path / "frames", 3)
        sheet0 = adapter.compute_output_paths(bundle, MagicMock(), item_id="x")[0]

        with (
            patch(
                "media_preview_generator.output.jellyfin_trickplay._pack_sheets_into_dir", side_effect=OSError("full")
            ),
            pytest.raises(OSError),
        ):
            adapter.publish(bundle, [sheet0], item_id="x")

        assert not any(p.name.startswith(".") for p in tmp_path.iterdir())


def _runner_kwargs(tmp_path: Path, **overrides):
    config = SimpleNamespace(
        ffmpeg_path="/usr/bin/ffmpeg",
        plex_bif_frame_interval=10,
        thumbnail_quality=4,
        log_level="INFO",
        ffmpeg_threads=1,
    )
    kwargs = dict(
        video_file="/fake/source.mkv",
        output_folder=str(tmp_path),
        gpu=None,
        gpu_device_path=None,
        config=config,
        progress_callback=None,
        ffmpeg_threads_override=None,
        cancel_check=None,
        pause_check=None,
        path_kind="sdr",
        libplacebo_vf=None,
        use_libplacebo=False,
        dv5_software_fallback=False,
        base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
        fps_filter="fps=fps=0.1:round=up",
        hdr10_zscale_chain="",
    )
    kwargs.update(overrides)
    return kwargs


class TestFfmpegRunnerCleanup:
    def test_raising_callback_kills_ffmpeg(self, tmp_path):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.pid = 4242

        def boom(*_a, **_kw):
            raise RuntimeError("callback failed")

        with patch("subprocess.Popen", return_value=proc):
            runner = create_ffmpeg_runner(**_runner_kwargs(tmp_path, progress_callback=boom))
            with pytest.raises(RuntimeError, match="callback failed"):
                runner(use_skip=False, init_vulkan=False)

        proc.kill.assert_called_once()

    def test_popen_failure_surfaces_the_real_error(self, tmp_path):
        with patch("subprocess.Popen", side_effect=FileNotFoundError("no ffmpeg")):
            runner = create_ffmpeg_runner(**_runner_kwargs(tmp_path))
            with pytest.raises(FileNotFoundError):
                runner(use_skip=False, init_vulkan=False)


class TestAtomicJsonSaveFsyncs:
    def test_file_is_fsynced_before_replace(self, tmp_path):
        target = tmp_path / "settings.json"
        calls: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            calls.append("fsync")
            return real_fsync(fd)

        def spy_replace(src, dst):
            calls.append("replace")
            return real_replace(src, dst)

        with patch("os.fsync", side_effect=spy_fsync), patch("os.replace", side_effect=spy_replace):
            atomic_json_save(str(target), {"a": 1})

        assert calls.index("fsync") < calls.index("replace")
        assert json.loads(target.read_text()) == {"a": 1}


class TestStallDoesNotRetryFullFrame:
    def test_stalled_first_run_is_not_rerun_without_skip_frame(self, tmp_path, mock_config):
        from media_preview_generator.processing import generate_images
        from media_preview_generator.processing.ffmpeg_runner import STALL_WATCHDOG_LINE

        info = MagicMock()
        info.video_tracks = [MagicMock(hdr_format=None)]
        fake_runner = MagicMock(return_value=(1, 300.0, 0.0, [STALL_WATCHDOG_LINE]))

        with (
            patch("media_preview_generator.processing.generator.MediaInfo") as mediainfo,
            patch("media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner", return_value=fake_runner),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch("os.path.exists", return_value=True),
        ):
            mediainfo.parse.return_value = info
            try:
                generate_images("/test/video.mkv", str(tmp_path), None, None, mock_config)
            except Exception:  # noqa: BLE001 - the outcome after a stall is not under test, only the rerun count
                pass

        assert fake_runner.call_count == 1
