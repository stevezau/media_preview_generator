"""BIF publication keeps complete files visible to concurrent readers."""

import builtins
import os
import stat
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_preview_generator.processing import generator


def _frames(tmp_path: Path) -> tuple[Path, bytes]:
    frames = tmp_path / "frames"
    frames.mkdir()
    image = b"\xff\xd8\xffnew preview\xff\xd9"
    (frames / "0000000000.jpg").write_bytes(image)
    return frames, image


def test_complete_bif_replaces_old_file_atomically(tmp_path, monkeypatch):
    frames, image = _frames(tmp_path)
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
        generator.generate_bif(str(destination), str(frames), SimpleNamespace(plex_bif_frame_interval=10))
        assert existing_reader.read() == b"previous complete BIF"
    assert len(replacements) == 1
    assert destination.read_bytes()[80:] == image
    assert not list(tmp_path.glob(".*.bif-tmp"))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("failure", ["missing_image", "replace"])
def test_failed_publication_preserves_destination_and_removes_staging(tmp_path, monkeypatch, existing, failure):
    frames, _ = _frames(tmp_path)
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
        generator.generate_bif(str(destination), str(frames), SimpleNamespace(plex_bif_frame_interval=10))

    if existing:
        assert destination.read_bytes() == b"previous complete BIF"
    else:
        assert not destination.exists()
    assert not list(tmp_path.glob(".*.bif-tmp"))


@pytest.mark.parametrize("existing", [False, True])
def test_published_bif_preserves_reader_permissions(tmp_path, existing):
    frames, _ = _frames(tmp_path)
    destination = tmp_path / "index-sd.bif"
    if existing:
        destination.write_bytes(b"previous complete BIF")
        destination.chmod(0o640)
        expected_mode = stat.S_IMODE(destination.stat().st_mode)
    else:
        reference = tmp_path / "normal-file"
        reference.write_bytes(b"normal open permissions")
        expected_mode = stat.S_IMODE(reference.stat().st_mode)

    generator.generate_bif(str(destination), str(frames), SimpleNamespace(plex_bif_frame_interval=10))

    assert stat.S_IMODE(destination.stat().st_mode) == expected_mode


@pytest.mark.parametrize("reject", [False, True])
def test_before_publish_checks_complete_staging_before_replacing_destination(tmp_path, reject):
    frames, image = _frames(tmp_path)
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
            generator.generate_bif(
                str(destination), str(frames), SimpleNamespace(plex_bif_frame_interval=10), before_publish=check_source
            )
        assert destination.read_bytes() == b"previous complete BIF"
    else:
        generator.generate_bif(
            str(destination), str(frames), SimpleNamespace(plex_bif_frame_interval=10), before_publish=check_source
        )
        assert destination.read_bytes()[80:] == image
    assert checked == [True]
    assert not list(tmp_path.glob(".*.bif-tmp"))


def test_concurrent_writers_to_shared_bundle_keep_independent_complete_staging(tmp_path):
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
        generator.generate_bif(
            str(destination),
            str(folder),
            SimpleNamespace(plex_bif_frame_interval=10),
            before_publish=lambda: ready.wait(timeout=5),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish, folder) for folder in folders]
        for future in futures:
            future.result(timeout=10)

    assert destination.read_bytes()[80:] in payloads
    assert sorted(path.name for path in tmp_path.iterdir()) == ["frames-0", "frames-1", "index-sd.bif"]
