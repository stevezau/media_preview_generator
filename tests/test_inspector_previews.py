"""Where each server keeps a file's preview (Plex bundle, Emby sidecar, Jellyfin trickplay) and what it holds."""

from __future__ import annotations

import os
import struct
import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.bif_reader import BIF_MAGIC
from media_preview_generator.inspector.previews import (
    NO_CONFIG_FOLDER_NOTE,
    NO_JELLYFIN_FOLDER_NOTE,
    NOT_ANALYSED_NOTE,
    chosen_preview,
    file_previews,
    server_preview,
)
from media_preview_generator.servers.base import Library, ServerConfig, ServerType
from media_preview_generator.servers.ownership import OwnershipMatch

MTIME = 1_780_000_000.0


def _write_bif(path, *, frames: int, multiplier_ms: int = 10_000) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    jpegs = [b"\xff\xd8\xff" + bytes([i]) * 10 for i in range(frames)]
    header = BIF_MAGIC + struct.pack("<III", 0, frames, multiplier_ms) + b"\x00" * 44
    offset = len(header) + 8 * (frames + 1)
    index = b""
    for i, jpeg in enumerate(jpegs):
        index += struct.pack("<II", i, offset)
        offset += len(jpeg)
    index += struct.pack("<II", 0xFFFFFFFF, offset)
    with open(path, "wb") as f:
        f.write(header + index + b"".join(jpegs))
    os.utime(path, (MTIME, MTIME))
    return str(path)


def _config(sid: str, stype: ServerType, *, name: str | None = None, output=None, path_mappings=None) -> ServerConfig:
    return ServerConfig(
        id=sid,
        type=stype,
        name=name if name is not None else sid.title(),
        enabled=True,
        url="http://127.0.0.1:9",
        auth={},
        libraries=[Library("1", "Movies", ("/data/movies",))],
        output=output if output is not None else {"frame_interval": 10},
        path_mappings=path_mappings or [],
    )


def _matches(sid: str, *library_ids: str) -> list[OwnershipMatch]:
    return [OwnershipMatch(sid, lid, f"Library {lid}", "/data/movies") for lid in library_ids]


@pytest.fixture(autouse=True)
def no_resting_servers():
    """A server one test timed out mustn't sit out the next test's lookups."""
    from media_preview_generator.inspector import previews

    previews._resting_since.clear()
    yield
    previews._resting_since.clear()


@pytest.fixture
def video(tmp_path):
    folder = tmp_path / "media" / "movies" / "Film (2020)"
    folder.mkdir(parents=True)
    path = folder / "Film (2020).mkv"
    path.write_bytes(b"x")
    return str(path)


class TestEmby:
    def test_sidecar_next_to_the_video_with_facts_from_the_bif(self, video):
        cfg = _config("emby-1", ServerType.EMBY, output={"width": 320, "frame_interval": 10})
        sidecar = os.path.join(os.path.dirname(video), "Film (2020)-320-10.bif")
        _write_bif(sidecar, frames=12, multiplier_ms=10_000)

        row = server_preview(cfg, None, video, _matches("emby-1", "1"))

        assert row["path"] == sidecar
        assert row["kind"] == "bif"
        assert row["exists"] is True
        assert row["frame_count"] == 12
        assert row["interval_ms"] == 10_000
        assert row["file_size"] == os.path.getsize(sidecar)
        assert row["created_at"] == datetime.fromtimestamp(MTIME, tz=UTC).isoformat()
        assert (row["server_id"], row["server_name"], row["server_type"]) == ("emby-1", "Emby-1", "emby")
        assert row["error"] == "" and row["note"] == ""

    def test_sidecar_name_follows_the_servers_width_and_interval(self, video):
        cfg = _config("emby-1", ServerType.EMBY, output={"width": 480, "frame_interval": 5})

        row = server_preview(cfg, None, video, _matches("emby-1", "1"))

        assert row["path"] == os.path.join(os.path.dirname(video), "Film (2020)-480-5.bif")
        assert row["exists"] is False
        assert "frame_count" not in row

    def test_without_facts_only_the_header_frame_count_is_read(self, video):
        cfg = _config("emby-1", ServerType.EMBY)
        _write_bif(os.path.join(os.path.dirname(video), "Film (2020)-320-10.bif"), frames=7)

        row = server_preview(cfg, None, video, _matches("emby-1", "1"), with_facts=False)

        assert row["exists"] is True
        assert row["frame_count"] == 7
        assert "interval_ms" not in row and "file_size" not in row

    def test_unreadable_sidecar_is_an_error_row(self, video):
        cfg = _config("emby-1", ServerType.EMBY)
        with open(os.path.join(os.path.dirname(video), "Film (2020)-320-10.bif"), "wb") as f:
            f.write(b"not a bif at all")

        row = server_preview(cfg, None, video, _matches("emby-1", "1"))

        assert row["exists"] is True
        assert row["error"] == "This preview file couldn't be read"
        assert "frame_count" not in row


def _write_sheet(path, *, tiles_filled: int, tile_px: int = 16, grid: int = 10) -> None:
    img = Image.new("RGB", (tile_px * grid, tile_px * grid), (0, 0, 0))
    for i in range(tiles_filled):
        row, col = divmod(i, grid)
        img.paste((255, 255, 255), (col * tile_px, row * tile_px, (col + 1) * tile_px, (row + 1) * tile_px))
    img.save(path, "JPEG", quality=95)


class TestJellyfin:
    def test_save_with_media_is_the_trickplay_folder_next_to_the_video(self, video):
        cfg = _config("jf-1", ServerType.JELLYFIN, output={"width": 320, "frame_interval": 10})
        server = MagicMock()

        row = server_preview(cfg, server, video, _matches("jf-1", "1"))

        assert row["kind"] == "trickplay"
        assert row["path"] == os.path.join(os.path.dirname(video), "Film (2020).trickplay", "320 - 10x10")
        assert row["exists"] is False
        server.resolve_remote_path_to_item_id.assert_not_called()

    def test_trickplay_sheets_give_the_frame_count_and_the_servers_interval(self, video):
        cfg = _config("jf-1", ServerType.JELLYFIN, output={"width": 320, "frame_interval": 10})
        sheets = os.path.join(os.path.dirname(video), "Film (2020).trickplay", "320 - 10x10")
        os.makedirs(sheets)
        _write_sheet(os.path.join(sheets, "0.jpg"), tiles_filled=100)
        _write_sheet(os.path.join(sheets, "1.jpg"), tiles_filled=3)

        row = server_preview(cfg, MagicMock(), video, _matches("jf-1", "1"))

        assert row["exists"] is True
        assert row["frame_count"] == 103
        assert row["interval_ms"] == 10_000
        assert (row["tile_width"], row["tile_height"]) == (10, 10)
        assert row["sheets_dir"] == sheets
        assert row["file_size"] == sum(os.path.getsize(os.path.join(sheets, n)) for n in ("0.jpg", "1.jpg"))

    def test_off_media_without_a_config_folder_is_a_note(self, video):
        cfg = _config("jf-1", ServerType.JELLYFIN, output={"save_with_media": False, "frame_interval": 10})
        server = MagicMock()

        row = server_preview(cfg, server, video, _matches("jf-1", "1"))

        assert row["note"] == NO_JELLYFIN_FOLDER_NOTE
        assert (row["path"], row["exists"]) == ("", False)
        server.resolve_remote_path_to_item_id.assert_not_called()

    def test_off_media_is_under_jellyfins_config_folder_by_item_id(self, video, tmp_path):
        guid = "0123456789abcdef0123456789abcdef"
        cfg = _config(
            "jf-1",
            ServerType.JELLYFIN,
            output={"save_with_media": False, "jellyfin_config_folder": str(tmp_path / "jfcfg"), "frame_interval": 10},
        )
        server = MagicMock()
        server.resolve_remote_path_to_item_id.return_value = guid

        row = server_preview(cfg, server, video, _matches("jf-1", "1", "4"))

        server.resolve_remote_path_to_item_id.assert_called_once_with(video, library_ids=["1", "4"])
        # Jellyfin keeps the folder under the dashed GUID.
        assert row["path"] == str(
            tmp_path / "jfcfg" / "data" / "trickplay" / "01" / "01234567-89ab-cdef-0123-456789abcdef" / "320 - 10x10"
        )
        assert row["exists"] is False

    def test_off_media_item_not_found_is_a_note(self, video, tmp_path):
        cfg = _config(
            "jf-1",
            ServerType.JELLYFIN,
            name="Jelly",
            output={"save_with_media": False, "jellyfin_config_folder": str(tmp_path / "jfcfg"), "frame_interval": 10},
        )
        server = MagicMock()
        server.resolve_remote_path_to_item_id.return_value = None

        row = server_preview(cfg, server, video, _matches("jf-1", "1"))

        assert row["note"] == "Jelly doesn't list this file yet"
        assert row["path"] == ""


class TestPlex:
    @pytest.fixture
    def plex(self, tmp_path, video):
        media_movies = os.path.dirname(os.path.dirname(video))
        cfg = _config(
            "plex-1",
            ServerType.PLEX,
            name="Plex",
            output={"plex_config_folder": str(tmp_path / "plexcfg")},
            path_mappings=[{"remote_prefix": "/data/movies", "local_prefix": media_movies}],
        )
        server = MagicMock()
        server.resolve_remote_path_to_item_id.return_value = "123"
        server.get_bundle_metadata.return_value = [("abcdef0123", "/data/movies/Film (2020)/Film (2020).mkv")]
        return cfg, server

    def test_bif_is_under_the_config_folder_by_bundle_hash(self, plex, video, tmp_path):
        cfg, server = plex
        expected = os.path.join(
            str(tmp_path / "plexcfg"), "Media", "localhost", "a", "bcdef0123.bundle", "Contents", "Indexes"
        )
        _write_bif(os.path.join(expected, "index-sd.bif"), frames=9)

        row = server_preview(cfg, server, video, _matches("plex-1", "1", "2"))

        server.resolve_remote_path_to_item_id.assert_called_once_with(video, library_ids=["1", "2"])
        server.get_bundle_metadata.assert_called_once_with("123")
        assert row["path"] == os.path.join(expected, "index-sd.bif")
        assert row["item_id"] == "123"
        assert row["exists"] is True
        assert row["frame_count"] == 9
        assert row["versions"] == [video]

    def test_fallback_config_folder_when_the_server_has_none(self, plex, video, tmp_path):
        _cfg, server = plex
        cfg = _config("plex-1", ServerType.PLEX, name="Plex", output={})

        row = server_preview(cfg, server, video, _matches("plex-1", "1"), plex_config_folder=str(tmp_path / "fb"))

        assert row["path"].startswith(str(tmp_path / "fb" / "Media" / "localhost") + os.sep)

    def test_no_config_folder_anywhere_is_a_note(self, plex, video):
        _cfg, server = plex
        cfg = _config("plex-1", ServerType.PLEX, name="Plex", output={})

        row = server_preview(cfg, server, video, _matches("plex-1", "1"), plex_config_folder="")

        assert row["note"] == NO_CONFIG_FOLDER_NOTE
        server.resolve_remote_path_to_item_id.assert_not_called()

    @pytest.mark.parametrize(("name", "expected"), [("Plex", "Plex"), ("", "Plex"), ("Den", "Den")])
    def test_item_not_found_is_a_note(self, plex, video, name, expected):
        cfg, server = plex
        cfg.name = name
        server.resolve_remote_path_to_item_id.return_value = None

        row = server_preview(cfg, server, video, _matches("plex-1", "1"))

        assert row["note"] == f"{expected} doesn't list this file yet"
        assert (row["path"], row["exists"]) == ("", False)
        server.get_bundle_metadata.assert_not_called()

    @pytest.mark.parametrize("parts", [[], [("", "/data/movies/Film (2020)/Film (2020).mkv")]])
    def test_no_analysed_part_is_a_note(self, plex, video, parts):
        cfg, server = plex
        server.get_bundle_metadata.return_value = parts

        row = server_preview(cfg, server, video, _matches("plex-1", "1"))

        assert row["note"] == NOT_ANALYSED_NOTE
        assert row["path"] == ""

    def test_versions_list_only_local_files_that_exist(self, plex, video):
        cfg, server = plex
        folder = os.path.dirname(video)
        uhd = os.path.join(folder, "Film (2020) - 2160p.mkv")
        with open(uhd, "wb") as f:
            f.write(b"x")
        server.get_bundle_metadata.return_value = [
            ("abcdef0123", "/data/movies/Film (2020)/Film (2020).mkv"),
            ("bbcdef0123", "/data/movies/Film (2020)/Film (2020) - 2160p.mkv"),
            ("cbcdef0123", "/data/movies/Film (2020)/Film (2020) - 720p.mkv"),
            ("dbcdef0123", ""),
        ]

        row = server_preview(cfg, server, video, _matches("plex-1", "1"))

        assert row["versions"] == [video, uhd]
        assert os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(row["path"])))) == "bcdef0123.bundle"


class TestFilePreviews:
    def test_a_server_that_raises_marks_only_its_own_row(self, video):
        emby = _config("emby-1", ServerType.EMBY)
        _write_bif(os.path.join(os.path.dirname(video), "Film (2020)-320-10.bif"), frames=4)
        plex = _config("plex-1", ServerType.PLEX, name="Plex", output={"plex_config_folder": "/plexcfg"})
        broken = MagicMock()
        broken.resolve_remote_path_to_item_id.side_effect = RuntimeError("boom")

        rows = file_previews(video, [(plex, broken, _matches("plex-1", "1")), (emby, None, _matches("emby-1", "1"))])

        assert [r["server_id"] for r in rows] == ["plex-1", "emby-1"]
        assert rows[0]["error"] == "Couldn't reach Plex"
        assert (rows[0]["kind"], rows[0]["exists"]) == ("bif", False)
        assert rows[1]["error"] == ""
        assert (rows[1]["exists"], rows[1]["frame_count"]) == (True, 4)

    def test_a_server_that_hangs_past_the_timeout_is_an_error_row(self, video):
        emby = _config("emby-1", ServerType.EMBY)
        jf = _config(
            "jf-1",
            ServerType.JELLYFIN,
            name="Jelly",
            output={"save_with_media": False, "jellyfin_config_folder": "/jfcfg", "frame_interval": 10},
        )
        release = threading.Event()
        hanging = MagicMock()
        hanging.resolve_remote_path_to_item_id.side_effect = lambda *a, **k: release.wait(5) and None
        try:
            rows = file_previews(
                video,
                [(jf, hanging, _matches("jf-1", "1")), (emby, None, _matches("emby-1", "1"))],
                timeout_s=0.2,
            )
        finally:
            release.set()

        assert rows[0]["error"] == "Couldn't reach Jelly"
        assert rows[0]["kind"] == "trickplay"
        assert rows[1]["error"] == "" and rows[1]["server_id"] == "emby-1"

    def test_several_hanging_servers_are_waited_for_together(self, video):
        release = threading.Event()
        owners = []
        for n in range(3):
            cfg = _config(
                f"jf-{n}",
                ServerType.JELLYFIN,
                output={"save_with_media": False, "jellyfin_config_folder": "/jfcfg", "frame_interval": 10},
            )
            hanging = MagicMock()
            hanging.resolve_remote_path_to_item_id.side_effect = lambda *a, **k: release.wait(5) and None
            owners.append((cfg, hanging, _matches(cfg.id, "1")))
        started = time.monotonic()
        try:
            rows = file_previews(video, owners, timeout_s=0.5)
        finally:
            release.set()
        elapsed = time.monotonic() - started

        assert [r["error"] != "" for r in rows] == [True, True, True]
        # Waited together ≈ one timeout (0.5 s); one after another would be 1.5 s. The gap leaves room for a
        # loaded CI runner (0.3 s / < 0.6 s measured 0.70 s there).
        assert elapsed < 1.2

    def test_a_server_that_timed_out_rests_then_is_asked_again(self, video, monkeypatch):
        from media_preview_generator.inspector import previews

        jf = _config(
            "jf-1",
            ServerType.JELLYFIN,
            name="Jelly",
            output={"save_with_media": False, "jellyfin_config_folder": "/jfcfg", "frame_interval": 10},
        )
        release = threading.Event()
        hanging = MagicMock()
        hanging.resolve_remote_path_to_item_id.side_effect = lambda *a, **k: release.wait(5) and None
        try:
            file_previews(video, [(jf, hanging, _matches("jf-1", "1"))], timeout_s=0.1)
        finally:
            release.set()
        asked = hanging.resolve_remote_path_to_item_id.call_count

        [resting] = file_previews(video, [(jf, hanging, _matches("jf-1", "1"))], timeout_s=0.1)
        assert resting["error"] == "Couldn't reach Jelly"
        assert hanging.resolve_remote_path_to_item_id.call_count == asked

        monkeypatch.setattr(previews, "REST_S", 0.0)
        hanging.resolve_remote_path_to_item_id.side_effect = None
        hanging.resolve_remote_path_to_item_id.return_value = None
        [again] = file_previews(video, [(jf, hanging, _matches("jf-1", "1"))], timeout_s=1.0)
        assert again["error"] == ""
        assert hanging.resolve_remote_path_to_item_id.call_count == asked + 1

    def test_with_facts_is_passed_to_every_lookup(self, video):
        emby = _config("emby-1", ServerType.EMBY)
        _write_bif(os.path.join(os.path.dirname(video), "Film (2020)-320-10.bif"), frames=4)

        [row] = file_previews(video, [(emby, None, _matches("emby-1", "1"))], with_facts=False)

        assert row["frame_count"] == 4
        assert "interval_ms" not in row


def _preview_row(kind: str, *, exists=True, error="", frame_count=10, sid="s") -> dict:
    return {"server_id": sid, "kind": kind, "exists": exists, "error": error, "frame_count": frame_count}


class TestChosenPreview:
    def test_an_existing_bif_wins_over_an_earlier_trickplay(self):
        rows = [_preview_row("trickplay", sid="jf"), _preview_row("bif", sid="emby")]

        assert chosen_preview(rows)["server_id"] == "emby"

    def test_trickplay_when_no_bif_is_usable(self):
        rows = [_preview_row("bif", exists=False, sid="plex"), _preview_row("trickplay", sid="jf")]

        assert chosen_preview(rows)["server_id"] == "jf"

    @pytest.mark.parametrize(
        "bad",
        [
            {"error": "Couldn't reach Plex"},
            {"frame_count": 0},
            {"frame_count": None},
            {"exists": False},
        ],
    )
    def test_rows_with_an_error_no_frames_or_no_file_are_skipped(self, bad):
        rows = [_preview_row("bif", sid="first", **bad), _preview_row("bif", sid="second")]

        assert chosen_preview(rows)["server_id"] == "second"

    def test_none_when_nothing_is_usable(self):
        assert chosen_preview([_preview_row("bif", exists=False), _preview_row("trickplay", error="x")]) is None
        assert chosen_preview([]) is None
