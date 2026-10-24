"""Which frames from an FFmpeg run that didn't exit cleanly become the preview.

A run can stop part-way and leave frames on disk. The rule, per run and per tier:

* interrupted, i.e. stopped from outside wherever it was: a SIGTERM, SIGINT, SIGHUP or SIGKILL (raw, as FFmpeg's own
  255 after one, or a shell's 129/130/143/137), the stall watchdog, or an I/O error (exit 251) whose stderr shows no GPU
  error. On any tier the frames are discarded and nothing is published. A GPU run hands the file to the CPU; a CPU
  run fails, so the job retries it later;
* ended on the file: a non-zero exit of its own, a crash (SIGSEGV, SIGABRT, SIGBUS, SIGFPE or SIGILL, raw or as a
  shell's 128+n: the same file crashes the same way on every scan), or exit 251 with a GPU, hardware accelerator or
  OpenCL error in its stderr. With at least ``PUBLISH_MIN_FRAME_RATIO`` of the expected frames (runtime / thumbnail
  interval) it is published with a warning on any tier, so decoders that only error at EOF keep their GPU offload
  instead of paying a second full decode on the CPU. With fewer, a GPU run hands the file to the CPU and a CPU run
  publishes the short preview with a warning (a truncated file fails the same way on every scan);
* the folder refused a write: full, over its quota, read-only or not writable by this user (ENOSPC exit 228, EROFS
  exit 226, the error's text on an output line, or the folder refusing a new file itself). On any tier and device the frames are deleted and ``WorkingFolderError`` ends the file at
  once: whatever the run wrote stops where the folder filled up, and the next tier or the CPU writes to the same folder;
* exit 0: published.

The Dolby Vision ladder (hardware DV5 -> software libplacebo -> DV-safe chain -> CPU) follows the same table:
the software-libplacebo and DV-safe tiers are for runs that ended on the file having written nothing (a filter chain
the GPU or file can't take), so an interrupted run, or one whose frames were discarded, goes straight to the CPU (GPU)
or fails (CPU).

Only the FFmpeg boundary is mocked; counting, cleanup, renaming and the cascade run for real against ``tmp_path``.
"""

import contextlib
import errno
import itertools
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.logging_config import _app_sink_filter
from media_preview_generator.processing import CancellationError, CodecNotSupportedError
from media_preview_generator.processing.ffmpeg_runner import STALL_WATCHDOG_LINE
from media_preview_generator.processing.generator import (
    FFMPEG_STALL_TIMEOUT_SEC,
    PUBLISH_MIN_FRAME_RATIO,
    WorkingFolderError,
    _diagnose_ffmpeg_exit_code,
    _folder_write_errno,
    _was_interrupted,
    clear_failures,
    failure_scope,
    generate_images,
    get_failures,
    gpu_fallback_announcement,
)
from media_preview_generator.processing.hwaccel import hwaccel_decode_args

INTERVAL_S = 10
DURATION_S = 1000
EXPECTED = DURATION_S // INTERVAL_S  # 100 thumbnails for a complete run
AT_RATIO = int(EXPECTED * PUBLISH_MIN_FRAME_RATIO)  # 95: exactly on the boundary
BELOW_RATIO = AT_RATIO - 1
NEARLY_ALL = EXPECTED - 1

STALL_KILLED = -9  # the stall watchdog's proc.kill()
FFMPEG_SIGNALLED = 255  # what FFmpeg itself exits with after a SIGTERM or SIGINT it handled
RAW_SIGTERM = -15  # a SIGTERM FFmpeg didn't handle, as Popen reports it
SHELL_SIGKILL = 137  # 128 + 9, as a wrapper shell reports it
IO_ERROR = 251  # AVERROR(EIO)
SELF_EXIT = 1
RAW_SIGINT = -signal.SIGINT
RAW_SIGHUP = -signal.SIGHUP
SHELL_SIGINT = 128 + signal.SIGINT
SHELL_SIGTERM = 128 + signal.SIGTERM
RAW_SIGSEGV = -signal.SIGSEGV  # a crash, as Popen reports it
RAW_SIGABRT = -signal.SIGABRT
SHELL_SIGSEGV = 128 + signal.SIGSEGV
SHELL_SIGBUS = 128 + signal.SIGBUS
# Exit 251 is also what a GPU decoder or an OpenCL filter's failure ends in; its stderr tells them from a disk.
HWACCEL_LINE = "[hevc @ 0x55a5] Failed to transfer data to output frame: -5."
OPENCL_LINE = "[Parsed_tonemap_opencl_2 @ 0x55a5] Failed to enqueue kernel: -5."
# What FFmpeg prints about OpenCL at -loglevel debug (the app's DEBUG log level) on a run that works: none of it is an
# error, so a plain I/O error next to it is still the disk's or the working folder's.
OPENCL_DEBUG_CHATTER = [
    "[OpenCL @ 0x5617a0] 1 OpenCL platforms found.",
    '[OpenCL @ 0x5617a0] 1 OpenCL devices found on platform "Intel(R) OpenCL Graphics".',
    "[OpenCL @ 0x5617a0] 0.0: Intel(R) OpenCL Graphics / Intel(R) Arc(TM) A380 Graphics",
    "[OpenCL @ 0x5617a0] QSV to OpenCL mapping not usable.",
    "[OpenCL @ 0x5617a0] Maximum supported image size 16384x16384.",
    "[OpenCL @ 0x5617a0] Format nv12 supported.",
    "Filter 'Parsed_tonemap_opencl_2' formats:",
    "  out[0] 'default': opencl",
    "[tonemap_opencl @ 0x7f3a10] Filter input: opencl, 3840x2160 (0).",
    "[in#0/matroska @ 0x5617a0] Error during demuxing: Input/output error",
]
SHELL_SIGHUP = 128 + signal.SIGHUP
# The folder the thumbnails go to refused a write. Real FFmpeg 8.1 lines (jellyfin-ffmpeg, a full tmpfs and a read-only
# mount); FFmpeg exits with AVERROR(errno) & 0xff.
FOLDER_FULL = 256 - errno.ENOSPC  # 228
FOLDER_READ_ONLY = 256 - errno.EROFS  # 226
SHELL_SIGABRT = 128 + signal.SIGABRT  # 134: what EDQUOT exits with too
FOLDER_FULL_STDERR = [
    "[vost#0:0/mjpeg @ 0x5edfed19cbc0] Error submitting a packet to the muxer: No space left on device",
    "[out#0/image2 @ 0x5edfed1ba180] Error muxing a packet",
    "[out#0/image2 @ 0x5edfed1ba180] Task finished with error code: -28 (No space left on device)",
    "[out#0/image2 @ 0x5edfed1ba180] Terminating thread with return code -28 (No space left on device)",
    "frame=   22 fps=2.2 q=2.0 Lsize=N/A time=00:03:40.00 bitrate=N/A speed=21.7x elapsed=0:00:10.14",
    "Conversion failed!",
]
FOLDER_READ_ONLY_STDERR = [
    "[image2 @ 0x619ec303d300] Could not open file : /work/frames/img-000001.jpg",
    "[vost#0:0/mjpeg @ 0x619ec3037bc0] Error submitting a packet to the muxer: Read-only file system",
    "[out#0/image2 @ 0x619ec3055180] Task finished with error code: -30 (Read-only file system)",
    "Conversion failed!",
]
# Stock FFmpeg 8.0.1 (Ubuntu) whose image2 muxer can't create a frame file: AVERROR(EIO) whatever the real error, so
# exit 251 and no errno text. A full btrfs, ZFS or XFS folder refuses the file itself this way, as does a read-only one.
CANT_OPEN_FRAME_STDERR = [
    "[image2 @ 0x5bd024a95f80] Could not open file : /work/frames/img-000001.jpg",
    "[vost#0:0/mjpeg @ 0x5bd024a96380] Error submitting a packet to the muxer: Input/output error",
    "[out#0/image2 @ 0x5bd024a95e80] Error muxing a packet",
    "[out#0/image2 @ 0x5bd024a95e80] Task finished with error code: -5 (Input/output error)",
    "[out#0/image2 @ 0x5bd024a95e80] Terminating thread with return code -5 (Input/output error)",
    "Conversion failed!",
]
OVER_QUOTA_LINE = (
    f"[out#0/image2 @ 0x5edfed1ba180] Task finished with error code: -{errno.EDQUOT} (Disk quota exceeded)"
)
OLD_FFMPEG_FULL_LINE = "av_interleaved_write_frame(): No space left on device"
# The app image's FFmpeg 8.1.2, run as a user that may not write the folder (chmod 555): exit AVERROR(EACCES) & 0xff and
# the errno's text on the output lines. A video that user may not read exits 243 too, saying so only about the input.
PERMISSION_DENIED = 256 - errno.EACCES  # 243
NOT_PERMITTED = 256 - errno.EPERM  # 255, also FFmpeg's own exit after a SIGTERM it handled
FOLDER_PERMISSION_DENIED_STDERR = [
    "[image2 @ 0x5c8427bd0480] Could not open file : /work/frames/img-000001.jpg",
    "[vost#0:0/mjpeg @ 0x5c8427bd0700] Error submitting a packet to the muxer: Permission denied",
    "[out#0/image2 @ 0x5c8427b9ae40] Error muxing a packet",
    "[out#0/image2 @ 0x5c8427b9ae40] Task finished with error code: -13 (Permission denied)",
    "[out#0/image2 @ 0x5c8427b9ae40] Terminating thread with return code -13 (Permission denied)",
    "Conversion failed!",
]
FOLDER_NOT_PERMITTED_STDERR = [
    line.replace("Permission denied", "Operation not permitted").replace("-13", "-1")
    for line in FOLDER_PERMISSION_DENIED_STDERR
]
VIDEO_PERMISSION_DENIED_STDERR = [
    "[in#0 @ 0x5f186b621c40] Error opening input: Permission denied",
    "Error opening input file /media/movie.mkv.",
    "Error opening input files: Permission denied",
]
# Mesa on a read-only root: about its shader cache, not the thumbnails; the run goes on fine.
MESA_SHADER_CACHE_LINE = "Failed to create //.cache for shader cache (Read-only file system)---disabling."

DEVICE_FOR = {None: None, "NVIDIA": "cuda:0", "AMD": "/dev/dri/renderD128", "INTEL": "/dev/dri/renderD128"}


@dataclass
class Outcome:
    result: object
    calls: list
    frames_left: list[str]
    failures: list[dict]
    warnings: list[str] = field(default_factory=list)
    # "LEVEL|message" for what the job's own log gets (INFO and up) and what app.log gets at the default INFO level.
    job_log: list[str] = field(default_factory=list)
    app_log: list[str] = field(default_factory=list)

    @property
    def tiers(self) -> list[str]:
        return [_tier(c) for c in self.calls]


def _tier(call: dict) -> str:
    if call.get("path_kind_override") == "sdr":
        return "dv-safe"
    if call.get("disable_vaapi_dv5"):
        return "sw-libplacebo"
    return "primary"


def _write_frames(output_folder: str, n_frames: int) -> None:
    for i in range(1, n_frames + 1):
        with open(os.path.join(output_folder, f"img-{i:06d}.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xdb" + str(i).encode())


def _make_movie(ffmpeg: str, path: Path) -> None:
    """A real 20-second video for the tests that run real FFmpeg."""
    made = subprocess.run(
        [ffmpeg, "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=20:s=64x36:r=5"]
        + ["-c:v", "mpeg4", str(path)],
        capture_output=True,
        timeout=60,
    )
    assert made.returncode == 0, made.stderr


def _ffmpeg_script(attempts: list[tuple], output_folder: str, calls: list):
    """``create_ffmpeg_runner`` stand-in: attempt N writes ``attempts[N][1]`` distinct frames, exits ``[0]``
    and prints the optional ``[2]`` as stderr."""

    def factory(*, gpu=None, gpu_device_path=None, last_command=None, **_kwargs):
        def _run(use_skip=False, init_vulkan=False, **kwargs):
            rc, n_frames, *stderr = attempts[len(calls)]
            calls.append({"use_skip": use_skip, "init_vulkan": init_vulkan, **kwargs})
            if last_command is not None:
                # The decoder the real runner asks for: the worker's GPU's, none on the software Dolby Vision tier.
                software = kwargs.get("disable_vaapi_dv5", False)
                decode = () if software else hwaccel_decode_args(gpu, gpu_device_path, keep_on_gpu=True).args
                last_command[:] = ["ffmpeg", *decode]
            _write_frames(output_folder, n_frames)
            return rc, 1.0, "10x", list(stderr)

        return _run

    return factory


@pytest.fixture
def run(mock_config, tmp_path):
    mock_config.plex_bif_frame_interval = INTERVAL_S

    def _generate(
        attempts,
        *,
        gpu=None,
        keyframe_gap=None,
        dv5=False,
        duration_s=DURATION_S,
        runner_factory=None,
        track_fields=None,
    ):
        calls: list = []
        out = str(tmp_path / "frames")
        os.makedirs(out, exist_ok=True)
        # Dolby Vision Profile 5 declares no PQ/HLG transfer, so it takes the libplacebo path (hardware Vulkan).
        track = MagicMock(
            hdr_format="Dolby Vision" if dv5 else None,
            transfer_characteristics=None,
            duration=duration_s * 1000 if duration_s else None,
            **(track_fields or {}),
        )
        warnings: list[str] = []
        job_log: list[str] = []
        app_log: list[str] = []
        sinks = [
            logger.add(lambda m: warnings.append(str(m)), level="WARNING", format="{level}|{message}"),
            logger.add(lambda m: job_log.append(str(m)), level="INFO", format="{level}|{message}"),
            logger.add(
                lambda m: app_log.append(str(m)),
                level="INFO",
                format="{level}|{message}",
                filter=_app_sink_filter("INFO", console=False),
            ),
        ]
        job_id = f"partial-output-{uuid.uuid4()}"
        try:
            with (
                failure_scope(job_id),
                patch("media_preview_generator.processing.generator.MediaInfo") as mock_mediainfo,
                patch(
                    "media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=keyframe_gap
                ),
                patch("media_preview_generator.processing.generator.display_turn", return_value=None),
                patch(
                    "media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info",
                    return_value=MagicMock(device="Real GPU", is_software=False),
                ),
                patch(
                    "media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner",
                    runner_factory or _ffmpeg_script(attempts, out, calls),
                ),
            ):
                mock_mediainfo.parse.return_value = MagicMock(video_tracks=[track])
                try:
                    result = generate_images(str(tmp_path / "movie.mkv"), out, gpu, DEVICE_FOR[gpu], mock_config)
                except (CodecNotSupportedError, WorkingFolderError) as exc:
                    result = exc
                failures = get_failures()
                clear_failures()
        finally:
            for sink in sinks:
                logger.remove(sink)
        frames_left = sorted(p for p in os.listdir(out) if p.endswith(".jpg"))
        return Outcome(result, calls, frames_left, failures, warnings, job_log, app_log)

    return _generate


def _job_logged(outcome: Outcome, *fragments: str) -> bool:
    """Whether the job's own log got a line with all the fragments (the runner's per-run lines go there, not to app.log)."""
    return any(line.startswith("INFO|") and all(f in line for f in fragments) for line in outcome.job_log)


def _warned(outcome: Outcome, *fragments: str) -> bool:
    return any(line.startswith("WARNING|") and all(f in line for f in fragments) for line in outcome.warnings)


def assert_published(outcome: Outcome, n_frames: int) -> None:
    assert not isinstance(outcome.result, Exception), f"expected a published preview, got {outcome.result!r}"
    success, image_count, *_ = outcome.result
    assert (success, image_count) == (True, n_frames)
    # The publisher re-reads the folder: every frame kept is renamed onto its timestamp.
    assert outcome.frames_left == [f"{k * INTERVAL_S:010d}.jpg" for k in range(n_frames)]
    assert outcome.failures == []


def assert_published_with_warning(outcome: Outcome, rc: int, n_frames: int) -> None:
    assert_published(outcome, n_frames)
    assert _warned(outcome, f"exited with code {rc}", f"writing {n_frames} ", "publishing them"), outcome.warnings


def assert_handed_to_cpu(outcome: Outcome) -> None:
    assert isinstance(outcome.result, CodecNotSupportedError), f"expected a CPU hand-off, got {outcome.result!r}"
    assert outcome.frames_left == [], "frames left on disk would be counted by the publisher"
    # A hand-off isn't a failed item: the same worker retries it on the CPU.
    assert outcome.failures == []


def assert_refused(outcome: Outcome) -> None:
    assert isinstance(outcome.result, WorkingFolderError), f"expected the folder's failure, got {outcome.result!r}"
    assert os.path.basename(outcome.result.filename) == "frames"
    assert outcome.frames_left == [], "frames left on disk would be counted by the publisher"
    # Nothing to retry on another device or tier: they write to the same folder.
    assert len(outcome.calls) == 1
    assert outcome.failures == []


def assert_failed(outcome: Outcome, rc: int) -> None:
    assert not isinstance(outcome.result, Exception), f"expected a failed item, got {outcome.result!r}"
    success, image_count, *_ = outcome.result
    assert (success, image_count) == (False, 0)
    assert outcome.frames_left == [], "frames left on disk would be counted by the publisher"
    assert [(f["exit_code"], f["worker_type"]) for f in outcome.failures] == [(rc, "CPU")]


def test_publish_ratio_is_ninety_five_percent():
    assert PUBLISH_MIN_FRAME_RATIO == 0.95
    assert (AT_RATIO, BELOW_RATIO) == (95, 94)


class TestInterruptedOrEndedOnTheFile:
    """Every exit code the rule branches on, one cell each: stopped from outside, or ended on the file."""

    @pytest.mark.parametrize(
        "rc",
        [
            -signal.SIGHUP,
            -signal.SIGINT,
            -signal.SIGKILL,
            -signal.SIGTERM,
            FFMPEG_SIGNALLED,
            128 + signal.SIGHUP,
            128 + signal.SIGINT,
            128 + signal.SIGKILL,
            128 + signal.SIGTERM,
        ],
        ids=["raw-sighup", "raw-sigint", "raw-sigkill", "raw-sigterm", "ffmpeg-255", "129", "130", "137", "143"],
    )
    def test_a_signal_from_outside_interrupts(self, rc):
        assert _was_interrupted(rc, []) is True

    @pytest.mark.parametrize(
        "sig", [signal.SIGSEGV, signal.SIGABRT, signal.SIGBUS, signal.SIGFPE, signal.SIGILL], ids=lambda s: s.name
    )
    @pytest.mark.parametrize("form", ["raw", "shell"])
    def test_a_crash_ends_on_the_file(self, sig, form):
        rc = -sig if form == "raw" else 128 + sig
        assert _was_interrupted(rc, []) is False

    def test_the_stall_watchdog_interrupts(self):
        assert _was_interrupted(STALL_KILLED, ["frame=10", STALL_WATCHDOG_LINE]) is True

    @pytest.mark.parametrize(
        ("stderr", "interrupted"),
        [
            ([], True),
            (["Error writing trailer: Input/output error"], True),
            (OPENCL_DEBUG_CHATTER, True),
            ([HWACCEL_LINE], False),
            ([OPENCL_LINE], False),
        ],
        ids=["no-stderr", "plain-io", "plain-io-with-opencl-debug-chatter", "gpu-error", "opencl-error"],
    )
    def test_exit_251_interrupts_only_without_a_gpu_error(self, stderr, interrupted):
        assert _was_interrupted(IO_ERROR, stderr) is interrupted

    @pytest.mark.parametrize("rc", [0, SELF_EXIT, 234, 69])
    def test_an_exit_of_its_own_ends_on_the_file(self, rc):
        assert _was_interrupted(rc, []) is False


def test_the_ratios_comment_gives_its_real_reason_and_trade_off():
    # Comments-vs-code drift guard: the comment above the constant must say what the 95% is for and what it costs.
    import inspect

    from media_preview_generator.processing import generator

    source = inspect.getsource(generator)
    comment = " ".join(
        line.strip().lstrip("#").strip()
        for line in source[: source.index("PUBLISH_MIN_FRAME_RATIO = ")].splitlines()[-8:]
        if line.strip().startswith("#")
    )
    assert "exited non-zero on its own" in comment and "crashed" in comment
    assert "duration" in comment and "end of the file" in comment
    assert "5% of the tail" in comment


# (id, rc, frames written, verdict on GPU, verdict on CPU[, stderr]). "publish-warn" = published with a WARNING line.
OUTCOMES = [
    ("exit-0", 0, EXPECTED, "publish", "publish"),
    # Interrupted: stopped from outside wherever it was.
    ("stall-kill", STALL_KILLED, NEARLY_ALL, "cpu", "fail"),
    ("sigterm", FFMPEG_SIGNALLED, NEARLY_ALL, "cpu", "fail"),
    ("sigterm-complete", FFMPEG_SIGNALLED, EXPECTED, "cpu", "fail"),
    ("raw-sigterm", RAW_SIGTERM, NEARLY_ALL, "cpu", "fail"),
    ("raw-sigint", RAW_SIGINT, NEARLY_ALL, "cpu", "fail"),
    ("raw-sighup", RAW_SIGHUP, NEARLY_ALL, "cpu", "fail"),
    ("shell-sigkill-137", SHELL_SIGKILL, NEARLY_ALL, "cpu", "fail"),
    ("shell-sigint-130", SHELL_SIGINT, NEARLY_ALL, "cpu", "fail"),
    ("shell-sigterm-143", SHELL_SIGTERM, NEARLY_ALL, "cpu", "fail"),
    ("shell-sighup-129", SHELL_SIGHUP, NEARLY_ALL, "cpu", "fail"),
    ("io-error-251", IO_ERROR, NEARLY_ALL, "cpu", "fail"),
    ("io-error-251-complete", IO_ERROR, EXPECTED, "cpu", "fail"),
    ("io-error-251-opencl-debug-chatter", IO_ERROR, NEARLY_ALL, "cpu", "fail", *OPENCL_DEBUG_CHATTER),
    # Ended on the file: a self exit, a crash, or exit 251 from a GPU error.
    ("self-exit-99pct", SELF_EXIT, NEARLY_ALL, "publish-warn", "publish-warn"),
    ("self-exit-exactly-95pct", SELF_EXIT, AT_RATIO, "publish-warn", "publish-warn"),
    ("self-exit-94pct", SELF_EXIT, BELOW_RATIO, "cpu", "publish-warn"),
    ("self-exit-40pct", SELF_EXIT, 40, "cpu", "publish-warn"),
    ("crash-raw-sigsegv-99pct", RAW_SIGSEGV, NEARLY_ALL, "publish-warn", "publish-warn"),
    ("crash-shell-sigsegv-99pct", SHELL_SIGSEGV, NEARLY_ALL, "publish-warn", "publish-warn"),
    ("crash-raw-sigabrt-exactly-95pct", RAW_SIGABRT, AT_RATIO, "publish-warn", "publish-warn"),
    ("crash-raw-sigsegv-94pct", RAW_SIGSEGV, BELOW_RATIO, "cpu", "publish-warn"),
    ("crash-shell-sigbus-40pct", SHELL_SIGBUS, 40, "cpu", "publish-warn"),
    ("io-error-251-gpu-error-99pct", IO_ERROR, NEARLY_ALL, "publish-warn", "publish-warn", HWACCEL_LINE),
    ("io-error-251-opencl-error-99pct", IO_ERROR, NEARLY_ALL, "publish-warn", "publish-warn", OPENCL_LINE),
    ("io-error-251-gpu-error-exactly-95pct", IO_ERROR, AT_RATIO, "publish-warn", "publish-warn", HWACCEL_LINE),
    ("io-error-251-gpu-error-94pct", IO_ERROR, BELOW_RATIO, "cpu", "publish-warn", HWACCEL_LINE),
    # The folder refused a write: never published, on any device, however many frames it holds.
    ("folder-full-228", FOLDER_FULL, 2, "refused", "refused", *FOLDER_FULL_STDERR),
    ("folder-full-228-99pct", FOLDER_FULL, NEARLY_ALL, "refused", "refused", *FOLDER_FULL_STDERR),
    ("folder-full-228-stderr-cut-off", FOLDER_FULL, 40, "refused", "refused"),
    ("folder-full-windows-exit", 2**32 - errno.ENOSPC, 40, "refused", "refused"),
    ("folder-full-old-ffmpeg-exit-1", SELF_EXIT, 40, "refused", "refused", OLD_FFMPEG_FULL_LINE),
    ("folder-read-only-226", FOLDER_READ_ONLY, 0, "refused", "refused", *FOLDER_READ_ONLY_STDERR),
    ("folder-over-quota-134", SHELL_SIGABRT, 40, "refused", "refused", OVER_QUOTA_LINE),
    ("crash-shell-sigabrt-134-99pct", SHELL_SIGABRT, NEARLY_ALL, "publish-warn", "publish-warn"),
    ("mesa-read-only-cache-99pct", SELF_EXIT, NEARLY_ALL, "publish-warn", "publish-warn", MESA_SHADER_CACHE_LINE),
]


def _attempt(row: tuple) -> tuple:
    """A row's one FFmpeg run: (rc, frames[, stderr])."""
    return (row[1], row[2], *row[5:])


def _check(outcome: Outcome, verdict: str, rc: int, n_frames: int) -> None:
    if verdict == "publish":
        assert_published(outcome, n_frames)
        assert not _warned(outcome, "publishing them"), outcome.warnings
    elif verdict == "publish-warn":
        assert_published_with_warning(outcome, rc, n_frames)
    elif verdict == "cpu":
        assert_handed_to_cpu(outcome)
    elif verdict == "refused":
        assert_refused(outcome)
    else:
        assert_failed(outcome, rc)


class TestPublishOrDiscardMatrix:
    @pytest.mark.parametrize(
        ("attempt", "verdict"),
        [pytest.param(_attempt(row), row[3], id=row[0]) for row in OUTCOMES],
    )
    def test_gpu_run(self, run, attempt, verdict):
        rc, n_frames = attempt[:2]
        outcome = run([attempt], gpu="NVIDIA")

        _check(outcome, verdict, rc, n_frames)
        assert len(outcome.calls) == 1, "the verdict comes from the one GPU run; nothing is re-decoded on the GPU"
        if verdict == "cpu" and rc != 0:
            assert f"exit code {rc}" in str(outcome.result)

    @pytest.mark.parametrize(
        ("attempt", "verdict"),
        [pytest.param(_attempt(row), row[4], id=row[0]) for row in OUTCOMES],
    )
    def test_cpu_run(self, run, attempt, verdict):
        rc, n_frames = attempt[:2]
        outcome = run([attempt])

        _check(outcome, verdict, rc, n_frames)
        assert len(outcome.calls) == 1

    def test_gpu_self_exit_is_discarded_when_the_runtime_is_unknown(self, run):
        """No duration means no expected count to measure against: the GPU run can't show it's complete."""
        outcome = run([(SELF_EXIT, NEARLY_ALL)], gpu="NVIDIA", duration_s=None)

        assert_handed_to_cpu(outcome)
        assert _warned(outcome, f"exited with code {SELF_EXIT}", "discarding"), outcome.warnings

    def test_cpu_self_exit_is_published_short_when_the_runtime_is_unknown(self, run):
        outcome = run([(SELF_EXIT, 40)], duration_s=None)

        assert_published_with_warning(outcome, SELF_EXIT, 40)
        assert _warned(outcome, "may be short"), outcome.warnings

    @pytest.mark.parametrize(("gpu", "verdict"), [("NVIDIA", "cpu"), (None, "fail")], ids=["gpu", "cpu"])
    def test_io_error_with_no_frames(self, run, gpu, verdict):
        """An I/O error says nothing about the GPU decoder either way; the CPU still gets its turn."""
        outcome = run([(IO_ERROR, 0)], gpu=gpu)

        _check(outcome, verdict, IO_ERROR, 0)

    @pytest.mark.parametrize(
        ("attempt", "kind"),
        [
            ((IO_ERROR, BELOW_RATIO, HWACCEL_LINE), "hwaccel"),
            ((IO_ERROR, 0, HWACCEL_LINE), "hwaccel"),
            ((IO_ERROR, 0, OPENCL_LINE), "hwaccel"),
            ((IO_ERROR, BELOW_RATIO), "io_error"),
            ((IO_ERROR, 0), "io_error"),
        ],
        ids=["gpu-error-94pct", "gpu-error-no-frames", "opencl-error-no-frames", "plain-94pct", "plain-no-frames"],
    )
    def test_exit_251_hands_off_as_a_gpu_error_only_when_its_stderr_shows_one(self, run, attempt, kind):
        outcome = run([attempt], gpu="NVIDIA")

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == kind

    def test_discarded_run_logs_why(self, run):
        outcome = run([(STALL_KILLED, NEARLY_ALL)])

        assert _warned(outcome, f"exit code {STALL_KILLED}", f"{NEARLY_ALL} thumbnails", "discarding"), outcome.warnings

    def test_short_preview_does_not_also_claim_scrubbing_is_out_of_sync(self, run):
        """The short-preview warning already explains it; the drift backstop's "unexpected, please report" doesn't
        apply."""
        outcome = run([(SELF_EXIT, 40)])

        assert_published(outcome, 40)
        assert not _warned(outcome, "out of sync"), outcome.warnings


class TestAFolderThatRefusesTheThumbnails:
    """The working folder (a node's temp folder) fills up, runs out of quota or turns read-only mid-run."""

    @pytest.mark.parametrize(
        ("attempt", "code"),
        [
            ((FOLDER_FULL, 2, *FOLDER_FULL_STDERR), errno.ENOSPC),
            ((FOLDER_FULL, 2), errno.ENOSPC),
            ((FOLDER_READ_ONLY, 0, *FOLDER_READ_ONLY_STDERR), errno.EROFS),
            ((SHELL_SIGABRT, 40, OVER_QUOTA_LINE), errno.EDQUOT),
        ],
        ids=["full", "full-stderr-cut-off", "read-only", "over-quota"],
    )
    def test_the_error_names_the_folder_and_why(self, run, tmp_path, attempt, code):
        # A node reads errno + filename to tell its temp folder's problem from the file's (nodes/server/tasks.py).
        outcome = run([attempt], gpu="NVIDIA")

        assert_refused(outcome)
        assert (outcome.result.errno, outcome.result.filename) == (code, str(tmp_path / "frames"))
        assert _diagnose_ffmpeg_exit_code(attempt[0], list(attempt[2:])) == "folder_write_error"

    @pytest.mark.parametrize(
        ("attempt", "code"),
        [
            ((PERMISSION_DENIED, 0, *FOLDER_PERMISSION_DENIED_STDERR), errno.EACCES),
            ((NOT_PERMITTED, 0, *FOLDER_NOT_PERMITTED_STDERR), errno.EPERM),
        ],
        ids=["no-permission", "not-permitted"],
    )
    def test_a_permission_error_on_the_output_is_the_folders_when_the_folder_refuses_a_new_file(
        self, run, tmp_path, attempt, code
    ):
        with self._refusing_new_files(tmp_path / "frames", code):
            outcome = run([attempt], gpu="NVIDIA")

        assert_refused(outcome)
        assert (outcome.result.errno, outcome.result.filename) == (code, str(tmp_path / "frames"))

    def test_operation_not_permitted_on_the_output_of_a_folder_that_takes_files_is_not_the_folders(self, run):
        # AVERROR(EPERM) is -1, FFmpeg's catch-all: an encoder's bare failure prints it on a [vost#] line.
        failed = "[vost#0:0/mjpeg @ 0x5c8427bd0700] Task finished with error code: -1 (Operation not permitted)"
        outcome = run([(NOT_PERMITTED, 0, failed, "Conversion failed!")] * 4, gpu="NVIDIA")

        assert not isinstance(outcome.result, WorkingFolderError), outcome.result
        assert _diagnose_ffmpeg_exit_code(NOT_PERMITTED, [failed]) != "folder_write_error"

    @pytest.mark.parametrize("gpu", ["NVIDIA", None], ids=["gpu", "cpu"])
    def test_a_keyframe_pass_that_fills_the_folder_is_not_retried_with_full_decode(self, run, gpu):
        outcome = run([(FOLDER_FULL, 12, *FOLDER_FULL_STDERR), (0, EXPECTED)], gpu=gpu, keyframe_gap=1.0)

        assert_refused(outcome)
        assert [c["use_skip"] for c in outcome.calls] == [True]

    @staticmethod
    def _refusing_new_files(folder: Path, code: int):
        """The folder refuses any new file with ``code``, as a full btrfs, ZFS or XFS one does."""
        real_open = os.open

        def refusing_open(path, *args, **kwargs):
            if Path(path).parent == folder:
                raise OSError(code, os.strerror(code), path)
            return real_open(path, *args, **kwargs)

        return patch.object(os, "open", refusing_open)

    @pytest.mark.parametrize(
        "code",
        [errno.ENOSPC, errno.EROFS, errno.EACCES, errno.EPERM],
        ids=["full", "read-only", "no-permission", "not-permitted"],
    )
    def test_a_frame_file_ffmpeg_could_not_create_is_the_folders_when_it_takes_no_new_file(self, run, tmp_path, code):
        with self._refusing_new_files(tmp_path / "frames", code):
            outcome = run([(IO_ERROR, 0, *CANT_OPEN_FRAME_STDERR)], gpu="NVIDIA")

        assert_refused(outcome)
        assert outcome.result.errno == code

    def test_a_frame_file_ffmpeg_could_not_create_in_a_folder_that_takes_files_is_a_plain_io_error(self, run, tmp_path):
        outcome = run([(IO_ERROR, 0, *CANT_OPEN_FRAME_STDERR)], gpu="NVIDIA")

        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == "io_error"
        assert os.listdir(tmp_path / "frames") == [], "the check leaves no file behind"

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="a folder without write permission stops only a POSIX non-root user",
    )
    def test_real_ffmpeg_that_could_not_create_a_frame_file_in_a_full_folder_ends_on_the_folder(
        self, run, tmp_path, mock_config
    ):
        """Real FFmpeg, runner and cascade. A folder without write permission makes FFmpeg's image2 muxer fail to
        create its first frame file, exactly as a full btrfs folder does; the check then finds the folder full."""
        from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

        ffmpeg = shutil.which("ffmpeg")
        _make_movie(ffmpeg, tmp_path / "movie.mkv")
        mock_config.ffmpeg_path = ffmpeg
        frames = tmp_path / "frames"
        frames.mkdir()
        frames.chmod(0o555)
        try:
            with self._refusing_new_files(frames, errno.ENOSPC):
                outcome = run([], gpu=None, duration_s=20, runner_factory=create_ffmpeg_runner)
        finally:
            frames.chmod(0o755)

        assert isinstance(outcome.result, WorkingFolderError), outcome.result
        assert (outcome.result.errno, outcome.result.filename) == (errno.ENOSPC, str(frames))
        refused = [line for line in outcome.warnings if "couldn't write thumbnails" in line]
        assert len(refused) == 1 and "No space left on device (exit code 251)" in refused[0], outcome.warnings
        # Nor is it put down to the video's disk as well.
        assert not any("hit an I/O error" in line for line in outcome.warnings), outcome.warnings

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="a folder without write permission stops only a POSIX non-root user",
    )
    def test_real_ffmpeg_into_a_folder_this_user_may_not_write_ends_on_the_folder(self, run, tmp_path, mock_config):
        """Real FFmpeg, runner and cascade, a folder left by another user (PUID/PGID): stock 8.0.1 says only "Could not
        open file" (the check asks the folder), the app image's 8.1.2 says "Permission denied" itself."""
        from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

        ffmpeg = shutil.which("ffmpeg")
        _make_movie(ffmpeg, tmp_path / "movie.mkv")
        mock_config.ffmpeg_path = ffmpeg
        frames = tmp_path / "frames"
        frames.mkdir()
        frames.chmod(0o555)
        try:
            outcome = run([], gpu=None, duration_s=20, runner_factory=create_ffmpeg_runner)
        finally:
            frames.chmod(0o755)

        assert isinstance(outcome.result, WorkingFolderError), outcome.result
        assert (outcome.result.errno, outcome.result.filename) == (errno.EACCES, str(frames))
        refused = [line for line in outcome.warnings if "couldn't write thumbnails" in line]
        assert len(refused) == 1 and "Permission denied (exit code" in refused[0], outcome.warnings
        assert "check PUID and PGID" in refused[0] and "Free up space" not in refused[0], refused[0]

    @pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="a file without read permission stops only a POSIX non-root user",
    )
    def test_real_ffmpeg_on_a_video_this_user_may_not_read_is_not_the_folders(self, run, tmp_path, mock_config):
        from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

        ffmpeg = shutil.which("ffmpeg")
        movie = tmp_path / "movie.mkv"
        _make_movie(ffmpeg, movie)
        mock_config.ffmpeg_path = ffmpeg
        movie.chmod(0)
        try:
            outcome = run([], gpu=None, duration_s=20, runner_factory=create_ffmpeg_runner)
        finally:
            movie.chmod(0o644)

        assert_failed(outcome, PERMISSION_DENIED)
        assert not any("couldn't write thumbnails" in line for line in outcome.warnings), outcome.warnings

    def test_a_video_this_user_may_not_read_is_the_files_failure(self, run):
        outcome = run([(PERMISSION_DENIED, 0, *VIDEO_PERMISSION_DENIED_STDERR)] * 4, gpu=None)

        assert_failed(outcome, PERMISSION_DENIED)
        assert _folder_write_errno(PERMISSION_DENIED, VIDEO_PERMISSION_DENIED_STDERR) is None
        assert _diagnose_ffmpeg_exit_code(PERMISSION_DENIED, VIDEO_PERMISSION_DENIED_STDERR) != "folder_write_error"

    def test_the_errors_text_is_read_as_ffmpeg_words_it_whatever_the_os_calls_the_errno(self, monkeypatch):
        # Windows has no EDQUOT: Python's is WSAEDQUOT, which os.strerror calls "Unknown error".
        real_strerror = os.strerror
        monkeypatch.setattr(
            os, "strerror", lambda code: "Unknown error" if code == errno.EDQUOT else real_strerror(code)
        )
        unknown = "[out#0/image2 @ 0x5edfed1ba180] Task finished with error code: -1313558101 (Unknown error)"

        assert _folder_write_errno(SELF_EXIT, [unknown]) is None
        assert _folder_write_errno(SELF_EXIT, [OVER_QUOTA_LINE]) == errno.EDQUOT
        # macOS words it the BSD way.
        assert _folder_write_errno(SELF_EXIT, [OVER_QUOTA_LINE.replace("Disk", "Disc")]) == errno.EDQUOT


class TestStallWatchdogThroughTheRealRunner:
    """The stall cell end to end: the real runner's watchdog kills FFmpeg after it wrote nearly every frame."""

    @pytest.mark.parametrize(("gpu", "verdict"), [("NVIDIA", "cpu"), (None, "fail")], ids=["gpu", "cpu"])
    def test_stall_kill_after_nearly_all_frames_is_discarded(self, run, tmp_path, gpu, verdict):
        from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

        out = str(tmp_path / "frames")
        procs: list[MagicMock] = []

        def popen(args, **_kwargs):
            assert args[-1] == f"{out}/img-%06d.jpg"
            _write_frames(out, NEARLY_ALL)
            proc = MagicMock(pid=4242, returncode=STALL_KILLED)
            proc.poll.return_value = None  # never exits on its own: only the watchdog ends it
            procs.append(proc)
            return proc

        # Every clock read is one stall timeout later than the last, so the first check trips the watchdog.
        fake_time = MagicMock(time_ns=time.time_ns)
        fake_time.time.side_effect = (i * (FFMPEG_STALL_TIMEOUT_SEC + 1) for i in itertools.count())
        with (
            patch("media_preview_generator.processing.ffmpeg_runner.subprocess.Popen", side_effect=popen),
            patch("media_preview_generator.processing.ffmpeg_runner.time", fake_time),
        ):
            outcome = run([], gpu=gpu, runner_factory=create_ffmpeg_runner)

        _check(outcome, verdict, STALL_KILLED, 0)
        assert len(procs) == 1
        procs[0].kill.assert_called_once_with()
        if gpu:
            # The hand-off names the stall (the runner says so in the stderr it returns), not a codec or a signal.
            assert outcome.result.kind == "stall"


class TestTheRunnersFailureLines:
    """The real runner's WARNING for a failed FFmpeg names the cause the exit code and stderr point at: exit 251 names
    both places an I/O error can come from (and no disk when the stderr shows the GPU failed); only a SIGKILL is put
    down to the out-of-memory killer; a crash signal is FFmpeg crashing on the file."""

    @staticmethod
    def _run_real(run, tmp_path, rc: int, stderr: str = "", gpu: str | None = "NVIDIA") -> Outcome:
        from media_preview_generator.processing.ffmpeg_runner import create_ffmpeg_runner

        out = str(tmp_path / "frames")

        def popen(args, **kwargs):
            _write_frames(out, 0)
            if stderr:
                kwargs["stderr"].write(stderr + "\n")
                kwargs["stderr"].flush()
            proc = MagicMock(pid=4242, returncode=rc)
            proc.poll.return_value = rc
            return proc

        with patch("media_preview_generator.processing.ffmpeg_runner.subprocess.Popen", side_effect=popen):
            return run([], gpu=gpu, runner_factory=create_ffmpeg_runner)

    def test_a_plain_251_names_the_videos_disk_and_the_working_folder(self, run, tmp_path):
        outcome = self._run_real(run, tmp_path, IO_ERROR)

        assert _warned(outcome, "I/O error", "disk or network share", "working folder"), outcome.warnings
        assert outcome.result.kind == "io_error"

    @pytest.mark.parametrize(
        ("rc", "stderr", "said"),
        [
            (FOLDER_FULL, "\n".join(FOLDER_FULL_STDERR), "Free up space there"),
            (FOLDER_READ_ONLY, "\n".join(FOLDER_READ_ONLY_STDERR), "Make it writable"),
            (SHELL_SIGABRT, OVER_QUOTA_LINE, "Free up space there"),
            (
                PERMISSION_DENIED,
                "\n".join(FOLDER_PERMISSION_DENIED_STDERR),
                "Let the app's user write to it (in Docker, check PUID and PGID), or mount a bigger, writable disk",
            ),
        ],
        ids=["full", "read-only", "over-quota", "no-permission"],
    )
    def test_a_refused_write_names_the_folder_not_a_crash_or_the_gpu(
        self, run, tmp_path, mock_config, rc, stderr, said
    ):
        outcome = self._run_real(run, tmp_path, rc, stderr)

        assert isinstance(outcome.result, WorkingFolderError)
        refused = [line for line in outcome.warnings if "couldn't write thumbnails" in line]
        assert len(refused) == 1 and str(tmp_path / "frames") in refused[0] and said in refused[0], outcome.warnings
        assert f"mount a bigger, writable disk at {mock_config.tmp_folder}." in refused[0], refused
        assert not any(
            "crashed" in line or "hardware acceleration" in line or "GPU" in line for line in outcome.warnings
        ), outcome.warnings

    @pytest.mark.parametrize("node", [True, False], ids=["node", "app"])
    @pytest.mark.parametrize(
        ("rc", "stderr", "refuses_files", "temp_root_refuses", "handed_back"),
        [
            (FOLDER_FULL, FOLDER_FULL_STDERR, None, False, True),
            (SHELL_SIGABRT, [OVER_QUOTA_LINE], None, False, True),
            (FOLDER_READ_ONLY, FOLDER_READ_ONLY_STDERR, None, False, True),
            (PERMISSION_DENIED, FOLDER_PERMISSION_DENIED_STDERR, errno.EACCES, True, True),
            (PERMISSION_DENIED, FOLDER_PERMISSION_DENIED_STDERR, errno.EACCES, False, False),
            (NOT_PERMITTED, FOLDER_NOT_PERMITTED_STDERR, errno.EPERM, True, True),
            (NOT_PERMITTED, FOLDER_NOT_PERMITTED_STDERR, errno.EPERM, False, False),
        ],
        ids=[
            "full",
            "over-quota",
            "read-only",
            "no-permission-temp-folder-refuses",
            "no-permission-temp-folder-takes-folders",
            "not-permitted-temp-folder-refuses",
            "not-permitted-temp-folder-takes-folders",
        ],
    )
    def test_a_refused_write_on_a_node_is_one_warning_only_for_a_file_it_hands_back(
        self, run, tmp_path, monkeypatch, rc, stderr, refuses_files, temp_root_refuses, handed_back, node
    ):
        """Lab: a node whose temp folder filled up logged an ERROR and five stderr WARNING lines for a file it only
        handed back to the queue, so a job that completed with every preview made read as having a failed file. A
        permission error the node fails the file for (its temp folder still takes new folders: nodes/server/tasks.py)
        keeps the ERROR and stderr lines that say why."""
        if node:
            monkeypatch.setenv("MODE", "node")
            # A node's thumbnails go to its task's folder inside its temp folder.
            monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path))
            # Its setpriv lookup would run through the stand-in Popen.
            monkeypatch.setattr(
                "media_preview_generator.processing.ffmpeg_runner.dies_with_node", lambda args: (args, None)
            )
        else:
            monkeypatch.delenv("MODE", raising=False)
        if temp_root_refuses:
            monkeypatch.setattr(
                "media_preview_generator.nodes.server.tasks.probe_temp_folder",
                lambda temp_root: f"The node can't write to its temp folder {temp_root} (Permission denied)",
            )

        with (
            TestAFolderThatRefusesTheThumbnails._refusing_new_files(tmp_path / "frames", refuses_files)
            if refuses_files
            else contextlib.nullcontext()
        ):
            outcome = self._run_real(run, tmp_path, rc, "\n".join(stderr))

        assert isinstance(outcome.result, WorkingFolderError)
        said = [line.split("|", 1)[0] for line in outcome.warnings if "couldn't write thumbnails" in line]
        stderr_dumps = [line for line in outcome.job_log if "stderr lines" in line]
        if node and handed_back:
            assert (said, stderr_dumps) == (["WARNING"], []), outcome.warnings
            assert not any(line.startswith("ERROR|") for line in outcome.warnings), outcome.warnings
        else:
            assert (said, len(stderr_dumps)) == (["ERROR"], 1), outcome.warnings

    def test_a_251_with_a_gpu_error_blames_neither(self, run, tmp_path):
        outcome = self._run_real(run, tmp_path, IO_ERROR, HWACCEL_LINE)

        assert not any("working folder" in line for line in outcome.warnings), outcome.warnings
        assert outcome.result.kind == "hwaccel"

    @pytest.mark.parametrize("rc", [-signal.SIGKILL, 128 + signal.SIGKILL], ids=["raw-sigkill", "137"])
    def test_a_sigkill_is_put_down_to_the_out_of_memory_killer(self, run, tmp_path, rc):
        outcome = self._run_real(run, tmp_path, rc, gpu=None)

        assert _job_logged(outcome, "SIGKILL", "out of memory"), outcome.job_log
        assert not any("crashed" in line for line in outcome.job_log), outcome.job_log

    @pytest.mark.parametrize(
        "rc",
        [-signal.SIGSEGV, -signal.SIGABRT, -signal.SIGBUS, -signal.SIGFPE, -signal.SIGILL, 128 + signal.SIGSEGV],
        ids=["raw-sigsegv", "raw-sigabrt", "raw-sigbus", "raw-sigfpe", "raw-sigill", "139"],
    )
    def test_a_crash_signal_is_ffmpeg_crashing_on_the_file(self, run, tmp_path, rc):
        outcome = self._run_real(run, tmp_path, rc, gpu=None)

        assert _job_logged(outcome, "FFmpeg crashed on", "movie.mkv"), outcome.job_log
        assert not any("memory" in line.lower() for line in outcome.job_log), outcome.job_log

    @pytest.mark.parametrize(
        "rc",
        [-signal.SIGTERM, -signal.SIGINT, -signal.SIGHUP, 128 + signal.SIGTERM, 128 + signal.SIGHUP],
        ids=["raw-sigterm", "raw-sigint", "raw-sighup", "143", "129"],
    )
    def test_another_signal_says_it_was_stopped_from_outside(self, run, tmp_path, rc):
        outcome = self._run_real(run, tmp_path, rc, gpu=None)

        assert _job_logged(outcome, "FFmpeg was stopped by"), outcome.job_log
        assert not any("memory" in line.lower() or "crashed" in line for line in outcome.job_log), outcome.job_log


# The Dolby Vision Profile 5 ladder. (id, gpu, attempts, verdict, tiers run).
DV_LADDER = [
    # CPU worker: libplacebo -> DV-safe; the DV-safe run is the final CPU tier.
    ("cpu/primary/stall-kill", None, [(STALL_KILLED, NEARLY_ALL)], "fail", ["primary"]),
    ("cpu/primary/io-error", None, [(IO_ERROR, NEARLY_ALL)], "fail", ["primary"]),
    # Killed or cut off before its first frame: no filter-chain tier runs (DV-safe would publish wrong colours).
    ("cpu/primary/stall-kill-no-frames", None, [(STALL_KILLED, 0), (0, EXPECTED)], "fail", ["primary"]),
    ("cpu/primary/io-error-no-frames", None, [(IO_ERROR, 0), (0, EXPECTED)], "fail", ["primary"]),
    ("cpu/primary/sigterm-no-frames", None, [(FFMPEG_SIGNALLED, 0), (0, EXPECTED)], "fail", ["primary"]),
    ("cpu/primary/raw-sigint-no-frames", None, [(RAW_SIGINT, 0), (0, EXPECTED)], "fail", ["primary"]),
    # A crash ended on the file: with nothing written, the DV-safe chain is still its last resort.
    ("cpu/primary/crash-no-frames", None, [(RAW_SIGSEGV, 0), (0, EXPECTED)], "publish", ["primary", "dv-safe"]),
    ("cpu/primary/shell-crash-no-frames", None, [(SHELL_SIGSEGV, 0), (0, EXPECTED)], "publish", ["primary", "dv-safe"]),
    ("cpu/primary/crash-99pct", None, [(RAW_SIGSEGV, NEARLY_ALL)], "publish-warn", ["primary"]),
    ("cpu/primary/crash-94pct", None, [(RAW_SIGSEGV, BELOW_RATIO)], "publish-warn", ["primary"]),
    ("cpu/primary/self-exit-99pct", None, [(SELF_EXIT, NEARLY_ALL)], "publish-warn", ["primary"]),
    ("cpu/primary/self-exit-95pct", None, [(SELF_EXIT, AT_RATIO)], "publish-warn", ["primary"]),
    ("cpu/primary/self-exit-94pct", None, [(SELF_EXIT, BELOW_RATIO)], "publish-warn", ["primary"]),
    ("cpu/dv-safe/exit-0", None, [(SELF_EXIT, 0), (0, EXPECTED)], "publish", ["primary", "dv-safe"]),
    ("cpu/dv-safe/stall-kill", None, [(SELF_EXIT, 0), (STALL_KILLED, NEARLY_ALL)], "fail", ["primary", "dv-safe"]),
    ("cpu/dv-safe/io-error", None, [(SELF_EXIT, 0), (IO_ERROR, NEARLY_ALL)], "fail", ["primary", "dv-safe"]),
    ("cpu/dv-safe/self-exit-99pct", None, [(SELF_EXIT, 0), (SELF_EXIT, NEARLY_ALL)], "publish-warn", None),
    ("cpu/dv-safe/self-exit-40pct", None, [(SELF_EXIT, 0), (SELF_EXIT, 40)], "publish-warn", ["primary", "dv-safe"]),
    ("cpu/dv-safe/raw-sigint", None, [(SELF_EXIT, 0), (RAW_SIGINT, NEARLY_ALL)], "fail", ["primary", "dv-safe"]),
    ("cpu/dv-safe/crash-40pct", None, [(SELF_EXIT, 0), (RAW_SIGSEGV, 40)], "publish-warn", ["primary", "dv-safe"]),
    # NVIDIA: CUDA decode + libplacebo -> DV-safe -> CPU.
    ("nvidia/primary/stall-kill", "NVIDIA", [(STALL_KILLED, NEARLY_ALL)], "cpu", ["primary"]),
    ("nvidia/primary/io-error", "NVIDIA", [(IO_ERROR, NEARLY_ALL)], "cpu", ["primary"]),
    ("nvidia/primary/stall-kill-no-frames", "NVIDIA", [(STALL_KILLED, 0), (0, EXPECTED)], "cpu", ["primary"]),
    ("nvidia/primary/io-error-no-frames", "NVIDIA", [(IO_ERROR, 0), (0, EXPECTED)], "cpu", ["primary"]),
    ("nvidia/primary/self-exit-99pct", "NVIDIA", [(SELF_EXIT, NEARLY_ALL)], "publish-warn", ["primary"]),
    ("nvidia/primary/self-exit-95pct", "NVIDIA", [(SELF_EXIT, AT_RATIO)], "publish-warn", ["primary"]),
    ("nvidia/primary/self-exit-94pct", "NVIDIA", [(SELF_EXIT, BELOW_RATIO)], "cpu", ["primary"]),
    ("nvidia/primary/raw-sigint-no-frames", "NVIDIA", [(RAW_SIGINT, 0), (0, EXPECTED)], "cpu", ["primary"]),
    ("nvidia/primary/crash-no-frames", "NVIDIA", [(RAW_SIGSEGV, 0), (0, EXPECTED)], "publish", ["primary", "dv-safe"]),
    ("nvidia/primary/crash-99pct", "NVIDIA", [(RAW_SIGSEGV, NEARLY_ALL)], "publish-warn", ["primary"]),
    ("nvidia/primary/crash-94pct", "NVIDIA", [(RAW_SIGSEGV, BELOW_RATIO)], "cpu", ["primary"]),
    ("nvidia/dv-safe/exit-0", "NVIDIA", [(SELF_EXIT, 0), (0, EXPECTED)], "publish", ["primary", "dv-safe"]),
    ("nvidia/dv-safe/stall-kill", "NVIDIA", [(SELF_EXIT, 0), (STALL_KILLED, NEARLY_ALL)], "cpu", None),
    ("nvidia/dv-safe/io-error", "NVIDIA", [(SELF_EXIT, 0), (IO_ERROR, NEARLY_ALL)], "cpu", None),
    ("nvidia/dv-safe/self-exit-99pct", "NVIDIA", [(SELF_EXIT, 0), (SELF_EXIT, NEARLY_ALL)], "publish-warn", None),
    ("nvidia/dv-safe/self-exit-94pct", "NVIDIA", [(SELF_EXIT, 0), (SELF_EXIT, BELOW_RATIO)], "cpu", None),
    ("nvidia/dv-safe/raw-sigint", "NVIDIA", [(SELF_EXIT, 0), (RAW_SIGINT, NEARLY_ALL)], "cpu", None),
    ("nvidia/dv-safe/crash-99pct", "NVIDIA", [(SELF_EXIT, 0), (RAW_SIGSEGV, NEARLY_ALL)], "publish-warn", None),
    ("nvidia/dv-safe/crash-94pct", "NVIDIA", [(SELF_EXIT, 0), (RAW_SIGSEGV, BELOW_RATIO)], "cpu", None),
]
# AMD (VAAPI -> Vulkan) and Intel (VAAPI -> OpenCL) add a software-libplacebo tier after the hardware one.
for _vendor in ("AMD", "INTEL"):
    _v = _vendor.lower()
    DV_LADDER += [
        (f"{_v}/primary/stall-kill", _vendor, [(STALL_KILLED, NEARLY_ALL)], "cpu", ["primary"]),
        (f"{_v}/primary/io-error", _vendor, [(IO_ERROR, NEARLY_ALL)], "cpu", ["primary"]),
        (f"{_v}/primary/stall-kill-no-frames", _vendor, [(STALL_KILLED, 0), (0, EXPECTED)], "cpu", ["primary"]),
        (f"{_v}/primary/io-error-no-frames", _vendor, [(IO_ERROR, 0), (0, EXPECTED)], "cpu", ["primary"]),
        # Exit 251 with the GPU's own error in stderr is a hardware failure, not a disk: the software tier runs.
        (
            f"{_v}/primary/io-error-gpu-error-no-frames",
            _vendor,
            [(IO_ERROR, 0, HWACCEL_LINE), (0, EXPECTED)],
            "publish",
            ["primary", "sw-libplacebo"],
        ),
        (
            f"{_v}/primary/io-error-opencl-error-no-frames",
            _vendor,
            [(IO_ERROR, 0, OPENCL_LINE), (0, EXPECTED)],
            "publish",
            ["primary", "sw-libplacebo"],
        ),
        (
            f"{_v}/primary/io-error-gpu-error-99pct",
            _vendor,
            [(IO_ERROR, NEARLY_ALL, HWACCEL_LINE)],
            "publish-warn",
            None,
        ),
        (f"{_v}/primary/crash-no-frames", _vendor, [(RAW_SIGSEGV, 0), (0, EXPECTED)], "publish", None),
        (f"{_v}/primary/self-exit-99pct", _vendor, [(SELF_EXIT, NEARLY_ALL)], "publish-warn", ["primary"]),
        (f"{_v}/primary/self-exit-95pct", _vendor, [(SELF_EXIT, AT_RATIO)], "publish-warn", ["primary"]),
        (f"{_v}/primary/self-exit-94pct", _vendor, [(SELF_EXIT, BELOW_RATIO)], "cpu", ["primary"]),
        (f"{_v}/sw-libplacebo/exit-0", _vendor, [(SELF_EXIT, 0), (0, EXPECTED)], "publish", None),
        (f"{_v}/sw-libplacebo/stall-kill", _vendor, [(SELF_EXIT, 0), (STALL_KILLED, NEARLY_ALL)], "cpu", None),
        (f"{_v}/sw-libplacebo/io-error", _vendor, [(SELF_EXIT, 0), (IO_ERROR, NEARLY_ALL)], "cpu", None),
        (
            f"{_v}/sw-libplacebo/self-exit-99pct",
            _vendor,
            [(SELF_EXIT, 0), (SELF_EXIT, NEARLY_ALL)],
            "publish-warn",
            None,
        ),
        (f"{_v}/sw-libplacebo/self-exit-94pct", _vendor, [(SELF_EXIT, 0), (SELF_EXIT, BELOW_RATIO)], "cpu", None),
        (
            f"{_v}/dv-safe/self-exit-99pct",
            _vendor,
            [(SELF_EXIT, 0), (SELF_EXIT, 0), (SELF_EXIT, NEARLY_ALL)],
            "publish-warn",
            ["primary", "sw-libplacebo", "dv-safe"],
        ),
        (
            f"{_v}/dv-safe/stall-kill",
            _vendor,
            [(SELF_EXIT, 0), (SELF_EXIT, 0), (STALL_KILLED, NEARLY_ALL)],
            "cpu",
            ["primary", "sw-libplacebo", "dv-safe"],
        ),
    ]


def _default_tiers(gpu: str | None, attempts: list[tuple]) -> list[str]:
    ladder = ["primary", "sw-libplacebo", "dv-safe"] if gpu in ("AMD", "INTEL") else ["primary", "dv-safe"]
    return ladder[: len(attempts)]


class TestDolbyVisionLadder:
    """Filter-chain tiers retry runs that exited on their own having written nothing. A run that was killed (a signal,
    FFmpeg's 255 after one, the stall watchdog) or cut off by an I/O error skips them whatever it wrote, and so does a
    run whose frames were discarded: a stalled disk never becomes a full-length wrong-colour DV-safe preview, and a
    GPU run goes straight to the CPU."""

    @pytest.mark.parametrize(
        ("gpu", "attempts", "verdict", "tiers"),
        [pytest.param(gpu, a, v, t, id=i) for i, gpu, a, v, t in DV_LADDER],
    )
    def test_ladder(self, run, gpu, attempts, verdict, tiers):
        outcome = run(attempts, gpu=gpu, dv5=True)

        rc, n_frames = attempts[len(outcome.calls) - 1][:2]  # the last run that happened
        _check(outcome, verdict, rc, n_frames)
        assert outcome.tiers == (tiers or _default_tiers(gpu, attempts))
        for call in outcome.calls:
            assert call["use_skip"] is False, "DV5 RPU side-data can't survive keyframe-only decode"
            if _tier(call) == "sw-libplacebo":
                assert call["init_vulkan"] is True
                assert "libplacebo" in call["vf_override"]


class TestFallbackNamesItsCause:
    """A GPU run handed to the CPU says why in words the user can act on; a stall or an I/O error is never a codec,
    and nothing a disk did is answered with "remux the file"."""

    @pytest.mark.parametrize(
        ("attempts", "kind", "said"),
        [
            ([(SELF_EXIT, 0, "No decoder for codec hevc")], "codec", "codec"),
            ([(SELF_EXIT, BELOW_RATIO)], "stopped_part_way", "stopped part-way"),
            ([(IO_ERROR, 40)], "io_error", "I/O error"),
            ([(STALL_KILLED, 40, STALL_WATCHDOG_LINE)], "stall", "stopped making progress"),
            ([(-11, 40)], "signal", "signal"),
        ],
        ids=["codec", "stopped-part-way", "io-error", "stall", "signal"],
    )
    def test_the_gpu_hand_off_carries_its_cause(self, run, attempts, kind, said):
        outcome = run(attempts, gpu="NVIDIA")
        assert_handed_to_cpu(outcome)
        assert outcome.result.kind == kind
        assert said in str(outcome.result)
        assert not any("remux" in line for line in outcome.warnings)

    @pytest.mark.parametrize(
        ("rc", "stderr", "said"),
        [(STALL_KILLED, STALL_WATCHDOG_LINE, "stopped making progress"), (IO_ERROR, None, "I/O error")],
        ids=["stall", "io-error"],
    )
    def test_a_dv_safe_cpu_run_a_disk_stopped_never_says_remux(self, run, rc, stderr, said):
        final = (rc, NEARLY_ALL, stderr) if stderr else (rc, NEARLY_ALL)
        outcome = run([(SELF_EXIT, 0, "Error parsing Dolby Vision RPU"), final], dv5=True)
        assert_failed(outcome, rc)
        assert not any("remux" in line for line in outcome.warnings), outcome.warnings
        assert any(said in line for line in outcome.warnings), outcome.warnings

    @pytest.mark.parametrize(
        ("final", "kind"),
        [
            ((SELF_EXIT, 0), "hwaccel"),
            ((IO_ERROR, 0), "io_error"),
            ((STALL_KILLED, 0, STALL_WATCHDOG_LINE), "stall"),
            ((RAW_SIGSEGV, 0), "signal"),
            ((SELF_EXIT, 0, "No decoder for codec hevc"), "codec"),
        ],
        ids=["filter-chain", "io-error", "stall", "signal", "codec"],
    )
    def test_a_failed_dv_safe_gpu_run_hands_off_with_its_real_cause(self, run, final, kind):
        outcome = run([(SELF_EXIT, 0, "Error parsing Dolby Vision RPU"), final], gpu="NVIDIA", dv5=True)

        assert_handed_to_cpu(outcome)
        assert outcome.tiers == ["primary", "dv-safe"]
        assert outcome.result.kind == kind

    def test_a_failed_dv_safe_gpu_run_is_announced_as_the_gpus_decoder_or_filters(self, run):
        outcome = run([(SELF_EXIT, 0, "Error parsing Dolby Vision RPU"), (SELF_EXIT, 0)], gpu="NVIDIA", dv5=True)

        announced = gpu_fallback_announcement(outcome.result.kind, "movie.mkv")
        assert "decoder or filters" in announced and "movie.mkv" in announced
        assert "codec" not in announced


class TestUnchangedPaths:
    def test_cancel_mid_run_publishes_nothing(self, tmp_path):
        """Cancel terminates FFmpeg and raises; nothing reaches the publisher."""
        out = str(tmp_path / "frames")
        os.makedirs(out, exist_ok=True)

        def factory(**_kwargs):
            def _run(use_skip=False, init_vulkan=False, **kwargs):
                _write_frames(out, 10)
                raise CancellationError("Processing cancelled")

            return _run

        with (
            patch("media_preview_generator.processing.generator.MediaInfo") as mock_mediainfo,
            patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=None),
            patch("media_preview_generator.processing.ffmpeg_runner.create_ffmpeg_runner", factory),
            pytest.raises(CancellationError),
        ):
            mock_mediainfo.parse.return_value = MagicMock(video_tracks=[])
            generate_images(str(tmp_path / "movie.mkv"), out, None, None, MagicMock(plex_bif_frame_interval=10))

    @pytest.mark.parametrize("gpu", ["NVIDIA", None], ids=["gpu", "cpu"])
    def test_skip_frame_retry_still_runs_and_its_complete_result_is_published(self, run, gpu):
        """A fast keyframe-only pass that fails is retried with full decode on the same worker, as before."""
        outcome = run([(SELF_EXIT, 12), (0, EXPECTED)], gpu=gpu, keyframe_gap=1.0)

        assert_published(outcome, EXPECTED)
        assert [c["use_skip"] for c in outcome.calls] == [True, False]

    def test_gpu_partial_failure_with_unrecognised_error_hands_off_to_cpu(self, run):
        """Below the ratio the CPU gets its turn even when the error isn't a known GPU one."""
        outcome = run([(SELF_EXIT, 40, "Error reinitializing filters!")], gpu="NVIDIA")

        assert_handed_to_cpu(outcome)
        assert "GPU run stopped part-way" in str(outcome.result)
