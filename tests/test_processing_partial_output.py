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
* exit 0: published.

The Dolby Vision ladder (hardware DV5 -> software libplacebo -> DV-safe chain -> CPU) follows the same table:
the software-libplacebo and DV-safe tiers are for runs that ended on the file having written nothing (a filter chain
the GPU or file can't take), so an interrupted run, or one whose frames were discarded, goes straight to the CPU (GPU)
or fails (CPU).

Only the FFmpeg boundary is mocked; counting, cleanup, renaming and the cascade run for real against ``tmp_path``.
"""

import itertools
import os
import signal
import time
import uuid
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.processing import CancellationError, CodecNotSupportedError
from media_preview_generator.processing.ffmpeg_runner import STALL_WATCHDOG_LINE
from media_preview_generator.processing.generator import (
    FFMPEG_STALL_TIMEOUT_SEC,
    PUBLISH_MIN_FRAME_RATIO,
    _was_interrupted,
    clear_failures,
    failure_scope,
    generate_images,
    get_failures,
    gpu_fallback_announcement,
)

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

DEVICE_FOR = {None: None, "NVIDIA": "cuda:0", "AMD": "/dev/dri/renderD128", "INTEL": "/dev/dri/renderD128"}


@dataclass
class Outcome:
    result: object
    calls: list
    frames_left: list[str]
    failures: list[dict]
    warnings: list[str] = field(default_factory=list)

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


def _ffmpeg_script(attempts: list[tuple], output_folder: str, calls: list):
    """``create_ffmpeg_runner`` stand-in: attempt N writes ``attempts[N][1]`` distinct frames, exits ``[0]``
    and prints the optional ``[2]`` as stderr."""

    def factory(**_kwargs):
        def _run(use_skip=False, init_vulkan=False, **kwargs):
            rc, n_frames, *stderr = attempts[len(calls)]
            calls.append({"use_skip": use_skip, "init_vulkan": init_vulkan, **kwargs})
            _write_frames(output_folder, n_frames)
            return rc, 1.0, "10x", list(stderr)

        return _run

    return factory


@pytest.fixture
def run(mock_config, tmp_path):
    mock_config.plex_bif_frame_interval = INTERVAL_S

    def _generate(attempts, *, gpu=None, keyframe_gap=None, dv5=False, duration_s=DURATION_S, runner_factory=None):
        calls: list = []
        out = str(tmp_path / "frames")
        os.makedirs(out, exist_ok=True)
        # Dolby Vision Profile 5 declares no PQ/HLG transfer, so it takes the libplacebo path (hardware Vulkan).
        track = MagicMock(
            hdr_format="Dolby Vision" if dv5 else None,
            transfer_characteristics=None,
            duration=duration_s * 1000 if duration_s else None,
        )
        warnings: list[str] = []
        sink = logger.add(lambda m: warnings.append(str(m)), level="WARNING", format="{level}|{message}")
        job_id = f"partial-output-{uuid.uuid4()}"
        try:
            with (
                failure_scope(job_id),
                patch("media_preview_generator.processing.generator.MediaInfo") as mock_mediainfo,
                patch(
                    "media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=keyframe_gap
                ),
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
                except CodecNotSupportedError as exc:
                    result = exc
                failures = get_failures()
                clear_failures()
        finally:
            logger.remove(sink)
        frames_left = sorted(p for p in os.listdir(out) if p.endswith(".jpg"))
        return Outcome(result, calls, frames_left, failures, warnings)

    return _generate


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
    # A hand-off isn't a failed item: the CPU worker gets its turn.
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

    def test_a_251_with_a_gpu_error_blames_neither(self, run, tmp_path):
        outcome = self._run_real(run, tmp_path, IO_ERROR, HWACCEL_LINE)

        assert not any("working folder" in line for line in outcome.warnings), outcome.warnings
        assert outcome.result.kind == "hwaccel"

    @pytest.mark.parametrize("rc", [-signal.SIGKILL, 128 + signal.SIGKILL], ids=["raw-sigkill", "137"])
    def test_a_sigkill_is_put_down_to_the_out_of_memory_killer(self, run, tmp_path, rc):
        outcome = self._run_real(run, tmp_path, rc, gpu=None)

        assert _warned(outcome, "SIGKILL", "out of memory"), outcome.warnings
        assert not any("crashed" in line for line in outcome.warnings), outcome.warnings

    @pytest.mark.parametrize(
        "rc",
        [-signal.SIGSEGV, -signal.SIGABRT, -signal.SIGBUS, -signal.SIGFPE, -signal.SIGILL, 128 + signal.SIGSEGV],
        ids=["raw-sigsegv", "raw-sigabrt", "raw-sigbus", "raw-sigfpe", "raw-sigill", "139"],
    )
    def test_a_crash_signal_is_ffmpeg_crashing_on_the_file(self, run, tmp_path, rc):
        outcome = self._run_real(run, tmp_path, rc, gpu=None)

        assert _warned(outcome, "FFmpeg crashed on", "movie.mkv"), outcome.warnings
        assert not any("memory" in line.lower() for line in outcome.warnings), outcome.warnings

    @pytest.mark.parametrize(
        "rc",
        [-signal.SIGTERM, -signal.SIGINT, -signal.SIGHUP, 128 + signal.SIGTERM, 128 + signal.SIGHUP],
        ids=["raw-sigterm", "raw-sigint", "raw-sighup", "143", "129"],
    )
    def test_another_signal_says_it_was_stopped_from_outside(self, run, tmp_path, rc):
        outcome = self._run_real(run, tmp_path, rc, gpu=None)

        assert _warned(outcome, "FFmpeg was stopped by"), outcome.warnings
        assert not any("memory" in line.lower() or "crashed" in line for line in outcome.warnings), outcome.warnings


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
