"""A GPU run on a codec the GPU can't decode is stopped at the decoder's own verdict.

FFmpeg's AV1 decoder for GPUs has no software path. On a GPU without AV1 decode it says so on the first packet, then
again for every packet to the end of the file, and only then exits 69 having written nothing: 46,745 times over 8
minutes for one 44-minute episode, with the GPU worker held the whole time. The stall watchdog never fires because
every new line counts as progress.

The trigger is that decoder's line, seen while decode runs on the GPU and before FFmpeg has written a frame. In 137
production failure logs it appears in 68 runs, each of which wrote no frame (the fewest repeats: 623). The generic
``hwaccel initialisation returned error`` line is not the trigger: decoders with a software path print it once or
twice in runs that had already written 328-414 frames, and FFmpeg ends those runs itself.

Only the FFmpeg process is mocked; the runner's poll loop and the retry cascade run for real.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.processing import ffmpeg_runner
from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner
from media_preview_generator.processing.generator import _was_interrupted, gpu_fallback_announcement
from tests.test_processing_partial_output import (
    EXPECTED,
    FFMPEG_SIGNALLED,
    RAW_SIGTERM,
    STALL_KILLED,
    _warned,
    assert_failed,
    assert_handed_to_cpu,
    assert_published,
    run,  # noqa: F401  (fixture)
)

# What the decoder prints for one packet (production, jellyfin-ffmpeg 8.1.3 on NVIDIA; identical on FFmpeg 8.0.1).
AV1_VERDICT = "[av1 @ 0x6098cbe1abc0] Your platform doesn't support hardware accelerated AV1 decoding."


def _av1_block(hwaccel: str) -> list[str]:
    return [
        "[av1 @ 0x6098cbe1abc0] Hardware is lacking required capabilities",
        f"[av1 @ 0x6098cbe1abc0] Failed setup for format {hwaccel}: hwaccel initialisation returned error.",
        AV1_VERDICT,
        "[av1 @ 0x6098cbe1abc0] Failed to get pixel format.",
        "[av1 @ 0x6098cbe1abc0] Get current frame error",
        "[vist#0:0/av1 @ 0x6098cbe43440] [dec:av1 @ 0x6098cbe21b80] Error submitting packet to decoder: "
        "Function not implemented",
    ]


# A decoder with a software path prints only this (production: HEVC on VAAPI, after 328 good frames).
HEVC_HWACCEL_INIT_ONLY = [
    "[hevc @ 0x5cd5d4f49c80] Failed to create decode context: 2 (resource allocation failed).",
    "[hevc @ 0x5cd5d4f49c80] Failed setup for format vaapi: hwaccel initialisation returned error.",
]
PROGRESS = "frame=  328 fps=4.4 q=4.0 size=N/A time=00:32:47.99 bitrate=N/A dup=230 drop=0 speed=26.6x elapsed=0:01:14"
INPUT_DUMP = [
    "Input #0, matroska,webm, from 'movie.mkv':",
    "Duration: 00:44:15.68, start: 0.000000, bitrate: 3398 kb/s",
    "Stream #0:0(eng): Video: av1 (libdav1d) (Main), yuv420p10le(tv), 1920x1080, 23.98 fps",
    "Stream mapping:",
    "Stream #0:0 -> #0:0 (av1 (native) -> mjpeg (native))",
]
LEFT_ALONE_EXIT = 69  # what FFmpeg ends with once it has failed every packet of the file

# (worker GPU, device, the hwaccel FFmpeg names in its lines)
GPU_DECODE = [
    pytest.param("NVIDIA", "cuda:0", "cuda", id="NVIDIA"),
    pytest.param("INTEL", "/dev/dri/renderD128", "vaapi", id="INTEL"),
    pytest.param("AMD", "/dev/dri/renderD128", "vaapi", id="AMD"),
]
# The runner's trigger depends only on whether decode runs on the GPU, so it covers every vendor that does.
RUNNER_GPU_DECODE = [
    *GPU_DECODE,
    pytest.param("WINDOWS_GPU", None, "d3d11va", id="WINDOWS_GPU"),
    pytest.param("APPLE", None, "videotoolbox", id="APPLE"),
]


def _runner(tmp_path, mock_config, gpu, device):
    return create_ffmpeg_runner(
        video_file=str(tmp_path / "movie.mkv"),
        output_folder=str(tmp_path / "frames"),
        gpu=gpu,
        gpu_device_path=device,
        config=mock_config,
        progress_callback=None,
        ffmpeg_threads_override=None,
        cancel_check=None,
        path_kind="sdr",
        libplacebo_vf=None,
        use_libplacebo=False,
        dv5_software_fallback=False,
        base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
        fps_filter="fps=fps=0.1:round=up",
        hdr10_zscale_chain="",
    )


def _fake_ffmpeg(stderr_lines: list[str], procs: list):
    """A ``Popen`` whose process prints ``stderr_lines``, runs for two polls, then exits 69 unless terminated."""

    def popen(args, **kwargs):
        kwargs["stderr"].write("\n".join(stderr_lines) + "\n")
        kwargs["stderr"].flush()
        proc = MagicMock(pid=4242, returncode=None)
        polls_left = iter([None, None])

        def poll():
            if proc.returncode is None and next(polls_left, "exited") == "exited":
                proc.returncode = LEFT_ALONE_EXIT
            return proc.returncode

        def terminate():
            proc.returncode = FFMPEG_SIGNALLED  # FFmpeg's own exit code after a SIGTERM it handled

        proc.poll.side_effect = poll
        proc.terminate.side_effect = terminate
        procs.append(proc)
        return proc

    return popen


def _run_once(tmp_path, mock_config, gpu, device, stderr_lines):
    procs: list[MagicMock] = []
    runner = _runner(tmp_path, mock_config, gpu, device)
    with patch(
        "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
        side_effect=_fake_ffmpeg(stderr_lines, procs),
    ):
        rc, _seconds, _speed, lines = runner(use_skip=False)
    assert len(procs) == 1
    return procs[0], rc, lines


class TestTheRunnerStopsAtTheVerdict:
    @pytest.mark.parametrize(("gpu", "device", "hwaccel"), RUNNER_GPU_DECODE)
    def test_gpu_run_is_stopped_at_the_first_verdict_when_no_frame_was_written(
        self, tmp_path, mock_config, gpu, device, hwaccel
    ):
        proc, rc, lines = _run_once(tmp_path, mock_config, gpu, device, INPUT_DUMP + _av1_block(hwaccel))

        proc.terminate.assert_called_once_with()
        proc.kill.assert_not_called()
        assert rc == FFMPEG_SIGNALLED
        assert lines == INPUT_DUMP + _av1_block(hwaccel) + [ffmpeg_runner.GPU_CANT_DECODE_LINE]

    @pytest.mark.parametrize(("gpu", "device", "hwaccel"), RUNNER_GPU_DECODE)
    @pytest.mark.parametrize(
        "stderr",
        ["no-error", "hwaccel-init-error-of-a-decoder-with-a-software-path", "verdict-after-frames-were-written"],
    )
    def test_gpu_run_is_left_alone_without_a_verdict_before_the_first_frame(
        self, tmp_path, mock_config, gpu, device, hwaccel, stderr
    ):
        printed = {
            "no-error": INPUT_DUMP + [PROGRESS],
            "hwaccel-init-error-of-a-decoder-with-a-software-path": INPUT_DUMP + HEVC_HWACCEL_INIT_ONLY * 2,
            "verdict-after-frames-were-written": INPUT_DUMP + [PROGRESS] + _av1_block(hwaccel) * 3,
        }[stderr]

        proc, rc, lines = _run_once(tmp_path, mock_config, gpu, device, printed)

        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        assert rc == LEFT_ALONE_EXIT
        assert lines == printed

    @pytest.mark.parametrize(
        ("gpu", "device"),
        [(None, None), ("INTEL", None)],
        ids=["cpu-worker", "gpu-worker-decoding-in-software"],
    )
    def test_a_run_that_decodes_in_software_is_never_stopped(self, tmp_path, mock_config, gpu, device):
        """With no GPU decode there is nothing to fall back from: the run ends on its own."""
        printed = INPUT_DUMP + _av1_block("cuda") * 3

        proc, rc, lines = _run_once(tmp_path, mock_config, gpu, device, printed)

        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()
        assert rc == LEFT_ALONE_EXIT
        assert lines == printed

    def test_ffmpeg_is_killed_when_it_ignores_the_terminate(self, tmp_path, mock_config):
        proc = MagicMock(pid=4242, returncode=-9)
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("ffmpeg", 5), None]

        def popen(args, **kwargs):
            kwargs["stderr"].write("\n".join(_av1_block("cuda")) + "\n")
            kwargs["stderr"].flush()
            return proc

        runner = _runner(tmp_path, mock_config, "NVIDIA", "cuda:0")
        with patch("media_preview_generator.processing.ffmpeg_runner.subprocess.Popen", side_effect=popen):
            rc, _seconds, _speed, lines = runner(use_skip=False)

        proc.terminate.assert_called_once_with()
        proc.kill.assert_called_once_with()
        assert rc == -9
        assert lines[-1] == ffmpeg_runner.GPU_CANT_DECODE_LINE


class TestTheHandOff:
    """A GPU run the runner stopped goes to the CPU as an unsupported codec, as the full walk's exit 69 did."""

    @staticmethod
    def _stopped(rc: int = FFMPEG_SIGNALLED, hwaccel: str = "cuda") -> tuple:
        return (rc, 0, *_av1_block(hwaccel), ffmpeg_runner.GPU_CANT_DECODE_LINE)

    @pytest.mark.parametrize(("gpu", "device", "hwaccel"), GPU_DECODE)
    @pytest.mark.parametrize("rc", [FFMPEG_SIGNALLED, RAW_SIGTERM, STALL_KILLED], ids=["255", "raw-sigterm", "killed"])
    def test_hands_off_as_an_unsupported_codec(self, run, gpu, device, hwaccel, rc):  # noqa: F811
        outcome = run([self._stopped(rc, hwaccel)], gpu=gpu)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert "GPU can't decode" in str(outcome.result)
        assert "codec" in gpu_fallback_announcement(outcome.result.kind, "movie.mkv")

    def test_the_fast_pass_is_not_repeated_with_full_decode(self, run):  # noqa: F811
        """The decoder can't decode on this GPU with or without keyframe-only decode."""
        outcome = run([self._stopped(), (0, EXPECTED)], gpu="NVIDIA", keyframe_gap=1.0)

        assert_handed_to_cpu(outcome)
        assert [c["use_skip"] for c in outcome.calls] == [True]
        assert not _warned(outcome, "retrying with full-frame"), outcome.warnings

    @pytest.mark.parametrize("rc", [FFMPEG_SIGNALLED, RAW_SIGTERM, STALL_KILLED], ids=["255", "raw-sigterm", "killed"])
    def test_a_stopped_run_ended_on_the_file_not_from_outside(self, rc):
        assert _was_interrupted(rc, [ffmpeg_runner.GPU_CANT_DECODE_LINE]) is False
        assert _was_interrupted(rc, []) is True

    @pytest.mark.parametrize("gpu", ["AMD", "INTEL"])
    def test_dolby_vision_still_gets_its_software_decode_tier(self, run, gpu):  # noqa: F811
        """As after the full walk's exit 69: the software-decode libplacebo tier runs on the same worker."""
        outcome = run([self._stopped(hwaccel="vaapi"), (0, EXPECTED)], gpu=gpu, dv5=True)

        assert_published(outcome, EXPECTED)
        assert outcome.tiers == ["primary", "sw-libplacebo"]

    def test_gpu_run_end_to_end_through_the_real_runner(self, run, tmp_path):  # noqa: F811
        procs: list[MagicMock] = []
        errors: list[str] = []
        sink = logger.add(lambda m: errors.append(str(m)), level="ERROR", format="{message}")
        try:
            with patch(
                "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
                side_effect=_fake_ffmpeg(INPUT_DUMP + _av1_block("cuda"), procs),
            ):
                outcome = run([], gpu="NVIDIA", keyframe_gap=1.0, runner_factory=create_ffmpeg_runner)
        finally:
            logger.remove(sink)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert len(procs) == 1, "one GPU run: no full-decode retry of a codec the GPU can't decode"
        procs[0].terminate.assert_called_once_with()
        # A deliberate stop is neither a failure nor something outside stopping FFmpeg.
        assert errors == []
        assert not _warned(outcome, "FFmpeg was stopped by"), outcome.warnings

    def test_cpu_run_end_to_end_fails_normally(self, run, tmp_path):  # noqa: F811
        procs: list[MagicMock] = []
        with patch(
            "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
            side_effect=_fake_ffmpeg(INPUT_DUMP + _av1_block("cuda"), procs),
        ):
            outcome = run([], gpu=None, runner_factory=create_ffmpeg_runner)

        assert_failed(outcome, LEFT_ALONE_EXIT)
        assert procs, "the CPU run happened"
        for proc in procs:
            proc.terminate.assert_not_called()
