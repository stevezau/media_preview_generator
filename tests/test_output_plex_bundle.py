"""Plex bundle output derives destinations from current media, without an API."""

from __future__ import annotations

import builtins
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from media_preview_generator.output import BifBundle, PlexBundleAdapter
from media_preview_generator.output.plex_hash import (
    SourceFileChangedError,
    calculate_plex_hash,
    get_source_fingerprint,
)
from media_preview_generator.servers import PlexServer


def _make_bundle(
    canonical_path: str,
    frame_dir: Path,
    *,
    prefetched_bundle_metadata: tuple[tuple[str, str], ...] = (),
) -> BifBundle:
    return BifBundle(
        canonical_path=canonical_path,
        frame_dir=frame_dir,
        bif_path=None,
        frame_interval=10,
        width=320,
        height=180,
        frame_count=0,
        prefetched_bundle_metadata=prefetched_bundle_metadata,
    )


class TestComputeOutputPaths:
    def test_no_server_metadata_required(self):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        assert adapter.needs_server_metadata() is False
        assert adapter.name == "plex_bundle"

    @pytest.mark.parametrize("item_id", [None, "42", "/library/metadata/42"])
    def test_resolves_without_server_or_item_id(self, tmp_path, item_id):
        media = tmp_path / "movie.mkv"
        media.write_bytes(b"current media" * 20000)
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        bundle_hash = calculate_plex_hash(media)

        paths = adapter.compute_output_paths(_make_bundle(str(media), tmp_path), None, item_id)

        assert paths == [
            Path(f"/cfg/Media/localhost/{bundle_hash[0]}/{bundle_hash[1:]}.bundle/Contents/Indexes/index-sd.bif")
        ]

    @pytest.mark.parametrize("item_id", [None, "42", "/library/metadata/42"])
    def test_never_contacts_plex(self, tmp_path, mock_config, item_id):
        media = tmp_path / "movie.mkv"
        media.write_bytes(b"current media" * 20000)
        server = PlexServer(mock_config)
        server._ensure_connected = MagicMock(side_effect=AssertionError("must not connect"))
        server.get_bundle_metadata = MagicMock(side_effect=AssertionError("must not request metadata"))
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)

        paths = adapter.compute_output_paths(_make_bundle(str(media), tmp_path), server, item_id)

        assert len(paths) == 1
        server._ensure_connected.assert_not_called()
        server.get_bundle_metadata.assert_not_called()

    @pytest.mark.parametrize("metadata_kind", ["exact_stale", "ambiguous", "unrelated", "invalid"])
    def test_prefetched_metadata_cannot_redirect_current_file(self, tmp_path, metadata_kind):
        media = tmp_path / "movie.mkv"
        media.write_bytes(b"replacement media" * 20000)
        metadata = {
            "exact_stale": (("a" * 40, str(media)),),
            "ambiguous": (("a" * 40, "/4k/movie.mkv"), ("b" * 40, "/1080/movie.mkv")),
            "unrelated": (("c" * 40, "/different/disc2.mkv"),),
            "invalid": (("", str(media)),),
        }[metadata_kind]
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        bundle = _make_bundle(str(media), tmp_path, prefetched_bundle_metadata=metadata)

        paths = adapter.compute_output_paths(bundle, None, "42")

        assert paths == [adapter.bundle_bif_path("/cfg", calculate_plex_hash(media))]
        assert paths[0] != adapter.bundle_bif_path("/cfg", "a" * 40)

    def test_same_basename_versions_use_their_own_bytes(self, tmp_path):
        paths = []
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        for name, content in [("4k", b"4K video"), ("1080p", b"1080p video")]:
            media = tmp_path / name / "movie.mkv"
            media.parent.mkdir()
            media.write_bytes(content * 20000)
            output = adapter.compute_output_paths(_make_bundle(str(media), tmp_path), None, "42")
            assert output == [adapter.bundle_bif_path("/cfg", calculate_plex_hash(media))]
            paths.append(output[0])
        assert paths[0] != paths[1]

    def test_replaced_file_gets_new_bundle(self, tmp_path):
        media = tmp_path / "movie.mkv"
        media.write_bytes(b"old content" * 20000)
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        bundle = _make_bundle(str(media), tmp_path)
        previous = adapter.compute_output_paths(bundle, None, None)
        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"new content" * 20000)
        replacement.replace(media)

        current = adapter.compute_output_paths(bundle, None, None)

        assert current != previous
        assert current == [adapter.bundle_bif_path("/cfg", calculate_plex_hash(media))]

    def test_missing_source_does_not_use_stale_metadata(self, tmp_path):
        media = tmp_path / "missing.mkv"
        bundle = _make_bundle(str(media), tmp_path, prefetched_bundle_metadata=(("a" * 40, str(media)),))
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        with pytest.raises(FileNotFoundError):
            adapter.compute_output_paths(bundle, None, "42")


class TestGetBundleMetadata:
    @pytest.mark.parametrize("item_id", ["557676", "/library/metadata/557676"])
    def test_existing_server_api_does_not_double_prefix_metadata_url(self, mock_config, item_id):
        server = PlexServer(mock_config)
        server._plex = MagicMock()
        server._plex.query.return_value = ET.fromstring(
            '<MediaContainer><MetadataItem><MediaItem><MediaPart hash="abcdef0123456789" '
            'file="/m/foo.mkv" /></MediaItem></MetadataItem></MediaContainer>'
        )

        assert server.get_bundle_metadata(item_id) == [("abcdef0123456789", "/m/foo.mkv")]
        server._plex.query.assert_called_once_with("/library/metadata/557676/tree")


# Real source files model single-part items, stacked parts, and identical
# copies. Expected hashes are fixed vectors, independent of the adapter.
_STACKED_PARTS = [("episode-pt1.mp4", b"A" * 70000), ("episode-pt2.mp4", b"B" * 70000)]
_COPY_PARTS = [
    ("copy-pt2.mkv", b"A" * 70000),
    ("original.mkv", b"A" * 70000),
    ("different-version.mkv", b"C" * 70000),
]


class TestOutputPathPerPart:
    """Each part uses its own bytes; identical copies share one Plex bundle."""

    @pytest.mark.parametrize(
        ("parts", "selected", "expected_hash"),
        [
            pytest.param([_STACKED_PARTS[0]], 0, "5fda157f4dfe306730d711ee01ae3279db0afe6c", id="single-part"),
            pytest.param(_STACKED_PARTS, 0, "5fda157f4dfe306730d711ee01ae3279db0afe6c", id="stacked-pt1"),
            pytest.param(_STACKED_PARTS, 1, "19588016240093667feb2b337ccafe4ae081a35a", id="stacked-pt2"),
            pytest.param(_COPY_PARTS, 0, "5fda157f4dfe306730d711ee01ae3279db0afe6c", id="copy-pt2"),
            pytest.param(_COPY_PARTS, 1, "5fda157f4dfe306730d711ee01ae3279db0afe6c", id="copy-original"),
            pytest.param(_COPY_PARTS, 2, "b49d6c4a7413613eccc8985005c46a7d421efdd6", id="different-version"),
        ],
    )
    @pytest.mark.parametrize("prefetched", [False, True])
    def test_output_path_is_the_parts_own_bundle(
        self, tmp_path, mock_config, parts, selected, expected_hash, prefetched
    ):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        server = PlexServer(mock_config)
        server._plex = MagicMock()
        server._plex.query.side_effect = AssertionError("must not call /tree")
        local_paths = []
        for filename, content in parts:
            local_path = tmp_path / filename
            local_path.write_bytes(content)
            local_paths.append(str(local_path))
        metadata = tuple(("f" * 40, path) for path in local_paths) if prefetched else ()
        bundle = _make_bundle(local_paths[selected], tmp_path, prefetched_bundle_metadata=metadata)
        expected = Path(
            f"/cfg/Media/localhost/{expected_hash[0]}/{expected_hash[1:]}.bundle/Contents/Indexes/index-sd.bif"
        )

        assert adapter.compute_output_paths(bundle, server, item_id="689756") == [expected]
        server._plex.query.assert_not_called()


class TestPublish:
    def test_source_replacement_during_packing_preserves_old_preview(self, tmp_path, monkeypatch):
        source = tmp_path / "movie.mkv"
        source.write_bytes(b"old media" * 20000)
        frames = tmp_path / "frames"
        frames.mkdir()
        frame = frames / "00000.jpg"
        frame.write_bytes(b"jpeg content")
        output = tmp_path / "index-sd.bif"
        output.write_bytes(b"previous complete preview")
        bundle = replace(
            _make_bundle(str(source), frames),
            source_fingerprint=get_source_fingerprint(source),
        )
        real_open = builtins.open

        def replacing_open(file, mode="r", *args, **kwargs):
            if Path(file) == frame and mode == "rb":
                source.write_bytes(b"replacement media" * 20000)
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", replacing_open)
        adapter = PlexBundleAdapter(str(tmp_path), frame_interval=10)

        with pytest.raises(SourceFileChangedError, match="changed before publishing"):
            adapter.publish(bundle, [output])

        assert output.read_bytes() == b"previous complete preview"
        assert sorted(path.name for path in tmp_path.iterdir()) == ["frames", "index-sd.bif", "movie.mkv"]

    def test_creates_parent_dirs_and_writes_bif(self, tmp_path):
        # Arrange: a frame dir with three small JPGs.
        frame_dir = tmp_path / "frames"
        frame_dir.mkdir()
        for i in range(3):
            (frame_dir / f"{i:05d}.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        out_dir = tmp_path / "deeply" / "nested" / "Indexes"
        out_path = out_dir / "index-sd.bif"

        adapter = PlexBundleAdapter(plex_config_folder=str(tmp_path), frame_interval=5)
        bundle = BifBundle(
            canonical_path="/m/foo.mkv",
            frame_dir=frame_dir,
            bif_path=None,
            frame_interval=5,
            width=320,
            height=180,
            frame_count=3,
        )

        # Act
        adapter.publish(bundle, [out_path])

        # Assert: directory was created and file is non-empty BIF.
        assert out_path.exists()
        data = out_path.read_bytes()
        assert data[:8] == bytes([0x89, 0x42, 0x49, 0x46, 0x0D, 0x0A, 0x1A, 0x0A])  # BIF magic

    def test_empty_output_paths_raises(self, tmp_path):
        adapter = PlexBundleAdapter(plex_config_folder="/cfg", frame_interval=10)
        bundle = _make_bundle("/m/foo.mkv", tmp_path)
        with pytest.raises(ValueError):
            adapter.publish(bundle, [])
