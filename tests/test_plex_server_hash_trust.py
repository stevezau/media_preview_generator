"""Bulk scans take Plex's bundle hash only when it provably describes this file."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from media_preview_generator.output import BifBundle, PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash

PLEX_HASH = "ab" + "c" * 38


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "Movie (2020).mkv"
    path.write_bytes(b"current media" * 20000)
    return path


def _paths(media: Path, parts, *, trust: bool = True, item_id: str | None = "42", server=True):
    fake = MagicMock()
    fake.get_bundle_parts.return_value = parts
    bundle = BifBundle(
        canonical_path=str(media),
        frame_dir=media.parent,
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=0,
        trust_server_hash=trust,
    )
    adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
    return adapter.compute_output_paths(bundle, fake if server else None, item_id)[0], fake


def _bundle_hash(path: Path) -> str:
    return path.parts[-5] + path.parts[-4].removesuffix(".bundle")


def _size(media: Path) -> int:
    return media.stat().st_size


class TestServerHashTrust:
    def test_matching_size_and_name_uses_plex_hash_without_reading_file(self, media, monkeypatch):
        monkeypatch.setattr(
            "media_preview_generator.output.plex_bundle.calculate_plex_hash",
            MagicMock(side_effect=AssertionError("must not read the file")),
        )
        path, plex = _paths(media, [(PLEX_HASH, f"/plex/{media.name}", _size(media))])
        assert _bundle_hash(path) == PLEX_HASH
        plex.get_bundle_parts.assert_called_once_with("42", quiet=True)

    def test_size_mismatch_reads_file(self, media):
        path, _ = _paths(media, [(PLEX_HASH, f"/plex/{media.name}", _size(media) + 1)])
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_no_plex_hash_yet_reads_file(self, media):
        path, _ = _paths(media, [])
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_other_filename_reads_file(self, media):
        path, _ = _paths(media, [(PLEX_HASH, "/plex/Other.mkv", _size(media))])
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_conflicting_hashes_read_file(self, media):
        parts = [(PLEX_HASH, f"/p/{media.name}", _size(media)), ("f" * 40, f"/q/{media.name}", _size(media))]
        path, _ = _paths(media, parts)
        assert _bundle_hash(path) == calculate_plex_hash(media)

    def test_multi_version_picks_matching_size(self, media):
        parts = [("f" * 40, "/p/Movie (2020) 4K.mkv", 999), (PLEX_HASH, f"/p/{media.name}", _size(media))]
        path, _ = _paths(media, parts)
        assert _bundle_hash(path) == PLEX_HASH

    def test_windows_remote_path(self, media):
        path, _ = _paths(media, [(PLEX_HASH, f"D:\\Movies\\{media.name}", _size(media))])
        assert _bundle_hash(path) == PLEX_HASH

    @pytest.mark.parametrize("kwargs", [{"trust": False}, {"item_id": None}, {"server": False}])
    def test_webhook_and_path_only_never_ask_plex(self, media, kwargs):
        path, plex = _paths(media, [(PLEX_HASH, f"/plex/{media.name}", _size(media))], **kwargs)
        assert _bundle_hash(path) == calculate_plex_hash(media)
        plex.get_bundle_parts.assert_not_called()


class TestBundleParts:
    def test_tree_parts_carry_size(self):
        from xml.etree import ElementTree as ET

        from media_preview_generator.servers.plex import PlexServer

        xml = ET.fromstring(
            '<MediaContainer><MediaPart hash="h1" file="/a/b.mkv" size="123"/><MediaPart hash="h2" file="/a/c.mkv"/></MediaContainer>'
        )
        server = PlexServer.__new__(PlexServer)
        server._connect = MagicMock(return_value=MagicMock(query=MagicMock(return_value=xml)))
        assert server.get_bundle_parts("/library/metadata/7") == [("h1", "/a/b.mkv", 123), ("h2", "/a/c.mkv", -1)]
        assert server.get_bundle_metadata("7") == [("h1", "/a/b.mkv"), ("h2", "/a/c.mkv")]
