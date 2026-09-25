"""Tests for the Emby sidecar BIF output adapter."""

from __future__ import annotations

import builtins
import errno
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from media_preview_generator.output import BifBundle, EmbyBifAdapter
from media_preview_generator.processing.generator import _bif_temp_path
from media_preview_generator.processing.multi_server import cleanup_orphaned_outputs
from media_preview_generator.servers.base import ServerConfig, ServerType


def _make_bundle(canonical_path: str, frame_dir: Path) -> BifBundle:
    return BifBundle(
        canonical_path=canonical_path,
        frame_dir=frame_dir,
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=0,
    )


class TestNeedsServerMetadata:
    def test_returns_false(self):
        adapter = EmbyBifAdapter()
        # Emby sidecar paths are derived purely from the media path; no
        # API roundtrip needed.
        assert adapter.needs_server_metadata() is False

    def test_name(self):
        assert EmbyBifAdapter().name == "emby_sidecar"


class TestComputeOutputPaths:
    def test_default_naming(self, tmp_path):
        adapter = EmbyBifAdapter()
        paths = adapter.compute_output_paths(
            _make_bundle("/m/Foo (2024)/Foo (2024).mkv", tmp_path),
            MagicMock(),
            item_id=None,
        )
        assert paths == [Path("/m/Foo (2024)/Foo (2024)-320-10.bif")]

    def test_respects_width_and_interval(self, tmp_path):
        adapter = EmbyBifAdapter(width=480, frame_interval=5)
        paths = adapter.compute_output_paths(
            _make_bundle("/m/Foo.mkv", tmp_path),
            MagicMock(),
            item_id=None,
        )
        assert paths == [Path("/m/Foo-480-5.bif")]

    def test_handles_episode_paths_with_dashes(self, tmp_path):
        adapter = EmbyBifAdapter()
        paths = adapter.compute_output_paths(
            _make_bundle("/m/Show/S01/Show - S01E01 - Pilot.mkv", tmp_path),
            MagicMock(),
            item_id=None,
        )
        assert paths == [Path("/m/Show/S01/Show - S01E01 - Pilot-320-10.bif")]


class TestPublish:
    def test_writes_bif_with_emby_filename(self, tmp_path):
        # Arrange: a frame dir + an "existing" media file whose sibling
        # we expect the BIF to land beside.
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        for i in range(3):
            (frame_dir / f"{i:05d}.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        media_dir = tmp_path / "Movies" / "Test (2024)"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "Test (2024).mkv"
        media_file.write_bytes(b"")  # placeholder; publish doesn't touch it

        adapter = EmbyBifAdapter(width=320, frame_interval=10)
        bundle = BifBundle(
            canonical_path=str(media_file),
            frame_dir=frame_dir,
            bif_path=None,
            frame_interval=10,
            width=320,
            height=180,
            frame_count=3,
        )
        out_path = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)[0]

        # Act
        adapter.publish(bundle, [out_path])

        # Assert: exact Emby filename + valid BIF magic.
        assert out_path == media_dir / "Test (2024)-320-10.bif"
        assert out_path.exists()
        magic = out_path.read_bytes()[:8]
        assert magic == bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])

    def test_creates_missing_parent_dir(self, tmp_path):
        # Arrange: frame dir exists but the target *parent* doesn't yet.
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        (frame_dir / "00000.jpg").write_bytes(b"\xff\xd8\xff")

        adapter = EmbyBifAdapter()
        out_path = tmp_path / "new_dir" / "Foo-320-10.bif"

        bundle = BifBundle(
            canonical_path=str(tmp_path / "new_dir" / "Foo.mkv"),
            frame_dir=frame_dir,
            bif_path=None,
            frame_interval=10,
            width=320,
            height=180,
            frame_count=1,
        )

        adapter.publish(bundle, [out_path])

        assert out_path.exists()

    def test_empty_output_paths_raises(self, tmp_path):
        adapter = EmbyBifAdapter()
        bundle = _make_bundle("/m/Foo.mkv", tmp_path)
        with pytest.raises(ValueError):
            adapter.publish(bundle, [])


class TestStaticHelpers:
    def test_sidecar_path(self):
        assert EmbyBifAdapter.sidecar_path(
            "/m/Foo (2024)/Foo (2024).mkv",
            width=320,
            frame_interval=10,
        ) == Path("/m/Foo (2024)/Foo (2024)-320-10.bif")


class TestPublishWriteDenied:
    """A media mount Emby can't write to must log an actionable hint.

    The sidecar lives beside the video, so the write fails when the ``.bif``
    is opened — the media folder already exists, so ``mkdir`` succeeds even
    on a read-only mount. EACCES (permission) and EROFS (``:ro`` mount) need
    different advice; any other OSError gets neither hint.
    """

    def _publish_with_open_error(self, tmp_path, monkeypatch, err_no: int) -> tuple[OSError, list[str]]:
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        (frame_dir / "00000.jpg").write_bytes(b"\xff\xd8\xff")
        media_dir = tmp_path / "Movies" / "Test (2024)"
        media_dir.mkdir(parents=True)
        media_file = media_dir / "Test (2024).mkv"
        media_file.write_bytes(b"")

        adapter = EmbyBifAdapter()
        bundle = _make_bundle(str(media_file), frame_dir)
        out_path = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)[0]

        real_open = builtins.open

        def fake_open(file, mode="r", *args, **kwargs):
            # Any file written in the media folder: the BIF goes in beside the sidecar and is renamed over it.
            if Path(file).parent == out_path.parent and "w" in mode:
                raise OSError(err_no, os.strerror(err_no), str(file))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fake_open)
        messages: list[str] = []
        sink_id = logger.add(lambda msg: messages.append(str(msg)), level="ERROR", format="{message}")
        try:
            with pytest.raises(OSError) as excinfo:
                adapter.publish(bundle, [out_path])
        finally:
            logger.remove(sink_id)
        return excinfo.value, messages

    def test_logs_mount_read_write_hint_when_media_mount_is_read_only(self, tmp_path, monkeypatch):
        exc, messages = self._publish_with_open_error(tmp_path, monkeypatch, errno.EROFS)

        assert exc.errno == errno.EROFS
        hint = next((m for m in messages if "read-only" in m), None)
        assert hint is not None, f"no read-only hint logged: {messages}"
        assert "media folder is mounted read-only" in hint
        assert "Emby" in hint
        assert "read-write" in hint

    def test_logs_permission_hint_when_media_folder_is_not_writable(self, tmp_path, monkeypatch):
        exc, messages = self._publish_with_open_error(tmp_path, monkeypatch, errno.EACCES)

        assert exc.errno == errno.EACCES
        hint = next((m for m in messages if "Emby preview file" in m), None)
        assert hint is not None, f"no Emby permission hint logged: {messages}"
        assert "permission denied" in hint
        assert "read-write" in hint

    def test_logs_no_mount_hint_when_error_is_unrelated(self, tmp_path, monkeypatch):
        exc, messages = self._publish_with_open_error(tmp_path, monkeypatch, errno.ENOSPC)

        assert exc.errno == errno.ENOSPC
        assert not [m for m in messages if "read-only" in m or "permission denied" in m], messages

    def test_logs_no_media_hint_when_frames_folder_is_unreadable(self, tmp_path, monkeypatch):
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        media_file = tmp_path / "Movies" / "Test.mkv"
        media_file.parent.mkdir()
        media_file.write_bytes(b"")
        adapter = EmbyBifAdapter()
        bundle = _make_bundle(str(media_file), frame_dir)
        out_path = adapter.compute_output_paths(bundle, MagicMock(), item_id=None)[0]

        def denied_listdir(path):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))

        monkeypatch.setattr(os, "listdir", denied_listdir)
        messages: list[str] = []
        sink_id = logger.add(lambda msg: messages.append(str(msg)), level="ERROR", format="{message}")
        try:
            with pytest.raises(PermissionError):
                adapter.publish(bundle, [out_path])
        finally:
            logger.remove(sink_id)

        assert not [m for m in messages if "Emby preview file" in m], messages


def _temp_name(sidecar: Path) -> str:
    """The name the generator writes ``sidecar`` under before renaming it into place."""
    return os.path.basename(_bif_temp_path(str(sidecar)))


def _aged(path: Path, seconds: float) -> Path:
    stamp = time.time() - seconds
    os.utime(path, (stamp, stamp), follow_symlinks=False)
    return path


def _emby_registry() -> MagicMock:
    registry = MagicMock()
    registry.configs.return_value = [
        ServerConfig(
            id="emby-1",
            type=ServerType.EMBY,
            name="Emby",
            enabled=True,
            url="http://localhost",
            auth={},
            output={},
        )
    ]
    return registry


class TestInterruptedWriteTempsSwept:
    """A crash while a sidecar BIF is written leaves the temp file ``generator._bif_temp_path`` names beside the video.

    The folder's orphan sweep removes it once no write can still be using it, and touches nothing else there.
    """

    DAY = 24 * 3600
    MINUTE = 60

    @pytest.fixture()
    def folder(self, tmp_path):
        folder = tmp_path / "Movies"
        folder.mkdir()
        (folder / "Movie.mkv").write_bytes(b"video")
        (folder / "Movie-320-10.bif").write_bytes(b"bif")
        return folder

    @pytest.mark.parametrize(
        "sidecar_name",
        [
            "Movie (2024)-320-10.bif",
            "Show - S01E01 - Épisode – ü-240-5.bif",
            "x" * 240 + "-320-10.bif",
        ],
        ids=["plain", "unicode", "long"],
    )
    def test_matcher_accepts_the_generators_temp_name(self, tmp_path, sidecar_name):
        from media_preview_generator.output.emby_sidecar import _BIF_TEMP_NAME

        assert _BIF_TEMP_NAME.fullmatch(_temp_name(tmp_path / sidecar_name))

    def test_stale_temps_are_swept_with_unlink_only(self, folder, mock_config, monkeypatch):
        # A regeneration of a live video that crashed, and a first write whose video has since gone. Never listed as
        # orphans (the generic removal would rmtree a directory that took the name meanwhile): the adapter unlinks
        # them itself.
        import shutil

        from media_preview_generator.output import emby_sidecar
        from media_preview_generator.processing import multi_server

        live_temp = folder / _temp_name(folder / "Movie-320-10.bif")
        gone_temp = folder / _temp_name(folder / "Gone-320-10.bif")
        for temp in (live_temp, gone_temp):
            temp.write_bytes(b"partial")
            _aged(temp, self.DAY)
        unlinked: list[str] = []
        real_unlink = emby_sidecar.os.unlink
        monkeypatch.setattr(emby_sidecar.os, "unlink", lambda path: unlinked.append(str(path)) or real_unlink(path))
        real_safe_remove = multi_server._safe_remove

        def safe_remove(path):
            if path.name.endswith(".bif-tmp"):
                pytest.fail(f"generic removal of {path}")
            return real_safe_remove(path)

        monkeypatch.setattr(multi_server, "_safe_remove", safe_remove)
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: pytest.fail("rmtree under a media folder"))

        assert EmbyBifAdapter().list_orphans_in_folder(folder, {"Movie"}) == []
        removed = cleanup_orphaned_outputs(
            str(folder / "Movie.mkv"), deleted_paths=None, registry=_emby_registry(), config=mock_config
        )

        assert sorted(removed) == sorted([live_temp, gone_temp])
        assert sorted(unlinked) == sorted([str(live_temp), str(gone_temp)])
        assert sorted(p.name for p in folder.iterdir()) == ["Movie-320-10.bif", "Movie.mkv"]

    @pytest.mark.parametrize("became", ["directory", "young-file", "symlink"])
    def test_a_temp_that_changed_since_it_was_listed_is_left_alone(self, folder, tmp_path, became):
        # Listed while stale, then replaced before the unlink: the check right before it sees what is there now.
        from unittest.mock import patch

        from media_preview_generator.output import emby_sidecar

        temp = folder / _temp_name(folder / "Gone-320-10.bif")
        temp.write_bytes(b"partial")
        _aged(temp, self.DAY)
        listed = emby_sidecar.EmbyBifAdapter._stale_write_temps(folder)
        assert listed == [temp]
        temp.unlink()
        if became == "directory":
            temp.mkdir()
            (temp / "keep.jpg").write_bytes(b"user")
            _aged(temp, self.DAY)
        elif became == "young-file":
            temp.write_bytes(b"a write in progress")
        else:
            outside = tmp_path / "elsewhere.bin"
            outside.write_bytes(b"user")
            temp.symlink_to(outside)
        with patch.object(emby_sidecar.EmbyBifAdapter, "_stale_write_temps", return_value=listed):
            assert EmbyBifAdapter().sweep_stale_write_temps(folder) == []
        assert temp.exists() or temp.is_symlink()
        if became == "directory":
            assert (temp / "keep.jpg").read_bytes() == b"user"

    def test_a_temp_a_write_may_still_be_using_is_kept(self, folder, mock_config):
        # Another worker writing a sibling episode's BIF in this folder right now.
        in_flight = folder / _temp_name(folder / "Other-320-10.bif")
        in_flight.write_bytes(b"partial")
        _aged(in_flight, self.MINUTE)

        assert EmbyBifAdapter().sweep_stale_write_temps(folder) == []
        cleanup_orphaned_outputs(
            str(folder / "Movie.mkv"), deleted_paths=None, registry=_emby_registry(), config=mock_config
        )
        assert in_flight.read_bytes() == b"partial"

    def test_look_alikes_links_and_folders_are_never_touched(self, folder, tmp_path, mock_config):
        real = _temp_name(folder / "Gone-320-10.bif")
        digest = real[1:17]
        look_alikes = [
            "movie-320-10.bif.bak",
            ".movie.bif-tmp",
            f".{digest[:15]}.bif-tmp",
            f".{digest}0.bif-tmp",
            ".0123456789ABCDEF.bif-tmp",
            f"{digest}.bif-tmp",
            f"x.{digest}.bif-tmp",
            f".{digest}.bif-tmp.old",
            f".{digest}.bif-tmp\n",
            f".{digest}.bif",
        ]
        for name in look_alikes:
            (folder / name).write_bytes(b"user")
            _aged(folder / name, self.DAY)
        same_name_dir = folder / real
        same_name_dir.mkdir()
        (same_name_dir / "keep.jpg").write_bytes(b"user")
        _aged(same_name_dir, self.DAY)
        outside_file = tmp_path / "elsewhere.bin"
        outside_file.write_bytes(b"user")
        outside_dir = tmp_path / "elsewhere"
        outside_dir.mkdir()
        (outside_dir / "keep.jpg").write_bytes(b"user")
        file_link = folder / _temp_name(folder / "Linked-320-10.bif")
        file_link.symlink_to(outside_file)
        dir_link = folder / _temp_name(folder / "LinkedDir-320-10.bif")
        dir_link.symlink_to(outside_dir, target_is_directory=True)
        for path in (outside_file, outside_dir, file_link, dir_link):
            _aged(path, self.DAY)
        before = sorted(p.name for p in folder.iterdir())

        assert EmbyBifAdapter().list_orphans_in_folder(folder, {"Movie"}) == []
        assert EmbyBifAdapter().sweep_stale_write_temps(folder) == []
        removed = cleanup_orphaned_outputs(
            str(folder / "Movie.mkv"), deleted_paths=None, registry=_emby_registry(), config=mock_config
        )

        assert removed == []
        assert sorted(p.name for p in folder.iterdir()) == before
        assert all((folder / name).read_bytes() == b"user" for name in look_alikes)
        assert (same_name_dir / "keep.jpg").read_bytes() == b"user"
        assert file_link.is_symlink() and outside_file.read_bytes() == b"user"
        assert dir_link.is_symlink() and (outside_dir / "keep.jpg").read_bytes() == b"user"
