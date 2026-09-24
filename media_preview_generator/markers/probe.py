"""ffprobe wrapper for duration, chapters, the container's and first audio stream's first timestamps, the main video
stream's codec and first packet headers, and the bounded kill it shares with the fingerprint ffmpeg.

A process stuck in an uninterruptible read on a stalled network mount can't die until that read returns, and it holds
its pipes until then. Collecting it with a plain ``wait()`` or ``communicate()`` -- what ``subprocess.run`` does after
its own timeout -- blocks the caller, and whatever worker slot or lock it holds, for as long as the stall lasts. Such a
process goes to a reaper thread instead, and counts as stuck until the reaper collects it, so callers can stop
starting more on what is most likely the same stalled mount.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass

from loguru import logger

KILL_WAIT_S = 5.0
FFPROBE_REAPER = "ffprobe-reaper"
# ffprobes left stuck on their files (after timing out) before no new one is started.
MAX_STUCK_FFPROBES = 2
_stuck: dict[str, int] = {}
_stuck_lock = threading.Lock()


class ProbeError(Exception):
    """ffprobe could not read the file."""


class ProbeTimeoutError(ProbeError):
    """ffprobe didn't finish within its time limit (most likely a stalled mount)."""


class ProbeStalledError(ProbeError):
    """``MAX_STUCK_FFPROBES`` earlier ffprobes are still stuck reading their files, so none is started: not this
    file's fault, and nothing should be recorded against it."""


@dataclass(frozen=True)
class Chapter:
    """A container chapter (times in ms)."""

    start_ms: int
    end_ms: int | None
    title: str


@dataclass(frozen=True)
class MediaProbe:
    """What the marker pipeline needs from ffprobe.

    Attributes:
        duration_ms: Container duration, None when ffprobe reports none.
        chapters: Chapters in container order.
        start_time_ms: The container's first timestamp. Recorded-TV ``.ts`` files carry a PCR base (30000 s measured),
            and frame timestamps read with ``-copyts`` are that much later than the file's own seconds.
        frame_rate: The first video stream's frame rate (cover art left out), None when there is none or ffprobe
            reports none (``markers.speed``: a 25 fps release of a film-rate show plays 4.3 % fast).
    """

    duration_ms: int | None
    chapters: tuple[Chapter, ...]
    start_time_ms: int | None = None
    frame_rate: float | None = None


def stuck_processes(reaper_name: str) -> int:
    """How many killed processes handed to this reaper are still stuck holding their output."""
    with _stuck_lock:
        return _stuck.get(reaper_name, 0)


def _count_stuck(reaper_name: str, step: int) -> None:
    with _stuck_lock:
        _stuck[reaper_name] = _stuck.get(reaper_name, 0) + step


def kill_and_collect(
    proc: subprocess.Popen,
    *,
    what: str,
    reaper_name: str,
    wait_s: float | None = None,
) -> bool:
    """Kill a process and collect it, waiting at most ``wait_s``.

    One that still holds its output then goes to a daemon reaper thread, and counts in
    ``stuck_processes(reaper_name)`` until the reaper collects it. The count is app-wide, not per mount: telling mounts
    apart needs a stat of the file, which itself blocks on the stalled mount (as the pipeline's own stat already does).

    Args:
        proc: The process, started with pipes.
        what: What it was doing, for the warning (``ffprobe reading x.mkv``).
        reaper_name: The reaper thread's name, which its stuck processes are counted under.
        wait_s: How long it may take to exit and let go of its pipes (``KILL_WAIT_S`` when None).

    Returns:
        True when it was collected here; False when it still held its output.
    """
    proc.kill()
    try:
        proc.communicate(timeout=KILL_WAIT_S if wait_s is None else wait_s)
        return True
    except subprocess.TimeoutExpired:
        pass
    logger.warning("{} still holds its output after being stopped; leaving it to finish on its own", what)
    _count_stuck(reaper_name, 1)  # before the reaper starts, so its count-down can never come first
    try:
        threading.Thread(target=_reap, args=(proc, reaper_name), daemon=True, name=reaper_name).start()
    except RuntimeError as exc:
        # Nothing will ever collect it, so nothing could ever count it down: counted, it would stop that kind of
        # process for the rest of the app's run.
        _count_stuck(reaper_name, -1)
        logger.warning("Couldn't start a thread to collect {}: {}", what, exc)
    return False


def _reap(proc: subprocess.Popen, reaper_name: str) -> None:
    try:
        with contextlib.suppress(OSError, ValueError):
            proc.communicate()
    finally:
        _count_stuck(reaper_name, -1)


def ffprobe_path_for(ffmpeg_path: str | None) -> str:
    """Prefer the ffprobe shipped next to the configured ffmpeg (jellyfin-ffmpeg in the image)."""
    if ffmpeg_path:
        sibling = os.path.join(os.path.dirname(ffmpeg_path), "ffprobe")
        if os.path.isfile(sibling) and os.access(sibling, os.X_OK):
            return sibling
    return shutil.which("ffprobe") or "ffprobe"


def _ms(value: object) -> int | None:
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError, OverflowError):
        # Defensive: non-finite values can't become ms (nan raises ValueError, inf raises
        # OverflowError on round()); not a claim that ffprobe actually emits either.
        return None


def _seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None  # "N/A", or absent
    return seconds if math.isfinite(seconds) else None


def probe_media(path: str, *, ffprobe: str, timeout_s: float = 60.0) -> MediaProbe:
    """Read duration, chapters and the container's first timestamp.

    Args:
        path: Media file.
        ffprobe: ffprobe binary.
        timeout_s: Hard timeout (hung mounts must not hold a check thread forever): the call returns within it plus
            the bounded wait for the killed ffprobe (``KILL_WAIT_S``). ``subprocess.run`` would wait for it
            without a limit, and an ffprobe stuck in a read on a stalled mount can't exit until the read returns.

    Returns:
        Duration (None if unknown), chapters in container order, and the container's first timestamp (None if the file
        reports none; recordings carry a large one, which credits frame times are read against).

    Raises:
        ProbeStalledError: ``MAX_STUCK_FFPROBES`` earlier ffprobes are still stuck; none is started.
        ProbeTimeoutError: ffprobe ran past ``timeout_s``.
        ProbeError: ffprobe missing, failed or returned invalid JSON.
    """
    entries = "stream=codec_type,avg_frame_rate,r_frame_rate:stream_disposition=attached_pic"
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_chapters",
           "-show_entries", entries, path]  # fmt: skip
    stdout = _run_ffprobe(cmd, path, timeout_s)
    try:
        data = json.loads(stdout or "")
    except ValueError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(data, dict):
        raise ProbeError(f"ffprobe returned unexpected JSON for {path}: top level was {type(data).__name__}")
    chapters = []
    for raw in data.get("chapters") or []:
        tags = {str(k).lower(): v for k, v in (raw.get("tags") or {}).items()}
        start = _ms(raw.get("start_time"))
        if start is None:
            continue
        chapters.append(Chapter(start, _ms(raw.get("end_time")), str(tags.get("title") or "")))
    fmt = data.get("format") or {}
    return MediaProbe(
        duration_ms=_ms(fmt.get("duration")),
        chapters=tuple(chapters),
        start_time_ms=_ms(fmt.get("start_time")),
        frame_rate=_video_frame_rate(data.get("streams")),
    )


def _rate(value: object) -> float | None:
    """An ffprobe rate such as ``24000/1001`` as a number; None for ``0/0``, ``N/A`` or anything unreadable."""
    try:
        numerator, denominator = (float(part) for part in str(value).split("/"))
    except ValueError:
        return None
    if denominator <= 0 or numerator <= 0 or not math.isfinite(numerator / denominator):
        return None
    return numerator / denominator


def _video_frame_rate(streams: object) -> float | None:
    """The first video stream's average frame rate, or its base rate when it reports no average; cover art (an
    attached picture) doesn't count."""
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        return _rate(stream.get("avg_frame_rate")) or _rate(stream.get("r_frame_rate"))
    return None


@dataclass(frozen=True)
class StreamStarts:
    """Where the container and its first audio stream start, and whether it has a picture to decode.

    Attributes:
        container_s: The container's first timestamp (0.0 when it reports none).
        audio_s: The first audio stream's first timestamp, None when the file has no audio stream or reports none.
        has_video: Whether it has a video stream that isn't cover art.
    """

    container_s: float
    audio_s: float | None
    has_video: bool = True

    @property
    def audio_offset_s(self) -> float:
        """How far into the file the first audio sample plays: a fingerprint's point 0 is that sample, since ffmpeg
        doesn't pad the late start of an audio stream (measured on an mkv whose audio starts 2 s after its video)."""
        return 0.0 if self.audio_s is None else max(0.0, self.audio_s - self.container_s)


def stream_starts(path: str, *, ffprobe: str, timeout_s: float = 60.0) -> StreamStarts:
    """The container's and the first audio stream's start times, and whether there is a video stream, from one ffprobe
    that reads only the headers.

    Args:
        path: Media file.
        ffprobe: ffprobe binary.
        timeout_s: Hard timeout, as for :func:`probe_media`.

    Returns:
        Both start times.

    Raises:
        ProbeStalledError: ``MAX_STUCK_FFPROBES`` earlier ffprobes are still stuck; none is started.
        ProbeTimeoutError: ffprobe ran past ``timeout_s``.
        ProbeError: ffprobe missing, failed or returned something other than its JSON.
    """
    cmd = [ffprobe, "-v", "error", "-show_entries",
           "format=start_time:stream=codec_type,start_time:stream_disposition=attached_pic", "-of", "json", path]  # fmt: skip
    stdout = _run_ffprobe(cmd, path, timeout_s)
    try:
        data = json.loads(stdout or "")
    except ValueError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path}") from exc
    if not isinstance(data, dict):
        raise ProbeError(f"ffprobe returned unexpected JSON for {path}: top level was {type(data).__name__}")
    streams = data.get("streams") or []
    if not isinstance(streams, list):
        raise ProbeError(f"ffprobe returned an unexpected stream list for {path}")
    streams = [stream for stream in streams if isinstance(stream, dict)]
    fmt = data.get("format") or {}
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), {})
    has_video = any(
        stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic")
        for stream in streams
    )
    container = _seconds(fmt.get("start_time") if isinstance(fmt, dict) else None)
    return StreamStarts(container or 0.0, _seconds(audio.get("start_time")), has_video)


@dataclass(frozen=True)
class VideoPacket:
    """One video packet's header.

    Attributes:
        pts_s: Its presentation time in seconds, None when the container gives none.
        keyframe: Whether it decodes on its own.
    """

    pts_s: float | None
    keyframe: bool


@dataclass(frozen=True)
class VideoPackets:
    """The main video stream's codec, pixel format and first packet headers.

    Attributes:
        codec: ffprobe's ``codec_name`` for the stream (``h264``, ``vp9``), None when the file has no video stream or
            ffprobe names none.
        packets: The packets in the file's order.
        pix_fmt: ffprobe's ``pix_fmt`` for the stream (``yuv420p``, ``yuv420p10le``), None when it names none.
    """

    codec: str | None
    packets: tuple[VideoPacket, ...]
    pix_fmt: str | None = None


def video_packets(path: str, *, ffprobe: str, packets: int, timeout_s: float = 60.0) -> VideoPackets:
    """The codec, pixel format and first ``packets`` packet headers of the file's main video stream, from one
    ffprobe.

    Read from the start of the file up to those packets (not the whole file), and nothing is decoded. ``V`` leaves out
    cover art and thumbnails, which are single-picture streams.

    Args:
        path: Media file.
        ffprobe: ffprobe binary.
        packets: How many video packets to read.
        timeout_s: Hard timeout, as for :func:`probe_media`.

    Returns:
        The stream's codec and pixel format, and its packets: fewer when the stream is shorter, none when the file
        has no video.

    Raises:
        ProbeStalledError: ``MAX_STUCK_FFPROBES`` earlier ffprobes are still stuck; none is started.
        ProbeTimeoutError: ffprobe ran past ``timeout_s``.
        ProbeError: ffprobe missing, failed or returned something other than its JSON packet and stream lists.
    """
    cmd = [ffprobe, "-v", "error", "-select_streams", "V:0", "-read_intervals", f"%+#{packets}",
           "-show_entries", "packet=pts_time,flags:stream=codec_name,pix_fmt", "-of", "json", path]  # fmt: skip
    stdout = _run_ffprobe(cmd, path, timeout_s)
    try:
        data = json.loads(stdout or "")
    except ValueError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {path}") from exc
    raw = data.get("packets", []) if isinstance(data, dict) else None
    if not isinstance(raw, list) or not all(isinstance(packet, dict) for packet in raw):
        raise ProbeError(f"ffprobe returned an unexpected packet list for {path}")
    streams = data.get("streams", [])
    if not isinstance(streams, list) or not all(isinstance(stream, dict) for stream in streams):
        raise ProbeError(f"ffprobe returned an unexpected stream list for {path}")
    codec = streams[0].get("codec_name") if streams else None
    pix_fmt = streams[0].get("pix_fmt") if streams else None
    return VideoPackets(
        codec if isinstance(codec, str) and codec else None,
        tuple(VideoPacket(_seconds(packet.get("pts_time")), "K" in str(packet.get("flags", ""))) for packet in raw),
        pix_fmt if isinstance(pix_fmt, str) and pix_fmt else None,
    )


def _run_ffprobe(cmd: list[str], path: str, timeout_s: float) -> str:
    """Run ffprobe with a hard timeout and a bounded kill; its stdout.

    Raises:
        ProbeStalledError: ``MAX_STUCK_FFPROBES`` earlier ffprobes are still stuck; none is started.
        ProbeTimeoutError: ffprobe ran past ``timeout_s``.
        ProbeError: ffprobe missing or exited non-zero.
    """
    stuck = stuck_processes(FFPROBE_REAPER)
    if stuck >= MAX_STUCK_FFPROBES:
        raise ProbeStalledError(f"Not reading {path}: {stuck} earlier ffprobes are still stuck reading their files")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as exc:
        raise ProbeError(f"ffprobe failed for {path}: {type(exc).__name__}: {exc}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        kill_and_collect(proc, what=f"ffprobe reading {os.path.basename(path)}", reaper_name=FFPROBE_REAPER)
        raise ProbeTimeoutError(f"ffprobe failed for {path}: {type(exc).__name__}: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe exited {proc.returncode} for {path}: {(stderr or '').strip()[:300]}")
    return stdout
