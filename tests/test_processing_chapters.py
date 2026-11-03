"""Chapter artifact lifecycles, separate from an already completed BIF."""

import json
import os
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger
from PIL import Image
from pymediainfo import MediaInfo

from media_preview_generator.markers.publishers.base import Capability
from media_preview_generator.output.plex_bundle import PlexBundleAdapter
from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters, multi_server
from media_preview_generator.processing.generator import CancellationError
from media_preview_generator.processing.multi_server import MultiServerStatus, PublisherStatus, process_canonical_path
from media_preview_generator.servers import ServerRegistry
from media_preview_generator.servers.plex_chapters import Chapter, ChapterError, ChapterTarget
from tests.conftest import CTIME_ONLY_CHANGES, change_ctime_only, change_source


@pytest.fixture
def config():
    return SimpleNamespace(
        tonemap_algorithm="hable", ffmpeg_threads=2, thumbnail_quality=2, ffmpeg_path="ffmpeg", log_level="INFO"
    )


@pytest.fixture
def plan(tmp_path, config):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"stable video bytes")
    target = ChapterTarget(
        1,
        2,
        3,
        calculate_plex_hash(source),
        str(source),
        source.stat().st_size,
        100,
        (Chapter(1, 0, 1000, "", 10, 20), Chapter(2, 1000, 2000, "", 11, 21)),
        "machine",
    )
    return chapters.ChapterPlan(
        MagicMock(),
        str(source),
        get_source_fingerprint(source),
        tmp_path / "Chapters",
        {"version": 1, "width": 1280, "quality": 4, "tonemap": "hable"},
        target,
        chapters.ChapterOutcome("queued", total=2),
    )


def _image(path, color="red"):
    Image.new("RGB", (1280, 720), color).save(path, "JPEG")


@pytest.fixture
def extraction(monkeypatch):
    monkeypatch.setattr(MediaInfo, "parse", lambda _path: object())
    run = MagicMock(side_effect=lambda _source, _start, path, *_args, **_kw: _image(path))
    monkeypatch.setattr(chapters, "extract_chapter_frame", run)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)
    return run, register


def test_partial_failure_keeps_success_and_retries_only_failed_chapter(plan, config, extraction):
    run, register = extraction
    plan.folder.mkdir()
    old = plan.folder / "chapter2.jpg"
    _image(old, "blue")
    old_bytes = old.read_bytes()

    def first(_source, start, output, *_args, **_kw):
        if start:
            raise RuntimeError("decoder failed")
        _image(output)

    run.side_effect = first
    result = chapters.publish_chapters(plan, config)
    assert (result.status, result.completed, result.total) == ("failed", 1, 2)
    assert old.read_bytes() == old_bytes
    register.assert_not_called()
    assert set(chapters._fresh_images(plan)) == {"1"}
    run.reset_mock()
    run.side_effect = lambda _source, _start, path, *_args, **_kw: _image(path, "green")
    result = chapters.publish_chapters(plan, config)
    assert (result.status, result.completed) == ("ready", 2)
    assert run.call_count == 1
    assert run.call_args.args[1] == 1000
    assert set(register.call_args.args[2]) == {1, 2}


def test_registration_retry_reuses_images(plan, config, extraction):
    run, register = extraction
    register.side_effect = ChapterError("Plex unavailable", code="registration")
    outcome = chapters.publish_chapters(plan, config)
    assert outcome.status == "failed" and outcome.retryable
    assert run.call_count == 2
    run.reset_mock()
    register.side_effect = None
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()


def test_registered_fresh_images_need_no_work_but_cleared_refs_do(plan, config, extraction):
    run, register = extraction
    assert chapters.publish_chapters(plan, config).status == "ready"
    images = chapters._fresh_images(plan)
    plan.target = replace(
        plan.target,
        chapters=tuple(
            replace(
                c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={images[str(c.index)]['sha256']}"
            )
            for c in plan.target.chapters
        ),
    )
    assert not chapters.chapter_work_needed(plan)
    assert chapters.chapter_work_needed(plan, regenerate=True)
    run.reset_mock()
    register.reset_mock()
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()
    register.assert_not_called()
    plan.target = replace(plan.target, chapters=tuple(replace(c, thumb_url="") for c in plan.target.chapters))
    assert chapters.chapter_work_needed(plan)
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()
    register.assert_called_once()


def _with_thumbs(plan, *thumbs):
    plan.target = replace(
        plan.target, chapters=tuple(replace(c, thumb_url=t) for c, t in zip(plan.target.chapters, thumbs, strict=True))
    )


PLEX_THUMB = "media://8/abc.bundle/Contents/Chapters/chapter{}.jpg"


def test_chapter_images_plex_made_need_no_work_and_are_kept(plan, config, extraction):
    """Library Health counts Plex's own chapter images as done; a Previews job must agree (#436)."""
    run, register = extraction
    _with_thumbs(plan, PLEX_THUMB.format(1), PLEX_THUMB.format(2))

    assert chapters.chapter_work_needed(plan) is False
    outcome = chapters.publish_chapters(plan, config)

    assert (outcome.status, outcome.completed, outcome.total) == ("ready", 2, 2)
    assert outcome.message == "Plex's own chapter thumbnails kept"
    run.assert_not_called()
    register.assert_not_called()
    assert not plan.folder.exists()


def test_regenerate_replaces_plex_made_chapter_images(plan, config, extraction):
    run, register = extraction
    _with_thumbs(plan, PLEX_THUMB.format(1), PLEX_THUMB.format(2))

    assert chapters.chapter_work_needed(plan, regenerate=True) is True
    assert chapters.publish_chapters(plan, config, regenerate=True).status == "ready"

    assert run.call_count == 2
    assert set(register.call_args.args[2]) == {1, 2}


@pytest.mark.parametrize(
    "thumbs",
    [
        pytest.param(("PLEX1", ""), id="plex-image-missing-on-one-chapter"),
        pytest.param(("PLEX1", "/library/media/2/chapterImages/2?mpgChapter=stale"), id="mixed-with-an-app-image"),
    ],
)
def test_chapter_images_plex_made_only_in_part_are_still_filled(plan, config, extraction, thumbs):
    """Only a full Plex-made set is left alone: Library Health counts a chapter without an image as to-do too."""
    run, register = extraction
    _with_thumbs(plan, *(PLEX_THUMB.format(1) if t == "PLEX1" else t for t in thumbs))

    assert chapters.chapter_work_needed(plan) is True
    assert chapters.publish_chapters(plan, config).status == "ready"

    assert run.call_count == 2
    register.assert_called_once()


def test_committed_refs_without_api_visibility_fail_verification_and_can_retry(plan, config, extraction):
    run, register = extraction

    def committed_but_unverified(_server, _target, revisions, *, verify_source):
        verify_source()
        plan.target = replace(
            plan.target,
            chapters=tuple(
                replace(c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={revisions[c.index]}")
                for c in plan.target.chapters
            ),
        )
        raise ChapterError("Plex API verification failed", code="registration")

    register.side_effect = committed_but_unverified
    outcome = chapters.publish_chapters(plan, config)
    assert outcome.status == "failed" and outcome.retryable
    run.reset_mock()
    assert chapters.chapter_work_needed(plan)
    outcome = chapters.publish_chapters(plan, config)
    assert outcome.status == "failed" and outcome.retryable
    run.assert_not_called()
    assert chapters.chapter_work_needed(plan)
    register.side_effect = None
    assert chapters.publish_chapters(plan, config).status == "ready"
    run.assert_not_called()
    assert not chapters.chapter_work_needed(plan)


def test_changed_chapter_timestamp_rebuilds_only_that_image(plan, config, extraction):
    run, _register = extraction
    chapters.publish_chapters(plan, config)
    run.reset_mock()
    plan.target = replace(
        plan.target, chapters=(plan.target.chapters[0], replace(plan.target.chapters[1], start_ms=1100))
    )
    chapters.publish_chapters(plan, config)
    assert run.call_count == 1
    assert run.call_args.args[1] == 1100


def test_corrupt_image_does_not_invalidate_other_images(plan, config, extraction):
    run, _ = extraction
    chapters.publish_chapters(plan, config)
    (plan.folder / "chapter2.jpg").write_bytes(b"not an image")
    run.reset_mock()
    chapters.publish_chapters(plan, config)
    assert run.call_count == 1
    assert run.call_args.args[1] == 1000


def test_truncated_jpeg_with_valid_header_is_not_fresh(plan, config, extraction):
    run, _ = extraction
    chapters.publish_chapters(plan, config)
    path = plan.folder / "chapter2.jpg"
    data = path.read_bytes()
    path.write_bytes(data[:-100])
    with Image.open(path) as image:
        assert image.size == (1280, 720)
    assert "2" not in chapters._fresh_images(plan)
    with pytest.raises(OSError):
        chapters._revision(path)


def test_force_failure_does_not_leave_prior_manifest_fresh(plan, config, extraction):
    run, _ = extraction
    chapters.publish_chapters(plan, config)
    run.side_effect = RuntimeError("extraction failed")
    result = chapters.publish_chapters(plan, config, regenerate=True)
    assert result.status == "failed"
    assert result.completed == 0
    assert chapters._fresh_images(plan) == {}


def test_source_changed_during_extraction_never_replaces_image_or_registers(plan, config, extraction):
    run, register = extraction

    def replace_source(_source, _start, output, *_args, **_kw):
        _image(output)
        Path(plan.canonical_path).write_bytes(b"replacement source")

    run.side_effect = replace_source
    result = chapters.publish_chapters(plan, config)
    assert result.status == "failed" and result.retryable
    assert not (plan.folder / "chapter1.jpg").exists()
    register.assert_not_called()


def test_cancel_preserves_existing_outputs_and_stops_registration(plan, config, extraction):
    run, register = extraction
    with pytest.raises(CancellationError):
        chapters.publish_chapters(plan, config, cancel_check=lambda: True)
    run.assert_not_called()
    register.assert_not_called()


def test_stale_plex_hash_waits_for_plex_without_image_work(plan, config, monkeypatch):
    server = plan.server
    server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_k: replace(plan.target, bundle_hash="stale-hash"),
    )
    prepared = chapters.prepare_chapters(
        server, SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}), plan.canonical_path, config
    )
    assert prepared.target is None
    assert prepared.outcome.status == "waiting" and prepared.outcome.retryable
    assert "current source" in prepared.outcome.message
    assert not plan.folder.exists()


@pytest.mark.parametrize("size_delta, expected", [(0, "queued"), (1, "waiting")])
def test_bulk_scan_takes_plex_hash_on_size_match_without_reading_the_file(
    plan, config, monkeypatch, size_delta, expected
):
    plan.server.path_mappings = []
    server_hash = "f" + "0" * 39
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_k: replace(
            plan.target, bundle_hash=server_hash, source_size=plan.target.source_size + size_delta
        ),
    )
    monkeypatch.setattr(chapters, "calculate_plex_hash", MagicMock(side_effect=AssertionError("file was read")))
    plex_config = plan.folder.parent / "plex"
    prepared = chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plex_config)}),
        plan.canonical_path,
        config,
        trust_server_hash=True,
    )
    assert prepared.outcome.status == expected
    if expected == "queued":
        bif = PlexBundleAdapter.bundle_bif_path(str(plex_config), server_hash)
        assert prepared.folder == bif.parent.parent / "Chapters"
    else:
        assert prepared.target is None


@pytest.mark.parametrize("kind", ["sdr", "pq", "hlg"])
def test_timestamp_runner_uses_correct_color_path_and_bounded_threads(tmp_path, config, monkeypatch, kind):
    track = SimpleNamespace(
        hdr_format="Dolby Vision" if kind == "dv5" else None,
        transfer_characteristics={"pq": "PQ", "hlg": "HLG"}.get(kind),
    )
    output = tmp_path / "frame.jpg"
    runner = MagicMock(side_effect=lambda **_kw: (_image(output), (0, 0.1, 1, []))[1])
    factory = MagicMock(return_value=runner)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)
    chapters.extract_chapter_frame("movie.mkv", 1234, output, config, media_info=SimpleNamespace(video_tracks=[track]))
    kwargs = factory.call_args.kwargs
    assert kwargs["chapter_start_ms"] == 1234
    assert kwargs["chapter_output"] == str(output)
    assert kwargs["ffmpeg_threads_override"] == 2
    assert kwargs["gpu"] is None
    expected_scale = "scale=w=1280:h=-2"
    if kind != "sdr":
        expected_scale += ":in_range=tv:out_range=pc,format=yuvj420p"
        assert kwargs["hdr10_zscale_chain"].endswith("zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
    assert kwargs["base_scale"] == expected_scale
    assert kwargs["path_kind"] == ("sdr" if kind == "sdr" else "hdr10_zscale")
    assert runner.call_args.kwargs == {"use_skip": False}


@pytest.mark.parametrize("failure", [RuntimeError("FFmpeg exit 234"), ValueError("Invalid JPEG"), OSError("No frame")])
def test_extraction_errors_fail_instead_of_waiting_for_plex(plan, config, extraction, failure):
    run, register = extraction
    run.side_effect = failure

    result = chapters.publish_chapters(plan, config)

    assert result.status == "failed"
    assert (result.completed, result.total) == (0, 2)
    assert result.message == str(failure)
    register.assert_not_called()


def test_a_file_whose_details_mediainfo_cant_read_gets_every_chapter_and_one_warning(
    plan, config, extraction, monkeypatch
):
    run, register = extraction
    # What pymediainfo raises for a tag MediaInfo writes into its XML as it is (U+FFFF in a title).
    parse = MagicMock(side_effect=ET.ParseError("not well-formed (invalid token): line 50, column 8"))
    monkeypatch.setattr(MediaInfo, "parse", parse)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        result = chapters.publish_chapters(plan, config)
    finally:
        logger.remove(sink)

    assert (result.status, result.completed, result.total) == ("ready", 2, 2)
    assert parse.call_count == 1
    assert [call.kwargs["media_info"].video_tracks[0].hdr_format for call in run.call_args_list] == [None, None]
    assert len(warnings) == 1 and plan.canonical_path in warnings[0], warnings
    register.assert_called_once()


def test_a_chapter_frame_of_a_file_whose_details_mediainfo_cant_read_is_extracted(video_mediainfo_cant_read, tmp_path):
    config = SimpleNamespace(
        ffmpeg_path=shutil.which("ffmpeg"), ffmpeg_threads=2, thumbnail_quality=4, tonemap_algorithm="hable",
        log_level="INFO",
    )  # fmt: skip
    output = tmp_path / "chapter.jpg"

    chapters.extract_chapter_frame(video_mediainfo_cant_read, 2000, output, config)

    with Image.open(output) as image:
        assert image.width == 1280


def test_rejected_image_reports_actual_dimensions(tmp_path):
    output = tmp_path / "frame.jpg"
    Image.new("RGB", (1278, 534)).save(output, "JPEG")

    with pytest.raises(ValueError, match="expected a 1280px JPEG; got JPEG 1278x534"):
        chapters._revision(output)


@pytest.mark.parametrize(
    "error, status, retryable",
    [
        (ChapterError("Plex has not indexed the source", code="pending_index"), "waiting", True),
        (ChapterError("Plex chapter snapshot changed", code="source_changed"), "failed", True),
        (ChapterError("API verification failed", code="registration"), "failed", True),
        (ChapterError("Unsupported source", code="unsupported"), "failed", False),
        (chapters.SourceFileChangedError("File changed"), "failed", True),
        (chapters.ProbeTimeoutError("Probe timed out"), "failed", True),
        (chapters.ProbeStalledError("Probe stalled"), "failed", True),
        (chapters.ProbeError("Unreadable source"), "failed", False),
        (chapters.RequestException("Connection failed"), "failed", True),
        (TimeoutError("Connection timed out"), "failed", True),
        (PermissionError("Read-only folder"), "failed", False),
        (FileNotFoundError("Missing source"), "failed", False),
        (RuntimeError("Decoder failed"), "failed", False),
        (ValueError("Wrong image dimensions"), "failed", False),
        (chapters.UnsupportedChapterFormatError("Unsupported Dolby Vision"), "skipped", False),
        (ChapterError("Split version", code="unsupported_source"), "skipped", False),
        (ChapterError("Other version", code="other_version"), "skipped", False),
    ],
)
def test_chapter_failure_and_retry_eligibility_are_independent(error, status, retryable):
    result = chapters._failure(error, completed=2, total=3)

    assert result.status == status
    assert result.retryable is retryable
    assert (result.completed, result.total) == (2, 3)
    assert str(error) in result.message
    assert result.to_dict()["retryable"] is retryable


@pytest.mark.parametrize("state", [None, *chapters.Capability])
def test_only_transient_publisher_capability_failures_can_retry(state):
    result = chapters._failure(chapters.PublishError("Registration unavailable", state=state))

    assert result.status == "failed"
    assert result.retryable is (
        state
        in {None, chapters.Capability.READY, chapters.Capability.UNREACHABLE, chapters.Capability.AGENT_UNAVAILABLE}
    )


def test_unwritable_chapter_folder_fails_with_actionable_reason(plan, config, extraction, monkeypatch):
    mkdir = Path.mkdir

    def denied(path, *args, **kwargs):
        if path == plan.folder:
            raise PermissionError("Permission denied: Chapters")
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", denied)

    result = chapters.publish_chapters(plan, config)

    assert result.status == "failed" and not result.retryable
    assert "Check the app user's access" in result.message
    extraction[0].assert_not_called()
    extraction[1].assert_not_called()


def test_dv_without_compatible_base_fails_without_starting_unselected_gpu(tmp_path, config, monkeypatch):
    track = SimpleNamespace(hdr_format="Dolby Vision", transfer_characteristics=None)
    factory = MagicMock()
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)
    with pytest.raises(chapters.UnsupportedChapterFormatError, match="Dolby Vision"):
        chapters.extract_chapter_frame(
            "movie.mkv", 0, tmp_path / "frame.jpg", config, media_info=SimpleNamespace(video_tracks=[track])
        )
    factory.assert_not_called()


def test_no_chapters_code_maps_to_none_outcome():
    result = chapters._failure(ChapterError("No chapters", code="no_chapters"), completed=2, total=3)

    assert result.status == "none"
    assert result.message == "No chapters"
    assert result.retryable is False


def test_no_video_stream_is_unsupported_source(tmp_path, config):
    with pytest.raises(chapters.UnsupportedChapterFormatError, match="No video stream"):
        chapters.extract_chapter_frame(
            "movie.mkv", 0, tmp_path / "frame.jpg", config, media_info=SimpleNamespace(video_tracks=[])
        )
    assert chapters._failure(chapters.UnsupportedChapterFormatError("No video stream")).status == "skipped"


def _registry(plan, enabled=True):
    return ServerRegistry.from_settings(
        [
            {
                "id": "plex",
                "type": "plex",
                "name": "Plex",
                "enabled": True,
                "url": "http://plex.invalid",
                "auth": {"token": "test"},
                "libraries": [
                    {
                        "id": "1",
                        "name": "Movies",
                        "remote_paths": [str(Path(plan.canonical_path).parent)],
                        "enabled": True,
                    }
                ],
                "output": {
                    "adapter": "plex_bundle",
                    "plex_config_folder": str(plan.folder.parent / "plex"),
                    "chapter_thumbnails": enabled,
                },
            }
        ]
    )


@pytest.mark.parametrize("check_only", [False, True])
def test_existing_bif_chapter_backfill_does_not_extract_or_pack_bif(
    plan, mock_config, extraction, monkeypatch, check_only
):
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    generate = MagicMock(side_effect=AssertionError("BIF extraction must not run"))
    pack = MagicMock(side_effect=AssertionError("BIF packing must not run"))
    monkeypatch.setattr("media_preview_generator.processing.multi_server.generate_images", generate)
    monkeypatch.setattr("media_preview_generator.processing.generator.generate_bif", pack)
    progress = []
    previews = multi_server._process_canonical_path_previews

    def check_scrubber_progress(*args, **kwargs):
        assert progress[-1] is None  # Clear chapter preparation before any scrubber work.
        return previews(*args, **kwargs)

    preview_boundary = MagicMock(side_effect=check_scrubber_progress)
    monkeypatch.setattr(multi_server, "_process_canonical_path_previews", preview_boundary)
    result = process_canonical_path(
        plan.canonical_path, registry, mock_config, check_only=check_only, chapter_progress_callback=progress.append
    )
    assert result.status is (MultiServerStatus.NEEDS_GENERATION if check_only else MultiServerStatus.PUBLISHED)
    assert bif.read_bytes() == b"existing BIF"
    generate.assert_not_called()
    pack.assert_not_called()
    preview_boundary.assert_called_once()
    if check_only:
        extraction[0].assert_not_called()
    else:
        assert extraction[0].call_count == 2
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        assert result.publishers[0].artifacts["chapters"]["updated"] is True
        assert result.publishers[0].artifacts["bif"]["status"] == "skipped_output_exists"
        # Counted as generated, so the file's reason says the scrubber was reused (#436).
        assert result.message == "Chapter thumbnails updated; scrubber previews already existed"
        assert progress[-1] == {"stage": "complete", "processed": 2, "total": 2, "ready": 2, "failed": 0}
    assert progress[0]["stage"] == "preparing"


def test_plex_made_chapter_images_beside_an_existing_bif_leave_the_file_untouched(
    plan, mock_config, extraction, monkeypatch
):
    """The #436 report: a full-library job sent these to a worker and counted them as generated."""
    _with_thumbs(plan, PLEX_THUMB.format(1), PLEX_THUMB.format(2))
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)

    checked = process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True)
    assert checked.status is MultiServerStatus.SKIPPED

    result = process_canonical_path(plan.canonical_path, registry, mock_config)
    assert result.status is MultiServerStatus.SKIPPED
    assert result.publishers[0].artifacts["chapters"]["updated"] is False
    extraction[0].assert_not_called()
    extraction[1].assert_not_called()


def test_disabled_chapters_never_resolve_metadata(plan, mock_config, monkeypatch):
    registry = _registry(plan, enabled=False)
    prepare = MagicMock(side_effect=AssertionError("disabled chapter lookup"))
    monkeypatch.setattr(chapters, "prepare_chapters", prepare)
    process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True)
    prepare.assert_not_called()


@pytest.mark.parametrize(
    "source, webhook_source, webhook_paths, trusted",
    [
        (None, None, None, True),
        ("sonarr", None, None, False),
        (None, "radarr", None, False),
        (None, None, ["/data/x.mkv"], False),
    ],
)
def test_only_bulk_scans_let_chapter_planning_trust_plex_hash(
    plan, mock_config, monkeypatch, source, webhook_source, webhook_paths, trusted
):
    registry = _registry(plan)
    mock_config.webhook_source = webhook_source
    mock_config.webhook_paths = webhook_paths
    prepare = MagicMock(return_value=plan)
    monkeypatch.setattr(chapters, "prepare_chapters", prepare)
    process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True, source=source)
    assert prepare.call_args.kwargs["trust_server_hash"] is trusted


@pytest.mark.parametrize("already_registered", [False, True])
def test_chapter_update_provenance_distinguishes_registration_from_reused_outputs(
    plan, mock_config, config, extraction, monkeypatch, already_registered
):
    run, register = extraction
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    assert chapters.publish_chapters(plan, config).status == "ready"
    images = chapters._fresh_images(plan)
    if already_registered:
        plan.target = replace(
            plan.target,
            chapters=tuple(
                replace(
                    c, thumb_url=f"/library/media/2/chapterImages/{c.index}?mpgChapter={images[str(c.index)]['sha256']}"
                )
                for c in plan.target.chapters
            ),
        )
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    run.reset_mock()
    register.reset_mock()

    result = process_canonical_path(plan.canonical_path, registry, mock_config)

    assert result.status is (MultiServerStatus.SKIPPED if already_registered else MultiServerStatus.PUBLISHED)
    artifact = result.publishers[0].artifacts["chapters"]
    assert artifact["status"] == "ready"
    assert artifact["updated"] is (not already_registered)
    run.assert_not_called()
    if already_registered:
        register.assert_not_called()
    else:
        verify_source = register.call_args.kwargs["verify_source"]
        assert callable(verify_source)
        verify_source()
        register.assert_called_once_with(
            plan.server,
            plan.target,
            {int(index): entry["sha256"] for index, entry in images.items()},
            verify_source=verify_source,
        )


def test_ffmpeg_failure_preserves_existing_bif_and_records_chapter_warning(plan, mock_config, monkeypatch, tmp_path):
    from media_preview_generator.jobs.orchestrator import (
        _outcome_for_multi_server_status,
        _publisher_rows_from_result,
        merge_chain_publishers_best_per_path,
    )
    from media_preview_generator.web.jobs import JobManager
    from media_preview_generator.web.routes.job_runner import _chapter_completion_warning

    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    track = SimpleNamespace(hdr_format=None, transfer_characteristics=None)
    monkeypatch.setattr(MediaInfo, "parse", lambda _path: SimpleNamespace(video_tracks=[track]))
    runner = MagicMock(return_value=(234, 0.1, 0, ["File ended prematurely"]))
    factory = MagicMock(return_value=runner)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)

    result = process_canonical_path(plan.canonical_path, registry, mock_config)

    assert result.status is MultiServerStatus.SKIPPED
    assert bif.read_bytes() == b"existing BIF"
    assert [call.kwargs["chapter_start_ms"] for call in factory.call_args_list] == [0, 1000]
    assert all(call.kwargs["base_scale"] == "scale=w=1280:h=-2" for call in factory.call_args_list)
    assert all(call.kwargs == {"use_skip": False} for call in runner.call_args_list)
    register.assert_not_called()
    manager = JobManager(config_dir=str(tmp_path / "jobs"))
    job = manager.create_job(library_name="Chapters")
    manager.record_file_result(
        job.id,
        plan.canonical_path,
        _outcome_for_multi_server_status(result.status).value,
        servers=_publisher_rows_from_result(result, plan.canonical_path),
    )
    stored = manager.get_file_results(job.id)
    assert stored[0]["outcome"] == "skipped_bif_exists"
    outcome = stored[0]["servers"][0]["artifacts"]["chapters"]
    assert outcome["status"] == "failed"
    assert "FFmpeg exit 234" in outcome["message"]
    assert "chapter thumbnails failed for 1" in _chapter_completion_warning(
        merge_chain_publishers_best_per_path(stored), include_pending=True
    )


def test_waiting_chapters_preserve_bif_success_and_request_retry(plan, mock_config, monkeypatch):
    registry = _registry(plan)
    bif = PlexBundleAdapter.bundle_bif_path(
        registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
    )
    bif.parent.mkdir(parents=True)
    bif.write_bytes(b"existing BIF")
    plan.target = None
    plan.outcome = chapters.ChapterOutcome("waiting", message="Plex has not indexed this source yet", retryable=True)
    monkeypatch.setattr(chapters, "prepare_chapters", lambda *_a, **_kw: plan)
    result = process_canonical_path(plan.canonical_path, registry, mock_config, check_only=True)
    publisher = result.publishers[0]
    assert publisher.status is PublisherStatus.PUBLISHED_PENDING_CHAPTERS
    assert publisher.artifacts["bif"]["status"] == "skipped_output_exists"
    assert publisher.artifacts["chapters"]["updated"] is False
    from media_preview_generator.processing.retry_queue import PENDING_PUBLISHER_STATUSES

    assert publisher.status.value in PENDING_PUBLISHER_STATUSES


@pytest.mark.parametrize("source_chapters, expected", [((), "none"), ((object(),), "waiting")])
def test_empty_plex_rows_require_positive_source_chapter_check(plan, config, monkeypatch, source_chapters, expected):
    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_kw: replace(plan.target, chapters=()),
    )
    probe = MagicMock(return_value=SimpleNamespace(chapters=source_chapters, duration_ms=2000))
    monkeypatch.setattr(chapters, "probe_media", probe)
    result = chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}),
        plan.canonical_path,
        config,
    )
    assert result.outcome.status == expected
    assert probe.call_args.kwargs["timeout_s"] == 5


def test_a_library_scan_trusts_plexs_empty_chapter_list_without_probing(plan, config, monkeypatch):
    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_kw: replace(plan.target, chapters=(), analysed=True),
    )
    probe = MagicMock(return_value=SimpleNamespace(chapters=(object(),), duration_ms=2000))
    monkeypatch.setattr(chapters, "probe_media", probe)
    result = chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}),
        plan.canonical_path,
        config,
        trust_server_hash=True,
    )
    assert result.outcome.status == "none"
    assert result.outcome.message == "No chapters"
    probe.assert_not_called()


@pytest.mark.parametrize("analysed", [False, "agent_without_signal"])
def test_a_library_scan_probes_a_part_plex_scanned_but_not_analysed(plan, config, monkeypatch, analysed):
    plan.server.path_mappings = []
    target = replace(plan.target, chapters=())
    if analysed is False:
        target = replace(target, analysed=False)
    else:
        from media_preview_generator.markers.publishers.plex_chapters import target_from_json, target_to_json

        wire = target_to_json(target)
        del wire["analysed"]
        target = target_from_json(wire)
    assert target.analysed is False
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target", lambda *_a, **_kw: target
    )
    probe = MagicMock(return_value=SimpleNamespace(chapters=(), duration_ms=2000))
    monkeypatch.setattr(chapters, "probe_media", probe)
    result = chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}),
        plan.canonical_path,
        config,
        trust_server_hash=True,
    )
    assert result.outcome.status == "none"
    probe.assert_called_once()


def test_a_library_scan_still_waits_when_plex_indexed_a_different_size(plan, config, monkeypatch):
    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_kw: replace(plan.target, chapters=(), source_size=plan.target.source_size + 1),
    )
    probe = MagicMock()
    monkeypatch.setattr(chapters, "probe_media", probe)
    result = chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}),
        plan.canonical_path,
        config,
        trust_server_hash=True,
    )
    assert result.outcome.status == "waiting"
    probe.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "source_changed", "cancel"])
def test_empty_plex_rows_never_complete_after_probe_failure_or_source_race(plan, config, monkeypatch, failure):
    from media_preview_generator.markers.probe import ProbeTimeoutError

    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target",
        lambda *_a, **_kw: replace(plan.target, chapters=()),
    )
    cancelled = False

    def probe(*_a, **_kw):
        nonlocal cancelled
        if failure == "timeout":
            raise ProbeTimeoutError("metadata probe timed out")
        if failure == "source_changed":
            Path(plan.canonical_path).write_bytes(b"replacement source")
        if failure == "cancel":
            cancelled = True
        return SimpleNamespace(chapters=(), duration_ms=2000)

    monkeypatch.setattr(chapters, "probe_media", probe)

    def prepare():
        return chapters.prepare_chapters(
            plan.server,
            SimpleNamespace(output={"plex_config_folder": str(plan.folder.parent)}),
            plan.canonical_path,
            config,
            cancel_check=lambda: cancelled,
        )

    if failure == "cancel":
        with pytest.raises(CancellationError):
            prepare()
    else:
        result = prepare()
        assert result.target is None and result.outcome.status == "failed"
        assert result.outcome.retryable


@pytest.mark.parametrize("cached", [0, 1, 2])
def test_chapter_progress_counts_cached_images_and_reports_registration(plan, config, extraction, cached):
    run, register = extraction
    if cached:
        assert chapters.publish_chapters(plan, config).status == "ready"
        if cached == 1:
            (plan.folder / "chapter2.jpg").unlink()
    run.reset_mock()
    register.reset_mock()
    progress = []
    during_registration = []
    register.side_effect = lambda *_a, **_kw: during_registration.append(progress[-1])

    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)

    assert result.status == "ready"
    assert [call.args[1] for call in run.call_args_list] == [0, 1000][cached:]
    expected = {"stage": "registering", "processed": 2, "total": 2, "ready": 2, "failed": 0}
    assert during_registration == [expected]
    assert progress[-1] == {**expected, "stage": "complete"}
    if cached < 2:
        first_attempt = next(snapshot for snapshot in progress if snapshot["stage"] == "extracting")
        assert first_attempt == {"stage": "extracting", "processed": cached, "total": 2, "ready": cached, "failed": 0}
    assert [snapshot["processed"] for snapshot in progress] == sorted(snapshot["processed"] for snapshot in progress)


def test_failed_chapter_attempt_advances_work_progress_without_counting_as_ready(plan, config, extraction):
    run, register = extraction

    def extract(_source, start, output, *_args, **_kwargs):
        if start == 0:
            raise RuntimeError("Cannot decode first chapter")
        _image(output)

    run.side_effect = extract
    progress = []
    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)

    assert result.status == "failed"
    assert progress[-1] == {"stage": "failed", "processed": 2, "total": 2, "ready": 1, "failed": 1}
    assert {"stage": "extracting", "processed": 1, "total": 2, "ready": 0, "failed": 1} in progress
    assert all(snapshot["stage"] != "complete" for snapshot in progress)
    register.assert_not_called()


def test_chapter_registration_failure_keeps_completed_image_progress(plan, config, extraction):
    _, register = extraction
    register.side_effect = ChapterError("Plex verification failed", code="registration")
    progress = []

    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)

    assert result.status == "failed"
    assert progress[-2:] == [
        {"stage": "registering", "processed": 2, "total": 2, "ready": 2, "failed": 0},
        {"stage": "failed", "processed": 2, "total": 2, "ready": 2, "failed": 0},
    ]


def test_cancellation_never_reports_remaining_chapter_work_complete(plan, config, extraction):
    run, register = extraction
    progress = []
    with pytest.raises(CancellationError):
        chapters.publish_chapters(
            plan,
            config,
            chapter_progress_callback=progress.append,
            cancel_check=lambda: bool(progress and progress[-1]["processed"] == 1),
        )
    assert progress[-1] == {"stage": "extracting", "processed": 1, "total": 2, "ready": 1, "failed": 0}
    assert [call.args[1] for call in run.call_args_list] == [0]
    register.assert_not_called()


def test_waiting_for_chapter_lock_reports_no_attempts_and_can_cancel(plan, config, extraction, monkeypatch):
    lock = MagicMock()
    lock.acquire.return_value = False
    monkeypatch.setattr(chapters, "_LOCKS", (lock,))
    progress = []
    with pytest.raises(CancellationError):
        chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append, cancel_check=lambda: True)
    assert progress == [{"stage": "waiting", "processed": 0, "total": 2, "ready": 0, "failed": 0}]
    lock.acquire.assert_called_once_with(timeout=0.1)
    lock.release.assert_not_called()
    extraction[0].assert_not_called()


def _prepare_for(plan, config, monkeypatch, **kwargs):
    plan.server.path_mappings = []
    monkeypatch.setattr(
        "media_preview_generator.servers.plex_chapters.resolve_chapter_target", lambda *_a, **_k: plan.target
    )
    plex_config = Path(plan.canonical_path).parent / "plex"
    return chapters.prepare_chapters(
        plan.server,
        SimpleNamespace(output={"plex_config_folder": str(plex_config)}),
        plan.canonical_path,
        config,
        **kwargs,
    )


@pytest.mark.parametrize("item_id", [None, 7], ids=["no-item", "item-named"])
def test_a_file_plex_lists_as_several_items_asks_the_caller_to_name_one(plan, config, monkeypatch, item_id):
    """Only "several items" reaches the caller, which then names each item; every other Plex refusal is an outcome."""

    def read(*_args, item_id_hint=None, **_kwargs):
        if item_id_hint is None:
            raise ChapterError("Plex lists this file as more than one item", code="several_items")
        return plan.target

    _prepare_for(plan, config, monkeypatch)
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.resolve_chapter_target", read)
    plex_config = Path(plan.canonical_path).parent / "plex"

    def prepare():
        return chapters.prepare_chapters(
            plan.server,
            SimpleNamespace(output={"plex_config_folder": str(plex_config)}),
            plan.canonical_path,
            config,
            item_id_hint=item_id,
        )

    if item_id is None:
        with pytest.raises(ChapterError) as raised:
            prepare()
        assert raised.value.code == "several_items"
    else:
        assert prepare().target is plan.target


def test_unsupported_format_is_stamped_and_next_prepare_skips_all_work(plan, config, extraction, monkeypatch):
    run, register = extraction
    run.side_effect = chapters.UnsupportedChapterFormatError("Chapter thumbnails do not yet support Dolby Vision")
    plan.folder = _prepare_for(plan, config, monkeypatch).folder

    first = chapters.publish_chapters(plan, config)

    assert first.status == "skipped"
    assert first.retryable is False
    register.assert_not_called()
    again = _prepare_for(plan, config, monkeypatch)
    assert again.target is None
    assert again.outcome.status == "skipped"
    assert "Dolby Vision" in again.outcome.message
    assert chapters.chapter_work_needed(again) is False


def test_unsupported_stamp_is_ignored_by_regenerate_and_by_a_changed_file(plan, config, extraction, monkeypatch):
    run, _register = extraction
    run.side_effect = chapters.UnsupportedChapterFormatError("Unsupported Dolby Vision")
    plan.folder = _prepare_for(plan, config, monkeypatch).folder
    chapters.publish_chapters(plan, config)

    assert _prepare_for(plan, config, monkeypatch, regenerate=True).target is not None
    Path(plan.canonical_path).write_bytes(b"different, longer video bytes")
    plan.target = replace(plan.target, source_size=Path(plan.canonical_path).stat().st_size)
    assert _prepare_for(plan, config, monkeypatch).outcome.status != "skipped"


def test_unsupported_format_reports_skipped_stage_without_counting_failures(plan, config, extraction):
    run, _register = extraction
    run.side_effect = chapters.UnsupportedChapterFormatError("Unsupported Dolby Vision")
    events = []

    chapters.publish_chapters(plan, config, chapter_progress_callback=events.append)

    assert events[-1]["stage"] == "skipped"
    assert events[-1]["failed"] == 0


def _past_end_on_second_chapter(run):
    def extract(_source, start_ms, path, *_args, **_kw):
        if start_ms == 1000:
            raise chapters.ChapterPastEndError("Chapter timestamp 1000ms is outside the current video's duration")
        _image(path)

    run.side_effect = extract


def test_past_end_chapter_gets_no_thumbnail_and_the_rest_register(plan, config, extraction):
    run, register = extraction
    _past_end_on_second_chapter(run)
    events = []

    result = chapters.publish_chapters(plan, config, chapter_progress_callback=events.append)

    assert result.status == "ready"
    assert "1 chapter starts after the video ends and has no thumbnail" in result.message
    assert events[-1]["failed"] == 0
    assert list(register.call_args.args[2]) == [1]
    manifest = json.loads(plan.manifest_path.read_text())
    assert manifest["images"]["2"] == {"start_ms": 1000, "end_ms": 2000, "past_end": True}
    assert "sha256" in manifest["images"]["1"]
    assert manifest["verified"]["images"].keys() == {"1"}


def test_second_publish_does_not_seek_a_past_end_chapter_again(plan, config, extraction):
    run, register = extraction
    _past_end_on_second_chapter(run)
    chapters.publish_chapters(plan, config)
    register_after_first = register.call_count
    run.reset_mock()
    plan.target = replace(
        plan.target,
        chapters=(
            replace(
                plan.target.chapters[0],
                thumb_url=f"/library/media/2/chapterImages/1?mpgChapter={_manifest_sha(plan, '1')}",
            ),
            plan.target.chapters[1],
        ),
    )

    assert chapters.chapter_work_needed(plan) is False
    result = chapters.publish_chapters(plan, config)

    assert result.status == "ready"
    run.assert_not_called()
    assert register.call_count == register_after_first


def _manifest_sha(plan, index):
    return json.loads(plan.manifest_path.read_text())["images"][index]["sha256"]


def test_every_chapter_past_end_still_fails(plan, config, extraction):
    run, register = extraction
    run.side_effect = chapters.ChapterPastEndError("Chapter timestamp is outside the current video's duration")

    result = chapters.publish_chapters(plan, config)

    assert result.status == "failed"
    assert "All 2 chapters start after the video ends" in result.message
    register.assert_not_called()


def test_single_past_end_chapter_message_is_singular(plan, config, extraction):
    run, register = extraction
    run.side_effect = chapters.ChapterPastEndError("Chapter timestamp is after the last video frame")
    plan = replace(plan, target=replace(plan.target, chapters=plan.target.chapters[:1]))

    result = chapters.publish_chapters(plan, config)

    assert result.status == "failed"
    assert result.message == (
        "The only chapter starts after the video ends; no frame exists for it. Check the source chapter metadata."
    )
    register.assert_not_called()


def test_all_past_end_fails_with_a_clear_message_on_every_scan(plan, config, extraction):
    run, register = extraction
    run.side_effect = chapters.ChapterPastEndError("Chapter timestamp is outside the current video's duration")
    first = chapters.publish_chapters(plan, config)
    run.reset_mock()

    second = chapters.publish_chapters(plan, config)

    assert first.status == second.status == "failed"
    assert second.message == first.message
    assert second.message.startswith("All 2 chapters start after the video ends")
    assert second.retryable is False
    run.assert_not_called()
    register.assert_not_called()


def test_past_end_chapter_removes_its_stale_image(plan, config, extraction):
    run, _register = extraction
    _past_end_on_second_chapter(run)
    plan.folder.mkdir(parents=True)
    stale = plan.folder / "chapter2.jpg"
    _image(stale)

    chapters.publish_chapters(plan, config)

    assert not stale.exists()
    assert (plan.folder / "chapter1.jpg").exists()


def test_corrupt_index_with_past_end_chapter_is_ready_with_note(plan, config, extraction, monkeypatch):
    run, register = extraction
    monkeypatch.setattr(chapters, "inspect_seek_index", lambda *a, **kw: "Invalid Cues")
    track = SimpleNamespace(duration=1500)
    monkeypatch.setattr(MediaInfo, "parse", lambda _path: SimpleNamespace(video_tracks=[track]))
    plan.target = replace(
        plan.target, chapters=(plan.target.chapters[0], replace(plan.target.chapters[1], start_ms=9000))
    )
    recovered = []

    def recover(_plan, chapter, staged):
        recovered.append(chapter.index)
        _image(staged)
        return {"timestamp_ms": 0}

    monkeypatch.setattr(chapters, "recover_bif_frame", recover)

    result = chapters.publish_chapters(plan, config)

    assert result.status == "ready"
    assert "starts after the video ends" in result.message
    assert recovered == [1]
    run.assert_not_called()
    assert list(register.call_args.args[2]) == [1]


def _flood(_source, start_ms, *_args, **_kw):
    raise chapters.ChapterDiagnosticFloodError(
        f"Chapter extraction at {start_ms}ms produced excessive diagnostic output"
    )


class TestRememberedChapterFailures:
    def test_diagnostic_flood_is_remembered_and_next_prepare_skips(self, plan, config, extraction, monkeypatch):
        run, register = extraction
        run.side_effect = _flood
        monkeypatch.setattr(chapters, "recover_bif_frame", lambda *_a: None)
        plan.folder = _prepare_for(plan, config, monkeypatch).folder

        first = chapters.publish_chapters(plan, config)

        assert first.status == "failed"
        run.reset_mock()
        again = _prepare_for(plan, config, monkeypatch)
        assert again.target is None
        assert again.outcome.status == "skipped"
        assert "failed before (FFmpeg produced excessive diagnostic output)" in again.outcome.message
        assert "retried when the file is replaced" in again.outcome.message
        assert chapters.chapter_work_needed(again) is False
        run.assert_not_called()
        register.assert_not_called()

    def test_changed_file_is_retried(self, plan, config, extraction, monkeypatch):
        run, _register = extraction
        run.side_effect = _flood
        monkeypatch.setattr(chapters, "recover_bif_frame", lambda *_a: None)
        plan.folder = _prepare_for(plan, config, monkeypatch).folder
        chapters.publish_chapters(plan, config)

        Path(plan.canonical_path).write_bytes(b"different, longer video bytes")
        plan.target = replace(plan.target, source_size=Path(plan.canonical_path).stat().st_size)

        assert _prepare_for(plan, config, monkeypatch).outcome.status != "skipped"

    def test_regenerate_overrides_and_clears_memory(self, plan, config, extraction, monkeypatch):
        run, _register = extraction
        run.side_effect = _flood
        monkeypatch.setattr(chapters, "recover_bif_frame", lambda *_a: None)
        plan.folder = _prepare_for(plan, config, monkeypatch).folder
        chapters.publish_chapters(plan, config)

        forced = _prepare_for(plan, config, monkeypatch, regenerate=True)
        assert forced.target is not None
        run.side_effect = lambda _source, _start, path, *_a, **_kw: _image(path)
        assert chapters.publish_chapters(forced, config, regenerate=True).status == "ready"
        assert _prepare_for(plan, config, monkeypatch).outcome.status != "skipped"

    @pytest.mark.parametrize(
        "error",
        [
            chapters.ChapterExtractionStalledError("watchdog stopped it"),
            TimeoutError("slow disk"),
            RuntimeError("Chapter extraction failed at 0ms (FFmpeg exit -9)"),
            ChapterError("Waiting for Plex Analyze", code="pending_index"),
        ],
    )
    def test_transient_failures_are_not_remembered(self, plan, config, extraction, monkeypatch, error):
        run, _register = extraction
        run.side_effect = error
        monkeypatch.setattr(chapters, "recover_bif_frame", lambda *_a: None)
        plan.folder = _prepare_for(plan, config, monkeypatch).folder

        chapters.publish_chapters(plan, config)

        assert _prepare_for(plan, config, monkeypatch).outcome.status != "skipped"

    @pytest.mark.parametrize("rc", [1, 127, 128, 137, 255, -9])
    def test_any_ffmpeg_exit_raises_plain_runtime_error_and_is_not_remembered(self, tmp_path, config, monkeypatch, rc):
        track = SimpleNamespace(hdr_format=None, transfer_characteristics=None)
        runner = MagicMock(return_value=(rc, 0.1, 1, []))
        monkeypatch.setattr(chapters, "create_ffmpeg_runner", MagicMock(return_value=runner))

        with pytest.raises(RuntimeError, match=f"FFmpeg exit {rc}") as raised:
            chapters.extract_chapter_frame(
                "movie.mkv", 0, tmp_path / "frame.jpg", config, media_info=SimpleNamespace(video_tracks=[track])
            )

        assert type(raised.value) is RuntimeError
        assert chapters._repeatable_reason(raised.value) is None

    def test_generic_ffmpeg_exit_1_is_not_remembered_by_the_next_prepare(self, plan, config, extraction, monkeypatch):
        run, _register = extraction
        run.side_effect = RuntimeError("Chapter extraction failed at 0ms (FFmpeg exit 1)")
        monkeypatch.setattr(chapters, "recover_bif_frame", lambda *_a: None)
        plan.folder = _prepare_for(plan, config, monkeypatch).folder

        chapters.publish_chapters(plan, config)

        assert _prepare_for(plan, config, monkeypatch).outcome.status != "skipped"


class TestImageStamps:
    """An image whose size, modification time and inode match its stamp isn't read again to be hashed."""

    def _hashes(self, monkeypatch):
        hashed: list[str] = []
        real = chapters._file_sha256
        monkeypatch.setattr(chapters, "_file_sha256", lambda path: hashed.append(path.name) or real(path))
        return hashed

    def test_a_stamped_image_is_not_read_again(self, plan, config, extraction, monkeypatch):
        chapters.publish_chapters(plan, config)
        hashed = self._hashes(monkeypatch)
        assert set(chapters._fresh_images(plan)) == {"1", "2"}
        assert hashed == []

    def test_a_rewritten_image_is_hashed_and_a_different_one_is_not_fresh(self, plan, config, extraction, monkeypatch):
        chapters.publish_chapters(plan, config)
        _image(plan.folder / "chapter2.jpg", color="blue")
        hashed = self._hashes(monkeypatch)
        assert set(chapters._fresh_images(plan)) == {"1"}
        assert hashed == ["chapter2.jpg"]

    def test_an_unstamped_manifest_is_hashed_once_then_stamped(self, plan, config, extraction, monkeypatch):
        chapters.publish_chapters(plan, config)
        manifest = json.loads(plan.manifest_path.read_text())
        for entry in manifest["images"].values():
            for key in ("size", "mtime_ns", "ino"):
                entry.pop(key)
        plan.manifest_path.write_text(json.dumps(manifest))
        hashed = self._hashes(monkeypatch)

        assert set(chapters._fresh_images(plan)) == {"1", "2"}
        assert sorted(hashed) == ["chapter1.jpg", "chapter2.jpg"]
        stamped = json.loads(plan.manifest_path.read_text())
        assert stamped["verified"] == manifest["verified"]
        assert {k: v["sha256"] for k, v in stamped["images"].items()} == {
            k: v["sha256"] for k, v in manifest["images"].items()
        }

        hashed.clear()
        assert set(chapters._fresh_images(plan)) == {"1", "2"}
        assert hashed == []

    def test_a_same_size_rewrite_with_the_old_time_is_still_caught_by_its_new_inode(
        self, plan, config, extraction, monkeypatch
    ):
        import os

        chapters.publish_chapters(plan, config)
        path = plan.folder / "chapter2.jpg"
        before = os.stat(path)
        replacement = plan.folder / "replacement.jpg"
        replacement.write_bytes(bytes(reversed(path.read_bytes())))
        os.replace(replacement, path)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert os.stat(path).st_size == before.st_size and os.stat(path).st_mtime_ns == before.st_mtime_ns
        assert "2" not in chapters._fresh_images(plan)

    def _unstamp(self, plan):
        manifest = json.loads(plan.manifest_path.read_text())
        for entry in manifest["images"].values():
            for key in ("size", "mtime_ns", "ino"):
                entry.pop(key)
        plan.manifest_path.write_text(json.dumps(manifest))
        return manifest

    def test_no_stamps_are_written_while_a_worker_holds_the_folder(self, plan, config, extraction):
        chapters.publish_chapters(plan, config)
        before = self._unstamp(plan)
        lock = chapters._folder_lock(plan)
        with lock:
            assert set(chapters._fresh_images(plan)) == {"1", "2"}
        assert json.loads(plan.manifest_path.read_text()) == before

    def test_a_manifest_a_worker_rewrote_meanwhile_keeps_its_entries(self, plan, config, extraction, monkeypatch):
        chapters.publish_chapters(plan, config)
        self._unstamp(plan)
        real = chapters._stamp_manifest

        def worker_first(plan_, proven):
            # A worker replaced chapter 2's entry (new hash) and wrote "failed" between the hash and the stamp.
            manifest = json.loads(plan_.manifest_path.read_text())
            manifest["images"]["2"]["sha256"] = "worker-wrote-this"
            manifest["failed"] = "worker state"
            plan_.manifest_path.write_text(json.dumps(manifest))
            real(plan_, proven)

        monkeypatch.setattr(chapters, "_stamp_manifest", worker_first)
        chapters._fresh_images(plan)
        after = json.loads(plan.manifest_path.read_text())
        assert after["failed"] == "worker state"
        assert after["images"]["2"]["sha256"] == "worker-wrote-this" and "ino" not in after["images"]["2"]
        assert "ino" in after["images"]["1"]


class TestOneFileSeveralPlexItems:
    """A double episode is two Plex items over one file: one bundle, one manifest, a registration per item."""

    @staticmethod
    def _as_item(plan, item, media, part):
        images = chapters._fresh_images(plan)
        target = replace(
            plan.target,
            rating_key=item,
            media_id=media,
            part_id=part,
            chapters=tuple(
                replace(
                    c,
                    thumb_url=(
                        f"/library/media/{media}/chapterImages/{c.index}?mpgChapter={images[str(c.index)]['sha256']}"
                        if str(c.index) in images
                        else ""
                    ),
                )
                for c in plan.target.chapters
            ),
        )
        return replace(plan, target=target)

    def test_each_item_is_registered_once_and_neither_is_redone_by_later_scans(self, plan, config, extraction):
        run, register = extraction
        assert chapters.publish_chapters(plan, config).status == "ready"
        first, second = self._as_item(plan, 1, 2, 3), self._as_item(plan, 7, 8, 9)
        assert not chapters.chapter_work_needed(first)
        assert chapters.chapter_work_needed(second)  # its own registration isn't proven yet
        register.reset_mock()
        assert chapters.publish_chapters(second, config).status == "ready"
        register.assert_called_once()
        assert register.call_args.args[1] is second.target
        assert register.call_args.args[2] == {
            int(index): entry["sha256"] for index, entry in chapters._fresh_images(second).items()
        }
        # Scans reach either item, in either order: neither needs work again, nothing registers again.
        register.reset_mock()
        for _ in range(2):
            for item_plan in (first, second):
                assert not chapters.chapter_work_needed(item_plan)
                assert chapters.publish_chapters(item_plan, config).status == "ready"
        register.assert_not_called()
        run.assert_called()  # only the first publish extracted
        assert run.call_count == 2

    def test_a_regeneration_drops_the_other_items_proof(self, plan, config, extraction):
        run, register = extraction
        chapters.publish_chapters(plan, config)
        second = self._as_item(plan, 7, 8, 9)
        chapters.publish_chapters(second, config)
        manifest = json.loads(plan.manifest_path.read_text())
        assert [v["item"] for v in manifest["also_verified"]] == [1]
        _image(plan.folder / "chapter1.jpg", color="blue")  # the image changed: no registration of it is proven
        assert chapters.chapter_work_needed(self._as_item(plan, 1, 2, 3))
        register.reset_mock()
        chapters.publish_chapters(plan, config, regenerate=True)
        manifest = json.loads(plan.manifest_path.read_text())
        assert manifest["verified"]["item"] == 1
        assert "also_verified" not in manifest
        assert chapters.chapter_work_needed(self._as_item(plan, 7, 8, 9))

    def test_another_servers_or_another_images_proof_is_not_kept(self, plan, config, extraction):
        chapters.publish_chapters(plan, config)
        manifest = json.loads(plan.manifest_path.read_text())
        foreign = {**manifest["verified"], "item": 50, "machine": "other-plex"}
        stale = {**manifest["verified"], "item": 60, "images": {"1": "0" * 64}}
        plan.manifest_path.write_text(json.dumps({**manifest, "also_verified": [foreign, stale]}))
        chapters.publish_chapters(self._as_item(plan, 7, 8, 9), config)
        manifest = json.loads(plan.manifest_path.read_text())
        assert [v["item"] for v in manifest["also_verified"]] == [1]

    def test_a_manifest_written_before_this_reads_as_before(self, plan, config, extraction):
        """Manifests in Plex bundles today hold one ``verified`` and no ``also_verified``."""
        chapters.publish_chapters(plan, config)
        manifest = json.loads(plan.manifest_path.read_text())
        assert "also_verified" not in manifest
        assert not chapters.chapter_work_needed(self._as_item(plan, 1, 2, 3))

    def test_a_changed_image_reextracted_drops_the_other_items_proof(self, plan, config, extraction):
        run, register = extraction
        chapters.publish_chapters(plan, config)
        chapters.publish_chapters(self._as_item(plan, 7, 8, 9), config)
        (plan.folder / "chapter1.jpg").unlink()  # gone: the next publish extracts it again, a new image
        run.side_effect = lambda _source, _start, path, *_args, **_kw: _image(path, "green")
        chapters.publish_chapters(plan, config)
        manifest = json.loads(plan.manifest_path.read_text())
        assert manifest["verified"]["item"] == 1
        assert "also_verified" not in manifest
        assert chapters.chapter_work_needed(self._as_item(plan, 7, 8, 9))

    def test_the_same_items_older_proof_is_never_kept_beside_its_new_one(self, plan, config, extraction):
        """Plex re-adding an item gives it new media and part ids: its old proof must go, not pile up."""
        chapters.publish_chapters(plan, config)
        chapters.publish_chapters(self._as_item(plan, 7, 8, 9), config)
        chapters.publish_chapters(self._as_item(plan, 1, 20, 30), config)
        manifest = json.loads(plan.manifest_path.read_text())
        assert (manifest["verified"]["item"], manifest["verified"]["media"]) == (1, 20)
        assert [(v["item"], v["media"]) for v in manifest["also_verified"]] == [(7, 8)]


class TestEveryPlexItemOfAFile:
    """A job that names no Plex item, for a file Plex lists as several items, handles each item (#420)."""

    @pytest.fixture
    def two_items(self, plan, config, extraction, monkeypatch):
        """Items 1 and 7 share the file; both registered, then Plex's Analyze of item 1 wiped its references."""
        chapters.publish_chapters(plan, config)
        second = TestOneFileSeveralPlexItems._as_item(plan, 7, 8, 9)
        chapters.publish_chapters(second, config)
        wiped = replace(plan.target, chapters=tuple(replace(c, thumb_url="") for c in plan.target.chapters))
        by_item = {1: replace(plan, target=wiped), 7: second}
        prepared = []

        def prepare(server, *_args, item_id_hint=None, **_kwargs):
            prepared.append(item_id_hint)
            if item_id_hint is None:
                if None in by_item:  # one read as a single item, before Plex lists the rest
                    return replace(by_item.pop(None), server=server)
                raise ChapterError("Plex lists this file as more than one item", code="several_items")
            return replace(by_item[item_id_hint], server=server)

        monkeypatch.setattr(chapters, "prepare_chapters", prepare)
        registry = _registry(plan)
        bif = PlexBundleAdapter.bundle_bif_path(
            registry.get_config("plex").output["plex_config_folder"], plan.target.bundle_hash
        )
        bif.parent.mkdir(parents=True)
        bif.write_bytes(b"existing BIF")
        holding = MagicMock(return_value=[1, 7])
        monkeypatch.setattr("media_preview_generator.servers.plex.PlexServer.items_holding_file", holding)
        extraction[0].reset_mock()
        extraction[1].reset_mock()
        return registry, prepared, holding, by_item

    def test_a_job_naming_no_item_restores_the_wiped_item(self, plan, mock_config, extraction, two_items):
        registry, prepared, holding, _by_item = two_items
        run, register = extraction

        result = process_canonical_path(plan.canonical_path, registry, mock_config)

        assert {call.args for call in holding.call_args_list} == {(plan.canonical_path,)}
        assert prepared == [None, 1, 7, 1, 7]  # planned, then each read again after Plex's Analyze
        run.assert_not_called()  # the images are shared and still current
        register.assert_called_once()
        assert register.call_args.args[1].rating_key == 1
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"
        assert result.publishers[0].artifacts["chapters"]["updated"] is True

    def test_a_file_that_becomes_several_items_after_planning_restores_each(
        self, plan, mock_config, extraction, two_items
    ):
        """A new double episode: planned while Plex had one item, read again after Analyze as two."""
        registry, prepared, holding, by_item = two_items
        _run, register = extraction
        by_item[None] = replace(by_item[1], target=None, outcome=chapters.ChapterOutcome("waiting", retryable=True))

        result = process_canonical_path(plan.canonical_path, registry, mock_config)

        assert prepared == [None, None, 1, 7]
        register.assert_called_once()
        assert register.call_args.args[1].rating_key == 1
        assert result.publishers[0].artifacts["chapters"]["status"] == "ready"

    def test_a_regeneration_extracts_the_shared_images_once(
        self, plan, mock_config, extraction, two_items, monkeypatch
    ):
        registry, _prepared, _holding, _by_item = two_items
        run, register = extraction
        previews = multi_server._process_canonical_path_previews
        monkeypatch.setattr(  # the scrubber BIF is kept: only the chapter half is under test
            multi_server,
            "_process_canonical_path_previews",
            lambda *args, **kwargs: previews(*args, **{**kwargs, "regenerate": False}),
        )

        process_canonical_path(plan.canonical_path, registry, mock_config, regenerate=True)

        assert run.call_count == len(plan.target.chapters)
        assert sorted(call.args[1].rating_key for call in register.call_args_list) == [1, 7]

    def test_a_regeneration_skips_past_an_item_without_chapters(
        self, plan, mock_config, extraction, two_items, monkeypatch
    ):
        registry, _prepared, _holding, by_item = two_items
        run, _register = extraction
        by_item[1] = replace(
            by_item[1], target=None, outcome=chapters.ChapterOutcome("skipped", message="Other version")
        )
        previews = multi_server._process_canonical_path_previews
        monkeypatch.setattr(
            multi_server,
            "_process_canonical_path_previews",
            lambda *args, **kwargs: previews(*args, **{**kwargs, "regenerate": False}),
        )

        process_canonical_path(plan.canonical_path, registry, mock_config, regenerate=True)

        assert run.call_count == len(plan.target.chapters)  # item 7 still re-extracts them

    def test_a_job_naming_its_item_handles_only_that_item(self, plan, mock_config, extraction, two_items):
        registry, prepared, holding, _by_item = two_items
        _run, register = extraction

        process_canonical_path(plan.canonical_path, registry, mock_config, item_id_by_server={"plex": "7"})

        holding.assert_not_called()
        assert prepared == [7, 7]
        register.assert_not_called()  # item 7 is still registered

    def test_a_retryable_item_keeps_the_file_retrying(self, plan, mock_config, extraction, two_items):
        registry, _prepared, _holding, by_item = two_items
        _run, register = extraction
        by_item[7] = replace(
            by_item[7],
            target=replace(
                by_item[7].target, chapters=tuple(replace(c, thumb_url="") for c in by_item[7].target.chapters)
            ),
        )

        def refuse(_server, target, *_args, **_kwargs):
            if target.rating_key == 1:
                raise ChapterError("Plex metadata is unsupported", code="unsupported")
            raise ChapterError("Plex is busy", state=Capability.UNREACHABLE)

        register.side_effect = refuse

        result = process_canonical_path(plan.canonical_path, registry, mock_config)

        chapters_artifact = result.publishers[0].artifacts["chapters"]
        assert (chapters_artifact["status"], chapters_artifact["retryable"]) == ("failed", True)
        assert result.publishers[0].status is PublisherStatus.PUBLISHED_CHAPTERS_FAILED

    def test_plex_not_listing_the_items_keeps_the_old_skip(self, plan, mock_config, extraction, two_items):
        registry, prepared, holding, _by_item = two_items
        holding.return_value = []

        result = process_canonical_path(plan.canonical_path, registry, mock_config)

        assert prepared == [None]
        assert result.publishers[0].artifacts["chapters"]["status"] == "skipped"
        extraction[1].assert_not_called()


class TestIdenticalCopies:
    """Identical copies at two paths share Plex's chapter folder; their own modified times must not matter."""

    def test_copies_with_different_modified_times_settle(self, plan, config, extraction, tmp_path):
        run, register = extraction
        copy = tmp_path / "copy.mkv"
        shutil.copyfile(plan.canonical_path, copy)
        os.utime(copy, ns=(1_000_000_000, 1_000_000_000))
        chapters.publish_chapters(plan, config)
        first = TestOneFileSeveralPlexItems._as_item(plan, 1, 2, 3)
        other = TestOneFileSeveralPlexItems._as_item(plan, 7, 8, 9)
        other = replace(
            other,
            canonical_path=str(copy),
            source_fingerprint=get_source_fingerprint(copy),
            target=replace(other.target, source_path=str(copy)),
        )
        chapters.publish_chapters(other, config)
        run.reset_mock()
        register.reset_mock()

        for _scan in range(2):
            for copy_plan in (first, other):
                assert not chapters.chapter_work_needed(copy_plan)
                assert chapters.publish_chapters(copy_plan, config).status == "ready"

        run.assert_not_called()
        register.assert_not_called()


class TestMetadataOnlySourceChange:
    """A hard link, chmod or *arr re-import moves the source's change time but not its content."""

    @pytest.mark.parametrize("how", CTIME_ONLY_CHANGES)
    def test_a_publish_planned_before_the_change_isnt_failed(self, plan, config, extraction, how):
        change_ctime_only(plan.canonical_path, how)

        assert chapters.publish_chapters(plan, config).status == "ready"

    @pytest.mark.parametrize("how", CTIME_ONLY_CHANGES)
    def test_the_next_scan_reuses_the_images(self, plan, config, extraction, how):
        run, _register = extraction
        assert chapters.publish_chapters(plan, config).status == "ready"
        run.reset_mock()

        change_ctime_only(plan.canonical_path, how)

        rescanned = replace(plan, source_fingerprint=get_source_fingerprint(plan.canonical_path))
        assert set(chapters._fresh_images(rescanned)) == {"1", "2"}
        assert chapters.publish_chapters(rescanned, config).status == "ready"
        run.assert_not_called()

    @pytest.mark.parametrize("how", CTIME_ONLY_CHANGES)
    def test_an_unsupported_stamp_still_skips(self, plan, config, extraction, monkeypatch, how):
        run, _register = extraction
        run.side_effect = chapters.UnsupportedChapterFormatError("Unsupported Dolby Vision")
        plan.folder = _prepare_for(plan, config, monkeypatch).folder
        chapters.publish_chapters(plan, config)

        change_ctime_only(plan.canonical_path, how)

        assert _prepare_for(plan, config, monkeypatch).outcome.status == "skipped"

    def test_a_changed_file_waits_for_plexs_new_folder_and_gets_new_images(self, plan, config, extraction, monkeypatch):
        """Plex names the chapter folder after the file's content: a changed file gets a new, empty folder."""
        run, _register = extraction
        plan.folder = _prepare_for(plan, config, monkeypatch).folder
        assert chapters.publish_chapters(plan, config).status == "ready"

        change_source(plan.canonical_path, "size")

        assert chapters.publish_chapters(plan, config).status == "failed"
        assert _prepare_for(plan, config, monkeypatch).outcome.status == "waiting"  # Plex still has the old hash
        run.reset_mock()
        plan.target = replace(
            plan.target,
            bundle_hash=calculate_plex_hash(plan.canonical_path),
            source_size=Path(plan.canonical_path).stat().st_size,
        )
        rescanned = _prepare_for(plan, config, monkeypatch)
        assert rescanned.folder != plan.folder
        assert chapters._fresh_images(rescanned) == {}
        assert chapters.publish_chapters(rescanned, config).status == "ready"
        assert run.call_count == 2

    @pytest.mark.parametrize("how", ["mtime", "inode"])
    def test_same_bytes_fail_the_running_publish_but_the_next_reuses_the_thumbnails(
        self, plan, config, extraction, how
    ):
        """A touched file, or Unraid's mover giving it a new inode (#414), keeps its content and so its folder."""
        run, _register = extraction
        assert chapters.publish_chapters(plan, config).status == "ready"

        change_source(plan.canonical_path, how)

        assert chapters.publish_chapters(plan, config).status == "failed"
        run.reset_mock()
        rescanned = replace(plan, source_fingerprint=get_source_fingerprint(plan.canonical_path))
        assert chapters._fresh_images(rescanned) != {}
        assert chapters.publish_chapters(rescanned, config).status == "ready"
        assert run.call_count == 0
