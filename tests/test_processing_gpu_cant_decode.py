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

import os
import subprocess
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from loguru import logger
from PIL import Image

from media_preview_generator.processing import ffmpeg_runner
from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner
from media_preview_generator.processing.filter_chain import DV5_PATH_LIBPLACEBO
from media_preview_generator.processing.generator import (
    GPU_LACKS_FORMAT,
    _was_interrupted,
    gpu_fallback_announcement,
    gpu_lacks_the_format,
)
from media_preview_generator.processing.hwaccel import hwaccel_decode_args
from tests.test_processing_partial_output import (
    EXPECTED,
    FFMPEG_SIGNALLED,
    RAW_SIGSEGV,
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
        fps_filter="fps=fps=0.1:round=up:start_time=0",
        hdr10_zscale_chain="",
    )


def _fake_ffmpeg(stderr_lines: list[str], procs: list, exit_code: int = LEFT_ALONE_EXIT):
    """A ``Popen`` whose process prints ``stderr_lines``, runs for two polls, then exits ``exit_code`` unless
    terminated."""

    def popen(args, **kwargs):
        kwargs["stderr"].write("\n".join(stderr_lines) + "\n")
        kwargs["stderr"].flush()
        proc = MagicMock(pid=4242, returncode=None)
        polls_left = iter([None, None])

        def poll():
            if proc.returncode is None and next(polls_left, "exited") == "exited":
                proc.returncode = exit_code
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


# A running container that lost its GPU, as after a host daemon-reload (nvidia-smi: "Failed to initialize NVML: Unknown
# Error"): r6 image on a Quadro P5000, Tears of Steel (2012).mov, keyframe-only and full-decode runs alike.
GONE_GPU_STDERR = [
    "Stream #0:0[0x1](eng), 2, 1/24: Video: h264 (Main), 1 reference frame (avc1 / 0x31637661), "
    "yuv420p(progressive, left), 1920x800 [SAR 1:1 DAR 12:5], 0/1, 6162 kb/s, 24 fps, 24 tbr, 24 tbn (default)",
    "[CUDA @ 0x630c839ac3c0] Loaded lib: libcuda.so.1",
    "[CUDA @ 0x630c839ac3c0] cu->cuInit(0) failed -> CUDA_ERROR_NO_DEVICE: no CUDA-capable device is detected",
    "Device creation failed: -542398533.",
    "[vist#0:0/h264 @ 0x630c83638200] [dec:h264 @ 0x630c836c0c80] No device available for decoder: device type cuda "
    "needed for codec h264.",
    "[vist#0:0/h264 @ 0x630c83638200] [dec:h264 @ 0x630c836c0c80] Hardware device setup failed for decoder: Generic "
    "error in an external library",
    "Error opening output file /tmp/gone-9094581566807085525/img-%06d.jpg.",
    "Error opening output files: Generic error in an external library",
    "Exiting with exit code -542398533",
]
EXTERNAL_LIBRARY_EXIT = 187  # AVERROR_EXTERNAL, as the process exit code


class TestAGpuThatCantStartTheDecode:
    """FFmpeg couldn't set up the GPU for the decoder and decoded nothing: the worker's CPU rerun makes the preview.

    A keyframe-only file failed outright (exit 187); the full-decode file next to it reached the CPU only because its
    probe happened to print "Could not find codec parameters", which reads as a codec error.
    """

    @pytest.mark.parametrize("keyframe_gap", [2.0, None], ids=["keyframe-only", "full-decode"])
    def test_hands_off_to_the_cpu_as_a_gpu_failure(self, run, keyframe_gap):  # noqa: F811
        outcome = run([(EXTERNAL_LIBRARY_EXIT, 0, *GONE_GPU_STDERR)] * 2, gpu="NVIDIA", keyframe_gap=keyframe_gap)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "hwaccel"
        assert _warned(outcome, "another GPU or the CPU takes it", "check the GPU driver"), outcome.warnings


# A real NVIDIA GPU (Quadro P5000) on an H.264 High 10 file: NVDEC has no decoder for that profile, the driver is fine.
# FFmpeg ends the run itself (exit 218); a High 4:4:4 file prints the same lines.
H264_HI10P_ON_NVDEC = [
    "Stream #0:0: Video: h264 (High 10), yuv420p10le(progressive), 1920x1080, 23.98 fps",
    "[h264 @ 0x5dbb72cb3c40] Hardware is lacking required capabilities",
    "[h264 @ 0x5dbb72cb3c40] Failed setup for format cuda: hwaccel initialisation returned error.",
    "Impossible to convert between the formats supported by the filter 'graph -1 input from stream 0:0' and the "
    "filter 'auto_scale_0'",
    "[vf#0:0 @ 0x5dbb72ce1040] Error reinitializing filters!",
    "[vost#0:0/mjpeg @ 0x5dbb72ce8880] Task finished with error code: -22 (Invalid argument)",
    "[out#0/image2 @ 0x5dbb72cb1380] Nothing was written into output file, because at least one of its streams "
    "received no packets.",
    "Conversion failed!",
]
NVDEC_PROFILE_EXIT = 218


class TestAProfileTheGpuLacks:
    """NVDEC's "Hardware is lacking required capabilities" is a codec the GPU can't decode, not a driver error.

    Read as a driver error, a node answered every such file as its GPU failing (a GPU re-check per file) and the log
    said to check the GPU driver.
    """

    def test_is_handed_to_the_cpu_as_a_codec_the_gpu_cant_decode(self, run):  # noqa: F811
        outcome = run([(NVDEC_PROFILE_EXIT, 0, *H264_HI10P_ON_NVDEC)], gpu="NVIDIA")

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert not _warned(outcome, "GPU driver"), outcome.warnings

    @pytest.mark.parametrize(
        "stderr",
        [
            pytest.param(HEVC_HWACCEL_INIT_ONLY, id="init-failed-without-the-verdict"),
            pytest.param(
                [
                    "[h264 @ 0x1] cuvidCreateDecoder(&ctx->decoder, params) failed -> CUDA_ERROR_OUT_OF_MEMORY: "
                    "out of memory",
                    "[h264 @ 0x1] Failed setup for format cuda: hwaccel initialisation returned error.",
                    "[vf#0:0 @ 0x2] Error reinitializing filters!",
                    "Conversion failed!",
                ],
                id="cuda-out-of-memory",
            ),
        ],
    )
    def test_a_driver_failure_stays_a_hardware_accelerator_error(self, run, stderr):  # noqa: F811
        outcome = run([(NVDEC_PROFILE_EXIT, 0, *stderr)], gpu="NVIDIA")

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "hwaccel"

    def test_av1_the_gpu_cant_decode_stays_a_codec(self, run):  # noqa: F811
        # The AV1 block carries the same capabilities line ahead of its own verdict; a full walk ends in exit 69.
        outcome = run([(LEFT_ALONE_EXIT, 0, *INPUT_DUMP, *_av1_block("cuda"))], gpu="NVIDIA")

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"


def _frames_refused(scaler: str) -> list[str]:
    """How the run ends when the GPU scaler (``scale_vaapi``, ``scale_cuda``) can't take the frames: software-decoded
    ones, or GPU frames in a format the filter chain doesn't take. The filter graph fails before writing a frame and
    FFmpeg exits 218."""
    return [
        "Impossible to convert between the formats supported by the filter 'Parsed_fps_0' and the filter "
        "'auto_scale_0'",
        "Link 'Parsed_fps_0.default' -> 'auto_scale_0.default':",
        f"Link 'auto_scale_0.default' -> 'Parsed_{scaler}_1.default':",
        "[vf#0:0 @ 0x6470e4eecb00] Error reinitializing filters!",
        "[vf#0:0 @ 0x6470e4eecb00] Task finished with error code: -38 (Function not implemented)",
        "[vf#0:0 @ 0x6470e4eecb00] Terminating thread with return code -38 (Function not implemented)",
        "[vost#0:0/mjpeg @ 0x6470e4ef2840] [enc:mjpeg @ 0x6470e4ec5380] Could not open encoder before EOF",
        "[vost#0:0/mjpeg @ 0x6470e4ef2840] Task finished with error code: -22 (Invalid argument)",
        "[vost#0:0/mjpeg @ 0x6470e4ef2840] Terminating thread with return code -22 (Invalid argument)",
        "[out#0/image2 @ 0x6470e4ef24c0] Nothing was written into output file, because at least one of its streams "
        "received no packets.",
        "Conversion failed!",
    ]


SOFTWARE_FRAMES_EXIT = 218


class TestACodecTheGpuHasNoDecoderFor:
    """A codec or profile the GPU has no decoder for is handed to the CPU as a codec, on Intel/AMD (VAAPI) too.

    Captured with the app's own preview command and FFmpeg 8.1.2 on an Intel UHD 770 (VAAPI, iHD 26.1.5) and a
    Quadro P5000 (NVDEC). VAAPI never prints NVDEC's "Hardware is lacking required capabilities". For a codec FFmpeg
    has no decoder for on that GPU (DivX 3, WMV, ProRes on NVDEC or VAAPI, H.264 4:2:2 or 4:4:4 on VAAPI) it says
    nothing, decodes in software and the GPU scaler refuses the frames: that run used to fail the file outright instead
    of retrying it on the CPU.
    """

    @pytest.mark.parametrize(
        ("gpu", "stderr"),
        [
            pytest.param(
                "INTEL",
                [
                    "Stream #0:0: Video: msmpeg4v3 (msmpeg4) (MP43 / 0x3334504D), yuv420p, 640x360, 369 kb/s, "
                    "SAR 1:1 DAR 16:9, 24 fps, 24 tbr, 24 tbn",
                    *_frames_refused("scale_vaapi"),
                ],
                id="divx3-on-vaapi",
            ),
            pytest.param(
                "INTEL",
                [
                    "Stream #0:0: Video: h264 (High 4:4:4 Predictive), yuv444p(tv, progressive), 640x360 "
                    "[SAR 1:1 DAR 16:9], 24 fps, 24 tbr, 1k tbn",
                    *_frames_refused("scale_vaapi"),
                ],
                id="h264-high-444-on-vaapi",
            ),
            pytest.param(
                "NVIDIA",
                [
                    "Stream #0:0: Video: wmv2 (WMV2 / 0x32564D57), yuv420p, 640x360, 356 kb/s, SAR 1:1 DAR 16:9, "
                    "24 fps, 24 tbr, 24 tbn",
                    *_frames_refused("scale_cuda"),
                ],
                id="wmv8-on-nvdec",
            ),
            pytest.param(
                "INTEL",
                [
                    "Stream #0:0: Video: mpeg4 (Simple Profile) (FMP4 / 0x34504D46), yuv420p, 640x360 "
                    "[SAR 1:1 DAR 16:9], 384 kb/s, 24 fps, 24 tbr, 24 tbn",
                    # VAAPI's own verdict: the driver lists no profile for the codec.
                    "[mpeg4 @ 0x5cc6e7411600] No support for codec mpeg4 profile 0.",
                    "[mpeg4 @ 0x5cc6e7411600] Failed setup for format vaapi: hwaccel initialisation returned error.",
                    *_frames_refused("scale_vaapi"),
                ],
                id="mpeg4-part2-on-vaapi",
            ),
            pytest.param(
                "INTEL",
                [
                    "Stream #0:0: Video: h264 (High 10), yuv420p10le(tv, progressive), 640x360 [SAR 1:1 DAR 16:9], "
                    "24 fps, 24 tbr, 1k tbn",
                    # VAAPI's verdict for a profile it lacks, printed only at the -loglevel debug of DEBUG logging.
                    "[h264 @ 0x5fd8fa87d040] Codec h264 profile 110 not supported for hardware decode.",
                    "[h264 @ 0x5fd8fa87d040] Failed setup for format vaapi: hwaccel initialisation returned error.",
                    *_frames_refused("scale_vaapi"),
                ],
                id="h264-high10-on-vaapi-debug-log",
            ),
            pytest.param(
                "NVIDIA",
                [
                    # FFmpeg 8.0.1 on the Quadro P5000, with the app's scale_cuda chain: NVDEC has no ProRes decoder.
                    "Stream #0:0[0x1]: Video: prores (4444) (ap4h / 0x68347061), yuv444p12le(progressive), 640x360, "
                    "9427 kb/s, SAR 1:1 DAR 16:9, 24 fps, 24 tbr, 12288 tbn (default)",
                    *_frames_refused("scale_cuda"),
                ],
                id="prores-on-nvdec",
            ),
            pytest.param(
                "INTEL",
                [
                    "Stream #0:0: Video: prores (HQ) (apch / 0x68637061), yuv422p10le(progressive), 1920x1080, "
                    "24 fps, 24 tbr, 12288 tbn (default)",
                    *_frames_refused("scale_vaapi"),
                ],
                id="prores-on-vaapi",
            ),
            pytest.param(
                "AMD",
                [
                    "Stream #0:0[0x1e0]: Video: mpeg1video, yuv420p(tv, progressive), 352x240 [SAR 200:219 DAR 4:3], "
                    "1150 kb/s, 29.97 fps, 29.97 tbr, 90k tbn",
                    *_frames_refused("scale_vaapi"),
                ],
                id="mpeg1-on-vaapi",
            ),
        ],
    )
    def test_is_handed_to_the_cpu_as_a_codec_the_gpu_cant_decode(self, run, gpu, stderr):  # noqa: F811
        outcome = run([(SOFTWARE_FRAMES_EXIT, 0, *stderr)], gpu=gpu)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert not _warned(outcome, "GPU driver"), outcome.warnings

    def test_a_driver_failure_before_the_software_decode_stays_a_hardware_accelerator_error(self, run):  # noqa: F811
        # A decoder whose GPU set-up failed in the driver falls back to software too, and ends the same way.
        outcome = run(
            [(SOFTWARE_FRAMES_EXIT, 0, *HEVC_HWACCEL_INIT_ONLY, *_frames_refused("scale_vaapi"))],
            gpu="INTEL",
        )

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "hwaccel"

    @pytest.mark.parametrize(
        ("gpu", "stream", "scaler"),
        [
            pytest.param(
                "INTEL",
                "Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x2160, 23.98 fps",
                "scale_vaapi",
                id="hevc-main10-on-vaapi",
            ),
            pytest.param(
                "NVIDIA",
                "Stream #0:0(eng): Video: h264 (High), yuv420p(tv, bt709, progressive), 1920x1080, 23.98 fps",
                "scale_cuda",
                id="h264-high-on-nvdec",
            ),
            pytest.param(
                "INTEL",
                "Stream #0:0(eng): Video: h264 (High), yuv420p(tv, bt709, progressive), 1920x1080, 23.98 fps",
                "scale_vaapi",
                id="h264-high-on-vaapi",
            ),
            pytest.param(
                # VAAPI has no MPEG-1 decoder, NVDEC has one.
                "NVIDIA",
                "Stream #0:0[0x1e0]: Video: mpeg1video, yuv420p(tv, progressive), 352x240 [SAR 200:219 DAR 4:3], "
                "1150 kb/s, 29.97 fps, 29.97 tbr, 90k tbn",
                "scale_cuda",
                id="mpeg1-on-nvdec",
            ),
        ],
    )
    def test_frames_of_a_codec_the_gpu_decodes_are_refused_as_a_gpu_error(self, run, gpu, stream, scaler):  # noqa: F811
        """The GPU decoded these frames, so the filter chain failed on the GPU (a 10-bit surface into an 8-bit chain):
        the hand-off says to check the GPU, and a node re-checks its GPU, rather than calling it a codec."""
        outcome = run([(SOFTWARE_FRAMES_EXIT, 0, stream, *_frames_refused(scaler))], gpu=gpu)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "hwaccel"

    def test_a_driver_error_in_an_earlier_run_stays_a_hardware_accelerator_error(self, run):  # noqa: F811
        stream = "Stream #0:0: Video: msmpeg4v3 (msmpeg4) (MP43 / 0x3334504D), yuv420p, 640x360, 24 fps"
        driver_error = "[AVHWFramesContext @ 0x6470e4f1a2c0] Failed to create surface: 2 (resource allocation failed)."
        outcome = run(
            [
                (SOFTWARE_FRAMES_EXIT, 0, stream, driver_error, *_frames_refused("scale_vaapi")),
                (SOFTWARE_FRAMES_EXIT, 0, stream, *_frames_refused("scale_vaapi")),
            ],
            gpu="INTEL",
            keyframe_gap=1.0,
        )

        assert [c["use_skip"] for c in outcome.calls] == [True, False]
        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "hwaccel"


MPEG2_422_STREAM = (
    "Stream #0:0[0x1e0]: Video: mpeg2video (4:2:2), yuv422p(tv, progressive), 640x360 [SAR 1:1 DAR 16:9], 24 fps, "
    "24 tbr, 90k tbn, start 0.541667"
)
VP9_444_STREAM = (
    "Stream #0:0: Video: vp9 (Profile 1), yuv444p(tv, progressive), 640x360, SAR 1:1 DAR 16:9, 24 fps, 24 tbr, 1k tbn"
)


class TestAChromaFormatTheGpuDecoderLacks:
    """FFmpeg offers its MPEG-2 and VP9 GPU decoders 4:2:0 video only: the rest it decodes in software without a word.

    Captured with jellyfin-ffmpeg 8.1.3 on a Quadro P5000, a TITAN RTX and an Intel UHD 770: an MPEG-2 4:2:2 file on
    NVDEC and VAAPI, and a VP9 Profile 1 (4:4:4) file on NVDEC, end with the GPU scaler refusing the frames, exit 218.
    Judged by codec alone, those were handed off as the GPU's filters failing: "check the GPU driver", and on a node a
    GPU re-check and a GPU rescan on the app for every such file.
    """

    @pytest.mark.parametrize(
        ("gpu", "stream", "scaler"),
        [
            pytest.param("NVIDIA", MPEG2_422_STREAM, "scale_cuda", id="mpeg2-422-on-nvdec"),
            pytest.param("INTEL", MPEG2_422_STREAM, "scale_vaapi", id="mpeg2-422-on-vaapi"),
            pytest.param("NVIDIA", VP9_444_STREAM, "scale_cuda", id="vp9-444-on-nvdec"),
        ],
    )
    def test_is_handed_to_the_cpu_as_a_codec_the_gpu_cant_decode(self, run, gpu, stream, scaler):  # noqa: F811
        outcome = run([(SOFTWARE_FRAMES_EXIT, 0, stream, *_frames_refused(scaler))], gpu=gpu)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert not _warned(outcome, "GPU driver"), outcome.warnings

    @pytest.mark.parametrize(
        ("gpu", "stream", "scaler"),
        [
            pytest.param(
                "NVIDIA",
                "Stream #0:0[0x1e0]: Video: mpeg2video (Main), yuv420p(tv, progressive), 640x360, 24 fps",
                "scale_cuda",
                id="mpeg2-420-on-nvdec",
            ),
            pytest.param(
                "NVIDIA",
                "Stream #0:0: Video: vp9 (Profile 0), yuv420p(tv, progressive), 640x360, 24 fps",
                "scale_cuda",
                id="vp9-420-on-nvdec",
            ),
        ],
    )
    def test_the_same_codec_in_4_2_0_is_still_refused_as_a_gpu_error(self, run, gpu, stream, scaler):  # noqa: F811
        """The GPU decodes these, so its filters failed on frames it decoded, as for H.264 above."""
        outcome = run([(SOFTWARE_FRAMES_EXIT, 0, stream, *_frames_refused(scaler))], gpu=gpu)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "hwaccel"


def _stream(codec: str, rest: str = "yuv420p(tv, progressive), 1920x1080, 23.98 fps") -> list[str]:
    return [f"Stream #0:0: Video: {codec}, {rest}"]


PRORES_ON_NVDEC = [
    "Stream #0:0[0x1]: Video: prores (4444) (ap4h / 0x68347061), yuv444p12le(progressive), 640x360, 9427 kb/s, "
    "SAR 1:1 DAR 16:9, 24 fps, 24 tbr, 12288 tbn (default)",
    *_frames_refused("scale_cuda"),
]


def _verdict_then_frames_refused(stream: str, codec: str, verdict: str, hwaccel: str) -> list[str]:
    """A run whose GPU decoder said it lacks the codec or profile, then FFmpeg's generic set-up failure line, then
    the GPU scaler refusing the software-decoded frames (jellyfin-ffmpeg 8.1.3: a Quadro P5000, a UHD 770)."""
    return [
        f"Stream #0:0: Video: {stream}",
        f"Stream #0:0 -> #0:0 ({codec} (native) -> mjpeg (native))",
        f"[{codec} @ 0x61d830a57080] {verdict}",
        f"[{codec} @ 0x61d830a57080] Failed setup for format {hwaccel}: hwaccel initialisation returned error.",
        *_frames_refused("scale_cuda" if hwaccel == "cuda" else "scale_vaapi"),
    ]


NVDEC_LACKS = "Hardware is lacking required capabilities"
# Every NVIDIA cell where the GPU has the codec's decoder but not the profile, NVDEC's own word for H.263, and VAAPI's
# for a codec or profile the driver lacks.
VERDICT_THEN_FRAMES_REFUSED = [
    pytest.param(
        "NVIDIA",
        _verdict_then_frames_refused(
            "h263 (s263 / 0x33363273), yuv420p(progressive), 352x288 [SAR 12:11 DAR 4:3], 613 kb/s, 25 fps",
            "h263",
            "Unsupported codec ID",
            "cuda",
        ),
        id="h263-on-nvdec",
    ),
    pytest.param(
        "NVIDIA",
        _verdict_then_frames_refused("vp8, yuv420p(tv, progressive), 640x360, 24 fps", "vp8", NVDEC_LACKS, "cuda"),
        id="vp8-on-pascal",
    ),
    pytest.param(
        "NVIDIA",
        _verdict_then_frames_refused(
            "h264 (High 10), yuv420p10le(tv, progressive), 640x360, 24 fps", "h264", NVDEC_LACKS, "cuda"
        ),
        id="h264-high10-on-nvdec",
    ),
    pytest.param(
        "NVIDIA",
        _verdict_then_frames_refused(
            "h264 (High 4:2:2), yuv422p(tv, progressive), 640x360, 24 fps", "h264", NVDEC_LACKS, "cuda"
        ),
        id="h264-422-on-nvdec",
    ),
    pytest.param(
        "NVIDIA",
        _verdict_then_frames_refused(
            "h264 (High 4:4:4 Predictive), yuv444p(tv, progressive), 640x360, 24 fps", "h264", NVDEC_LACKS, "cuda"
        ),
        id="h264-444-on-nvdec",
    ),
    pytest.param(
        "NVIDIA",
        _verdict_then_frames_refused(
            "hevc (Rext), yuv422p10le(tv, progressive), 640x360, 24 fps", "hevc", NVDEC_LACKS, "cuda"
        ),
        id="hevc-422-on-nvdec",
    ),
    pytest.param(
        "INTEL",
        _verdict_then_frames_refused(
            "mpeg4 (Simple Profile) (XVID / 0x44495658), yuv420p, 640x360, 1112 kb/s, 24 fps",
            "mpeg4",
            "No support for codec mpeg4 profile 0.",
            "vaapi",
        ),
        id="xvid-on-vaapi",
    ),
    pytest.param(
        "INTEL",
        _verdict_then_frames_refused(
            "mpeg4 (Advanced Simple Profile) (DX50 / 0x30355844), yuv420p, 640x352, 1180 kb/s, 25 fps",
            "mpeg4",
            "No support for codec mpeg4 profile 15.",
            "vaapi",
        ),
        id="divx-on-vaapi",
    ),
    pytest.param(
        "INTEL",
        _verdict_then_frames_refused(
            "h263 (s263 / 0x33363273), yuv420p(progressive), 352x288, 613 kb/s, 25 fps",
            "h263",
            "No support for codec h263 profile -99.",
            "vaapi",
        ),
        id="h263-on-vaapi",
    ),
    pytest.param(
        "INTEL",
        _verdict_then_frames_refused(
            "vp8, yuv420p(tv, progressive), 640x360, 24 fps", "vp8", "No support for codec vp8 profile 0.", "vaapi"
        ),
        id="vp8-on-vaapi",
    ),
]


class TestAFormatAWorkingGpuMayLack:
    """Issue #408: only 8-bit 4:2:0 H.264 failing points at the GPU's driver; every GPU the app supports decodes it."""

    @pytest.mark.parametrize(
        ("stream", "lacks"),
        [
            pytest.param("h264 (High), yuv420p(tv, bt709, progressive), 1920x1080", False, id="h264-8-bit-4-2-0"),
            pytest.param("h264 (High) (avc1 / 0x31637661), yuvj420p(pc, progressive), 720x1280", False, id="h264-jpeg"),
            pytest.param("h264 (High 10), yuv420p10le(tv, progressive), 1920x1080", True, id="h264-10-bit"),
            pytest.param("h264 (High 4:2:2), yuv422p(tv, progressive), 1920x1080", True, id="h264-4-2-2"),
            pytest.param("h264 (High), gray(tv, progressive), 1920x1080", True, id="h264-greyscale"),
            pytest.param("hevc (Main), yuv420p(tv, progressive), 1920x1080", True, id="hevc"),
            pytest.param("mpeg1video, yuv420p(tv), 352x240", True, id="mpeg-1"),
            pytest.param("mpeg4 (Advanced Simple Profile) (DX50 / 0x30355844), yuv420p, 640x480", True, id="divx"),
        ],
    )
    def test_only_8_bit_4_2_0_h264_blames_the_gpu(self, stream, lacks):
        lines = [f"  Stream #0:0: Video: {stream}"]
        assert gpu_lacks_the_format(lines, lines) is lacks

    def test_the_cover_art_is_not_the_video(self):
        lines = [
            "  Stream #0:0: Video: mjpeg (Baseline), yuvj444p(pc), 600x900 (attached pic)",
            "  Stream #0:1: Video: h264 (High), yuv420p(tv, progressive), 1920x1080",
        ]
        assert gpu_lacks_the_format(lines, lines) is False

    def test_a_run_stopped_at_the_verdict_names_the_format_the_gpu_lacks(self, run):  # noqa: F811
        stopped = (FFMPEG_SIGNALLED, 0, *INPUT_DUMP, *_av1_block("cuda"), ffmpeg_runner.GPU_CANT_DECODE_LINE)
        outcome = run([stopped], gpu="NVIDIA")

        assert_handed_to_cpu(outcome)
        assert GPU_LACKS_FORMAT in str(outcome.result), outcome.result

    def test_the_simpler_chains_retry_names_the_format_the_gpu_lacks_too(self, run):  # noqa: F811
        stopped = (FFMPEG_SIGNALLED, 0, *INPUT_DUMP, *_av1_block("cuda"), ffmpeg_runner.GPU_CANT_DECODE_LINE)
        outcome = run([(RAW_SIGSEGV, 0), stopped], gpu="NVIDIA", dv5=True)

        assert outcome.tiers == ["primary", "dv-safe"]
        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert GPU_LACKS_FORMAT in str(outcome.result), outcome.result

    def test_without_the_listing_the_gpu_is_blamed(self):
        assert gpu_lacks_the_format(["Conversion failed!"], ["Conversion failed!"]) is False

    @pytest.mark.parametrize(
        "driver_error",
        [
            "[hevc @ 0x1] Failed to sync surface 0x5 (operation failed).",
            "[AVHWFramesContext @ 0x1] Failed to create surface: 2 (resource allocation failed).",
        ],
        ids=["surface-sync", "surface-create"],
    )
    def test_a_driver_error_beside_any_format_blames_the_gpu(self, driver_error):
        lines = ["  Stream #0:0: Video: hevc (Main 10), yuv420p10le(tv, progressive), 3840x2160", driver_error]
        assert gpu_lacks_the_format(lines, lines) is False

    def test_the_generic_set_up_line_after_the_decoders_verdict_is_no_driver_error(self):
        lines = _verdict_then_frames_refused(
            "hevc (Main 4:2:2 10), yuv422p10le(tv, progressive), 1920x1080",
            "hevc",
            "No support for codec hevc profile 4.",
            "vaapi",
        )
        assert gpu_lacks_the_format(lines, lines) is True

    def test_an_8_bit_h264_the_driver_says_it_lacks_is_still_handed_off_as_the_gpus_fault(self, run):  # noqa: F811
        stderr = _verdict_then_frames_refused(
            "h264 (High), yuv420p(tv, progressive), 1920x1080, 23.98 fps",
            "h264",
            "No support for codec h264 profile 100.",
            "vaapi",
        )
        with patch(
            "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
            side_effect=_fake_ffmpeg(stderr, [], exit_code=SOFTWARE_FRAMES_EXIT),
        ):
            outcome = run([], gpu="INTEL", keyframe_gap=1.0, runner_factory=create_ffmpeg_runner)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert GPU_LACKS_FORMAT not in str(outcome.result), outcome.result


class TestAGpuWithoutTheDecoderIsAQuietHandOff:
    """A ProRes, WMV, DivX 3, H.263 or VP8 file on a GPU worker is a normal CPU fallback ("No action needed").

    It used to log two ERROR "FFmpeg failed …" lines, two "unusual exit code … broken hardware acceleration driver"
    warnings and two stderr dumps, around a full-frame rerun that decodes in software the same way and fails the
    same way (jellyfin-ffmpeg 8.1.3 on a Quadro P5000). So did a file whose GPU decoder says it lacks the codec or
    profile (NVDEC on VP8, H.264 High 10 / 4:2:2 / 4:4:4, HEVC 4:2:2, H.263; VAAPI on MPEG-4 part 2, H.263, VP8): FFmpeg
    follows that verdict with its generic "hwaccel initialisation returned error", which read as a driver error.
    """

    @staticmethod
    def _run_real(run, stderr: list[str], gpu: str = "NVIDIA"):  # noqa: F811
        procs: list[MagicMock] = []
        records: list[str] = []
        sink = logger.add(lambda m: records.append(str(m)), level="INFO", format="{level}|{message}")
        try:
            with patch(
                "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
                side_effect=_fake_ffmpeg(stderr, procs, exit_code=SOFTWARE_FRAMES_EXIT),
            ):
                outcome = run([], gpu=gpu, keyframe_gap=1.0, runner_factory=create_ffmpeg_runner)
        finally:
            logger.remove(sink)
        return outcome, procs, records

    @pytest.mark.parametrize(
        ("gpu", "stderr"),
        [
            pytest.param("NVIDIA", PRORES_ON_NVDEC, id="prores-on-nvdec"),
            pytest.param(
                "INTEL",
                [
                    "Stream #0:0: Video: wmv2 (WMV2 / 0x32564D57), yuv420p, 640x360, 356 kb/s, 24 fps",
                    *_frames_refused("scale_vaapi"),
                ],
                id="wmv8-on-vaapi",
            ),
            *VERDICT_THEN_FRAMES_REFUSED,
        ],
    )
    def test_one_gpu_run_and_no_error_before_the_cpu_takes_it(self, run, gpu, stderr):  # noqa: F811
        outcome, procs, records = self._run_real(run, stderr, gpu)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        # A format a working GPU may lack: the worker doesn't count it towards "GPU keeps failing".
        assert GPU_LACKS_FORMAT in str(outcome.result), outcome.result
        assert len(procs) == 1, "a full-frame rerun decodes in software the same way"
        assert not [line for line in records if line.startswith("ERROR|")], records
        assert not _warned(outcome, "unusual exit code"), outcome.warnings
        assert not _warned(outcome, "stderr lines"), outcome.warnings
        assert not _warned(outcome, "retrying with full-frame"), outcome.warnings
        assert any(line.startswith("INFO|The GPU can't decode the video of") for line in records), records

    @pytest.mark.parametrize(
        "stderr",
        [
            pytest.param(
                [
                    "Stream #0:0(eng): Video: h264 (High), yuv420p(tv, bt709, progressive), 1920x1080, 23.98 fps",
                    *_frames_refused("scale_cuda"),
                ],
                id="frames-the-gpu-decoded",
            ),
            pytest.param(
                [
                    PRORES_ON_NVDEC[0],
                    "[AVHWFramesContext @ 0x6470e4f1a2c0] Failed to create surface: 2 (resource allocation failed).",
                    *_frames_refused("scale_cuda"),
                ],
                id="a-driver-error-first",
            ),
            pytest.param(
                [
                    "Stream #0:0: Video: hevc (Main), yuv420p(tv, progressive), 1920x1080, 23.98 fps",
                    *HEVC_HWACCEL_INIT_ONLY,
                    *_frames_refused("scale_cuda"),
                ],
                id="a-decode-context-the-driver-failed-to-create",
            ),
        ],
    )
    def test_a_gpu_failure_still_says_so_and_gets_its_full_frame_rerun(self, run, stderr):  # noqa: F811
        outcome, procs, records = self._run_real(run, stderr)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "hwaccel"
        assert len(procs) == 2
        assert any(line.startswith("INFO|FFmpeg failed while extracting frames") for line in records), records
        assert not any("The GPU can't decode the video of" in line for line in records), records

    def test_a_driver_error_besides_the_decoders_verdict_still_says_so(self, run):  # noqa: F811
        # Only FFmpeg's generic set-up line gives way to the verdict; a driver error of its own keeps the run noisy.
        verdict_run = VERDICT_THEN_FRAMES_REFUSED[1].values[1]
        stderr = [
            *verdict_run[:3],
            "[AVHWFramesContext @ 0x6470e4f1a2c0] Failed to create surface: 2 (resource allocation failed).",
            *verdict_run[3:],
        ]

        outcome, procs, records = self._run_real(run, stderr)

        assert_handed_to_cpu(outcome)
        assert len(procs) == 2
        assert any(line.startswith("INFO|FFmpeg failed while extracting frames") for line in records), records
        assert not any("The GPU can't decode the video of" in line for line in records), records


class TestEachGpuDecoderHasItsOwnCodecs:
    """Whether FFmpeg decoded the video in software is judged by the decoder the run asked for: libavcodec's
    ``*_nvdec``, ``*_vaapi``, ``*_d3d11va`` and ``*_videotoolbox`` hwaccels differ, so a codec one decodes on the GPU
    is decoded in software by another. Each pair below flips with the decoder, so one set shared by every decoder
    fails one side of it."""

    @pytest.mark.parametrize(
        ("codec", "hwaccel", "in_software"),
        [
            pytest.param("mpeg1video", "cuda", False, id="mpeg1-nvdec"),
            pytest.param("mpeg1video", "vaapi", True, id="mpeg1-vaapi"),
            pytest.param("h263", "cuda", True, id="h263-nvdec"),
            pytest.param("h263", "vaapi", False, id="h263-vaapi"),
            pytest.param("vvc", "cuda", True, id="vvc-nvdec"),
            pytest.param("vvc", "vaapi", False, id="vvc-vaapi"),
            pytest.param("mpeg4 (Simple Profile) (FMP4 / 0x34504D46)", "cuda", False, id="mpeg4-nvdec"),
            pytest.param("mpeg4 (Simple Profile) (FMP4 / 0x34504D46)", "d3d11va", True, id="mpeg4-d3d11va"),
            pytest.param("prores (HQ) (apch / 0x68637061)", "cuda", True, id="prores-nvdec"),
            pytest.param("prores (HQ) (apch / 0x68637061)", "videotoolbox", False, id="prores-videotoolbox"),
            pytest.param("hevc (Main 10)", "cuda", False, id="hevc-nvdec"),
            pytest.param("hevc (Main 10)", None, True, id="hevc-no-gpu-decoder"),
            pytest.param("h264 (High)", "vaapi", False, id="h264-vaapi"),
            pytest.param("h264 (High)", None, True, id="h264-no-gpu-decoder"),
        ],
    )
    def test_a_codec_is_decoded_in_software_only_where_that_decoder_lacks_it(self, codec, hwaccel, in_software):
        from media_preview_generator.processing.generator import _decoded_in_software

        assert _decoded_in_software(_stream(codec), hwaccel) is in_software


class TestTheHandOffJudgesTheDecoderTheRunAskedFor:
    """A tier can decode with another ``-hwaccel`` than the worker's GPU: the software Dolby Vision tier asks for none.
    The CPU hand-off reads the decoder from the command that ran, which the runner keeps."""

    @pytest.mark.parametrize(
        ("gpu", "device", "tier", "hwaccel"),
        [
            pytest.param("AMD", "/dev/dri/renderD128", {}, "vaapi", id="amd-primary"),
            pytest.param(
                "AMD",
                "/dev/dri/renderD128",
                {"init_vulkan": True, "disable_vaapi_dv5": True},
                None,
                id="amd-software-dv",
            ),
            pytest.param("NVIDIA", "cuda:0", {}, "cuda", id="nvidia-primary"),
        ],
    )
    def test_the_runner_keeps_the_command_each_run_started(self, tmp_path, mock_config, gpu, device, tier, hwaccel):
        from media_preview_generator.processing.generator import hwaccel_in

        last_command: list[str] = []
        runner = create_ffmpeg_runner(
            video_file=str(tmp_path / "movie.mkv"),
            output_folder=str(tmp_path / "frames"),
            gpu=gpu,
            gpu_device_path=device,
            config=mock_config,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            path_kind=DV5_PATH_LIBPLACEBO if tier else "sdr",
            libplacebo_vf="fps=fps=0.1,hwupload,libplacebo" if tier else None,
            use_libplacebo=bool(tier),
            dv5_software_fallback=False,
            base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
            fps_filter="fps=fps=0.1:round=up:start_time=0",
            hdr10_zscale_chain="",
            last_command=last_command,
        )
        started: list[list[str]] = []
        popen = _fake_ffmpeg(INPUT_DUMP, [])
        with patch(
            "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
            side_effect=lambda args, **kwargs: started.append(list(args)) or popen(args, **kwargs),
        ):
            runner(use_skip=False, **tier)

        # Any other Popen is an FFmpeg probe, not a decode of the file.
        assert [args for args in started if "-i" in args] == [last_command]
        assert hwaccel_in(last_command) == hwaccel

    def test_frames_a_run_decoded_in_software_on_purpose_are_refused_by_the_gpu_filters(self):
        """No GPU decoder was asked for, so the GPU can't have lacked one: the hand-off says to check the GPU, not that
        it can't decode the codec."""
        from media_preview_generator.processing.generator import _gpu_hand_off

        stderr = [
            *_stream("hevc (Main 10)", "yuv420p10le(tv, bt2020nc), 3840x2160, 23.98 fps"),
            *_frames_refused("hwupload"),
        ]
        assert _gpu_hand_off(SOFTWARE_FRAMES_EXIT, stderr, stderr, stopped_part_way=False, hwaccel=None) == (
            "hwaccel",
            "the GPU's filters couldn't take the decoded frames",
        )


def _logged(run, *args, **kwargs):  # noqa: F811
    records: list[str] = []
    sink = logger.add(lambda m: records.append(str(m)), level="INFO", format="{level}|{message}")
    try:
        outcome = run(*args, **kwargs)
    finally:
        logger.remove(sink)
    return outcome, records


GREY_H264 = {"format": "AVC", "color_space": "Y"}


class TestGreyscaleH264OnNvdecGoesToTheCpu:
    """NVDEC decodes H.264 greyscale (4:0:0) into coloured stripes and FFmpeg reports success (a Quadro P5000 and a
    TITAN RTX). MediaInfo reads the format before FFmpeg runs, so the file goes to the CPU without a GPU run."""

    def test_the_cpu_makes_its_thumbnails_and_the_gpu_never_runs(self, run):  # noqa: F811
        outcome, records = _logged(run, [(0, EXPECTED)], gpu="NVIDIA", track_fields=GREY_H264)

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert outcome.calls == [], "the GPU run would publish stripes"
        assert any(line.startswith("INFO|") and "greyscale H.264" in line for line in records), records
        assert not outcome.warnings, outcome.warnings

    @pytest.mark.parametrize(
        ("gpu", "fields"),
        [
            pytest.param("INTEL", GREY_H264, id="vaapi-decodes-grey-right"),
            pytest.param("AMD", GREY_H264, id="amd-vaapi"),
            pytest.param(None, GREY_H264, id="the-cpu-run"),
            pytest.param("NVIDIA", {"format": "HEVC", "color_space": "Y"}, id="hevc-grey-hands-off-by-itself"),
            pytest.param("NVIDIA", {"format": "AVC", "color_space": "YUV"}, id="colour-h264"),
        ],
    )
    def test_everything_else_keeps_its_run(self, run, gpu, fields):  # noqa: F811
        outcome = run([(0, EXPECTED)], gpu=gpu, track_fields=fields)

        assert_published(outcome, EXPECTED)
        assert len(outcome.calls) == 1


def _picture(red, green, blue) -> np.ndarray:
    return np.dstack([np.broadcast_to(np.asarray(c), (180, 320)) for c in (red, green, blue)]).astype(np.uint8)


_RAMP = np.tile(np.linspace(80, 200, 320), (180, 1))  # the luma detail a quarter-value frame keeps
_NOISE = np.random.default_rng(7).integers(0, 14, size=(180, 320))
# Measured on a Quadro P5000 from VP9 Profile 2: SDR comes out 0,~130,0; after HDR tone mapping red ~12 and blue ~7.
SDR_LOST_COLOUR = _picture(0, _RAMP, 0)
HDR_LOST_COLOUR = _picture(6 + _NOISE, _RAMP - 10, _NOISE)
# Two thirds of it green-only (grass) under a sky.
GRASS_UNDER_SKY = np.vstack([_picture(100, 150, 220)[:60], _picture(30 + _NOISE, 140, 20 + _NOISE)[60:]])
NIGHT_FOREST = _picture(5, 40, 5)
GREEN_SCREEN = _picture(40 + _NOISE, 200, 70 + _NOISE)


def _thumbnail_runner(pictures: list[np.ndarray]):
    """``create_ffmpeg_runner`` stand-in whose GPU run writes these pictures as its JPEG thumbnails."""

    def factory(*, output_folder, gpu=None, gpu_device_path=None, last_command=None, **_kwargs):
        def _run(use_skip=False, **_run_kwargs):
            if last_command is not None:
                last_command[:] = ["ffmpeg", *hwaccel_decode_args(gpu, gpu_device_path, keep_on_gpu=True).args]
            for i, picture in enumerate(pictures, 1):
                Image.fromarray(picture).save(os.path.join(output_folder, f"img-{i:06d}.jpg"), quality=85)
            return 0, 1.0, "10x", []

        return _run

    return factory


class TestThumbnailsTheGpuDecodedWithoutColour:
    """NVDEC on a Pascal card (a Quadro P5000) returns VP9 Profile 2 frames at a quarter of their values and FFmpeg
    reports success: every thumbnail is green with no red or blue. The GPU lacks that profile, so the CPU makes them."""

    @pytest.mark.parametrize(
        ("gpu", "picture"),
        [
            pytest.param("NVIDIA", SDR_LOST_COLOUR, id="sdr-on-pascal"),
            pytest.param("NVIDIA", HDR_LOST_COLOUR, id="hdr10-tone-mapped-on-pascal"),
            pytest.param("INTEL", SDR_LOST_COLOUR, id="any-gpu-decoder"),
        ],
    )
    def test_green_only_thumbnails_go_to_the_cpu(self, run, gpu, picture):  # noqa: F811
        outcome, records = _logged(run, [], gpu=gpu, duration_s=50, runner_factory=_thumbnail_runner([picture] * 5))

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "codec"
        assert any(line.startswith("INFO|") and "without its colours" in line for line in records), records
        assert not [line for line in records if line.startswith(("WARNING|", "ERROR|"))], records

    @pytest.mark.parametrize(
        "pictures",
        [
            pytest.param([GRASS_UNDER_SKY] * 5, id="grass-under-a-sky"),
            pytest.param([NIGHT_FOREST] * 5, id="dark-green-night"),
            pytest.param([GREEN_SCREEN] * 5, id="green-screen"),
            pytest.param(
                [GRASS_UNDER_SKY, SDR_LOST_COLOUR, SDR_LOST_COLOUR, SDR_LOST_COLOUR, GRASS_UNDER_SKY],
                id="green-only-in-the-middle",
            ),
        ],
    )
    def test_real_green_footage_is_published(self, run, pictures):  # noqa: F811
        outcome = run([], gpu="NVIDIA", duration_s=50, runner_factory=_thumbnail_runner(pictures))

        assert_published(outcome, 5)

    def test_the_cpu_run_publishes_what_it_decoded(self, run):  # noqa: F811
        outcome = run([], gpu=None, duration_s=50, runner_factory=_thumbnail_runner([SDR_LOST_COLOUR] * 5))

        assert_published(outcome, 5)
