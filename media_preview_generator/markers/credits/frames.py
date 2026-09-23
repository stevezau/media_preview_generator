"""Frames of a file's ending for credit text detection (spec §5.4): the keyframes of the tail, then one frame a second
just before the coarse answer. Decoded with the worker's GPU through the same hwaccel arguments as previews.

Only 320×180 NV12 leaves ffmpeg and only the Y plane is kept; each chunk of frames goes to text detection as it
arrives, so memory stays bounded whatever the file's keyframe spacing. A row keeps the boxes that chunk found, not
only how many (``rule_j.Row``); they come from the same detection call, so they cost no extra decoding or detection.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import queue
import re
import signal
import statistics
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from typing import BinaryIO, NamedTuple

import numpy as np
from loguru import logger

from ...processing.hwaccel import hwaccel_decode_args
from ..probe import (
    ProbeError,
    ProbeStalledError,
    ProbeTimeoutError,
    VideoPacket,
    ffprobe_path_for,
    probe_media,
    video_packets,
)
from .rule_j import Box, Row

FRAME_W = 320
FRAME_H = 180
MOVIE_TAIL_S = 900.0
EPISODE_TAIL_S = 450.0
FFMPEG_THREADS = 2
CHUNK_FRAMES = 64
DECODE_TIMEOUT_S = 600.0
PROBE_TIMEOUT_S = 30.0
# A stream whose first this-many video packets are all keyframes is intra-only (ProRes, DNxHD, MJPEG, an all-I H.264):
# every frame is a keyframe. A normal stream's keyframes are a GOP apart (x264 won't put IDR frames closer than
# min-keyint, keyint/10 capped at the frame rate: 23-25 frames at its default keyint of 250, 4 at a 2 s GOP), so 24
# keyframes in a row is never a normal stream.
INTRA_CHECK_PACKETS = 24
# The keyframe spacing rule J was measured at: the median gap between keyframes is 2.0 s on the median file of the
# 80 (1.46 s at the 10th percentile, 8.1 s at the 90th). An intra-only stream's keyframe pass keeps one packet per
# this much and drops the rest before they are decoded.
INTRA_ONLY_SPACING_S = 2.0
# Codecs whose FFmpeg decoder never reads -skip_frame, so the keyframe pass would decode every frame: VP9's (vp9.c, up
# to 8.1 and master), which every hwaccel decodes inside. Their packets not flagged as keyframes are dropped before the
# decoder instead. The other decoders honor it (H.264, HEVC, MPEG-1/2, VC-1, MPEG-4 part 2, VP8, AV1 through dav1d or
# a hwaccel), measured per codec in phase3-harness.md.
SKIP_FRAME_IGNORED = frozenset({"vp9"})
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


class GpuNoFramesError(GpuDecodeError):
    """The GPU decode exited cleanly and gave no frames. A GPU failure like any other -- unless the file has already
    decoded on this GPU and the window can hold no keyframe, which the credit text detector's later steps before the
    tail tell apart (``detector._step_rows``)."""


class DecodeTimeoutError(FrameDecodeError):
    """The decode ran past its time limit (on the GPU or the CPU: never a reason to rerun on the CPU, T-R7)."""


class DecodeCancelledError(Exception):
    """The job was cancelled during the decode."""


class KeyframeThinning(NamedTuple):
    """Which packets of the first ordinary video stream the keyframe pass drops before the decoder.

    Attributes:
        keep_every: Decode one packet in this many (an intra-only stream), or None.
        drop_non_key: Drop every packet not flagged as a keyframe (a codec in ``SKIP_FRAME_IGNORED``).
    """

    keep_every: int | None = None
    drop_non_key: bool = False


def tail_length_s(*, is_episode: bool, tv_s: int | None = None, movie_s: int | None = None) -> float:
    """How much of the end is the tail: the last 450 s of an episode, the last 900 s of anything else (T-R4), unless
    the user chose a window in Settings.

    Args:
        is_episode: The file is a TV episode (a file of unknown kind is not).
        tv_s: The user's window for episodes, None for Automatic.
        movie_s: The user's window for movies and files of unknown kind, None for Automatic.

    Returns:
        Seconds.
    """
    chosen = tv_s if is_episode else movie_s
    if chosen is not None:
        return float(chosen)
    return EPISODE_TAIL_S if is_episode else MOVIE_TAIL_S


def tail_start_s(duration_ms: int, *, tail_s: float) -> float:
    """Where the tail starts.

    Args:
        duration_ms: The file's duration.
        tail_s: The tail's length (:func:`tail_length_s`).

    Returns:
        Seconds from the start of the file, never below 0.
    """
    return max(0.0, duration_ms / 1000.0 - tail_s)


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
    keep_every: int | None = None,
    drop_non_key: bool = False,
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
        keep_every: Decode one packet in this many of the first ordinary video stream (the one
            :func:`keyframe_thinning` probed), counted from the seek point; the rest are still read from the file but
            dropped before the decoder (an intra-only stream's keyframe pass, where every packet decodes on its own).
        drop_non_key: Drop that stream's packets not flagged as keyframes before the decoder (the keyframe pass of a
            codec whose decoder ignores ``-skip_frame``). With neither, every packet is decoded and the command is
            exactly the spec's.

    Returns:
        The argv, and whether decode runs on the GPU.
    """
    keep_on_gpu = gpu in _GPU_SCALE_VENDORS
    decode = hwaccel_decode_args(gpu, gpu_device_path, keep_on_gpu=keep_on_gpu)
    video_filter = _scale_filter(gpu, decode.active, keep_on_gpu)
    if fps:
        video_filter = f"fps={fps},{video_filter}"
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", str(FFMPEG_THREADS), *decode.args]
    drops = (["not(key)"] if drop_non_key else []) + ([f"mod(n\\,{keep_every})"] if keep_every else [])
    if drops:
        # An input bitstream filter (ffmpeg 7.1+) drops the packets between the demuxer and the decoder: thinned by a
        # filter after it, a 4K ProRes tail still decodes every frame (measured 490 s of CPU per 900 s tail). Only on
        # V:0, the stream that was probed: ffmpeg may decode another video stream, which must not be thinned. ``n``
        # counts packets from the seek and ``key`` is the packet's keyframe flag; ``noise`` drops a packet whose
        # expression is non-zero, so ``+`` drops what either term drops. The comma is escaped from the filter list,
        # and with only ``drop`` set ``noise`` leaves every kept packet's bytes alone.
        command += ["-bsf:V:0", "noise=drop=" + "+".join(drops)]
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


def keyframe_thinning(path: str, ffmpeg: str, *, cancel_check: Callable[[], bool] | None = None,
                      timeout_s: float = PROBE_TIMEOUT_S) -> KeyframeThinning:  # fmt: skip
    """Which packets the keyframe pass drops before the decoder, from one ffprobe of the first video packets.

    Two kinds of stream make ``-skip_frame nokey`` skip nothing, so the keyframe pass would decode and read every frame
    of the tail for text: as many frames as the keyframe pass would otherwise keep one of (48 for an intra-only
    24 fps stream at ``INTRA_ONLY_SPACING_S``, a VP9 stream's whole GOP), and past the decode timeout on a movie's
    tail on the CPU.

    - An intra-only stream, where every frame is a keyframe. It is told from the first ``INTRA_CHECK_PACKETS`` packet
      flags rather than the codec, so an all-I H.264 or HEVC counts as well as ProRes, DNxHD or MJPEG, and one packet
      is kept per ``INTRA_ONLY_SPACING_S`` over their frame interval.
    - A codec whose decoder ignores ``-skip_frame`` (``SKIP_FRAME_IGNORED``: VP9): its packets not flagged as
      keyframes are dropped. A file whose container flags none in the tail gives the pass no frames, like any file
      without a keyframe in its tail: the GPU decode fails, and the worker's CPU rerun finds no roll.

    Thinning saves the decode and the text detection, not the reading: the tail is still read from disk in full.

    Args:
        path: The media file.
        ffmpeg: The ffmpeg binary (ffprobe is taken from beside it).
        cancel_check: True once the job is cancelled; checked before probing.
        timeout_s: Hard limit for the probe (plus ``probe.KILL_WAIT_S``).

    Returns:
        What to drop. Nothing when neither applies, or when ffprobe fails on the packets (not a timeout or a stall,
        which raise): the file is then read the ordinary way, exactly as before this check existed. For a VP9 or
        intra-only file that means every frame of the tail, bounded by ``DECODE_TIMEOUT_S`` (rare: the start time
        probe of the same file has just succeeded).

    Raises:
        DecodeCancelledError: Already cancelled (nothing is probed).
        DecodeTimeoutError: ffprobe ran past ``timeout_s``, as :func:`container_start_s` does.
        FrameDecodeError: Earlier ffprobes are still stuck, so this one wasn't started.
    """
    name = os.path.basename(path)
    if cancel_check and cancel_check():
        raise DecodeCancelledError(f"cancelled before decoding {name}")
    try:
        probed = video_packets(path, ffprobe=ffprobe_path_for(ffmpeg), packets=INTRA_CHECK_PACKETS, timeout_s=timeout_s)
    except ProbeTimeoutError as exc:
        raise DecodeTimeoutError(f"reading the video packets of {name} timed out after {timeout_s:g} s") from exc
    except ProbeStalledError as exc:
        raise FrameDecodeError(f"could not read the video packets of {name}: {exc}") from exc
    except ProbeError as exc:
        logger.debug("Couldn't read the video packets of {}, so it is read the ordinary way: {}", name, exc)
        return KeyframeThinning()
    return KeyframeThinning(_intra_only_stride(probed.packets, name), probed.codec in SKIP_FRAME_IGNORED)


def _intra_only_stride(packets: tuple[VideoPacket, ...], name: str) -> int | None:
    """One packet in how many an intra-only stream's keyframe pass keeps; None when the packets aren't all keyframes,
    are fewer than ``INTRA_CHECK_PACKETS``, their times give no frame interval, or the stride would be 1."""
    if len(packets) < INTRA_CHECK_PACKETS or not all(packet.keyframe for packet in packets):
        return None
    times = sorted(packet.pts_s for packet in packets if packet.pts_s is not None)
    intervals = [later - earlier for earlier, later in itertools.pairwise(times) if later > earlier]
    if not intervals:
        logger.debug("{} is intra-only but its packets carry no times, so it is read the ordinary way", name)
        return None
    stride = round(INTRA_ONLY_SPACING_S / statistics.median(intervals))
    return stride if stride > 1 else None


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
    detect_boxes: Callable[[np.ndarray], list[tuple[Box, ...]]],
    pts_offset_s: float,
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = DECODE_TIMEOUT_S,
    chunk_frames: int = CHUNK_FRAMES,
    name: str = "",
) -> list[Row]:
    """Run one decode and read its text boxes chunk by chunk.

    Args:
        command: From :func:`decode_command`.
        hw_active: Decode runs on the GPU (a failure is then a :class:`GpuDecodeError`).
        detect_boxes: Text boxes for (n, 180, 320) uint8 luma planes, one ``(left, top, right, bottom)`` tuple per box.
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
        ``(pts, box count, luma, boxes)`` per frame, in decode order. A frame ffmpeg gave no timestamp for is left out;
        the frames around it keep their own.

    Raises:
        DecodeCancelledError: Cancelled (ffmpeg is killed).
        GpuDecodeError: The GPU decode exited non-zero, or (:class:`GpuNoFramesError`, a narrower kind) gave no
            frames.
        DecodeTimeoutError: The decode (on the GPU or the CPU) ran past ``timeout_s``; ffmpeg is killed.
        FrameDecodeError: ffmpeg couldn't be started, a CPU decode exited non-zero, text detection answered boxes for a
            number of frames it wasn't asked or boxes that aren't four numbers each, or a clean exit wrote a number of
            timestamps that doesn't match the frames.
    """
    boxes: list[tuple[Box, ...]] = []
    luma: list[float] = []
    pending: list[bytes] = []
    stop = threading.Event()
    # Two chunks in flight (7.4 MB of luma at 64 frames). Unbounded, a decoder faster than text detection would buffer
    # the whole tail: 671 keyframes on the measured 4K movie, 39 MB per worker.
    frames: queue.Queue = queue.Queue(maxsize=chunk_frames * 2)
    deadline = time.monotonic() + timeout_s

    def flush() -> None:
        planes = np.frombuffer(b"".join(pending), dtype=np.uint8).reshape(len(pending), FRAME_H, FRAME_W)
        found = detect_boxes(planes)
        if len(found) != len(pending):
            raise FrameDecodeError(f"text detection answered {len(found)} frames' boxes for {len(pending)} frames")
        try:
            boxes.extend(tuple((int(a), int(b), int(c), int(d)) for a, b, c, d in frame) for frame in found)
        except (TypeError, ValueError) as exc:
            # The helper's own answer is checked before it gets here; this is any other text detector's, and the
            # caller only handles the decode errors this module names.
            raise FrameDecodeError(f"text detection answered boxes that aren't four numbers each: {exc}") from exc
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
        raise GpuNoFramesError(f"the GPU decoded no frames from {name}")
    pts = [_parse_pts(value, pts_offset_s) for value in _PTS_RE.findall(stderr)]
    if len(pts) != len(boxes):
        # ffmpeg exited cleanly, so a showinfo line per frame is the contract. Pairing anyway would put a frame's boxes
        # on another frame's timestamp and store a credits start at the wrong second; the file is skipped instead.
        raise FrameDecodeError(f"ffmpeg wrote {len(pts)} timestamps for {len(boxes)} frames of {name}")
    rows = [(pts[i], len(boxes[i]), luma[i], boxes[i]) for i in range(len(boxes)) if pts[i] is not None]
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
    detect_boxes: Callable[[np.ndarray], list[tuple[Box, ...]]],
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float = DECODE_TIMEOUT_S,
    start_time_s: float | None = None,
    keep_every: int | None = None,
    drop_non_key: bool = False,
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
        gpu_device_path=gpu_device_path, keep_every=keep_every, drop_non_key=drop_non_key,
    )  # fmt: skip
    offset_s = (
        container_start_s(path, ffmpeg, timeout_s=min(PROBE_TIMEOUT_S, timeout_s))
        if start_time_s is None
        else start_time_s
    )
    return run_decode(command, hw_active=hw_active, detect_boxes=detect_boxes, cancel_check=cancel_check,
                      timeout_s=timeout_s, pts_offset_s=offset_s, name=name)  # fmt: skip
