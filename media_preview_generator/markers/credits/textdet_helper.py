"""Credit text detection in helper processes: one per GPU device, and one per request on the CPU up to the CPU worker
count (spec §5.4, §6.4 item 7).

A GPU worker's text detection runs on its GPU, as its previews do, whenever the GPU's WebGPU session is on a hardware
adapter and finds exactly the CPU's boxes; how fast it is doesn't matter. A failure costs that request only (it is read
on the CPU), and the GPU is tried again after a short back-off that doubles with failures in a row, up to a cap: it is
never given up on, as previews keep trying the GPU on later files.

Why processes: the Vulkan loader reads its environment once per process and NVIDIA needs overrides that hide other
GPUs (a host can have an NVIDIA and an Intel GPU); a driver crash or hang can't take the web app down; the WebGPU plugin
can hang at shutdown (ORT PR #29591).

Protocol, one request at a time per helper:
- helper → parent, once: ``{"ready": true, "backend": "webgpu"|"cpu", "selftest": {...}|null, "reason": str}``, plus
  ``"failed": true`` when a GPU helper is on the CPU because its WebGPU session failed this time
- parent → helper: ``{"id": n, "frames": N, "height": H, "width": W}`` and a newline, then N×H×W bytes of luma
- helper → parent: ``{"id": n, "boxes": [N lists of [left, top, right, bottom]]}`` (one list per frame, in frame
  order) or ``{"id": n, "error": str}``
- stdin closed → the helper exits 0; no request for ``--idle-exit-s`` → it exits 75.

The web app imports this module; it loads no ONNX Runtime or OpenCV (only :func:`main`, in the helper, imports
``textdet``).
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import importlib.util
import json
import math
import os
import queue
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from statistics import median
from typing import Any, BinaryIO

import numpy as np
from loguru import logger

from ..locks import KeyedLocks
from .devices import choose_ep_device, pin_env_to_gpu, worker_pci_bus_id
from .rule_j import Box

MODULE = "media_preview_generator.markers.credits.textdet_helper"
MODEL_ENV = "MEDIA_PREVIEW_TEXTDET_MODEL"
DEFAULT_MODEL_PATH = "/app/models/ch_PP-OCRv4_det_infer.onnx"
CPU_KEY = "cpu"
# ONNX Runtime threads per helper. Each CPU worker's request gets a helper of its own, so a small number keeps the
# total in line with the worker count the user chose.
THREADS = 2
SELFTEST_FRAMES = 20
# The detector reads a tail with no answer at 320x180 again at this many times the size (``detector.RETRY_SCALE``;
# this module can't import the detector), so the self-test also compares the boxes on these many frames of that size.
SELFTEST_LARGE_SCALE = 2
SELFTEST_LARGE_FRAMES = 8
# GPU/CPU pairs, each run back to back. Every round's boxes must match, so a backend whose answers vary from run to
# run fails; the median of the rounds' own GPU/CPU time ratios goes in the log as information and decides nothing.
SELFTEST_ROUNDS = 7
# After a GPU helper fails, the requests of the next GPU_RETRY_BASE_S are read on the CPU before the GPU is tried
# again, the wait doubling with each failure in a row up to GPU_RETRY_MAX_S; a success resets it. Every failure costs
# the request that tries the GPU: a failing start seconds of driver setup (up to START_TIMEOUT_S when it hangs), a
# crashed or hung request its own wait (up to REQUEST_TIMEOUT_S) plus a helper restart and self-test. A GPU that keeps
# failing would pay that before every 64-frame request of every file, each about a second of CPU work; the cap keeps
# a GPU that recovers in use within minutes. There is deliberately no "CPU for the rest of the process" switch.
GPU_RETRY_BASE_S = 5.0
GPU_RETRY_MAX_S = 600.0
# How often a request waiting for a CPU helper reads the saved CPU worker count again, when nothing wakes it sooner.
CPU_WAIT_POLL_S = 1.0
START_TIMEOUT_S = 120.0
REQUEST_TIMEOUT_S = 60.0
IDLE_EXIT_S = 600.0
IDLE_EXIT_CODE = 75
CHECK_ABSENT_CODE = 3
CHECK_TIMEOUT_S = 30.0
CHECK_RETRY_S = 600.0
CLOSE_GRACE_S = 5.0
KILL_WAIT_S = 5.0
EXIT_CODE_WAIT_S = 2.0
# After a stuck write is killed, how long the writer thread may take to unwind its EPIPE before close() gives up
# on it and hands the process to the reaper.
WRITER_UNWIND_S = 1.0
# A helper this close to its idle exit is replaced before a request instead of racing its exit timer.
IDLE_RESTART_MARGIN_S = 5.0
_WEBGPU_VENDORS = frozenset({"NVIDIA", "INTEL", "AMD"})
# VkPhysicalDeviceType values.
VK_OTHER, VK_INTEGRATED_GPU, VK_DISCRETE_GPU, VK_VIRTUAL_GPU, VK_CPU = range(5)
# Vulkan device names of software renderers, whatever device type their driver reports.
_SOFTWARE_RENDERERS = ("llvmpipe", "lavapipe", "swiftshader", "software")
NOT_INSTALLED = "Needs ONNX Runtime and OpenCV, which the Docker image includes; they aren't installed here"
NO_ANSWER = "The text detection check didn't answer; it is checked again in 10 minutes"
_monotonic = time.monotonic
_perf_counter = time.perf_counter


class TextDetState(str, Enum):
    """Whether credit text detection can run in this process's container."""

    AVAILABLE = "available"
    # Packages or model missing, or the check said so: kept for the process.
    ABSENT = "absent"
    # The check timed out, couldn't start or crashed: asked again after CHECK_RETRY_S.
    UNKNOWN = "unknown"


class HelperError(Exception):
    """A helper process didn't start, answer or read a request."""


class HelperStartError(HelperError):
    """A helper process couldn't start, or a GPU helper came up without its GPU this time."""


class TextDetUnavailableError(Exception):
    """Text detection can't answer this time (the CPU helper failed)."""


class TextDetCancelledError(Exception):
    """The job was cancelled while its request waited for a CPU helper.

    Not a ``TextDetUnavailableError``: a caller that keeps an earlier answer when text detection fails (the credits
    detector's 640x360 reading) must never keep one for a cancelled job.
    """


class TextDetShuttingDownError(TextDetUnavailableError):
    """The pool is closed: the app is stopping, so nothing read now may be stored as an answer."""


def model_path() -> str:
    """The model file: ``MEDIA_PREVIEW_TEXTDET_MODEL`` (dev and harness runs), else the image's copy."""
    return os.environ.get(MODEL_ENV) or DEFAULT_MODEL_PATH


_state_lock = threading.Lock()
_state: tuple[TextDetState, str, float | None] | None = None


def _find_spec(name: str) -> Any:
    """``importlib.util.find_spec`` (tests replace this one name, never the process-wide function)."""
    return importlib.util.find_spec(name)


def _run_state_check() -> tuple[TextDetState, str]:
    if any(_find_spec(name) is None for name in ("onnxruntime", "cv2", "pyclipper")):
        return TextDetState.ABSENT, NOT_INSTALLED
    path = model_path()
    if not os.path.isfile(path):
        return (
            TextDetState.ABSENT,
            f"Needs the text detection model, which the Docker image includes; it isn't at {path}",
        )
    try:
        proc = subprocess.run(
            [sys.executable, "-m", MODULE, "--check", "--model", path],
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return TextDetState.UNKNOWN, NO_ANSWER
    if proc.returncode == 0:
        return TextDetState.AVAILABLE, ""
    if proc.returncode == CHECK_ABSENT_CODE:
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        return TextDetState.ABSENT, lines[-1] if lines else NOT_INSTALLED
    logger.debug("Text detection check exited {}: {}", proc.returncode, (proc.stderr or "")[-300:])
    return TextDetState.UNKNOWN, NO_ANSWER


def _cached_state() -> tuple[TextDetState, str]:
    global _state
    with _state_lock:
        if _state is not None and (_state[2] is None or _monotonic() < _state[2]):
            return _state[0], _state[1]
        state, reason = _run_state_check()
        _state = (state, reason, _monotonic() + CHECK_RETRY_S if state is TextDetState.UNKNOWN else None)
        return state, reason


def text_detection_state() -> TextDetState:
    """Whether credit text detection can run in this container (checked once per process; UNKNOWN is asked again
    after 10 min)."""
    return _cached_state()[0]


def text_detection_status() -> tuple[bool, str]:
    """``(available, reason)`` for Settings; the reason is "" when available."""
    state, reason = _cached_state()
    return state is TextDetState.AVAILABLE, reason


def forget_text_detection_state() -> None:
    """Forget the cached check (tests)."""
    global _state
    with _state_lock:
        _state = None


@dataclass(frozen=True)
class SelfTest:
    """A GPU session against the CPU on the same frames, over several back-to-back rounds.

    Attributes:
        gpu_ms: The GPU's median milliseconds per frame (logged, not used to decide).
        cpu_ms: The CPU's median milliseconds per frame (logged, not used to decide).
        ratio: The median of each round's own GPU/CPU time ratio (logged, not used to decide).
        same_boxes: Every frame's boxes matched, corner for corner, in every round.
        same_boxes_large: Every larger frame's boxes matched too (``SELFTEST_LARGE_SCALE``), or none were compared.
    """

    gpu_ms: float
    cpu_ms: float
    ratio: float
    same_boxes: bool
    same_boxes_large: bool = True

    @property
    def use_gpu(self) -> bool:
        """The GPU is used when it finds exactly the CPU's boxes, in the same places and at both sizes, however fast
        it is: a GPU worker's work runs on its GPU, and the credits answer mustn't depend on which one read it."""
        return self.same_boxes and self.same_boxes_large

    def cpu_reason(self) -> str:
        """Why this result keeps the CPU (for the helper's ready line and the log)."""
        if not self.same_boxes:
            return "the GPU was finding different boxes than the CPU"
        return "the GPU was finding different boxes than the CPU at 640x360"


def self_test(
    gpu: Any,
    cpu: Any,
    frames: np.ndarray,
    *,
    clock: Callable[[], float] | None = None,
    warmup: int = 3,
    rounds: int = SELFTEST_ROUNDS,
    large_frames: np.ndarray | None = None,
) -> SelfTest:
    """Run both detectors on the same frames after a warm-up, compare the boxes they find, and time them.

    **The boxes, not their count.** ``count`` is ``len(detect(...))``, so comparing counts costs the same and says
    less: a backend that finds the same *number* of boxes in different *places* passes a count test and then answers
    differently from the CPU everywhere rule J reads a position (``rule_j.overlay_boxes``, ``rule_j.same_roll``,
    ``rule_j.reach_back``). This self-test is the only runtime check there is. Its verdict is kept for the process.

    The pair is run ``rounds`` times, the GPU and then the CPU back to back, and the boxes must match in every round.
    The timing is reported for the log only: the median of the rounds' own GPU/CPU ratios, which load lasting across a
    round (another job's decode) leaves alone, since it slows both halves alike.

    Args:
        gpu: A detector with ``detect(frames) -> list[tuple[Box, ...]]`` on the GPU.
        cpu: The same on the CPU.
        frames: (n, H, W) uint8 luma.
        clock: Seconds (tests pass a fake); ``time.perf_counter`` by default.
        warmup: Frames each detector runs before timing (session start-up and shader compilation: the first WebGPU
            calls take several times as long as later ones).
        rounds: How many GPU/CPU pairs to run.
        large_frames: (n, H, W) uint8 luma at the detector's larger reading's size, each side's boxes compared once
            after the timed rounds (not timed); None compares none.

    Returns:
        Each side's median milliseconds per frame, the median per-round ratio, and whether every frame's
        boxes matched, corner for corner, in every round and on the larger frames.
    """
    now = clock or _perf_counter
    for detector in (gpu, cpu):
        detector.detect(frames[:warmup])

    def timed(detector: Any) -> tuple[list[tuple], float]:
        started = now()
        boxes = detector.detect(frames)
        return boxes, (now() - started) * 1000.0 / len(frames)

    gpu_times: list[float] = []
    cpu_times: list[float] = []
    same_boxes = True
    for _ in range(rounds):
        gpu_boxes, gpu_ms = timed(gpu)
        cpu_boxes, cpu_ms = timed(cpu)
        gpu_times.append(gpu_ms)
        cpu_times.append(cpu_ms)
        same_boxes = same_boxes and list(gpu_boxes) == list(cpu_boxes)
    ratios = [g / c if c > 0 else math.inf for g, c in zip(gpu_times, cpu_times, strict=True)]
    same_large = large_frames is None or list(gpu.detect(large_frames)) == list(cpu.detect(large_frames))
    return SelfTest(
        round(median(gpu_times), 2), round(median(cpu_times), 2), round(median(ratios), 4), same_boxes, same_large
    )


@dataclass(frozen=True)
class HelperSpec:
    """How to start one helper.

    Attributes:
        key: The device key (``device_key``), or a CPU helper's own key (``cpu``, ``cpu#2``, ...).
        backend: ``webgpu`` or ``cpu``.
        pci_bus_id: The worker GPU's PCI address, for picking the EP device.
        selftest: Run the 20-frame self-test (the first start for a device).
        env: The helper's environment: the GPU pin (``pin_env_to_gpu``) and, on NVIDIA, the app's Vulkan
            overrides. Nothing else selects the GPU, so this field is what makes the helper per-device.
    """

    key: str
    backend: str
    pci_bus_id: str | None
    selftest: bool
    env: dict[str, str]


def _report_cpu_fallback(on_cpu: Callable[[str], None] | None, reason: str | None) -> None:
    """Tell the worker a GPU worker's request is read on the CPU (its row's fallback flag). Never raises: the row is
    only a display, and the request still has to be read."""
    if on_cpu is None or not reason:
        return
    try:
        on_cpu(f"Credit text detection on the CPU: {reason}")
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("Couldn't show the text detection CPU fallback on the worker row: {}", exc)


def device_key(gpu: str | None, gpu_device_path: str | None) -> str:
    """The helper a worker's requests go to: its device path (or GPU type), ``cpu`` for a CPU worker.

    A GPU whose device path or type is itself spelt like a CPU helper's key is prefixed so it can never share one.
    """
    if gpu is None:
        return CPU_KEY
    key = gpu_device_path or gpu
    return f"gpu:{key}" if _is_cpu_helper_key(key) else key


def _cpu_helper_key(number: int) -> str:
    """The nth CPU helper's key: ``cpu``, then ``cpu#2``, ``cpu#3``, ..."""
    return CPU_KEY if number == 1 else f"{CPU_KEY}#{number}"


def _is_cpu_helper_key(key: str) -> bool:
    return key == CPU_KEY or key.startswith(f"{CPU_KEY}#")


def helper_command(spec: HelperSpec) -> list[str]:
    """``python -m …textdet_helper`` with the spec's arguments."""
    command = [sys.executable, "-m", MODULE, "--backend", spec.backend, "--model", model_path(),
               "--threads", str(THREADS), "--idle-exit-s", f"{IDLE_EXIT_S:g}"]  # fmt: skip
    if spec.pci_bus_id:
        command += ["--pci-bus-id", spec.pci_bus_id]
    if not spec.selftest:
        command.append("--no-selftest")
    return command


class _Helper:
    """One running helper process and its answer queue."""

    def __init__(self, proc: subprocess.Popen, stderr_file: BinaryIO, request_timeout_s: float) -> None:
        """Wrap a started helper and start reading its output.

        Args:
            proc: The helper, started with pipes for stdin and stdout.
            stderr_file: The temporary file its stderr goes to (read for the tail of a failure).
            request_timeout_s: Longest wait for one request's answer.
        """
        self.proc = proc
        self.ready: dict[str, Any] = {}
        self._stderr = stderr_file
        self._timeout = request_timeout_s
        self._lines: queue.Queue[bytes] = queue.Queue()
        self._next_id = 0
        self._writer: threading.Thread | None = None
        self.last_used = _monotonic()
        threading.Thread(target=self._pump, daemon=True, name="textdet-helper-out").start()

    def _pump(self) -> None:
        try:
            # A closed pipe (the helper is gone, or close() got there first) simply ends the stream.
            with contextlib.suppress(OSError, ValueError):
                for line in iter(self.proc.stdout.readline, b""):
                    self._lines.put(line)
        finally:
            self._lines.put(b"")
        # A helper that left on its idle timer is otherwise reaped only by its device's next request, which may be
        # hours away; until then it shows as <defunct>. A timed wait holds the waitpid lock only for each non-blocking
        # poll, never while it sleeps, so it blocks no other waiter; a poll() landing on one of those instants reads
        # "still running", which every caller already survives (a dead helper fails its request and is replaced).
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=EXIT_CODE_WAIT_S)

    def read_message(self, timeout_s: float) -> dict[str, Any]:
        """The helper's next JSON line.

        Args:
            timeout_s: Longest wait.

        Returns:
            The decoded message.

        Raises:
            HelperError: Nothing arrived in time, the helper exited, or the line wasn't a JSON object.
        """
        try:
            line = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            raise HelperError(f"no answer within {timeout_s:g} s") from None
        if not line:
            raise HelperError(f"the helper exited ({self.proc.poll()}){self._stderr_tail()}")
        try:
            message = json.loads(line)
        except ValueError:
            raise HelperError(f"unreadable answer {line[:80]!r}") from None
        if not isinstance(message, dict):
            raise HelperError(f"unreadable answer {line[:80]!r}")
        return message

    def request(self, planes: np.ndarray) -> list[tuple[Box, ...]]:
        """Each plane's text boxes for (n, H, W) uint8 luma planes.

        Raises:
            HelperError: The helper didn't read the frames, didn't answer, or answered something else.
        """
        frames = np.ascontiguousarray(planes, dtype=np.uint8)
        count, height, width = frames.shape
        self._next_id += 1
        header = json.dumps({"id": self._next_id, "frames": count, "height": height, "width": width}).encode()
        # One budget covers sending and answering, so the worst case is the timeout this pool was given, not twice it.
        deadline = _monotonic() + self._timeout
        self._write(header + b"\n" + frames.tobytes(), deadline)
        reply = self.read_message(max(0.0, deadline - _monotonic()))
        self.last_used = _monotonic()
        boxes = reply.get("boxes")
        if reply.get("id") != self._next_id or "error" in reply or not isinstance(boxes, list) or len(boxes) != count:
            raise HelperError(f"bad answer: {str(reply.get('error') or reply)[:200]}")
        try:
            return [tuple((int(a), int(b), int(c), int(d)) for a, b, c, d in frame) for frame in boxes]
        except (TypeError, ValueError) as exc:
            # A frame's entry that isn't four numbers per box would otherwise reach rule J as a row it can't read.
            raise HelperError(f"bad answer: boxes aren't four numbers each ({exc})") from exc

    def _write(self, data: bytes, deadline: float) -> None:
        failed: list[BaseException] = []

        def write() -> None:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                failed.append(exc)

        writer = threading.Thread(target=write, daemon=True, name="textdet-helper-in")
        self._writer = writer
        writer.start()
        budget = max(0.0, deadline - _monotonic())
        writer.join(budget)
        if writer.is_alive():
            self.kill()  # a helper that stopped reading would hold this write forever
            # The kill closes the read end, so the write is about to fail with EPIPE. Waiting for that here keeps
            # close() from finding a writer that is merely unwinding and warning about a process that is already dead.
            writer.join(WRITER_UNWIND_S)
            raise HelperError(f"the helper didn't read a request within {budget:g} s")
        if failed:
            raise HelperError(f"couldn't send the frames: {failed[0]}")

    def exit_code(self) -> int | None:
        """The helper's exit code, or None while it runs."""
        return self.proc.poll()

    def wait_exit(self, timeout_s: float) -> int | None:
        """The exit code once the helper has exited, waiting up to ``timeout_s`` (None: still running).

        Right after its output ends, a helper leaving on its idle timer may not have been reaped yet; reading ``poll()``
        at once would take that for a crash.
        """
        with contextlib.suppress(subprocess.TimeoutExpired):
            return self.proc.wait(timeout=timeout_s)
        return None

    def _stderr_tail(self) -> str:
        with contextlib.suppress(OSError, ValueError):
            self._stderr.seek(0, os.SEEK_END)
            size = self._stderr.tell()
            self._stderr.seek(max(0, size - 300))
            tail = self._stderr.read().decode("utf-8", "replace").strip()
            return f": {tail}" if tail else ""
        return ""

    def kill(self) -> None:
        """Kill the helper's process group (it runs in its own session) and wait a bounded time for it."""
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=KILL_WAIT_S)

    def close(self, grace_s: float) -> None:
        """Stop the helper without ever blocking on its pipes.

        A write stuck in ``stdin.write`` (a helper that stopped reading, a driver hang) holds the pipe's buffer lock, so
        closing stdin then would block this thread; and a process that doesn't die when killed keeps its pipes. Both
        go to a daemon reaper that closes them once they let go.

        Args:
            grace_s: How long the helper may take to leave on its own after stdin closes.
        """
        writer = self._writer
        if writer is not None and writer.is_alive():
            self.kill()
        else:
            with contextlib.suppress(OSError, ValueError):
                self.proc.stdin.close()
            try:
                self.proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                self.kill()  # the WebGPU plugin can hang at exit
        if self.proc.poll() is None or (writer is not None and writer.is_alive()):
            logger.warning("A text detection helper didn't stop when killed; leaving it to finish on its own")
            threading.Thread(target=self._reap, daemon=True, name="textdet-helper-reaper").start()
            return
        self._close_pipes()

    def _reap(self) -> None:
        if self._writer is not None:
            self._writer.join()
        with contextlib.suppress(OSError):
            self.proc.wait()
        self._close_pipes()

    def _close_pipes(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout, self._stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()


def _start(spec: HelperSpec, *, command: Callable[[HelperSpec], list[str]], popen: Any, start_timeout_s: float,
           request_timeout_s: float) -> _Helper:  # fmt: skip
    """Start one helper and read its ready line.

    Raises:
        HelperError: It couldn't be started, or its first line wasn't a ready line.
    """
    stderr_file = tempfile.TemporaryFile()
    try:
        proc = popen(command(spec), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file, env=spec.env,
                     start_new_session=True)  # fmt: skip
    except OSError as exc:
        stderr_file.close()
        raise HelperStartError(f"couldn't start: {exc}") from exc
    helper = _Helper(proc, stderr_file, request_timeout_s)
    try:
        ready = helper.read_message(start_timeout_s)
        if ready.get("ready") is not True or ready.get("backend") not in ("webgpu", "cpu"):
            raise HelperStartError(f"unexpected start line: {str(ready)[:200]}")
    except HelperError as exc:
        helper.kill()
        helper.close(0)
        if isinstance(exc, HelperStartError):
            raise
        raise HelperStartError(str(exc)) from exc
    helper.ready = ready
    return helper


def _vulkan_device_info() -> Any:
    from ...gpu.vulkan_probe import get_vulkan_device_info

    return get_vulkan_device_info()


def _vulkan_env_overrides() -> dict[str, str]:
    from ...gpu.vulkan_probe import get_vulkan_env_overrides

    return get_vulkan_env_overrides()


@dataclass
class _GpuState:
    """What one GPU device's text detection runs on, and how its last attempts on the GPU went.

    Attributes:
        verdict: ``webgpu`` once a helper came up on the GPU; ``cpu`` once the GPU proved it can't run text detection
            (no WebGPU path, a software renderer, other boxes than the CPU's), kept for the process; None before either.
        failures: Failed attempts on the GPU in a row (a crash, a hang, a start that failed).
        retry_at: ``_monotonic`` time before which the GPU isn't tried again after a start failed.
        cpu_reason: Why its requests are read on the CPU now: the verdict's reason, or the start failure's.
    """

    verdict: str | None = None
    failures: int = 0
    retry_at: float = 0.0
    cpu_reason: str = ""


class TextDetectorPool:
    """Text boxes for luma planes on a worker's device.

    A GPU worker's requests go to its device's GPU helper, one request at a time. A CPU worker's request, or a GPU
    worker's that can't be on its GPU this time, goes to a CPU helper of its own: CPU workers' requests hold at most
    the saved CPU worker count of them at once (at least one), and a GPU worker's request always gets one, as a
    preview's CPU rerun runs on its own worker. A request with no GPU counts as a CPU worker's unless it says
    ``gpu_worker=True``, as a GPU worker's rerun after a failed GPU decode does (the worker's type reaches the detector
    through ``process_fn``, spec §5.4 CPU). A CPU helper is held by one request at a time, so it is started and used by
    one thread at a time.
    """

    def __init__(
        self,
        *,
        command: Callable[[HelperSpec], list[str]] = helper_command,
        popen: Any = subprocess.Popen,
        start_timeout_s: float = START_TIMEOUT_S,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        vulkan_info: Callable[[], Any] | None = None,
        vulkan_env: Callable[[], dict[str, str]] | None = None,
        cpu_limit: Callable[[], int] | None = None,
    ) -> None:
        """Create an empty pool (helpers start on first use).

        Args:
            command: Builds a helper's argv (tests start a fake).
            popen: Starts a process.
            start_timeout_s: Longest wait for a helper's ready line (a WebGPU start with the self-test included).
            request_timeout_s: Longest wait for one request's answer.
            vulkan_info: The app's Vulkan probe result.
            vulkan_env: The app's Vulkan env overrides (applied to NVIDIA helpers only).
            cpu_limit: The saved CPU worker count, read on every CPU request (the app's pool reads the settings); one
                CPU helper when None.
        """
        self._command, self._popen = command, popen
        self._start_timeout_s, self._request_timeout_s = start_timeout_s, request_timeout_s
        self._vulkan_info = vulkan_info or _vulkan_device_info
        self._vulkan_env = vulkan_env or _vulkan_env_overrides
        self._cpu_limit = cpu_limit or (lambda: 1)
        self._helpers: dict[str, _Helper] = {}
        self._gpus: dict[str, _GpuState] = {}  # device key → its state, for the process lifetime
        self._locks = KeyedLocks()
        self._guard = threading.Lock()
        # Everything below is read and changed only while _guard is held (the condition shares it).
        self._cpu_free = threading.Condition(self._guard)
        # Every CPU helper key handed out (its process may have left on its idle timer since), and the idle ones.
        self._cpu_helpers: list[str] = []
        self._cpu_idle: list[str] = []  # the ones no request holds, the most recently used last
        self._cpu_worker_holds = 0  # CPU helpers CPU workers' requests hold now: at most the saved count
        self._gpu_worker_holds = 0  # CPU helpers GPU workers' requests hold now: no limit but the GPU workers
        self._cpu_limit_seen: int | None = None  # the saved count the helpers follow
        # Each read of the saved count takes a ticket; only the latest-started read's value is applied, so a read that
        # began before a save can't undo the resize that save made.
        self._cpu_limit_reads = 0
        self._cpu_limit_applied = 0
        self._closed = False

    def detect_boxes(
        self,
        planes: np.ndarray,
        *,
        gpu: str | None,
        gpu_device_path: str | None,
        gpu_worker: bool | None = None,
        on_cpu: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> list[tuple[Box, ...]]:
        """Text boxes per luma plane on the worker's device.

        Args:
            planes: (n, H, W) uint8.
            gpu: The worker's GPU type, None on a CPU worker.
            gpu_device_path: The worker's device.
            gpu_worker: Whether a GPU worker asks. None means ``gpu is not None``; a GPU worker rereading a file with
                no GPU after its GPU decode failed passes True, so its CPU helper isn't one of the CPU workers'.
            on_cpu: Told why when a request with a GPU is read on the CPU (the worker row's fallback flag).
            cancel_check: True once the job is cancelled; ends a wait for a CPU helper.

        Returns:
            One plane's boxes per plane, each ``(left, top, right, bottom)`` in the plane's own pixels.

        Raises:
            TextDetUnavailableError: The CPU helper failed (the next call starts a new one).
            TextDetCancelledError: The job was cancelled while the request waited for a CPU helper.
            TextDetShuttingDownError: The pool is closed.
        """
        key = device_key(gpu, gpu_device_path)
        self._raise_if_closed()
        if key != CPU_KEY:
            # The device's state is read inside its lock: a second worker reading it while the first is still in the
            # self-test would act on a stale "GPU allowed" and start a second, un-self-tested GPU helper.
            with self._locks.hold(key):
                cpu_reason = self._gpu_refused(key, gpu)
                if cpu_reason is None:
                    try:
                        boxes = self._on_helper(
                            key,
                            planes,
                            lambda: self._gpu_spec(key, gpu, gpu_device_path),
                            accept=lambda ready, spec: self._accept_gpu_helper(key, ready, spec.pci_bus_id),
                        )
                    except HelperError as exc:
                        self._raise_if_closed()  # close_all killed it: the pool ending, not the GPU failing
                        cpu_reason = self._gpu_failed(key, exc)
                    else:
                        if boxes is not None:
                            self._gpu_worked(key)
                            return boxes
                        cpu_reason = self._cpu_reason(key)  # the helper just gave the device its CPU verdict
            _report_cpu_fallback(on_cpu, cpu_reason)
        return self._on_cpu(
            planes, for_gpu_worker=key != CPU_KEY if gpu_worker is None else gpu_worker, cancel_check=cancel_check
        )

    def backend_of(self, gpu: str | None, gpu_device_path: str | None) -> str | None:
        """What a worker's text detection runs on now: ``webgpu``, ``cpu``, or None while its device has no verdict
        and isn't waiting out a failure (its next request tries the GPU).

        The app itself only logs it; this is how the lab scripts and the tests read it.
        """
        key = device_key(gpu, gpu_device_path)
        if key == CPU_KEY:
            return "cpu"
        with self._guard:
            state = self._gpus.get(key)
            if state is None:
                return None
            if state.verdict == "cpu" or _monotonic() < state.retry_at:
                return "cpu"
            return state.verdict

    def reconcile_cpu_helpers(self) -> None:
        """Bring the CPU helpers to the saved CPU worker count now: idle ones beyond it stop at once, busy ones when
        their request ends, and a higher count lets waiting requests start helpers (a settings save calls this; every
        CPU request also reads the count)."""
        self._stop_helpers(self._follow_cpu_limit())

    def close_all(self) -> None:
        """Stop every helper (killed after ``CLOSE_GRACE_S``); the pool serves nothing afterwards.

        A request still in flight is killed with its helper. It must not be read as that GPU failing — the device
        would be sent to the CPU on the way out — and a request that arrives afterwards (a worker thread still
        running at ``atexit``) must not start a fresh process during interpreter shutdown. A request waiting for a
        CPU helper is woken and told the pool is shutting down.
        """
        with self._cpu_free:
            self._closed = True
            helpers, self._helpers = list(self._helpers.values()), {}
            self._cpu_free.notify_all()
        for helper in helpers:
            helper.close(CLOSE_GRACE_S)

    def _raise_if_closed(self) -> None:
        with self._guard:
            closed = self._closed
        if closed:
            raise TextDetShuttingDownError("Text detection is shutting down")

    # ---------------------------------------------------------------------------------------------------- GPU helpers

    def _gpu_refused(self, key: str, gpu: str | None) -> str | None:
        """Why this request can't try the device's GPU (its CPU verdict, or a start failure's cool-down); None: try it."""
        with self._guard:
            state = self._gpus.setdefault(key, _GpuState())
            verdict, retry_at = state.verdict, state.retry_at
        if verdict == "cpu" or _monotonic() < retry_at:
            return self._cpu_reason(key)
        if verdict is None:
            if gpu not in _WEBGPU_VENDORS:
                self._use_cpu(key, f"{gpu} GPUs have no WebGPU path in this app")
                return self._cpu_reason(key)
            info = self._vulkan_info()
            if info.device is None or info.is_software:
                self._use_cpu(key, "Vulkan reports no hardware GPU")
                return self._cpu_reason(key)
        return None

    def _cpu_reason(self, key: str) -> str:
        with self._guard:
            state = self._gpus.setdefault(key, _GpuState())
            return state.cpu_reason or "its GPU can't run text detection"

    def _accept_gpu_helper(self, key: str, ready: dict[str, Any], pci_bus_id: str | None) -> bool:
        """Whether a GPU helper that just came up serves this device; logs and keeps the device's verdict.

        Args:
            key: The device key.
            ready: The helper's ready line.
            pci_bus_id: The address the helper was told to pin to. Nothing can confirm from here that Dawn honoured
                it, so it goes in the log: two devices reporting the same address is the symptom of a wrong-card
                landing, and one grep finds it.

        Returns:
            True when it came up on the GPU; False when the GPU can't run text detection or finds other boxes than
            the CPU (the device's text detection stays on the CPU for the process).

        Raises:
            HelperError: It came up on the CPU for a reason that isn't a verdict on the GPU: its WebGPU session failed,
                or a device that has served from the GPU came back without it (a driver reset). That is a failure of
                this attempt, and the GPU is tried again later.
        """
        backend = ready["backend"]
        reason = ready.get("reason") or "no reason given"
        with self._guard:
            state = self._gpus.setdefault(key, _GpuState())
            before = state.verdict
            if backend == "webgpu":
                state.verdict = "webgpu"
        if backend == "webgpu":
            if before is None:
                test = ready.get("selftest") or {}
                logger.info(
                    "Credit text detection on {}: GPU (median {} ms per frame, CPU {} ms; GPU/CPU {} per round), "
                    "pinned to {}",
                    key,
                    test.get("gpu_ms"),
                    test.get("cpu_ms"),
                    test.get("ratio"),
                    pci_bus_id or "no address (unpinned)",  # fmt: skip
                )
            return True
        if ready.get("failed") or before == "webgpu":
            raise HelperStartError(f"it came up on the CPU: {reason}")
        # Only a self-test that found other boxes than the CPU's comes back with its results and the CPU.
        self._use_cpu(key, reason, boxes_differ=ready.get("selftest") is not None)
        return False

    def _use_cpu(self, key: str, reason: str, *, boxes_differ: bool = False) -> None:
        """Keep the device's text detection on the CPU for the process: its GPU can't run it, or answers differently."""
        with self._guard:
            state = self._gpus.setdefault(key, _GpuState())
            already = state.verdict == "cpu"
            state.verdict = "cpu"
            if not already:
                state.cpu_reason = "this GPU finds other text boxes than the CPU" if boxes_differ else reason
        if already:
            return
        if boxes_differ:
            logger.warning(
                "Credit text detection on {} runs on the CPU for the rest of this run of the app: {}. A credits answer "
                "mustn't depend on which device read the file, so a GPU that finds other text boxes isn't used",
                key,
                reason,
            )
        else:
            logger.info("Credit text detection on {}: CPU ({})", key, reason)

    def _gpu_failed(self, key: str, exc: HelperError) -> str:
        """Count a failed attempt on the GPU (a start, a request, a timeout): this request goes to the CPU, and the GPU
        is tried again after ``GPU_RETRY_BASE_S``, doubling with each failure in a row up to ``GPU_RETRY_MAX_S``. The
        first failure of a run of them is a WARNING; the rest are DEBUG, and :meth:`_gpu_worked` says when the GPU
        answers again.

        Returns:
            Why this request is read on the CPU.
        """
        what = "couldn't start" if isinstance(exc, HelperStartError) else "failed"
        reason = f"its GPU helper {what} ({exc})"
        with self._guard:
            state = self._gpus.setdefault(key, _GpuState())
            state.failures += 1
            failures = state.failures
            back_off = min(GPU_RETRY_BASE_S * 2 ** min(failures - 1, 32), GPU_RETRY_MAX_S)
            state.retry_at = _monotonic() + back_off
            state.cpu_reason = reason
        log = logger.warning if failures == 1 else logger.debug
        log(
            "Credit text detection on {}: {}; this request is read on the CPU and the GPU is tried again in {:g} s",
            key,
            reason,
            back_off,
        )
        return reason

    def _gpu_worked(self, key: str) -> None:
        with self._guard:
            state = self._gpus.setdefault(key, _GpuState())
            failed = state.failures
            state.failures, state.retry_at = 0, 0.0
        if failed:
            logger.info(
                "Credit text detection on {}: back on the GPU after {} failed request{}",
                key,
                failed,
                "s" * (failed > 1),
            )

    def _gpu_spec(self, key: str, gpu: str | None, gpu_device_path: str | None) -> HelperSpec:
        pci_bus_id = worker_pci_bus_id(gpu, gpu_device_path)
        env = dict(os.environ)
        if gpu == "NVIDIA":
            # NVIDIA's ICD only loads with these (spec §5.4); they hide other vendors' GPUs, so only NVIDIA helpers
            # get them.
            env.update(self._vulkan_env())
        # Note N1: the EP device object picks the provider, not the GPU. This is what puts the helper on the
        # worker's own card, so two GPU workers don't both land on GPU 0.
        env = pin_env_to_gpu(env, pci_bus_id)
        with self._guard:
            state = self._gpus.get(key)
            known = state is not None and state.verdict is not None
        return HelperSpec(key, "webgpu", pci_bus_id, not known, env)

    # ---------------------------------------------------------------------------------------------------- CPU helpers

    def _on_cpu(
        self, planes: np.ndarray, *, for_gpu_worker: bool, cancel_check: Callable[[], bool] | None = None
    ) -> list[tuple[Box, ...]]:
        key = self._take_cpu_helper(for_gpu_worker, cancel_check)
        try:
            boxes = self._on_helper(key, planes, lambda: self._cpu_spec(key))
        except HelperError as exc:
            self._raise_if_closed()  # close_all killed it: the pool ending, not text detection failing
            raise TextDetUnavailableError(f"Text detection failed: {exc}") from exc
        finally:
            self._return_cpu_helper(key, for_gpu_worker)
        if boxes is None:  # pragma: no cover - a CPU helper either serves or raises
            raise TextDetUnavailableError("Text detection failed: the CPU helper didn't answer")
        return boxes

    def _read_cpu_limit(self) -> int:
        """The saved CPU worker count. Never read while _guard is held: the settings have a lock of their own."""
        return max(0, int(self._cpu_limit()))

    def _follow_cpu_limit(self) -> list[_Helper]:
        """Read the saved CPU worker count and follow it if it changed.

        Returns:
            The processes of idle helpers the lower count leaves no room for, to stop once _guard is released (already
            out of the pool).
        """
        with self._cpu_free:
            self._cpu_limit_reads += 1
            ticket = self._cpu_limit_reads
        limit = self._read_cpu_limit()
        with self._cpu_free:
            # A read that began before one already applied may hold a value from before the last save.
            if self._closed or ticket < self._cpu_limit_applied:
                return []
            self._cpu_limit_applied = ticket
            if limit == self._cpu_limit_seen:
                return []
            self._cpu_limit_seen = limit
            return self._resize_cpu_helpers()

    def _cpu_worker_cap(self) -> int:
        """How many CPU helpers CPU workers' requests may hold at once (_guard held)."""
        return max(1, self._cpu_limit_seen or 0)

    def _take_cpu_helper(self, for_gpu_worker: bool, cancel_check: Callable[[], bool] | None = None) -> str:
        """A CPU helper's key for one request: an idle one, else a new one, when this kind of request has room; else
        the next one freed. A GPU worker's request always has room.

        Raises:
            TextDetShuttingDownError: The pool closed (also while waiting).
            TextDetCancelledError: The job was cancelled while waiting (asked outside the pool's lock).
        """
        while True:
            if cancel_check is not None and cancel_check():
                raise TextDetCancelledError("cancelled")
            # Old helpers are stopped before a key is taken, so nothing can fail between taking one and using it.
            self._stop_helpers(self._follow_cpu_limit())
            with self._cpu_free:
                if self._closed:
                    raise TextDetShuttingDownError("Text detection is shutting down")
                if for_gpu_worker or self._cpu_worker_holds < self._cpu_worker_cap():
                    if self._cpu_idle:
                        # The most recently used: the rest stay idle long enough to leave on their own idle timer.
                        key = self._cpu_idle.pop()
                    else:
                        key = self._new_cpu_helper_key()
                    if for_gpu_worker:
                        self._gpu_worker_holds += 1
                    else:
                        self._cpu_worker_holds += 1
                    return key
                # Woken by a returned helper, a resize or close_all; the timeout reads the saved count again.
                self._cpu_free.wait(CPU_WAIT_POLL_S)

    def _new_cpu_helper_key(self) -> str:
        """The first CPU helper key not in use, now in use (_guard held)."""
        number = 1
        while _cpu_helper_key(number) in self._cpu_helpers:
            number += 1
        key = _cpu_helper_key(number)
        self._cpu_helpers.append(key)
        return key

    def _resize_cpu_helpers(self) -> list[_Helper]:
        """Follow a changed saved count (_guard held): idle helpers beyond what CPU workers may hold now, plus the
        ones GPU workers hold, leave the pool. Busy CPU workers' helpers beyond the count go when they are returned.
        An unchanged count changes nothing, so helpers GPU workers use between two of their requests aren't stopped
        and started again.

        Returns:
            The processes to stop once _guard is released (already out of the pool).
        """
        target = self._cpu_worker_cap() + self._gpu_worker_holds
        stop: list[_Helper] = []
        while len(self._cpu_helpers) > target and self._cpu_idle:
            stop += self._retire_cpu_helper(self._cpu_idle.pop(0))  # the least recently used first
        self._cpu_free.notify_all()  # a higher count lets waiting requests take helpers
        return stop

    def _retire_cpu_helper(self, key: str) -> list[_Helper]:
        """Take a CPU helper out of the pool (_guard held), its process with it: a helper started later under the same
        key is a new one, which stopping this one's process can't touch."""
        self._cpu_helpers.remove(key)
        helper = self._helpers.pop(key, None)
        return [helper] if helper is not None else []

    def _return_cpu_helper(self, key: str, for_gpu_worker: bool) -> None:
        stop: list[_Helper] = []
        with self._cpu_free:
            if for_gpu_worker:
                self._gpu_worker_holds -= 1
                beyond_the_count = False
            else:
                self._cpu_worker_holds -= 1
                # The others still hold as many as the count allows: this one is beyond a count that went down.
                beyond_the_count = self._cpu_worker_holds >= self._cpu_worker_cap()
            if beyond_the_count or self._closed:
                stop = self._retire_cpu_helper(key)
            else:
                self._cpu_idle.append(key)
            self._cpu_free.notify()
        self._stop_helpers(stop)

    def _cpu_spec(self, key: str) -> HelperSpec:
        return HelperSpec(key, "cpu", None, False, pin_env_to_gpu(os.environ, None))

    @staticmethod
    def _stop_helpers(helpers: list[_Helper]) -> None:
        for helper in helpers:
            helper.kill()
            helper.close(0)

    # ------------------------------------------------------------------------------------------------- every helper

    def _on_helper(
        self,
        key: str,
        planes: np.ndarray,
        spec_for: Callable[[], HelperSpec],
        *,
        accept: Callable[[dict[str, Any], HelperSpec], bool] | None = None,
    ) -> list[tuple[Box, ...]] | None:
        for attempt in (1, 2):
            helper = self._helper(key, spec_for, accept)
            if helper is None:
                return None  # a GPU helper that came up on the CPU for good: the request goes to a CPU helper
            try:
                return helper.request(planes)
            except HelperError:
                code = helper.wait_exit(EXIT_CODE_WAIT_S)
                self._drop(key)
                if code != IDLE_EXIT_CODE:
                    raise
                if attempt == 2:
                    # Two idle exits in a row is not the idle timer: something ends the helper on every request.
                    raise HelperError("the helper went idle twice in a row") from None
        return None  # pragma: no cover - the loop always returns or raises

    def _helper(
        self,
        key: str,
        spec_for: Callable[[], HelperSpec],
        accept: Callable[[dict[str, Any], HelperSpec], bool] | None,
    ) -> _Helper | None:
        """The key's running helper, started (again) when there is none, it has exited, or it is about to leave on its
        idle timer.

        Args:
            key: A GPU device key or a CPU helper key; the caller holds it alone (the device's lock, or a CPU helper
                taken for this request).
            spec_for: How to start it.
            accept: For a GPU helper: whether one that just came up serves (``_accept_gpu_helper``).

        Returns:
            The helper, or None when a GPU helper came up on the CPU for good.

        Raises:
            HelperError: It couldn't be started, it came up on the CPU this time only, or a GPU helper exited between
                requests (it crashed).
        """
        with self._guard:
            helper = self._helpers.get(key)
        if helper is not None:
            code = helper.exit_code()
            if code is not None:
                self._drop(key)
                helper = None
                # A CPU helper is started again whatever it exited with, so both of its codes take this same path.
                if not _is_cpu_helper_key(key) and code != IDLE_EXIT_CODE:
                    raise HelperError(f"it exited {code} between requests")
            elif _monotonic() - helper.last_used > IDLE_EXIT_S - IDLE_RESTART_MARGIN_S:
                self._drop(key)  # about to leave on its idle timer (T-R8): start a new one rather than race it
                helper = None
        if helper is not None:
            return helper
        self._raise_if_closed()
        spec = spec_for()
        helper = _start(
            spec,
            command=self._command,
            popen=self._popen,
            start_timeout_s=self._start_timeout_s,
            request_timeout_s=self._request_timeout_s,
        )
        with self._guard:
            closed = self._closed
            if not closed:
                self._helpers[key] = helper
        if closed:
            # close_all ran while this one started and won't see it: nothing else would ever stop it.
            helper.kill()
            helper.close(0)
            self._raise_if_closed()
        if accept is not None:
            try:
                accepted = accept(helper.ready, spec)
            except HelperError:
                self._drop(key)
                raise
            if not accepted:
                self._drop(key)
                return None
        return helper

    def _drop(self, key: str) -> None:
        with self._guard:
            helper = self._helpers.pop(key, None)
        if helper is not None:
            helper.kill()
            helper.close(0)


_pool: TextDetectorPool | None = None
_pool_lock = threading.Lock()


def get_textdet_pool() -> TextDetectorPool:
    """The process's helper pool: its CPU helpers follow the saved CPU worker count, and its helpers stop at exit."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = TextDetectorPool(cpu_limit=_configured_cpu_workers)
            atexit.register(_pool.close_all)
        return _pool


def reconcile_textdet_cpu_helpers() -> None:
    """Bring the process pool's CPU helpers to the saved CPU worker count (a settings save calls this).

    Starts no pool: before any text detection there are no helpers to resize.
    """
    with _pool_lock:
        pool = _pool
    if pool is not None:
        pool.reconcile_cpu_helpers()


def _configured_cpu_workers() -> int:
    """The saved CPU worker count (Settings → Workers), or 1 when it can't be read.

    Only the app's settings, never loaded here: a process that hasn't loaded them (the eval harness) gets 1.
    """
    try:
        from ...web.settings_manager import peek_settings_manager

        settings = peek_settings_manager()
        return 1 if settings is None else max(0, int(settings.cpu_threads))
    except Exception as exc:  # noqa: BLE001 - a helper count must never fail a request
        logger.debug("Couldn't read the CPU worker count for text detection, so one CPU helper: {}", exc)
        return 1


# ----------------------------------------------------------------------------------------------- the helper process


def _send(protocol: BinaryIO, message: dict[str, Any]) -> None:
    protocol.write(json.dumps(message).encode() + b"\n")


def _send_text(protocol: BinaryIO, text: str) -> None:
    protocol.write(text.encode() + b"\n")


def _vulkan_adapters() -> list[tuple[str, int]] | None:
    """Each Vulkan device this process's environment exposes, as ``(name, VkPhysicalDeviceType)``.

    The helper's environment is what pins it to its GPU (``pin_env_to_gpu``: the device-select layer then reports only
    that device), and Dawn takes an adapter from this same list, so this is what WebGPU can run on. Asked through the
    Vulkan loader itself (the image has no Vulkan tools); a separate instance, gone before Dawn starts its own.

    Returns:
        The devices in the loader's order, or None when Vulkan can't be asked (no loader, no instance, or anything
        else going wrong: the check is then skipped and the WebGPU session itself says whether there's an adapter).
    """
    try:
        return _ask_vulkan_loader()
    except Exception as exc:  # noqa: BLE001 - a broken check must never count as a GPU failure
        logger.debug("Couldn't list the Vulkan devices: {}: {}", type(exc).__name__, exc)
        return None


def _ask_vulkan_loader() -> list[tuple[str, int]] | None:
    import ctypes

    class AppInfo(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_int), ("pNext", ctypes.c_void_p), ("pApplicationName", ctypes.c_char_p),
                    ("applicationVersion", ctypes.c_uint32), ("pEngineName", ctypes.c_char_p),
                    ("engineVersion", ctypes.c_uint32), ("apiVersion", ctypes.c_uint32)]  # fmt: skip

    class InstanceInfo(ctypes.Structure):
        _fields_ = [("sType", ctypes.c_int), ("pNext", ctypes.c_void_p), ("flags", ctypes.c_uint32),
                    ("pApplicationInfo", ctypes.POINTER(AppInfo)), ("enabledLayerCount", ctypes.c_uint32),
                    ("ppEnabledLayerNames", ctypes.c_void_p), ("enabledExtensionCount", ctypes.c_uint32),
                    ("ppEnabledExtensionNames", ctypes.c_void_p)]  # fmt: skip

    try:
        vk = ctypes.CDLL("libvulkan.so.1")
        vk.vkCreateInstance.argtypes = [ctypes.POINTER(InstanceInfo), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        vk.vkCreateInstance.restype = ctypes.c_int
        vk.vkEnumeratePhysicalDevices.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        vk.vkEnumeratePhysicalDevices.restype = ctypes.c_int
        vk.vkGetPhysicalDeviceProperties.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        vk.vkGetPhysicalDeviceProperties.restype = None
        vk.vkDestroyInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        vk.vkDestroyInstance.restype = None
        # sType 0 and 1: VK_STRUCTURE_TYPE_APPLICATION_INFO and _INSTANCE_CREATE_INFO; Vulkan 1.1.
        app = AppInfo(0, None, b"textdet", 0, None, 0, (1 << 22) | (1 << 12))
        info = InstanceInfo(1, None, 0, ctypes.pointer(app), 0, None, 0, None)
        instance = ctypes.c_void_p()
        if vk.vkCreateInstance(ctypes.byref(info), None, ctypes.byref(instance)) != 0:
            return None
    except (OSError, AttributeError):
        return None
    try:
        count = ctypes.c_uint32(0)
        if vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None) != 0:
            return None
        handles = (ctypes.c_void_p * count.value)()
        if vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), handles) not in (0, 5):  # 5: VK_INCOMPLETE
            return None
        adapters = []
        for handle in handles[: count.value]:
            # VkPhysicalDeviceProperties: apiVersion, driverVersion, vendorID, deviceID (uint32 each), deviceType
            # (a 4-byte enum) at byte 16, then deviceName[256]; the limits after it make the whole under 1 KB.
            properties = ctypes.create_string_buffer(4096)
            vk.vkGetPhysicalDeviceProperties(handle, properties)
            raw = properties.raw
            name = raw[20:276].split(b"\0", 1)[0].decode("utf-8", "replace")
            adapters.append((name, int.from_bytes(raw[16:20], sys.byteorder)))
        return adapters
    finally:
        vk.vkDestroyInstance(instance, None)


def _software_adapter(adapters: list[tuple[str, int]] | None) -> str | None:
    """The software renderer WebGPU would run on, or None when there is a GPU to run on (or nothing is known).

    Dawn prefers any GPU adapter to a CPU one, so it lands on a software renderer only when that is all the helper's
    environment leaves it: when a GPU's own Vulkan driver doesn't load in the container, say.

    Args:
        adapters: From :func:`_vulkan_adapters`.

    Returns:
        The software renderer's name, e.g. ``llvmpipe (LLVM 19.1.1, 256 bits)``.
    """
    if not adapters:
        return None
    for name, kind in adapters:
        if kind != VK_CPU and not any(renderer in name.lower() for renderer in _SOFTWARE_RENDERERS):
            return None
    return adapters[0][0]


def _start_detector(textdet: Any, args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """The detector this helper serves with, and the ready line saying which backend won and why.

    A GPU helper serves from the GPU when its WebGPU session is on a hardware adapter and, on a device's first start,
    the self-test finds the CPU's boxes. Otherwise the ready line says ``cpu`` with the reason, and ``failed`` when that
    reason is a failure this time (a session or self-test that raised) rather than a verdict on the GPU.

    Raises:
        ModelError: The model file is missing or isn't the pinned one.
    """
    built: list[Any] = []

    def cpu() -> Any:
        # Built only when it is going to be used: on the --no-selftest path a CPU session would otherwise keep its
        # ONNX Runtime arena and intra-op threads for the helper's whole life without ever running a frame.
        if not built:
            built.append(textdet.TextDetector(textdet.cpu_session(args.model, args.threads), backend="cpu"))
        return built[0]

    if args.backend == "cpu":
        return cpu(), {"backend": "cpu", "selftest": None, "reason": ""}
    try:
        found = textdet.webgpu_devices()
        index = choose_ep_device([dict(d.device.metadata) for d in found], args.pci_bus_id)
        if index is None:
            return cpu(), {
                "backend": "cpu",
                "selftest": None,
                "reason": f"no WebGPU device is this GPU ({len(found)} found)",
            }
        software = _software_adapter(_vulkan_adapters())
        if software is not None:
            # Not a GPU: it finds the CPU's boxes, so the self-test alone would keep it.
            return cpu(), {
                "backend": "cpu",
                "selftest": None,
                "reason": f"WebGPU would run on {software}, a software renderer, not on this GPU",
            }
        gpu = textdet.TextDetector(textdet.webgpu_session(args.model, found[index], args.threads), backend="webgpu")
        if not args.selftest:
            return gpu, {"backend": "webgpu", "selftest": None, "reason": ""}
        result = self_test(
            gpu,
            cpu(),
            textdet.synthetic_frames(SELFTEST_FRAMES),
            large_frames=textdet.synthetic_frames(SELFTEST_LARGE_FRAMES, scale=SELFTEST_LARGE_SCALE),
        )
    except textdet.ModelError:
        raise
    except textdet.WebGpuSessionError as exc:
        # Expected on a host whose Vulkan environment leaves Dawn no adapter (note N2), or a GPU Dawn rejects: the GPU
        # can't run text detection here. ONNX Runtime has already printed its own "falling back to
        # CPUExecutionProvider" block to stderr above this.
        return cpu(), {"backend": "cpu", "selftest": None, "reason": f"this GPU has no usable WebGPU adapter: {exc}"}
    except Exception as exc:  # noqa: BLE001 - any EP or driver failure means the CPU, this time
        return cpu(), {
            "backend": "cpu",
            "selftest": None,
            "reason": f"the WebGPU session failed: {type(exc).__name__}: {exc}",
            "failed": True,
        }
    if result.use_gpu:
        return gpu, {"backend": "webgpu", "selftest": asdict(result), "reason": ""}
    return cpu(), {"backend": "cpu", "selftest": asdict(result), "reason": result.cpu_reason()}


def _serve(detector: Any, protocol: BinaryIO, idle_exit_s: float) -> int:
    stdin = sys.stdin.buffer
    # poll, not select: select() raises ValueError for any descriptor at or above FD_SETSIZE (1024).
    requests = select.poll()
    requests.register(stdin, select.POLLIN)
    while True:
        # The parent waits for each answer before it sends again, so nothing sits in stdin's buffer between requests.
        # A closed stdin wakes the poll too (POLLHUP), and readline() below then sees the end of the stream.
        if not requests.poll(idle_exit_s * 1000):
            return IDLE_EXIT_CODE
        header = stdin.readline()
        if not header:
            return 0
        request = json.loads(header)
        shape = (int(request["frames"]), int(request["height"]), int(request["width"]))
        data = stdin.read(shape[0] * shape[1] * shape[2])
        if len(data) != shape[0] * shape[1] * shape[2]:
            return 0
        try:
            _send(
                protocol,
                {"id": request["id"], "boxes": detector.detect(np.frombuffer(data, np.uint8).reshape(shape))},
            )
        except Exception as exc:  # noqa: BLE001 - the parent decides what a failed request means
            _send(protocol, {"id": request["id"], "error": f"{type(exc).__name__}: {exc}"})


def main(argv: list[str] | None = None) -> int:
    """The helper process: ``--check`` once, or serve each frame's text boxes until stdin closes or it has been idle.

    Args:
        argv: Command-line arguments (``sys.argv[1:]`` when None).

    Returns:
        0, IDLE_EXIT_CODE, or CHECK_ABSENT_CODE when the packages or the model aren't usable.
    """
    parser = argparse.ArgumentParser(prog=f"python -m {MODULE}")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--backend", choices=("webgpu", "cpu"), default="cpu")
    parser.add_argument("--model", default=model_path())
    parser.add_argument("--threads", type=int, default=THREADS)
    parser.add_argument("--pci-bus-id", default=None)
    parser.add_argument("--no-selftest", dest="selftest", action="store_false")
    parser.add_argument("--idle-exit-s", type=float, default=IDLE_EXIT_S)
    args = parser.parse_args(argv)
    protocol = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)  # a native library printing to stdout must not corrupt the protocol
    try:
        from . import textdet
    except ImportError as exc:
        _send_text(protocol, f"{NOT_INSTALLED} ({exc})")
        return CHECK_ABSENT_CODE
    try:
        if args.check:
            textdet.TextDetector(textdet.cpu_session(args.model, args.threads), backend="cpu").count(
                textdet.synthetic_frames(1)
            )
            return 0
        detector, ready = _start_detector(textdet, args)
    except textdet.ModelError as exc:
        _send_text(protocol, str(exc))
        return CHECK_ABSENT_CODE
    _send(protocol, {"ready": True, **ready})
    return _serve(detector, protocol, args.idle_exit_s)


if __name__ == "__main__":
    sys.exit(main())
