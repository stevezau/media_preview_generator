"""Issue #408: the first thumbnail of a video that starts after its audio, and 3D videos FFmpeg refused.

For a video that starts after its audio (41 of 400 sampled library MKVs, by up to 83 ms) ``fps=…:round=up`` began at
the first interval mark after the video's first frame, and FFmpeg filled thumbnail 0 with a copy of thumbnail 1: the
BIF showed the first interval's picture at 0 s. A clip shorter than the interval got no thumbnail at all, and FFmpeg
then failed opening the JPEG encoder (exit 234, "Error sending frames to consumers: Invalid argument").

A 3D MKV whose H.264 also holds a frame-packing SEI carries its 3D layout twice, and FFmpeg 8 refused every frame
("-17 (File exists)", exit 239) on the CPU and every GPU. It is read again without the SEI.
"""

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from PIL import Image, ImageStat

from media_preview_generator.processing.filter_chain import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
    build_dv5_vf,
    build_fps_filter,
)
from media_preview_generator.processing.generator import STRIP_H264_SEI_BSF, h264_side_data_twice
from tests.test_processing_gpu_cant_decode import _fake_ffmpeg, _runner
from tests.test_processing_partial_output import (
    EXPECTED,
    INTERVAL_S,
    _write_frames,
    assert_failed,
    assert_published,
    run,  # noqa: F401  (fixture)
)

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None or shutil.which("ffprobe") is None, reason="needs ffmpeg and ffprobe")

# FFmpeg 8.1.2 (the app image) on a 3D side-by-side H.264 MKV with a frame-packing SEI, CPU decode.
H264_3D_STREAM = (
    "Stream #0:0: Video: h264 (High), yuv420p(tv, progressive), 1920x1008 [SAR 1:1 DAR 40:21], 23.98 fps, 23.98 tbr"
)
SIDE_DATA_TWICE = [
    "[vf#0:0 @ 0x5ce1e7324d00] Task finished with error code: -17 (File exists)",
    "[vf#0:0 @ 0x5ce1e7324d00] Terminating thread with return code -17 (File exists)",
    "[vost#0:0/mjpeg @ 0x5ce1e73246c0] Task finished with error code: -22 (Invalid argument)",
    "Conversion failed!",
]
SIDE_DATA_TWICE_EXIT = 239  # -EEXIST & 0xff


def _make_video_after_audio(path: Path, duration_s: float, offset_s: float = 0.04) -> None:
    """A real video whose brightness is 8 × its time in seconds, starting ``offset_s`` after its audio."""
    subprocess.run(
        [FFMPEG, "-nostdin", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={duration_s}",
         "-itsoffset", str(offset_s),
         "-f", "lavfi", "-i", f"nullsrc=s=64x36:r=5:d={duration_s},geq=lum='T*8':cb=128:cr=128",
         "-map", "1:v", "-map", "0:a", "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", str(path)],
        check=True,
        capture_output=True,
        timeout=60,
    )  # fmt: skip
    starts = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.split()
    start = dict(line.split(",") for line in starts)
    assert float(start["video"]) > float(start["audio"]), starts


def _brightness(path: Path) -> float:
    with Image.open(path) as image:
        return ImageStat.Stat(image.convert("L")).mean[0]


def _real_run(run, mock_config, duration_s: float):  # noqa: F811
    from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

    mock_config.ffmpeg_path = FFMPEG
    return run([], gpu=None, duration_s=duration_s, runner_factory=create_ffmpeg_runner)


class TestThumbnailsStartAtZero:
    def test_the_fps_filter_pins_the_first_thumbnail_to_zero(self):
        assert build_fps_filter(0.1) == "fps=fps=0.1:round=up:start_time=0"

    @pytest.mark.parametrize("path_kind", [DV5_PATH_INTEL_OPENCL, DV5_PATH_VAAPI_VULKAN, DV5_PATH_LIBPLACEBO])
    def test_dolby_vision_chains_use_the_same_fps_filter(self, path_kind):
        vf = build_dv5_vf(path_kind=path_kind, tonemap_algorithm="hable", fps_value=0.1, base_scale="scale=320:240")

        assert vf.startswith(build_fps_filter(0.1) + ",")

    @needs_ffmpeg
    def test_each_thumbnail_shows_its_own_time_when_the_video_starts_after_the_audio(
        self,
        run,  # noqa: F811
        tmp_path,
        mock_config,
    ):
        _make_video_after_audio(tmp_path / "movie.mkv", duration_s=25)

        outcome = _real_run(run, mock_config, duration_s=25)

        assert_published(outcome, 3)
        # Brightness is 8 × seconds: 0 s, 10 s and 20 s read about 0, 80 and 160. Before, thumbnail 0 read about 80.
        brightness = [_brightness(tmp_path / "frames" / name) for name in outcome.frames_left]
        for k, value in enumerate(brightness):
            assert abs(value - 8 * k * INTERVAL_S) < 12, brightness

    @needs_ffmpeg
    def test_a_clip_shorter_than_the_interval_gets_its_one_thumbnail(
        self,
        run,  # noqa: F811
        tmp_path,
        mock_config,
    ):
        _make_video_after_audio(tmp_path / "movie.mkv", duration_s=9.75)

        outcome = _real_run(run, mock_config, duration_s=9.75)

        assert_published(outcome, 1)
        assert _brightness(tmp_path / "frames" / outcome.frames_left[0]) < 12


class TestSideDataTwiceDetection:
    def test_an_h264_video_refused_for_its_3d_layout_twice(self):
        assert h264_side_data_twice(SIDE_DATA_TWICE_EXIT, [H264_3D_STREAM, *SIDE_DATA_TWICE])

    @pytest.mark.parametrize(
        ("rc", "stderr"),
        [
            pytest.param(0, [H264_3D_STREAM, *SIDE_DATA_TWICE], id="a-run-that-succeeded"),
            pytest.param(
                SIDE_DATA_TWICE_EXIT,
                [H264_3D_STREAM.replace("h264 (High)", "hevc (Main)"), *SIDE_DATA_TWICE],
                id="hevc-whose-nal-type-6-is-not-sei",
            ),
            pytest.param(SIDE_DATA_TWICE_EXIT, SIDE_DATA_TWICE, id="no-input-listing"),
            pytest.param(
                SIDE_DATA_TWICE_EXIT,
                [H264_3D_STREAM, "[out#0/image2 @ 0x5c84] Task finished with error code: -17 (File exists)"],
                id="file-exists-from-the-output-not-the-filters",
            ),
        ],
    )
    def test_anything_else_is_not(self, rc, stderr):
        assert not h264_side_data_twice(rc, stderr)


def _scripted_ffmpeg(calls: list[list[str]], *, stripped_exits: list[int], codec_line: str = H264_3D_STREAM):
    """``Popen`` for the real runner: a run with the SEI refuses the frames; runs without it exit ``stripped_exits``
    in turn (the last one repeats), writing every thumbnail when they exit 0 and refusing again on 239."""

    def popen(args, **kwargs):
        args = list(args)
        calls.append(args)
        stripped = "-bsf:V:0" in args
        exit_code = SIDE_DATA_TWICE_EXIT
        if stripped:
            done = sum("-bsf:V:0" in c for c in calls) - 1
            exit_code = stripped_exits[min(done, len(stripped_exits) - 1)]
        lines = [codec_line, *(SIDE_DATA_TWICE if exit_code == SIDE_DATA_TWICE_EXIT else [])]
        if exit_code == 0:
            _write_frames(os.path.dirname(args[-1]), EXPECTED)
        kwargs["stderr"].write("\n".join(lines) + "\n")
        kwargs["stderr"].flush()
        proc = MagicMock(pid=4242, returncode=None)
        polls_left = iter([None])

        def poll():
            if proc.returncode is None and next(polls_left, "exited") == "exited":
                proc.returncode = exit_code
            return proc.returncode

        proc.poll.side_effect = poll
        return proc

    return popen


def _run_scripted(run, gpu, **script):  # noqa: F811
    from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

    calls: list[list[str]] = []
    records: list[str] = []
    sink = logger.add(lambda m: records.append(str(m)), level="INFO", format="{level}|{message}")
    try:
        with patch(
            "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
            side_effect=_scripted_ffmpeg(calls, **script),
        ):
            outcome = run([], gpu=gpu, keyframe_gap=1.0, runner_factory=create_ffmpeg_runner)
    finally:
        logger.remove(sink)
    return outcome, calls, records


def _reads_without_sei(args: list[str]) -> bool:
    if "-bsf:V:0" not in args:
        return False
    at = args.index("-bsf:V:0")
    return args[at + 1] == STRIP_H264_SEI_BSF and at < args.index("-i")


class TestA3dVideoIsReadAgainWithoutItsSei:
    @pytest.mark.parametrize(
        ("gpu", "hwaccel"),
        [pytest.param(None, None, id="cpu"), pytest.param("INTEL", "vaapi", id="vaapi"), ("NVIDIA", "cuda")],
    )
    def test_the_refused_run_is_read_again_without_the_sei_decoding_every_frame(self, run, gpu, hwaccel):  # noqa: F811
        outcome, calls, records = _run_scripted(run, gpu, stripped_exits=[0])

        assert_published(outcome, EXPECTED)
        assert len(calls) == 2
        first, again = calls
        assert "-skip_frame:v" in first and not _reads_without_sei(first)
        # Without the SEI, keyframe-only decode keeps only an open-GOP encode's first keyframe (no recovery points).
        assert _reads_without_sei(again) and "-skip_frame:v" not in again
        if hwaccel:
            assert again[again.index("-hwaccel") + 1] == hwaccel
        assert not [line for line in records if line.startswith("ERROR|")], records
        assert any(line.startswith("INFO|") and "3D layout is stored twice" in line for line in records), records

    def test_every_later_run_keeps_reading_without_the_sei(self, run):  # noqa: F811
        outcome, calls, _records = _run_scripted(run, "INTEL", stripped_exits=[1, 0])

        assert len(calls) >= 3
        assert all(_reads_without_sei(c) and "-skip_frame:v" not in c for c in calls[1:])
        assert_published(outcome, EXPECTED)

    def test_a_run_without_the_sei_still_refused_fails_with_its_error(self, run):  # noqa: F811
        outcome, calls, records = _run_scripted(run, None, stripped_exits=[SIDE_DATA_TWICE_EXIT])

        assert_failed(outcome, SIDE_DATA_TWICE_EXIT)
        assert all(_reads_without_sei(c) for c in calls[1:])
        assert any(line.startswith("INFO|FFmpeg failed while extracting frames") for line in records), records

    def test_other_codecs_are_not_read_again(self, run):  # noqa: F811
        hevc = H264_3D_STREAM.replace("h264 (High)", "hevc (Main)")
        outcome, calls, records = _run_scripted(run, None, stripped_exits=[0], codec_line=hevc)

        assert not any(_reads_without_sei(c) for c in calls)
        assert_failed(outcome, SIDE_DATA_TWICE_EXIT)
        assert any(line.startswith("INFO|FFmpeg failed while extracting frames") for line in records), records

    def test_a_chapter_grab_is_read_again_without_the_sei(self, tmp_path, mock_config):
        from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

        calls: list[list[str]] = []
        output = tmp_path / "chapter.jpg"
        runner = create_ffmpeg_runner(
            video_file=str(tmp_path / "movie.mkv"),
            output_folder=str(tmp_path),
            gpu=None,
            gpu_device_path=None,
            config=mock_config,
            progress_callback=None,
            ffmpeg_threads_override=None,
            cancel_check=None,
            path_kind="sdr",
            libplacebo_vf=None,
            use_libplacebo=False,
            dv5_software_fallback=False,
            base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
            fps_filter="null",
            hdr10_zscale_chain="",
            chapter_output=str(output),
            chapter_start_ms=60_000,
        )
        with patch(
            "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
            side_effect=_scripted_ffmpeg(calls, stripped_exits=[0]),
        ):
            rc, *_ = runner(use_skip=False)

        assert rc == 0
        assert [_reads_without_sei(c) for c in calls] == [False, True]

    @pytest.mark.skipif(FFMPEG is None, reason="needs ffmpeg")
    def test_a_real_open_gop_3d_h264_mkv_gets_every_thumbnail_on_the_fast_path(
        self,
        run,  # noqa: F811
        tmp_path,
        mock_config,
    ):
        """Keyframes every 2 s clear the keyframe-only fast path; open GOP needs the SEI's recovery points there."""
        from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

        made = subprocess.run(
            [FFMPEG, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=60:s=64x36:r=5",
             "-c:v", "libx264", "-x264-params", "keyint=10:open-gop=1:frame-packing=3",
             "-metadata:s:v:0", "stereo_mode=left_right", str(tmp_path / "movie.mkv")],
            capture_output=True,
            timeout=60,
        )  # fmt: skip
        if made.returncode != 0:
            pytest.skip("needs ffmpeg with libx264")
        mock_config.ffmpeg_path = FFMPEG

        outcome = run([], gpu=None, duration_s=60, keyframe_gap=2.0, runner_factory=create_ffmpeg_runner)

        assert_published(outcome, 6)


class TestDetectionNeedsTheRefusal:
    def test_a_normal_run_keeps_the_sei(self, tmp_path, mock_config):
        procs: list[MagicMock] = []
        with patch(
            "media_preview_generator.processing.ffmpeg_runner.subprocess.Popen",
            side_effect=_fake_ffmpeg([H264_3D_STREAM], procs, exit_code=0),
        ) as popen:
            _runner(tmp_path, mock_config, None, None)(use_skip=True)

        assert len(procs) == 1
        assert "-bsf:V:0" not in popen.call_args.args[0]
