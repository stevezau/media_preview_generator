"""Chapter extraction at rounded video endpoints and with container HDR metadata."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from media_preview_generator.processing import chapters


@pytest.fixture
def extraction(tmp_path, monkeypatch):
    output = tmp_path / "chapter.jpg"
    track = SimpleNamespace(
        hdr_format=None,
        transfer_characteristics=None,
        color_primaries=None,
        matrix_coefficients=None,
        duration="10000",
    )
    config = SimpleNamespace(tonemap_algorithm="hable", ffmpeg_threads=2, thumbnail_quality=2)
    factory = MagicMock()
    monkeypatch.setattr(chapters, "create_ffmpeg_runner", factory)

    def successful_run(**kwargs):
        Image.new("RGB", (1280, 720)).save(output, "JPEG")
        return 0, 0, "", []

    factory.return_value = successful_run

    def extract(start=10000, **kwargs):
        chapters.extract_chapter_frame(
            "movie.mkv", start, output, config, media_info=SimpleNamespace(video_tracks=[track]), **kwargs
        )

    return track, factory, extract, successful_run, output


@pytest.mark.parametrize("transfer,expected", [("PQ", "smpte2084"), ("HLG", "arib-std-b67")])
def test_declared_hdr_input_colors_reach_zscale(extraction, transfer, expected):
    track, factory, extract, _, _ = extraction
    track.transfer_characteristics = transfer
    track.color_primaries = "BT.2020"
    track.matrix_coefficients = "BT.2020 non-constant"
    extract(0)
    options = factory.call_args.kwargs
    assert options["path_kind"] == "hdr10_zscale"
    assert options["hdr10_zscale_chain"].startswith(f"zscale=tin={expected}:pin=bt2020:min=bt2020nc:t=linear")
    assert options["base_scale"] == "scale=w=1280:h=-2"


@pytest.mark.parametrize("transfer,hdr", [(None, None), (None, "SMPTE ST 2086"), ("BT.709", None)])
def test_unknown_color_properties_are_not_invented(extraction, transfer, hdr):
    track, factory, extract, _, _ = extraction
    track.transfer_characteristics, track.hdr_format = transfer, hdr
    extract(0)
    chain = factory.call_args.kwargs["hdr10_zscale_chain"]
    assert "tin=" not in chain and "pin=" not in chain and "min=" not in chain
    assert factory.call_args.kwargs["path_kind"] == ("hdr10_zscale" if hdr else "sdr")


def test_incompatible_dolby_vision_is_still_rejected(extraction):
    track, factory, extract, _, _ = extraction
    track.hdr_format = "Dolby Vision"
    with pytest.raises(chapters.UnsupportedChapterFormatError):
        extract(0)
    factory.assert_not_called()


@pytest.mark.parametrize("empty_result", [(234, 0, "", ["No filtered frames for output stream"]), (0, 0, "", [])])
def test_final_frame_retry_is_bounded_and_keeps_runner_controls(extraction, empty_result):
    _, factory, extract, success, _ = extraction
    factory.side_effect = [lambda **kwargs: empty_result, success]
    cancel = MagicMock(return_value=False)
    pause = MagicMock(return_value=False)
    extract(10002, cancel_check=cancel, pause_check=pause, ffmpeg_threads_override=3)
    assert [call.kwargs["chapter_start_ms"] for call in factory.call_args_list] == [10002, 9002]
    for call in factory.call_args_list:
        assert call.kwargs["cancel_check"] is cancel
        assert call.kwargs["pause_check"] is pause
        assert call.kwargs["ffmpeg_threads_override"] == 3
        assert call.kwargs["gpu"] is None


def test_endpoint_retry_reaches_video_ending_before_container_duration(extraction):
    track, factory, extract, success, output = extraction
    track.duration = "5458458"
    last_frame_ms = 5457410

    def runner_for_seek(**options):
        if options["chapter_start_ms"] > last_frame_ms:
            return lambda **kwargs: (234, 0, "", ["No filtered frames for output stream"])
        return success

    factory.side_effect = runner_for_seek
    extract(5458240)
    assert [call.kwargs["chapter_start_ms"] for call in factory.call_args_list] == [5458240, 5457240]
    assert factory.call_args.kwargs["base_scale"] == "scale=w=1280:h=-2"
    assert factory.call_args.kwargs["chapter_output"] == str(output)


@pytest.mark.parametrize(
    "duration,start,expected",
    [(10000, 9750, 8750), (10000, 10250, 9250), (10000, 11000, 10000), (1200, 500, 0)],
)
def test_endpoint_retry_stays_within_one_second_before_chapter(extraction, duration, start, expected):
    track, factory, extract, success, _ = extraction
    track.duration = duration
    factory.side_effect = [lambda **kwargs: (234, 0, "", ["No filtered frames"]), success]
    extract(start)
    seeks = [call.kwargs["chapter_start_ms"] for call in factory.call_args_list]
    assert seeks == [start, expected]
    assert 0 < start - seeks[-1] <= 1000


def test_endpoint_retry_does_not_repeat_zero_timestamp(extraction):
    track, factory, extract, _, _ = extraction
    track.duration = 1000
    factory.return_value = lambda **kwargs: (234, 0, "", ["No filtered frames"])
    with pytest.raises(RuntimeError):
        extract(0)
    assert [call.kwargs["chapter_start_ms"] for call in factory.call_args_list] == [0]


@pytest.mark.parametrize("duration", [None, "unknown", float("nan"), float("inf"), 0, -10, 999])
def test_no_endpoint_retry_without_safe_video_duration(extraction, duration):
    track, factory, extract, _, _ = extraction
    track.duration = duration
    factory.return_value = lambda **kwargs: (234, 0, "", ["No filtered frames"])
    with pytest.raises(RuntimeError):
        extract()
    assert factory.call_count == 1


@pytest.mark.parametrize(
    "start,stderr",
    [
        (1000, ["No filtered frames"]),
        (12000, ["No filtered frames"]),
        (10000, ["no path between colorspaces"]),
        (10000, ["File ended prematurely", "No filtered frames"]),
    ],
)
def test_no_retry_for_truncation_early_seek_or_other_filter_failure(extraction, start, stderr):
    _, factory, extract, _, _ = extraction
    factory.return_value = lambda **kwargs: (234, 0, "", stderr)
    with pytest.raises(RuntimeError) as error:
        extract(start)
    assert factory.call_count == 1
    if "File ended prematurely" in stderr:
        assert "source ended prematurely" in str(error.value)


def test_endpoint_fallback_only_runs_once(extraction):
    _, factory, extract, _, _ = extraction
    factory.return_value = lambda **kwargs: (234, 0, "", ["No filtered frames"])
    with pytest.raises(RuntimeError):
        extract()
    assert factory.call_count == 2


def test_cancellation_prevents_endpoint_fallback(extraction):
    _, factory, extract, _, _ = extraction
    factory.return_value = lambda **kwargs: (234, 0, "", ["No filtered frames"])
    with pytest.raises(chapters.CancellationError):
        extract(cancel_check=MagicMock(side_effect=[False, True]))
    assert factory.call_count == 1


def test_invalid_image_does_not_trigger_endpoint_fallback(extraction):
    _, factory, extract, _, output = extraction

    def invalid_image(**kwargs):
        Image.new("RGB", (1278, 720)).save(output, "JPEG")
        return 0, 0, "", []

    factory.return_value = invalid_image
    with pytest.raises(ValueError, match="1280px"):
        extract()
    assert factory.call_count == 1
