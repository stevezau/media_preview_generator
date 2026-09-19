"""Frames of a file's ending for credit text detection (spec §5.4): the keyframes of the tail, then one frame a second
just before the coarse answer. Decoded with the worker's GPU through the same hwaccel arguments as previews.

Only 320×180 NV12 leaves ffmpeg and only the Y plane is kept; each chunk of frames goes to text detection as it
arrives, so memory stays bounded whatever the file's keyframe spacing.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from typing import BinaryIO

import numpy as np
from loguru import logger

from ...processing.hwaccel import hwaccel_decode_args
from ..probe import ProbeError, ProbeTimeoutError, ffprobe_path_for, probe_media
from .rule_j import Row

FRAME_W = 320
FRAME_H = 180
MOVIE_TAIL_S = 900.0
EPISODE_TAIL_S = 450.0
FFMPEG_THREADS = 2
CHUNK_FRAMES = 64
DECODE_TIMEOUT_S = 600.0
PROBE_TIMEOUT_S = 30.0
_Y_BYTES = FRAME_W * FRAME_H
_NV12_BYTES = _Y_BYTES * 3 // 2
# Every showinfo line is matched, `pts_time:NOPTS` included: a line that can't be read has to drop its own frame's row,
# never shift later frames onto an earlier frame's timestamp.
_PTS_RE = re.compile(rb"pts_time:(\S+)")
# The GPU scale filters below exist only for these vendors; any other VAAPI node decodes on the GPU but downloads each
# frame, or a wasted GpuDecodeError run on unscalable GPU surfaces would follow.
_GPU_SCALE_VENDORS = ("NVIDIA", "INTEL", "AMD")
_POLL_S = 0.1
_KILL_WAIT_S = 5.0
_READER_JOIN_S = 2.0
_END = object()


class FrameDecodeError(Exception):
    """ffmpeg couldn't decode the frames."""


class GpuDecodeError(FrameDecodeError):
    """The GPU decode failed or gave no frames; the worker reruns the file on the CPU."""


class DecodeTimeoutError(FrameDecodeError):
    """The decode ran past its time limit (on the GPU or the CPU: never a reason to rerun on the CPU, T-R7)."""


class DecodeCancelledError(Exception):
    """The job was cancelled during the decode."""


def tail_start_s(duration_ms: int, *, is_episode: bool) -> float:
    """Where the tail starts: the last 450 s of an episode, the last 900 s of anything else (T-R4).

    Args:
        duration_ms: The file's duration.
        is_episode: The file is a TV episode.

    Returns:
        Seconds from the start of the file, never below 0.
    """
    tail = EPISODE_TAIL_S if is_episode else MOVIE_TAIL_S
    return max(0.0, duration_ms / 1000.0 - tail)


def _scale_filter(gpu: str | None, hw_active: bool, keep_on_gpu: bool) -> str:
    if hw_active and keep_on_gpu and gpu == "NVIDIA":
        return f"scale_cuda={FRAME_W}:{FRAME_H}:format=nv12,hwdownload,format=nv12"
    if hw_active and keep_on_gpu:
        return f"scale_vaapi=w={FRAME_W}:h={FRAME_H}:format=nv12,hwdownload,format=nv12"
    return f"scale={FRAME_W}:{FRAME_H},format=nv12"


def decode_command(
    ffmpeg: str,
    path: str,
    *,
    start_s: float,
    length_s: float | None,
    keyframes_only: bool,
    fps: int | None,
    gpu: str | None,
    gpu_device_path: str | None,
) -> tuple[list[str], bool]:
    """The spec §5.4 ffmpeg command for one decode.

    Args:
        ffmpeg: ffmpeg binary.
        path: The media file (read only).
        start_s: Where decoding starts (``-ss`` before the input, timestamps kept with ``-copyts``).
        length_s: How long to decode, or None to the end.
        keyframes_only: Decode keyframes only (the tail).
        fps: Frames per second to keep (the refine window), or None for every decoded frame.
        gpu: The worker's GPU type, None on a CPU worker.
        gpu_device_path: The worker's device.

    Returns:
        The argv, and whether decode runs on the GPU.
    """
    keep_on_gpu = gpu in _GPU_SCALE_VENDORS
    decode = hwaccel_decode_args(gpu, gpu_device_path, keep_on_gpu=keep_on_gpu)
    video_filter = _scale_filter(gpu, decode.active, keep_on_gpu)
    if fps:
        video_filter = f"fps={fps},{video_filter}"
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", str(FFMPEG_THREADS), *decode.args]
    if keyframes_only:
        command += ["-skip_frame", "nokey"]
    command += ["-ss", f"{start_s:.3f}"]
    if length_s is not None:
        command += ["-t", f"{length_s:.3f}"]
    command += ["-copyts", "-i", path, "-an", "-sn", "-dn", "-fps_mode", "passthrough",
                "-vf", f"{video_filter},showinfo", "-f", "rawvideo", "-"]  # fmt: skip
    return command, decode.active


def _parse_pts(value: bytes, offset_s: float) -> float | None:
    """One ``showinfo`` timestamp as seconds from the start of the file, or None for an unusable one (``NOPTS``)."""
    try:
        return round(float(value) - offset_s, 3)
    except ValueError:
        return None


def container_start_s(
    path: str,
    ffmpeg: str,
    *,
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> float:
    """The container's own first timestamp in seconds, 0.0 when it has none.

    ``-copyts`` reports each frame at the container's timestamp while ``-ss`` seeks from the start of the file. A
    recorded-TV ``.ts`` with a PCR base (30000 s measured on a 40 s capture) would otherwise put every credits answer
    tens of thousands of seconds outside the file.

    Args:
        path: The media file.
        ffmpeg: The ffmpeg binary (ffprobe is taken from beside it).
        cancel_check: True once the job is cancelled; checked before probing (a stalled mount would otherwise hold the
            worker for the probe's timeout after a cancel).
        timeout_s: Hard limit for the probe, so a stalled mount holds the worker for this long plus
            ``probe.KILL_WAIT_S`` at most.

    Raises:
        DecodeCancelledError: Already cancelled (nothing is probed).
        DecodeTimeoutError: ffprobe ran past ``timeout_s``: the file is left alone for a day like a decode that
            timed out, instead of taking a worker every run only to stall again.
        FrameDecodeError: ffprobe couldn't read the file, so there is no answer this run.
    """
    name = os.path.basename(path)
    if cancel_check and cancel_check():
        raise DecodeCancelledError(f"cancelled before decoding {name}")
    try:
        probe = probe_media(path, ffprobe=ffprobe_path_for(ffmpeg), timeout_s=timeout_s)
    except ProbeTimeoutError as exc:
        raise DecodeTimeoutError(f"reading the start time of {name} timed out after {timeout_s:g} s") from exc
    except ProbeError as exc:
        raise FrameDecodeError(f"could not read the start time of {name}: {exc}") from exc
    return (probe.start_time_ms or 0) / 1000.0


def _read_frames(stream: BinaryIO, frames: queue.Queue, stop: threading.Event) -> None:
    """Reader thread: each whole frame's Y plane into the bounded queue, then ``_END``.

    Every put waits for room for as long as it takes (text detection on the consumer side can take longer per chunk than
    the reader takes to fill the queue), so neither a frame nor the end marker is ever dropped. Only ``stop`` (the
    consumer gave up) ends a wait.
    """

    def put(item: object) -> bool:
        while not stop.is_set():
            try:
                frames.put(item, timeout=_POLL_S)
                return True
            except queue.Full:
                continue
        return False

    try:
        while not stop.is_set():
            data = stream.read(_NV12_BYTES)
            if len(data) < _NV12_BYTES:
                break  # end of stream (a partial trailing frame is dropped)
            if not put(data[:_Y_BYTES]):
                return
    except (OSError, ValueError):
        pass
    put(_END)


def _kill(proc: subprocess.Popen) -> None:
    """Kill ffmpeg's whole process group (it runs in its own session) and wait a bounded time for it."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_KILL_WAIT_S)


def _reap(proc: subprocess.Popen, reader: threading.Thread) -> None:
    reader.join()
    with contextlib.suppress(OSError, ValueError):
        proc.stdout.close()
    with contextlib.suppress(OSError):
        proc.wait()


def _release(proc: subprocess.Popen, reader: threading.Thread, stop: threading.Event, name: str) -> None:
    """Let go of the decode without ever blocking the worker on its pipe.

    A process stuck in an uninterruptible read (a stalled network mount) or anything else still holding the pipe keeps
    the reader blocked in ``read()``; closing the pipe then would wait on the reader's buffer lock. Such handles go to
    a daemon reaper that closes them once the reader lets go.
    """
    stop.set()
    reader.join(timeout=_READER_JOIN_S)
    if reader.is_alive() or proc.poll() is None:
        logger.warning(
            "ffmpeg for {} still holds its output after being stopped; leaving it to finish on its own", name
        )
        threading.Thread(target=_reap, args=(proc, reader), daemon=True, name="credits-frames-reaper").start()
        return
    with contextlib.suppress(OSError, ValueError):
        proc.stdout.close()


def run_decode(
    command: list[str],
    *,
    hw_active: bool,
    count_boxes: Callable[[np.ndarray], list[int]],
    pts_offset_s: float,
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = DECODE_TIMEOUT_S,
    chunk_frames: int = CHUNK_FRAMES,
    name: str = "",
) -> list[Row]:
    """Run one decode and count text boxes chunk by chunk.

    Args:
        command: From :func:`decode_command`.
        hw_active: Decode runs on the GPU (a failure is then a :class:`GpuDecodeError`).
        count_boxes: Box counts for (n, 180, 320) uint8 luma planes.
        pts_offset_s: The container's own first timestamp, subtracted from every row so they are seconds from the start
            of the file (see :func:`container_start_s`). Required, and 0.0 only for a container that starts at 0: a
            default would quietly hand back a recording's raw timestamps, tens of thousands of seconds out.
        cancel_check: True once the job is cancelled; checked between chunks and while ffmpeg exits.
        timeout_s: Time limit for the decode, checked between chunks: the worker is released within ``timeout_s`` plus
            one text detection call plus 7 s (the bounded kill and reader waits). A stalled network mount must not hold
            a worker.
        chunk_frames: Frames per text detection request.
        name: The file's name for messages.

    Returns:
        ``(pts, boxes, luma)`` per frame, in decode order. A frame ffmpeg gave no timestamp for is left out; the frames
        around it keep their own.

    Raises:
        DecodeCancelledError: Cancelled (ffmpeg is killed).
        GpuDecodeError: The GPU decode exited non-zero or gave no frames.
        DecodeTimeoutError: The decode (on the GPU or the CPU) ran past ``timeout_s``; ffmpeg is killed.
        FrameDecodeError: ffmpeg couldn't be started, a CPU decode exited non-zero, text detection answered a count per
            frame it wasn't asked, or a clean exit wrote a number of timestamps that doesn't match the frames.
    """
    boxes: list[int] = []
    luma: list[float] = []
    pending: list[bytes] = []
    stop = threading.Event()
    # Two chunks in flight (7.4 MB of luma at 64 frames). Unbounded, a decoder faster than text detection would buffer
    # the whole tail: 671 keyframes on the measured 4K movie, 39 MB per worker.
    frames: queue.Queue = queue.Queue(maxsize=chunk_frames * 2)
    deadline = time.monotonic() + timeout_s

    def flush() -> None:
        planes = np.frombuffer(b"".join(pending), dtype=np.uint8).reshape(len(pending), FRAME_H, FRAME_W)
        counts = count_boxes(planes)
        if len(counts) != len(pending):
            raise FrameDecodeError(f"text detection answered {len(counts)} counts for {len(pending)} frames")
        boxes.extend(int(c) for c in counts)
        luma.extend(round(float(plane.mean()), 1) for plane in planes)
        pending.clear()

    with tempfile.TemporaryFile() as stderr_file:
        try:
            # Its own session, so a kill reaches everything ffmpeg started; the app's process group is never signalled.
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr_file, start_new_session=True)
        except OSError as exc:
            raise FrameDecodeError(f"could not run {command[0]} for {name}: {exc}") from exc
        reader = threading.Thread(
            target=_read_frames, args=(proc.stdout, frames, stop), daemon=True, name="credits-frames"
        )
        reader.start()
        try:
            while True:
                if cancel_check and cancel_check():
                    raise DecodeCancelledError(f"cancelled while decoding {name}")
                if time.monotonic() > deadline:
                    raise DecodeTimeoutError(f"decoding {name} timed out after {timeout_s:g} s")
                try:
                    item = frames.get(timeout=_POLL_S)
                except queue.Empty:
                    if not reader.is_alive() and frames.empty():
                        break  # the reader died without its end marker (it only returns early once stopped)
                    continue
                if item is _END:
                    break
                pending.append(item)
                if len(pending) == chunk_frames:
                    flush()
            if pending:
                flush()
            while proc.poll() is None:  # ffmpeg can outlive its output; a cancel here must not wait for the deadline
                if cancel_check and cancel_check():
                    raise DecodeCancelledError(f"cancelled while decoding {name}")
                if time.monotonic() > deadline:
                    raise DecodeTimeoutError(f"decoding {name} timed out after {timeout_s:g} s")
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=_POLL_S)
            returncode = proc.returncode
        except BaseException:
            _kill(proc)
            raise
        finally:
            _release(proc, reader, stop, name)
        stderr_file.seek(0)
        stderr = stderr_file.read()
    if returncode != 0:
        tail = stderr.decode("utf-8", "replace").strip()[-300:]
        where = "on the GPU" if hw_active else "on the CPU"
        error = GpuDecodeError if hw_active else FrameDecodeError
        raise error(f"ffmpeg exited {returncode} decoding {name} {where}: {tail}")
    if not boxes and hw_active:
        raise GpuDecodeError(f"the GPU decoded no frames from {name}")
    pts = [_parse_pts(value, pts_offset_s) for value in _PTS_RE.findall(stderr)]
    if len(pts) != len(boxes):
        # ffmpeg exited cleanly, so a showinfo line per frame is the contract. Pairing anyway would put a frame's boxes
        # on another frame's timestamp and store a credits start at the wrong second; the file is skipped instead.
        raise FrameDecodeError(f"ffmpeg wrote {len(pts)} timestamps for {len(boxes)} frames of {name}")
    rows = [(pts[i], boxes[i], luma[i]) for i in range(len(boxes)) if pts[i] is not None]
    if len(rows) != len(boxes):
        logger.warning("{} frames of {} have no timestamp and are dropped", len(boxes) - len(rows), name)
    return rows


def decode_rows(
    path: str,
    *,
    ffmpeg: str,
    start_s: float,
    length_s: float | None,
    keyframes_only: bool,
    fps: int | None,
    gpu: str | None,
    gpu_device_path: str | None,
    count_boxes: Callable[[np.ndarray], list[int]],
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = DECODE_TIMEOUT_S,
    start_time_s: float | None = None,
) -> list[Row]:
    """:func:`decode_command` then :func:`run_decode` (other arguments as there).

    Rows come back as seconds from the start of the file, whatever timestamps the container carries.

    A refine decode (``fps`` set) reports the fps filter's own grid, not the source frame's time: on a 5 fps clip
    starting at 20.3 s the rows read 20.0, 21.0 … for source frames at 20.4, 21.4 …, so a refine row can be up to one
    source-frame interval early (the safe direction for a credits start).

    Args:
        start_time_s: The container's first timestamp (``format.start_time``). None probes the file here, which is the
            right choice unless the caller has a probe of its own **from this run**: ``pipeline._attempt`` leaves its
            probe None for a file that hasn't changed and has a stored duration, and the store never keeps a start
            time, so a stale or absent probe passed as 0.0 would put a recording's answers 30000 s out.

    Raises:
        DecodeCancelledError: Already cancelled (nothing is probed or decoded).
        DecodeTimeoutError: The start time probe ran past its limit, plus everything :func:`run_decode` raises.
        FrameDecodeError: The start time couldn't be read. Probing adds at most ``min(30 s, timeout_s)`` plus
            ``probe.KILL_WAIT_S`` before :func:`run_decode`'s own release bound.
    """
    name = os.path.basename(path)
    if cancel_check and cancel_check():
        raise DecodeCancelledError(f"cancelled before decoding {name}")
    command, hw_active = decode_command(
        ffmpeg, path, start_s=start_s, length_s=length_s, keyframes_only=keyframes_only, fps=fps, gpu=gpu,
        gpu_device_path=gpu_device_path,
    )  # fmt: skip
    offset_s = (
        container_start_s(path, ffmpeg, timeout_s=min(PROBE_TIMEOUT_S, timeout_s))
        if start_time_s is None
        else start_time_s
    )
    return run_decode(command, hw_active=hw_active, count_boxes=count_boxes, cancel_check=cancel_check,
                      timeout_s=timeout_s, pts_offset_s=offset_s, name=name)  # fmt: skip
