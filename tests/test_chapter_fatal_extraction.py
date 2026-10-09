"""A stalled or corrupt source must not consume one watchdog interval per chapter."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.output.plex_hash import calculate_plex_hash, get_source_fingerprint
from media_preview_generator.processing import chapters
from media_preview_generator.servers.plex_chapters import Chapter, ChapterTarget

CORRUPTION = "[matroska,webm @ 0x1234] 0x00 at pos 3843235529 invalid as first byte of an EBML number"


@pytest.fixture
def extraction(tmp_path, monkeypatch):
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"stable source")
    target = ChapterTarget(
        1,
        2,
        3,
        calculate_plex_hash(source),
        str(source),
        source.stat().st_size,
        100,
        tuple(Chapter(i + 1, i * 1000, (i + 1) * 1000, "", i + 10, 20) for i in range(3)),
        "machine",
    )
    plan = chapters.ChapterPlan(
        MagicMock(), str(source), get_source_fingerprint(source), tmp_path / "Chapters", {}, target
    )
    track = SimpleNamespace(duration=10000, transfer_characteristics=None, hdr_format=None)
    media = SimpleNamespace(video_tracks=[track])
    monkeypatch.setattr(chapters.MediaInfo, "parse", lambda _path: media)
    config = SimpleNamespace(tonemap_algorithm="hable", ffmpeg_threads=2, thumbnail_quality=2)
    register = MagicMock()
    monkeypatch.setattr("media_preview_generator.servers.plex_chapters.register_chapters", register)
    return plan, config, media, register


@pytest.mark.parametrize(
    "stderr,retryable",
    [
        ([chapters.STALL_WATCHDOG_LINE], True),
        ([CORRUPTION], False),
        ([CORRUPTION, chapters.STALL_WATCHDOG_LINE], False),
    ],
)
def test_fatal_source_stop_preserves_completed_images_and_resume_skips_them(extraction, monkeypatch, stderr, retryable):
    plan, config, _media, register = extraction

    def factory(**options):
        def run(**_kwargs):
            if options["chapter_start_ms"] == 1000:
                return -9, 0, "", stderr
            Image.new("RGB", (1280, 720)).save(options["chapter_output"], "JPEG")
            return 0, 0, "", []

        return run

    runner = MagicMock(side_effect=factory)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    progress = []
    result = chapters.publish_chapters(plan, config, chapter_progress_callback=progress.append)
    assert (result.status, result.completed, result.total, result.retryable) == ("failed", 1, 3, retryable)
    assert "remaining chapter attempts stopped" in result.message
    assert [c.kwargs["chapter_start_ms"] for c in runner.call_args_list] == [0, 1000]
    assert progress[-1] == {"stage": "failed", "processed": 2, "total": 3, "ready": 1, "failed": 1}
    register.assert_not_called()
    assert set(chapters._fresh_images(plan)) == {"1"}
    ready_bytes = (plan.folder / "chapter1.jpg").read_bytes()
    runner.reset_mock()
    chapters.publish_chapters(plan, config)
    assert [c.kwargs["chapter_start_ms"] for c in runner.call_args_list] == [1000]
    assert (plan.folder / "chapter1.jpg").read_bytes() == ready_bytes
    assert not (plan.folder / "chapter3.jpg").exists()


@pytest.mark.parametrize("stderr", [[chapters.STALL_WATCHDOG_LINE], [CORRUPTION]])
def test_fatal_error_cannot_trigger_near_eof_fallback(extraction, monkeypatch, stderr):
    plan, config, media, _register = extraction
    runner = MagicMock(return_value=lambda **_kwargs: (-9, 0, "", [*stderr, "No filtered frames"]))
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    with pytest.raises((chapters.ChapterExtractionStalledError, chapters.ChapterSourceCorruptionError)):
        chapters.extract_chapter_frame(plan.canonical_path, 10000, plan.folder / "frame.jpg", config, media_info=media)
    assert runner.call_count == 1


@pytest.mark.parametrize(
    "returncode,stderr",
    [(234, ["No filtered frames"]), (234, ["File ended prematurely"]), (-9, [])],
)
def test_individual_frame_failure_does_not_abort_other_chapters(extraction, monkeypatch, returncode, stderr):
    plan, config, _media, register = extraction

    def factory(**options):
        def run(**_kwargs):
            if options["chapter_start_ms"] == 1000:
                return returncode, 0, "", stderr
            Image.new("RGB", (1280, 720)).save(options["chapter_output"], "JPEG")
            return 0, 0, "", []

        return run

    runner = MagicMock(side_effect=factory)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    result = chapters.publish_chapters(plan, config)
    assert (result.status, result.completed, result.total, result.retryable) == ("failed", 2, 3, False)
    assert runner.call_count == 3
    assert set(chapters._fresh_images(plan)) == {"1", "3"}
    register.assert_not_called()


HEVC_PARAMETER_FAILURE = [
    "[hevc @ 0x123] VPS 0 does not exist",
    "[hevc @ 0x123] SPS 0 does not exist.",
    "[hevc @ 0x456] PPS id out of range: 0",
    "[hevc @ 0x456] Skipping invalid undecodable NALU: 20",
]


@pytest.mark.parametrize("returncode", [0, 183, -9])
def test_hevc_parameter_initialization_failure_is_nonretryable_even_with_success_exit(returncode):
    with pytest.raises(RuntimeError, match="HEVC video parameters") as caught:
        chapters._check_fatal_extraction(returncode, HEVC_PARAMETER_FAILURE, 377210)
    assert not chapters._failure(caught.value).retryable
    assert "corrupt" not in str(caught.value).lower()


@pytest.mark.parametrize(
    "diagnostics",
    [
        HEVC_PARAMETER_FAILURE[:2],
        HEVC_PARAMETER_FAILURE[2:],
        ["title: " + line for line in HEVC_PARAMETER_FAILURE],
        [line.replace("[hevc", "[h264") for line in HEVC_PARAMETER_FAILURE],
    ],
)
def test_partial_or_unrelated_decoder_diagnostics_do_not_claim_hevc_failure(diagnostics):
    chapters._check_fatal_extraction(0, diagnostics, 377210)
    with pytest.raises(chapters.ChapterExtractionStalledError):
        chapters._check_fatal_extraction(-9, [*diagnostics, chapters.DIAGNOSTIC_LIMIT_LINE], 377210)


def test_hevc_failure_stops_source_but_explicit_retry_rechecks_missing_chapters(extraction, monkeypatch):
    plan, config, _media, register = extraction

    def factory(**options):
        def run(**_kwargs):
            if options["chapter_start_ms"]:
                return -9, 0, "", [*HEVC_PARAMETER_FAILURE, chapters.DIAGNOSTIC_LIMIT_LINE]
            Image.new("RGB", (1280, 720)).save(options["chapter_output"], "JPEG")
            return 0, 0, "", []

        return run

    runner = MagicMock(side_effect=factory)
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    first = chapters.publish_chapters(plan, config)
    assert (first.status, first.completed, first.total, first.retryable) == ("failed", 1, 3, False)
    assert "HEVC video parameters" in first.message
    assert [call.kwargs["chapter_start_ms"] for call in runner.call_args_list] == [0, 1000]
    ready = (plan.folder / "chapter1.jpg").read_bytes()
    runner.reset_mock()
    second = chapters.publish_chapters(plan, config)
    assert not second.retryable and second.completed == 1
    assert [call.kwargs["chapter_start_ms"] for call in runner.call_args_list] == [1000]
    assert (plan.folder / "chapter1.jpg").read_bytes() == ready
    register.assert_not_called()


def test_watchdog_during_endpoint_fallback_still_aborts(extraction, monkeypatch):
    plan, config, media, _register = extraction
    runner = MagicMock(
        side_effect=[
            lambda **_kwargs: (234, 0, "", ["No filtered frames"]),
            lambda **_kwargs: (-9, 0, "", [chapters.STALL_WATCHDOG_LINE]),
        ]
    )
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)
    with pytest.raises(chapters.ChapterExtractionStalledError):
        chapters.extract_chapter_frame(plan.canonical_path, 10000, plan.folder / "frame.jpg", config, media_info=media)
    assert runner.call_count == 2


@pytest.mark.parametrize("returncode", [0, 234])
def test_confirmed_empty_seek_beyond_duration_has_nonretryable_timestamp_error(extraction, monkeypatch, returncode):
    plan, config, media, _register = extraction
    runner = MagicMock(return_value=lambda **_kwargs: (returncode, 0, "", ["No filtered frames"]))
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)

    with pytest.raises(ValueError, match="Chapter timestamp 75000ms is outside.*duration \\(10000ms\\)") as caught:
        chapters.extract_chapter_frame(plan.canonical_path, 75000, plan.folder / "frame.jpg", config, media_info=media)

    assert not chapters._failure(caught.value).retryable
    assert runner.call_count == 1
    assert runner.call_args.kwargs["chapter_start_ms"] == 75000
    assert runner.call_args.kwargs["chapter_output"] == str(plan.folder / "frame.jpg")


@pytest.mark.parametrize("duration", [5679279, "5679279"])
def test_container_duration_diagnoses_missing_video_duration_without_guessing_frames(extraction, monkeypatch, duration):
    plan, config, media, _register = extraction
    media.video_tracks[0].duration = None
    media.general_tracks = [SimpleNamespace(duration=duration)]
    runner = MagicMock(return_value=lambda **_kwargs: (0, 0, "", ["No filtered frames"]))
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)

    with pytest.raises(ValueError, match="Chapter timestamp 5753160ms is outside.*duration \\(5679279ms\\)") as caught:
        chapters.extract_chapter_frame(
            plan.canonical_path, 5753160, plan.folder / "frame.jpg", config, media_info=media
        )

    assert not chapters._failure(caught.value).retryable
    assert [call.kwargs["chapter_start_ms"] for call in runner.call_args_list] == [5753160]


@pytest.mark.parametrize("duration", [None, "malformed", float("nan"), float("inf"), 0, -1])
def test_unknown_container_duration_reports_missing_frame_without_temporary_path(extraction, monkeypatch, duration):
    plan, config, media, _register = extraction
    media.video_tracks[0].duration = None
    media.general_tracks = [SimpleNamespace(duration=duration)]
    runner = MagicMock(return_value=lambda **_kwargs: (0, 0, "", ["No filtered frames"]))
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)

    with pytest.raises(ValueError, match="No video frame was available for chapter timestamp 75000ms") as caught:
        chapters.extract_chapter_frame(plan.canonical_path, 75000, plan.folder / "frame.jpg", config, media_info=media)

    assert "outside" not in str(caught.value)
    assert str(plan.folder) not in str(caught.value)
    assert not chapters._failure(caught.value).retryable
    assert [call.kwargs["chapter_start_ms"] for call in runner.call_args_list] == [75000]


def test_container_duration_does_not_enable_video_endpoint_fallback(extraction, monkeypatch):
    plan, config, media, _register = extraction
    media.video_tracks[0].duration = None
    media.general_tracks = [SimpleNamespace(duration=10000)]
    runner = MagicMock(return_value=lambda **_kwargs: (0, 0, "", ["No filtered frames"]))
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", runner)

    with pytest.raises(ValueError, match="No video frame was available"):
        chapters.extract_chapter_frame(plan.canonical_path, 10000, plan.folder / "frame.jpg", config, media_info=media)

    assert [call.kwargs["chapter_start_ms"] for call in runner.call_args_list] == [10000]


def test_successful_frame_is_rejected_when_demuxer_proves_corruption(extraction, monkeypatch):
    plan, config, media, _register = extraction
    output = plan.folder.parent / "frame.jpg"

    def run(**_kwargs):
        Image.new("RGB", (1280, 720)).save(output, "JPEG")
        return 0, 0, "", [CORRUPTION]

    monkeypatch.setattr(chapters, "create_ffmpeg_runner", lambda **_kwargs: run)
    with pytest.raises(chapters.ChapterSourceCorruptionError):
        chapters.extract_chapter_frame(plan.canonical_path, 0, output, config, media_info=media)


@pytest.mark.parametrize("prefix", ["matroska", "matroska,webm", "in#0/matroska,webm", "in#12/matroska"])
@pytest.mark.parametrize(
    "message",
    [
        "0x00 at pos 3843235529 invalid as first byte of an EBML number",
        "0x00 at pos 254 (0xfe) invalid as first byte of an EBML number",
        "Length 5 indicated by an EBML number's first byte 0x0b at pos 197030 (0x301a6) exceeds max length 4.",
    ],
)
def test_structural_demux_errors_are_fatal_even_after_ffmpeg_reports_success(prefix, message):
    with pytest.raises(chapters.ChapterSourceCorruptionError):
        chapters._check_fatal_extraction(0, [f"[{prefix} @ 0x1234] {message}"], 4000)


@pytest.mark.parametrize(
    "line",
    [
        "[in#0/matroska,webm @ 0x1234] Unknown entry 0x6DB8 at pos. 19413194771",
        "[ffv1 @ 0x1234] Length 5 indicated by an EBML number's first byte 0x0b exceeds max length 4.",
        "[matroska,webm @ 0x1234] title: Length 5 indicated by an EBML number's first byte 0x0b exceeds max length 4.",
        "Metadata title: [matroska @ 0x1234] 0x00 at pos 1 invalid as first byte of an EBML number",
        "[in#0/matroska,webm @ 0x1234] Invalid data found when processing input",
    ],
)
def test_optional_unknown_elements_metadata_and_generic_codec_errors_are_not_proven_corruption(line):
    chapters._check_fatal_extraction(0, [line], 4000)
