"""Plex's loudnorm command, its report, and the ln:* fields Plex stores (loudness.analyze)."""

from __future__ import annotations

import shutil
import subprocess

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
        "ffmpeg", "-hide_banner", "-nostats", "-i", "/m/a.mkv", "-map", "0:3",
        "-af", "loudnorm=I=-16:TP=-1:LRA=9:print_format=json", "-f", "null", "-",
    ]  # fmt: skip


@pytest.mark.parametrize(("codec", "no_drc"), [("eac3", True), ("ac3", False), ("aac", False)])
def test_eac3_is_decoded_without_drc_as_plexs_dolby_decoder_does(codec, no_drc):
    cmd = analyze.command("ffmpeg", "/m/a.mkv", 1, codec)
    assert (cmd[3:5] == ["-drc_scale", "0"]) is no_drc
    assert cmd[-9:] == ["-i", "/m/a.mkv", "-map", "0:1", "-af", analyze.LOUDNORM_FILTER, "-f", "null", "-"]


def test_report_maps_to_plexs_fields_verbatim():
    assert analyze.ln_fields(analyze.parse(REPORT)) == PLEX_FIELDS


def test_no_report_is_an_error():
    with pytest.raises(analyze.LoudnessError, match="no loudnorm report"):
        analyze.parse("Error opening input file")


def test_silent_stream_is_refused_not_written():
    report = analyze.parse(REPORT.replace('"-23.23"', '"-inf"'))
    with pytest.raises(analyze.LoudnessError, match="silent"):
        analyze.ln_fields(report)


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
