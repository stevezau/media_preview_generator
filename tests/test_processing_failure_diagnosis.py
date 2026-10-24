"""What a failed FFmpeg run is called, where it goes next, and what its failure log keeps.

* Exit 251 is an I/O error only when the stderr shows no GPU error; with one it is the GPU's, and saying "io_error"
  sends people to check their disk.
* A video track FFmpeg has no decoder for (an unknown or protected codec, e.g. an encrypted ``encv`` track) can't be
  decoded by any device: it fails once, as a normal per-file failure, and is not rerun on the CPU.
* The "last stderr lines" excerpt skips the container's metadata tags so the cause shows.
* A failure log keeps the first and last 200 lines: one AV1 file wrote 280,594 lines (21 MB).

Only the FFmpeg process is mocked; the runner and the retry cascade run for real.
"""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner
from media_preview_generator.processing.generator import (
    NO_DECODER_SUMMARY,
    _diagnose_ffmpeg_exit_code,
    _extract_ffmpeg_error_summary,
    _gpu_hand_off,
    _save_ffmpeg_failure_log,
    _without_metadata_tags,
    video_has_no_decoder,
    video_input_unreadable,
)
from tests.test_processing_partial_output import (
    HWACCEL_LINE,
    IO_ERROR,
    RAW_SIGSEGV,
    Outcome,
    _write_frames,
    assert_handed_to_cpu,
    assert_published,
    run,  # noqa: F401  (fixture)
)

# Production stderr of an encrypted video track (exit 234 in ~20 ms), jellyfin-ffmpeg 8.1.2.
NO_DECODER_STDERR = [
    "[in#0 @ 0x64271d06af00] mov FourCC not found encv.",
    "[in#0 @ 0x64271d06af00] Unknown/unsupported AVCodecID V_QUICKTIME.",
    "[in#0/matroska,webm @ 0x64271d06af00] Could not find codec parameters for stream 0 "
    "(Video: none (encv / 0x76636E65), none, 1920x1080): unknown codec",
    "Input #0, matroska,webm, from 'movie.mkv':",
    "Metadata:",
    "encoder         : libebml v1.4.5 + libmatroska v1.7.1",
    "creation_time   : 2026-06-15T09:37:29.000000Z",
    "Duration: 00:42:03.18, start: 0.000000, bitrate: 10306 kb/s",
    "Stream #0:0: Video: none (encv / 0x76636E65), none, 1920x1080, SAR 1:1 DAR 16:9, 23.98 tbr, 1k tbn (default)",
    "Metadata:",
    "BPS             : 10242270",
    "DURATION        : 00:42:03.063000000",
    "NUMBER_OF_FRAMES: 60493",
    "NUMBER_OF_BYTES : 3230236810",
    "_STATISTICS_WRITING_APP: mkvmerge v97.0 ('You Don't Have A Clue') 64-bit",
    "_STATISTICS_WRITING_DATE_UTC: 2026-06-15 09:37:29",
    "_STATISTICS_TAGS: BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES",
    "[vist#0:0/none @ 0x64271d086700] Decoding requested, but no decoder found for: none",
    "Error opening output file /tmp/frame_cache/frames-09c404b1d87b5255/img-%06d.jpg.",
    "Error opening output files: Invalid argument",
]
# The same file on a keyframe-only run (production, job f9ced906): with no decoder for the video nothing takes
# ``-skip_frame:v``, and FFmpeg stops on the leftover option at input open, before it says "no decoder found".
SKIP_OPTION_UNUSED = (
    "[in#0/matroska,webm @ 0x5caa7bd7ac40] Codec AVOption skip_frame:v (Rate Control Based Frame Skip) is not a "
    "decoding option."
)
BARE_SKIP_OPTION_UNUSED = (
    "[in#0/matroska,webm @ 0x5a4da9e2ad40] Codec AVOption skip_frame (Allow frame skipping) is not a decoding option."
)
NO_DECODER_KEYFRAME_STDERR = NO_DECODER_STDERR[:-3] + [
    "Stream #0:1(eng): Audio: aac (HE-AAC), 48000 Hz, stereo, fltp (default)",
    "Stream #0:2(eng): Subtitle: subrip (srt), start 4.796000",
    "[in#0/matroska,webm @ 0x5caa7bd7ac40] Codec AVOption threads:v (set the number of threads) has not been used "
    "for any stream. The most likely reason is either wrong type (e.g. a video option with no video streams) or "
    "that it is a private option of some decoder which was not actually used for any stream.",
    SKIP_OPTION_UNUSED,
    "Error opening input file movie.mkv.",
    "Error opening input files: Invalid argument",
]
VIDEO_STREAM_LINES = [NO_DECODER_STDERR[2], NO_DECODER_STDERR[8]]
# A file whose video decodes but whose *audio* codec FFmpeg doesn't know (production: av3a next to HEVC). "unknown
# codec" alone says nothing about the video, so it must not stop the CPU rerun.
UNKNOWN_AUDIO_CODEC = (
    "[in#0/mov,mp4,m4a,3gp,3g2,mj2 @ 0x5cd5d4e9ef40] Could not find codec parameters for stream 1 "
    "(Audio: none (av3a / 0x61337661), 48000 Hz, 6 channels, 384 kb/s): unknown codec"
)
PLAIN_IO_STDERR = "[in#0/matroska @ 0x5617a0] Error during demuxing: Input/output error"
NO_DECODER_EXIT = 234


def _run_real(
    generate,
    tmp_path,
    rc: int,
    stderr: list[str],
    gpu: str | None,
    keyframe_gap: float | None = None,
    *,
    keyframe_stderr: list[str] | None = None,
    frames: int = 0,
    dv5: bool = False,
) -> tuple[Outcome, list[MagicMock]]:
    """Run the real runner and cascade over a fake FFmpeg process.

    ``keyframe_stderr`` is what a keyframe-only (``-skip_frame:v``) run prints when that differs from a full-decode
    run's ``stderr``; ``frames`` is how many thumbnails each run writes.
    """
    procs: list[MagicMock] = []

    def popen(args, **kwargs):
        keyframe_only = "-skip_frame:v" in args
        lines = keyframe_stderr if keyframe_only and keyframe_stderr is not None else stderr
        if lines:
            kwargs["stderr"].write("\n".join(lines) + "\n")
            kwargs["stderr"].flush()
        _write_frames(str(tmp_path / "frames"), frames)
        proc = MagicMock(pid=4242, returncode=rc, keyframe_only=keyframe_only)
        proc.poll.return_value = rc
        procs.append(proc)
        return proc

    with patch("media_preview_generator.processing.ffmpeg_runner.subprocess.Popen", side_effect=popen):
        return generate([], gpu=gpu, keyframe_gap=keyframe_gap, dv5=dv5, runner_factory=create_ffmpeg_runner), procs


def _error_header(outcome: Outcome) -> str:
    headers = [line for line in outcome.job_log if "FFmpeg failed while extracting frames" in line]
    assert headers, outcome.job_log
    return headers[-1]


def _failure_log(tmp_path, monkeypatch, rc: int, stderr: list[str]) -> str:
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    _save_ffmpeg_failure_log("/media/movie.mkv", rc, stderr)
    (log,) = (tmp_path / "logs" / "ffmpeg_failures").glob("*.log")
    return log.read_text(encoding="utf-8")


class TestExit251Tag:
    @pytest.mark.parametrize(
        ("stderr", "tag"),
        [
            ([HWACCEL_LINE], "gpu_error"),
            (["[AVHWFramesContext @ 0x7e29f0001700] Failed to sync surface 0x4: 34 (HW busy now)."], "gpu_error"),
            ([PLAIN_IO_STDERR], "io_error"),
            ([], "io_error"),
        ],
        ids=["hwaccel-transfer", "hw-busy", "plain-io", "no-stderr"],
    )
    def test_tag_follows_the_stderr(self, stderr, tag):
        assert _diagnose_ffmpeg_exit_code(IO_ERROR, stderr) == tag

    def test_tag_without_stderr_is_unchanged(self):
        assert _diagnose_ffmpeg_exit_code(IO_ERROR) == "io_error"

    @pytest.mark.parametrize(
        ("stderr", "tag"),
        [([HWACCEL_LINE], "gpu_error"), ([PLAIN_IO_STDERR], "io_error")],
        ids=["hwaccel", "plain-io"],
    )
    def test_error_header_and_failure_log_carry_the_tag(self, run, tmp_path, monkeypatch, stderr, tag):  # noqa: F811
        outcome, _procs = _run_real(run, tmp_path, IO_ERROR, stderr, gpu="NVIDIA")

        assert f"(exit code {IO_ERROR}: {tag})" in _error_header(outcome)
        assert f"exit_diagnosis: {tag}\n" in _failure_log(tmp_path, monkeypatch, IO_ERROR, stderr)


# Production stderr of a Matroska file whose first bytes are zeroes (exit 183), and of one that is gone (exit 254).
EBML_STDERR = [
    "[matroska,webm @ 0x55d0c8a4e8c0] 0x00 at pos 0 (0x0) invalid as first byte of an EBML number",
    "[in#0 @ 0x55d0c8a4e780] Error opening input: Invalid data found when processing input",
    "Error opening input file /media/movie.mkv.",
    "Error opening input files: Invalid data found when processing input",
]
MID_FILE_EBML_STDERR = [
    "[matroska,webm @ 0x55d0c8a4e8c0] Length 9 indicated by an EBML number's first byte 0x01 at pos 4096 (0x1000) "
    "exceeds max length 8.",
    "[in#0/matroska,webm @ 0x55d0c8a4e8c0] Error during demuxing: Invalid data found when processing input",
]
GONE_STDERR = [
    "[in#0 @ 0x5581d3c0b780] Error opening input: No such file or directory",
    "Error opening input file /media/movie.mkv.",
    "Error opening input files: No such file or directory",
]
INVALID_DATA_EXIT = 183
NO_SUCH_FILE_EXIT = 254


STATS_LINES = [
    "[out#0/image2 @ 0x5581d3c0b780] video:0KiB audio:0KiB subtitle:0KiB other streams:0KiB global headers:0KiB "
    "muxing overhead: unknown",
    "frame=    0 fps=0.0 q=0.0 Lsize=N/A time=N/A bitrate=N/A speed=N/A",
    "Conversion failed!",
]


class TestExtractFfmpegErrorSummary:
    """The reason shown for a failed run is FFmpeg's error line, never the stats it prints on the way out."""

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            ([*EBML_STDERR, *STATS_LINES], "Error opening input files: Invalid data found when processing input"),
            (
                ["Error while decoding stream #0:0: Invalid data found", *STATS_LINES],
                "Error while decoding stream #0:0: Invalid data found",
            ),
            (["[hevc @ 0x1] Cannot allocate memory", *STATS_LINES], "Cannot allocate memory"),
            (STATS_LINES, ""),
            (["Stream mapping:", "Press [q] to stop"], ""),
            ([], ""),
        ],
        ids=["error-before-stats", "decode-error-before-stats", "prefix-stripped", "stats-only", "no-error", "empty"],
    )
    def test_skips_the_stats_lines(self, stderr, expected):
        assert _extract_ffmpeg_error_summary(stderr) == expected

    @pytest.mark.parametrize("line", STATS_LINES)
    def test_a_stats_line_is_never_the_reason_even_with_an_error_keyword_in_it(self, line):
        assert _extract_ffmpeg_error_summary([line]) == ""


class TestBadInputIsNotTheGpusFault:
    """A file FFmpeg can't open fails the same way on every device: one attempt, no hand-off, no GPU blame. A damaged
    file that opens (FFmpeg resyncs past its container errors) is not that: its GPU failures are handled as before."""

    @pytest.mark.parametrize(
        ("rc", "stderr", "frames_written", "expected"),
        [
            (INVALID_DATA_EXIT, EBML_STDERR, False, True),
            (NO_SUCH_FILE_EXIT, GONE_STDERR, False, True),
            (1, ["[in#0 @ 0x1] Error opening input: Permission denied"], False, True),
            (INVALID_DATA_EXIT, EBML_STDERR, True, False),
            (0, EBML_STDERR, False, False),
            # Container errors alone, without an open failure, are a file FFmpeg read on.
            (INVALID_DATA_EXIT, MID_FILE_EBML_STDERR, False, False),
            (1, [EBML_STDERR[0]], False, False),
            # An open failure that names no file error can be a device problem (an unknown -hwaccel).
            (1, ["Error opening input files: Invalid argument"], False, False),
            (1, ["[in#0 @ 0x1] Error opening input: Operation not permitted"], False, False),
            # Not input errors: a GPU error, a disk's I/O error, a plain failure and a decoder-side invalid frame.
            (IO_ERROR, [HWACCEL_LINE], False, False),
            (IO_ERROR, [PLAIN_IO_STDERR], False, False),
            (1, ["Some error"], False, False),
            (
                1,
                ["[hevc @ 0x1] Error while decoding stream #0:0: Invalid data found when processing input"],
                False,
                False,
            ),
            (1, [], False, False),
        ],
        ids=[
            "ebml-at-open",
            "gone",
            "permission-denied",
            "frames-written",
            "exit-0",
            "ebml-mid-file",
            "ebml-line-alone",
            "open-failure-invalid-argument",
            "open-failure-not-permitted",
            "gpu-error",
            "io-error",
            "plain",
            "decoder",
            "empty",
        ],
    )
    def test_verdict_follows_the_stderr(self, rc, stderr, frames_written, expected):
        assert video_input_unreadable(rc, stderr, frames_written=frames_written) is expected

    @pytest.mark.parametrize(
        ("rc", "stderr"),
        [(INVALID_DATA_EXIT, EBML_STDERR), (NO_SUCH_FILE_EXIT, GONE_STDERR)],
        ids=["ebml", "gone"],
    )
    def test_a_file_that_wont_open_is_not_handed_to_another_device(self, rc, stderr):
        assert _gpu_hand_off(rc, stderr, stderr, stopped_part_way=False, hwaccel="cuda") is None
        assert _diagnose_ffmpeg_exit_code(rc, stderr) == "bad_input"

    @pytest.mark.parametrize(
        ("rc", "stderr", "part_way"),
        [
            # A damaged-but-playable file whose GPU run really failed: another device gets it, as before.
            (IO_ERROR, [EBML_STDERR[0], HWACCEL_LINE], True),
            (IO_ERROR, [*MID_FILE_EBML_STDERR, HWACCEL_LINE], False),
            (RAW_SIGSEGV, [EBML_STDERR[0]], True),
            (RAW_SIGSEGV, MID_FILE_EBML_STDERR, False),
            (IO_ERROR, [EBML_STDERR[0], PLAIN_IO_STDERR], True),
            (INVALID_DATA_EXIT, MID_FILE_EBML_STDERR, True),
            # Frames were written, so the file opened: even an open failure line is the GPU's to answer for.
            (INVALID_DATA_EXIT, EBML_STDERR, True),
        ],
        ids=[
            "hwaccel-error",
            "hwaccel-error-no-frames",
            "sigsegv",
            "sigsegv-no-frames",
            "io-error",
            "stopped",
            "open-line",
        ],
    )
    def test_a_file_that_opened_still_goes_to_another_device(self, rc, stderr, part_way):
        assert _gpu_hand_off(rc, stderr, stderr, stopped_part_way=part_way, hwaccel="cuda") is not None

    @pytest.mark.parametrize("keyframe_gap", [1.0, None], ids=["keyframe", "full-decode"])
    @pytest.mark.parametrize(
        ("rc", "stderr"),
        [(INVALID_DATA_EXIT, EBML_STDERR), (NO_SUCH_FILE_EXIT, GONE_STDERR)],
        ids=["ebml", "gone"],
    )
    def test_a_gpu_run_on_a_file_that_wont_open_ends_in_one_attempt(self, run, tmp_path, rc, stderr, keyframe_gap):  # noqa: F811
        (tmp_path / "movie.mkv").write_bytes(b"\0" * 16)
        outcome, procs = _run_real(run, tmp_path, rc, stderr, gpu="NVIDIA", keyframe_gap=keyframe_gap)

        assert not isinstance(outcome.result, Exception), "a CodecNotSupportedError would send it to another device"
        assert [proc.keyframe_only for proc in procs] == [keyframe_gap is not None], "one FFmpeg run, no rerun"
        assert outcome.result[:2] == (False, 0)
        assert [f["exit_code"] for f in outcome.failures] == [rc]
        assert "(bad_input)" in outcome.failures[0]["reason"]
        # The app log has the file's one ERROR with the reason; the job log goes on to show the run's own line.
        app_errors = [line for line in outcome.app_log if line.startswith(("ERROR|", "WARNING|"))]
        assert len(app_errors) == 1 and app_errors[0].startswith("ERROR|"), outcome.app_log
        assert "(bad_input)" in app_errors[0] and _extract_ffmpeg_error_summary(stderr) in app_errors[0]
        assert any(f"exit code {rc}: bad_input" in line for line in outcome.job_log), outcome.job_log
        assert not any(f"exit code {rc}: bad_input" in line for line in outcome.app_log), outcome.app_log

    def test_a_file_that_is_gone_is_neither_logged_as_an_error_nor_recorded_as_failed(self, run, tmp_path):  # noqa: F811
        outcome, procs = _run_real(run, tmp_path, NO_SUCH_FILE_EXIT, GONE_STDERR, gpu="NVIDIA")

        assert len(procs) == 1
        assert outcome.result[:2] == (False, 0)
        assert outcome.failures == []
        assert not any(line.startswith(("ERROR|", "WARNING|")) for line in outcome.app_log), outcome.app_log

    @pytest.mark.parametrize("keyframe_gap", [1.0, None], ids=["keyframe", "full-decode"])
    @pytest.mark.parametrize(
        ("rc", "stderr"),
        [
            (INVALID_DATA_EXIT, [EBML_STDERR[0], *MID_FILE_EBML_STDERR]),
            (RAW_SIGSEGV, [EBML_STDERR[0]]),
            (IO_ERROR, [EBML_STDERR[0], HWACCEL_LINE]),
        ],
        ids=["damaged-file-fails", "damaged-file-crashes", "damaged-file-gpu-error"],
    )
    def test_a_damaged_file_whose_gpu_run_failed_with_some_frames_goes_to_the_cpu(
        self,
        run,  # noqa: F811
        tmp_path,
        rc,
        stderr,
        keyframe_gap,
    ):
        outcome, _procs = _run_real(run, tmp_path, rc, stderr, gpu="NVIDIA", keyframe_gap=keyframe_gap, frames=7)

        assert_handed_to_cpu(outcome)


class TestVideoNoDeviceCanDecode:
    def test_tagged_no_decoder(self):
        assert _diagnose_ffmpeg_exit_code(NO_DECODER_EXIT, NO_DECODER_STDERR) == "no_decoder"
        assert _diagnose_ffmpeg_exit_code(NO_DECODER_EXIT, NO_DECODER_KEYFRAME_STDERR) == "no_decoder"
        assert _diagnose_ffmpeg_exit_code(NO_DECODER_EXIT, ["Some error"]) == "high_exit_non_signal"
        assert _diagnose_ffmpeg_exit_code(NO_DECODER_EXIT) == "high_exit_non_signal"

    @pytest.mark.parametrize(
        ("stderr", "expected"),
        [
            (NO_DECODER_STDERR, True),
            (NO_DECODER_KEYFRAME_STDERR, True),
            # An audio-only file: nothing takes the video option either, but there is no video to call undecodable.
            ([line for line in NO_DECODER_KEYFRAME_STDERR if "Video:" not in line], False),
            ([UNKNOWN_AUDIO_CODEC, "Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv), 3840x2160"], False),
            (NO_DECODER_KEYFRAME_STDERR[:3], False),  # "unknown codec" for the video, and FFmpeg hasn't given up
            # Intro & Credits' keyframe pass: a bare ``-skip_frame``, and lines as FFmpeg indents them (ffmpeg 8.0.1).
            (
                [
                    "  Stream #0:0: Video: none (encv / 0x76636E65), none, 1920x1080, SAR 1:1 DAR 16:9, 23.98 tbr",
                    "  Stream #0:1(eng): Audio: aac (HE-AAC), 48000 Hz, stereo, fltp (default)",
                    BARE_SKIP_OPTION_UNUSED,
                ],
                True,
            ),
            (["  Stream #0:0: Audio: aac (LC), 44100 Hz, mono, fltp", BARE_SKIP_OPTION_UNUSED], False),
            (["Some error"], False),
            ([], False),
        ],
        ids=[
            "full-decode",
            "keyframe-only",
            "no-video-stream",
            "unknown-audio-codec",
            "unknown-codec-only",
            "bare-option-indented",
            "bare-option-no-video-stream",
            "x",
            "-",
        ],
    )
    def test_verdict_follows_the_stderr(self, stderr, expected):
        assert video_has_no_decoder(stderr) is expected

    @pytest.mark.parametrize("stderr", [NO_DECODER_STDERR, NO_DECODER_KEYFRAME_STDERR], ids=["full", "keyframe"])
    def test_is_not_handed_to_the_cpu(self, stderr):
        assert _gpu_hand_off(NO_DECODER_EXIT, stderr, stderr, stopped_part_way=False, hwaccel="cuda") is None

    @pytest.mark.parametrize(
        ("rc", "stderr"),
        [(NO_DECODER_EXIT, ["Some error"]), (69, ["Some error"]), (NO_DECODER_EXIT, [UNKNOWN_AUDIO_CODEC])],
        ids=["plain-234", "69", "unknown-audio-codec"],
    )
    def test_other_codec_failures_still_go_to_the_cpu(self, rc, stderr):
        assert _gpu_hand_off(rc, stderr, stderr, stopped_part_way=False, hwaccel="cuda") == ("codec", "codec error")

    @pytest.mark.parametrize("keyframe_gap", [1.0, None], ids=["keyframe", "full-decode"])
    @pytest.mark.parametrize("gpu", ["NVIDIA", "INTEL", "AMD", None], ids=["nvidia", "intel", "amd", "cpu"])
    def test_fails_once_and_says_so_plainly(self, run, tmp_path, gpu, keyframe_gap):  # noqa: F811
        outcome, procs = _run_real(
            run,
            tmp_path,
            NO_DECODER_EXIT,
            NO_DECODER_STDERR,
            gpu=gpu,
            keyframe_gap=keyframe_gap,
            keyframe_stderr=NO_DECODER_KEYFRAME_STDERR,
        )

        assert [proc.keyframe_only for proc in procs] == [keyframe_gap is not None], "one FFmpeg run, no rerun"
        assert not isinstance(outcome.result, Exception), "a CPU rerun would fail the same way"
        success, image_count, _hw, _seconds, _speed, summary = outcome.result
        assert (success, image_count) == (False, 0)
        # Exactly this: ``process_canonical_path`` ends the file on the summary being this constant.
        assert summary == NO_DECODER_SUMMARY
        assert [(f["exit_code"], f["worker_type"]) for f in outcome.failures] == [
            (NO_DECODER_EXIT, "GPU" if gpu else "CPU")
        ]
        assert outcome.failures[0]["reason"] == f"FFmpeg exit {NO_DECODER_EXIT} (no_decoder) — {summary}"
        # One failure line in the app log; the job log goes on with only FFmpeg's own lines about the video.
        errors = [line for line in outcome.app_log if line.startswith(("ERROR|", "WARNING|"))]
        assert len(errors) == 1 and errors[0].startswith("ERROR|"), outcome.app_log
        assert not any("stderr lines about the video" in line for line in outcome.app_log), outcome.app_log
        assert "can't be decoded by any device" in errors[0] and "unknown or protected codec" in errors[0]
        assert str(tmp_path / "movie.mkv") in errors[0]
        cause = VIDEO_STREAM_LINES + ([] if keyframe_gap else [NO_DECODER_STDERR[-3]])
        job_lines = [line.split("|", 1)[1].rstrip("\n") for line in outcome.job_log]
        header = f"FFmpeg's last {len(cause)} stderr lines about the video in {tmp_path / 'movie.mkv'}:"
        start = job_lines.index(header)
        assert job_lines[start:] == [header, *(f"  {line}" for line in cause)]

    @pytest.mark.parametrize("gpu", ["INTEL", "AMD", "NVIDIA", None], ids=["intel", "amd", "nvidia", "cpu"])
    def test_a_dolby_vision_file_is_not_rerun_on_the_simpler_chains(self, run, tmp_path, gpu):  # noqa: F811
        outcome, procs = _run_real(run, tmp_path, NO_DECODER_EXIT, NO_DECODER_STDERR, gpu=gpu, dv5=True)

        assert len(procs) == 1, "no filter chain decodes a video FFmpeg has no decoder for"
        assert not isinstance(outcome.result, Exception)
        assert outcome.result[5] == NO_DECODER_SUMMARY

    @pytest.mark.parametrize("keyframe_gap", [1.0, None], ids=["keyframe", "full-decode"])
    @pytest.mark.parametrize("gpu", ["NVIDIA", None], ids=["gpu", "cpu"])
    def test_an_unknown_audio_codec_does_not_stop_the_video_decoding(self, run, tmp_path, gpu, keyframe_gap):  # noqa: F811
        stderr = [UNKNOWN_AUDIO_CODEC, "Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv), 3840x2160"]
        outcome, procs = _run_real(run, tmp_path, 0, stderr, gpu=gpu, keyframe_gap=keyframe_gap, frames=100)

        assert_published(outcome, 100)
        assert len(procs) == 1
        assert outcome.warnings == []

    @pytest.mark.parametrize(
        ("rc", "stderr"),
        [(NO_DECODER_EXIT, ["Some error"]), (69, ["Some error"]), (NO_DECODER_EXIT, [UNKNOWN_AUDIO_CODEC])],
        ids=["plain-234", "69", "unknown-audio-codec"],
    )
    @pytest.mark.parametrize("keyframe_gap", [1.0, None], ids=["keyframe", "full-decode"])
    def test_gpu_run_with_another_codec_failure_goes_to_the_cpu(self, run, tmp_path, keyframe_gap, rc, stderr):  # noqa: F811
        outcome, procs = _run_real(run, tmp_path, rc, stderr, gpu="NVIDIA", keyframe_gap=keyframe_gap)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        # A failed keyframe-only pass is still repeated with full decode before the hand-off.
        assert [proc.keyframe_only for proc in procs] == ([True, False] if keyframe_gap else [False])

    @pytest.mark.parametrize(
        ("rc", "stderr"),
        [(NO_DECODER_EXIT, ["Some error"]), (69, ["Some error"]), (NO_DECODER_EXIT, [UNKNOWN_AUDIO_CODEC])],
        ids=["plain-234", "69", "unknown-audio-codec"],
    )
    @pytest.mark.parametrize("keyframe_gap", [1.0, None], ids=["keyframe", "full-decode"])
    def test_cpu_run_with_another_codec_failure_fails(self, run, tmp_path, keyframe_gap, rc, stderr):  # noqa: F811
        outcome, procs = _run_real(run, tmp_path, rc, stderr, gpu=None, keyframe_gap=keyframe_gap)

        assert not isinstance(outcome.result, Exception)
        assert [(f["exit_code"], f["worker_type"]) for f in outcome.failures] == [(rc, "CPU")]
        assert "can't be decoded by any device" not in outcome.result[5]
        assert not any("can't be decoded by any device" in line for line in outcome.job_log), outcome.job_log
        assert "FFmpeg failed while extracting frames" in _error_header(outcome)
        assert [proc.keyframe_only for proc in procs] == ([True, False] if keyframe_gap else [False])


class TestStderrExcerpt:
    def test_metadata_tags_are_dropped(self):
        assert _without_metadata_tags(NO_DECODER_STDERR) == [
            NO_DECODER_STDERR[0],
            NO_DECODER_STDERR[1],
            NO_DECODER_STDERR[2],
            "Input #0, matroska,webm, from 'movie.mkv':",
            "Duration: 00:42:03.18, start: 0.000000, bitrate: 10306 kb/s",
            "Stream #0:0: Video: none (encv / 0x76636E65), none, 1920x1080, SAR 1:1 DAR 16:9, 23.98 tbr, 1k tbn "
            "(default)",
            "[vist#0:0/none @ 0x64271d086700] Decoding requested, but no decoder found for: none",
            "Error opening output file /tmp/frame_cache/frames-09c404b1d87b5255/img-%06d.jpg.",
            "Error opening output files: Invalid argument",
        ]

    @pytest.mark.parametrize(
        "line",
        [
            "Error opening output files: Invalid argument",
            "av_interleaved_write_frame(): Permission denied",
            "[av1 @ 0x6098cbe1abc0] Failed setup for format cuda: hwaccel initialisation returned error.",
            "src: yuv420p10le",
            "Conversion failed!",
        ],
    )
    def test_a_cause_line_is_kept_even_right_after_a_metadata_block(self, line):
        assert _without_metadata_tags(["Metadata:", "BPS             : 102", line]) == [line]

    def test_a_tag_shaped_line_outside_a_metadata_block_is_kept(self):
        lines = ["Stream mapping:", "NUMBER_OF_FRAMES: 60493", "title           : x"]
        assert _without_metadata_tags(lines) == lines

    def test_continued_tag_values_are_dropped(self):
        lines = ["Metadata:", "Description     : first line", ": second line", "Duration: 00:01:00.00"]
        assert _without_metadata_tags(lines) == ["Duration: 00:01:00.00"]

    def test_the_logged_excerpt_shows_the_cause_not_the_tags(self, run, tmp_path):  # noqa: F811
        stderr = NO_DECODER_STDERR[:-3] + ["Conversion failed!"]  # ends: tags, then the one cause line
        outcome, _procs = _run_real(run, tmp_path, NO_DECODER_EXIT, stderr, gpu=None)

        start = next(i for i, line in enumerate(outcome.job_log) if "last 5 stderr lines" in line)
        excerpt = [line.split("|", 1)[1].strip() for line in outcome.job_log[start + 1 : start + 6]]
        assert excerpt == [
            NO_DECODER_STDERR[2],
            "Input #0, matroska,webm, from 'movie.mkv':",
            "Duration: 00:42:03.18, start: 0.000000, bitrate: 10306 kb/s",
            "Stream #0:0: Video: none (encv / 0x76636E65), none, 1920x1080, SAR 1:1 DAR 16:9, 23.98 tbr, 1k tbn "
            "(default)",
            "Conversion failed!",
        ]


class TestFailureLogIsBounded:
    def test_a_huge_log_keeps_its_head_and_tail_with_one_marker(self, tmp_path, monkeypatch):
        stderr = [f"line {i}" for i in range(280_594)]

        content = _failure_log(tmp_path, monkeypatch, 69, stderr)

        header, body = content.split("-" * 72 + "\n")
        assert "lines: 280594\n" in header
        assert body.splitlines() == stderr[:200] + ["… 280194 lines omitted …"] + stderr[-200:]

    @pytest.mark.parametrize("count", [0, 1, 400], ids=["empty", "one-line", "exactly-head-plus-tail"])
    def test_a_small_log_is_written_whole(self, tmp_path, monkeypatch, count):
        stderr = [f"line {i}" for i in range(count)]

        content = _failure_log(tmp_path, monkeypatch, 187, stderr)

        header, body = content.split("-" * 72 + "\n")
        assert f"lines: {count}\n" in header
        assert body.splitlines() == stderr

    def test_one_line_over_is_trimmed(self, tmp_path, monkeypatch):
        stderr = [f"line {i}" for i in range(401)]

        body = _failure_log(tmp_path, monkeypatch, 187, stderr).split("-" * 72 + "\n")[1]

        assert body.splitlines() == stderr[:200] + ["… 1 lines omitted …"] + stderr[-200:]
