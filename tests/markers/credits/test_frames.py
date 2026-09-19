"""Frame decode for credit text: the argv per worker, streaming, pts pairing, failures, cancel and timeout.

``run_decode`` runs a real child process (a small Python script standing in for ffmpeg), so pipes, the reader thread
and kills are exercised for real.
"""

from __future__ import annotations

import io
import os
import pathlib
import queue
import signal
import sys
import textwrap
import threading
import time

import numpy as np
import pytest
from loguru import logger

from media_preview_generator.markers.credits import frames
from media_preview_generator.markers.credits.frames import (
    DecodeCancelledError,
    DecodeTimeoutError,
    FrameDecodeError,
    GpuDecodeError,
    KeyframeThinning,
)
from media_preview_generator.markers.probe import (
    MediaProbe,
    ProbeError,
    ProbeStalledError,
    ProbeTimeoutError,
    VideoPacket,
    VideoPackets,
)

FF = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
MOVIE = "/media/Movie (2020)/Movie.mkv"
TAIL = ["-an", "-sn", "-dn", "-fps_mode", "passthrough"]
RENDER = "/dev/dri/renderD128"


class TestCommand:
    def test_nvidia_keyframes_of_the_tail(self):
        cmd, hw = frames.decode_command(
            FF,
            MOVIE,
            start_s=5100.0,
            length_s=None,
            keyframes_only=True,
            fps=None,
            gpu="NVIDIA",
            gpu_device_path="cuda:1",
        )
        assert hw is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
                       "-hwaccel", "cuda", "-hwaccel_device", "1", "-hwaccel_output_format", "cuda",
                       "-skip_frame", "nokey", "-ss", "5100.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "scale_cuda=320:180:format=nv12,hwdownload,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    def test_vaapi_refine_window_at_1_fps(self):
        cmd, hw = frames.decode_command(
            FF,
            MOVIE,
            start_s=5680.5,
            length_s=21.0,
            keyframes_only=False,
            fps=1,
            gpu="INTEL",
            gpu_device_path="/dev/dri/renderD128",
        )
        assert hw is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
                       "-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi",
                       "-ss", "5680.500", "-t", "21.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "fps=1,scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(
        ("gpu", "device", "hw_args", "hw"),
        [
            (None, None, [], False),
            ("INTEL", None, [], False),
            ("APPLE", "videotoolbox", ["-hwaccel", "videotoolbox"], True),
            ("WINDOWS_GPU", "d3d11va", ["-hwaccel", "d3d11va"], True),
            # Only NVIDIA, INTEL and AMD have a GPU scale filter here: other VAAPI nodes decode on the GPU and download
            # each frame (no -hwaccel_output_format), or the software scale would fail on GPU surfaces.
            ("ARM", RENDER, ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
            ("VIDEOCORE", RENDER, ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
            ("UNKNOWN", RENDER, ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
        ],
    )
    def test_software_scaling_cells(self, gpu, device, hw_args, hw):
        cmd, active = frames.decode_command(
            FF, MOVIE, start_s=0.0, length_s=None, keyframes_only=True, fps=None, gpu=gpu, gpu_device_path=device
        )
        assert active is hw
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", *hw_args,
                       "-skip_frame", "nokey", "-ss", "0.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "scale=320:180,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(
        ("gpu", "device", "hw_args", "scale"),
        [
            ("AMD", "/dev/dri/renderD129", ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129", "-hwaccel_output_format", "vaapi"], "scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12"),
            ("NVIDIA", None, ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"], "scale_cuda=320:180:format=nv12,hwdownload,format=nv12"),
        ],
    )  # fmt: skip
    def test_gpu_scaling_cells(self, gpu, device, hw_args, scale):
        cmd, active = frames.decode_command(
            FF, MOVIE, start_s=12.25, length_s=None, keyframes_only=True, fps=None, gpu=gpu, gpu_device_path=device
        )
        assert active is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", *hw_args,
                       "-skip_frame", "nokey", "-ss", "12.250", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", f"{scale},showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    def test_no_thinning_is_the_spec_command_exactly(self):
        # What every file that is neither intra-only nor VP9 gets (the detector passes keep_every=None and
        # drop_non_key=False): the command the 80 and the 205 were measured with, byte for byte.
        cmd, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None,
                                       gpu="NVIDIA", gpu_device_path="cuda:1", keep_every=None, drop_non_key=False)  # fmt: skip
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2",
                       "-hwaccel", "cuda", "-hwaccel_device", "1", "-hwaccel_output_format", "cuda",
                       "-skip_frame", "nokey", "-ss", "5100.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "scale_cuda=320:180:format=nv12,hwdownload,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(
        ("gpu", "device", "hw_args", "scale"),
        [
            (None, None, [], "scale=320:180,format=nv12"),
            ("NVIDIA", "cuda:0", ["-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"], "scale_cuda=320:180:format=nv12,hwdownload,format=nv12"),
            ("INTEL", RENDER, ["-hwaccel", "vaapi", "-hwaccel_device", RENDER, "-hwaccel_output_format", "vaapi"], "scale_vaapi=w=320:h=180:format=nv12,hwdownload,format=nv12"),
        ],
    )  # fmt: skip
    def test_an_intra_only_keyframe_pass_drops_packets_before_the_decoder(self, gpu, device, hw_args, scale):
        # An input option (before -i): as a filter after the decoder, every frame would still be decoded. V:0 is the
        # stream the stride was measured on (ffmpeg may decode another). The comma is escaped for ffmpeg's bitstream
        # filter list, not for a shell: argv never goes through one.
        cmd, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None,
                                       gpu=gpu, gpu_device_path=device, keep_every=48)  # fmt: skip
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", *hw_args,
                       "-bsf:V:0", "noise=drop=mod(n\\,48)", "-skip_frame", "nokey", "-ss", "5100.000", "-copyts",
                       "-i", MOVIE, *TAIL, "-vf", f"{scale},showinfo", "-f", "rawvideo", "-"]  # fmt: skip
        assert cmd[cmd.index("-bsf:V:0") + 1] == r"noise=drop=mod(n\,48)"

    # One -bsf:V:0 per command: a second would replace the first. noise drops a packet whose expression is non-zero,
    # and both terms are 0 or more, so the sum drops what either term drops.
    PACKET_DROPS = {
        (None, False): None,
        (48, False): r"noise=drop=mod(n\,48)",
        (None, True): "noise=drop=not(key)",
        (48, True): r"noise=drop=not(key)+mod(n\,48)",
    }

    @pytest.mark.parametrize(("keep_every", "drop_non_key"), list(PACKET_DROPS), ids=["plain", "intra-only", "vp9",
                                                                                       "vp9-intra-only"])  # fmt: skip
    @pytest.mark.parametrize(
        ("gpu", "device", "hw_args", "scale"),
        [
            (None, None, [], "scale=320:180,format=nv12"),
            ("NVIDIA", "cuda:0", ["-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"], "scale_cuda=320:180:format=nv12,hwdownload,format=nv12"),
        ],
        ids=["cpu", "cuda"],
    )  # fmt: skip
    @pytest.mark.parametrize("keyframe_pass", [True, False], ids=["keyframe-pass", "refine"])
    def test_every_packet_drop_cell_builds_its_own_argv(self, keep_every, drop_non_key, gpu, device, hw_args, scale,
                                                        keyframe_pass):  # fmt: skip
        # The whole matrix: VP9 or not × intra-only or not × CPU or GPU × keyframe pass or 1 fps refine. The detector
        # never passes a drop to a refine decode (test_detector); these refine rows pin that the command itself adds
        # nothing it wasn't asked for.
        window = (
            {"start_s": 5100.0, "length_s": None, "keyframes_only": True, "fps": None}
            if keyframe_pass
            else {"start_s": 5680.0, "length_s": 21.0, "keyframes_only": False, "fps": 1}
        )
        cmd, _ = frames.decode_command(FF, MOVIE, **window, gpu=gpu, gpu_device_path=device, keep_every=keep_every,
                                       drop_non_key=drop_non_key)  # fmt: skip
        bsf = self.PACKET_DROPS[(keep_every, drop_non_key)]
        seek = ["-skip_frame", "nokey", "-ss", "5100.000"] if keyframe_pass else ["-ss", "5680.000", "-t", "21.000"]
        video_filter = f"{scale},showinfo" if keyframe_pass else f"fps=1,{scale},showinfo"
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", *hw_args,
                       *(["-bsf:V:0", bsf] if bsf else []), *seek, "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", video_filter, "-f", "rawvideo", "-"]  # fmt: skip
        assert cmd.count("-bsf:V:0") == (1 if bsf else 0)

    @pytest.mark.parametrize(
        ("duration_ms", "episode", "expected"),
        [(6_000_000, False, 5100.0), (1_320_000, True, 870.0), (300_000, True, 0.0), (600_000, False, 0.0)],
    )
    def test_tail_start(self, duration_ms, episode, expected):
        assert frames.tail_start_s(duration_ms, is_episode=episode) == expected


def _fake_ffmpeg(
    frame_values: list[int],
    pts: list[str],
    *,
    exit_code: int = 0,
    sleep_s: float = 0.0,
    extra_bytes: int = 0,
    pid_file: str = "",
    child_pid_file: str = "",
    close_stdout: bool = False,
    linger_s: float = 0.0,
    ignore_sigterm: bool = False,
    progress_file: str = "",
) -> list[str]:
    """A child that writes NV12 frames (Y plane filled with each value) to stdout and showinfo lines to stderr.

    ``pts`` entries past the frames become showinfo lines with no whole frame behind them (ffmpeg dying mid-write);
    ``child_pid_file`` spawns a grandchild in the same process group, so the group kill can be asserted;
    ``ignore_sigterm`` stands in for an ffmpeg that won't take a polite signal; ``progress_file`` records how many
    frames have been written, so how far the decoder ran ahead of text detection can be read.
    """
    script = textwrap.dedent(f"""
        import os, signal, subprocess, sys, time
        if {ignore_sigterm!r}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if {pid_file!r}:
            open({pid_file!r}, "w").write(str(os.getpid()))
        if {child_pid_file!r}:
            child = subprocess.Popen(["sleep", "30"])
            open({child_pid_file!r}, "w").write(str(child.pid))
        out = sys.stdout.buffer
        pts = {pts!r}

        def line(i):
            sys.stderr.write("[Parsed_showinfo_3 @ 0x1] n:%d pts:%d pts_time:%-7s duration:1\\n" % (i, i, pts[i]))

        def progress(n):
            if {progress_file!r}:
                open({progress_file!r} + ".tmp", "w").write(str(n))
                os.replace({progress_file!r} + ".tmp", {progress_file!r})

        for i, value in enumerate({frame_values!r}):
            if i < len(pts):
                line(i)
            out.write(bytes([value]) * (320 * 180) + bytes([128]) * (320 * 90))
            out.flush()
            progress(i + 1)
            time.sleep({sleep_s})
        for i in range(len({frame_values!r}), len(pts)):
            line(i)
        out.write(b"x" * {extra_bytes})
        out.flush()
        if {close_stdout!r}:
            os.close(1)
        time.sleep({linger_s})
        sys.exit({exit_code})
    """)
    return [sys.executable, "-c", script]


def _counter(calls: list[np.ndarray]):
    def count(planes: np.ndarray) -> list[int]:
        calls.append(planes.copy())
        return [int(p[0, 0] > 200) * 3 for p in planes]

    return count


def _gone(pid: int) -> bool:
    """The process is dead: no ``/proc`` entry, or a zombie nobody has reaped yet (a pid can be reused, a state can't)."""
    try:
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split(" ", 1)[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return True
    return state == "Z"


def _assert_gone(pid_file, *, within_s: float = 0.0) -> None:
    """The process was killed by the time ``run_decode`` returned (a group kill reaches a grandchild a moment later)."""
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + within_s
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(pid), f"pid {pid} is still running"


def _reapers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "credits-frames-reaper"]


def _readers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "credits-frames"]


def _wait_for(condition, *, within_s: float) -> bool:
    deadline = time.monotonic() + within_s
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)
    return condition()


class TestReadFrames:
    def test_the_end_marker_waits_for_room_however_slow_the_consumer_is(self):
        # Directly pins the reader's half of the end-of-stream contract: with a queue too small for the stream and a
        # consumer that takes longer than any put timeout, every frame and the end marker still arrive.
        stream = io.BytesIO(b"".join(bytes([value]) * (320 * 180 * 3 // 2) for value in (1, 2, 3)))
        items: queue.Queue = queue.Queue(maxsize=1)
        stop = threading.Event()
        reader = threading.Thread(target=frames._read_frames, args=(stream, items, stop), daemon=True)
        reader.start()
        drained = []
        try:
            for _ in range(4):
                time.sleep(1.5)  # the queue is full each time the reader has more to put, the end marker included
                drained.append(items.get(timeout=5))
        finally:
            stop.set()
        assert [d[0] for d in drained[:3]] == [1, 2, 3]
        assert drained[3] is frames._END
        reader.join(timeout=5)
        assert not reader.is_alive()


class TestRunDecode:
    def test_rows_pair_boxes_luma_and_pts_in_decode_order(self):
        calls: list[np.ndarray] = []
        values = [10, 250, 250, 120, 5]
        pts = ["5100.1234", "5102.5", "5101.9", "5104", "5106.0005"]
        before = _reapers()
        rows = frames.run_decode(
            _fake_ffmpeg(values, pts), hw_active=False, pts_offset_s=0.0, count_boxes=_counter(calls), chunk_frames=2
        )
        assert rows == [
            (5100.123, 0, 10.0),
            (5102.5, 3, 250.0),
            (5101.9, 3, 250.0),
            (5104.0, 0, 120.0),
            (5106.001, 0, 5.0),
        ]
        assert [c.shape for c in calls] == [(2, 180, 320), (2, 180, 320), (1, 180, 320)]
        assert calls[0].dtype == np.uint8 and int(calls[0][1, 5, 5]) == 250  # the Y plane, not the chroma
        assert _reapers() == before  # a decode that ended cleanly closes its own pipe, no reaper

    def test_luma_is_the_mean_rounded_to_a_tenth(self):
        # 36 % of the pixels at 34 and the rest at 33 average 33.36: only rounding to a tenth gives 33.4 (a uniform
        # plane's mean is already whole, so it can't tell).
        script = textwrap.dedent("""
            import sys
            sys.stderr.write("[Parsed_showinfo_3 @ 0x1] n:0 pts:0 pts_time:1 duration:1\\n")
            sys.stdout.buffer.write(bytes([34]) * 20736 + bytes([33]) * (57600 - 20736) + bytes([128]) * 28800)
        """)
        rows = frames.run_decode(
            [sys.executable, "-c", script], hw_active=False, pts_offset_s=0.0, count_boxes=lambda p: [0]
        )
        assert rows == [(1.0, 0, 33.4)]

    def test_a_frame_without_a_timestamp_drops_its_own_row_only(self):
        # A NOPTS line in the middle: the frames after it must keep their own timestamps. Shifting them would move a
        # box count onto an earlier second and store a credits start there.
        calls: list[np.ndarray] = []
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250, 250], ["100", "NOPTS", "106"]),
            hw_active=False,
            pts_offset_s=0.0,
            count_boxes=_counter(calls),
            chunk_frames=1,
        )
        assert rows == [(100.0, 0, 10.0), (106.0, 3, 250.0)]

    def test_several_frames_without_timestamps_drop_only_their_own_rows(self):
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250, 250, 250, 5], ["100", "NOPTS", "104", "NOPTS", "108"]),
            hw_active=False,
            pts_offset_s=0.0,
            count_boxes=_counter([]),
            chunk_frames=2,
        )
        assert rows == [(100.0, 0, 10.0), (104.0, 3, 250.0), (108.0, 0, 5.0)]

    def test_a_partial_trailing_frame_is_ignored(self):
        rows = frames.run_decode(
            _fake_ffmpeg([10], ["1"], extra_bytes=1000),
            hw_active=False,
            pts_offset_s=0.0,
            count_boxes=lambda p: [0] * len(p),
        )
        assert rows == [(1.0, 0, 10.0)]

    @pytest.mark.parametrize(
        ("values", "pts", "extra", "message"),
        [
            ([10], ["1", "2"], 1000, "2 timestamps for 1 frames"),  # a showinfo line, then a half-written frame
            ([10, 10, 10], ["1", "2"], 0, "2 timestamps for 3 frames"),
        ],
    )
    def test_a_clean_exit_with_a_timestamp_missing_is_a_decode_error(self, values, pts, extra, message):
        # Pairing what's left would time later frames from earlier lines, and the wrong answer would be stored as good.
        with pytest.raises(FrameDecodeError, match=message) as excinfo:
            frames.run_decode(
                _fake_ffmpeg(values, pts, extra_bytes=extra),
                hw_active=False,
                pts_offset_s=0.0,
                count_boxes=lambda p: [0] * len(p),
            )
        assert type(excinfo.value) is FrameDecodeError

    @pytest.mark.parametrize(("hw", "error"), [(True, GpuDecodeError), (False, FrameDecodeError)])
    def test_a_non_zero_exit(self, hw, error):
        # Exact classes: a CPU exit must not look like a GPU failure (the worker would rerun it on the CPU again).
        with pytest.raises(FrameDecodeError, match="exited 3") as excinfo:
            frames.run_decode(
                _fake_ffmpeg([10], ["1"], exit_code=3),
                hw_active=hw,
                pts_offset_s=0.0,
                count_boxes=lambda p: [0] * len(p),
                name="Movie.mkv",
            )
        assert type(excinfo.value) is error

    def test_no_frames_on_the_gpu_is_a_gpu_failure(self):
        with pytest.raises(GpuDecodeError, match="no frames") as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], []), hw_active=True, pts_offset_s=0.0, count_boxes=lambda p: [0] * len(p)
            )
        assert type(excinfo.value) is GpuDecodeError

    def test_no_frames_on_the_cpu_is_an_empty_answer(self):
        assert (
            frames.run_decode(
                _fake_ffmpeg([], []), hw_active=False, pts_offset_s=0.0, count_boxes=lambda p: [0] * len(p)
            )
            == []
        )

    def test_cancel_between_chunks_kills_ffmpegs_whole_group(self, tmp_path):
        # The grandchild shares ffmpeg's group (a filter helper, a Dolby Vision tool): the group kill must reach it, or
        # a cancelled job leaves processes on the box.
        cancelled = threading.Event()
        calls: list[np.ndarray] = []
        pid_file = tmp_path / "ffmpeg.pid"
        child_pid_file = tmp_path / "child.pid"

        def count(planes):
            calls.append(planes)
            cancelled.set()
            return [0] * len(planes)

        started = time.monotonic()
        try:
            with pytest.raises(DecodeCancelledError):
                frames.run_decode(
                    _fake_ffmpeg(
                        [10] * 50,
                        ["1"] * 50,
                        sleep_s=0.2,
                        pid_file=str(pid_file),
                        child_pid_file=str(child_pid_file),
                    ),
                    hw_active=False,
                    pts_offset_s=0.0,
                    count_boxes=count,
                    cancel_check=cancelled.is_set,
                    chunk_frames=2,
                )
            assert len(calls) == 1 and time.monotonic() - started < 5
            _assert_gone(pid_file)
            _assert_gone(child_pid_file, within_s=5)
        finally:
            if child_pid_file.exists() and not _gone(int(child_pid_file.read_text())):
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)

    def test_cancel_while_ffmpeg_is_still_exiting(self, tmp_path):
        # ffmpeg can close its output and linger (flushing, a driver teardown). A cancel there must not wait for the
        # deadline: the job would hold the worker for the whole timeout and the file would be recorded as timed out.
        pid_file = tmp_path / "ffmpeg.pid"
        cancelled = threading.Event()
        timer = threading.Timer(0.3, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(DecodeCancelledError):
                frames.run_decode(
                    _fake_ffmpeg([10], ["1"], pid_file=str(pid_file), close_stdout=True, linger_s=30),
                    hw_active=False,
                    pts_offset_s=0.0,
                    count_boxes=lambda p: [0] * len(p),
                    cancel_check=cancelled.is_set,
                    timeout_s=30.0,
                )
            assert time.monotonic() - started < 3
            _assert_gone(pid_file)
        finally:
            timer.cancel()

    @pytest.mark.parametrize("hw", [True, False])
    def test_a_decode_past_the_timeout_is_killed_and_is_never_a_gpu_failure(self, hw, tmp_path):
        # T-R7: a stalled read times out the same on either worker; a GpuDecodeError would add a CPU rerun of the stall.
        pid_file = tmp_path / "ffmpeg.pid"
        started = time.monotonic()
        with pytest.raises(FrameDecodeError, match="timed out") as excinfo:
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=1.0, pid_file=str(pid_file)),
                hw_active=hw,
                pts_offset_s=0.0,
                count_boxes=lambda p: [0] * len(p),
                timeout_s=1.0,
            )
        assert type(excinfo.value) is DecodeTimeoutError
        assert time.monotonic() - started < 6
        _assert_gone(pid_file)

    def test_ffmpeg_that_closes_its_output_but_never_exits_times_out(self):
        script = "import os, sys, time; sys.stdout.flush(); os.close(1); time.sleep(30)"
        started = time.monotonic()
        with pytest.raises(DecodeTimeoutError, match="timed out"):
            frames.run_decode(
                [sys.executable, "-c", script],
                hw_active=False,
                pts_offset_s=0.0,
                count_boxes=lambda p: [0] * len(p),
                timeout_s=2.0,
            )
        assert time.monotonic() - started < 9

    def test_a_process_holding_the_pipe_after_the_kill_never_blocks_the_worker(self, tmp_path):
        # A grandchild in its own session keeps stdout open after ffmpeg's group is killed (like a read stuck on a stalled
        # mount): the decode still returns within its limit plus the bounded waits, and the pipe is left to a reaper.
        pid_file = tmp_path / "holder.pid"
        script = textwrap.dedent(f"""
            import subprocess, sys, time
            holder = subprocess.Popen(["sleep", "30"], start_new_session=True)
            open({str(pid_file)!r}, "w").write(str(holder.pid))
            time.sleep(30)
        """)
        warnings: list[str] = []
        sink = logger.add(warnings.append, level="WARNING", format="{message}")
        before = _reapers()
        started = time.monotonic()
        try:
            with pytest.raises(DecodeTimeoutError):
                frames.run_decode(
                    [sys.executable, "-c", script],
                    hw_active=False,
                    pts_offset_s=0.0,
                    count_boxes=lambda p: [0] * len(p),
                    timeout_s=2.0,
                )
            assert time.monotonic() - started < 2.0 + 6
            assert any("still holds its output" in message for message in warnings)
            handed_over = [t for t in _reapers() if t not in before]
            assert len(handed_over) == 1 and handed_over[0].is_alive()
        finally:
            logger.remove(sink)
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
        handed_over[0].join(timeout=10)
        assert not handed_over[0].is_alive()  # the reaper lets go once the holder does

    def test_a_child_that_ignores_a_polite_signal_is_still_killed(self, tmp_path):
        # ffmpeg can be mid-syscall or trapping signals; the kill has to be SIGKILL, or a stalled decode keeps its
        # worker's device and file handles after the timeout.
        # It closes its output first, so nothing but the signal can end it (a broken pipe would kill it anyway).
        pid_file = tmp_path / "ffmpeg.pid"
        try:
            with pytest.raises(DecodeTimeoutError):
                frames.run_decode(
                    _fake_ffmpeg(
                        [10],
                        ["1"],
                        pid_file=str(pid_file),
                        ignore_sigterm=True,
                        close_stdout=True,
                        linger_s=30,
                    ),
                    hw_active=False,
                    pts_offset_s=0.0,
                    count_boxes=lambda p: [0] * len(p),
                    timeout_s=1.0,
                )
            _assert_gone(pid_file, within_s=5)
        finally:
            if pid_file.exists() and not _gone(int(pid_file.read_text())):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_the_reader_thread_stops_when_the_decode_gives_up(self, tmp_path):
        # Cancelled with the queue full, the reader is waiting for room nobody will make. Without the stop flag it
        # waits for ever: a thread and a pipe leak on every cancelled or timed-out decode.
        before = _readers()
        cancelled = threading.Event()

        def count(planes):
            time.sleep(0.5)  # long enough for the reader to fill the queue and block
            cancelled.set()
            return [0] * len(planes)

        with pytest.raises(DecodeCancelledError):
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50),
                hw_active=False,
                pts_offset_s=0.0,
                count_boxes=count,
                cancel_check=cancelled.is_set,
                chunk_frames=1,
            )
        assert _wait_for(lambda: _readers() == before, within_s=10)

    def test_the_queue_bounds_how_far_the_decoder_runs_ahead(self, tmp_path):
        # Two chunks in flight, whatever the decoder's speed: unbounded, a GPU decode would buffer a whole 4K tail
        # (671 frames, 39 MB) per worker while text detection catches up.
        progress = tmp_path / "written"
        ahead: list[int] = []

        def count(planes):
            if not ahead:
                time.sleep(1.5)
                ahead.append(int(progress.read_text()))
            return [0] * len(planes)

        rows = frames.run_decode(
            _fake_ffmpeg([10] * 100, [str(i) for i in range(100)], progress_file=str(progress)),
            hw_active=False,
            pts_offset_s=0.0,
            count_boxes=count,
            chunk_frames=1,
            timeout_s=30,
        )
        assert len(rows) == 100
        assert ahead[0] <= 8  # 2 queued, the rest still in the pipe buffer; unbounded this reads 100

    @pytest.mark.parametrize("hw", [True, False])
    def test_an_ffmpeg_that_cannot_be_started_is_a_decode_error(self, hw, tmp_path):
        # Task 8 only catches FrameDecodeError; a bare OSError would fail the whole item. A GPU worker must not rerun
        # on the CPU either, since it would run the same missing binary.
        missing = str(tmp_path / "ffmpeg")
        with pytest.raises(FrameDecodeError, match="could not run") as excinfo:
            frames.run_decode(
                [missing, "-i", "x"],
                hw_active=hw,
                pts_offset_s=0.0,
                count_boxes=lambda p: [0] * len(p),
                name="Movie.mkv",
            )
        assert type(excinfo.value) is FrameDecodeError
        assert missing in str(excinfo.value) and "Movie.mkv" in str(excinfo.value)

    def test_rows_are_seconds_from_the_start_of_the_file(self):
        # A recorded .ts reports pts from its PCR base; rule J and the published marker need file seconds.
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250], ["30020.5", "30021.5"]),
            hw_active=False,
            count_boxes=lambda p: [0] * len(p),
            pts_offset_s=30000.0,
        )
        assert [r[0] for r in rows] == [20.5, 21.5]

    def test_the_timestamp_offset_has_to_be_given(self):
        # No default: a caller that forgets it would silently publish a recording's raw container timestamps.
        with pytest.raises(TypeError, match="pts_offset_s"):
            frames.run_decode(_fake_ffmpeg([10], ["1"]), hw_active=False, count_boxes=lambda p: [0] * len(p))

    def test_slow_text_detection_never_loses_the_end_of_the_stream(self):
        def slow(planes):
            time.sleep(1.5)
            return [0] * len(planes)

        started = time.monotonic()
        rows = frames.run_decode(
            _fake_ffmpeg([10, 10, 10], ["1", "2", "3"]),
            hw_active=False,
            pts_offset_s=0.0,
            count_boxes=slow,
            chunk_frames=1,
            timeout_s=12,
        )
        assert [r[0] for r in rows] == [1.0, 2.0, 3.0]
        assert time.monotonic() - started < 8

    @pytest.mark.parametrize("count", [192, 256, 320])
    def test_whole_chunks_with_a_slow_detector_finish(self, count):
        # Reproduced before the fix: a tail with a multiple of 64 frames filled the queue while the last chunk was being
        # counted, dropped the end marker and was reported as a timeout.
        def slow(planes):
            time.sleep(1.2)
            return [0] * len(planes)

        started = time.monotonic()
        rows = frames.run_decode(
            _fake_ffmpeg([10] * count, [str(i) for i in range(count)]),
            hw_active=True,
            pts_offset_s=0.0,
            count_boxes=slow,
            timeout_s=20,
        )
        assert len(rows) == count
        assert time.monotonic() - started < 1.2 * count / 64 + 6

    def test_a_failing_box_count_kills_ffmpeg_and_propagates(self, tmp_path):
        pid_file = tmp_path / "ffmpeg.pid"

        def count(planes):
            raise RuntimeError("helper gone")

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="helper gone"):
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.2, pid_file=str(pid_file)),
                hw_active=False,
                pts_offset_s=0.0,
                count_boxes=count,
                chunk_frames=1,
            )
        assert time.monotonic() - started < 5
        _assert_gone(pid_file)

    def test_a_box_count_per_frame_is_required(self, tmp_path):
        # A helper answering the wrong number of counts would shift every later row's boxes onto another frame's pts.
        pid_file = tmp_path / "ffmpeg.pid"
        with pytest.raises(FrameDecodeError, match="answered 1 counts for 2 frames") as excinfo:
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.2, pid_file=str(pid_file)),
                hw_active=True,
                pts_offset_s=0.0,
                count_boxes=lambda p: [0],
                chunk_frames=2,
            )
        assert type(excinfo.value) is FrameDecodeError
        _assert_gone(pid_file)


class TestDecodeRows:
    @pytest.mark.parametrize(
        ("gpu", "device", "hw"),
        [("NVIDIA", "cuda:0", True), (None, None, False)],
    )
    def test_runs_the_worker_command_with_the_callers_limits(self, monkeypatch, gpu, device, hw):
        seen: dict = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            seen.update(kwargs)
            return [(1.0, 0, 10.0)]

        monkeypatch.setattr(frames, "run_decode", fake_run)

        def count(planes):
            return [0] * len(planes)

        def cancel():
            return False

        rows = frames.decode_rows(
            MOVIE,
            ffmpeg=FF,
            start_s=5680.5,
            length_s=21.0,
            keyframes_only=False,
            fps=1,
            gpu=gpu,
            gpu_device_path=device,
            count_boxes=count,
            cancel_check=cancel,
            timeout_s=42.0,
            start_time_s=0.0,
        )
        expected_command, _ = frames.decode_command(
            FF, MOVIE, start_s=5680.5, length_s=21.0, keyframes_only=False, fps=1, gpu=gpu, gpu_device_path=device
        )
        assert rows == [(1.0, 0, 10.0)]
        assert seen == {
            "command": expected_command,
            "hw_active": hw,
            "count_boxes": count,
            "cancel_check": cancel,
            "timeout_s": 42.0,
            "pts_offset_s": 0.0,
            "name": "Movie.mkv",
        }

    @pytest.mark.parametrize(
        ("start_time_ms", "expected"),
        [(30_000_000, 30000.0), (0, 0.0), (None, 0.0)],  # a recorded .ts, a normal file, a container with no start
    )
    def test_the_containers_own_start_is_subtracted_from_every_row(self, monkeypatch, start_time_ms, expected):
        seen: dict = {}
        probed: dict = {}

        def fake_probe(path, **kwargs):
            probed["path"] = path
            probed.update(kwargs)
            return MediaProbe(1000, (), start_time_ms=start_time_ms)

        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: seen.update(kwargs) or [])
        monkeypatch.setattr(frames, "probe_media", fake_probe)
        frames.decode_rows(
            "/m/Recording.ts",
            ffmpeg=FF,
            start_s=20.0,
            length_s=None,
            keyframes_only=True,
            fps=None,
            gpu=None,
            gpu_device_path=None,
            count_boxes=lambda p: [0] * len(p),
            timeout_s=600.0,
        )
        assert seen["pts_offset_s"] == expected
        # The ffprobe beside the configured ffmpeg, and a bound well inside the decode's own: handing ffprobe the
        # ffmpeg binary, or leaving its 60 s default, would only show up in production.
        assert probed == {"path": "/m/Recording.ts", "ffprobe": frames.ffprobe_path_for(FF), "timeout_s": 30.0}

    def test_the_probe_is_capped_by_a_short_decode_timeout(self, monkeypatch):
        probed: dict = {}
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: [])
        monkeypatch.setattr(frames, "probe_media", lambda path, **kwargs: probed.update(kwargs) or MediaProbe(1000, ()))
        frames.decode_rows(
            "/m/Recording.ts",
            ffmpeg=FF,
            start_s=20.0,
            length_s=None,
            keyframes_only=True,
            fps=None,
            gpu=None,
            gpu_device_path=None,
            count_boxes=lambda p: [0] * len(p),
            timeout_s=5.0,
        )
        assert probed["timeout_s"] == 5.0

    def test_a_cancelled_job_never_probes_or_decodes(self, monkeypatch):
        # The probe runs before ffmpeg exists, outside run_decode's cancel checks: on a stalled mount it would hold the
        # worker for the probe's timeout after the job was cancelled.
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: pytest.fail("decoded anyway"))
        monkeypatch.setattr(frames, "probe_media", lambda path, **kwargs: pytest.fail("probed anyway"))
        with pytest.raises(DecodeCancelledError, match="cancelled before decoding Recording.ts"):
            frames.decode_rows(
                "/m/Recording.ts",
                ffmpeg=FF,
                start_s=20.0,
                length_s=None,
                keyframes_only=True,
                fps=None,
                gpu=None,
                gpu_device_path=None,
                count_boxes=lambda p: [0] * len(p),
                cancel_check=lambda: True,
            )

    def test_a_given_start_time_is_used_without_probing(self, monkeypatch):
        seen: dict = {}
        probes: list[str] = []
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: seen.update(kwargs) or [])
        monkeypatch.setattr(frames, "probe_media", lambda path, **kwargs: probes.append(path))
        frames.decode_rows(
            "/m/Recording.ts",
            ffmpeg=FF,
            start_s=20.0,
            length_s=None,
            keyframes_only=True,
            fps=None,
            gpu=None,
            gpu_device_path=None,
            count_boxes=lambda p: [0] * len(p),
            start_time_s=30000.0,
        )
        assert seen["pts_offset_s"] == 30000.0 and probes == []

    def test_a_file_whose_start_time_cannot_be_read_is_a_decode_error(self, monkeypatch):
        # Guessing 0.0 would time a recorded .ts 30000 s out and store it as a good answer; no answer is the safe one.
        def boom(path, **kwargs):
            raise ProbeError("ffprobe exited 1")

        monkeypatch.setattr(frames, "probe_media", boom)
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: pytest.fail("decoded anyway"))
        with pytest.raises(FrameDecodeError, match="could not read the start time of Recording.ts") as excinfo:
            frames.decode_rows(
                "/m/Recording.ts",
                ffmpeg=FF,
                start_s=20.0,
                length_s=None,
                keyframes_only=True,
                fps=None,
                gpu=None,
                gpu_device_path=None,
                count_boxes=lambda p: [0] * len(p),
            )
        assert type(excinfo.value) is FrameDecodeError

    def test_a_start_time_probe_that_times_out_is_a_decode_timeout(self, monkeypatch):
        # A stalled mount stalls ffprobe as surely as ffmpeg. As a plain decode error the file would take a worker
        # every run only to stall again; as a timeout it is left alone for a day, like a decode that timed out.
        def stalled(path, **kwargs):
            raise ProbeTimeoutError("ffprobe failed for /m/Recording.ts: TimeoutExpired")

        monkeypatch.setattr(frames, "probe_media", stalled)
        with pytest.raises(DecodeTimeoutError, match="reading the start time of Recording.ts timed out after 30 s"):
            frames.container_start_s("/m/Recording.ts", FF)

    def test_a_probe_not_started_for_earlier_stuck_ones_is_no_answer_this_time(self, monkeypatch):
        # Not this file's timeout: it isn't left alone for a day, only unanswered this run.
        def gated(path, **kwargs):
            raise ProbeStalledError(f"Not reading {path}: 2 earlier ffprobes are still stuck reading their files")

        monkeypatch.setattr(frames, "probe_media", gated)
        with pytest.raises(FrameDecodeError, match="could not read the start time of Recording.ts") as caught:
            frames.container_start_s("/m/Recording.ts", FF)
        assert type(caught.value) is FrameDecodeError

    @pytest.mark.parametrize(
        ("keep_every", "drop_non_key", "bsf"),
        [(50, False, r"noise=drop=mod(n\,50)"), (None, True, "noise=drop=not(key)"),
         (50, True, r"noise=drop=not(key)+mod(n\,50)")],
    )  # fmt: skip
    def test_the_packet_drops_reach_the_command(self, monkeypatch, keep_every, drop_non_key, bsf):
        seen: dict = {}
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: seen.update(command=command) or [])
        frames.decode_rows(MOVIE, ffmpeg=FF, start_s=5100.0, length_s=None, keyframes_only=True, fps=None, gpu=None,
                           gpu_device_path=None, count_boxes=lambda p: [0] * len(p), start_time_s=0.0,
                           keep_every=keep_every, drop_non_key=drop_non_key)  # fmt: skip
        expected, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None,
                                            gpu=None, gpu_device_path=None, keep_every=keep_every,
                                            drop_non_key=drop_non_key)  # fmt: skip
        assert seen["command"] == expected
        assert expected[expected.index("-bsf:V:0") + 1] == bsf


def _packets(count: int, *, fps: float = 24.0, start_s: float = 0.0, keyframe: bool = True) -> list[VideoPacket]:
    return [VideoPacket(round(start_s + i / fps, 6), keyframe) for i in range(count)]


class TestKeyframeThinning:
    @pytest.fixture
    def probed(self, monkeypatch):
        """Every packet probe's arguments; it answers ``probed.codec`` and ``probed.packets`` (or raises
        ``probed.error``)."""

        class Probed(list):
            codec: str | None = "h264"
            packets: list[VideoPacket] = []
            error: Exception | None = None

        calls = Probed()

        def video_packets(path, **kwargs):
            calls.append({"path": path, **kwargs})
            if calls.error:
                raise calls.error
            return VideoPackets(calls.codec, tuple(calls.packets))

        monkeypatch.setattr(frames, "video_packets", video_packets)
        return calls

    @pytest.mark.parametrize(
        ("fps", "stride"),
        [(24.0, 48), (24000 / 1001, 48), (25.0, 50), (30000 / 1001, 60), (50.0, 100), (60.0, 120), (1.0, 2)],
    )
    def test_an_intra_only_stream_keeps_one_packet_per_two_seconds(self, probed, fps, stride):
        probed.packets = _packets(
            24, fps=fps, start_s=30000.0
        )  # a recording's PCR base changes nothing: only gaps count
        assert frames.keyframe_thinning(MOVIE, FF) == KeyframeThinning(stride, False)
        # The ffprobe beside the configured ffmpeg, 24 packets, the probe's short bound: handing ffprobe the ffmpeg
        # binary or its 60 s default would only show up in production.
        assert probed == [{"path": MOVIE, "ffprobe": frames.ffprobe_path_for(FF), "packets": 24, "timeout_s": 30.0}]

    def test_the_millisecond_times_of_a_matroska_file_give_the_same_stride(self, probed):
        # Matroska stores milliseconds: 24 fps reads 0, 42, 83, 125 … ms, gaps of 41 and 42.
        probed.packets = [VideoPacket(round(i / 24, 3), True) for i in range(24)]
        assert frames.keyframe_thinning(MOVIE, FF).keep_every == 48

    def test_packets_out_of_order_or_repeated_or_without_a_time_still_give_the_frame_interval(self, probed):
        packets = _packets(26)
        probed.packets = [packets[1], packets[0], *packets[2:10], VideoPacket(None, True), *packets[10:22], packets[21]]
        assert len(probed.packets) == 24
        assert frames.keyframe_thinning(MOVIE, FF).keep_every == 48
        probed.packets = packets[23::-1]  # listed last to first: every gap between neighbours is negative
        assert frames.keyframe_thinning(MOVIE, FF).keep_every == 48

    def test_one_jump_in_the_times_leaves_the_frame_interval_alone(self, probed):
        # A timestamp discontinuity (a splice, a dropped run of frames) among the packets: the typical gap is the
        # frame interval, where an average over them would read one frame per half second.
        probed.packets = [*_packets(12), *_packets(12, start_s=10.0)]
        assert frames.keyframe_thinning(MOVIE, FF).keep_every == 48

    NORMAL_GOP = [VideoPacket(0.0, True), *_packets(23, start_s=1 / 24, keyframe=False)]

    @pytest.mark.parametrize(
        ("codec", "drop_non_key"),
        [("vp9", True), ("h264", False), ("hevc", False), ("av1", False), ("vp8", False), ("mpeg2video", False),
         ("mpeg4", False), ("vc1", False), ("mjpeg", False), (None, False)],
    )  # fmt: skip
    @pytest.mark.parametrize(("intra_only", "keep_every"), [(False, None), (True, 48)], ids=["gop", "intra-only"])
    def test_only_vp9_drops_its_non_key_packets_and_the_stride_is_decided_apart(self, probed, codec, drop_non_key,
                                                                               intra_only, keep_every):  # fmt: skip
        # VP9's decoder never reads -skip_frame (every hwaccel decodes inside it); every other decoder honours it, so
        # its command stays the spec's (measured per codec in phase3-harness.md). The codec comes from the one ffprobe
        # that reads the packets: no second process per file.
        probed.codec = codec
        probed.packets = _packets(24) if intra_only else self.NORMAL_GOP
        assert frames.keyframe_thinning(MOVIE, FF) == KeyframeThinning(keep_every, drop_non_key)
        assert len(probed) == 1

    def test_a_short_vp9_stream_still_drops_its_non_key_packets(self, probed):
        # Too few packets to call it intra-only says nothing about its decoder.
        probed.codec, probed.packets = "vp9", self.NORMAL_GOP[:10]
        assert frames.keyframe_thinning(MOVIE, FF) == KeyframeThinning(None, True)

    @pytest.mark.parametrize(
        "packets",
        [
            [VideoPacket(0.0, True), *_packets(23, start_s=1 / 24, keyframe=False)],  # a normal stream: one keyframe
            [*_packets(23), VideoPacket(23 / 24, False)],  # one frame that isn't a keyframe is enough
            _packets(23),  # too short to tell (and to take long, read in full)
            [],  # no video stream
            [VideoPacket(None, True)] * 24,  # no times: no frame interval to step by
            [VideoPacket(5.0, True)] * 24,  # one time for all: no frame interval either
            _packets(24, fps=0.5),  # a frame every 2 s already: a stride of 1 is no stride
            _packets(24, fps=0.4),  # sparser still (the stride would round to 1)
        ],
        ids=["normal-gop", "one-non-key", "too-short", "no-video", "no-times", "one-time", "stride-1", "sparser"],
    )
    def test_anything_else_is_read_the_ordinary_way(self, probed, packets):
        probed.packets = packets
        assert frames.keyframe_thinning(MOVIE, FF) == KeyframeThinning(None, False)

    def test_packets_that_cant_be_read_leave_the_file_read_the_ordinary_way(self, probed, loguru_caplog):
        # The start time probe on the same file already succeeded: failing here too would only lose an answer the file
        # could get the ordinary way.
        probed.error = ProbeError("ffprobe exited 1 for /media/Movie (2020)/Movie.mkv: invalid data")
        assert frames.keyframe_thinning(MOVIE, FF) == KeyframeThinning(None, False)
        assert "Couldn't read the video packets of Movie.mkv, so it is read the ordinary way" in loguru_caplog.text

    def test_a_probe_that_times_out_is_a_decode_timeout(self, probed):
        probed.error = ProbeTimeoutError("ffprobe failed for /media/Movie (2020)/Movie.mkv: TimeoutExpired")
        with pytest.raises(DecodeTimeoutError, match="reading the video packets of Movie.mkv timed out after 7 s"):
            frames.keyframe_thinning(MOVIE, FF, timeout_s=7.0)
        assert probed[0]["timeout_s"] == 7.0

    def test_a_probe_not_started_for_earlier_stuck_ones_is_no_answer_this_time(self, probed):
        # Not this file's fault, and not a timeout of its own: no answer this run, nothing recorded against it.
        probed.error = ProbeStalledError("Not reading x: 2 earlier ffprobes are still stuck reading their files")
        with pytest.raises(FrameDecodeError, match="could not read the video packets of Movie.mkv: Not reading x") as e:
            frames.keyframe_thinning(MOVIE, FF)
        assert type(e.value) is FrameDecodeError

    def test_a_cancelled_job_is_not_probed(self, probed):
        with pytest.raises(DecodeCancelledError, match="cancelled before decoding Movie.mkv"):
            frames.keyframe_thinning(MOVIE, FF, cancel_check=lambda: True)
        assert probed == []
