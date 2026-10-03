"""Plex hash boundaries, bounded reads and source replacement protection."""

from __future__ import annotations

import builtins
import os
from pathlib import Path

import pytest

from media_preview_generator.output import plex_hash


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, "0da39a3ee5e6b4b0d3255bfef95601890afd80709"),
        (1, "15ba93c9db0cff93f52b521d7420e43f6eda2784f"),
        (65535, "65535fcdcbc0a3c69abbb021afa4a13ec39c4ad11cdb8"),
        (65536, "aff6a157e5ff3ee638798b72b13c1d906a99566c"),
        (65537, "4b745963cc290677dffc6a1fc86e714230ad1093"),
        (100000, "020b6c3c383316a5d9956b0f1151182c98b086bf"),
        (131072, "33c4cdfc4665490d74cbe8012874072018e428fb"),
        (131073, "4ff117ef9ced35051329a5702c8aa04d4abffe7c"),
    ],
)
def test_hash_vectors_at_block_boundaries(tmp_path: Path, size: int, expected: str) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(bytes(i % 251 for i in range(size)))

    assert plex_hash.calculate_plex_hash(source) == expected


def test_reads_at_most_two_blocks_and_reuses_stable_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 1_000_000)
    reads: list[int] = []
    real_open = builtins.open

    class RecordingReader:
        def __init__(self, filename: str, mode: str) -> None:
            self.file = real_open(filename, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, size: int):
            reads.append(size)
            return self.file.read(size)

        def seek(self, offset: int):
            return self.file.seek(offset)

    monkeypatch.setattr(plex_hash, "open", RecordingReader, raising=False)

    first = plex_hash.calculate_plex_hash(source)
    second = plex_hash.calculate_plex_hash(source)

    assert first == second
    assert reads == [65536, 65536]


@pytest.mark.parametrize("replace_inode", [False, True])
def test_same_size_replacement_with_preserved_mtime_invalidates_hash(tmp_path: Path, replace_inode: bool) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    previous = plex_hash.calculate_plex_hash(source)
    before = source.stat()
    target = tmp_path / "replacement.mkv" if replace_inode else source
    target.write_bytes(b"b" * 70000)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    if replace_inode:
        target.replace(source)

    assert plex_hash.calculate_plex_hash(source) != previous


def test_replacement_after_hashing_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    real_hash = plex_hash._hash_stable_source

    def replace_after_read(path, fingerprint):
        result = real_hash(path, fingerprint)
        replacement = tmp_path / "replacement.mkv"
        replacement.write_bytes(b"b" * 70000)
        replacement.replace(source)
        return result

    monkeypatch.setattr(plex_hash, "_hash_stable_source", replace_after_read)
    with pytest.raises(plex_hash.SourceFileChangedError, match="changed while hashing"):
        plex_hash.calculate_plex_hash(source)


def test_modification_during_hashing_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"a" * 70000)
    real_open = builtins.open

    class MutatingReader:
        def __init__(self, filename: str, mode: str) -> None:
            self.file = real_open(filename, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def read(self, size):
            result = self.file.read(size)
            with real_open(source, "ab") as writer:
                writer.write(b"replacement bytes")
            return result

        def seek(self, offset):
            return self.file.seek(offset)

    monkeypatch.setattr(plex_hash, "open", MutatingReader, raising=False)
    with pytest.raises(plex_hash.SourceFileChangedError, match="changed while hashing"):
        plex_hash.calculate_plex_hash(source)


def test_unreadable_source_propagates_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "video.mkv"
    source.write_bytes(b"video")

    def denied(*args, **kwargs):
        raise PermissionError("media mount unreadable")

    monkeypatch.setattr(plex_hash, "open", denied, raising=False)
    with pytest.raises(PermissionError, match="media mount unreadable"):
        plex_hash.calculate_plex_hash(source)


def test_non_file_source_rejected(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="not a regular file"):
        plex_hash.calculate_plex_hash(tmp_path)


def test_missing_source_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        plex_hash.calculate_plex_hash(tmp_path / "missing.mkv")
