"""The per-GPU decode check for credits (spec §5.4): once per device per process, logged only; it never moves the
decodes off the worker's GPU."""

from __future__ import annotations

import hashlib
import re
import threading
import time
import tomllib
from pathlib import Path

import numpy as np
import pytest

from media_preview_generator.markers.credits import decode_check, detector, frames

ROOT = Path(__file__).resolve().parents[3]
H264, HEVC = "h264-8bit.mkv", "hevc-10bit.mkv"
NVIDIA, INTEL = ("NVIDIA", "cuda:0"), ("INTEL", "/dev/dri/renderD128")
EVERY_DECODE = [(clip, scale) for clip in (H264, HEVC) for scale in (1, 2)]


def reference(clip: str, scale: int) -> tuple[tuple[float, str], ...]:
    """What a decode of one clip at one scale gives: a timestamp and a Y plane digest per frame."""
    return tuple((float(i), f"{clip}@{scale}#{i}") for i in range(9))


class FakeDecode:
    """Stands in for ffmpeg: every device decodes like the CPU unless told otherwise.

    Args:
        differ: (device, clip, scale) cells whose GPU frame 4 has other pixels than the CPU's; a GPU without a device
            path is named by its type.
        fail: (device, clip, scale) → the exception that decode raises; device None is the CPU.
        delay_s: How long each decode takes.
    """

    def __init__(self, *, differ=(), fail=None, delay_s=0.0):
        self.differ, self.fail, self.delay_s = set(differ), dict(fail or {}), delay_s
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, ffmpeg, clip, *, scale, gpu, gpu_device_path, cancel_check, timeout_s):
        with self._lock:
            self.calls.append({"ffmpeg": ffmpeg, "clip": clip.name, "scale": scale, "gpu": gpu,
                               "device": gpu_device_path, "cancel_check": cancel_check, "timeout_s": timeout_s})  # fmt: skip
        if self.delay_s:
            time.sleep(self.delay_s)
        cell = (None if gpu is None else gpu_device_path or gpu, clip.name, scale)
        if cell in self.fail:
            raise self.fail[cell]
        found = reference(clip.name, scale)
        if cell in self.differ:
            found = (*found[:4], (4.0, "other pixels"), *found[5:])
        return found

    def gpu_calls(self, device: str | None = None) -> list[tuple[str, int]]:
        return [(c["clip"], c["scale"]) for c in self.calls if c["gpu"] is not None and device in (None, c["device"])]

    def cpu_calls(self) -> list[tuple[str, int]]:
        return [(c["clip"], c["scale"]) for c in self.calls if c["gpu"] is None]


def warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def infos(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "INFO"]


MISMATCH = "Credits decoding on {device} doesn't match the reference decode: {where}: {reason}. Credits detection keeps decoding on this GPU."  # noqa: E501


class TestVerdict:
    def test_a_gpu_that_decodes_every_clip_like_the_cpu_logs_that_it_matches(self, loguru_caplog):
        decode = FakeDecode()
        cancel = lambda: False  # noqa: E731 — identity is asserted: the check must forward this exact callable
        checks = decode_check.DecodeChecks(decode=decode)
        assert checks.check_device(*NVIDIA, ffmpeg="/usr/bin/ffmpeg", cancel_check=cancel) is True
        # Both clips at both sizes, each on the GPU and on the CPU, through the worker's own device and ffmpeg.
        assert sorted(decode.gpu_calls()) == sorted(decode.cpu_calls()) == sorted(EVERY_DECODE)
        assert {(c["gpu"], c["device"]) for c in decode.calls if c["gpu"]} == {NVIDIA}
        assert {c["ffmpeg"] for c in decode.calls} == {"/usr/bin/ffmpeg"}
        assert all(c["cancel_check"] is cancel for c in decode.calls)
        (line,) = [m for m in infos(loguru_caplog) if m.startswith("Credits decoding on")]
        assert re.fullmatch(
            r"Credits decoding on cuda:0 matches the reference decode \(h264-8bit\.mkv and hevc-10bit\.mkv at "
            r"320x180 and 640x360; checked in \d+ ms\)",
            line,
        )
        assert warnings(loguru_caplog) == []

    @pytest.mark.parametrize(
        ("differ", "where"),
        [
            ({(H264, 1)}, "h264-8bit.mkv at 320x180"),
            ({(H264, 2)}, "h264-8bit.mkv at 640x360"),
            ({(HEVC, 1), (HEVC, 2)}, "hevc-10bit.mkv at 320x180"),
            # The last decode the check makes: a check that stopped early would miss it.
            ({(HEVC, 2)}, "hevc-10bit.mkv at 640x360"),
        ],
        ids=["320x180-only", "640x360-only", "10-bit-only", "last-decode-only"],
    )
    def test_any_frame_that_differs_is_one_warning_and_the_decodes_stay_on_the_gpu(self, loguru_caplog, differ, where):
        decode = FakeDecode(differ={("cuda:0", clip, scale) for clip, scale in differ})
        assert decode_check.DecodeChecks(decode=decode).check_device(*NVIDIA, ffmpeg="ffmpeg") is False
        assert warnings(loguru_caplog) == [
            MISMATCH.format(
                device="cuda:0", where=where, reason="1 of 9 frames differ from the CPU's (the first at 4.0 s)"
            )
        ]

    @pytest.mark.parametrize(
        ("gpu_frames", "reason"),
        [
            (reference(H264, 1)[:8], "the GPU gave 8 frames, the CPU 9"),
            (((0.5, f"{H264}@1#0"), *reference(H264, 1)[1:]), "frame 1 of 9 is at 0.5 s on the GPU, 0.0 s on the CPU"),
        ],
        ids=["frame-count", "timestamp"],
    )
    def test_frames_missing_or_at_other_times_are_a_mismatch(self, loguru_caplog, gpu_frames, reason):
        def decode(ffmpeg, clip, *, scale, gpu, **kwargs):
            return gpu_frames if gpu and (clip.name, scale) == (H264, 1) else reference(clip.name, scale)

        assert decode_check.DecodeChecks(decode=decode).check_device(*NVIDIA, ffmpeg="ffmpeg") is False
        assert warnings(loguru_caplog) == [
            MISMATCH.format(device="cuda:0", where="h264-8bit.mkv at 320x180", reason=reason)
        ]

    @pytest.mark.parametrize(
        ("cell", "error", "reason"),
        [
            (("cuda:0", HEVC, 1), frames.GpuDecodeError("ffmpeg exited 1 decoding hevc-10bit.mkv on the GPU: boom"),
             "the GPU couldn't decode it: ffmpeg exited 1 decoding hevc-10bit.mkv on the GPU: boom"),
            (("cuda:0", H264, 2), frames.FrameDecodeError("could not run ffmpeg for h264-8bit.mkv: gone"),
             "the GPU couldn't decode it: could not run ffmpeg for h264-8bit.mkv: gone"),
            (("cuda:0", H264, 1), RuntimeError("anything else"), "the GPU couldn't decode it: anything else"),
            ((None, HEVC, 2), frames.FrameDecodeError("ffmpeg exited 1 decoding hevc-10bit.mkv on the CPU: no"),
             "the CPU couldn't decode it to compare: ffmpeg exited 1 decoding hevc-10bit.mkv on the CPU: no"),
        ],
        ids=["gpu-decode-error", "ffmpeg-wont-start", "unexpected-error", "cpu-reference-error"],
    )  # fmt: skip
    def test_a_decode_error_is_one_warning(self, loguru_caplog, cell, error, reason):
        decode = FakeDecode(fail={cell: error})
        assert decode_check.DecodeChecks(decode=decode).check_device(*NVIDIA, ffmpeg="ffmpeg") is False
        size = "320x180" if cell[2] == 1 else "640x360"
        assert warnings(loguru_caplog) == [
            MISMATCH.format(device="cuda:0", where=f"{cell[1]} at {size}", reason=reason)
        ]

    @pytest.mark.parametrize("device", ["cuda:0", None], ids=["gpu-decode", "cpu-reference"])
    def test_a_decode_that_times_out_is_one_warning_for_the_process(self, loguru_caplog, device):
        decode = FakeDecode(fail={(device, H264, 1): frames.DecodeTimeoutError("decoding h264-8bit.mkv timed out")})
        checks = decode_check.DecodeChecks(decode=decode)
        assert checks.check_device(*NVIDIA, ffmpeg="ffmpeg") is False
        assert warnings(loguru_caplog) == [
            MISMATCH.format(
                device="cuda:0",
                where="h264-8bit.mkv at 320x180",
                reason="the check timed out: decoding h264-8bit.mkv timed out",
            )
        ]
        # Kept for the rest of the process: the next file doesn't run the check again or warn again.
        before = len(decode.calls)
        assert checks.check_device(*NVIDIA, ffmpeg="ffmpeg") is False
        assert len(decode.calls) == before
        assert len(warnings(loguru_caplog)) == 1

    def test_every_decode_gets_what_is_left_of_one_time_limit_and_none_starts_past_it(self, loguru_caplog):
        now = [100.0]

        def decode(ffmpeg, clip, *, scale, gpu, timeout_s, **kwargs):
            limits.append(timeout_s)
            now[0] += 7.0  # each decode takes 7 s of the check's 20
            return reference(clip.name, scale)

        limits: list[float] = []
        checks = decode_check.DecodeChecks(decode=decode, timeout_s=20.0, clock=lambda: now[0])
        assert checks.check_device(*NVIDIA, ffmpeg="ffmpeg") is False
        # CPU then GPU for the first clip at 320x180, then the CPU reference at 640x360; the GPU decode after that
        # would start past the limit.
        assert limits == [20.0, 13.0, 6.0]
        assert warnings(loguru_caplog) == [
            MISMATCH.format(
                device="cuda:0", where="h264-8bit.mkv at 640x360", reason="the check timed out: it ran past 20 s"
            )
        ]

    @pytest.mark.parametrize(("gpu", "device"), [(None, None), ("INTEL", None), ("AMD", "")])
    def test_a_worker_whose_decodes_never_reach_a_gpu_runs_no_check(self, loguru_caplog, gpu, device):
        # A CPU worker, and a GPU worker without a usable device: frames.decode_command gives either the CPU command.
        decode = FakeDecode()
        assert decode_check.DecodeChecks(decode=decode).check_device(gpu, device, ffmpeg="ffmpeg") is None
        assert decode.calls == []
        assert not [m for m in loguru_caplog.messages if m.startswith("Credits decoding")]

    @pytest.mark.parametrize("gpu", ["APPLE", "WINDOWS_GPU"])
    @pytest.mark.parametrize("matches", [True, False])
    def test_a_gpu_whose_frames_ffmpeg_downloads_itself_is_checked_too(self, gpu, matches):
        # Their frames never stay surfaces (frames._SURFACE_VENDORS), but they are still decoded on the GPU.
        decode = FakeDecode(differ=set() if matches else {(gpu, H264, 1)})
        assert decode_check.DecodeChecks(decode=decode).check_device(gpu, None, ffmpeg="ffmpeg") is matches
        assert decode.gpu_calls()[0] == (H264, 1)

    def test_two_devices_are_checked_independently(self, loguru_caplog):
        decode = FakeDecode(differ={("/dev/dri/renderD128", HEVC, 2)})
        checks = decode_check.DecodeChecks(decode=decode)
        for _ in range(2):
            assert checks.check_device(*INTEL, ffmpeg="ffmpeg") is False
            assert checks.check_device(*NVIDIA, ffmpeg="ffmpeg") is True
        assert sorted(decode.gpu_calls("cuda:0")) == sorted(decode.gpu_calls(INTEL[1])) == sorted(EVERY_DECODE)
        # The CPU's side of the comparison is decoded once for the process, not once per device.
        assert sorted(decode.cpu_calls()) == sorted(EVERY_DECODE)
        assert [m.split(":")[0] for m in warnings(loguru_caplog)] == [
            "Credits decoding on /dev/dri/renderD128 doesn't match the reference decode"
        ]
        assert [m for m in infos(loguru_caplog) if m.startswith("Credits decoding on cuda:0 matches")]

    def test_the_same_device_through_another_ffmpeg_is_checked_again(self):
        decode = FakeDecode()
        checks = decode_check.DecodeChecks(decode=decode)
        checks.check_device(*NVIDIA, ffmpeg="/usr/bin/ffmpeg")
        checks.check_device(*NVIDIA, ffmpeg="/usr/lib/jellyfin-ffmpeg/ffmpeg")
        assert len(decode.gpu_calls()) == 2 * len(EVERY_DECODE)


class TestConcurrency:
    def test_workers_arriving_while_a_device_is_checked_decode_without_waiting_for_it(self, loguru_caplog):
        # A diagnostic holds nobody up: only the first worker on a device runs the check.
        running, release = threading.Event(), threading.Event()

        def decode(ffmpeg, clip, *, scale, **kwargs):
            running.set()
            assert release.wait(5)
            return reference(clip.name, scale)

        checks = decode_check.DecodeChecks(decode=decode)
        answers: list = []
        first = threading.Thread(target=lambda: answers.append(checks.check_device(*NVIDIA, ffmpeg="ffmpeg")))
        first.start()
        assert running.wait(5)

        assert [checks.check_device(*NVIDIA, ffmpeg="ffmpeg") for _ in range(7)] == [None] * 7

        release.set()
        first.join(5)
        assert answers == [True]
        assert checks.check_device(*NVIDIA, ffmpeg="ffmpeg") is True  # the kept result, not a second check
        assert len([m for m in loguru_caplog.messages if m.startswith("Credits decoding on cuda:0")]) == 1

    def test_two_devices_at_first_use_share_one_cpu_reference(self):
        decode = FakeDecode(delay_s=0.02)
        checks = decode_check.DecodeChecks(decode=decode)
        start = threading.Barrier(2)
        answers: dict = {}

        def worker(device):
            start.wait()
            answers[device] = checks.check_device(*device, ffmpeg="ffmpeg")

        threads = [threading.Thread(target=worker, args=(device,)) for device in (NVIDIA, INTEL)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert answers == {NVIDIA: True, INTEL: True}
        assert sorted(decode.cpu_calls()) == sorted(EVERY_DECODE)

    @pytest.mark.parametrize("cell", [("cuda:0", HEVC, 2), (None, H264, 1)], ids=["gpu-decode", "cpu-reference"])
    def test_a_cancelled_check_is_not_kept_and_the_next_file_checks_again(self, loguru_caplog, cell):
        decode = FakeDecode(fail={cell: frames.DecodeCancelledError("cancelled while decoding")})
        checks = decode_check.DecodeChecks(decode=decode)
        with pytest.raises(frames.DecodeCancelledError):
            checks.check_device(*NVIDIA, ffmpeg="ffmpeg")
        assert not [m for m in loguru_caplog.messages if m.startswith("Credits decoding")]
        decode.fail.clear()
        assert checks.check_device(*NVIDIA, ffmpeg="ffmpeg") is True
        # Checked again from the start, after the cancel.
        assert decode.gpu_calls()[-len(EVERY_DECODE) :] == EVERY_DECODE

    def test_a_worker_whose_job_is_cancelled_while_another_device_decodes_the_cpu_side_leaves_at_once(self):
        running, release, cancelled = threading.Event(), threading.Event(), threading.Event()

        def decode(ffmpeg, clip, *, scale, gpu, **kwargs):
            if gpu is None and not running.is_set():
                running.set()
                release.wait(5)
            return reference(clip.name, scale)

        checks = decode_check.DecodeChecks(decode=decode)
        answers: dict = {}

        def check(device, cancel_check=None):
            try:
                answers[device] = checks.check_device(*device, ffmpeg="ffmpeg", cancel_check=cancel_check)
            except frames.DecodeCancelledError as exc:
                answers[device] = str(exc)

        first = threading.Thread(target=check, args=(NVIDIA,))
        first.start()
        assert running.wait(5)
        second = threading.Thread(target=check, args=(INTEL, cancelled.is_set))
        second.start()
        time.sleep(0.05)
        cancelled.set()
        second.join(2)
        assert not second.is_alive() and first.is_alive()
        assert answers == {INTEL: "cancelled while waiting for the CPU's decode of h264-8bit.mkv"}
        release.set()
        first.join(5)
        assert answers[NVIDIA] is True
        # Nothing was kept for the cancelled device: its next file checks it, and it passes.
        assert checks.check_device(*INTEL, ffmpeg="ffmpeg") is True


class TestTheProcessWideChecks:
    def test_the_module_function_answers_from_the_processs_one_set_of_checks(self, monkeypatch):
        decode = FakeDecode()
        monkeypatch.setattr(decode_check, "_checks", decode_check.DecodeChecks(decode=decode))
        cancel = lambda: False  # noqa: E731
        for _ in range(2):
            assert decode_check.check_device(*NVIDIA, ffmpeg="/ff", cancel_check=cancel) is True
        assert sorted(decode.gpu_calls()) == sorted(EVERY_DECODE)  # once for the process, not per file
        assert all(c["cancel_check"] is cancel and c["ffmpeg"] == "/ff" for c in decode.calls)


class TestProductionDecode:
    """``decode_clip``: the reference clips through ``frames.decode_rows``, the credits decode's own path."""

    @pytest.fixture
    def commands(self, monkeypatch):
        seen: list[dict] = []

        def run_decode(command, **kwargs):
            seen.append({"command": command, **kwargs})
            size = frames.FRAME_H * kwargs["scale"], frames.FRAME_W * kwargs["scale"]
            planes = np.stack([np.full(size, value, np.uint8) for value in (16, 235)])
            kwargs["detect_boxes"](planes)
            return [(0.0, 0, 16.0, ()), (1.0, 0, 235.0, ())]

        monkeypatch.setattr(frames, "run_decode", run_decode)
        monkeypatch.setattr(frames, "container_start_s", lambda *a, **k: pytest.fail("probed a packaged clip"))
        return seen

    @pytest.mark.parametrize(
        ("gpu", "device", "clip", "scale", "hwaccel", "video_filter"),
        [
            ("NVIDIA", "cuda:1", 0, 1, ["-hwaccel", "cuda", "-hwaccel_device", "1", "-hwaccel_output_format", "cuda"],
             "fps=1,hwdownload,format=nv12,scale=320:180:flags=neighbor,format=nv12,showinfo"),
            ("NVIDIA", "cuda:1", 1, 2, ["-hwaccel", "cuda", "-hwaccel_device", "1", "-hwaccel_output_format", "cuda"],
             "fps=1,hwdownload,format=p010le,scale=640:360:flags=neighbor,format=nv12,showinfo"),
            ("INTEL", "/dev/dri/renderD129", 1, 1,
             ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129", "-hwaccel_output_format", "vaapi",
              "-extra_hw_frames", "8"],
             "fps=1,hwdownload,format=p010le,scale=320:180:flags=neighbor,format=nv12,showinfo"),
            ("AMD", "/dev/dri/renderD128", 0, 2,
             ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi",
              "-extra_hw_frames", "8"],
             "fps=1,hwdownload,format=nv12,scale=640:360:flags=neighbor,format=nv12,showinfo"),
            ("APPLE", None, 0, 1, ["-hwaccel", "videotoolbox"],
             "fps=1,scale=320:180:flags=neighbor,format=nv12,showinfo"),
            (None, None, 1, 2, [], "fps=1,scale=640:360:flags=neighbor,format=nv12,showinfo"),
        ],
        ids=["cuda-8bit-320", "cuda-10bit-640", "vaapi-intel-10bit-320", "vaapi-amd-8bit-640", "videotoolbox", "cpu"],
    )  # fmt: skip
    def test_each_clip_is_decoded_with_the_credits_decodes_own_command(
        self, commands, gpu, device, clip, scale, hwaccel, video_filter
    ):
        ref = decode_check.REFERENCE_CLIPS[clip]
        cancel = lambda: False  # noqa: E731
        decode_check.decode_clip("/usr/bin/ffmpeg", ref, scale=scale, gpu=gpu, gpu_device_path=device,
                                 cancel_check=cancel, timeout_s=12.5)  # fmt: skip
        (call,) = commands
        assert call["command"] == [
            "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", *hwaccel,
            "-ss", "0.000", "-copyts", "-i", ref.path, "-an", "-sn", "-dn", "-fps_mode", "passthrough",
            "-vf", video_filter, "-f", "rawvideo", "-",
        ]  # fmt: skip
        assert (call["hw_active"], call["scale"], call["timeout_s"], call["pts_offset_s"]) == (
            gpu is not None,
            scale,
            12.5,
            0.0,
        )
        assert call["cancel_check"] is cancel

    def test_each_frame_is_its_timestamp_and_its_y_planes_sha256(self, commands):
        found = decode_check.decode_clip("ffmpeg", decode_check.REFERENCE_CLIPS[0], scale=1, gpu=None,
                                         gpu_device_path=None, cancel_check=None, timeout_s=5.0)  # fmt: skip
        digest = [hashlib.sha256(np.full((180, 320), value, np.uint8).tobytes()).hexdigest() for value in (16, 235)]
        assert found == ((0.0, digest[0]), (1.0, digest[1]))


class TestReferenceClips:
    def test_the_clips_are_packaged_and_small(self):
        assert [clip.name for clip in decode_check.REFERENCE_CLIPS] == [H264, HEVC]
        assert [clip.download_format for clip in decode_check.REFERENCE_CLIPS] == ["nv12", "p010le"]
        sizes = [Path(clip.path).stat().st_size for clip in decode_check.REFERENCE_CLIPS]
        assert all(sizes) and sum(sizes) < 200_000
        # In the wheel: package data, not left to whether the build can see git.
        package_data = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
        package = ROOT / "media_preview_generator" / "markers" / "credits"
        packaged = {
            p for pattern in package_data["media_preview_generator.markers.credits"] for p in package.glob(pattern)
        }
        assert {Path(clip.path) for clip in decode_check.REFERENCE_CLIPS} <= packaged
        # In the image: the runtime stage copies the package folder whole, and .dockerignore leaves the clips in.
        final_stage = re.split(r"(?m)^FROM ", (ROOT / "Dockerfile").read_text())[-1]
        assert "COPY media_preview_generator/ ./media_preview_generator/" in final_stage
        ignored = [line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()]
        assert not any(line.endswith((".mkv", "reference_clips/", "reference_clips")) for line in ignored)

    def test_the_sizes_are_the_ones_credits_are_read_at(self):
        assert decode_check.SCALES == (1, detector.RETRY_SCALE)
