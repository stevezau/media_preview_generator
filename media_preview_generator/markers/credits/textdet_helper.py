"""Credit text detection in helper processes: one per GPU device and one for the CPU (spec §6.4 item 7).

Why processes: the Vulkan loader reads its environment once per process and NVIDIA needs overrides that hide other
GPUs (the plex host has NVIDIA + Intel); a driver crash or hang can't take the web app down; the WebGPU plugin can hang
at shutdown (ORT PR #29591).

Protocol, one request at a time per helper:
- helper → parent, once: ``{"ready": true, "backend": "webgpu"|"cpu", "selftest": {...}|null, "reason": str}``
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
THREADS = 2
SELFTEST_FRAMES = 20
# The detector reads a tail with no answer at 320x180 again at this many times the size (``detector.RETRY_SCALE``;
# this module can't import the detector), so the self-test also compares the boxes on these many frames of that size.
# Untimed: the speed verdict stays the 320x180 one, the size nearly every request is.
SELFTEST_LARGE_SCALE = 2
SELFTEST_LARGE_FRAMES = 8
# Timed GPU/CPU pairs, each timed back to back; the median of the pairs' own GPU/CPU ratios decides (see self_test).
# Seven, not five: on storage's P5000 (10 cold starts x 12 rounds, 2026-09-19) the median ratio of 5 rounds spread
# 0.047 around its typical 0.67, of 7 rounds 0.038 -- the TITAN RTX's 4.94 vs 6.18 ms is only 0.1 inside the margin.
# About 1.2 s more, once per device.
SELFTEST_ROUNDS = 7
# How much less time than the CPU the GPU must take, as that median ratio, to be worth using. A single 20-frame pair
# once handed the whole process to the GPU on a 0.06% win (17.99 vs 18.00 ms). A real GPU has room to spare: this
# self-test on storage's P5000 measures 10.7-11.6 vs 16.2-18.0 ms per frame (2026-09-19), about 35% faster (the
# planning bench's own script measured 13.3 vs 18.7 ms on the same card); the TITAN RTX on plex, 4.94 vs 6.18 ms.
GPU_SPEEDUP_MARGIN = 0.10
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


class TextDetUnavailableError(Exception):
    """Text detection can't answer this time (the CPU helper failed)."""


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
    """Whether credit text detection can run here (checked once per process; UNKNOWN is asked again after 10 min)."""
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
        gpu_ms: The GPU's median milliseconds per frame.
        cpu_ms: The CPU's median milliseconds per frame.
        ratio: The median of each round's own GPU/CPU time ratio: what decides.
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
        """The GPU is used only when it finds exactly the CPU's boxes, in the same places and at both sizes, at least
        ``GPU_SPEEDUP_MARGIN`` faster."""
        return self.same_boxes and self.same_boxes_large and self.ratio <= 1.0 - GPU_SPEEDUP_MARGIN

    def cpu_reason(self) -> str:
        """Why this result keeps the CPU (for the helper's ready line and the log)."""
        if not self.same_boxes:
            return "the GPU was finding different boxes than the CPU"
        if not self.same_boxes_large:
            return "the GPU was finding different boxes than the CPU at 640x360"
        return (
            f"the GPU wasn't at least {GPU_SPEEDUP_MARGIN:.0%} faster than the CPU "
            f"(median {self.gpu_ms} vs {self.cpu_ms} ms per frame; GPU/CPU {self.ratio} per round)"
        )


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
    """Time both detectors on the same frames after a warm-up and compare the boxes they find.

    **The boxes, not their count.** ``count`` is ``len(detect(...))``, so comparing counts costs the same and says
    less: a backend that finds the same *number* of boxes in different *places* passes a count test and then answers
    differently from the CPU everywhere rule J reads a position (``rule_j.overlay_boxes``, ``rule_j.same_roll``,
    ``rule_j.reach_back``). This self-test is the only runtime check there is.

    The pair is run ``rounds`` times, the GPU and then the CPU back to back, and the verdict is the median of the rounds'
    own GPU/CPU ratios. Load that lasts across a round (another job's decode, a transcode) slows both halves alike and
    leaves its ratio alone; a spike that hits one half only is one round of several, outvoted by the median. Comparing
    each side's best round instead favours the side whose times spread more, usually the GPU. The verdict is kept for
    the process.

    Args:
        gpu: A detector with ``detect(frames) -> list[tuple[Box, ...]]`` on the GPU.
        cpu: The same on the CPU.
        frames: (n, H, W) uint8 luma.
        clock: Seconds (tests pass a fake); ``time.perf_counter`` by default.
        warmup: Frames each detector runs before timing (session start-up and shader compilation: the first WebGPU
            call measured 61-101 ms per frame against 11-14 ms after it).
        rounds: How many GPU/CPU pairs to time.
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
        key: The device key (``device_key``) or ``cpu``.
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


def device_key(gpu: str | None, gpu_device_path: str | None) -> str:
    """The helper a worker's requests go to: its device path (or GPU type), ``cpu`` for a CPU worker.

    A GPU whose device path or type is itself spelt ``cpu`` is prefixed so it can never share the CPU helper's key.
    """
    if gpu is None:
        return CPU_KEY
    key = gpu_device_path or gpu
    return f"gpu:{key}" if key == CPU_KEY else key


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
        raise HelperError(f"couldn't start: {exc}") from exc
    helper = _Helper(proc, stderr_file, request_timeout_s)
    try:
        ready = helper.read_message(start_timeout_s)
        if ready.get("ready") is not True or ready.get("backend") not in ("webgpu", "cpu"):
            raise HelperError(f"unexpected start line: {str(ready)[:200]}")
    except HelperError:
        helper.kill()
        helper.close(0)
        raise
    helper.ready = ready
    return helper


def _vulkan_device_info() -> Any:
    from ...gpu.vulkan_probe import get_vulkan_device_info

    return get_vulkan_device_info()


def _vulkan_env_overrides() -> dict[str, str]:
    from ...gpu.vulkan_probe import get_vulkan_env_overrides

    return get_vulkan_env_overrides()


class TextDetectorPool:
    """Text boxes for luma planes on a worker's device: its GPU helper when that is proven faster and finds the same
    boxes, else the CPU helper."""

    def __init__(
        self,
        *,
        command: Callable[[HelperSpec], list[str]] = helper_command,
        popen: Any = subprocess.Popen,
        start_timeout_s: float = START_TIMEOUT_S,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
        vulkan_info: Callable[[], Any] | None = None,
        vulkan_env: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        """Create an empty pool (helpers start on first use).

        Args:
            command: Builds a helper's argv (tests start a fake).
            popen: Starts a process.
            start_timeout_s: Longest wait for a helper's ready line (a WebGPU start with the self-test included).
            request_timeout_s: Longest wait for one request's answer.
            vulkan_info: The app's Vulkan probe result.
            vulkan_env: The app's Vulkan env overrides (applied to NVIDIA helpers only).
        """
        self._command, self._popen = command, popen
        self._start_timeout_s, self._request_timeout_s = start_timeout_s, request_timeout_s
        self._vulkan_info = vulkan_info or _vulkan_device_info
        self._vulkan_env = vulkan_env or _vulkan_env_overrides
        self._helpers: dict[str, _Helper] = {}
        self._backends: dict[str, str] = {}  # device key → "webgpu" | "cpu", for the process lifetime
        self._locks = KeyedLocks()
        self._guard = threading.Lock()
        self._closed = False

    def detect_boxes(
        self, planes: np.ndarray, *, gpu: str | None, gpu_device_path: str | None
    ) -> list[tuple[Box, ...]]:
        """Text boxes per luma plane on the worker's device.

        Args:
            planes: (n, H, W) uint8.
            gpu: The worker's GPU type, None on a CPU worker.
            gpu_device_path: The worker's device.

        Returns:
            One plane's boxes per plane, each ``(left, top, right, bottom)`` in the plane's own pixels.

        Raises:
            TextDetUnavailableError: The CPU helper failed (the next call starts a new one).
            TextDetShuttingDownError: The pool is closed.
        """
        key = device_key(gpu, gpu_device_path)
        self._raise_if_closed()
        if key != CPU_KEY:
            # The verdict is read inside the device's lock: a second worker reading it while the first is still in
            # the self-test would act on a stale "GPU allowed" and start a second, un-self-tested GPU helper.
            with self._locks.hold(key):
                if self._gpu_allowed(key, gpu):
                    try:
                        boxes = self._on_helper(key, planes, lambda: self._gpu_spec(key, gpu, gpu_device_path))
                    except HelperError as exc:
                        self._raise_if_closed()
                        self._use_cpu(key, f"its GPU helper failed: {exc}", warning=True)
                    else:
                        if boxes is not None:
                            return boxes
        with self._locks.hold(CPU_KEY):
            self._raise_if_closed()
            try:
                boxes = self._on_helper(CPU_KEY, planes, self._cpu_spec)
            except HelperError as exc:
                self._raise_if_closed()  # close_all killed it: the pool ending, not text detection failing
                raise TextDetUnavailableError(f"Text detection failed: {exc}") from exc
        if boxes is None:  # pragma: no cover - the CPU helper either serves or raises
            raise TextDetUnavailableError("Text detection failed: the CPU helper didn't answer")
        return boxes

    def backend_of(self, gpu: str | None, gpu_device_path: str | None) -> str | None:
        """What a worker's text detection runs on: ``webgpu``, ``cpu``, or None before its first request.

        The app itself only logs the verdict (``_record``); this is how the lab scripts and the tests read it.
        """
        key = device_key(gpu, gpu_device_path)
        if key == CPU_KEY:
            return "cpu"
        with self._guard:
            return self._backends.get(key)

    def close_all(self) -> None:
        """Stop every helper (killed after ``CLOSE_GRACE_S``); the pool serves nothing afterwards.

        A request still in flight is killed with its helper. It must not be read as that GPU failing — the device
        would be demoted to the CPU on the way out — and a request that arrives afterwards (a worker thread still
        running at ``atexit``) must not start a fresh process during interpreter shutdown.
        """
        with self._guard:
            self._closed = True
            helpers, self._helpers = list(self._helpers.values()), {}
        for helper in helpers:
            helper.close(CLOSE_GRACE_S)

    def _raise_if_closed(self) -> None:
        with self._guard:
            closed = self._closed
        if closed:
            raise TextDetShuttingDownError("Text detection is shutting down")

    def _gpu_allowed(self, key: str, gpu: str | None) -> bool:
        with self._guard:
            known = self._backends.get(key)
        if known is not None:
            return known == "webgpu"
        if gpu not in _WEBGPU_VENDORS:
            self._use_cpu(key, f"{gpu} GPUs have no WebGPU path here")
            return False
        info = self._vulkan_info()
        if info.device is None or info.is_software:
            self._use_cpu(key, "Vulkan reports no hardware GPU")
            return False
        return True

    def _on_helper(
        self, key: str, planes: np.ndarray, spec_for: Callable[[], HelperSpec]
    ) -> list[tuple[Box, ...]] | None:
        for attempt in (1, 2):
            helper = self._helper(key, spec_for)
            if helper is None:
                return None  # no GPU helper: its self-test chose the CPU, or the device already moved to the CPU
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

    def _helper(self, key: str, spec_for: Callable[[], HelperSpec]) -> _Helper | None:
        with self._guard:
            helper = self._helpers.get(key)
        if helper is not None:
            code = helper.exit_code()
            if code is not None:
                self._drop(key)
                helper = None
                # A CPU helper is started again whatever it exited with, so both of its codes take this same path.
                if key != CPU_KEY and code != IDLE_EXIT_CODE:
                    # Crashed between requests: the device moves to the CPU for the process lifetime (spec §6.4 item 7).
                    self._use_cpu(key, f"its GPU helper exited {code} between requests", warning=True)
                    return None
            elif _monotonic() - helper.last_used > IDLE_EXIT_S - IDLE_RESTART_MARGIN_S:
                self._drop(key)  # about to leave on its idle timer (T-R8): start a new one rather than race it
                helper = None
        if helper is None:
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
            if key != CPU_KEY:
                self._record(key, helper.ready, spec.pci_bus_id)
                if helper.ready["backend"] != "webgpu":
                    self._drop(key)
                    return None
        return helper

    def _record(self, key: str, ready: dict[str, Any], pci_bus_id: str | None) -> None:
        """Log and remember which backend a device's helper came up on.

        Args:
            key: The device key.
            ready: The helper's ready line.
            pci_bus_id: The address the helper was told to pin to. Nothing can confirm from here that Dawn honoured
                it, so it goes in the log: two devices reporting the same address is the symptom of a wrong-card
                landing, and one grep finds it.
        """
        backend = ready["backend"]
        with self._guard:
            before = self._backends.get(key)
            self._backends[key] = backend
        if before == backend:
            return
        if before is not None:
            # A helper that restarted after an idle exit and came up on the CPU (the EP is gone, the driver was
            # reset). It halves the device's throughput for the rest of the run, so it can't be silent. The other
            # direction can't happen: once a device is on the CPU, _gpu_allowed never starts a GPU helper again.
            logger.warning(
                "Credit text detection on {} restarted on the CPU: {}",
                key,
                ready.get("reason") or "the helper came back on the other backend",
            )
            return
        test = ready.get("selftest") or {}
        if backend == "webgpu":
            logger.info(
                "Credit text detection on {}: GPU (median {} ms per frame, CPU {} ms; GPU/CPU {} per round), "
                "pinned to {}",
                key,
                test.get("gpu_ms"),
                test.get("cpu_ms"),
                test.get("ratio"),
                pci_bus_id or "no address (unpinned)",  # fmt: skip
            )
        else:
            logger.info(
                "Credit text detection on {}: CPU ({})", key, ready.get("reason") or "the GPU self-test chose the CPU"
            )

    def _use_cpu(self, key: str, reason: str, *, warning: bool = False) -> None:
        with self._guard:
            already = self._backends.get(key) == "cpu"
            self._backends[key] = "cpu"
        if already:
            return
        if warning:
            logger.warning(
                "Credit text detection on {} moves to the CPU for the rest of this run of the app: {}", key, reason
            )
        else:
            logger.info("Credit text detection on {}: CPU ({})", key, reason)

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
            known = key in self._backends
        return HelperSpec(key, "webgpu", pci_bus_id, not known, env)

    def _cpu_spec(self) -> HelperSpec:
        return HelperSpec(CPU_KEY, "cpu", None, False, pin_env_to_gpu(os.environ, None))

    def _drop(self, key: str) -> None:
        with self._guard:
            helper = self._helpers.pop(key, None)
        if helper is not None:
            helper.kill()
            helper.close(0)


_pool: TextDetectorPool | None = None
_pool_lock = threading.Lock()


def get_textdet_pool() -> TextDetectorPool:
    """The process's helper pool (its helpers are stopped at exit)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = TextDetectorPool()
            atexit.register(_pool.close_all)
        return _pool


# ----------------------------------------------------------------------------------------------- the helper process


def _send(protocol: BinaryIO, message: dict[str, Any]) -> None:
    protocol.write(json.dumps(message).encode() + b"\n")


def _send_text(protocol: BinaryIO, text: str) -> None:
    protocol.write(text.encode() + b"\n")


def _start_detector(textdet: Any, args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """The detector this helper serves with, and the ready line saying which backend won and why.

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
        # Expected on a host whose Vulkan environment leaves Dawn no adapter (note N2); ONNX Runtime has already
        # printed its own "falling back to CPUExecutionProvider" block to stderr above this.
        return cpu(), {"backend": "cpu", "selftest": None, "reason": f"this GPU has no usable WebGPU adapter: {exc}"}
    except Exception as exc:  # noqa: BLE001 - any EP or driver failure means the CPU
        return cpu(), {
            "backend": "cpu",
            "selftest": None,
            "reason": f"the WebGPU session failed: {type(exc).__name__}: {exc}",
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
