"""Tests for media_preview_generator.markers.probe (spec §5.1)."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers.probe import Chapter, ProbeError, ffprobe_path_for, probe_media

RUN = "media_preview_generator.markers.probe.subprocess.run"


def _ok(payload):
    return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")


def test_parses_duration_and_chapters_and_passes_exact_args():
    payload = {
        "format": {"duration": "1321.472000"},
        "chapters": [
            {"start_time": "0.000000", "end_time": "127.961000", "tags": {"title": "Chapter 1"}},
            {"start_time": "127.961000", "end_time": "159.826000", "tags": {"TITLE": "Title Sequence"}},
            {"start_time": "159.826000", "end_time": "1321.472000", "tags": {}},
        ],
    }
    with patch(RUN, return_value=_ok(payload)) as run:
        probe = probe_media("/m/a.mkv", ffprobe="/usr/bin/ffprobe")
    args, kwargs = run.call_args
    assert args[0] == [
        "/usr/bin/ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_chapters",
        "/m/a.mkv",
    ]
    assert kwargs["timeout"] == 60.0 and kwargs["capture_output"] is True and kwargs["text"] is True
    assert probe.duration_ms == 1_321_472
    assert probe.chapters == (
        Chapter(0, 127_961, "Chapter 1"),
        Chapter(127_961, 159_826, "Title Sequence"),
        Chapter(159_826, 1_321_472, ""),
    )


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ({"start_time": "30000.000000"}, 30_000_000),  # a recorded-TV .ts with a PCR base
        ({"start_time": "0.000000"}, 0),
        ({"start_time": "-0.023000"}, -23),
        ({}, None),
        ({"start_time": "N/A"}, None),
    ],
)
def test_container_start_time(fmt, expected):
    # Credits timestamps are read with -copyts, so the container's own start has to be subtracted from them.
    with patch(RUN, return_value=_ok({"format": fmt, "chapters": []})):
        assert probe_media("/m/a.ts", ffprobe="ffprobe").start_time_ms == expected


def test_missing_duration_is_none():
    with patch(RUN, return_value=_ok({"format": {}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


@pytest.mark.parametrize(
    "side_effect",
    [
        subprocess.TimeoutExpired(cmd="ffprobe", timeout=60),
        FileNotFoundError("ffprobe"),
        PermissionError("ffprobe"),
        None,  # non-zero return code
    ],
)
def test_failures_raise_probe_error(side_effect):
    ret = MagicMock(returncode=1, stdout="", stderr="Invalid data found when processing input")
    with patch(RUN, side_effect=side_effect, return_value=ret):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_invalid_json_raises():
    with patch(RUN, return_value=MagicMock(returncode=0, stdout="not json", stderr="")):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_ffprobe_path_prefers_sibling_of_ffmpeg(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")
    ffprobe.chmod(0o755)
    assert ffprobe_path_for(str(ffmpeg)) == str(ffprobe)


def test_ffprobe_path_falls_back_to_path_lookup(tmp_path):
    with patch("media_preview_generator.markers.probe.shutil.which", return_value="/usr/bin/ffprobe"):
        assert ffprobe_path_for(str(tmp_path / "ffmpeg")) == "/usr/bin/ffprobe"
        assert ffprobe_path_for(None) == "/usr/bin/ffprobe"


def test_ffprobe_path_skips_non_executable_sibling(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffprobe = tmp_path / "ffprobe"
    ffmpeg.write_text("")
    ffprobe.write_text("")
    ffprobe.chmod(0o644)  # a sibling file that exists but isn't runnable must not be trusted
    with patch("media_preview_generator.markers.probe.shutil.which", return_value="/usr/bin/ffprobe"):
        assert ffprobe_path_for(str(ffmpeg)) == "/usr/bin/ffprobe"


def test_ms_rounds_to_nearest_instead_of_truncating():
    # 1.0009 * 1000 = 1000.9: round -> 1001, truncation -> 1000. Ties matter for BIF timestamps.
    with patch(RUN, return_value=_ok({"format": {"duration": "1.0009"}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms == 1001


def test_nonzero_returncode_raises_even_when_stdout_is_valid_json():
    payload = json.dumps({"format": {}, "chapters": []})
    ret = MagicMock(returncode=1, stdout=payload, stderr="Invalid data found when processing input")
    with patch(RUN, return_value=ret):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_nonzero_returncode_error_message_includes_path():
    ret = MagicMock(returncode=1, stdout="", stderr="broken")
    with patch(RUN, return_value=ret):
        with pytest.raises(ProbeError, match=r"/m/a\.mkv"):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_probe_error_message_truncates_huge_stderr():
    huge = "x" * 5000
    ret = MagicMock(returncode=1, stdout="", stderr=huge)
    with patch(RUN, return_value=ret):
        with pytest.raises(ProbeError) as exc_info:
            probe_media("/m/a.mkv", ffprobe="ffprobe")
    assert huge not in str(exc_info.value)
    assert len(str(exc_info.value)) < 400


def test_timeout_s_is_passed_through_verbatim():
    with patch(RUN, return_value=_ok({"format": {}, "chapters": []})) as run:
        probe_media("/m/a.mkv", ffprobe="ffprobe", timeout_s=5)
    assert run.call_args.kwargs["timeout"] == 5


def test_duration_non_numeric_string_is_none():
    with patch(RUN, return_value=_ok({"format": {"duration": "abc"}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


def test_duration_inf_is_none():
    with patch(RUN, return_value=_ok({"format": {"duration": "inf"}, "chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


def test_non_dict_json_raises():
    with patch(RUN, return_value=MagicMock(returncode=0, stdout="[]", stderr="")):
        with pytest.raises(ProbeError):
            probe_media("/m/a.mkv", ffprobe="ffprobe")


def test_chapters_key_absent_defaults_to_no_chapters():
    with patch(RUN, return_value=_ok({"format": {"duration": "10.0"}})):
        probe = probe_media("/m/a.mkv", ffprobe="ffprobe")
    assert probe.chapters == ()


def test_format_key_absent_duration_is_none():
    with patch(RUN, return_value=_ok({"chapters": []})):
        assert probe_media("/m/a.mkv", ffprobe="ffprobe").duration_ms is None


def test_chapter_with_missing_start_time_is_skipped():
    payload = {"format": {"duration": "10.0"}, "chapters": [{"end_time": "5.0", "tags": {"title": "Bad"}}]}
    with patch(RUN, return_value=_ok(payload)):
        probe = probe_media("/m/a.mkv", ffprobe="ffprobe")
    assert probe.chapters == ()


def test_ffprobe_path_returns_literal_when_which_finds_nothing():
    with patch("media_preview_generator.markers.probe.shutil.which", return_value=None):
        assert ffprobe_path_for(None) == "ffprobe"


def test_ffprobe_path_skips_directory_named_ffprobe(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("")
    (tmp_path / "ffprobe").mkdir()  # a directory, not a binary -- os.access(X_OK) alone would pass this
    with patch("media_preview_generator.markers.probe.shutil.which", return_value="/usr/bin/ffprobe"):
        assert ffprobe_path_for(str(ffmpeg)) == "/usr/bin/ffprobe"
