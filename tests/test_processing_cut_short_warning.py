"""Tests for telling a cut-short file apart from an unexplained thumbnail shortfall.

Live case: 16 of 17 "This is unexpected — please report it" warnings in one
log were files whose data simply stops early (an interrupted download: a
42 min Blu-ray remux holding 1.2 GB, its video ending at 10.5 min). The
thumbnails were right; the file was short. Only the remaining one — a
complete file that still came up 20 thumbnails short — is worth a report.
"""

import json
import logging
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.processing.generator import (
    _probe_video_end_seconds,
    _warn_if_frame_count_disagrees_with_duration,
)

VIDEO = "/data/TV/Show - S03E16.mkv"
STATED_S = 2549.056  # 42.5 min in the header
INTERVAL = 6


@pytest.fixture
def loguru_caplog(caplog):
    class _PropagateHandler(logging.Handler):
        def emit(self, record):  # pragma: no cover — handler glue
            logging.getLogger(record.name).handle(record)

    handler_id = logger.add(_PropagateHandler(), level="DEBUG", format="{message}")
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.remove(handler_id)


def _media_info(duration_s: float) -> SimpleNamespace:
    return SimpleNamespace(video_tracks=[SimpleNamespace(duration=duration_s * 1000)])


def _warn(image_count: int, video_end_s: float | None) -> MagicMock:
    with patch(
        "media_preview_generator.processing.generator._probe_video_end_seconds", return_value=video_end_s
    ) as probe:
        _warn_if_frame_count_disagrees_with_duration(
            VIDEO, "/tmp/frames", image_count, _media_info(STATED_S), SimpleNamespace(plex_bif_frame_interval=INTERVAL)
        )
    return probe


class TestCutShortWarning:
    def test_says_cut_short_and_suggests_redownload_when_video_stops_where_thumbnails_do(self, loguru_caplog):
        probe = _warn(image_count=106, video_end_s=632.1)

        probe.assert_called_once_with(VIDEO, pytest.approx(STATED_S))
        text = loguru_caplog.text
        assert "'Show - S03E16.mkv' looks cut short" in text
        assert "says it runs 42 min, but its video stops at 11 min" in text
        assert "re-download the file" in text
        assert "please report it" not in text

    def test_asks_for_a_report_when_file_runs_to_its_stated_end(self, loguru_caplog):
        _warn(image_count=106, video_end_s=STATED_S - 0.04)

        assert "please report it" in loguru_caplog.text
        assert "cut short" not in loguru_caplog.text

    def test_asks_for_a_report_when_thumbnails_stop_well_before_where_the_video_stops(self, loguru_caplog):
        """The file is short, but not as short as the thumbnails: the cut doesn't explain it all."""
        _warn(image_count=106, video_end_s=1800.0)

        assert "please report it" in loguru_caplog.text
        assert "cut short" not in loguru_caplog.text

    def test_asks_for_a_report_when_the_probe_has_no_answer(self, loguru_caplog):
        _warn(image_count=106, video_end_s=None)

        assert "please report it" in loguru_caplog.text
        assert "cut short" not in loguru_caplog.text

    def test_does_not_probe_when_there_are_more_thumbnails_than_the_runtime_needs(self, loguru_caplog):
        probe = _warn(image_count=900, video_end_s=632.1)

        probe.assert_not_called()
        assert "please report it" in loguru_caplog.text

    def test_stays_quiet_and_does_not_probe_when_thumbnails_span_the_runtime(self, loguru_caplog):
        probe = _warn(image_count=425, video_end_s=632.1)

        probe.assert_not_called()
        assert "out of sync" not in loguru_caplog.text
        assert "cut short" not in loguru_caplog.text


def _ffprobe_result(packets: list[str], *, start_time: str = "0.000000", returncode: int = 0) -> MagicMock:
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = json.dumps({"packets": [{"pts_time": t} for t in packets], "format": {"start_time": start_time}})
    proc.stderr = "[matroska,webm @ 0x1] File ended prematurely"
    return proc


class TestProbeVideoEnd:
    def test_returns_last_packet_time_when_seek_lands_before_the_cut(self):
        with patch("media_preview_generator.processing.generator.subprocess.run") as run:
            run.return_value = _ffprobe_result(["631.840000", "632.131000", "632.048000"])
            assert _probe_video_end_seconds(VIDEO, STATED_S) == pytest.approx(632.131)

        cmd = run.call_args.args[0]
        assert cmd[0] == "ffprobe"
        assert cmd[cmd.index("-select_streams") + 1] == "v:0"
        assert cmd[cmd.index("-read_intervals") + 1] == "2539%"
        assert cmd[cmd.index("-show_entries") + 1] == "packet=pts_time:format=start_time"
        assert cmd[-1] == VIDEO
        assert run.call_args.kwargs["timeout"] == 30

    def test_subtracts_the_container_start_time(self):
        """MPEG-TS timestamps rarely start at zero; the runtime is measured from the first one."""
        with patch("media_preview_generator.processing.generator.subprocess.run") as run:
            run.return_value = _ffprobe_result(["1401.4", "1402.4"], start_time="1.400000")
            assert _probe_video_end_seconds(VIDEO, 1405.0) == pytest.approx(1401.0)

    def test_seeks_from_the_start_when_the_stated_runtime_is_under_ten_seconds(self):
        with patch("media_preview_generator.processing.generator.subprocess.run") as run:
            run.return_value = _ffprobe_result(["4.0"])
            _probe_video_end_seconds(VIDEO, 5.0)

        cmd = run.call_args.args[0]
        assert cmd[cmd.index("-read_intervals") + 1] == "0%"

    def test_skips_packets_without_a_timestamp(self):
        with patch("media_preview_generator.processing.generator.subprocess.run") as run:
            run.return_value = _ffprobe_result(["N/A", "12.5", "N/A"])
            assert _probe_video_end_seconds(VIDEO, 20.0) == pytest.approx(12.5)

    @pytest.mark.parametrize(
        "result",
        [
            pytest.param(_ffprobe_result([]), id="no-packets"),
            pytest.param(_ffprobe_result(["632.1"], returncode=1), id="ffprobe-failed"),
            pytest.param(MagicMock(spec=subprocess.CompletedProcess, returncode=0, stdout="not json"), id="bad-json"),
        ],
    )
    def test_returns_none_when_ffprobe_has_no_usable_answer(self, result):
        with patch("media_preview_generator.processing.generator.subprocess.run", return_value=result):
            assert _probe_video_end_seconds(VIDEO, STATED_S) is None

    @pytest.mark.parametrize(
        "error",
        [subprocess.TimeoutExpired(cmd="ffprobe", timeout=30), FileNotFoundError("ffprobe")],
        ids=["timeout", "ffprobe-missing"],
    )
    def test_returns_none_when_ffprobe_does_not_finish(self, error):
        with patch("media_preview_generator.processing.generator.subprocess.run", side_effect=error):
            assert _probe_video_end_seconds(VIDEO, STATED_S) is None
