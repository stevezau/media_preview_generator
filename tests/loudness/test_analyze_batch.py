"""Multi-stream loudnorm commands and named report validation."""

from __future__ import annotations

import json

import pytest

from media_preview_generator.loudness import analyze

_REPORT = {
    "input_i": "-23.23",
    "input_tp": "-8.57",
    "input_lra": "7.30",
    "input_thresh": "-33.93",
    "output_i": "-16.21",
    "output_tp": "-1.00",
    "output_lra": "6.30",
    "output_thresh": "-26.83",
    "normalization_type": "dynamic",
    "target_offset": "0.21",
}


def _named(index: int, report: dict[str, str] = _REPORT) -> str:
    return f"[loudnorm@track{index} @ 0x1234] {json.dumps(report)}"


def test_command_many_uses_absolute_indices_and_only_disables_eac3_drc():
    command = analyze.command_many(
        "ffmpeg",
        "/m/movie.mkv",
        [(1, "aac", 120_000), (3, "eac3", 120_000), (8, "ac3", 120_000)],
    )

    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-filter_threads",
        "1",
        "-drc_scale:3",
        "0",
        "-i",
        "/m/movie.mkv",
        "-map",
        "0:1",
        "-af",
        "loudnorm@track1=I=-16:TP=-1:LRA=9:print_format=json",
        "-f",
        "null",
        "-",
        "-map",
        "0:3",
        "-af",
        "loudnorm@track3=I=-16:TP=-1:LRA=9:print_format=json",
        "-f",
        "null",
        "-",
        "-map",
        "0:8",
        "-af",
        "loudnorm@track8=I=-16:TP=-1:LRA=9:print_format=json",
        "-f",
        "null",
        "-",
    ]


@pytest.mark.parametrize(
    "tracks",
    [
        [],
        [(1, "aac", 1000)],
        [(1, "aac", 1000), (2, "aac", 1000), (3, "aac", 1000), (4, "aac", 1000)],
        [(-1, "aac", 1000), (2, "aac", 1000)],
        [(True, "aac", 1000), (2, "aac", 1000)],
        [(1, "aac", 1000), (1, "eac3", 1000)],
    ],
)
def test_command_many_rejects_invalid_batch_indices_or_size(tracks):
    with pytest.raises(analyze.LoudnessError):
        analyze.command_many("ffmpeg", "/m/movie.mkv", tracks)


def test_parse_many_associates_reports_by_name_when_ffmpeg_finishes_out_of_order():
    expected = {
        1: {**_REPORT, "input_i": "-18.00", "target_offset": "0.00"},
        3: {**_REPORT, "input_i": "-23.00", "target_offset": "0.25"},
        8: {**_REPORT, "input_i": "-30.00", "target_offset": "0.50"},
    }
    reports = analyze.parse_many(
        f"{_named(8, expected[8])}\n{_named(1, expected[1])}\n{_named(3, expected[3])}", [1, 3, 8]
    )

    assert reports == expected


@pytest.mark.parametrize(
    ("stderr", "indices", "message"),
    [
        (f"{_named(1)}\n{_named(3)}", [1, 3, 8], "omitted"),
        (f"{_named(1)}\n{_named(3)}\n{_named(3)}\n{_named(8)}", [1, 3, 8], "duplicate"),
        (f"{_named(1)}\n{_named(3)}\n{_named(8)}\n{_named(9)}", [1, 3, 8], "unexpected"),
        ("[loudnorm@track1 @ 0x1234] {not json}", [1], "unreadable"),
    ],
)
def test_parse_many_rejects_missing_duplicate_unknown_or_malformed_reports(stderr, indices, message):
    with pytest.raises(analyze.LoudnessError, match=message):
        analyze.parse_many(stderr, indices)


def test_parse_many_accepts_verified_silence_and_rejects_other_nonfinite_values():
    silent = {**_REPORT, "input_i": "-inf", "input_tp": "-inf", "target_offset": "inf"}
    assert analyze.ln_fields(analyze.parse_many(_named(4, silent), [4])[4])["ln:loudness"] == "-inf"

    invalid = {**_REPORT, "input_tp": "inf"}
    with pytest.raises(analyze.LoudnessError, match="unsupported loudness"):
        analyze.ln_fields(analyze.parse_many(_named(4, invalid), [4])[4])


def test_parse_many_rejects_an_unreadable_named_report():
    stderr = '[loudnorm@track4 @ 0x1234] {"input_i": "-23.0", bad json}'
    with pytest.raises(analyze.LoudnessError, match="unreadable"):
        analyze.parse_many(stderr, [4])


def test_run_many_discards_reports_when_ffmpeg_exits_nonzero(tmp_path):
    script = tmp_path / "ffmpeg"
    script.write_text(f"#!/bin/sh\ncat >&2 <<'REPORTS'\n{_named(1)}\n{_named(3)}\nREPORTS\nexit 1\n")
    script.chmod(0o755)

    with pytest.raises(analyze.LoudnessError, match="exited 1"):
        analyze.run_many(str(script), "/m/movie.mkv", [(1, "aac", 1000), (3, "eac3", 1000)])
