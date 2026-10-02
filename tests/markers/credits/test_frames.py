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
    GpuReadNothingError,
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

# Imported before ``tests/markers/conftest.py`` fakes it for every test: these are its own tests.
readable_video_s = frames.readable_video_s

FF = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
MOVIE = "/media/Movie (2020)/Movie.mkv"
TAIL = ["-an", "-sn", "-dn", "-fps_mode", "passthrough"]
RENDER = "/dev/dri/renderD128"


class TestCommand:
    # Every path scales the same way: the decoded frame, whole, through one software scaler taking the nearest pixel
    # (flags=neighbor). A GPU scaler of its own (scale_cuda, scale_vaapi) or swscale's default bicubic blurs text only a
    # few pixels tall differently on each vendor, and credits found on NVIDIA were lost on Intel and the CPU.
    CUDA = ["-hwaccel", "cuda", "-hwaccel_device", "1", "-hwaccel_output_format", "cuda"]
    VAAPI = ["-hwaccel", "vaapi", "-hwaccel_device", RENDER, "-hwaccel_output_format", "vaapi", "-extra_hw_frames", "8"]

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
            download_format="nv12",
        )
        assert hw is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", *self.CUDA,
                       "-skip_frame", "nokey", "-ss", "5100.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "hwdownload,format=nv12,scale=320:180:flags=neighbor,format=nv12,showinfo",
                       "-f", "rawvideo", "-"]  # fmt: skip

    def test_vaapi_refine_window_at_1_fps_of_a_10_bit_stream(self):
        # fps=1 picks the frames while they are still GPU surfaces: only those are downloaded, in the stream's own
        # format (P010 for 10-bit), and the scaler converts to the 8-bit luma text detection reads.
        cmd, hw = frames.decode_command(
            FF,
            MOVIE,
            start_s=5680.5,
            length_s=21.0,
            keyframes_only=False,
            fps=1,
            gpu="INTEL",
            gpu_device_path=RENDER,
            download_format="p010le",
        )
        assert hw is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", *self.VAAPI,
                       "-ss", "5680.500", "-t", "21.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "fps=1,hwdownload,format=p010le,scale=320:180:flags=neighbor,format=nv12,showinfo",
                       "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(
        ("gpu", "device", "download_format", "hw_args", "hw"),
        [
            (None, None, "nv12", [], False),
            (None, None, None, [], False),
            ("INTEL", None, "nv12", [], False),
            ("APPLE", "videotoolbox", "nv12", ["-hwaccel", "videotoolbox"], True),
            ("WINDOWS_GPU", "d3d11va", "nv12", ["-hwaccel", "d3d11va"], True),
            # Only CUDA and the VAAPI GPUs keep their surfaces for the graph to download: other VAAPI nodes decode on
            # the GPU and ffmpeg downloads each frame itself (no -hwaccel_output_format).
            ("ARM", RENDER, "nv12", ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
            ("VIDEOCORE", RENDER, "nv12", ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
            ("UNKNOWN", RENDER, "nv12", ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
            # A stream whose surfaces' format isn't known (4:2:2, 4:4:4, 12-bit, a failed probe): hwdownload would
            # need it named, so ffmpeg downloads each frame itself, as on any other GPU.
            ("NVIDIA", "cuda:0", None, ["-hwaccel", "cuda", "-hwaccel_device", "0"], True),
            ("INTEL", RENDER, None, ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
            ("AMD", RENDER, None, ["-hwaccel", "vaapi", "-hwaccel_device", RENDER], True),
        ],
        ids=["cpu", "cpu-unknown-format", "intel-without-device", "apple", "windows", "arm", "videocore", "unknown",
             "cuda-unknown-format", "intel-unknown-format", "amd-unknown-format"],
    )  # fmt: skip
    def test_software_scaling_cells(self, gpu, device, download_format, hw_args, hw):
        cmd, active = frames.decode_command(FF, MOVIE, start_s=0.0, length_s=None, keyframes_only=True, fps=None,
                                            gpu=gpu, gpu_device_path=device, download_format=download_format)  # fmt: skip
        assert active is hw
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", *hw_args,
                       "-skip_frame", "nokey", "-ss", "0.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "scale=320:180:flags=neighbor,format=nv12,showinfo", "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(
        ("gpu", "device", "download_format", "hw_args"),
        [
            ("AMD", "/dev/dri/renderD129", "nv12", ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129", "-hwaccel_output_format", "vaapi"]),
            ("INTEL", RENDER, "p010le", ["-hwaccel", "vaapi", "-hwaccel_device", RENDER, "-hwaccel_output_format", "vaapi"]),
            ("NVIDIA", None, "p010le", ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]),
            ("NVIDIA", "cuda:0", "nv12", ["-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"]),
        ],
        ids=["amd", "intel-10-bit", "cuda-without-index-10-bit", "cuda"],
    )  # fmt: skip
    def test_gpu_download_cells(self, gpu, device, download_format, hw_args):
        # Spare surfaces for the frames the graph holds while it downloads them, on VAAPI: a 4K VAAPI decode failed
        # without them. CUDA never needed them (its scale_cuda chain held more frames than hwdownload alone does), and
        # each is a full-size NVDEC surface.
        cmd, active = frames.decode_command(FF, MOVIE, start_s=12.25, length_s=None, keyframes_only=True, fps=None,
                                            gpu=gpu, gpu_device_path=device, download_format=download_format)  # fmt: skip
        spare = [] if gpu == "NVIDIA" else ["-extra_hw_frames", "8"]
        assert active is True
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", *hw_args, *spare,
                       "-skip_frame", "nokey", "-ss", "12.250", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", f"hwdownload,format={download_format},scale=320:180:flags=neighbor,format=nv12,showinfo",
                       "-f", "rawvideo", "-"]  # fmt: skip

    @pytest.mark.parametrize(
        ("gpu", "device", "fps", "video_filter"),
        [
            ("NVIDIA", "cuda:0", None, "hwdownload,format=nv12,scale=640:360:flags=neighbor,format=nv12"),
            ("INTEL", RENDER, None, "hwdownload,format=nv12,scale=640:360:flags=neighbor,format=nv12"),
            (None, None, None, "scale=640:360:flags=neighbor,format=nv12"),
            ("NVIDIA", "cuda:0", 1, "fps=1,hwdownload,format=nv12,scale=640:360:flags=neighbor,format=nv12"),
            (None, None, 1, "fps=1,scale=640:360:flags=neighbor,format=nv12"),
        ],
        ids=["nvidia", "vaapi", "cpu", "nvidia-refine", "cpu-refine"],
    )
    def test_a_scale_of_2_decodes_every_frame_at_640x360(self, gpu, device, fps, video_filter):
        # The larger read of a tail whose 320x180 frames gave no answer: the same command with only the frame's size
        # doubled, on every path.
        window = {"start_s": 5100.0, "length_s": None, "keyframes_only": fps is None, "fps": fps, "gpu": gpu,
                  "gpu_device_path": device, "download_format": "nv12"}  # fmt: skip
        cmd, _ = frames.decode_command(FF, MOVIE, **window, scale=2)
        plain, _ = frames.decode_command(FF, MOVIE, **window)
        vf = cmd.index("-vf") + 1
        assert cmd[vf] == f"{video_filter},showinfo"
        assert cmd[:vf] + cmd[vf + 1 :] == plain[:vf] + plain[vf + 1 :]

    @pytest.mark.parametrize(
        ("gpu", "device", "ffmpeg_threads", "threads"),
        [
            ("NVIDIA", "cuda:1", 3, ["-threads", "3", "-filter_threads", "3"]),
            ("INTEL", RENDER, 3, ["-threads", "3", "-filter_threads", "3"]),
            ("NVIDIA", "cuda:1", None, []),
            ("NVIDIA", "cuda:1", 0, []),
            (None, None, None, []),
            (None, None, 3, []),
        ],
        ids=["gpu-worker-3", "vaapi-worker-3", "gpu-worker-no-value", "gpu-worker-0", "cpu-worker",
             "cpu-decode-on-a-gpu-worker"],
    )  # fmt: skip
    def test_a_gpu_workers_own_ffmpeg_threads_cap_its_decode_as_previews_do(self, gpu, device, ffmpeg_threads, threads):
        # Previews cap a GPU worker's FFmpeg at its GPU's ffmpeg_threads (-threads and -filter_threads) and leave every
        # CPU decode, a GPU worker's CPU rerun included, at FFmpeg's own count. 0 or no value is no cap there too.
        cmd, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None,
                                       gpu=gpu, gpu_device_path=device, download_format="nv12",
                                       ffmpeg_threads=ffmpeg_threads)  # fmt: skip
        assert cmd[:5] == [FF, "-nostdin", "-hide_banner", "-loglevel", "info"]
        assert cmd[5 : 5 + len(threads)] == threads
        assert cmd.count("-threads") == cmd.count("-filter_threads") == (1 if threads else 0)

    def test_no_thinning_is_the_spec_command_exactly(self):
        # What every file that is neither intra-only nor VP9 gets (the detector passes keep_every=None and
        # drop_non_key=False): the command the 80 and the 205 were measured with, byte for byte.
        cmd, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None,
                                       gpu="NVIDIA", gpu_device_path="cuda:1", keep_every=None, drop_non_key=False,
                                       download_format="nv12")  # fmt: skip
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", *self.CUDA,
                       "-skip_frame", "nokey", "-ss", "5100.000", "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", "hwdownload,format=nv12,scale=320:180:flags=neighbor,format=nv12,showinfo",
                       "-f", "rawvideo", "-"]  # fmt: skip

    GPU_DOWNLOAD = "hwdownload,format=nv12,scale=320:180:flags=neighbor,format=nv12"
    CPU_SCALE = "scale=320:180:flags=neighbor,format=nv12"

    @pytest.mark.parametrize(
        ("gpu", "device", "hw_args", "scale"),
        [
            (None, None, [], CPU_SCALE),
            ("NVIDIA", "cuda:0", ["-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"], GPU_DOWNLOAD),
            ("INTEL", RENDER, ["-hwaccel", "vaapi", "-hwaccel_device", RENDER, "-hwaccel_output_format", "vaapi", "-extra_hw_frames", "8"], GPU_DOWNLOAD),
        ],
    )  # fmt: skip
    def test_an_intra_only_keyframe_pass_drops_packets_before_the_decoder(self, gpu, device, hw_args, scale):
        # An input option (before -i): as a filter after the decoder, every frame would still be decoded. V:0 is the
        # stream the stride was measured on (ffmpeg may decode another). The comma is escaped for ffmpeg's bitstream
        # filter list, not for a shell: argv never goes through one.
        cmd, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None,
                                       gpu=gpu, gpu_device_path=device, keep_every=48, download_format="nv12")  # fmt: skip
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", *hw_args,
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
            (None, None, [], CPU_SCALE),
            ("NVIDIA", "cuda:0", ["-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"], GPU_DOWNLOAD),
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
                                       drop_non_key=drop_non_key, download_format="nv12")  # fmt: skip
        bsf = self.PACKET_DROPS[(keep_every, drop_non_key)]
        seek = ["-skip_frame", "nokey", "-ss", "5100.000"] if keyframe_pass else ["-ss", "5680.000", "-t", "21.000"]
        video_filter = f"{scale},showinfo" if keyframe_pass else f"fps=1,{scale},showinfo"
        assert cmd == [FF, "-nostdin", "-hide_banner", "-loglevel", "info", *hw_args,
                       *(["-bsf:V:0", bsf] if bsf else []), *seek, "-copyts", "-i", MOVIE, *TAIL,
                       "-vf", video_filter, "-f", "rawvideo", "-"]  # fmt: skip
        assert cmd.count("-bsf:V:0") == (1 if bsf else 0)

    @pytest.mark.parametrize(
        ("duration_ms", "tail_s", "expected"),
        [(6_000_000, 900.0, 5100.0), (1_320_000, 450.0, 870.0), (300_000, 450.0, 0.0), (600_000, 900.0, 0.0),
         (6_000_000, 1800.0, 4200.0), (1_320_000, 300.0, 1020.0)],
    )  # fmt: skip
    def test_tail_start(self, duration_ms, tail_s, expected):
        assert frames.tail_start_s(duration_ms, tail_s=tail_s) == expected

    @pytest.mark.parametrize(
        ("is_episode", "tv_s", "movie_s", "expected"),
        [
            # Automatic is what it has always been: 450 s an episode, 900 s a movie or a file of unknown kind.
            (True, None, None, 450.0),
            (False, None, None, 900.0),
            # A window applies to its own kind only.
            (True, 600, None, 600.0),
            (False, 600, None, 900.0),
            (True, None, 1800, 450.0),
            (False, None, 1800, 1800.0),
            (True, 300, 1800, 300.0),
            (False, 300, 1800, 1800.0),
        ],
    )
    def test_tail_length_follows_the_window_for_the_files_kind(self, is_episode, tv_s, movie_s, expected):
        assert frames.tail_length_s(is_episode=is_episode, tv_s=tv_s, movie_s=movie_s) == expected

    def test_tail_length_with_no_window_arguments_is_automatic(self):
        assert frames.tail_length_s(is_episode=True) == 450.0
        assert frames.tail_length_s(is_episode=False) == 900.0


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
    width: int = 320,
    height: int = 180,
    stderr_tail: str = "",
    stderr_head: str = "",
    head_wait_s: float = 0.0,
) -> list[str]:
    """A child that writes NV12 frames (Y plane filled with each value) to stdout and showinfo lines to stderr.

    ``pts`` entries past the frames become showinfo lines with no whole frame behind them (ffmpeg dying mid-write);
    ``child_pid_file`` spawns a grandchild in the same process group, so the group kill can be asserted;
    ``ignore_sigterm`` stands in for an ffmpeg that won't take a polite signal; ``progress_file`` records how many
    frames have been written, so how far the decoder ran ahead of text detection can be read; ``width`` and ``height``
    are the frames' size; ``stderr_tail`` is written to stderr after the frames (what ffmpeg says as it exits);
    ``stderr_head`` is written before them, which then wait ``head_wait_s`` (what a decoder says before any frame).
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

        sys.stderr.write({stderr_head!r})
        sys.stderr.flush()
        time.sleep({head_wait_s})
        for i, value in enumerate({frame_values!r}):
            if i < len(pts):
                line(i)
            out.write(bytes([value]) * ({width} * {height}) + bytes([128]) * ({width} * {height} // 2))
            out.flush()
            progress(i + 1)
            time.sleep({sleep_s})
        for i in range(len({frame_values!r}), len(pts)):
            line(i)
        out.write(b"x" * {extra_bytes})
        out.flush()
        sys.stderr.write({stderr_tail!r})
        if {close_stdout!r}:
            os.close(1)
        time.sleep({linger_s})
        sys.exit({exit_code})
    """)
    return [sys.executable, "-c", script]


# What _detector answers for a bright frame: three boxes, each at its own place in the 320x180 frame.
BRIGHT_BOXES = ((40, 24, 128, 44), (41, 55, 130, 75), (44, 85, 133, 105))


def _detector(calls: list[np.ndarray]):
    def detect(planes: np.ndarray) -> list[tuple[tuple[int, int, int, int], ...]]:
        calls.append(planes.copy())
        return [BRIGHT_BOXES if p[0, 0] > 200 else () for p in planes]

    return detect


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
            _fake_ffmpeg(values, pts), hw_active=False, pts_offset_s=0.0, detect_boxes=_detector(calls), chunk_frames=2
        )
        assert rows == [
            (5100.123, 0, 10.0, ()),
            (5102.5, 3, 250.0, BRIGHT_BOXES),
            (5101.9, 3, 250.0, BRIGHT_BOXES),
            (5104.0, 0, 120.0, ()),
            (5106.001, 0, 5.0, ()),
        ]
        assert [c.shape for c in calls] == [(2, 180, 320), (2, 180, 320), (1, 180, 320)]
        assert calls[0].dtype == np.uint8 and int(calls[0][1, 5, 5]) == 250  # the Y plane, not the chroma
        assert _reapers() == before  # a decode that ended cleanly closes its own pipe, no reaper

    def test_a_frame_at_scale_2_is_read_whole_and_its_boxes_come_back_in_320x180_pixels(self):
        # Rule J's pixel numbers (the band's 32 px, the overlay boxes) are the 320x180 frame's, so a box found on the
        # 640x360 frame is halved: inclusive pixel indices 0..639 become 0..319, and a frame-wide box stays frame-wide.
        calls: list[np.ndarray] = []

        def detect(planes: np.ndarray) -> list[tuple[tuple[int, int, int, int], ...]]:
            calls.append(planes.copy())
            return [((0, 0, 639, 359), (321, 181, 323, 183))] * len(planes)

        rows = frames.run_decode(
            _fake_ffmpeg([10, 250], ["1", "2"], width=640, height=360),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=detect,
            scale=2,
        )
        halved = ((0, 0, 319, 179), (160, 90, 161, 91))
        assert rows == [(1.0, 2, 10.0, halved), (2.0, 2, 250.0, halved)]
        assert [c.shape for c in calls] == [(2, 360, 640)]
        assert int(calls[0][1, 359, 639]) == 250  # the whole Y plane of the larger frame, not the chroma after it

    def test_a_frame_with_no_one_or_several_boxes_keeps_its_own_boxes_and_their_count(self):
        # The count rule J reads and the positions the next rules read come from one detection call and must agree,
        # frame by frame: a row's second field is the length of its fourth.
        per_frame = [(), (BRIGHT_BOXES[0],), BRIGHT_BOXES]

        rows = frames.run_decode(
            _fake_ffmpeg([10, 20, 30], ["1", "2", "3"]),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=lambda planes: per_frame[: len(planes)],
            chunk_frames=3,
        )
        assert [(row[1], row[3]) for row in rows] == [(0, ()), (1, (BRIGHT_BOXES[0],)), (3, BRIGHT_BOXES)]
        assert all(row[1] == len(row[3]) for row in rows)

    def test_a_detector_that_answers_lists_gives_rows_of_tuples(self):
        # The helper's JSON answers arrive as lists; rows must hold tuples, so a row can be a dict key, compared and
        # stored without a copy of its own.
        rows = frames.run_decode(
            _fake_ffmpeg([10], ["1"]),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=lambda planes: [[[10, 20, 30, 40]]] * len(planes),
        )
        assert rows == [(1.0, 1, 10.0, ((10, 20, 30, 40),))]
        assert isinstance(rows[0][3], tuple) and isinstance(rows[0][3][0], tuple)

    def test_luma_is_the_mean_rounded_to_a_tenth(self):
        # 36 % of the pixels at 34 and the rest at 33 average 33.36: only rounding to a tenth gives 33.4 (a uniform
        # plane's mean is already whole, so it can't tell).
        script = textwrap.dedent("""
            import sys
            sys.stderr.write("[Parsed_showinfo_3 @ 0x1] n:0 pts:0 pts_time:1 duration:1\\n")
            sys.stdout.buffer.write(bytes([34]) * 20736 + bytes([33]) * (57600 - 20736) + bytes([128]) * 28800)
        """)
        rows = frames.run_decode(
            [sys.executable, "-c", script], hw_active=False, pts_offset_s=0.0, detect_boxes=lambda p: [()]
        )
        assert rows == [(1.0, 0, 33.4, ())]

    def test_a_frame_without_a_timestamp_drops_its_own_row_only(self):
        # A NOPTS line in the middle: the frames after it must keep their own timestamps. Shifting them would move a
        # box count onto an earlier second and store a credits start there.
        calls: list[np.ndarray] = []
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250, 250], ["100", "NOPTS", "106"]),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=_detector(calls),
            chunk_frames=1,
        )
        assert rows == [(100.0, 0, 10.0, ()), (106.0, 3, 250.0, BRIGHT_BOXES)]

    def test_several_frames_without_timestamps_drop_only_their_own_rows(self):
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250, 250, 250, 5], ["100", "NOPTS", "104", "NOPTS", "108"]),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=_detector([]),
            chunk_frames=2,
        )
        assert rows == [(100.0, 0, 10.0, ()), (104.0, 3, 250.0, BRIGHT_BOXES), (108.0, 0, 5.0, ())]

    def test_a_partial_trailing_frame_is_ignored(self):
        rows = frames.run_decode(
            _fake_ffmpeg([10], ["1"], extra_bytes=1000),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=lambda p: [()] * len(p),
        )
        assert rows == [(1.0, 0, 10.0, ())]

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
                detect_boxes=lambda p: [()] * len(p),
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
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )
        assert type(excinfo.value) is error

    # What jellyfin-ffmpeg 8.1.2 says for a 10-bit AV1 file on an NVIDIA GPU without AV1 decode (Pascal here, Turing
    # on sflix: 21 Bridges, 2026-09-25), the input and the lines before its end kept.
    AV1_ON_A_GPU_WITHOUT_AV1 = (
        "Input #0, matroska,webm, from '/media/21 Bridges (2019).mkv':\n"
        "  Stream #0:0(eng): Video: av1 (libdav1d) (Main), yuv420p10le(tv, bt2020nc/bt2020/smpte2084), 3840x1600\n"
        "Stream mapping:\n"
        "  Stream #0:0 -> #0:0 (av1 (native) -> rawvideo (native))\n"
        "[av1 @ 0x5c0071e12a80] Hardware is lacking required capabilities\n"
        "[av1 @ 0x5c0071e12a80] Failed setup for format cuda: hwaccel initialisation returned error.\n"
        "[av1 @ 0x5c0071e12a80] Your platform doesn't support hardware accelerated AV1 decoding.\n"
        "[vist#0:0/av1 @ 0x5c0071e10e40] [dec:av1 @ 0x5c0071e12300] Decode error rate 1 exceeds maximum 0.666667\n"
        "[vist#0:0/av1 @ 0x5c0071e10e40] [dec:av1 @ 0x5c0071e12300] Terminating thread with return code -22 "
        "(Invalid argument)\n"
        "[out#0/rawvideo @ 0x5acf76549a40] Nothing was written into output file, because at least one of its streams "
        "received no packets.\n"
        "frame=    0 fps=0.0 q=0.0 Lsize=       0KiB time=N/A bitrate=N/A speed=N/A elapsed=0:00:08.64    \n"
        "Conversion failed!\n"
    )

    @pytest.mark.parametrize(
        ("exit_code", "stderr", "message"),
        [
            # No exit code after the decoder's verdict: a run stopped at it would say the kill's.
            (69, AV1_ON_A_GPU_WITHOUT_AV1, "the GPU can't decode this file's AV1 video"),
            (
                69,
                "[h264 @ 0x1] Decode error rate 1 exceeds maximum 0.666667\nConversion failed!\n",
                "the GPU can't decode this file's video (ffmpeg exited 69)",
            ),
            (
                251,
                "[hevc @ 0x1] Failed to sync surface 0x5 (operation failed).\n"
                "[vist#0:0/hevc @ 0x2] Decoding error: Input/output error\nConversion failed!\n",
                "the GPU's decoder hit a hardware or driver error (ffmpeg exited 251)",
            ),
            (
                251,
                "[matroska,webm @ 0x1] Read error at pos. 5234901 (0x4fe0d5)\n"
                "[in#0/matroska,webm @ 0x2] Error during demuxing: Input/output error\nConversion failed!\n",
                "ffmpeg couldn't read the file (an I/O error, ffmpeg exited 251); if this keeps happening, check the "
                "disk or network share it is on",
            ),
            (
                3,
                "[Parsed_scale_0 @ 0x1] Error while filtering: Cannot allocate memory\nConversion failed!\n",
                "ffmpeg exited 3 decoding Movie.mkv on the GPU: Error while filtering: Cannot allocate memory",
            ),
        ],
        ids=["codec-av1", "codec-unnamed", "hwaccel", "io-error", "anything-else"],
    )
    def test_a_gpu_failure_names_the_cause_previews_gives_it(self, exit_code, stderr, message):
        # The worker's CPU rerun shows this as its reason: the last 300 characters of stderr said "inating thread with
        # return code -22 ... Conversion failed!" for a GPU that can't decode AV1.
        with pytest.raises(GpuDecodeError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], [], exit_code=exit_code, stderr_tail=stderr),
                hw_active=True,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )
        assert str(excinfo.value) == message

    def test_a_gpu_decode_killed_by_a_signal_says_so(self):
        # An OOM kill, say: the CPU rerun is previews' answer to it too.
        command = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
        with pytest.raises(GpuDecodeError) as excinfo:
            frames.run_decode(command, hw_active=True, pts_offset_s=0.0, detect_boxes=lambda p: [()] * len(p))
        assert str(excinfo.value) == "ffmpeg was stopped by a signal (exit -9)"
        assert excinfo.value.stderr_tail == ()

    def test_a_gpu_failure_keeps_ffmpegs_last_lines_that_say_why(self):
        # Production: 53 Intel reads fell back to the CPU with only the classified reason logged; ffmpeg's own lines
        # went to DEBUG. Its closing lines (the progress line, "Terminating thread", "Nothing was written",
        # "Conversion failed!") end every failed run and say nothing.
        with pytest.raises(GpuDecodeError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], [], exit_code=69, stderr_tail=self.AV1_ON_A_GPU_WITHOUT_AV1),
                hw_active=True,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )
        assert excinfo.value.stderr_tail == (
            "[av1 @ 0x5c0071e12a80] Hardware is lacking required capabilities",
            "[av1 @ 0x5c0071e12a80] Failed setup for format cuda: hwaccel initialisation returned error.",
            "[av1 @ 0x5c0071e12a80] Your platform doesn't support hardware accelerated AV1 decoding.",
            "[vist#0:0/av1 @ 0x5c0071e10e40] [dec:av1 @ 0x5c0071e12300] Decode error rate 1 exceeds maximum 0.666667",
        )

    def test_a_long_line_of_ffmpegs_is_cut_and_only_the_last_few_are_kept(self):
        stderr = "".join(f"[hevc @ 0x1] error {n} " + "x" * 400 + "\n" for n in range(9))
        with pytest.raises(GpuDecodeError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], [], exit_code=251, stderr_tail=stderr),
                hw_active=True,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
            )
        tail = excinfo.value.stderr_tail
        assert [line[:21] for line in tail] == [f"[hevc @ 0x1] error {n} " for n in range(5, 9)]
        assert all(len(line) <= frames.STDERR_LINE_CHARS for line in tail)

    def test_ffmpegs_lines_are_found_on_the_error_a_gpu_failure_was_raised_from(self):
        failure = GpuDecodeError("ffmpeg exited 251", stderr_tail=("[hevc @ 0x1] Failed to sync surface",))
        try:
            raise RuntimeError("the worker's CPU rerun") from failure
        except RuntimeError as wrapped:
            assert frames.gpu_failure_lines(wrapped) == ("[hevc @ 0x1] Failed to sync surface",)
        assert frames.gpu_failure_lines(failure) == ("[hevc @ 0x1] Failed to sync surface",)
        assert frames.gpu_failure_lines(RuntimeError("no ffmpeg ran")) == ()
        assert frames.gpu_failure_lines(frames.GpuReadNothingError()) == ()

    LONG_REASON = "Invalid data found when processing input" + " while reading the header of the file" * 8
    # showinfo's per-frame lines, the second of which reads as an error to a summary ("unknown").
    SHOWINFO = (
        "[Parsed_showinfo_2 @ 0x1] n:   0 pts:      0 pts_time:0       duration:1\n"
        "[Parsed_showinfo_2 @ 0x1]   color_range:tv color_space:unknown color_primaries:unknown color_trc:unknown\n"
    ) * 10

    @pytest.mark.parametrize(
        ("stderr", "error"),
        [
            (f"{SHOWINFO}[in#0 @ 0x55d2] Error opening input: {LONG_REASON}\n", f"Error opening input: {LONG_REASON}"),
            (
                f"{SHOWINFO}[vist#0:0/h264 @ 0x1] [dec:h264 @ 0x2] Decoding error: Invalid data found when processing "
                "input\nConversion failed!\n",
                "Decoding error: Invalid data found when processing input",
            ),
        ],
        ids=["longer-than-300-characters", "no-line-starts-with-error"],
    )
    def test_a_cpu_failure_quotes_ffmpegs_whole_line(self, stderr, error):
        # Never cut mid-word: the error line ffmpeg ends on is quoted whole, however long, without its "[x @ 0x…]", and
        # neither showinfo's lines nor the "Conversion failed!" every failed run ends on stand in for it.
        with pytest.raises(FrameDecodeError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], [], exit_code=1, stderr_tail=stderr),
                hw_active=False,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )
        assert str(excinfo.value) == f"ffmpeg exited 1 decoding Movie.mkv on the CPU: {error}"

    def test_no_frames_on_the_gpu_says_so_without_blaming_the_gpu(self):
        # A GPU failure to anything that meets it, but its own kind: the credit text detector reads the file on the CPU
        # to tell a GPU that missed the frames from a file that has none there.
        with pytest.raises(GpuReadNothingError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], []), hw_active=True, pts_offset_s=0.0, detect_boxes=lambda p: [()] * len(p)
            )
        assert isinstance(excinfo.value, GpuDecodeError)
        assert str(excinfo.value) == "the GPU read no frames in that part of the file"

    def test_no_frames_on_the_cpu_is_an_empty_answer(self):
        assert (
            frames.run_decode(
                _fake_ffmpeg([], []), hw_active=False, pts_offset_s=0.0, detect_boxes=lambda p: [()] * len(p)
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
            return [()] * len(planes)

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
                    detect_boxes=count,
                    cancel_check=cancelled.is_set,
                    chunk_frames=2,
                )
            assert len(calls) == 1 and time.monotonic() - started < 5
            _assert_gone(pid_file)
            _assert_gone(child_pid_file, within_s=5)
        finally:
            if child_pid_file.exists() and not _gone(int(child_pid_file.read_text())):
                os.kill(int(child_pid_file.read_text()), signal.SIGKILL)

    @pytest.mark.parametrize("hw", [True, False], ids=["gpu", "cpu"])
    @pytest.mark.parametrize("frames_left", [50, 3], ids=["mid-decode", "last-chunk"])
    def test_a_cancel_during_a_slow_text_detection_call_kills_ffmpeg_at_once(self, tmp_path, hw, frames_left):
        # Lab phase 3 row 6: a fresh app's first text detection request waits 10-14 s for its helper to start and
        # self-test. A cancel landing then must stop ffmpeg within a poll, not after the request, and the decode ends
        # as cancelled (never as a GPU failure, which would read the file again on the CPU). "last-chunk": ffmpeg has
        # written its last frames, so the decode is in its final, partial text detection call when the cancel lands.
        cancelled, in_detection, let_go = threading.Event(), threading.Event(), threading.Event()
        pid_file = tmp_path / "ffmpeg.pid"
        calls: list[int] = []

        def helper_starting(planes):
            calls.append(len(planes))
            in_detection.set()
            let_go.wait(20)
            return [()] * len(planes)

        outcome: dict = {}

        def decode():
            try:
                frames.run_decode(
                    _fake_ffmpeg(
                        [10] * frames_left,
                        ["1"] * frames_left,
                        sleep_s=0.05,
                        pid_file=str(pid_file),
                        linger_s=30,
                        close_stdout=frames_left < 4,  # the last frames are out: ffmpeg lingers as it exits
                    ),
                    hw_active=hw,
                    pts_offset_s=0.0,
                    detect_boxes=helper_starting,
                    cancel_check=cancelled.is_set,
                    chunk_frames=4,
                )
            except BaseException as exc:  # noqa: BLE001 - the test reads what ended the decode
                outcome["error"] = exc

        worker = threading.Thread(target=decode, daemon=True)
        worker.start()
        try:
            assert in_detection.wait(10)
            cancelled.set()
            _assert_gone(pid_file, within_s=2)  # while text detection is still busy
            assert worker.is_alive() and calls == [calls[0]]
        finally:
            let_go.set()
            worker.join(10)
        assert isinstance(outcome.get("error"), DecodeCancelledError), outcome
        assert len(calls) == 1  # nothing more is sent to text detection after the cancel

    def test_a_decode_that_ends_without_a_cancel_starts_no_kill(self, tmp_path):
        # The watcher only acts on a cancel: a decode that finishes on its own keeps its rows and exit code.
        before = set(threading.enumerate())
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250], ["1", "2"]), hw_active=True, pts_offset_s=0.0, detect_boxes=_detector([]),
            cancel_check=lambda: False, chunk_frames=4,
        )  # fmt: skip
        assert [row[:2] for row in rows] == [(1.0, 0), (2.0, 3)]
        assert not [t for t in set(threading.enumerate()) - before if t.name == "credits-cancel" and t.is_alive()]

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
                    detect_boxes=lambda p: [()] * len(p),
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
                detect_boxes=lambda p: [()] * len(p),
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
                detect_boxes=lambda p: [()] * len(p),
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
                    detect_boxes=lambda p: [()] * len(p),
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
                    detect_boxes=lambda p: [()] * len(p),
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
            return [()] * len(planes)

        with pytest.raises(DecodeCancelledError):
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50),
                hw_active=False,
                pts_offset_s=0.0,
                detect_boxes=count,
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
            return [()] * len(planes)

        rows = frames.run_decode(
            _fake_ffmpeg([10] * 100, [str(i) for i in range(100)], progress_file=str(progress)),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=count,
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
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )
        assert type(excinfo.value) is FrameDecodeError
        assert missing in str(excinfo.value) and "Movie.mkv" in str(excinfo.value)

    def test_rows_are_seconds_from_the_start_of_the_file(self):
        # A recorded .ts reports pts from its PCR base; rule J and the published marker need file seconds.
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250], ["30020.5", "30021.5"]),
            hw_active=False,
            detect_boxes=lambda p: [()] * len(p),
            pts_offset_s=30000.0,
        )
        assert [r[0] for r in rows] == [20.5, 21.5]

    def test_the_timestamp_offset_has_to_be_given(self):
        # No default: a caller that forgets it would silently publish a recording's raw container timestamps.
        with pytest.raises(TypeError, match="pts_offset_s"):
            frames.run_decode(_fake_ffmpeg([10], ["1"]), hw_active=False, detect_boxes=lambda p: [()] * len(p))

    def test_slow_text_detection_never_loses_the_end_of_the_stream(self):
        def slow(planes):
            time.sleep(1.5)
            return [()] * len(planes)

        started = time.monotonic()
        rows = frames.run_decode(
            _fake_ffmpeg([10, 10, 10], ["1", "2", "3"]),
            hw_active=False,
            pts_offset_s=0.0,
            detect_boxes=slow,
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
            return [()] * len(planes)

        started = time.monotonic()
        rows = frames.run_decode(
            _fake_ffmpeg([10] * count, [str(i) for i in range(count)]),
            hw_active=True,
            pts_offset_s=0.0,
            detect_boxes=slow,
            timeout_s=20,
        )
        assert len(rows) == count
        assert time.monotonic() - started < 1.2 * count / 64 + 6

    def test_a_failing_text_detection_kills_ffmpeg_and_propagates(self, tmp_path):
        pid_file = tmp_path / "ffmpeg.pid"

        def count(planes):
            raise RuntimeError("helper gone")

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="helper gone"):
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.2, pid_file=str(pid_file)),
                hw_active=False,
                pts_offset_s=0.0,
                detect_boxes=count,
                chunk_frames=1,
            )
        assert time.monotonic() - started < 5
        _assert_gone(pid_file)

    @pytest.mark.parametrize(
        "answer",
        [
            [3],  # a count, as text detection answered before positions
            [[[10, 20, 30]]],  # three numbers in a box
            [[[10, 20, 30, "x"]]],  # a corner that isn't a number
        ],
    )
    def test_boxes_that_are_not_four_numbers_each_are_a_decode_error(self, answer, tmp_path):
        # detect_credits_text only handles this module's own errors, so a detector answering something else has to
        # come back as one of them rather than a bare TypeError nothing catches.
        pid_file = tmp_path / "ffmpeg.pid"
        with pytest.raises(FrameDecodeError, match="aren't four numbers each") as excinfo:
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.2, pid_file=str(pid_file)),
                hw_active=True,
                pts_offset_s=0.0,
                detect_boxes=lambda p: answer * len(p),
                chunk_frames=1,
            )
        assert type(excinfo.value) is FrameDecodeError
        _assert_gone(pid_file)

    def test_a_frames_boxes_per_frame_are_required(self, tmp_path):
        # A helper answering for the wrong number of frames would shift every later row's boxes onto another frame's
        # pts.
        pid_file = tmp_path / "ffmpeg.pid"
        with pytest.raises(FrameDecodeError, match="answered 1 frames' boxes for 2 frames") as excinfo:
            frames.run_decode(
                _fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.2, pid_file=str(pid_file)),
                hw_active=True,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()],
                chunk_frames=2,
            )
        assert type(excinfo.value) is FrameDecodeError
        _assert_gone(pid_file)


VERDICT = "Your platform doesn't support hardware accelerated AV1 decoding."
# The hwaccel line FFmpeg prints for any decoder whose GPU setup failed; healthy H.264/HEVC runs print it and decode on.
GENERIC_HWACCEL_LINE = "[h264 @ 0x1] Failed setup for format {hwaccel}: hwaccel initialisation returned error.\n"
VENDORS = [("NVIDIA", "cuda:0", "cuda"), ("INTEL", RENDER, "vaapi"), ("AMD", "/dev/dri/renderD129", "vaapi")]


def _cant_decode_lines(hwaccel: str, packets: int = 3) -> str:
    """What ffmpeg's own AV1 decoder says, once per packet to the end of the file, on a GPU without AV1 decode
    (ffmpeg 8.0.1 on a Quadro P5000, 2026-10-02: 6 lines a packet, 3,756 lines in 1.5 s; "Hardware is lacking" is
    NVDEC's own line and is left out)."""
    packet = (
        f"[av1 @ 0x58ba5204abc0] Failed setup for format {hwaccel}: hwaccel initialisation returned error.\n"
        f"[av1 @ 0x58ba5204abc0] {VERDICT}\n"
        "[av1 @ 0x58ba5204abc0] Failed to get pixel format.\n"
        "[av1 @ 0x58ba5204abc0] Get current frame error\n"
        "[vist#0:0/av1 @ 0x58ba520356c0] [dec:av1 @ 0x58ba5204a680] Error submitting packet to decoder: Function not "
        "implemented\n"
    )
    return "  Stream #0:0: Video: av1 (libdav1d) (Main), yuv420p(tv), 1280x720\n" + packet * packets


def _hw(gpu: str | None, device: str | None) -> bool:
    """Whether the worker's own command decodes on the GPU."""
    return frames.decode_command(
        FF, MOVIE, start_s=0.0, length_s=None, keyframes_only=True, fps=None, gpu=gpu, gpu_device_path=device
    )[1]


class TestAGpuThatCantDecodeTheFile:
    """Previews' rule (``ffmpeg_runner``): a GPU run is stopped at the decoder's first verdict that it can't decode the
    file, before any frame; never a CPU run, never on the generic hwaccel line, never once a frame has arrived."""

    @pytest.mark.parametrize(("gpu", "device", "hwaccel"), VENDORS, ids=[v[0] for v in VENDORS])
    def test_a_gpu_run_is_stopped_at_the_verdict_and_is_a_gpu_failure(self, gpu, device, hwaccel, tmp_path):
        # Production: the end-picture check's GPU decode of an AV1 episode failed on every packet to its 120 s timeout
        # on a TITAN RTX (Person of Interest S02E19, 2026-10-02); the frames after the wait stand for a file walked on.
        pid_file = tmp_path / "ffmpeg.pid"
        started = time.monotonic()
        with pytest.raises(GpuDecodeError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([10, 250], ["1", "2"], stderr_head=_cant_decode_lines(hwaccel), head_wait_s=30,
                             pid_file=str(pid_file)),
                hw_active=_hw(gpu, device),
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )  # fmt: skip
        assert time.monotonic() - started < 5
        assert type(excinfo.value) is GpuDecodeError  # the worker's CPU rerun, not "the GPU read no frames"
        assert str(excinfo.value) == "the GPU can't decode this file's AV1 video"
        assert any(VERDICT in line for line in excinfo.value.stderr_tail)
        _assert_gone(pid_file)

    @pytest.mark.parametrize("linger_s", [0, 30], ids=["ffmpeg-ended-first", "stopped"])
    def test_the_failure_reads_the_same_whether_ffmpeg_ended_or_was_stopped(self, linger_s):
        # A short tail ends by itself (exit 69) before the first look at stderr; which comes first is timing.
        with pytest.raises(GpuDecodeError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], [], exit_code=69, stderr_tail=TestRunDecode.AV1_ON_A_GPU_WITHOUT_AV1,
                             linger_s=linger_s),
                hw_active=True,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )  # fmt: skip
        assert str(excinfo.value) == "the GPU can't decode this file's AV1 video"
        assert excinfo.value.stderr_tail == (
            "[av1 @ 0x5c0071e12a80] Hardware is lacking required capabilities",
            "[av1 @ 0x5c0071e12a80] Failed setup for format cuda: hwaccel initialisation returned error.",
            "[av1 @ 0x5c0071e12a80] Your platform doesn't support hardware accelerated AV1 decoding.",
            "[vist#0:0/av1 @ 0x5c0071e10e40] [dec:av1 @ 0x5c0071e12300] Decode error rate 1 exceeds maximum 0.666667",
        )

    @pytest.mark.parametrize("said", ["generic", "nothing"])
    @pytest.mark.parametrize(("gpu", "device", "hwaccel"), VENDORS, ids=[v[0] for v in VENDORS])
    def test_a_gpu_run_without_the_verdict_runs_on(self, gpu, device, hwaccel, said):
        # 0.4 s without a frame is four looks at stderr: the generic line alone stops nothing.
        head = GENERIC_HWACCEL_LINE.format(hwaccel=hwaccel) if said == "generic" else ""
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250], ["1", "2"], stderr_head=head, head_wait_s=0.4),
            hw_active=_hw(gpu, device),
            pts_offset_s=0.0,
            detect_boxes=_detector([]),
        )
        assert [row[0] for row in rows] == [1.0, 2.0]

    @pytest.mark.parametrize(("gpu", "device", "hwaccel"), VENDORS, ids=[v[0] for v in VENDORS])
    def test_a_gpu_run_that_already_gave_frames_is_never_stopped(self, gpu, device, hwaccel):
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250], ["1", "2"], stderr_tail=_cant_decode_lines(hwaccel), linger_s=0.4),
            hw_active=_hw(gpu, device),
            pts_offset_s=0.0,
            detect_boxes=_detector([]),
        )
        assert [row[0] for row in rows] == [1.0, 2.0]

    # One row per thing said: a CPU run's command names no vendor (``decode_command`` with no GPU), on a CPU worker
    # and on a GPU worker's CPU rerun alike.
    @pytest.mark.parametrize(
        "head",
        [_cant_decode_lines("cuda"), _cant_decode_lines("vaapi"), GENERIC_HWACCEL_LINE.format(hwaccel="cuda"), ""],
        ids=["verdict-cuda", "verdict-vaapi", "generic", "nothing"],
    )
    def test_a_cpu_run_is_never_stopped(self, head):
        rows = frames.run_decode(
            _fake_ffmpeg([10, 250], ["1", "2"], stderr_head=head, head_wait_s=0.4),
            hw_active=_hw(None, None),
            pts_offset_s=0.0,
            detect_boxes=_detector([]),
        )
        assert [row[0] for row in rows] == [1.0, 2.0]

    @pytest.mark.parametrize("before", [frames._STDERR_PEEK_BYTES - 30, frames._STDERR_PEEK_BYTES + 40_007])
    def test_the_verdict_is_found_wherever_it_falls_in_what_ffmpeg_wrote(self, before, tmp_path):
        # Across the edge of one read, and in a later read of the same look.
        pid_file = tmp_path / "ffmpeg.pid"
        head = "x" * (before - 1) + "\n" + _cant_decode_lines("cuda")
        with pytest.raises(GpuDecodeError, match="can't decode this file's AV1 video"):
            frames.run_decode(
                _fake_ffmpeg([], [], stderr_head=head, head_wait_s=30, pid_file=str(pid_file)),
                hw_active=True,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
                timeout_s=5,
            )
        _assert_gone(pid_file)

    def test_a_verdict_still_being_written_is_found_on_the_next_look(self, tmp_path):
        said = VERDICT.encode()
        with open(tmp_path / "stderr", "w+b") as stderr_file:
            stderr_file.write(b"Stream mapping:\n" + said[:20])
            stderr_file.flush()
            found, seen = frames._said_cant_decode(stderr_file, 0)
            assert found is False
            stderr_file.write(said[20:] + b"\n")
            stderr_file.flush()
            assert frames._said_cant_decode(stderr_file, seen)[0] is True
            # ffmpeg writes through the same open file: a look must leave its position alone.
            assert stderr_file.tell() == len(b"Stream mapping:\n" + said + b"\n")

    # What FFmpeg said in production for a video track with no decoder at all (previews' failure log; the output is
    # this decode's pipe).
    NO_DECODER = (
        "[vist#0:0/none @ 0x64271d086700] Decoding requested, but no decoder found for: none\n"
        "Error opening output file -.\n"
        "Error opening output files: Invalid argument\n"
    )

    @pytest.mark.parametrize("hw", [True, False], ids=["gpu", "cpu"])
    def test_a_video_no_device_can_decode_is_the_files_failure_on_either_worker(self, hw):
        # Previews' rule: a CPU rerun would fail the same way, so a GPU worker's failure isn't a GpuDecodeError.
        with pytest.raises(FrameDecodeError) as excinfo:
            frames.run_decode(
                _fake_ffmpeg([], [], exit_code=234, stderr_tail=self.NO_DECODER),
                hw_active=hw,
                pts_offset_s=0.0,
                detect_boxes=lambda p: [()] * len(p),
                name="Movie.mkv",
            )
        assert type(excinfo.value) is frames.NoDecoderError
        assert str(excinfo.value) == "This file's video can't be decoded by any device (unknown or protected codec)"


def _state(pid: int) -> str:
    return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split(" ", 1)[0]


def _progress(path: pathlib.Path) -> int:
    try:
        return int(path.read_text() or 0)
    except (FileNotFoundError, ValueError):
        return 0


@pytest.fixture
def group_signals(monkeypatch):
    """The signals sent to a process group by the pause, still sent."""
    from media_preview_generator.markers import freeze

    sent: list[tuple[int, int]] = []
    real = os.killpg

    def killpg(pgid, sig):
        if sig in (signal.SIGSTOP, signal.SIGCONT):
            sent.append((pgid, sig))
        real(pgid, sig)

    monkeypatch.setattr(freeze.os, "killpg", killpg)
    return sent


class TestRunDecodePause:
    """Pause all, quiet hours and a schedule's stop time freeze the running decode where it is, as previews' FFmpeg."""

    def _decode_in_background(self, command, **kwargs) -> tuple[threading.Thread, dict]:
        out: dict = {}

        def run():
            try:
                out["rows"] = frames.run_decode(command, hw_active=False, pts_offset_s=0.0, **kwargs)
            except BaseException as exc:
                out["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, out

    def test_a_pause_stops_ffmpegs_group_and_the_resume_goes_on_with_the_deadline_moved_out(
        self, tmp_path, group_signals
    ):
        pid_file, progress = tmp_path / "ffmpeg.pid", tmp_path / "progress"
        paused = threading.Event()
        chunks: list[int] = []

        def detect(planes):
            chunks.append(len(planes))
            if len(chunks) == 1:
                paused.set()  # everything is paused as the first chunk is read
            return [()] * len(planes)

        # 20 frames over about 1 s of work; the pause lasts longer than the whole time limit.
        thread, out = self._decode_in_background(
            _fake_ffmpeg([10] * 20, ["1"] * 20, sleep_s=0.05, pid_file=str(pid_file), progress_file=str(progress)),
            detect_boxes=detect, pause_check=paused.is_set, chunk_frames=2, timeout_s=2.5,
        )  # fmt: skip
        try:
            assert _wait_for(paused.is_set, within_s=5)
            pid = int(pid_file.read_text())
            assert _wait_for(lambda: _state(pid) == "T", within_s=5)
            frozen_at = _progress(progress)
            time.sleep(3.0)  # past the 2.5 s limit
            assert _state(pid) == "T" and _progress(progress) == frozen_at
            assert thread.is_alive()
        finally:
            paused.clear()
            thread.join(10)
        assert "error" not in out, out.get("error")
        assert len(out["rows"]) == 20  # every frame, nothing lost to the pause or to the time limit
        assert group_signals == [(pid, signal.SIGSTOP), (pid, signal.SIGCONT)]

    def test_a_cancel_while_paused_kills_the_frozen_group(self, tmp_path, group_signals):
        pid_file = tmp_path / "ffmpeg.pid"
        paused, cancelled = threading.Event(), threading.Event()

        def detect(planes):
            paused.set()
            return [()] * len(planes)

        thread, out = self._decode_in_background(
            _fake_ffmpeg([10] * 50, ["1"] * 50, sleep_s=0.1, pid_file=str(pid_file)),
            detect_boxes=detect, pause_check=paused.is_set, cancel_check=cancelled.is_set, chunk_frames=2,
        )  # fmt: skip
        assert _wait_for(paused.is_set, within_s=5)
        pid = int(pid_file.read_text())
        assert _wait_for(lambda: _state(pid) == "T", within_s=5)
        started = time.monotonic()
        cancelled.set()
        thread.join(10)
        assert isinstance(out.get("error"), DecodeCancelledError)
        assert time.monotonic() - started < 3
        _assert_gone(pid_file, within_s=5)
        # Stopped by the pause, then killed as it stood: the cancel watcher kills the frozen group directly (SIGKILL
        # reaches a stopped process), so no SIGCONT has to come first.
        assert group_signals[0] == (pid, signal.SIGSTOP)

    def test_no_ffmpeg_starts_while_paused(self, tmp_path):
        pid_file = tmp_path / "ffmpeg.pid"
        paused = threading.Event()
        paused.set()
        thread, out = self._decode_in_background(
            _fake_ffmpeg([10, 250], ["1", "2"], pid_file=str(pid_file)),
            detect_boxes=lambda planes: [()] * len(planes), pause_check=paused.is_set, timeout_s=1.0,
        )  # fmt: skip
        time.sleep(1.5)  # past the time limit: it starts counting once the decode starts
        assert not pid_file.exists() and thread.is_alive()
        paused.clear()
        thread.join(10)
        assert "error" not in out, out.get("error")
        assert [row[2] for row in out["rows"]] == [10.0, 250.0]

    def test_a_cancel_before_the_start_while_paused_starts_nothing(self, tmp_path):
        pid_file = tmp_path / "ffmpeg.pid"
        with pytest.raises(DecodeCancelledError):
            frames.run_decode(
                _fake_ffmpeg([10], ["1"], pid_file=str(pid_file)), hw_active=False, pts_offset_s=0.0,
                detect_boxes=lambda planes: [()] * len(planes), pause_check=lambda: True, cancel_check=lambda: True,
            )  # fmt: skip
        assert not pid_file.exists()


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
            return [(1.0, 0, 10.0, ())]

        monkeypatch.setattr(frames, "run_decode", fake_run)

        def count(planes):
            return [()] * len(planes)

        def cancel():
            return False

        def paused():
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
            detect_boxes=count,
            cancel_check=cancel,
            pause_check=paused,
            timeout_s=42.0,
            start_time_s=0.0,
            download_format="p010le",
            ffmpeg_threads=3,
        )
        expected_command, _ = frames.decode_command(
            FF, MOVIE, start_s=5680.5, length_s=21.0, keyframes_only=False, fps=1, gpu=gpu, gpu_device_path=device,
            download_format="p010le", ffmpeg_threads=3,
        )  # fmt: skip
        assert ("-threads" in seen["command"]) is hw  # the worker's threads reached the command it runs
        assert rows == [(1.0, 0, 10.0, ())]
        assert seen == {
            "command": expected_command,
            "hw_active": hw,
            "detect_boxes": count,
            "cancel_check": cancel,
            "pause_check": paused,
            "timeout_s": 42.0,
            "pts_offset_s": 0.0,
            "name": "Movie.mkv",
            "scale": 1,
            "chunk_frames": frames.CHUNK_FRAMES,
        }

    @pytest.mark.parametrize(("gpu", "device"), [("NVIDIA", "cuda:0"), (None, None)], ids=["gpu", "cpu"])
    def test_the_scale_reaches_both_the_command_and_the_frame_reader(self, monkeypatch, gpu, device):
        # Half of it alone breaks the decode: a 640x360 command read as 320x180 frames, or the other way round.
        seen: dict = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            seen.update(kwargs)
            return []

        monkeypatch.setattr(frames, "run_decode", fake_run)
        frames.decode_rows(MOVIE, ffmpeg=FF, start_s=5100.0, length_s=None, keyframes_only=True, fps=None, gpu=gpu,
                           gpu_device_path=device, detect_boxes=lambda planes: [], start_time_s=0.0, scale=2,
                           download_format="nv12")  # fmt: skip
        expected_command, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True,
                                                    fps=None, gpu=gpu, gpu_device_path=device, scale=2,
                                                    download_format="nv12")  # fmt: skip
        assert (seen["command"], seen["scale"]) == (expected_command, 2)
        # A quarter of the frames per text detection request: the same pixels, so the helper's per-request timeout
        # (sized for 64 frames at 320x180) holds at 640x360 too, and the queue holds the same bytes.
        assert seen["chunk_frames"] == frames.CHUNK_FRAMES // 4 == 16

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
            detect_boxes=lambda p: [()] * len(p),
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
            detect_boxes=lambda p: [()] * len(p),
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
                detect_boxes=lambda p: [()] * len(p),
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
            detect_boxes=lambda p: [()] * len(p),
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
                detect_boxes=lambda p: [()] * len(p),
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
        with pytest.raises(frames.ReadStalledError, match="could not read the start time of Recording.ts"):
            frames.container_start_s("/m/Recording.ts", FF)

    @pytest.mark.parametrize(
        ("keep_every", "drop_non_key", "bsf"),
        [(50, False, r"noise=drop=mod(n\,50)"), (None, True, "noise=drop=not(key)"),
         (50, True, r"noise=drop=not(key)+mod(n\,50)")],
    )  # fmt: skip
    def test_the_packet_drops_reach_the_command(self, monkeypatch, keep_every, drop_non_key, bsf):
        seen: dict = {}
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: seen.update(command=command) or [])
        frames.decode_rows(MOVIE, ffmpeg=FF, start_s=5100.0, length_s=None, keyframes_only=True, fps=None, gpu=None,
                           gpu_device_path=None, detect_boxes=lambda p: [()] * len(p), start_time_s=0.0,
                           keep_every=keep_every, drop_non_key=drop_non_key)  # fmt: skip
        expected, _ = frames.decode_command(FF, MOVIE, start_s=5100.0, length_s=None, keyframes_only=True, fps=None,
                                            gpu=None, gpu_device_path=None, keep_every=keep_every,
                                            drop_non_key=drop_non_key)  # fmt: skip
        assert seen["command"] == expected
        assert expected[expected.index("-bsf:V:0") + 1] == bsf


class TestReadTextAt:
    @pytest.mark.parametrize(
        ("gpu", "device", "hw"), [("NVIDIA", "cuda:0", True), (None, None, False)], ids=["gpu", "cpu"]
    )
    def test_one_second_at_the_read_size_on_the_workers_device_one_frame_per_request(
        self, monkeypatch, gpu, device, hw
    ):
        seen: dict = {}
        asked: list[tuple] = []

        def fake_run(command, **kwargs):
            seen["command"] = command
            seen.update(kwargs)
            assert kwargs["detect_boxes"](np.zeros((1, 720, 1280), np.uint8)) == [()]
            assert kwargs["detect_boxes"](np.zeros((1, 720, 1280), np.uint8)) == [()]
            return []

        def read_text(planes):
            asked.append(planes.shape)
            return [["THE INVESTIGATION", "IS NOW CLOSED."] if len(asked) == 1 else ["LATER"]]

        def cancel():
            return False

        monkeypatch.setattr(frames, "run_decode", fake_run)
        lines = frames.read_text_at(MOVIE, ffmpeg=FF, at_s=5692.0, scale=4, gpu=gpu, gpu_device_path=device,
                                    read_text=read_text, cancel_check=cancel, timeout_s=42.0, start_time_s=0.0,
                                    download_format="p010le", ffmpeg_threads=3)  # fmt: skip
        expected_command, _ = frames.decode_command(
            FF, MOVIE, start_s=5692.0, length_s=1.0, keyframes_only=False, fps=1, gpu=gpu, gpu_device_path=device,
            scale=4, download_format="p010le", ffmpeg_threads=3,
        )  # fmt: skip
        # The first frame of the second is the card's: a second frame (the next second's, when ffmpeg rounds) is never
        # read at all.
        assert lines == ["THE INVESTIGATION", "IS NOW CLOSED."]
        assert asked == [(1, 720, 1280)]
        seen.pop("detect_boxes")
        assert seen == {
            "command": expected_command,
            "hw_active": hw,
            "cancel_check": cancel,
            "pause_check": None,
            "timeout_s": 42.0,
            "pts_offset_s": 0.0,
            "name": "Movie.mkv",
            "scale": 4,
            "chunk_frames": 1,
        }

    def test_no_frame_there_reads_nothing(self, monkeypatch):
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: [])
        lines = frames.read_text_at(MOVIE, ffmpeg=FF, at_s=5692.0, scale=4, gpu=None, gpu_device_path=None,
                                    read_text=lambda planes: [["NEVER"]], start_time_s=0.0)  # fmt: skip
        assert lines == []

    def test_a_cancel_before_the_decode_starts_none(self, monkeypatch):
        monkeypatch.setattr(frames, "run_decode", lambda command, **kwargs: pytest.fail("decoded after a cancel"))
        with pytest.raises(frames.DecodeCancelledError):
            frames.read_text_at(MOVIE, ffmpeg=FF, at_s=5692.0, scale=4, gpu=None, gpu_device_path=None,
                                read_text=lambda planes: [], cancel_check=lambda: True, start_time_s=0.0)  # fmt: skip


def _packets(count: int, *, fps: float = 24.0, start_s: float = 0.0, keyframe: bool = True) -> list[VideoPacket]:
    return [VideoPacket(round(start_s + i / fps, 6), keyframe) for i in range(count)]


class TestKeyframeThinning:
    @pytest.fixture
    def probed(self, monkeypatch):
        """Every packet probe's arguments; it answers ``probed.codec`` and ``probed.packets`` (or raises
        ``probed.error``)."""

        class Probed(list):
            codec: str | None = "h264"
            pix_fmt: str | None = None
            packets: list[VideoPacket] = []
            error: Exception | None = None

        calls = Probed()

        def video_packets(path, **kwargs):
            calls.append({"path": path, **kwargs})
            if calls.error:
                raise calls.error
            return VideoPackets(calls.codec, tuple(calls.packets), calls.pix_fmt)

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

    @pytest.mark.parametrize(
        ("pix_fmt", "download_format"),
        [("yuv420p", "nv12"), ("nv12", "nv12"), ("yuv420p10le", "p010le"), ("p010le", "p010le"),
         # MJPEG's full-range 4:2:0: VAAPI's JPEG surfaces aren't reliably NV12, so ffmpeg downloads them itself.
         ("yuvj420p", None), ("yuv422p10le", None), ("yuv444p", None), ("yuv420p12le", None), ("gray", None),
         (None, None)],
    )  # fmt: skip
    def test_the_stream_names_the_format_its_gpu_surfaces_are_downloaded_in(self, probed, pix_fmt, download_format):
        # CUDA and VAAPI decode 8-bit 4:2:0 into NV12 surfaces and 10-bit 4:2:0 into P010; hwdownload has to be told
        # which. Anything else is left to ffmpeg to download (``frames.decode_command``). Read from the same ffprobe
        # as the packets: no second process per file.
        probed.pix_fmt, probed.packets = pix_fmt, self.NORMAL_GOP
        assert frames.keyframe_thinning(MOVIE, FF) == KeyframeThinning(None, False, download_format)
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
        assert frames.keyframe_thinning(MOVIE, FF) == KeyframeThinning(None, False, None)  # ffmpeg downloads frames
        assert "Couldn't read the video packets of Movie.mkv, so it is read the ordinary way" in loguru_caplog.text

    def test_a_probe_that_times_out_is_a_decode_timeout(self, probed):
        probed.error = ProbeTimeoutError("ffprobe failed for /media/Movie (2020)/Movie.mkv: TimeoutExpired")
        with pytest.raises(DecodeTimeoutError, match="reading the video packets of Movie.mkv timed out after 7 s"):
            frames.keyframe_thinning(MOVIE, FF, timeout_s=7.0)
        assert probed[0]["timeout_s"] == 7.0

    def test_a_probe_not_started_for_earlier_stuck_ones_is_no_answer_this_time(self, probed):
        # Not this file's fault, and not a timeout of its own: no answer this run, nothing recorded against it.
        probed.error = ProbeStalledError("Not reading x: 2 earlier ffprobes are still stuck reading their files")
        with pytest.raises(
            frames.ReadStalledError, match="could not read the video packets of Movie.mkv: Not reading x"
        ):
            frames.keyframe_thinning(MOVIE, FF)

    def test_a_cancelled_job_is_not_probed(self, probed):
        with pytest.raises(DecodeCancelledError, match="cancelled before decoding Movie.mkv"):
            frames.keyframe_thinning(MOVIE, FF, cancel_check=lambda: True)
        assert probed == []


class TestReadableVideo:
    """How far a file whose tail gave no frame can be read (a file cut short keeps its stated duration)."""

    @pytest.fixture
    def reads(self, monkeypatch):
        """``probe.last_video_time_s`` per call: the next of ``reads.answers`` (a time, None, or an error); the
        container's start time is ``reads.start`` (or an error), read by the real ``container_start_s`` rules."""

        class Reads(list):
            answers: list = []
            start: float | BaseException = 0.0
            starts: list = []

        calls = Reads()

        def last_video_time_s(path, **kwargs):
            calls.append({"path": path, **kwargs})
            answer = calls.answers[len(calls) - 1]
            if isinstance(answer, BaseException):
                raise answer
            return answer

        def container_start_s(path, ffmpeg, *, cancel_check=None, timeout_s):
            if cancel_check and cancel_check():
                raise DecodeCancelledError(f"cancelled before decoding {os.path.basename(path)}")
            calls.starts.append({"path": path, "ffmpeg": ffmpeg, "timeout_s": timeout_s})
            if isinstance(calls.start, BaseException):
                raise calls.start
            return calls.start

        calls.starts = []
        monkeypatch.setattr(frames, "last_video_time_s", last_video_time_s)
        monkeypatch.setattr(frames, "container_start_s", container_start_s)
        return calls

    def test_the_tail_is_read_from_where_it_starts(self, reads):
        reads.answers = [1630.2]
        assert readable_video_s(MOVIE, FF, from_s=2190.0) == 1630.2
        # The ffprobe beside the configured ffmpeg, the probe's short bound.
        assert reads == [{"path": MOVIE, "ffprobe": frames.ffprobe_path_for(FF), "from_s": 2190.0, "timeout_s": 30.0}]
        assert reads.starts == [{"path": MOVIE, "ffmpeg": FF, "timeout_s": 30.0}]

    def test_the_seek_is_on_the_streams_own_times(self, reads):
        # A recording whose times start 30000 s in: seeking to 2190 would read the whole file from its first packet.
        reads.start, reads.answers = 30000.0, [2230.5]
        assert readable_video_s(MOVIE, FF, from_s=2190.0) == 2230.5
        assert reads[0]["from_s"] == 32190.0

    def test_a_tail_read_that_finds_no_packet_reads_the_whole_file(self, reads):
        # An MP4 cut short: its index points past the end, so the seek into the tail reads nothing.
        reads.answers = [None, 45.2]
        assert readable_video_s(MOVIE, FF, from_s=2190.0, timeout_s=7.0) == 45.2
        assert [(call["from_s"], call["timeout_s"]) for call in reads] == [(2190.0, 7.0), (None, 7.0)]

    @pytest.mark.parametrize(
        ("start", "answers"),
        [
            (0.0, [None, None]),
            (0.0, [ProbeTimeoutError("ffprobe failed: TimeoutExpired")]),
            (0.0, [ProbeStalledError("Not reading x: 2 earlier ffprobes are still stuck")]),
            (0.0, [None, ProbeError("ffprobe exited 1")]),
            (DecodeTimeoutError("reading the start time of Movie.mkv timed out after 30 s"), []),
            (frames.ReadStalledError("could not read the start time of Movie.mkv"), []),
            (FrameDecodeError("could not read the start time of Movie.mkv: ffprobe exited 1"), []),
        ],
        ids=["no-packets", "timeout", "stalled", "failed", "start-timeout", "start-stalled", "start-failed"],
    )
    def test_what_cant_be_told_is_none(self, reads, start, answers):
        # Never a reason to call a file cut short: its tail is then "nothing found", as before.
        reads.start, reads.answers = start, answers
        assert readable_video_s(MOVIE, FF, from_s=2190.0) is None

    def test_a_cancelled_job_is_not_probed(self, reads):
        with pytest.raises(DecodeCancelledError, match="cancelled before decoding Movie.mkv"):
            readable_video_s(MOVIE, FF, from_s=2190.0, cancel_check=lambda: True)
        assert (list(reads), reads.starts) == ([], [])
