"""BIF files are replaced atomically, keeping the replaced file's mode and owner.

``generate_bif`` used to truncate the final ``.bif`` and write into it. A
crash or error part-way left a short BIF at the path Plex and Emby read, and
with no ``.meta`` next to it the freshness check counts it as current. Now
the BIF is written beside the target and renamed over it once complete.

Writing in place kept the existing file's inode, so its mode and owner; the
replacement copies them so a regenerated BIF stays readable the same way.
"""

import builtins
import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.processing import generator
from media_preview_generator.processing.generator import generate_bif


def _frames(folder, count: int) -> str:
    os.makedirs(folder, exist_ok=True)
    for i in range(count):
        with open(os.path.join(folder, f"{i * 10:010d}.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8\xff" + bytes([i]) * 10)
    return str(folder)


def _image_count(bif_path) -> int:
    with open(bif_path, "rb") as fh:
        fh.seek(12)
        return struct.unpack("<I", fh.read(4))[0]


@pytest.fixture
def config():
    return SimpleNamespace(plex_bif_frame_interval=10)


class TestAtomicReplace:
    def test_new_bif_is_complete_with_nothing_left_beside_it(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"

        generate_bif(str(bif), _frames(tmp_path / "frames", 4), config)

        assert sorted(os.listdir(out_dir)) == ["index-sd.bif"]
        assert _image_count(bif) == 4

    def test_temp_file_written_in_the_target_folder(self, tmp_path, config):
        """Same folder, so the rename can't cross filesystems."""
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"

        with patch.object(generator.os, "replace", wraps=os.replace) as replace:
            generate_bif(str(bif), _frames(tmp_path / "frames", 2), config)

        src, dst = replace.call_args.args
        assert os.path.dirname(src) == str(out_dir)
        assert dst == str(bif)

    def test_leftover_temp_from_a_crash_is_replaced_not_kept(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        leftover = generator._bif_temp_path(str(bif))
        with open(leftover, "wb") as fh:
            fh.write(b"half a bif from a crash")

        generate_bif(str(bif), _frames(tmp_path / "frames", 3), config)

        assert sorted(os.listdir(out_dir)) == ["index-sd.bif"]
        assert _image_count(bif) == 3

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_leftover_temp_the_process_cant_open_is_replaced(self, tmp_path, config):
        """A leftover owned by another user (an earlier root run) must not block the write."""
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        leftover = generator._bif_temp_path(str(bif))
        with open(leftover, "wb") as fh:
            fh.write(b"stale")
        os.chmod(leftover, 0o444)

        generate_bif(str(bif), _frames(tmp_path / "frames", 2), config)

        assert sorted(os.listdir(out_dir)) == ["index-sd.bif"]
        assert _image_count(bif) == 2

    def test_long_sidecar_name_still_writes(self, tmp_path, config):
        """The temp name mustn't be longer than the target: an Emby sidecar name near the 255-byte limit worked
        when it was written in place."""
        out_dir = tmp_path / "Show"
        out_dir.mkdir()
        bif = out_dir / ("x" * 241 + "-320-10.bif")  # 252 bytes; 256 with a ".tmp" suffix

        generate_bif(str(bif), _frames(tmp_path / "frames", 2), config)

        assert sorted(os.listdir(out_dir)) == [bif.name]
        assert _image_count(bif) == 2

    def test_temp_name_is_hidden_and_fixed_per_target(self, tmp_path):
        a = generator._bif_temp_path(str(tmp_path / "Show" / "A-320-10.bif"))
        b = generator._bif_temp_path(str(tmp_path / "Show" / "B-320-10.bif"))

        assert os.path.dirname(a) == str(tmp_path / "Show")
        assert os.path.basename(a).startswith(".")
        assert a == generator._bif_temp_path(str(tmp_path / "Show" / "A-320-10.bif"))
        assert a != b


class TestModeAndOwnerKept:
    def test_replacing_keeps_existing_mode(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        bif.write_bytes(b"old")
        os.chmod(bif, 0o640)

        generate_bif(str(bif), _frames(tmp_path / "frames", 2), config)

        assert stat.S_IMODE(os.stat(bif).st_mode) == 0o640

    def test_new_file_gets_the_umask_mode_like_before(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        umask = os.umask(0o022)
        os.umask(umask)

        generate_bif(str(bif), _frames(tmp_path / "frames", 2), config)

        assert stat.S_IMODE(os.stat(bif).st_mode) == 0o666 & ~umask

    def test_replacing_gives_the_new_file_the_existing_owner(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        bif.write_bytes(b"old")
        real_stat = os.stat
        plex_owned = SimpleNamespace(st_mode=0o100644, st_uid=4242, st_gid=4343)

        def stat_seeing_plex_owner(path, *args, **kwargs):
            return plex_owned if str(path) == str(bif) else real_stat(path, *args, **kwargs)

        with (
            patch.object(generator.os, "stat", side_effect=stat_seeing_plex_owner),
            patch.object(generator.os, "chown", create=True) as chown,
        ):
            generate_bif(str(bif), _frames(tmp_path / "frames", 2), config)

        assert chown.call_args_list[0].args[1:] == (4242, 4343)
        assert os.path.dirname(chown.call_args_list[0].args[0]) == str(out_dir)

    def test_group_kept_when_owner_change_not_permitted(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        bif.write_bytes(b"old")
        real_stat = os.stat
        plex_owned = SimpleNamespace(st_mode=0o100640, st_uid=4242, st_gid=4343)

        def stat_seeing_plex_owner(path, *args, **kwargs):
            return plex_owned if str(path) == str(bif) else real_stat(path, *args, **kwargs)

        def chown_as_non_root(path, uid, gid):
            if uid != -1:
                raise PermissionError(1, "Operation not permitted", path)

        with (
            patch.object(generator.os, "stat", side_effect=stat_seeing_plex_owner),
            patch.object(generator.os, "chown", create=True, side_effect=chown_as_non_root) as chown,
        ):
            generate_bif(str(bif), _frames(tmp_path / "frames", 2), config)

        assert [c.args[1:] for c in chown.call_args_list] == [(4242, 4343), (-1, 4343)]
        assert sorted(os.listdir(out_dir)) == ["index-sd.bif"]


class TestForcedToDisk:
    """The BIF's data is on disk before its name is: a power loss can't leave a 0-byte BIF in place."""

    def test_fsyncs_the_temp_file_before_renaming_it_into_place(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            synced = os.fstat(fd)
            events.append(("fsync", synced.st_ino, synced.st_size))
            real_fsync(fd)

        def replace(src, dst):
            events.append(("replace", os.stat(src).st_ino, str(dst)))
            real_replace(src, dst)

        with patch.object(generator.os, "fsync", side_effect=fsync), patch.object(generator.os, "replace", replace):
            generate_bif(str(bif), _frames(tmp_path / "frames", 3), config)

        written = os.stat(bif)
        assert events == [("fsync", written.st_ino, written.st_size), ("replace", written.st_ino, str(bif))]
        assert _image_count(bif) == 3

    def test_bif_still_published_when_fsync_is_not_supported(self, tmp_path, config):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"

        with patch.object(generator.os, "fsync", side_effect=OSError(22, "Invalid argument")) as fsync:
            generate_bif(str(bif), _frames(tmp_path / "frames", 3), config)

        fsync.assert_called_once()
        assert sorted(os.listdir(out_dir)) == ["index-sd.bif"]
        assert _image_count(bif) == 3


def _single_frame(tmp_path: Path) -> tuple[Path, bytes]:
    frames = tmp_path / "single-frame"
    frames.mkdir()
    image = b"\xff\xd8\xffnew preview\xff\xd9"
    (frames / "0000000000.jpg").write_bytes(image)
    return frames, image


class TestPublication:
    def test_complete_bif_replaces_old_file_atomically(self, tmp_path, config, monkeypatch):
        frames, image = _single_frame(tmp_path)
        destination = tmp_path / "index-sd.bif"
        destination.write_bytes(b"previous complete BIF")
        replace = os.replace
        replacements = []

        def observe_replace(source, target):
            staging = Path(source)
            assert staging.parent == destination.parent
            assert Path(target) == destination
            assert destination.read_bytes() == b"previous complete BIF"
            content = staging.read_bytes()
            assert content[:8] == b"\x89BIF\r\n\x1a\n"
            assert struct.unpack_from("<III", content, 8) == (0, 1, 10000)
            assert struct.unpack_from("<II", content, 64) == (0, 80)
            assert struct.unpack_from("<II", content, 72) == (0xFFFFFFFF, 80 + len(image))
            assert content[80:] == image
            replacements.append((source, target))
            replace(source, target)

        monkeypatch.setattr(generator.os, "replace", observe_replace)
        with destination.open("rb") as existing_reader:
            generate_bif(str(destination), str(frames), config)
            assert existing_reader.read() == b"previous complete BIF"
        assert len(replacements) == 1
        assert destination.read_bytes()[80:] == image
        assert not list(tmp_path.glob(".*.bif-tmp"))

    @pytest.mark.parametrize("existing", [False, True])
    @pytest.mark.parametrize("failure", ["missing_image", "replace"])
    def test_failed_publication_preserves_destination_and_removes_staging(
        self, tmp_path, config, monkeypatch, existing, failure
    ):
        frames, _ = _single_frame(tmp_path)
        destination = tmp_path / "index-sd.bif"
        if existing:
            destination.write_bytes(b"previous complete BIF")

        def fail_image_read(path, mode="r", *args, **kwargs):
            if mode == "rb" and Path(path).suffix == ".jpg":
                raise FileNotFoundError("thumbnail disappeared after indexing")
            return builtins.open(path, mode, *args, **kwargs)

        def fail_replace(source, target):
            assert Path(source).parent == destination.parent
            assert Path(target) == destination
            raise OSError("publish failed")

        if failure == "missing_image":
            monkeypatch.setattr(generator, "open", fail_image_read, raising=False)
        else:
            monkeypatch.setattr(generator.os, "replace", fail_replace)

        with pytest.raises(OSError):
            generate_bif(str(destination), str(frames), config)

        if existing:
            assert destination.read_bytes() == b"previous complete BIF"
        else:
            assert not destination.exists()
        assert not list(tmp_path.glob(".*.bif-tmp"))

    @pytest.mark.parametrize("reject", [False, True])
    def test_before_publish_checks_complete_staging_before_replacing_destination(self, tmp_path, config, reject):
        frames, image = _single_frame(tmp_path)
        destination = tmp_path / "index-sd.bif"
        destination.write_bytes(b"previous complete BIF")
        checked = []

        def check_source():
            staging = list(tmp_path.glob(".*.bif-tmp"))
            assert len(staging) == 1
            assert staging[0].read_bytes()[80:] == image
            assert destination.read_bytes() == b"previous complete BIF"
            checked.append(True)
            if reject:
                raise OSError("source changed during packing")

        if reject:
            with pytest.raises(OSError, match="source changed"):
                generate_bif(str(destination), str(frames), config, before_publish=check_source)
            assert destination.read_bytes() == b"previous complete BIF"
        else:
            generate_bif(str(destination), str(frames), config, before_publish=check_source)
            assert destination.read_bytes()[80:] == image
        assert checked == [True]
        assert not list(tmp_path.glob(".*.bif-tmp"))

    def test_concurrent_writers_to_shared_bundle_keep_independent_complete_staging(self, tmp_path, config):
        """Identical sources in different folders can publish one Plex hash concurrently."""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier

        destination = tmp_path / "index-sd.bif"
        destination.write_bytes(b"previous complete BIF")
        ready = Barrier(2)
        payloads = [b"first source JPEG", b"second source JPEG"]
        folders = []
        for index, payload in enumerate(payloads):
            folder = tmp_path / f"frames-{index}"
            folder.mkdir()
            (folder / "0000000000.jpg").write_bytes(payload)
            folders.append(folder)

        def publish(folder):
            generate_bif(str(destination), str(folder), config, before_publish=lambda: ready.wait(timeout=5))

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(publish, folder) for folder in folders]
            for future in futures:
                future.result(timeout=10)

        assert destination.read_bytes()[80:] in payloads
        assert sorted(path.name for path in tmp_path.iterdir()) == ["frames-0", "frames-1", "index-sd.bif"]
