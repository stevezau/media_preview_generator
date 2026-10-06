"""Chapter recovery must prove index damage and bind every reused frame to its source."""

import io
import struct
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.bif_reader import BIF_MAGIC
from media_preview_generator.output.journal import write_meta
from media_preview_generator.output.plex_hash import SourceFileChangedError, get_source_fingerprint
from media_preview_generator.processing import chapter_recovery as recovery
from media_preview_generator.processing import chapters


def element(eid, data):
    width = max(1, (len(data).bit_length() + 6) // 7)
    size = ((1 << (7 * width)) | len(data)).to_bytes(width, "big")
    return bytes.fromhex(eid) + size + data


def integer(eid, value):
    return element(eid, value.to_bytes(8, "big"))


def indexed_source(path, defect=None):
    info = element("1549a966", integer("2ad7b1", 1_000_000))
    tracks = element("1654ae6b", element("ae", integer("d7", 1) + integer("83", 1)))

    def seek(entries):
        return element(
            "114d9b74",
            b"".join(element("4dbb", element("53ab", bytes.fromhex(eid)) + integer("53ac", at)) for eid, at in entries),
        )

    def cues(at):
        return element(
            "1c53bb6b", element("bb", integer("b3", 0) + element("b7", integer("f7", 1) + integer("f1", at)))
        )

    ids = ["1549a966", "1654ae6b"] + ([] if defect == "absent" else ["1c53bb6b"])
    head_len = len(seek([(eid, 0) for eid in ids]))
    cues_at = head_len + len(info) + len(tracks)
    cluster_at = cues_at + len(cues(0))
    head = seek(
        [(ids[0], head_len), (ids[1], head_len + len(info))]
        + ([] if defect == "absent" else [(ids[2], cues_at + (4 if defect == "cues" else 0))])
    )
    body = (
        head
        + info
        + tracks
        + cues(cluster_at + (4 if defect == "cluster" else 0))
        + element("1f43b675", integer("e7", 0))
    )
    path.write_bytes(element("1a45dfa3", b"") + element("18538067", body))
    return get_source_fingerprint(path)


@pytest.mark.parametrize("defect,expected", [(None, None), ("absent", None), ("cues", "Cues"), ("cluster", "Cluster")])
def test_only_proven_advertised_index_damage_is_reported(tmp_path, defect, expected):
    path = tmp_path / "movie.mkv"
    fingerprint = indexed_source(path, defect)
    before = path.read_bytes()
    result = recovery._inspect_index(str(path), fingerprint, [1000])
    assert (expected in result) if expected else result is None
    assert path.read_bytes() == before
    assert get_source_fingerprint(path) == fingerprint


def test_source_changed_since_planning_is_not_classified_as_index_corruption(tmp_path):
    path = tmp_path / "movie.mkv"
    old = indexed_source(path, "cues")
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(SourceFileChangedError):
        recovery._inspect_index(str(path), old, [1000])


@pytest.fixture
def bound_bif(tmp_path):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"stable current source")
    folder = tmp_path / "Contents" / "Chapters"
    folder.mkdir(parents=True)
    bif = folder.parent / "Indexes" / "index-sd.bif"
    bif.parent.mkdir()
    images = []
    for color in ["red", "green", "blue"]:
        data = io.BytesIO()
        Image.new("RGB", (320, 180), color).save(data, "JPEG")
        images.append(data.getvalue())
    header = BIF_MAGIC + struct.pack("<III", 0, 3, 0) + bytes(44)
    offset = 64 + 4 * 8
    entries = []
    for stamp, jpeg in zip([0, 2, 4], images, strict=True):
        entries.append(struct.pack("<II", stamp, offset))
        offset += len(jpeg)
    bif.write_bytes(header + b"".join(entries) + struct.pack("<II", 0xFFFFFFFF, offset) + b"".join(images))
    plan = SimpleNamespace(folder=folder, canonical_path=str(source), source_fingerprint=get_source_fingerprint(source))
    write_meta([bif], str(source), source_fingerprint=plan.source_fingerprint)
    return plan, bif, folder / "out.jpg"


def test_verified_bif_recovery_has_real_in_chapter_timestamp_and_resolution_provenance(bound_bif):
    plan, bif, output = bound_bif
    before = bif.read_bytes()
    result = recovery.recover_bif_frame(plan, SimpleNamespace(start_ms=1000, end_ms=3000), output)
    assert result == {"source": "bif", "timestamp_ms": 2000, "original_dimensions": [320, 180]}
    with Image.open(output) as image:
        assert image.size == (1280, 720)
        assert image.getpixel((0, 0))[1] > 100
    assert bif.read_bytes() == before


@pytest.mark.parametrize("start,end", [(2100, 4000), (5000, 6000), (0, 0)])
def test_recovery_never_crosses_chapter_end_or_uses_a_frame_before_start(bound_bif, start, end):
    plan, _bif, output = bound_bif
    assert recovery.recover_bif_frame(plan, SimpleNamespace(start_ms=start, end_ms=end), output) is None
    assert not output.exists()


@pytest.mark.parametrize("damage", ["missing_journal", "stale_source", "sentinel", "offset", "timestamp", "jpeg"])
def test_untrusted_or_malformed_bif_is_never_recovered(bound_bif, damage):
    plan, bif, output = bound_bif
    data = bytearray(bif.read_bytes())
    if damage == "missing_journal":
        for meta in bif.parent.glob("*.meta"):
            meta.unlink()
    elif damage == "stale_source":
        source = Path(plan.canonical_path)
        source.write_bytes(b"replacement")
        plan.source_fingerprint = get_source_fingerprint(source)
    elif damage == "sentinel":
        struct.pack_into("<I", data, 88, 0)
    elif damage == "offset":
        struct.pack_into("<I", data, 76, 10)
    elif damage == "timestamp":
        struct.pack_into("<I", data, 72, 0)
    else:
        at = struct.unpack_from("<I", data, 76)[0]
        data[at : at + 10] = b"not a jpeg"
    if damage not in {"missing_journal", "stale_source"}:
        bif.write_bytes(data)
    assert recovery.recover_bif_frame(plan, SimpleNamespace(start_ms=1000, end_ms=3000), output) is None
    assert not output.exists()


def test_proven_index_damage_bypasses_ffmpeg_even_when_it_would_return_success(bound_bif, monkeypatch):
    plan, _bif, _output = bound_bif
    chapter = SimpleNamespace(index=1, start_ms=1000, end_ms=3000, thumb_url="")
    plan.target = SimpleNamespace(chapters=[chapter], machine_identifier="m", rating_key=1, media_id=2, part_id=3)
    plan.profile = {}
    plan.server = MagicMock()
    plan.manifest_path = plan.folder / "chapter-thumbnails.mpg.json"
    monkeypatch.setattr(chapters, "inspect_seek_index", lambda *a, **kw: "Invalid Cues")
    runner = MagicMock()
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)
    result = chapters.publish_chapters(plan, SimpleNamespace())
    assert result.status == "ready" and result.completed == 1
    assert "lower-resolution" in result.message
    runner.assert_not_called()
    assert callable(register.call_args.kwargs["verify_source"])
    register.call_args.kwargs["verify_source"]()
    assert chapters._fresh_images(plan)["1"]["recovery"]["timestamp_ms"] == 2000


def test_probe_deadline_kills_and_bounds_reaping_without_classifying_corruption(tmp_path, monkeypatch):
    path = tmp_path / "movie.mkv"
    fingerprint = indexed_source(path, "cues")
    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(recovery.subprocess, "Popen", MagicMock(return_value=proc))
    monkeypatch.setattr(recovery.time, "monotonic", MagicMock(side_effect=[0, 6]))
    assert recovery.inspect_seek_index(str(path), fingerprint, [1000]) is None
    proc.kill.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=1)
    proc.stdout.read.assert_not_called()
    proc.stdout.close.assert_called_once_with()


def test_probe_cancellation_is_not_silently_turned_into_unknown(tmp_path, monkeypatch):
    path = tmp_path / "movie.mkv"
    fingerprint = indexed_source(path)
    proc = MagicMock()
    proc.poll.return_value = None
    monkeypatch.setattr(recovery.subprocess, "Popen", MagicMock(return_value=proc))
    cancel_check = MagicMock(side_effect=[False, True])
    with pytest.raises(chapters.CancellationError):
        recovery.inspect_seek_index(str(path), fingerprint, [1000], cancel_check=cancel_check)
    proc.kill.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=1)


def test_recovery_rejects_unbounded_output_dimensions(bound_bif, monkeypatch):
    plan, bif, output = bound_bif
    data = io.BytesIO()
    Image.new("RGB", (1, 10000)).save(data, "JPEG")
    jpeg = data.getvalue()
    bif.write_bytes(
        BIF_MAGIC
        + struct.pack("<III", 0, 1, 1000)
        + bytes(44)
        + struct.pack("<IIII", 1, 80, 0xFFFFFFFF, 80 + len(jpeg))
        + jpeg
    )
    resize = MagicMock(side_effect=AssertionError("Must reject before allocation"))
    monkeypatch.setattr(Image.Image, "resize", resize)
    assert recovery.recover_bif_frame(plan, SimpleNamespace(start_ms=1000, end_ms=3000), output) is None
    resize.assert_not_called()


def test_one_failed_seek_switches_remaining_chapters_to_safe_recovery(bound_bif, monkeypatch):
    plan, _bif, _output = bound_bif
    plan.target = SimpleNamespace(
        chapters=[
            SimpleNamespace(index=i + 1, start_ms=i * 2000, end_ms=(i + 1) * 2000, thumb_url="") for i in range(3)
        ],
        machine_identifier="m",
        rating_key=1,
        media_id=2,
        part_id=3,
    )
    plan.profile = {}
    plan.server = MagicMock()
    plan.manifest_path = plan.folder / "chapter-thumbnails.mpg.json"
    monkeypatch.setattr(chapters, "inspect_seek_index", lambda *a, **kw: None)
    monkeypatch.setattr(
        chapters.MediaInfo,
        "parse",
        lambda _: SimpleNamespace(
            video_tracks=[SimpleNamespace(duration=6000, transfer_characteristics=None, hdr_format=None)]
        ),
    )
    runner = MagicMock(return_value=lambda **kw: (-9, 0, "", [chapters.STALL_WATCHDOG_LINE]))
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)
    config = SimpleNamespace(ffmpeg_threads=2, tonemap_algorithm="hable")
    result = chapters.publish_chapters(plan, config)
    assert (result.status, result.completed, result.total) == ("ready", 3, 3)
    assert runner.call_args.kwargs["chapter_start_ms"] == 0
    assert runner.call_args.kwargs["active_timeout_s"] == 300
    runner.assert_called_once()
    assert all(entry["recovery"]["cause"] == "seek_timeout" for entry in chapters._fresh_images(plan).values())


def test_known_bad_index_without_strong_bif_binding_stops_without_automatic_retry(bound_bif, monkeypatch):
    plan, bif, _output = bound_bif
    for meta in bif.parent.glob("*.meta"):
        meta.unlink()
    plan.target = SimpleNamespace(chapters=[SimpleNamespace(index=1, start_ms=1000, end_ms=3000)])
    plan.profile = {}
    plan.manifest_path = plan.folder / "chapter-thumbnails.mpg.json"
    monkeypatch.setattr(
        chapters, "inspect_seek_index", lambda *a, **kw: "Matroska seek index points to invalid Cues data"
    )
    runner = MagicMock()
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    result = chapters.publish_chapters(plan, SimpleNamespace())
    assert (result.status, result.completed, result.total, result.retryable) == ("failed", 0, 1, False)
    assert "no source-verified scrubber frame" in result.message
    runner.assert_not_called()


def test_timed_out_index_process_has_deferred_reaper_when_kernel_holds_it(tmp_path, monkeypatch):
    path = tmp_path / "movie.mkv"
    fingerprint = indexed_source(path, "cues")
    proc = MagicMock(pid=987)
    proc.poll.return_value = None
    proc.wait.side_effect = recovery.subprocess.TimeoutExpired("probe", 1)
    monkeypatch.setattr(recovery.subprocess, "Popen", MagicMock(return_value=proc))
    monkeypatch.setattr(recovery.time, "monotonic", MagicMock(side_effect=[0, 6]))
    thread = MagicMock()
    factory = MagicMock(return_value=thread)
    monkeypatch.setattr(recovery, "threading", SimpleNamespace(Thread=factory))
    assert recovery.inspect_seek_index(str(path), fingerprint, [1000]) is None
    proc.kill.assert_called_once_with()
    proc.wait.assert_called_once_with(timeout=1)
    factory.assert_called_once_with(target=proc.wait, name="chapter-inspect-reap-987", daemon=True)
    thread.start.assert_called_once_with()
    proc.stdout.close.assert_called_once_with()
