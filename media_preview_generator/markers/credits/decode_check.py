"""Whether a GPU decodes credits frames exactly as the CPU does: a diagnostic, run once per device per process
(spec §5.4).

A credits answer depends on the decoder alone: every path downloads the whole decoded frame and shrinks it with the
same software scaler (``frames._scale_filter``). NVIDIA and Intel VAAPI decode H.264 and HEVC bit-exactly against the
CPU (no pixel differed on 8 real files), but no other hardware was measured. So on a device's first credits decode, two
packaged reference clips go through the credits decode's own command (``frames.decode_rows``) on that device and on the
CPU, at both sizes credits are read at, and every frame's Y plane is compared. The result is only logged: a match as an
info line, a difference, decode error or timeout as one warning. The work never moves: a GPU worker decodes credits on
its GPU, every codec included (the owner's worker model: users choose GPU or CPU workers, often to take the work off
the CPU). A GPU that can't decode a file at all is another matter: its ``frames.GpuDecodeError`` still sends that file
to the worker's CPU rerun. Text detection has its own per-device self-test (``textdet_helper``).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import time
from collections.abc import Callable, Hashable, Iterator
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from loguru import logger

from ...processing.hwaccel import hwaccel_decode_args
from ..locks import KeyedLocks
from . import frames
from .rule_j import Box
from .textdet_helper import device_key

CLIPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reference_clips")
# 1 and ``detector.RETRY_SCALE``: 320x180, and the 640x360 a tail without an answer is read again at.
SCALES = (1, 2)
# The whole check on one device, CPU side included. On storage's P5000 it takes 2.9 s: 2.4 s for the four GPU decodes,
# most of it CUDA starting up once per decode, and 0.5 s for the CPU's, which every later device reuses. The limit
# leaves room for a GPU busy with preview work.
CHECK_TIMEOUT_S = 60.0
# How often a worker waiting for another worker's check asks whether its own job was cancelled.
WAIT_POLL_S = 0.1

Frames = tuple[tuple[float, str], ...]


@dataclass(frozen=True)
class ReferenceClip:
    """One packaged clip (made by ``reference_clips/make_reference_clips.sh``).

    Attributes:
        name: Its file name in ``CLIPS_DIR``.
        download_format: The format its decoded GPU surfaces are downloaded in, as ``frames.keyframe_thinning`` probes
            it for a file of this pixel format.
    """

    name: str
    download_format: str

    @property
    def path(self) -> str:
        return os.path.join(CLIPS_DIR, self.name)


# 8-bit H.264 and 10-bit HEVC.
REFERENCE_CLIPS = (ReferenceClip("h264-8bit.mkv", "nv12"), ReferenceClip("hevc-10bit.mkv", "p010le"))


class ClipDecoder(Protocol):
    def __call__(
        self,
        ffmpeg: str,
        clip: ReferenceClip,
        *,
        scale: int,
        gpu: str | None,
        gpu_device_path: str | None,
        cancel_check: Callable[[], bool] | None,
        timeout_s: float,
    ) -> Frames: ...


def decode_clip(
    ffmpeg: str,
    clip: ReferenceClip,
    *,
    scale: int,
    gpu: str | None,
    gpu_device_path: str | None,
    cancel_check: Callable[[], bool] | None,
    timeout_s: float,
) -> Frames:
    """Decode a reference clip the way a credits refine window is decoded, every frame kept.

    The command is ``frames.decode_command``'s own, as a 1 fps refine decode builds it: the device's hwaccel arguments,
    the surfaces downloaded whole and the shared scaler. The clips are 1 fps, so every frame is decoded and kept,
    keyframes and the frames between them alike; the tail's keyframe pass decodes those same keyframes, only skipping
    the rest (``-skip_frame nokey``).

    Args:
        ffmpeg: The ffmpeg binary the job decodes with.
        clip: The clip.
        scale: Frames this many times 320x180.
        gpu: The GPU type, None for the CPU.
        gpu_device_path: The GPU's device.
        cancel_check: True once the job is cancelled.
        timeout_s: Time limit for the decode.

    Returns:
        Each frame's timestamp and its Y plane's SHA-256, in decode order.

    Raises:
        frames.DecodeCancelledError: Cancelled.
        frames.FrameDecodeError: The decode failed, gave no frames on a GPU, or ran past ``timeout_s``
            (``frames.DecodeTimeoutError``).
    """
    digests: list[str] = []

    def digest(planes: np.ndarray) -> list[tuple[Box, ...]]:
        digests.extend(hashlib.sha256(plane.tobytes()).hexdigest() for plane in planes)
        return [()] * len(planes)

    rows = frames.decode_rows(
        clip.path, ffmpeg=ffmpeg, start_s=0.0, length_s=None, keyframes_only=False, fps=1, gpu=gpu,
        gpu_device_path=gpu_device_path, detect_boxes=digest, cancel_check=cancel_check, timeout_s=timeout_s,
        start_time_s=0.0, scale=scale, download_format=clip.download_format,
    )  # fmt: skip
    if len(rows) != len(digests):
        raise frames.FrameDecodeError(f"{len(digests) - len(rows)} frames of {clip.name} had no timestamp")
    return tuple((row[0], found) for row, found in zip(rows, digests, strict=True))


class DecodeChecks:
    """Each GPU device's check, run once per process on the first credits decode for it."""

    def __init__(
        self,
        *,
        decode: ClipDecoder = decode_clip,
        clips: tuple[ReferenceClip, ...] = REFERENCE_CLIPS,
        timeout_s: float = CHECK_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the checks (none run until a device's first credits decode).

        Args:
            decode: Decodes one clip at one scale on one device (tests pass a fake).
            clips: The reference clips.
            timeout_s: Time limit for one device's whole check.
            clock: Monotonic seconds.
        """
        self._decode, self._clips, self._timeout_s, self._clock = decode, clips, timeout_s, clock
        self._verdicts: dict[tuple[str, str], bool] = {}  # (ffmpeg, device key) → its frames matched the CPU's
        self._cpu_frames: dict[tuple[str, str, int], Frames] = {}  # (ffmpeg, clip, scale) → the CPU's frames
        self._guard = threading.Lock()
        self._device_locks = KeyedLocks()
        self._cpu_locks = KeyedLocks()

    def check_device(
        self,
        gpu: str | None,
        gpu_device_path: str | None,
        *,
        ffmpeg: str,
        cancel_check: Callable[[], bool] | None = None,
    ) -> bool | None:
        """Check a worker's GPU once per process, and log what it found. Never changes where anything decodes.

        The first caller for a device runs its check; one arriving meanwhile doesn't wait for a diagnostic and goes on
        decoding. A cancelled check is not kept, so the next caller runs it again. A worker whose decodes never reach a
        GPU (a CPU worker, a GPU without a usable device) runs none.

        Args:
            gpu: The worker's GPU type, None on a CPU worker.
            gpu_device_path: The worker's device.
            ffmpeg: The ffmpeg binary the job decodes with.
            cancel_check: True once the job is cancelled.

        Returns:
            True when the device's frames matched the CPU's, False when they didn't or it couldn't decode the clips in
            time, None when no check answered for this call (not a GPU decode, or another worker's check running).

        Raises:
            frames.DecodeCancelledError: The job was cancelled during the check.
        """
        if gpu is None or not hwaccel_decode_args(gpu, gpu_device_path, keep_on_gpu=False).active:
            return None
        device = device_key(gpu, gpu_device_path)
        key = (ffmpeg, device)
        with self._guard:
            if key in self._verdicts:
                return self._verdicts[key]
        with self._device_locks.try_hold(key, 0) as held:
            if not held:
                return None
            with self._guard:
                verdict = self._verdicts.get(key)
            if verdict is None:
                verdict = self._check(device, gpu, gpu_device_path, ffmpeg, cancel_check)
                with self._guard:
                    self._verdicts[key] = verdict
        return verdict

    @staticmethod
    @contextlib.contextmanager
    def _hold(
        locks: KeyedLocks, key: Hashable, cancel_check: Callable[[], bool] | None, waiting: str
    ) -> Iterator[None]:
        """Hold ``key``'s lock, waiting for whoever holds it for as long as this job isn't cancelled."""
        while True:
            with locks.try_hold(key, WAIT_POLL_S) as held:
                if held:
                    yield
                    return
            if cancel_check and cancel_check():
                raise frames.DecodeCancelledError(f"cancelled while waiting for {waiting}")

    def _check(
        self,
        device: str,
        gpu: str,
        gpu_device_path: str | None,
        ffmpeg: str,
        cancel_check: Callable[[], bool] | None,
    ) -> bool:
        started = self._clock()
        deadline = started + self._timeout_s
        for clip in self._clips:
            for scale in SCALES:
                problem = self._compare(clip, scale, gpu, gpu_device_path, ffmpeg, cancel_check, deadline)
                if problem is not None:
                    logger.warning(
                        "Credits decoding on {} doesn't match the reference decode: {} at {}: {}. Credits detection "
                        "keeps decoding on this GPU.",
                        device, clip.name, _size(scale), problem,
                    )  # fmt: skip
                    return False
        logger.info(
            "Credits decoding on {} matches the reference decode ({} at {}; checked in {} ms)",
            device,
            " and ".join(clip.name for clip in self._clips),
            " and ".join(_size(scale) for scale in SCALES),
            round((self._clock() - started) * 1000),
        )
        return True

    def _compare(
        self,
        clip: ReferenceClip,
        scale: int,
        gpu: str,
        gpu_device_path: str | None,
        ffmpeg: str,
        cancel_check: Callable[[], bool] | None,
        deadline: float,
    ) -> str | None:
        """Why the GPU's frames of one clip at one scale aren't the CPU's, or None when they are."""
        try:
            expected = self._cpu_reference(clip, scale, ffmpeg, cancel_check, deadline)
        except frames.DecodeCancelledError:
            raise
        except frames.DecodeTimeoutError as exc:
            return f"the check timed out: {exc}"
        except Exception as exc:  # any failure leaves the device unverified; it never fails the job
            return f"the CPU couldn't decode it to compare: {exc}"
        try:
            found = self._decode(ffmpeg, clip, scale=scale, gpu=gpu, gpu_device_path=gpu_device_path,
                                 cancel_check=cancel_check, timeout_s=self._remaining(deadline))  # fmt: skip
        except frames.DecodeCancelledError:
            raise
        except frames.DecodeTimeoutError as exc:
            return f"the check timed out: {exc}"
        except Exception as exc:  # as above
            return f"the GPU couldn't decode it: {exc}"
        return _difference(expected, found)

    def _cpu_reference(
        self,
        clip: ReferenceClip,
        scale: int,
        ffmpeg: str,
        cancel_check: Callable[[], bool] | None,
        deadline: float,
    ) -> Frames:
        """The CPU's frames of a clip, decoded once per process and shared by every device's check.

        Waiting for another device's check to decode them counts against this check's own time limit.
        """
        key = (ffmpeg, clip.name, scale)
        with self._hold(self._cpu_locks, key, cancel_check, f"the CPU's decode of {clip.name}"):
            with self._guard:
                found = self._cpu_frames.get(key)
            if found is None:
                found = self._decode(ffmpeg, clip, scale=scale, gpu=None, gpu_device_path=None,
                                     cancel_check=cancel_check, timeout_s=self._remaining(deadline))  # fmt: skip
                with self._guard:
                    self._cpu_frames[key] = found
        return found

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise frames.DecodeTimeoutError(f"it ran past {self._timeout_s:g} s")
        return remaining


def _size(scale: int) -> str:
    return f"{frames.FRAME_W * scale}x{frames.FRAME_H * scale}"


def _difference(expected: Frames, found: Frames) -> str | None:
    """How the GPU's frames differ from the CPU's: their number, a timestamp, or pixels; None when they don't."""
    if len(found) != len(expected):
        return f"the GPU gave {len(found)} frames, the CPU {len(expected)}"
    for i, ((gpu_pts, _), (cpu_pts, _)) in enumerate(zip(found, expected, strict=True)):
        if gpu_pts != cpu_pts:
            return f"frame {i + 1} of {len(expected)} is at {gpu_pts} s on the GPU, {cpu_pts} s on the CPU"
    differing = [cpu_pts for (_, gpu_y), (cpu_pts, cpu_y) in zip(found, expected, strict=True) if gpu_y != cpu_y]
    if differing:
        return f"{len(differing)} of {len(expected)} frames differ from the CPU's (the first at {differing[0]} s)"
    return None


_checks = DecodeChecks()


def check_device(
    gpu: str | None,
    gpu_device_path: str | None,
    *,
    ffmpeg: str,
    cancel_check: Callable[[], bool] | None = None,
) -> bool | None:
    """The process's :meth:`DecodeChecks.check_device`."""
    return _checks.check_device(gpu, gpu_device_path, ffmpeg=ffmpeg, cancel_check=cancel_check)
