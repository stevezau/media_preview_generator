"""The heavy steps a worker node runs: the app's own functions, unchanged, called with the node's path (spec §3 Ops).

Each op is ``(ctx, node_path, params) -> dict``. ``params`` carry only plain values the app decided (intervals,
quality, tonemap, threads, durations, tracks, windows); the node picks its own FFmpeg the way the app picks its own,
and uses ``ctx.gpu`` for the device. Results are JSON-safe; numpy arrays go as ``protocol.encode_array``.

Progress goes out as ``ctx.progress(percent, args=[...], phase=..., fallbacks=[...])``: ``args`` are the positional
values the local function handed its own progress callback after the percent, ``phase`` the step text, and
``fallbacks`` every GPU-to-CPU note so far. A failure the app knows how to raise again (``nodes.remote_ops``) leaves as
an ``OpError`` carrying ``error_type``, the class name of the exception the local function raised.

Imports of the app's processing code are inside the ops: this module is loaded only when the worker role is on.
"""

from __future__ import annotations

import os
import tarfile
import threading
import uuid
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Literal, overload

from .. import protocol
from ..protocol import OpError, OpFn, TaskContext


def _jsonable(value: object) -> object:
    """``value`` made of JSON types only: dataclasses as dicts, tuples as lists, numpy scalars and arrays unboxed."""
    import numpy as np

    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _node_ffmpeg() -> str:
    """This node's FFmpeg, chosen by the app's own rule (jellyfin-ffmpeg when installed, else ``ffmpeg`` on PATH)."""
    from ...config import _resolve_ffmpeg_path

    return _resolve_ffmpeg_path() or "ffmpeg"


class _Reporter:
    """Keeps the task's progress state and sends all of it on every change (the task keeps only the latest)."""

    def __init__(self, ctx: TaskContext) -> None:
        self._ctx = ctx
        self._percent = 0.0
        self._args: list | None = None
        self._phase: str | None = None
        self._fallbacks: list[str] = []

    def _send(self) -> None:
        extra: dict[str, object] = {}
        if self._args is not None:
            extra["args"] = self._args
        if self._phase is not None:
            extra["phase"] = self._phase
        if self._fallbacks:
            extra["fallbacks"] = list(self._fallbacks)
        self._ctx.progress(self._percent, **extra)

    def worker_progress(self, percent: float, *args: object, **_ignored: object) -> None:
        """A local progress callback: ``(percent, *values)``; keyword extras (a node path) stay here."""
        self._percent = float(percent or 0.0)
        self._args = [_jsonable(a) for a in args]
        self._send()

    def phase(self, text: str) -> None:
        self._phase = str(text or "")
        self._send()

    def fallback(self, reason: str) -> None:
        self._fallbacks.append(str(reason))
        self._send()


@overload
def _choice(params: dict, name: str, allowed: Collection[str]) -> str: ...


@overload
def _choice(params: dict, name: str, allowed: Collection[str], *, optional: Literal[True]) -> str | None: ...


def _choice(params: dict, name: str, allowed: Collection[str], *, optional: bool = False) -> str | None:
    """``params[name]`` when it is one of ``allowed`` (or None when ``optional``).

    These values are written into FFmpeg filter strings, where a comma or a colon starts another filter or option: only
    the names the app itself can send are let through.

    Raises:
        OpError: Kind ``failed``: any other value.
    """
    value = params.get(name)
    if (value is None and optional) or (isinstance(value, str) and value in allowed):
        return value
    raise OpError(protocol.KIND_FAILED, f"{name} must be one of {', '.join(sorted(allowed))}, not {value!r}")


def _optional_float(params: dict, name: str) -> float | None:
    value = params.get(name)
    return None if value is None else float(value)


def _op_error(ctx: TaskContext, exc: BaseException, *, gpu_failed: bool = False, **extra: object) -> OpError:
    """The OpError for an exception the app raises again by name: cancelled once the job is, else a GPU or plain failure."""
    if ctx.cancel_check():
        kind = protocol.KIND_CANCELLED
    elif gpu_failed and ctx.gpu is not None:
        kind = protocol.KIND_GPU_FAILED
    else:
        kind = protocol.KIND_FAILED
    return OpError(kind, str(exc), error_type=type(exc).__name__, **extra)


@dataclass(frozen=True)
class PreviewConfig:
    """Every config value ``generate_images`` (and ``ffmpeg_runner``) reads; the FFmpeg is the node's own."""

    plex_bif_frame_interval: int
    tonemap_algorithm: str
    thumbnail_quality: int
    ffmpeg_threads: int
    log_level: str
    server_display_name: str | None
    ffmpeg_path: str


def preview_frames(ctx: TaskContext, node_path: str, params: dict) -> dict:
    """``generate_images`` into the task's temp dir; the JPEGs (sorted) become the task's artifact tar."""
    from ...config.validation import VALID_TONEMAP_ALGORITHMS
    from ...processing.generator import (
        FALLBACK_HWACCEL,
        FALLBACK_SIGNAL,
        CancellationError,
        CodecNotSupportedError,
        clear_failures,
        failure_scope,
        generate_images,
        get_failures,
    )

    config = PreviewConfig(
        plex_bif_frame_interval=int(params["frame_interval"]),
        tonemap_algorithm=_choice(params, "tonemap_algorithm", VALID_TONEMAP_ALGORITHMS),
        thumbnail_quality=int(params["thumbnail_quality"]),
        ffmpeg_threads=int(params["ffmpeg_threads"]),
        log_level=str(params.get("log_level") or "INFO"),
        server_display_name=params.get("server_display_name"),
        ffmpeg_path=_node_ffmpeg(),
    )
    out = os.path.join(ctx.temp_dir, "frames")
    os.makedirs(out, exist_ok=True)
    gpu, device = ctx.gpu or (None, None)
    reporter = _Reporter(ctx)
    threads = params.get("ffmpeg_threads_override")
    slow_path = False

    def note_slow_path() -> None:
        nonlocal slow_path
        slow_path = True

    # A scope of its own, so generate_images' failure records land somewhere and go back to the app's job.
    with failure_scope(f"node-task-{uuid.uuid4().hex}"):
        try:
            result = generate_images(
                node_path,
                out,
                gpu,
                device,
                config,  # type: ignore[arg-type]
                reporter.worker_progress,
                ffmpeg_threads_override=None if threads is None else int(threads),
                cancel_check=ctx.cancel_check,
                pause_check=ctx.pause_check,
                slow_path_callback=note_slow_path,
            )
            failures = get_failures()
        except CodecNotSupportedError as exc:
            # A hardware accelerator error or a signal is the GPU's; the rest (a codec, a stall, I/O) only says the
            # file needs the CPU.
            on_gpu = exc.kind in (FALLBACK_HWACCEL, FALLBACK_SIGNAL) and gpu is not None
            kind = protocol.KIND_GPU_FAILED if on_gpu else protocol.KIND_CODEC_NOT_SUPPORTED
            raise OpError(kind, str(exc), fallback_kind=exc.kind) from exc
        except CancellationError as exc:
            raise OpError(protocol.KIND_CANCELLED, str(exc), error_type="CancellationError") from exc
        finally:
            clear_failures()
    names = sorted(name for name in os.listdir(out) if name.endswith(".jpg"))
    tar_path = os.path.join(ctx.temp_dir, "frames.tar")
    with tarfile.open(tar_path, "w") as tar:
        for name in names:
            tar.add(os.path.join(out, name), arcname=name)
    ctx.set_artifact(tar_path)
    return {
        "generate_images": _jsonable(list(result)),
        "frames": len(names),
        "slow_path": slow_path,
        "failures": [
            {"exit_code": f.get("exit_code", 0), "reason": f.get("reason", ""), "worker_type": f.get("worker_type", "")}
            for f in failures
        ],
    }


def loudness_analyze(ctx: TaskContext, node_path: str, params: dict) -> dict:
    """``analyze.run`` (one track) or ``analyze.run_many`` (``batch``) with the node's loudness FFmpeg."""
    from ...loudness import analyze

    tracks = [(int(index), str(codec), None if ms is None else int(ms)) for index, codec, ms in params["tracks"]]
    ffmpeg = analyze.resolve_ffmpeg(_node_ffmpeg())
    nice_level = int(params.get("nice_level", 10))
    reporter = _Reporter(ctx)
    longest_s = max(((ms or 0) / 1000 for _i, _c, ms in tracks), default=0.0)

    def on_progress(processed_s: float, speed: float | None) -> None:
        percent = min(100.0, processed_s / longest_s * 100) if longest_s > 0 else 0.0
        reporter.worker_progress(percent, processed_s, speed)

    try:
        if params.get("batch"):
            results = analyze.run_many(
                ffmpeg,
                node_path,
                tracks,
                cancel_check=ctx.cancel_check,
                pause_check=ctx.pause_check,
                on_progress=on_progress,
                nice_level=nice_level,
            )
            return {
                "results": [
                    {"index": index, "fields": value}
                    if isinstance(value, dict)
                    else {"index": index, "error_type": type(value).__name__, "message": str(value)}
                    for index, value in results.items()
                ]
            }
        index, codec, duration_ms = tracks[0]
        fields = analyze.run(
            ffmpeg,
            node_path,
            index,
            duration_ms=duration_ms,
            codec=codec,
            cancel_check=ctx.cancel_check,
            pause_check=ctx.pause_check,
            on_progress=on_progress,
            nice_level=nice_level,
        )
    except analyze.LoudnessError as exc:
        raise _op_error(ctx, exc) from exc
    return {"fields": fields}


def intro_fingerprint(ctx: TaskContext, node_path: str, params: dict) -> dict:
    """``compute_fingerprint`` with the node's chromaprint FFmpeg; the points as base64 little-endian u32."""
    from ...markers.audio import fingerprint
    from ...markers.progress import StepProgress

    ffmpeg = fingerprint.chromaprint_ffmpeg(_node_ffmpeg())
    if ffmpeg is None:
        raise OpError(protocol.KIND_FAILED, "This node has no ffmpeg with chromaprint", error_type="FingerprintError")
    reporter = _Reporter(ctx)
    try:
        points = fingerprint.compute_fingerprint(
            node_path,
            int(params["duration_ms"]),
            ffmpeg=ffmpeg,
            cancel_check=ctx.cancel_check,
            retime=params.get("retime"),
            pause_check=ctx.pause_check,
            progress=(
                (lambda total_s, clock: StepProgress(reporter.worker_progress, total_s=total_s, clock=clock))
                if params.get("progress")
                else None
            ),
        )
    except fingerprint.FingerprintError as exc:
        raise _op_error(ctx, exc) from exc
    return {"points": protocol.encode_array(points, "uint32")}


def intro_end_picture(ctx: TaskContext, node_path: str, params: dict) -> dict:
    """``decode_frames`` on the task's device; each frame as base64 f32 with its shape."""
    from ...markers.audio import end_picture
    from ...markers.credits import frames

    gpu, device = ctx.gpu or (None, None)
    reporter = _Reporter(ctx)
    threads = params.get("ffmpeg_threads")
    try:
        decoded = end_picture.decode_frames(
            node_path,
            float(params["start_s"]),
            float(params["length_s"]),
            ffmpeg=_node_ffmpeg(),
            gpu=gpu,
            gpu_device_path=device,
            container_start_s=float(params["container_start_s"]),
            cancel_check=ctx.cancel_check,
            download_format=_choice(params, "download_format", set(frames.DOWNLOAD_FORMATS.values()), optional=True),
            pause_check=ctx.pause_check,
            ffmpeg_threads=None if threads is None else int(threads),
            fallback_callback=reporter.fallback,
            progress_callback=reporter.worker_progress if params.get("progress") else None,
        )
    except end_picture.GpuAttemptFailedError as exc:
        raise _op_error(ctx, exc, gpu_failed=True) from exc
    except frames.GpuDecodeError as exc:
        raise _op_error(ctx, exc, stderr_tail=list(exc.stderr_tail)) from exc
    except (frames.FrameDecodeError, frames.DecodeCancelledError) as exc:
        raise _op_error(ctx, exc) from exc
    return {"frames": [{"t": float(t), "frame": protocol.encode_array(a, "float32")} for t, a in decoded]}


# The app caps its CPU text-detection helpers at its saved CPU worker count; a node has no settings, and runs exactly
# the CPU credits tasks the app's members send it. So its cap is the most of them it has run at once: never lower, so
# a helper isn't stopped between one file and the next only to be started again (idle helpers leave on their own).
_credits_lock = threading.Lock()
_cpu_credits_running = 0
_cpu_credits_most = 0


def _cpu_credits_cap() -> int:
    with _credits_lock:
        return _cpu_credits_most


@contextmanager
def _counted_credits_task(*, on_cpu_worker: bool) -> Iterator[None]:
    """Count a CPU worker's credits task as running while inside (a GPU worker's text detection is never capped)."""
    global _cpu_credits_running, _cpu_credits_most
    if not on_cpu_worker:
        yield
        return
    with _credits_lock:
        _cpu_credits_running += 1
        _cpu_credits_most = max(_cpu_credits_most, _cpu_credits_running)
    try:
        yield
    finally:
        with _credits_lock:
            _cpu_credits_running -= 1


def credits_find(ctx: TaskContext, node_path: str, params: dict) -> dict:
    """``find_credits`` with this node's own text-detection pool; its CPU notes go out as progress ``fallbacks``."""
    from ...markers.credits import frames
    from ...markers.credits.detector import find_credits
    from ...markers.credits.textdet_helper import (
        TextDetCancelledError,
        TextDetUnavailableError,
        get_textdet_pool,
        set_cpu_limit_source,
    )
    from ...processing.generator import FALLBACK_HWACCEL, FALLBACK_SIGNAL

    gpu, device = ctx.gpu or (None, None)
    set_cpu_limit_source(_cpu_credits_cap)
    pool = get_textdet_pool()
    reporter = _Reporter(ctx)
    detect: dict[str, Any] = {
        "gpu": gpu,
        "gpu_device_path": device,
        "gpu_worker": bool(params.get("gpu_worker")),
        "on_cpu": reporter.fallback,
        "cancel_check": ctx.cancel_check,
    }
    threads = params.get("ffmpeg_threads")
    with _counted_credits_task(on_cpu_worker=not detect["gpu_worker"]):
        try:
            result = find_credits(
                node_path,
                duration_ms=int(params["duration_ms"]),
                is_episode=bool(params["is_episode"]),
                tail_s=_optional_float(params, "tail_s"),
                ffmpeg=_node_ffmpeg(),
                detect_boxes=lambda planes: pool.detect_boxes(planes, **detect),
                read_text=(lambda planes: pool.read_text(planes, **detect)) if params.get("read_text", True) else None,
                gpu=gpu,
                gpu_device_path=device,
                cancel_check=ctx.cancel_check,
                phase=reporter.phase,
                earliest_start_s=_optional_float(params, "earliest_start_s"),
                pause_check=ctx.pause_check,
                ffmpeg_threads=None if threads is None else int(threads),
                progress_callback=reporter.worker_progress if params.get("progress") else None,
            )
        except frames.GpuReadNothingError as exc:
            # Not the GPU's failure: the app reads the file again on the CPU to tell (detect_credits_text).
            raise _op_error(ctx, exc) from exc
        except frames.GpuDecodeError as exc:
            # As preview_frames tells them: only a hardware accelerator error or a signal is the GPU's (and re-checks
            # it here and on the app); a codec it can't decode, an I/O error or an unnamed failure needs the CPU.
            raise _op_error(
                ctx,
                exc,
                gpu_failed=exc.fallback_kind in (FALLBACK_HWACCEL, FALLBACK_SIGNAL),
                stderr_tail=list(exc.stderr_tail),
                fallback_kind=exc.fallback_kind,
            ) from exc
        except (
            frames.FrameDecodeError,
            frames.DecodeCancelledError,
            TextDetCancelledError,
            TextDetUnavailableError,
        ) as exc:
            raise _op_error(ctx, exc) from exc
    return {"result": _jsonable(result)}


def load_op_code() -> None:
    """Import the app code the ops run, once, on the thread that starts the node.

    Its modules import each other in a circle: two tasks importing them for the first time at once deadlock on
    Python's module locks, and one of them fails.
    """
    from ...config import validation  # noqa: F401
    from ...loudness import analyze  # noqa: F401
    from ...markers import progress  # noqa: F401
    from ...markers.audio import end_picture, fingerprint  # noqa: F401
    from ...markers.credits import detector, frames, textdet_helper  # noqa: F401
    from ...processing import generator  # noqa: F401


OPS: dict[str, OpFn] = {
    protocol.OP_PREVIEW_FRAMES: preview_frames,
    protocol.OP_LOUDNESS_ANALYZE: loudness_analyze,
    protocol.OP_INTRO_FINGERPRINT: intro_fingerprint,
    protocol.OP_INTRO_END_PICTURE: intro_end_picture,
    protocol.OP_CREDITS_FIND: credits_find,
}
