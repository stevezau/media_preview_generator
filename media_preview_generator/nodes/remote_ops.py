"""The app side of the five node ops: each takes the local function's own arguments, runs the op on the current
thread's node (:func:`..executor.run_on_node`) and returns, or raises, exactly what the local function would.

A call site picks one or the other by :func:`..executor.current_placement`::

    run = remote_ops.compute_fingerprint if current_placement() else compute_fingerprint

The ``ffmpeg`` argument the local functions take is accepted and not sent: a node runs its own FFmpeg. A failure the
node names (``NodeTaskError.error_type``) is raised again as the same exception class with the same message; one it
doesn't name stays a ``NodeTaskError`` (a ``RuntimeError``), as an unexpected exception from the local function would
propagate. ``NodeUnavailableError`` and ``FileNotVisibleError`` always propagate: the worker gives the file back, unless
the file isn't the worker's own (a season sibling, read under ``executor.reading_other_file``), which its caller leaves
out.
"""

from __future__ import annotations

import glob
import os
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any

from . import protocol
from .executor import NodeTaskCancelledError, NodeTaskError, run_on_node

if TYPE_CHECKING:
    import numpy as np

    from ..markers.credits.detector import CreditsTextResult

# Builds the local exception from the node's message and the error's extra fields.
Rebuild = Callable[[str, dict], BaseException]


def _plain(cls: type[BaseException]) -> Rebuild:
    return lambda message, _extra: cls(message)


def _rebuilt(exc: NodeTaskError, known: Mapping[str, Rebuild], cancelled: Callable[[], BaseException]) -> BaseException:
    """The local exception for a failed or cancelled task."""
    make = known.get(exc.error_type)
    if make is not None:
        return make(exc.message, exc.extra)
    if isinstance(exc, NodeTaskCancelledError):
        return cancelled()
    # A copy: every caller raises this ``from exc``, and raising exc from itself makes a cause chain with no end.
    return NodeTaskError(exc.kind, exc.message, error_type=exc.error_type, extra=exc.extra)


def _gpu(gpu: str | None, gpu_device_path: str | None) -> tuple[str, str] | None:
    return None if gpu is None else (gpu, gpu_device_path or "")


class _Relay:
    """Turns the task's progress dicts back into the local callbacks: progress values, the step text, fallback notes."""

    def __init__(
        self,
        progress: Callable[[float, list], None] | None = None,
        *,
        phase: Callable[[str], None] | None = None,
        fallback: Callable[[str], None] | None = None,
    ) -> None:
        self._progress = progress
        self._phase = phase
        self._fallback = fallback
        self._last_args: list | None = None
        self._last_phase: str | None = None
        self._fallbacks_seen = 0

    def __call__(self, state: dict) -> None:
        phase = state.get("phase")
        if self._phase is not None and isinstance(phase, str) and phase != self._last_phase:
            self._last_phase = phase
            self._phase(protocol.relayed_text(phase))
        fallbacks = state.get("fallbacks")
        if isinstance(fallbacks, list):
            new, self._fallbacks_seen = fallbacks[self._fallbacks_seen :], max(self._fallbacks_seen, len(fallbacks))
            for reason in new:
                if self._fallback is not None:
                    self._fallback(protocol.relayed_text(str(reason)))
        args = state.get("args")
        if self._progress is not None and isinstance(args, list) and args != self._last_args:
            self._last_args = args
            self._progress(float(state.get("percent") or 0.0), args)


# --- preview.frames --------------------------------------------------------------------------------------------------


def _remove_frames(folder: str) -> None:
    for frame in glob.glob(os.path.join(glob.escape(folder), "*.jpg")):
        try:
            os.remove(frame)
        except OSError:
            pass


def _written_inside(exc: OSError, folder: str) -> bool:
    """Whether ``exc`` names a file in ``folder``."""
    if not isinstance(exc.filename, str):
        return False
    path, root = os.path.realpath(exc.filename), os.path.realpath(folder)
    return os.path.commonpath([path, root]) == root


def generate_images(
    video_file: str,
    output_folder: str,
    gpu: str | None,
    gpu_device_path: str | None,
    config: Any,
    progress_callback: Callable[..., None] | None = None,
    ffmpeg_threads_override: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
    slow_path_callback: Callable[[], None] | None = None,
) -> tuple[bool, int, bool, float, str, str | None]:
    """``processing.generator.generate_images`` on the node: its JPEGs land in ``output_folder`` as they would here.

    Raises:
        CodecNotSupportedError: The node's GPU couldn't produce the preview (the worker reruns it on the node's CPU).
        CancellationError: The job was cancelled.
        WorkingFolderError: The frames couldn't be unpacked into ``output_folder`` (full, read-only, or not writable by
            this app's user); none are kept.
    """
    from ..processing.generator import (
        FOLDER_PERMISSION_ERRNOS,
        FOLDER_WRITE_ERRNOS,
        CancellationError,
        WorkingFolderError,
        record_failure,
    )

    server_name = getattr(config, "server_display_name", None)
    params = {
        "frame_interval": int(config.plex_bif_frame_interval),
        "tonemap_algorithm": str(config.tonemap_algorithm),
        "thumbnail_quality": int(config.thumbnail_quality),
        "ffmpeg_threads": int(config.ffmpeg_threads),
        "log_level": str(getattr(config, "log_level", "INFO") or "INFO"),
        "server_display_name": server_name if isinstance(server_name, str) and server_name else None,
        "ffmpeg_threads_override": None if ffmpeg_threads_override is None else int(ffmpeg_threads_override),
    }
    # generate_images cleans the slot first, so a run only counts the frames it made itself.
    _remove_frames(output_folder)
    relay = None
    if progress_callback is not None:
        relay = _Relay(lambda percent, args: progress_callback(percent, *args, media_file=video_file))
    try:
        result = run_on_node(
            protocol.OP_PREVIEW_FRAMES,
            video_file,
            params,
            gpu=_gpu(gpu, gpu_device_path),
            cancel_check=cancel_check,
            pause_check=pause_check,
            progress_callback=relay,
            artifact_to=output_folder,
        )
    except NodeTaskError as exc:
        raise _rebuilt(
            exc,
            {"CancellationError": _plain(CancellationError)},
            partial(CancellationError, exc.message),
        ) from exc
    except OSError as exc:
        # This app's own folder filled up (or is read-only, or its user may not write there) while the node's frames
        # were unpacked into it: the frames already there are cut short, as a local run's would be. A permission error
        # counts only on a file in the folder: the video this app may not stat fails the same way.
        if exc.errno not in FOLDER_WRITE_ERRNOS or (
            exc.errno in FOLDER_PERMISSION_ERRNOS and not _written_inside(exc, output_folder)
        ):
            raise
        _remove_frames(output_folder)
        raise WorkingFolderError(exc.errno, exc.strerror or os.strerror(exc.errno), output_folder) from exc
    for failure in result.get("failures") or []:
        record_failure(
            video_file,
            int(failure.get("exit_code") or 0),
            protocol.relayed_text(str(failure.get("reason") or "")),
            protocol.relayed_text(str(failure.get("worker_type") or "")),
        )
    # A node older than this field never sends it: the file counts as fast.
    if result.get("slow_path") and slow_path_callback is not None:
        slow_path_callback()
    success, image_count, hw_used, seconds, speed, error_summary = result["generate_images"]
    return (
        bool(success),
        int(image_count),
        bool(hw_used),
        float(seconds),
        protocol.relayed_text(str(speed)),
        None if error_summary is None else protocol.relayed_text(str(error_summary)),
    )


# --- loudness.analyze ------------------------------------------------------------------------------------------------


def _loudness_errors() -> dict[str, Rebuild]:
    from ..loudness import analyze

    return {
        cls.__name__: _plain(cls)
        for cls in (
            analyze.LoudnessError,
            analyze.LoudnessCantMeasure,
            analyze.LoudnessNoDecoder,
            analyze.LoudnessFormatChanged,
            analyze.LoudnessTooManyDropped,
            analyze.LoudnessCancelled,
            analyze.LoudnessTimeout,
        )
    }


def _run_loudness(
    path: str,
    tracks: Sequence[tuple[int, str, int | None]],
    *,
    batch: bool,
    cancel_check: Callable[[], bool] | None,
    pause_check: Callable[[], bool] | None,
    on_progress: Callable[[float, float | None], None] | None,
    nice_level: int,
) -> dict:
    from ..loudness import analyze

    relay = None if on_progress is None else _Relay(lambda _percent, args: on_progress(*args))
    params = {
        "tracks": [[int(i), str(c), None if ms is None else int(ms)] for i, c, ms in tracks],
        "batch": batch,
        "nice_level": nice_level,
    }
    try:
        return run_on_node(
            protocol.OP_LOUDNESS_ANALYZE,
            path,
            params,
            gpu=None,  # loudness is CPU work everywhere
            cancel_check=cancel_check,
            pause_check=pause_check,
            progress_callback=relay,
        )
    except OSError as exc:
        raise analyze.LoudnessError(f"Couldn't read {os.path.basename(path)}: {exc}") from exc
    except NodeTaskError as exc:
        name = os.path.basename(path)
        raise _rebuilt(
            exc, _loudness_errors(), lambda: analyze.LoudnessCancelled(f"Loudness analysis of {name} cancelled")
        ) from exc


def loudness_run(
    ffmpeg: str,
    path: str,
    index: int,
    *,
    duration_ms: int | None,
    codec: str = "",
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
    on_progress: Callable[[float, float | None], None] | None = None,
    nice_level: int = 10,
) -> dict[str, str]:
    """``loudness.analyze.run`` on the node (with its own loudness FFmpeg; ``ffmpeg`` is not sent)."""
    result = _run_loudness(
        path,
        [(index, codec, duration_ms)],
        batch=False,
        cancel_check=cancel_check,
        pause_check=pause_check,
        on_progress=on_progress,
        nice_level=nice_level,
    )
    return {str(k): str(v) for k, v in result["fields"].items()}


def loudness_run_many(
    ffmpeg: str,
    path: str,
    tracks: list[tuple[int, str, int | None]],
    *,
    cancel_check: Callable[[], bool] | None = None,
    pause_check: Callable[[], bool] | None = None,
    on_progress: Callable[[float, float | None], None] | None = None,
    nice_level: int = 10,
) -> dict:
    """``loudness.analyze.run_many`` on the node: each stream's fields, or its (unraised) ``LoudnessError``."""
    from ..loudness import analyze

    result = _run_loudness(
        path,
        tracks,
        batch=True,
        cancel_check=cancel_check,
        pause_check=pause_check,
        on_progress=on_progress,
        nice_level=nice_level,
    )
    errors = _loudness_errors()
    out: dict[int, dict[str, str] | analyze.LoudnessError] = {}
    for row in result["results"]:
        index = int(row["index"])
        if "fields" in row:
            out[index] = {str(k): str(v) for k, v in row["fields"].items()}
        else:
            make = errors.get(str(row.get("error_type")), _plain(analyze.LoudnessError))
            out[index] = make(protocol.relayed_text(str(row.get("message") or "")), {})  # type: ignore[assignment]
    return out


# --- intro.fingerprint -----------------------------------------------------------------------------------------------


def compute_fingerprint(
    path: str,
    duration_ms: int,
    *,
    ffmpeg: str,
    cancel_check: Callable[[], bool] | None = None,
    timeout_s: float | None = None,
    retime: float | None = None,
    pause_check: Callable[[], bool] | None = None,
    progress: Callable[[float, Callable[[], float]], Any] | None = None,
) -> np.ndarray:
    """``markers.audio.fingerprint.compute_fingerprint`` on the node (its own chromaprint FFmpeg and time limit).

    Raises:
        FingerprintError: ffmpeg failed, timed out or the job was cancelled (a ``FingerprintStalledError`` too).
    """
    import time

    from ..markers.audio import fingerprint
    from ..markers.freeze import Freeze

    name = os.path.basename(path)
    clock = Freeze.of(pause_check).clock if pause_check is not None else time.monotonic
    step: list = []

    def on_progress(_percent: float, args: list) -> None:
        done_s, total_s = float(args[0]), float(args[1])
        if not step:
            step.append(progress(total_s, clock) if progress is not None else None)
        if step[0] is not None:
            step[0].update(done_s)

    errors = {
        cls.__name__: _plain(cls)
        for cls in (fingerprint.FingerprintError, fingerprint.FingerprintStalledError, fingerprint._NoDecoderError)
    }
    try:
        result = run_on_node(
            protocol.OP_INTRO_FINGERPRINT,
            path,
            {"duration_ms": int(duration_ms), "retime": retime, "progress": progress is not None},
            gpu=None,  # chromaprint is CPU only
            cancel_check=cancel_check,
            pause_check=pause_check,
            progress_callback=_Relay(on_progress) if progress is not None else None,
        )
    except OSError as exc:
        raise fingerprint.FingerprintError(f"Couldn't read {name}: {exc}") from exc
    except NodeTaskError as exc:
        raise _rebuilt(exc, errors, lambda: fingerprint.FingerprintError(f"Fingerprinting {name} cancelled")) from exc
    return protocol.decode_array(result["points"])


# --- intro.end_picture -----------------------------------------------------------------------------------------------


def _stderr_tail(raw: object) -> tuple[str, ...]:
    """FFmpeg's last lines a node sent with a GPU decode failure, as many as the job log shows, each one line."""
    from ..markers.credits.frames import STDERR_LINES

    lines = raw if isinstance(raw, list) else []
    return tuple(protocol.relayed_text(str(line)) for line in lines[-STDERR_LINES:])


def _frame_errors() -> dict[str, Rebuild]:
    from ..markers.audio.end_picture import GpuAttemptFailedError
    from ..markers.credits import frames
    from ..markers.credits.textdet_helper import TextDetCancelledError, TextDetUnavailableError

    known: dict[str, Rebuild] = {
        cls.__name__: _plain(cls)
        for cls in (
            frames.FrameDecodeError,
            frames.ReadStalledError,
            frames.NoDecoderError,
            frames.DecodeTimeoutError,
            frames.DecodeCancelledError,
            GpuAttemptFailedError,
            TextDetCancelledError,
            TextDetUnavailableError,
        )
    }
    known["GpuReadNothingError"] = lambda message, _extra: frames.GpuReadNothingError(message)
    known["GpuDecodeError"] = lambda message, extra: frames.GpuDecodeError(
        message,
        stderr_tail=_stderr_tail(extra.get("stderr_tail")),
        fallback_kind=protocol.relayed_text(str(extra["fallback_kind"])) if extra.get("fallback_kind") else None,
    )
    return known


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
    pause_check: Callable[[], bool] | None = None,
    ffmpeg_threads: int | None = None,
    fallback_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> list[tuple[float, np.ndarray]]:
    """``markers.audio.end_picture.decode_frames`` on the node: on its GPU, on its CPU when that fails."""
    from ..markers.credits import frames

    relay = _Relay(
        (lambda percent, args: progress_callback(percent, *args)) if progress_callback is not None else None,
        fallback=fallback_callback,
    )
    params = {
        "start_s": float(start_s),
        "length_s": float(length_s),
        "container_start_s": float(container_start_s),
        "download_format": download_format,
        "ffmpeg_threads": ffmpeg_threads,
        "progress": progress_callback is not None,
    }
    try:
        result = run_on_node(
            protocol.OP_INTRO_END_PICTURE,
            path,
            params,
            gpu=_gpu(gpu, gpu_device_path),
            cancel_check=cancel_check,
            pause_check=pause_check,
            progress_callback=relay,
        )
    except OSError as exc:
        raise frames.FrameDecodeError(f"Couldn't read {os.path.basename(path)}: {exc}") from exc
    except NodeTaskError as exc:
        raise _rebuilt(exc, _frame_errors(), lambda: frames.DecodeCancelledError("cancelled")) from exc
    return [(float(row["t"]), protocol.decode_array(row["frame"])) for row in result["frames"]]


# --- credits.find ----------------------------------------------------------------------------------------------------


def _rows(raw: Sequence) -> tuple:
    """Rule J rows back from JSON: ``(pts, box count, luma[, boxes])`` tuples, boxes as tuples of int tuples."""
    rows = []
    for row in raw:
        fields: list[Any] = [float(row[0]), int(row[1]), float(row[2])]
        if len(row) > 3:
            fields.append(tuple(tuple(int(v) for v in box) for box in row[3]))
        rows.append(tuple(fields))
    return tuple(rows)


def credits_result(raw: dict) -> CreditsTextResult:
    """A :class:`CreditsTextResult` from its JSON form (``ops._jsonable``)."""
    from ..markers.credits.detector import CreditsTextResult

    def seconds(value: object) -> float | None:
        return None if value is None else float(value)  # type: ignore[arg-type]

    return CreditsTextResult(
        start_s=seconds(raw.get("start_s")),
        end_s=seconds(raw.get("end_s")),
        key_rows=_rows(raw.get("key_rows") or ()),
        fine_rows=_rows(raw.get("fine_rows") or ()),
        end_rows=_rows(raw.get("end_rows") or ()),
        overlays=tuple((int(box[0]), int(box[1]), int(box[2]), int(box[3])) for box in raw.get("overlays") or ()),
        scale=int(raw.get("scale", 1)),
        run_rows=_rows(raw.get("run_rows") or ()),
        prose_start_s=seconds(raw.get("prose_start_s")),
    )


def find_credits(
    path: str,
    *,
    duration_ms: int,
    is_episode: bool,
    tail_s: float | None = None,
    ffmpeg: str,
    gpu: str | None,
    gpu_device_path: str | None,
    cancel_check: Callable[[], bool] | None = None,
    phase: Callable[[str], None] | None = None,
    earliest_start_s: float | None = None,
    pause_check: Callable[[], bool] | None = None,
    ffmpeg_threads: int | None = None,
    read_text: bool = True,
    progress_callback: Callable[..., None] | None = None,
    gpu_worker: bool = False,
    on_cpu: Callable[[str], None] | None = None,
) -> CreditsTextResult:
    """``markers.credits.detector.find_credits`` on the node, with the node's own text-detection pool.

    Args:
        read_text: Whether the card at a start is read (the local call's ``read_text`` is not None).
        gpu_worker: The text detection's ``gpu_worker`` (``TextDetectorPool.detect_boxes``).
        on_cpu: Told each time the node's text detection ran on the CPU although the worker has a GPU.
        Others: As :func:`find_credits` (``ffmpeg`` is not sent).
    """
    from ..markers.credits import frames

    relay = _Relay(
        (lambda percent, args: progress_callback(percent, *args)) if progress_callback is not None else None,
        phase=phase,
        fallback=on_cpu,
    )
    params = {
        "duration_ms": int(duration_ms),
        "is_episode": bool(is_episode),
        "tail_s": tail_s,
        "earliest_start_s": earliest_start_s,
        "ffmpeg_threads": ffmpeg_threads,
        "gpu_worker": bool(gpu_worker),
        "read_text": bool(read_text),
        "progress": progress_callback is not None,
    }
    try:
        result = run_on_node(
            protocol.OP_CREDITS_FIND,
            path,
            params,
            gpu=_gpu(gpu, gpu_device_path),
            cancel_check=cancel_check,
            pause_check=pause_check,
            progress_callback=relay,
        )
    except OSError as exc:
        raise frames.FrameDecodeError(f"Couldn't read {os.path.basename(path)}: {exc}") from exc
    except NodeTaskError as exc:
        raise _rebuilt(exc, _frame_errors(), lambda: frames.DecodeCancelledError("cancelled")) from exc
    return credits_result(result["result"])
