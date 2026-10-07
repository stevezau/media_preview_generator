"""Plex's loudnorm command, its report, and the ln:* fields Plex stores (loudness.analyze)."""

from __future__ import annotations

import io
import shutil
import subprocess
import time
from unittest.mock import MagicMock

import pytest

from media_preview_generator.loudness import analyze

# Plex's transcoder and ffmpeg 8.1.2 printed this for Puffin Rock S01E34-E36 stream 1 (2026-09-29); Plex stored the
# ln:* fields below for it (media_streams.extra_data).
REPORT = """[Parsed_loudnorm_0 @ 0x5da4] \n{
\t"input_i" : "-23.23",
\t"input_tp" : "-8.57",
\t"input_lra" : "7.30",
\t"input_thresh" : "-33.93",
\t"output_i" : "-16.21",
\t"output_tp" : "-1.00",
\t"output_lra" : "6.30",
\t"output_thresh" : "-26.83",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.21"
}
[out#0/null @ 0x5da4] video:0KiB audio:318688KiB"""
PLEX_FIELDS = {
    "ln:gainOffset": "0.21",
    "ln:loudness": "-23.23",
    "ln:loudnessAnalysisVersion": "0.02",
    "ln:lra": "7.30",
    "ln:peak": "-8.57",
    "ln:threshold": "-33.93",
}


def test_command_is_plexs_own_on_the_cpu():
    assert analyze.command("ffmpeg", "/m/a.mkv", 3) == [
        "ffmpeg", "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", "/m/a.mkv", "-map", "0:3",
        "-af", "loudnorm=I=-16:TP=-1:LRA=9:print_format=json", "-f", "null", "-",
    ]  # fmt: skip


@pytest.mark.parametrize(("codec", "no_drc"), [("eac3", True), ("ac3", False), ("aac", False)])
def test_eac3_is_decoded_without_drc_as_plexs_dolby_decoder_does(codec, no_drc):
    cmd = analyze.command("ffmpeg", "/m/a.mkv", 1, codec)
    assert (cmd[5:7] == ["-drc_scale", "0"]) is no_drc
    assert cmd[-9:] == ["-i", "/m/a.mkv", "-map", "0:1", "-af", analyze.LOUDNORM_FILTER, "-f", "null", "-"]


def test_report_maps_to_plexs_fields_verbatim():
    assert analyze.ln_fields(analyze.parse(REPORT)) == PLEX_FIELDS


def test_no_report_is_an_error():
    with pytest.raises(analyze.LoudnessError, match="no loudnorm report"):
        analyze.parse("Error opening input file")


@pytest.mark.parametrize("peak", ["-inf", "-16.32"])
def test_silent_and_short_streams_preserve_plexs_verified_sentinels(peak):
    report = {**analyze.parse(REPORT), "input_i": "-inf", "input_tp": peak, "target_offset": "inf"}
    fields = analyze.ln_fields(report)
    assert fields["ln:loudness"] == "-inf"
    assert fields["ln:peak"] == peak
    assert fields["ln:gainOffset"] == "inf"
    assert analyze.valid_measurements(fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"input_i": "nan"},
        {"input_tp": "inf"},
        {"input_i": "-inf"},
        {"target_offset": "inf"},
        {"input_i": "-inf", "target_offset": "inf", "input_lra": "inf"},
    ],
)
def test_other_nonfinite_measurements_are_refused(changes):
    with pytest.raises(analyze.LoudnessError):
        analyze.ln_fields({**analyze.parse(REPORT), **changes})


def test_timeout_scales_with_length_within_bounds():
    assert analyze.timeout_for(None) == analyze.MIN_TIMEOUT_S
    assert analyze.timeout_for(3_600_000) == 10_800
    assert analyze.timeout_for(10 * 3_600_000) == analyze.MAX_TIMEOUT_S


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg with the loudnorm filter")
def test_run_measures_a_real_tone(tmp_path):
    tone = tmp_path / "tone.mka"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=5",
            "-af",
            "volume=-20dB",
            str(tone),
        ],
        check=True,
    )
    fields = analyze.run("ffmpeg", str(tone), 0, duration_ms=5000)
    assert set(fields) == set(PLEX_FIELDS)
    assert fields["ln:loudnessAnalysisVersion"] == "0.02"
    assert -50 < float(fields["ln:loudness"]) < -30  # ffmpeg's sine is -18 dBFS; -20 dB on top


def test_run_reports_ffmpeg_failure(tmp_path):
    missing = str(tmp_path / "nope.mkv")
    with pytest.raises(analyze.LoudnessError):
        analyze.run(shutil.which("ffmpeg") or "false", missing, 0, duration_ms=1000)


def test_an_unreadable_or_incomplete_report_is_an_error():
    with pytest.raises(analyze.LoudnessError, match="unreadable"):
        analyze.parse("[Parsed_loudnorm_0] {not json}")
    with pytest.raises(analyze.LoudnessError, match="lacks a usable input_i"):
        analyze.ln_fields({k: v for k, v in analyze.parse(REPORT).items() if k != "input_i"})


def _fake_ffmpeg(tmp_path, body: str) -> str:
    script = tmp_path / "ffmpeg"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return str(script)


def test_a_stalled_ffmpeg_is_killed_at_the_time_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "timeout_for", lambda duration_ms: 0.3)
    monkeypatch.setattr(analyze, "_POLL_S", 0.05)
    with pytest.raises(analyze.LoudnessError, match="timed out"):
        analyze.run(_fake_ffmpeg(tmp_path, "exec sleep 30"), "/m/a.mkv", 1, duration_ms=None)


def test_a_cancelled_job_kills_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "_POLL_S", 0.05)
    with pytest.raises(analyze.LoudnessError, match="cancelled"):
        analyze.run(_fake_ffmpeg(tmp_path, "exec sleep 30"), "/m/a.mkv", 1, duration_ms=None, cancel_check=lambda: True)


def test_a_job_cancelled_while_paused_never_starts_ffmpeg(tmp_path):
    with pytest.raises(analyze.LoudnessError, match="cancelled"):
        analyze.run(
            str(tmp_path / "no-ffmpeg"),
            "/m/a.mkv",
            1,
            duration_ms=None,
            cancel_check=lambda: True,
            pause_check=lambda: True,
        )


def test_run_reads_the_report_ffmpeg_prints(tmp_path):
    ffmpeg = _fake_ffmpeg(tmp_path, "cat >&2 <<'EOT'\n" + REPORT + "\nEOT")
    assert analyze.run(ffmpeg, "/m/a.mkv", 1, duration_ms=1000) == PLEX_FIELDS


@pytest.mark.parametrize("index", [-1, True, 1.5, "1", None])
def test_invalid_stream_index_never_becomes_a_stream_map(index):
    with pytest.raises(analyze.LoudnessError, match="nonnegative integer"):
        analyze.command("ffmpeg", "/m/a.mkv", index)


def test_already_cancelled_unpaused_job_never_launches_ffmpeg(monkeypatch):
    launch = MagicMock()
    monkeypatch.setattr(analyze.subprocess, "Popen", launch)
    with pytest.raises(analyze.LoudnessError, match="cancelled"):
        analyze.run("ffmpeg", "/m/a.mkv", 1, duration_ms=1000, cancel_check=lambda: True)
    launch.assert_not_called()


def test_fast_completion_does_not_return_measurements_after_cancellation(monkeypatch):
    cancelled = False

    def wait(**kwargs):
        nonlocal cancelled
        cancelled = True

    proc = MagicMock(returncode=0)
    proc.stdout = io.BytesIO(b"")
    proc.stderr = io.BytesIO(REPORT.encode())
    proc.wait.side_effect = wait
    monkeypatch.setattr(analyze.subprocess, "Popen", MagicMock(return_value=proc))
    with pytest.raises(analyze.LoudnessError, match="cancelled"):
        analyze.run("ffmpeg", "/m/a.mkv", 1, duration_ms=1000, cancel_check=lambda: cancelled)


def test_missing_ffmpeg_is_an_item_analysis_error(monkeypatch):
    monkeypatch.setattr(analyze.subprocess, "Popen", MagicMock(side_effect=FileNotFoundError(2, "No such file")))
    with pytest.raises(analyze.LoudnessError, match="Could not start ffmpeg"):
        analyze.run("missing-ffmpeg", "/m/a.mkv", 1, duration_ms=1000)


def test_progress_is_requested_on_stdout():
    cmd = analyze.command("ffmpeg", "/m/a.mkv", 1)
    assert cmd[cmd.index("-progress") + 1] == "pipe:1"


def test_progress_blocks_report_seconds_and_speed_and_na_speed_is_none():
    seen = []
    stream = io.BytesIO(
        b"out_time_us=2000000\nspeed=1.4x\nprogress=continue\n"
        b"out_time_ms=5500000\nspeed=N/A\nprogress=continue\n"
        b"out_time_us=N/A\nspeed=2.0x\nprogress=continue\n"
        b"out_time_us=9000000\nspeed= 3.0x\nprogress=end\n"
    )
    analyze.read_progress(stream, lambda seconds, speed: seen.append((seconds, speed)))
    assert seen == [(2.0, 1.4), (5.5, None), (9.0, 3.0)]  # the N/A time block reports nothing


def test_a_failing_progress_callback_does_not_stop_the_drain():
    def boom(seconds, speed):
        raise RuntimeError("x")

    stream = io.BytesIO(b"out_time_us=1\nprogress=continue\nout_time_us=2\nprogress=end\n")
    analyze.read_progress(stream, boom)
    assert stream.read() == b""


def test_run_reports_progress_and_drains_stdout_more_than_a_pipe_holds(tmp_path):
    # ~1 MB on stdout, well past a 64 KB pipe: ffmpeg would block if nobody read it.
    body = (
        "i=0; while [ $i -lt 20000 ]; do echo 'frame=0 pad_pad_pad_pad_pad_pad_pad_pad_pad'; i=$((i+1)); done\n"
        "printf 'out_time_us=3000000\\nspeed=2.5x\\nprogress=end\\n'\n"
        "cat >&2 <<'EOT'\n" + REPORT + "\nEOT"
    )
    seen = []
    ffmpeg = _fake_ffmpeg(tmp_path, body)
    fields = analyze.run(ffmpeg, "/m/a.mkv", 1, duration_ms=6000, on_progress=lambda s, x: seen.append((s, x)))
    assert fields == PLEX_FIELDS
    assert seen == [(3.0, 2.5)]


def test_a_cancel_still_stops_a_chatty_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "_POLL_S", 0.05)
    chatty = _fake_ffmpeg(tmp_path, "while true; do echo out_time_us=1; echo progress=continue; sleep 0.01; done")
    calls = []
    with pytest.raises(analyze.LoudnessError, match="cancelled"):
        analyze.run(
            chatty,
            "/m/a.mkv",
            1,
            duration_ms=None,
            cancel_check=lambda: len(calls) > 3 or calls.append(1),
            on_progress=lambda s, x: None,
        )


def test_a_pause_window_holds_ffmpeg_then_the_fields_return(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "_POLL_S", 0.05)
    resume_at = time.monotonic() + 0.3
    ffmpeg = _fake_ffmpeg(tmp_path, "cat >&2 <<'EOT'\n" + REPORT + "\nEOT")
    started = time.monotonic()
    fields = analyze.run(ffmpeg, "/m/a.mkv", 1, duration_ms=1000, pause_check=lambda: time.monotonic() < resume_at)
    assert fields == PLEX_FIELDS
    assert time.monotonic() - started >= 0.25


def test_a_kill_is_time_bound_when_a_child_keeps_the_pipes_open(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "_POLL_S", 0.05)
    monkeypatch.setattr(analyze, "KILL_WAIT_S", 0.3)
    # The background sleep inherits both pipes and outlives the killed shell, as a stalled mount's child would.
    ffmpeg = _fake_ffmpeg(tmp_path, "sleep 3 &\nexec sleep 30")
    calls = []
    started = time.monotonic()
    with pytest.raises(analyze.LoudnessError, match="cancelled"):
        analyze.run(ffmpeg, "/m/a.mkv", 1, duration_ms=None, cancel_check=lambda: len(calls) > 2 or calls.append(1))
    assert time.monotonic() - started < 2.0
