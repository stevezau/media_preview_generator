"""Real FFmpeg proofs on tiny Matroska files with deliberately damaged indexes.

Run separately: pytest -m integration -n 0 --no-cov tests/integration/test_chapter_seek_recovery.py
Only temporary synthetic media and outputs are written. No server is required.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageChops

from media_preview_generator.bif_reader import read_bif_frame
from media_preview_generator.config import _resolve_ffmpeg_path
from media_preview_generator.output.journal import write_meta
from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters
from media_preview_generator.processing.generator import generate_bif
from media_preview_generator.servers.plex_chapters import Chapter, ChapterTarget

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]


def _elements(data: bytearray, start: int, end: int):
    """Locate known fixture fields without relying on the production validator."""

    def vint(at: int, *, identifier: bool = False) -> tuple[int, int]:
        width = next(n for n in range(1, 9) if data[at] & (0x100 >> n))
        value = int.from_bytes(data[at : at + width], "big")
        return value if identifier else value & ((1 << (7 * width)) - 1), at + width

    while start < end:
        element_id, at = vint(start, identifier=True)
        size, body = vint(at)
        assert body + size <= end
        yield element_id, body, body + size
        start = body + size


def _damage_index(source: Path, destination: Path, kind: str) -> None:
    data = bytearray(source.read_bytes())
    segment, end = next((a, z) for element_id, a, z in _elements(data, 0, len(data)) if element_id == 0x18538067)
    changed = 0
    for element_id, start, stop in _elements(data, segment, end):
        if kind == "seekhead" and element_id == 0x114D9B74:
            for child_id, first, last in _elements(data, start, stop):
                if child_id != 0x4DBB:
                    continue
                fields = {field: (a, z) for field, a, z in _elements(data, first, last)}
                id_start, id_end = fields[0x53AB]
                if int.from_bytes(data[id_start:id_end], "big") == 0x1C53BB6B:
                    a, z = fields[0x53AC]
                    offset = int.from_bytes(data[a:z], "big")
                    data[a:z] = (offset + 4).to_bytes(z - a, "big")
                    changed += 1
        if kind == "cluster" and element_id == 0x1C53BB6B:
            for child_id, first, last in _elements(data, start, stop):
                if child_id != 0xBB:
                    continue
                for field, a, z in _elements(data, first, last):
                    if field != 0xB7:
                        continue
                    for field, offset_start, offset_end in _elements(data, a, z):
                        if field == 0xF1:
                            offset = int.from_bytes(data[offset_start:offset_end], "big")
                            data[offset_start:offset_end] = (offset + 4).to_bytes(offset_end - offset_start, "big")
                            changed += 1
    assert changed > 0
    destination.write_bytes(data)


@pytest.fixture
def synthetic_movie(tmp_path):
    ffmpeg = _resolve_ffmpeg_path()
    if not ffmpeg:
        pytest.skip("Real FFmpeg is required for this integration proof")
    source = tmp_path / "valid.mkv"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=6",
            "-t",
            "8",
            "-c:v",
            "ffv1",
            "-threads",
            "1",
            "-cluster_time_limit",
            "1000",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    config = SimpleNamespace(
        ffmpeg_path=ffmpeg,
        ffmpeg_threads=1,
        thumbnail_quality=4,
        tonemap_algorithm="hable",
        log_level="INFO",
        plex_bif_frame_interval=1,
    )
    return source, config


def _linear_jpeg(source: Path, output: Path, config) -> None:
    subprocess.run(
        [
            config.ffmpeg_path,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-ss",
            "4",
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-q:v",
            "4",
            "-vf",
            "scale=w=1280:h=-2",
            str(output),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )


def test_valid_index_keeps_exact_direct_extraction(synthetic_movie, tmp_path):
    from media_preview_generator.processing.chapter_recovery import inspect_seek_index

    source, config = synthetic_movie
    before = source.read_bytes()
    assert inspect_seek_index(str(source), get_source_fingerprint(source), [4000]) is None
    expected, actual = tmp_path / "linear.jpg", tmp_path / "direct.jpg"
    _linear_jpeg(source, expected, config)
    chapters.extract_chapter_frame(str(source), 4000, actual, config)
    assert ImageChops.difference(Image.open(expected), Image.open(actual)).getbbox() is None
    assert source.read_bytes() == before


def test_corrupt_cluster_cannot_return_a_wrong_successful_frame(synthetic_movie, tmp_path):
    source, config = synthetic_movie
    damaged = tmp_path / "damaged.mkv"
    _damage_index(source, damaged, "cluster")
    expected, linear = tmp_path / "expected.jpg", tmp_path / "linear.jpg"
    _linear_jpeg(source, expected, config)
    _linear_jpeg(damaged, linear, config)
    assert ImageChops.difference(Image.open(expected), Image.open(linear)).getbbox() is None
    before = damaged.read_bytes()

    with pytest.raises(chapters.ChapterSourceCorruptionError):
        chapters.extract_chapter_frame(str(damaged), 4000, tmp_path / "wrong.jpg", config)

    assert damaged.read_bytes() == before


@pytest.mark.parametrize("damage", ["seekhead", "cluster"])
@pytest.mark.parametrize("bound_to_source", [True, False])
def test_damaged_index_reuses_only_source_bound_bif(synthetic_movie, tmp_path, monkeypatch, damage, bound_to_source):
    from media_preview_generator.processing.chapter_recovery import inspect_seek_index

    valid, config = synthetic_movie
    source = tmp_path / "damaged.mkv"
    _damage_index(valid, source, damage)
    fingerprint = get_source_fingerprint(source)
    defect = inspect_seek_index(str(source), fingerprint, [4000])
    assert defect
    folder = tmp_path / "Contents" / "Chapters"
    frames = tmp_path / "frames"
    frames.mkdir()
    subprocess.run(
        [
            config.ffmpeg_path,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            "fps=1,scale=320:-2",
            "-q:v",
            "4",
            "-threads",
            "1",
            "-filter_threads",
            "1",
            str(frames / "%05d.jpg"),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    bif = folder.parent / "Indexes" / "index-sd.bif"
    bif.parent.mkdir(parents=True)
    generate_bif(str(bif), str(frames), config)
    if bound_to_source:
        write_meta([bif], str(source), publisher="plex", source_fingerprint=fingerprint)
    target = ChapterTarget(
        1,
        2,
        3,
        calculate_plex_hash(source),
        str(source),
        source.stat().st_size,
        100,
        (Chapter(1, 4000, 6000, "", 10, 20),),
        "synthetic",
    )
    plan = chapters.ChapterPlan(object(), str(source), fingerprint, folder, {}, target)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)
    # The probe gives up after five seconds by design, and a loaded CI runner can hit that; reuse the
    # verdict proven above so this test covers what publish_chapters does with it.
    monkeypatch.setattr(chapters, "inspect_seek_index", lambda *_args, **_kwargs: defect)
    monkeypatch.setattr(
        chapters,
        "extract_chapter_frame",
        lambda *_args, **_kwargs: pytest.fail("A proven invalid seek index must not launch per-chapter FFmpeg"),
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    bif_hash = hashlib.sha256(bif.read_bytes()).hexdigest()

    result = chapters.publish_chapters(plan, config)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    assert hashlib.sha256(bif.read_bytes()).hexdigest() == bif_hash
    if not bound_to_source:
        assert (result.status, result.completed, result.total, result.retryable) == ("failed", 0, 1, False)
        register.assert_not_called()
        assert not (folder / "chapter1.jpg").exists()
        return
    assert (result.status, result.completed, result.total) == ("ready", 1, 1)
    assert "lower-resolution" in result.message
    entry = json.loads(plan.manifest_path.read_text())["images"]["1"]
    assert entry["recovery"] == {
        "source": "bif",
        "timestamp_ms": 4000,
        "original_dimensions": [320, 180],
        "cause": "corruption",
    }
    image = Image.open(io.BytesIO(read_bif_frame(str(bif), 4))).convert("RGB")
    expected = io.BytesIO()
    image.resize((1280, 720), Image.Resampling.LANCZOS).save(expected, "JPEG", quality=90)
    assert (folder / "chapter1.jpg").read_bytes() == expected.getvalue()
    register.assert_called_once()
    assert register.call_args.args[:2] == (plan.server, target)
    assert register.call_args.args[2] == {1: entry["sha256"]}
    assert callable(register.call_args.kwargs["verify_source"])
