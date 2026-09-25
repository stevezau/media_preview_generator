"""Season audio's end-picture check (spec §5.3): whether a repeated stretch near the start of an episode ends on the
same picture in the episodes it repeats in.

A theme tune ends on the show's title card, the same picture in every episode. A network ident, or music under a cold
open, repeats in the audio too, but it ends over whatever each episode shows at that point. The last 3 s of a
candidate are decoded at 2 fps as 64×36 grey in this episode and in its two partners with the longest matching runs, at
their aligned times. Two frames match when their correlation is above 0.6 (two flat frames: when their mean
brightness is within 12); the candidate passes when, in the median partner, at least 75% of the frame pairs match, or
the last 1.5 s all match on pictures whose inside isn't flat (the same end card after shots that differ: an opening
re-cut in later episodes). A partner with certainly no frames to compare (no video, or none at the instants) doesn't count, and a candidate with
none passes; a file ffprobe or ffmpeg couldn't read is never a pass (``ReadFailedError``).

Decoded with the worker's GPU through the credit text decode (``credits.frames``: its hwaccel arguments, its one
nearest-pixel software scaler, time limit, cancel and stall handling), 320×180 luma averaged down 5×5. That scaler
gives the same frames on every vendor (measured byte for byte on NVIDIA and the CPU here, on Intel for credit text;
AMD untested), so partners decoded by different workers' GPUs can't change the answer.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence

import numpy as np
from loguru import logger

from ..credits import frames
from ..probe import ProbeError, ProbeStalledError, StreamStarts, ffprobe_path_for, stream_starts
from .matcher import Hit

# Stored with every cached share: a change to how pictures are compared makes them compared again. Season audio's
# answers carry it too (``season.SEASON_AUDIO_ANSWER_VERSION``), so the answers resting on the shares are due again.
# 2: one nearest-pixel scaler on every vendor (was scale_cuda, scale_vaapi, or swscale's bicubic on the CPU).
# 3: the end card (the last 1.5 s matching on pictures that aren't flat passes the stretch).
CHECK_VERSION = 3
# Idents and music beds under a cold open start near the file's start; a candidate starting at or before this is
# checked, a later one is taken as it is (the window the owner's rule was measured with).
EARLY_START_S = 30.0
TAIL_S = 3.0
FPS = 2
PARTNERS = 2
MIN_SHARE = 0.75
MIN_CORRELATION = 0.6
# 64×36 frames whose grey levels vary by less than this are flat (a black or white card): compared by brightness.
FLAT_STD = 4.0
FLAT_MEAN_DIFF = 12.0
# The end card: when the last 1.5 s (3 instants) all match on pictures whose inside isn't flat, the stretch ends on the
# same picture whatever led into it (share 1.0). An anime opening re-cut in later episodes keeps its music and its last
# card while the shots before it change (Tomb Raider King S01E12 against E02-E05: 3 of the 6 instants, all on the
# card). The inside is the frame without a border of 4 rows and 7 columns (about 11 %): a shared fade to black is no
# card, nor is black with a channel's logo in its corner.
END_CARD_INSTANTS = 3
_CARD_BORDER_ROWS = 4
_CARD_BORDER_COLUMNS = 7
FRAME_W = 64
FRAME_H = 36
DECODE_TIMEOUT_S = 120.0
PROBE_TIMEOUT_S = 30.0
# Frames are decoded from half a second before the first sample to half a second after the last, so the frame nearest
# each sample (a 2 fps grid: at most 0.25 s away) is always there.
_PAD_S = 1.0 / FPS
_NEAREST_S = 0.5 / FPS + 0.05
_BLOCK_W = frames.FRAME_W // FRAME_W
_BLOCK_H = frames.FRAME_H // FRAME_H

Frames = list[tuple[float, np.ndarray]]


class CheckUnavailableError(Exception):
    """No verdict this time: cancelled, a stalled mount, or a file that couldn't be read (lately)."""


class ReadFailedError(Exception):
    """ffprobe or ffmpeg couldn't read one file for the check: an error, a non-zero exit or a timeout. Not proof that it
    has nothing to compare, so never a pass: the caller remembers the file for a while and reads it again later.

    Attributes:
        path: The file.
    """

    def __init__(self, path: str, message: str) -> None:
        super().__init__(message)
        self.path = path


def is_early(start_s: float) -> bool:
    """Whether a candidate starting here needs the end-picture check."""
    return start_s <= EARLY_START_S


def sample_times(start_s: float, end_s: float) -> list[float]:
    """The instants compared, in fingerprint time: every half second of the last 3 s (all of a shorter candidate)."""
    return [float(t) for t in np.arange(max(start_s, end_s - TAIL_S), end_s, 1.0 / FPS)]


def partners(members: Sequence[Hit]) -> list[Hit]:
    """The partners compared: the two whose longest hit in the cluster is longest, each with that hit.

    Args:
        members: The cluster's hits.

    Returns:
        Up to two hits, one per partner, longest first (ties in hit order).
    """
    longest: dict[str, Hit] = {}
    for hit in members:
        held = longest.get(hit.partner)
        if held is None or hit.end_s - hit.start_s > held.end_s - held.start_s:
            longest[hit.partner] = hit
    return sorted(longest.values(), key=lambda hit: -(hit.end_s - hit.start_s))[:PARTNERS]


def frames_alike(x: np.ndarray, y: np.ndarray) -> bool:
    """Whether two 64×36 grey frames show the same picture."""
    sx, sy = float(x.std()), float(y.std())
    if sx < FLAT_STD and sy < FLAT_STD:
        return abs(float(x.mean()) - float(y.mean())) < FLAT_MEAN_DIFF
    if sx < FLAT_STD or sy < FLAT_STD:
        return False
    zx, zy = (x - x.mean()).ravel(), (y - y.mean()).ravel()
    return float(zx @ zy / (np.linalg.norm(zx) * np.linalg.norm(zy))) > MIN_CORRELATION


def _card_picture(frame: np.ndarray) -> bool:
    """Whether a frame's inside (``END_CARD_INSTANTS``' border cropped) isn't flat: a card, not black with a logo."""
    inside = frame[_CARD_BORDER_ROWS:-_CARD_BORDER_ROWS, _CARD_BORDER_COLUMNS:-_CARD_BORDER_COLUMNS]
    return float(inside.std()) >= FLAT_STD


def _nearest(decoded: Frames, t: float) -> np.ndarray | None:
    best = min(decoded, key=lambda row: abs(row[0] - t), default=None)
    return best[1] if best is not None and abs(best[0] - t) <= _NEAREST_S else None


def share_alike(times: Sequence[float], target: Frames, target_shift_s: float, partner: Frames,
                partner_shift_s: float) -> float | None:  # fmt: skip
    """The share of instants whose two frames match.

    Args:
        times: :func:`sample_times`.
        target: This episode's frames (seconds from the start of the file, frame).
        target_shift_s: Added to an instant for this episode's file time (its audio offset).
        partner: The partner's frames.
        partner_shift_s: Added to an instant for the partner's file time (the alignment plus its audio offset).

    Returns:
        The share, or None when no instant has a frame in both; 1.0 when the last ``END_CARD_INSTANTS`` instants all
        have a frame in both that match, on a picture whose inside isn't flat (the same end card, whatever led into
        it).
    """
    alike: list[bool | None] = []
    card: list[bool] = []  # alike, on a picture that isn't flat
    for t in times:
        x, y = _nearest(target, t + target_shift_s), _nearest(partner, t + partner_shift_s)
        if x is None or y is None:
            alike.append(None)
            card.append(False)
        else:
            alike.append(frames_alike(x, y))
            card.append(alike[-1] and _card_picture(x))
    known = [verdict for verdict in alike if verdict is not None]
    if not known:
        return None
    if len(card) >= END_CARD_INSTANTS and all(card[-END_CARD_INSTANTS:]):
        return 1.0
    return sum(known) / len(known)


def passes(shares: Sequence[float | None]) -> bool:
    """A candidate's verdict from its partners' shares: the median of those with frames to compare is at least 75%
    (True when none has any: the check couldn't be made)."""
    known = [share for share in shares if share is not None]
    return not known or float(np.median(known)) >= MIN_SHARE


def _shrink(planes: np.ndarray) -> np.ndarray:
    """320×180 luma planes to 64×36 by averaging 5×5 blocks."""
    n = len(planes)
    return planes.reshape(n, FRAME_H, _BLOCK_H, FRAME_W, _BLOCK_W).mean(axis=(2, 4), dtype=np.float32)


def decode_frames(
    path: str,
    start_s: float,
    length_s: float,
    *,
    ffmpeg: str,
    gpu: str | None,
    gpu_device_path: str | None,
    container_start_s: float,
    cancel_check: Callable[[], bool] | None = None,
    download_format: str | None,
) -> Frames:
    """Decode a stretch at 2 fps, on the GPU when the worker has one and on the CPU when that fails.

    Args:
        path: The media file (read only).
        start_s: Seconds from the start of the file.
        length_s: How long.
        ffmpeg: ffmpeg binary.
        gpu: The worker's GPU type, None on the CPU.
        gpu_device_path: The worker's device.
        container_start_s: The container's first timestamp (``StreamStarts.container_s``).
        cancel_check: True once the job is cancelled.
        download_format: The format the stream's decoded GPU surfaces are downloaded in (``frames.DOWNLOAD_FORMATS``
            of its pixel format), or None: ffmpeg downloads each frame itself.

    Returns:
        (seconds from the start of the file, 64×36 grey frame) per decoded frame.

    Raises:
        frames.DecodeCancelledError: Cancelled.
        frames.DecodeTimeoutError: The decode ran past ``DECODE_TIMEOUT_S``.
        frames.FrameDecodeError: ffmpeg couldn't decode the stretch on the CPU either, or gave a frame no timestamp.
    """
    name = os.path.basename(path)
    try:
        return _decode(path, start_s, length_s, ffmpeg, gpu, gpu_device_path, container_start_s, cancel_check,
                       download_format)  # fmt: skip
    except frames.GpuDecodeError as exc:
        if gpu is None:
            raise
        logger.debug("The end-picture check decodes {} on the CPU: {}", name, exc)
    return _decode(path, start_s, length_s, ffmpeg, None, None, container_start_s, cancel_check, download_format)


def _decode(
    path: str,
    start_s: float,
    length_s: float,
    ffmpeg: str,
    gpu: str | None,
    gpu_device_path: str | None,
    container_start_s: float,
    cancel_check: Callable[[], bool] | None,
    download_format: str | None,
) -> Frames:
    command, hw_active = frames.decode_command(
        ffmpeg, path, start_s=start_s, length_s=length_s, keyframes_only=False, fps=FPS, gpu=gpu,
        gpu_device_path=gpu_device_path, download_format=download_format,
    )  # fmt: skip
    planes: list[np.ndarray] = []

    def keep(chunk: np.ndarray) -> list[tuple]:
        planes.append(_shrink(chunk))
        return [()] * len(chunk)

    rows = frames.run_decode(command, hw_active=hw_active, detect_boxes=keep, pts_offset_s=container_start_s,
                             cancel_check=cancel_check, timeout_s=DECODE_TIMEOUT_S, name=os.path.basename(path))  # fmt: skip
    decoded = np.concatenate(planes) if planes else np.zeros((0, FRAME_H, FRAME_W), dtype=np.float32)
    if len(rows) != len(decoded):
        raise frames.FrameDecodeError(f"ffmpeg gave {len(decoded) - len(rows)} frames of {path} no timestamp")
    return [(row[0], frame) for row, frame in zip(rows, decoded, strict=True)]


def window(times: Sequence[float], shift_s: float) -> tuple[float, float]:
    """The stretch of a file to decode for these instants: (start, length) in seconds from the start of the file."""
    start = max(0.0, times[0] + shift_s - _PAD_S)
    return start, times[-1] + shift_s + _PAD_S - start


class Reader:
    """Measures end-picture shares, reading each file's start times and each decoded stretch once.

    Only a file that ffprobe and ffmpeg read cleanly gives an answer: a share, or None when there is certainly nothing
    to compare (no video stream, or ffmpeg exited cleanly without frames at the instants). A file that couldn't be read
    raises :class:`ReadFailedError` naming it, once per run; a cancel, and ffprobes stalled on earlier files, raise
    :class:`CheckUnavailableError`.
    """

    def __init__(
        self,
        *,
        ffmpeg: str,
        gpu: str | None = None,
        gpu_device_path: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        """Set up the reader.

        Args:
            ffmpeg: ffmpeg binary (ffprobe is taken from beside it).
            gpu: The worker's GPU type, None on the CPU.
            gpu_device_path: The worker's device.
            cancel_check: True once the job is cancelled.
        """
        self._ffmpeg, self._gpu, self._gpu_device_path = ffmpeg, gpu, gpu_device_path
        self._cancel_check = cancel_check
        self._starts: dict[str, StreamStarts] = {}
        self._frames: dict[tuple[str, float, float], Frames] = {}
        self._failed: dict[str, ReadFailedError] = {}

    def share(self, target: str, partner: str, start_s: float, end_s: float, offset_s: float) -> float | None:
        """The share of matching frames at the end of a candidate and the same stretch of a partner.

        Args:
            target: The episode's path.
            partner: The partner's path.
            start_s: The candidate's start, fingerprint time on the episode's side.
            end_s: Its end.
            offset_s: The partner's time minus the episode's (``Hit.partner_start_s - Hit.start_s``).

        Returns:
            The share, or None when there are certainly no frames to compare.

        Raises:
            ReadFailedError: ffprobe or ffmpeg couldn't read one of the files (an error, a non-zero exit, a timeout).
            CheckUnavailableError: Cancelled, or earlier ffprobes are still stuck on their files.
        """
        times = sample_times(start_s, end_s)
        own_starts, partner_starts = self._read_starts(target), self._read_starts(partner)
        if not times or not own_starts.has_video or not partner_starts.has_video:
            return None
        own_shift = own_starts.audio_offset_s
        partner_shift = offset_s + partner_starts.audio_offset_s
        own = self._decoded(target, own_starts, times, own_shift)
        theirs = self._decoded(partner, partner_starts, times, partner_shift)
        return share_alike(times, own, own_shift, theirs, partner_shift)

    def _read_starts(self, path: str) -> StreamStarts:
        if path in self._failed:
            raise self._failed[path]
        if path not in self._starts:
            if self._cancel_check and self._cancel_check():
                raise CheckUnavailableError("cancelled")
            try:
                self._starts[path] = stream_starts(
                    path, ffprobe=ffprobe_path_for(self._ffmpeg), timeout_s=PROBE_TIMEOUT_S
                )
            except ProbeStalledError as exc:
                raise CheckUnavailableError(str(exc)) from exc
            except ProbeError as exc:  # a timeout included
                raise self._fail(path, exc) from exc
        return self._starts[path]

    def _decoded(self, path: str, starts: StreamStarts, times: Sequence[float], shift_s: float) -> Frames:
        if path in self._failed:
            raise self._failed[path]
        start_s, length_s = window(times, shift_s)
        key = (path, start_s, length_s)
        if key not in self._frames:
            try:
                self._frames[key] = decode_frames(
                    path,
                    start_s,
                    length_s,
                    ffmpeg=self._ffmpeg,
                    gpu=self._gpu,
                    gpu_device_path=self._gpu_device_path,
                    container_start_s=starts.container_s,
                    cancel_check=self._cancel_check,
                    download_format=frames.DOWNLOAD_FORMATS.get(starts.pix_fmt or ""),
                )
            except frames.DecodeCancelledError as exc:
                raise CheckUnavailableError("cancelled") from exc
            except frames.FrameDecodeError as exc:  # a timeout or a non-zero exit on the CPU included
                raise self._fail(path, exc) from exc
        return self._frames[key]

    def _fail(self, path: str, exc: Exception) -> ReadFailedError:
        logger.info("The end-picture check couldn't read {}: {}", os.path.basename(path), exc)
        self._failed[path] = ReadFailedError(path, str(exc))
        return self._failed[path]
