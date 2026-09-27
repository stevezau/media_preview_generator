"""Exact frames read straight from a video, one a second, for the Inspector's Adjust and Review.

A preview's frames can be 2-10 s apart, too coarse to put an edge on. These come from ffmpeg on the CPU: a fast seek to
the first second, then one frame a second, scaled small, with HDR10 and HLG tone-mapped the way previews are. At most
``MAX_RUNNING`` run at once and a request waits only ``SLOT_WAIT_S`` for a slot (the page retries), so frame reads
never hold the web server's threads. Each runs at low CPU priority and is killed after ``RUN_TIMEOUT_S`` (a stalled
network read), its slot freed at once. Answers are kept in a size-capped folder of this app's own under the system
temp folder, never next to the video.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import time

from loguru import logger

from ..markers.probe import ffprobe_path_for, kill_and_collect

MAX_FRAMES = 14
STEP_MS = 1000
WIDTHS = (160, 240, 320, 480)
DEFAULT_WIDTH = 320
MAX_RUNNING = 2
# How long a request waits for a free slot before it is told to try again: short, so a page asking for many strips at
# once can't tie up the web server's threads waiting.
SLOT_WAIT_S = 2.0
# A 4K HEVC file decodes 14 s of video in well under this on a normal CPU; one that doesn't is stalled (an NFS read),
# and is killed.
RUN_TIMEOUT_S = 30.0
PROBE_TIMEOUT_S = 10.0
# The longest a killed ffmpeg is waited for before a reaper thread takes it over.
_KILL_WAIT_S = 1.0
_REAPER = "inspector-frames-reaper"
CACHE_MAX_BYTES = 64 * 1024 * 1024
_CACHE_DIR_NAME = "media-preview-generator-inspector-frames"
_NICE_LEVEL = "10"
_STALE_WORK_S = 3600.0
# Transfer characteristics ffprobe reports for HDR10 (PQ) and HLG: frames of these are tone-mapped to SDR.
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})

_slots = threading.BoundedSemaphore(MAX_RUNNING)
_prune_lock = threading.Lock()


class FramesBusyError(RuntimeError):
    """Every slot stayed taken for ``SLOT_WAIT_S``."""


class FramesError(RuntimeError):
    """ffmpeg couldn't read the frames (missing binary, unreadable file, timeout)."""


def cache_root() -> str:
    """The folder answers are kept in (under the system temp folder)."""
    return os.path.join(tempfile.gettempdir(), _CACHE_DIR_NAME)


def _own_cache(root: str) -> None:
    """Make ``root`` this process's private folder, refusing one planted by another user (or a link to elsewhere).

    Raises:
        FramesError: The folder can't be made, is a link, or belongs to someone else.
    """
    try:
        os.makedirs(root, mode=0o700, exist_ok=True)
        info = os.lstat(root)
    except OSError as exc:
        raise FramesError(f"Couldn't make the frame cache ({type(exc).__name__})") from exc
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        raise FramesError("The frame cache folder isn't this app's own")
    if stat.S_IMODE(info.st_mode) & 0o077:
        try:
            os.chmod(root, 0o700)
        except OSError as exc:
            raise FramesError(f"Couldn't make the frame cache private ({type(exc).__name__})") from exc


def _key(path: str, start_ms: int, count: int, width: int) -> str:
    st = os.stat(path)
    # The file's size and modification time are part of the key: a replaced file never gets the old file's frames.
    raw = f"{path}\0{st.st_size}\0{st.st_mtime_ns}\0{start_ms}\0{count}\0{width}"
    return hashlib.sha256(raw.encode("utf-8", "surrogateescape")).hexdigest()[:40]


def _frames_in(folder: str) -> list[str]:
    try:
        return sorted(n for n in os.listdir(folder) if n.endswith(".jpg"))
    except OSError:
        return []


def _read(folder: str, names: list[str], start_ms: int) -> list[tuple[int, bytes]]:
    out = []
    for i, name in enumerate(names):
        with open(os.path.join(folder, name), "rb") as f:
            out.append((start_ms + i * STEP_MS, f.read()))
    return out


def _color_transfer(path: str, ffmpeg: str) -> str:
    """The first video stream's transfer characteristic as ffprobe names it; "" when it can't tell."""
    cmd = [
        ffprobe_path_for(ffmpeg),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=color_transfer",
        "-of",
        "json",
        f"file:{path}",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return ""
    try:
        out, _err = proc.communicate(timeout=PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        kill_and_collect(
            proc, what="ffprobe reading a video for the Inspector", reaper_name=_REAPER, wait_s=_KILL_WAIT_S
        )
        return ""
    try:
        streams = json.loads(out or b"{}").get("streams") or []
    except (ValueError, AttributeError):
        return ""
    return str((streams[0] if streams else {}).get("color_transfer") or "")


def video_filter(width: int, *, hdr: bool, tonemap: str = "hable") -> str:
    """The ``-vf`` chain: one frame a second, scaled to ``width``; HDR tone-mapped as the preview generator does."""
    base = f"fps=1,scale={width}:-2"
    if not hdr:
        return f"{base},format=yuvj420p"
    return (
        f"{base},zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap={tonemap}:desat=0,"
        "zscale=t=bt709:m=bt709:r=tv,format=yuv420p,format=yuvj420p"
    )


def _command(ffmpeg: str, path: str, start_ms: int, count: int, vf: str, out_dir: str) -> list[str]:
    nice = shutil.which("nice")
    prefix = [nice, "-n", _NICE_LEVEL] if nice else []
    return [
        *prefix,
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        # Before -i: a fast seek to the keyframe before, then decoded forward to the exact second.
        "-ss",
        f"{start_ms / 1000:.3f}",
        # "file:" so a name can never be read as another ffmpeg protocol or an option.
        "-i",
        f"file:{path}",
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        vf,
        "-frames:v",
        str(count),
        "-q:v",
        "5",
        "-f",
        "image2",
        os.path.join(out_dir, "%02d.jpg"),
    ]


def _run(cmd: list[str], *, timeout: float) -> tuple[int, bytes]:
    """Run ffmpeg; its exit code and stderr. Past ``timeout`` it is killed and handed to a reaper, not waited for.

    Raises:
        subprocess.TimeoutExpired: It ran past ``timeout``.
        OSError: It couldn't be started.
    """
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_and_collect(proc, what="ffmpeg reading frames for the Inspector", reaper_name=_REAPER, wait_s=_KILL_WAIT_S)
        raise
    return proc.returncode, err or b""


def _prune(root: str, keep: str) -> None:
    """Drop the least recently used answers until the folder is under ``CACHE_MAX_BYTES`` (``keep`` stays)."""
    with _prune_lock:
        entries = []
        total = 0
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if entry.name.startswith("."):
                        # A read's scratch folder, left behind only when the process died mid-read.
                        if time.time() - entry.stat().st_mtime > _STALE_WORK_S:
                            shutil.rmtree(entry.path, ignore_errors=True)
                        continue
                    size = sum(os.path.getsize(os.path.join(entry.path, n)) for n in _frames_in(entry.path))
                    entries.append((entry.stat().st_mtime, entry.path, size))
                    total += size
        except OSError as exc:
            logger.debug("Inspector frames: couldn't list the cache: {}", type(exc).__name__)
            return
        for _mtime, folder, size in sorted(entries):
            if total <= CACHE_MAX_BYTES:
                break
            if folder == keep:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            total -= size


def _extract(binary: str, path: str, start_ms: int, count: int, vf: str, work: str) -> list[str]:
    """One ffmpeg run into ``work``; the frame files it wrote (none when it failed, which is logged)."""
    try:
        code, err = _run(_command(binary, path, start_ms, count, vf, work), timeout=RUN_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise FramesError(f"Reading the frames took longer than {RUN_TIMEOUT_S:.0f} s") from exc
    except OSError as exc:
        raise FramesError(f"Couldn't run ffmpeg ({type(exc).__name__})") from exc
    names = _frames_in(work)
    if not names:
        detail = err.decode("utf-8", "replace").strip().splitlines()
        logger.info(
            "Inspector frames: ffmpeg read nothing at {} ms (exit {}): {}",
            start_ms,
            code,
            detail[-1] if detail else "no error text",
        )
    return names


def exact_frames(
    path: str,
    *,
    start_ms: int,
    count: int,
    width: int = DEFAULT_WIDTH,
    ffmpeg: str | None = None,
    tonemap: str = "hable",
) -> list[tuple[int, bytes]]:
    """``count`` JPEG frames one second apart from ``start_ms``, read from the video itself.

    Args:
        path: The video's local path. The caller validates it: only a file the app already knows reaches here.
        start_ms: The first frame's time.
        count: How many frames (1 to ``MAX_FRAMES``).
        width: Their width in pixels, one of ``WIDTHS``.
        ffmpeg: The ffmpeg binary; ``ffmpeg`` on PATH when None.
        tonemap: The tone-map curve for an HDR10 or HLG video (the previews' setting). An ffmpeg without ``zscale``
            reads the frames again without it.

    Returns:
        ``[(time_ms, jpeg bytes)]`` in order. Fewer than ``count`` when the video ends first.

    Raises:
        ValueError: ``path`` isn't absolute, or ``start_ms``, ``count`` or ``width`` is out of range.
        FramesBusyError: No slot came free within ``SLOT_WAIT_S``.
        FramesError: ffmpeg is missing, failed, timed out or produced nothing, or the cache can't be used.
    """
    if not os.path.isabs(path):
        raise ValueError("path must be absolute")
    if start_ms < 0 or not 1 <= count <= MAX_FRAMES or width not in WIDTHS:
        raise ValueError("start_ms, count or width out of range")
    binary = ffmpeg or shutil.which("ffmpeg")
    if not binary:
        raise FramesError("ffmpeg isn't installed")
    root = cache_root()
    _own_cache(root)
    try:
        folder = os.path.join(root, _key(path, start_ms, count, width))
    except OSError as exc:
        raise FramesError(f"Couldn't read the file ({type(exc).__name__})") from exc
    cached = _frames_in(folder)
    if cached:
        try:
            os.utime(folder)
            return _read(folder, cached, start_ms)
        except OSError:
            # Pruned by another request between the listing and the read: read the frames again.
            pass

    if not _slots.acquire(timeout=SLOT_WAIT_S):
        raise FramesBusyError("Busy reading frames for another request")
    work = ""
    try:
        try:
            work = tempfile.mkdtemp(prefix=".work-", dir=root)
        except OSError as exc:
            raise FramesError(f"Couldn't make room for the frames ({type(exc).__name__})") from exc
        started = time.monotonic()
        hdr = _color_transfer(path, binary) in _HDR_TRANSFERS
        names = _extract(binary, path, start_ms, count, video_filter(width, hdr=hdr, tonemap=tonemap), work)
        if not names and hdr:
            names = _extract(binary, path, start_ms, count, video_filter(width, hdr=False), work)
        if not names:
            raise FramesError("ffmpeg couldn't read frames there")
        frames = _read(work, names, start_ms)
        logger.debug("Inspector frames: {} frames in {:.1f} s", len(frames), time.monotonic() - started)
        try:
            os.rename(work, folder)
            work = ""
        except OSError:
            # Another request stored the same answer first; this copy is dropped below.
            pass
        _prune(root, folder)
        return frames
    finally:
        _slots.release()
        if work:
            shutil.rmtree(work, ignore_errors=True)
