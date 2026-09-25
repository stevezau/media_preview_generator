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

    def test_existing_bif_untouched_when_write_fails_midway(self, tmp_path, config, monkeypatch):
        out_dir = tmp_path / "Indexes"
        out_dir.mkdir()
        bif = out_dir / "index-sd.bif"
        bif.write_bytes(b"OLD-COMPLETE-BIF")
        frames = _frames(tmp_path / "frames", 4)
        broken = os.path.join(frames, f"{20:010d}.jpg")
        real_open = builtins.open

        def open_failing_on_third_frame(file, mode="r", *args, **kwargs):
            if str(file) == broken:
                raise OSError(5, "Input/output error", broken)
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", open_failing_on_third_frame)
        with pytest.raises(OSError):
            generate_bif(str(bif), frames, config)
        monkeypatch.setattr(builtins, "open", real_open)

        assert bif.read_bytes() == b"OLD-COMPLETE-BIF"
        assert sorted(os.listdir(out_dir)) == ["index-sd.bif"], "the partial write must not be left behind"

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
