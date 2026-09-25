"""Text detection helpers: GPU/CPU choice, self-test, crashes, hangs, idle exits, serialisation, availability."""

from __future__ import annotations

import fcntl
import io
import json
import os
import resource
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from media_preview_generator.markers.credits import textdet_helper as th
from media_preview_generator.markers.credits.textdet_helper import TextDetState

FAKE = Path(__file__).with_name("fake_textdet_helper.py")
HARDWARE = SimpleNamespace(device="Quadro P5000 (NVIDIA)", is_software=False)
SOFTWARE = SimpleNamespace(device="llvmpipe (LLVM 19.1.1, 256 bits)", is_software=True)
NO_VULKAN = SimpleNamespace(device=None, is_software=False)
PLANES = np.stack([np.zeros((180, 320), np.uint8), np.full((180, 320), 255, np.uint8)])
# What the fake helper answers for PLANES: no box on the dark plane, its nine on the bright one, positions included.
ANSWER = [(), tuple((n, 2 * n, n + 10, 2 * n + 12) for n in range(9))]
NVIDIA_ICD = "/etc/vulkan/icd.d/nvidia_icd.json"
# One address per worker device, so a test can tell two helpers' GPU pins apart.
WORKER_PCI = {"cuda:0": "0000:02:00.0", "cuda:1": "0000:65:00.0", "/dev/dri/renderD128": "0000:00:02.0"}
PIN_ENV = ("DRI_PRIME", "MESA_VK_DEVICE_SELECT", "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE", "NODEVICE_SELECT")


class Env:
    """A pool whose helpers are the fake, with a mode per backend that tests can change between calls."""

    def __init__(self, monkeypatch, *, modes=None, vulkan=HARDWARE, idle_exit_s=600.0, cpu_workers=1, **timeouts):
        self.modes = {"cpu": "ok", "webgpu": "ok", **(modes or {})}
        self.cpu_workers = cpu_workers  # the saved CPU worker count; a test may change it between calls
        self.specs: list[th.HelperSpec] = []
        self.procs: list[subprocess.Popen] = []
        self.started: list[dict] = []  # the kwargs each helper's popen really got
        self.proc_backends: dict[subprocess.Popen, str] = {}
        monkeypatch.setattr(
            th,
            "worker_pci_bus_id",
            lambda gpu, path: WORKER_PCI.get(path) if gpu in ("NVIDIA", "INTEL", "AMD") else None,
        )
        for name in ("VK_DRIVER_FILES", *PIN_ENV):
            monkeypatch.delenv(name, raising=False)

        def command(spec):
            self.specs.append(spec)
            mode = self.modes[spec.backend]
            if isinstance(mode, list):  # one mode per start, the last one repeating
                mode = mode.pop(0) if len(mode) > 1 else mode[0]
            cmd = [sys.executable, str(FAKE), "--backend", spec.backend, "--mode", mode,
                   "--idle-exit-s", str(idle_exit_s)]  # fmt: skip
            return cmd + ([] if spec.selftest else ["--no-selftest"])

        def popen(argv, **kwargs):
            self.started.append(kwargs)
            proc = subprocess.Popen(argv, **kwargs)
            self.procs.append(proc)
            self.proc_backends[proc] = argv[argv.index("--backend") + 1]
            return proc

        self.pool = th.TextDetectorPool(
            command=command,
            popen=popen,
            vulkan_info=lambda: vulkan,
            vulkan_env=lambda: {"VK_DRIVER_FILES": NVIDIA_ICD},
            start_timeout_s=timeouts.get("start_timeout_s", 20.0),
            request_timeout_s=timeouts.get("request_timeout_s", 20.0),
            cpu_limit=lambda: self.cpu_workers,
        )

    def backends(self):
        return [(s.key, s.backend, s.selftest) for s in self.specs]

    def gpu_starts(self):
        return [s for s in self.specs if s.backend == "webgpu"]

    def cpu_starts(self):
        return [s for s in self.specs if s.backend == "cpu"]

    def cpu_procs(self):
        return [proc for proc in self.procs if self.proc_backends[proc] == "cpu"]


@pytest.fixture
def envs(monkeypatch):
    made: list[Env] = []

    def make(**kwargs):
        made.append(Env(monkeypatch, **kwargs))
        return made[-1]

    yield make
    for env in made:
        env.pool.close_all()


class Clock:
    """The pool's monotonic clock, moved on by a test (a GPU's back-off) instead of waiting it out."""

    def __init__(self, monkeypatch):
        self.offset = 0.0
        monkeypatch.setattr(th, "_monotonic", lambda: time.monotonic() + self.offset)

    def advance(self, seconds: float) -> None:
        self.offset += seconds


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def warnings_in(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]


def infos_in(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "INFO"]


class InFlight:
    """Counts helper requests in flight at once (every helper's ``request``), and signals once ``wanted`` are."""

    def __init__(self, monkeypatch, wanted: int = 0):
        self.now = self.peak = 0
        self.wanted = wanted
        self.reached = threading.Event()
        self._lock = threading.Lock()
        real = th._Helper.request

        def request(helper, planes):
            with self._lock:
                self.now += 1
                self.peak = max(self.peak, self.now)
                if self.wanted and self.now >= self.wanted:
                    self.reached.set()
            try:
                return real(helper, planes)
            finally:
                with self._lock:
                    self.now -= 1

        monkeypatch.setattr(th._Helper, "request", request)


class Concurrent:
    """``call()`` ``each`` times on each of ``threads`` threads started together; their answers and errors."""

    def __init__(self, call, *, threads: int, each: int = 1):
        self.results: list = []
        self.errors: list[BaseException] = []
        start = threading.Barrier(threads)

        def work():
            start.wait(20)
            for _ in range(each):
                try:
                    self.results.append(call())
                except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                    self.errors.append(exc)

        self.threads = [threading.Thread(target=work) for _ in range(threads)]
        for thread in self.threads:
            thread.start()

    def join(self) -> tuple[list, list[BaseException]]:
        for thread in self.threads:
            thread.join(60)
        assert not any(thread.is_alive() for thread in self.threads)
        return self.results, self.errors


def run_concurrently(call, *, threads: int, each: int = 1) -> tuple[list, list[BaseException]]:
    return Concurrent(call, threads=threads, each=each).join()


@pytest.fixture
def release(monkeypatch, tmp_path):
    """Lets the fake's ``held`` requests answer: until it is called, each one waits."""
    path = tmp_path / "release"
    monkeypatch.setenv("FAKE_RELEASE_FILE", str(path))
    return path.touch


class TestRouting:
    def test_a_cpu_worker_uses_the_cpu_helper(self, envs):
        env = envs()
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]
        assert env.specs[0].pci_bus_id is None
        assert "VK_DRIVER_FILES" not in env.started[0]["env"]
        assert "DRI_PRIME" not in env.started[0]["env"]

    def test_an_nvidia_worker_starts_one_webgpu_helper_with_the_vulkan_env_and_keeps_it(self, envs, loguru_caplog):
        env = envs()
        for _ in range(3):
            assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True)]
        assert env.specs[0].pci_bus_id == "0000:02:00.0"
        started = env.started[0]["env"]
        assert started["VK_DRIVER_FILES"] == NVIDIA_ICD
        assert started["DRI_PRIME"] == "pci-0000_02_00_0"
        assert started["MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE"] == "1"
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert (
            "Credit text detection on cuda:0: GPU (median 9.0 ms per frame, CPU 18.0 ms; GPU/CPU 0.5 per round), "
            "pinned to 0000:02:00.0" in loguru_caplog.text
        )

    @pytest.mark.parametrize("gpu", ["INTEL", "AMD"])
    def test_a_vaapi_worker_gets_no_nvidia_overrides(self, envs, gpu):
        env = envs()
        assert env.pool.detect_boxes(PLANES, gpu=gpu, gpu_device_path="/dev/dri/renderD128") == ANSWER
        assert env.backends() == [("/dev/dri/renderD128", "webgpu", True)]
        started = env.started[0]["env"]
        assert "VK_DRIVER_FILES" not in started
        assert started["DRI_PRIME"] == "pci-0000_00_02_0"
        assert started["MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE"] == "1"

    @pytest.mark.parametrize(
        ("gpu", "path"), [("APPLE", "videotoolbox"), ("WINDOWS_GPU", "d3d11va"), ("ARM", "/dev/dri/renderD128")]
    )
    def test_gpus_without_a_webgpu_path_use_the_cpu(self, envs, gpu, path):
        env = envs()
        assert env.pool.detect_boxes(PLANES, gpu=gpu, gpu_device_path=path) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]
        assert env.pool.backend_of(gpu, path) == "cpu"

    @pytest.mark.parametrize(("vulkan", "why"), [(SOFTWARE, "software"), (NO_VULKAN, "no device at all")])
    def test_software_vulkan_never_starts_a_gpu_helper(self, envs, loguru_caplog, vulkan, why):
        env = envs(vulkan=vulkan)
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cpu", "cpu", False)], why
        assert "Credit text detection on cuda:0: CPU (Vulkan reports no hardware GPU)" in loguru_caplog.text

    def test_the_verdict_line_names_the_pin_even_when_there_is_no_address(self, envs, monkeypatch, loguru_caplog):
        env = envs()
        monkeypatch.setattr(th, "worker_pci_bus_id", lambda gpu, path: None)
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert "pinned to no address (unpinned)" in loguru_caplog.text

    def test_backend_of_answers_before_any_request(self, envs):
        env = envs()
        assert env.pool.backend_of(None, None) == "cpu"  # a CPU worker never has anything else
        assert env.pool.backend_of("NVIDIA", "cuda:0") is None  # not started yet

    def test_two_gpu_workers_get_a_helper_each(self, envs):
        env = envs()
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:1") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:1", "webgpu", True)]
        assert env.pool.backend_of("NVIDIA", "cuda:1") == "webgpu"
        # Controller note N1: only the environment puts a helper on its own card, so the two must differ.
        assert [k["env"]["DRI_PRIME"] for k in env.started] == ["pci-0000_02_00_0", "pci-0000_65_00_0"]


class TestGpuVerdicts:
    """What keeps a GPU worker's text detection on the CPU for good: only a GPU that can't run it, or wrong boxes."""

    def test_a_gpu_that_finds_other_boxes_than_the_cpu_moves_to_the_cpu_for_good_with_a_warning(
        self, envs, clock, loguru_caplog
    ):
        env = envs(modes={"webgpu": "selftest-cpu"})
        for _ in range(2):
            assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        clock.advance(th.IDLE_EXIT_S / 2)  # past every short back-off: a verdict has none to wait out
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.procs[0].wait(timeout=10) is not None  # the GPU helper was closed
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
        warnings = warnings_in(loguru_caplog)
        assert len(warnings) == 1
        assert "cuda:0" in warnings[0] and "the GPU was finding different boxes than the CPU" in warnings[0]

    def test_a_software_adapter_moves_the_device_to_the_cpu_for_good_without_a_warning(
        self, envs, clock, loguru_caplog
    ):
        # llvmpipe isn't a GPU: the CPU is right there, and nothing is wrong with the install's GPU worker.
        env = envs(modes={"webgpu": "software-adapter"})
        assert env.pool.detect_boxes(PLANES, gpu="INTEL", gpu_device_path="/dev/dri/renderD128") == ANSWER
        clock.advance(th.IDLE_EXIT_S / 2)
        assert env.pool.detect_boxes(PLANES, gpu="INTEL", gpu_device_path="/dev/dri/renderD128") == ANSWER
        assert env.backends() == [("/dev/dri/renderD128", "webgpu", True), ("cpu", "cpu", False)]
        assert env.pool.backend_of("INTEL", "/dev/dri/renderD128") == "cpu"
        assert warnings_in(loguru_caplog) == []
        assert (
            "Credit text detection on /dev/dri/renderD128: CPU (WebGPU would run on llvmpipe (LLVM 19.1.1, 256 bits), "
            "a software renderer, not on this GPU)" in infos_in(loguru_caplog)
        )


class TestGpuFailures:
    """A GPU helper failing costs that request, not the device: the GPU is tried again after a short back-off that
    doubles with failures in a row (capped, never given up on), as previews keep trying the GPU on later files."""

    def _detect(self, env, **kwargs):
        return env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0", **kwargs)

    @pytest.mark.parametrize(
        ("mode", "timeouts", "self_tested"),
        [
            ("crash-on-request", {}, True),
            ("hang-on-request", {"request_timeout_s": 1.0}, True),  # a request that times out
            ("error-reply", {}, True),
            ("hang-start", {"start_timeout_s": 4.0}, False),
            ("bad-ready", {}, False),
            ("session-failed", {}, False),
        ],
    )
    def test_a_failing_gpu_helper_hands_that_request_to_the_cpu_and_the_gpu_is_tried_after_the_back_off(
        self, envs, clock, loguru_caplog, mode, timeouts, self_tested
    ):
        env = envs(modes={"webgpu": [mode, "ok"]}, **timeouts)
        assert self._detect(env) == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.procs[0].wait(timeout=10) is not None  # killed, not left running
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
        # Inside the first back-off the next request stays on the CPU: one file's back-to-back requests don't each pay
        # a failing helper's start, hang or crash.
        clock.advance(th.GPU_RETRY_BASE_S - 0.5)
        assert self._detect(env) == ANSWER
        assert len(env.gpu_starts()) == 1
        clock.advance(1)
        assert self._detect(env) == ANSWER
        # A helper whose self-test had answered restarts without one; one that never got that far self-tests again.
        assert env.backends()[-1] == ("cuda:0", "webgpu", not self_tested)
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        (warning,) = warnings_in(loguru_caplog)
        assert warning.startswith("Credit text detection on cuda:0: its GPU helper ")
        assert f"the GPU is tried again in {th.GPU_RETRY_BASE_S:g} s" in warning
        assert "back on the GPU after 1 failed request" in " ".join(infos_in(loguru_caplog))

    def test_the_back_off_doubles_with_failures_in_a_row_and_stops_at_the_cap(self, envs, clock):
        env = envs(modes={"webgpu": "crash-on-request"})
        expected = []
        back_off = th.GPU_RETRY_BASE_S
        while True:
            expected.append(back_off)
            if back_off >= th.GPU_RETRY_MAX_S:
                break
            back_off = min(back_off * 2, th.GPU_RETRY_MAX_S)
        expected.append(th.GPU_RETRY_MAX_S)  # and stays there
        waited = []
        for failure, back_off in enumerate(expected, start=1):
            assert self._detect(env) == ANSWER
            assert len(env.gpu_starts()) == failure
            clock.advance(back_off - 0.5)
            assert self._detect(env) == ANSWER  # still inside this back-off: the CPU
            assert len(env.gpu_starts()) == failure
            assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
            clock.advance(0.5)
            waited.append(back_off)
        assert waited[:4] == [5.0, 10.0, 20.0, 40.0] and waited[-2:] == [600.0, 600.0]
        assert th.GPU_RETRY_BASE_S == 5.0 and th.GPU_RETRY_MAX_S == 600.0

    def test_a_success_resets_the_back_off(self, envs, clock):
        # Fail, fail (a 10 s back-off), succeed, then fail again: the back-off starts over at 5 s.
        env = envs(modes={"webgpu": ["crash-on-request", "crash-on-request", "crash-after-reply", "crash-on-request"]})
        assert self._detect(env) == ANSWER
        clock.advance(th.GPU_RETRY_BASE_S)
        assert self._detect(env) == ANSWER
        clock.advance(th.GPU_RETRY_BASE_S * 2)
        assert self._detect(env) == ANSWER  # the third helper answers on the GPU
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert len(env.gpu_starts()) == 3
        assert self._detect(env) == ANSWER  # it exited after its answer: a failure, read on the CPU
        clock.advance(th.GPU_RETRY_BASE_S - 0.5)
        assert self._detect(env) == ANSWER
        assert len(env.gpu_starts()) == 3
        clock.advance(1)
        assert self._detect(env) == ANSWER
        assert len(env.gpu_starts()) == 4  # 5 s after, not 20 s: the success reset the count

    def test_failures_in_a_row_keep_trying_the_gpu_and_warn_once_per_streak(self, envs, clock, loguru_caplog):
        # crash ×3 (one streak), a reply then an exit (the recovery, then a new streak), crash, ok.
        modes = ["crash-on-request"] * 3 + ["crash-after-reply", "crash-on-request", "ok"]
        env = envs(modes={"webgpu": modes})
        for request in range(1, 8):
            assert self._detect(env) == ANSWER, request
            clock.advance(100)  # past any back-off of these streaks, well inside a helper's idle time
        # Every request but the one that found the helper gone started a GPU helper: never given up on.
        assert len(env.gpu_starts()) == 6
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        warnings = warnings_in(loguru_caplog)
        assert len(warnings) == 2  # the first failure of each streak
        assert all(w.startswith("Credit text detection on cuda:0: its GPU helper failed") for w in warnings)
        recoveries = [m for m in infos_in(loguru_caplog) if "back on the GPU" in m]
        assert recoveries == [
            "Credit text detection on cuda:0: back on the GPU after 3 failed requests",
            "Credit text detection on cuda:0: back on the GPU after 2 failed requests",
        ]

    def test_a_gpu_helper_that_restarts_on_the_cpu_is_a_failure_not_a_verdict(self, envs, clock, loguru_caplog):
        # It ran on this GPU before: coming back on the CPU after an idle exit is the driver failing this time.
        env = envs(modes={"webgpu": ["ok", "software-adapter", "ok"]}, idle_exit_s=0.3)
        assert self._detect(env) == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert self._detect(env) == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False), ("cpu", "cpu", False)]
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
        clock.advance(th.GPU_RETRY_BASE_S + 1)
        assert self._detect(env) == ANSWER
        assert env.backends()[-1] == ("cuda:0", "webgpu", False)
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert any("came up on the CPU" in m and "software renderer" in m for m in warnings_in(loguru_caplog))


class TestCpuFallbackOnTheWorkerRow:
    """A GPU worker's text detection read on the CPU says so on the worker's row, as its other CPU fallbacks do."""

    @pytest.mark.parametrize(
        ("gpu", "modes", "requests", "reason"),
        [
            ("NVIDIA", {"webgpu": ["crash-on-request", "ok"]}, 1, "its GPU helper failed"),
            ("NVIDIA", {"webgpu": ["bad-ready", "ok"]}, 2, "its GPU helper couldn't start"),  # then its back-off
            ("NVIDIA", {"webgpu": "selftest-cpu"}, 2, "other text boxes than the CPU"),  # the verdict, every request
            ("APPLE", {}, 1, "no WebGPU path"),
        ],
        ids=["request-failed", "start-failed-then-cool-down", "boxes-differ", "no-webgpu-path"],
    )
    def test_a_gpu_workers_request_read_on_the_cpu_is_reported(self, envs, gpu, modes, requests, reason):
        env = envs(modes=modes)
        reported: list[str] = []
        device = "cuda:0" if gpu == "NVIDIA" else "videotoolbox"
        for _ in range(requests):
            assert env.pool.detect_boxes(PLANES, gpu=gpu, gpu_device_path=device, on_cpu=reported.append) == ANSWER
        assert len(reported) == requests
        assert all(r.startswith("Credit text detection on the CPU: ") and reason in r for r in reported)

    def test_a_request_read_on_the_gpu_or_by_a_cpu_worker_reports_nothing(self, envs):
        env = envs()
        reported: list[str] = []
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0", on_cpu=reported.append) == ANSWER
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None, on_cpu=reported.append) == ANSWER
        assert reported == []


class TestFallback:
    def test_a_failing_cpu_helper_is_unavailable_and_the_next_call_starts_a_new_one(self, envs):
        env = envs(modes={"cpu": "crash-on-request"})
        with pytest.raises(th.TextDetUnavailableError, match="Text detection failed"):
            env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
        env.modes["cpu"] = "ok"
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]

    def test_a_cpu_helper_that_went_idle_is_started_again(self, envs, loguru_caplog):
        env = envs(idle_exit_s=0.3)
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_a_cpu_helper_that_exits_between_requests_is_started_again(self, envs):
        env = envs(modes={"cpu": "crash-after-reply"})
        for _ in range(2):
            assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
            env.procs[-1].wait(timeout=10)
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]

    def test_an_idle_exit_as_a_request_arrives_starts_the_helper_again_on_the_gpu(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": ["idle-exit-on-request", "ok"]})
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_a_helper_still_leaving_on_its_idle_timer_is_not_taken_for_a_crash(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": ["idle-exit-slow-shutdown", "ok"]})
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    @pytest.mark.parametrize(("gpu", "device"), [(None, None), ("NVIDIA", "cuda:0")], ids=["cpu", "gpu"])
    def test_a_helper_that_left_on_its_idle_timer_is_reaped_without_another_request(self, envs, gpu, device):
        # Its device may get no credit text to look for again for hours; until something reaps it, the helper
        # sits in the container's process list as <defunct>. The test never waits on it, so only the app can.
        env = envs(idle_exit_s=0.3)
        assert env.pool.detect_boxes(PLANES, gpu=gpu, gpu_device_path=device) == ANSWER
        proc = env.procs[0]
        deadline = time.monotonic() + 10
        while proc.returncode is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.returncode == th.IDLE_EXIT_CODE
        assert not Path(f"/proc/{proc.pid}").exists()

    def test_a_helper_close_to_its_idle_exit_is_replaced_before_a_request(self, envs):
        env = envs()
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        helper = env.pool._helpers["cuda:0"]
        helper.last_used -= th.IDLE_EXIT_S
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert env.procs[0].wait(timeout=10) is not None

    def test_a_helper_whose_pipe_outlives_the_kill_never_blocks_the_worker(self, envs, monkeypatch, tmp_path):
        pid_file = tmp_path / "holder.pid"
        monkeypatch.setenv("FAKE_HOLDER_PID_FILE", str(pid_file))
        env = envs(modes={"cpu": "hold-stdin"}, request_timeout_s=1.0)
        big = np.zeros((20, 180, 320), np.uint8)  # more than a pipe buffer: the write blocks
        started = time.monotonic()
        try:
            with pytest.raises(th.TextDetUnavailableError, match="didn't read a request"):
                env.pool.detect_boxes(big, gpu=None, gpu_device_path=None)
            assert time.monotonic() - started < 1.0 + th.KILL_WAIT_S + th.EXIT_CODE_WAIT_S + 3
            assert env.procs[0].poll() is not None
        finally:
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_a_hung_helper_is_killed_with_everything_it_started(self, envs, monkeypatch, tmp_path):
        # A driver or runtime can leave worker processes of its own under the helper. The helper runs in its own
        # session and its whole process group is killed; killing only the helper would leave those running.
        pid_file = tmp_path / "child.pid"
        monkeypatch.setenv("FAKE_CHILD_PID_FILE", str(pid_file))
        env = envs(modes={"cpu": "hang-with-child"}, request_timeout_s=1.0)
        try:
            with pytest.raises(th.TextDetUnavailableError, match="no answer within"):
                env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
            child = int(pid_file.read_text())
            deadline = time.monotonic() + 10
            while _alive(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not _alive(child)
            assert env.started[0]["start_new_session"] is True  # what gives the helper a group of its own
        finally:
            if pid_file.exists() and _alive(int(pid_file.read_text())):
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_an_idle_exit_starts_the_helper_again_without_a_new_self_test(self, envs, loguru_caplog):
        env = envs(idle_exit_s=0.3)
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]


def _alive(pid: int) -> bool:
    """Whether a process is still running (a zombie waiting for its reaper counts as gone)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def test_requests_for_one_device_never_overlap(envs):
    env = envs()
    results: list[list[int]] = []
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(15):
                results.append(env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0"))
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == [] and results == [ANSWER] * 60
    assert env.backends() == [("cuda:0", "webgpu", True)]


def test_close_all_kills_a_helper_that_hangs_at_exit(envs, monkeypatch):
    monkeypatch.setattr(th, "CLOSE_GRACE_S", 0.5)
    env = envs(modes={"cpu": "hang-on-exit"})
    env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
    started = time.monotonic()
    env.pool.close_all()
    assert env.procs[0].poll() is not None and time.monotonic() - started < 10


def cpu_request(env):
    return lambda: env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)


def alive(procs) -> list[bool]:
    return sorted(proc.poll() is None for proc in procs)


def wait_until_alive_count(procs, count: int, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while sum(proc.poll() is None for proc in procs) > count and time.monotonic() < deadline:
        time.sleep(0.05)


class TestCpuHelpers:
    """CPU text detection scales with the CPU workers: a helper per request in flight, up to the saved count."""

    @pytest.mark.parametrize("workers", [1, 4, 8])
    def test_cpu_workers_get_up_to_their_count_of_helpers(self, envs, monkeypatch, release, workers):
        env = envs(modes={"cpu": "held"}, cpu_workers=workers)
        flight = InFlight(monkeypatch, wanted=workers)
        running = Concurrent(cpu_request(env), threads=2 * workers, each=2)
        assert flight.reached.wait(20)  # every CPU worker's request at once, each on its own helper
        time.sleep(0.3)
        assert flight.now == workers  # the rest wait for one of them
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * (4 * workers)
        started = env.cpu_starts()
        assert len(started) == workers  # as many as asked for, and no more
        assert len({spec.key for spec in started}) == workers
        assert all(spec.pci_bus_id is None and spec.selftest is False for spec in started)
        assert flight.peak == workers
        assert all("DRI_PRIME" not in kwargs["env"] for kwargs in env.started)

    def test_no_cpu_helper_starts_before_a_request(self, envs):
        env = envs(cpu_workers=8)
        assert env.specs == []
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]  # one request, one helper, even with 8 allowed

    def test_one_helper_serves_a_cpu_worker_request_after_request(self, envs):
        env = envs(cpu_workers=4)
        for _ in range(5):
            assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]

    @pytest.mark.parametrize("cpu_workers", [0, 1])
    def test_gpu_workers_on_the_cpu_get_helpers_of_their_own(self, envs, monkeypatch, release, cpu_workers):
        # A GPU worker whose GPU can't run text detection does it on the CPU itself, as a preview's CPU rerun does on
        # the same worker: it neither waits for a CPU worker's helper nor counts against the CPU worker count.
        env = envs(modes={"cpu": "held"}, cpu_workers=cpu_workers)
        flight = InFlight(monkeypatch, wanted=3 + cpu_workers)
        calls = [lambda: env.pool.detect_boxes(PLANES, gpu="APPLE", gpu_device_path="videotoolbox")] * 3
        calls += [cpu_request(env)] * cpu_workers
        pending = iter(calls)
        lock = threading.Lock()

        def next_call():
            with lock:
                call = next(pending)
            return call()

        running = Concurrent(next_call, threads=len(calls))
        assert flight.reached.wait(20)  # all of them at once, none waiting on another's helper
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * len(calls)
        assert len(env.cpu_starts()) == len(calls)
        assert (env.pool._cpu_worker_holds, env.pool._gpu_worker_holds) == (0, 0)

    def test_a_gpu_workers_cpu_rerun_gets_a_helper_of_its_own(self, envs, monkeypatch, release):
        # A GPU decode that fails is read again on the same worker with no GPU (as previews do): the caller says it is
        # still a GPU worker, so the rerun neither waits for nor counts against the CPU workers (none, here).
        env = envs(modes={"cpu": "held"}, cpu_workers=0)
        flight = InFlight(monkeypatch, wanted=2)
        calls = iter([cpu_request(env), lambda: env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None,
                                                                      gpu_worker=True)])  # fmt: skip
        lock = threading.Lock()

        def next_call():
            with lock:
                call = next(calls)
            return call()

        running = Concurrent(next_call, threads=2)
        assert flight.reached.wait(20)
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * 2
        assert len(env.cpu_starts()) == 2
        assert all(spec.backend == "cpu" and spec.pci_bus_id is None for spec in env.specs)

    def test_a_cpu_workers_wait_for_a_helper_ends_when_its_job_is_cancelled(self, envs, monkeypatch, release):
        # The saved count was lowered while CPU workers were mid-file: a request waits for a helper to come back, and a
        # cancel of its job ends that wait (previews' running CPU workers are never kept waiting either).
        env = envs(modes={"cpu": "held"}, cpu_workers=1)
        flight = InFlight(monkeypatch, wanted=1)
        holder = Concurrent(cpu_request(env), threads=1)
        assert flight.reached.wait(20)
        cancelled = threading.Event()
        raised: list[BaseException] = []

        def waiter():
            try:
                env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None, cancel_check=cancelled.is_set)
            except (th.TextDetUnavailableError, th.TextDetCancelledError) as exc:
                raised.append(exc)

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        thread.join(0.3)
        assert thread.is_alive()  # waiting: the one helper CPU workers may hold is busy
        cancelled.set()
        thread.join(5)
        assert not thread.is_alive()
        # A cancel, never "text detection failed": a caller that keeps an earlier answer on a failure must not here.
        assert [type(exc) for exc in raised] == [th.TextDetCancelledError] and str(raised[0]) == "cancelled"
        assert not isinstance(raised[0], th.TextDetUnavailableError)
        release()
        results, errors = holder.join()
        assert errors == [] and results == [ANSWER]
        assert len(env.cpu_starts()) == 1
        assert (env.pool._cpu_worker_holds, env.pool._gpu_worker_holds) == (0, 0)

    def test_a_gpu_workers_cpu_rerun_through_the_detector_gets_a_helper_with_no_cpu_workers(
        self, envs, monkeypatch, release, tmp_path
    ):
        # The path below the worker (which passes gpu_worker on its CPU rerun, test_job_runner_real): the credits
        # detector with no GPU asks the real pool as a GPU worker, so with the saved CPU worker count at 0 it gets a
        # helper of its own while a CPU worker's request holds the only one CPU workers may, and neither waits.
        from media_preview_generator.markers.credits import detector
        from media_preview_generator.markers.pipeline import DetectorAnswer
        from media_preview_generator.markers.store import FileRecord, MarkerStore

        env = envs(modes={"cpu": "held"}, cpu_workers=0)
        monkeypatch.setattr(detector, "get_textdet_pool", lambda: env.pool)
        boxes: list = []

        def find(path, **kwargs):
            boxes.append(kwargs["detect_boxes"](PLANES))
            return detector.CreditsTextResult(None, None, (), (), ())

        monkeypatch.setattr(detector, "find_credits", find)
        store = MarkerStore(str(tmp_path / "markers.db"))
        rec = FileRecord(7, "/media/movies/Movie (2020)/Movie (2020).mkv", 100, 1, 6_000_000, None, True)
        ctx = SimpleNamespace(config=SimpleNamespace(ffmpeg_path="ffmpeg"), force=False, store=store,
                              now=lambda: None, run_memo=lambda path: {},
                              settings=SimpleNamespace(credits_tv_s=None, credits_movie_s=None))  # fmt: skip
        flight = InFlight(monkeypatch, wanted=2)
        rerun = lambda: detector.detect_credits_text(rec, ctx=ctx, gpu=None, gpu_device_path=None, gpu_worker=True)  # noqa: E731
        calls = iter([cpu_request(env), rerun])
        lock = threading.Lock()

        def next_call():
            with lock:
                call = next(calls)
            return call()

        try:
            running = Concurrent(next_call, threads=2)
            assert flight.reached.wait(20)  # both in flight at once: neither waited for the other's helper
            release()
            results, errors = running.join()
        finally:
            store.close()
        assert errors == []
        assert sorted(map(type, results), key=lambda t: t.__name__) == [DetectorAnswer, list]
        assert boxes == [ANSWER]
        assert len(env.cpu_starts()) == 2
        assert (env.pool._cpu_worker_holds, env.pool._gpu_worker_holds) == (0, 0)

    def test_a_gpu_workers_request_never_spends_a_cpu_workers_retirement(self, envs, monkeypatch, release):
        # The count drops while two CPU workers' and one GPU worker's requests hold helpers. Whichever returns first,
        # the CPU workers end at the new count and the GPU worker's helper is never the one stopped.
        env = envs(modes={"cpu": "held"}, cpu_workers=2)
        flight = InFlight(monkeypatch, wanted=3)
        calls = iter([cpu_request(env), cpu_request(env),
                      lambda: env.pool.detect_boxes(PLANES, gpu="APPLE", gpu_device_path="videotoolbox")])  # fmt: skip
        lock = threading.Lock()

        def next_call():
            with lock:
                call = next(calls)
            return call()

        running = Concurrent(next_call, threads=3)
        assert flight.reached.wait(20)
        env.cpu_workers = 1
        env.pool.reconcile_cpu_helpers()
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * 3
        wait_until_alive_count(env.cpu_procs(), 2)
        assert alive(env.cpu_procs()) == [False, True, True]  # one CPU worker's helper stopped on its return

        flight = InFlight(monkeypatch)
        results, errors = run_concurrently(cpu_request(env), threads=3)
        assert errors == [] and results == [ANSWER] * 3
        assert flight.peak == 1  # CPU workers are held to the new count, idle helpers or not

    def test_a_count_read_before_a_newer_one_was_applied_is_ignored(self, envs):
        # Saved 3 → 1 → 3 in quick succession: a request that read the 1 but applies it after the second save's
        # resize would stop two helpers the saved count wants.
        env = envs(modes={"cpu": "slow"}, cpu_workers=3)
        run_concurrently(cpu_request(env), threads=3)
        reading = threading.Event()
        go_on = threading.Event()
        stale: dict[str, threading.Thread | None] = {"thread": None}

        def limit():
            value = env.cpu_workers
            if threading.current_thread() is stale["thread"]:
                reading.set()
                go_on.wait(20)
            return value

        env.pool._cpu_limit = limit
        env.cpu_workers = 1
        slow = threading.Thread(target=cpu_request(env))
        stale["thread"] = slow
        slow.start()
        assert reading.wait(20)
        env.cpu_workers = 3
        env.pool.reconcile_cpu_helpers()
        go_on.set()
        slow.join(30)
        assert not slow.is_alive()
        assert alive(env.cpu_procs()) == [True, True, True]
        assert len(env.cpu_starts()) == 3

    def test_a_request_whose_stopping_of_old_helpers_fails_leaves_no_helper_taken(self, envs, monkeypatch):
        env = envs(modes={"cpu": "slow"}, cpu_workers=3)
        run_concurrently(cpu_request(env), threads=3)
        real_stop = th.TextDetectorPool._stop_helpers
        failures = [RuntimeError("kill failed")]

        def stop(helpers):
            if helpers and failures:
                raise failures.pop()
            real_stop(helpers)

        monkeypatch.setattr(env.pool, "_stop_helpers", stop)
        env.cpu_workers = 1
        with pytest.raises(RuntimeError, match="kill failed"):
            env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
        # No helper was taken for the failed request, so the one CPU worker still gets one without waiting.
        answered = Concurrent(cpu_request(env), threads=1)
        results, errors = answered.join()
        assert errors == [] and results == [ANSWER]
        assert env.pool._cpu_worker_holds == 0

    def test_a_request_beyond_the_count_waits_for_a_free_helper(self, envs, monkeypatch, release):
        env = envs(modes={"cpu": "held"}, cpu_workers=2)
        flight = InFlight(monkeypatch, wanted=2)
        running = Concurrent(cpu_request(env), threads=5)
        assert flight.reached.wait(20)
        time.sleep(0.3)
        assert flight.now == 2
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * 5
        assert len(env.cpu_starts()) == 2 and flight.peak == 2

    def test_a_lower_count_stops_the_idle_helpers_beyond_it_at_once(self, envs, monkeypatch):
        env = envs(modes={"cpu": "slow"}, cpu_workers=3)
        run_concurrently(cpu_request(env), threads=3)
        assert alive(env.cpu_procs()) == [True, True, True]

        env.cpu_workers = 1
        env.pool.reconcile_cpu_helpers()
        assert alive(env.cpu_procs()) == [False, False, True]  # stopped now, not left to their idle timer

        flight = InFlight(monkeypatch)
        results, errors = run_concurrently(cpu_request(env), threads=3)
        assert errors == [] and results == [ANSWER] * 3
        assert len(env.cpu_starts()) == 3 and flight.peak == 1  # the one left serves them in turn

    def test_a_lower_count_saved_another_way_takes_effect_on_the_next_request(self, envs):
        # The workers API saves the count without the settings page's hooks; the next request reads it anyway.
        env = envs(modes={"cpu": "slow"}, cpu_workers=3)
        run_concurrently(cpu_request(env), threads=3)
        env.cpu_workers = 1
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert alive(env.cpu_procs()) == [False, False, True]

    def test_a_busy_helper_beyond_a_lower_count_stops_when_its_request_ends(self, envs, monkeypatch, release):
        env = envs(modes={"cpu": "held"}, cpu_workers=2)
        flight = InFlight(monkeypatch, wanted=2)
        running = Concurrent(cpu_request(env), threads=2)
        assert flight.reached.wait(20)
        env.cpu_workers = 1
        env.pool.reconcile_cpu_helpers()
        assert alive(env.cpu_procs()) == [True, True]  # neither is pulled mid-request
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * 2
        wait_until_alive_count(env.cpu_procs(), 1)
        assert alive(env.cpu_procs()) == [False, True]
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert len(env.cpu_starts()) == 2

    def test_a_raise_before_a_busy_helper_is_returned_keeps_it(self, envs, monkeypatch, release):
        env = envs(modes={"cpu": "held"}, cpu_workers=2)
        flight = InFlight(monkeypatch, wanted=2)
        running = Concurrent(cpu_request(env), threads=2)
        assert flight.reached.wait(20)
        env.cpu_workers = 1
        env.pool.reconcile_cpu_helpers()
        env.cpu_workers = 2
        env.pool.reconcile_cpu_helpers()
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * 2
        assert alive(env.cpu_procs()) == [True, True]

    def test_a_stopped_helper_leaves_the_pool_before_its_process_is_stopped(self, envs, monkeypatch):
        # Its key can be handed out again the moment the pool's lock is released: a request starting a helper under
        # that key must get a new process, never the one about to be killed.
        env = envs(modes={"cpu": "slow"}, cpu_workers=2)
        run_concurrently(cpu_request(env), threads=2)
        seen: list[tuple[set[str], list[int]]] = []

        def stop(helpers):
            seen.append((set(env.pool._helpers), [helper.proc.pid for helper in helpers]))
            th.TextDetectorPool._stop_helpers(helpers)

        monkeypatch.setattr(env.pool, "_stop_helpers", stop)
        env.cpu_workers = 1
        env.pool.reconcile_cpu_helpers()
        [(keys_left, stopped)] = [entry for entry in seen if entry[1]]
        assert len(stopped) == 1 and len(keys_left) == 1  # out of the pool before its kill
        assert stopped[0] not in {helper.proc.pid for helper in env.pool._helpers.values()}

    def test_a_higher_count_lets_more_helpers_start(self, envs, monkeypatch, release):
        env = envs(modes={"cpu": "held"}, cpu_workers=1)
        release()
        run_concurrently(cpu_request(env), threads=3)
        assert len(env.cpu_starts()) == 1
        Path(os.environ["FAKE_RELEASE_FILE"]).unlink()
        env.cpu_workers = 3
        flight = InFlight(monkeypatch, wanted=3)
        running = Concurrent(cpu_request(env), threads=3)
        assert flight.reached.wait(20)
        release()
        results, errors = running.join()
        assert errors == [] and results == [ANSWER] * 3
        assert len(env.cpu_starts()) == 3 and flight.peak == 3

    def test_close_all_wakes_a_request_waiting_for_a_cpu_helper(self, envs, monkeypatch):
        env = envs(modes={"cpu": "hang-on-request"}, cpu_workers=1, request_timeout_s=30.0)
        flight = InFlight(monkeypatch, wanted=1)
        failed: list[BaseException] = []

        def work():
            try:
                env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                failed.append(exc)

        first = threading.Thread(target=work)
        first.start()
        assert flight.reached.wait(20)
        second = threading.Thread(target=work)  # waits: the only helper is busy
        second.start()
        time.sleep(0.3)
        started = time.monotonic()
        env.pool.close_all()
        for thread in (first, second):
            thread.join(30)
        assert time.monotonic() - started < 10
        assert len(failed) == 2 and all(isinstance(exc, th.TextDetShuttingDownError) for exc in failed)
        assert len(env.cpu_starts()) == 1

    def test_every_cpu_helper_keeps_to_two_threads(self, monkeypatch, tmp_path):
        monkeypatch.setenv(th.MODEL_ENV, str(tmp_path / "m.onnx"))
        command = th.helper_command(th.HelperSpec("cpu#3", "cpu", None, False, {}))
        assert command[command.index("--threads") + 1] == "2"


class TestGpuPinning:
    """Only the helper's environment puts it on a physical GPU (controller note N1, measured 2026-09-16)."""

    def test_an_nvidia_worker_without_a_known_address_is_not_pinned(self, envs, monkeypatch):
        env = envs()
        monkeypatch.setattr(th, "worker_pci_bus_id", lambda gpu, path: None)
        assert env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        started = env.started[0]["env"]
        assert started["VK_DRIVER_FILES"] == NVIDIA_ICD
        assert "DRI_PRIME" not in started
        assert "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE" not in started

    @pytest.mark.parametrize("gpu", ["NVIDIA", "INTEL"])
    def test_an_inherited_selection_never_outranks_this_helpers_pin(self, envs, monkeypatch, gpu):
        env = envs()
        monkeypatch.setenv("MESA_VK_DEVICE_SELECT", "10005:0")  # would send Dawn to llvmpipe
        monkeypatch.setenv("NODEVICE_SELECT", "1")  # would turn the selection layer off entirely
        monkeypatch.setenv("DRI_PRIME", "pci-0000_99_00_0")  # another GPU's tag
        path = "cuda:0" if gpu == "NVIDIA" else "/dev/dri/renderD128"
        assert env.pool.detect_boxes(PLANES, gpu=gpu, gpu_device_path=path) == ANSWER
        started = env.started[0]["env"]
        assert "MESA_VK_DEVICE_SELECT" not in started
        assert "NODEVICE_SELECT" not in started
        assert started["DRI_PRIME"] == f"pci-{WORKER_PCI[path].replace(':', '_').replace('.', '_')}"

    def test_the_cpu_helper_inherits_no_gpu_selection(self, envs, monkeypatch):
        env = envs()
        monkeypatch.setenv("DRI_PRIME", "pci-0000_99_00_0")
        monkeypatch.setenv("MESA_VK_DEVICE_SELECT", "10005:0")
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert not [name for name in PIN_ENV if name in env.started[0]["env"]]


class TestCommand:
    """The argv the pool really starts (the fake replaces it everywhere else)."""

    @pytest.mark.parametrize(
        ("gpu", "path", "expected"),
        [
            (None, None, "cpu"),
            (None, "cuda:0", "cpu"),
            ("NVIDIA", "cuda:1", "cuda:1"),
            ("APPLE", None, "APPLE"),
            ("cpu", None, "gpu:cpu"),  # a GPU type or path spelt "cpu" must not share the CPU helper's key
            ("NVIDIA", "cpu", "gpu:cpu"),
            ("NVIDIA", "cpu#2", "gpu:cpu#2"),  # nor one spelt like another CPU helper's
        ],
    )
    def test_device_key(self, gpu, path, expected):
        assert th.device_key(gpu, path) == expected

    def test_a_gpu_helper_is_started_with_its_device_and_the_self_test(self, monkeypatch, tmp_path):
        monkeypatch.setenv(th.MODEL_ENV, str(tmp_path / "m.onnx"))
        spec = th.HelperSpec("cuda:0", "webgpu", "0000:02:00.0", True, {})
        assert th.helper_command(spec) == [
            sys.executable, "-m", th.MODULE, "--backend", "webgpu", "--model", str(tmp_path / "m.onnx"),
            "--threads", str(th.THREADS), "--idle-exit-s", f"{th.IDLE_EXIT_S:g}", "--pci-bus-id", "0000:02:00.0",
        ]  # fmt: skip

    def test_a_restart_without_an_address_asks_for_no_self_test(self, monkeypatch, tmp_path):
        monkeypatch.setenv(th.MODEL_ENV, str(tmp_path / "m.onnx"))
        command = th.helper_command(th.HelperSpec("cpu", "cpu", None, False, {}))
        assert command[3:5] == ["--backend", "cpu"]
        assert "--pci-bus-id" not in command
        assert command[-1] == "--no-selftest"


def boxes_for(count, shift=0):
    """One frame's boxes: ``count`` of them, each ``shift`` pixels right of where the other side puts it."""
    return tuple((10 + shift, 10 + 30 * n, 60 + shift, 30 + 30 * n) for n in range(count))


class FakeDetector:
    def __init__(self, counts, per_frame_s, clock, shift=0, large_shift=0):
        self.counts, self.per_frame_s, self.clock, self.shift = counts, per_frame_s, clock, shift
        self.large_shift = large_shift

    def detect(self, frames):
        self.clock.now += self.per_frame_s * len(frames)
        shift = self.large_shift if frames.shape[1] > 180 else self.shift
        return [boxes_for(n, shift) for n in self.counts[: len(frames)]]


class FakeClock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize(
    ("gpu_counts", "gpu_shift", "gpu_s", "use_gpu", "same"),
    [
        ([1, 0, 3], 0, 0.005, True, True),
        # A GPU worker's work runs on its GPU (the owner's worker model): a slower GPU is still used.
        ([1, 0, 3], 0, 0.030, True, True),
        ([1, 1, 3], 0, 0.005, False, False),
        # The same number of boxes in other places. Rule J version 3 reads row[3], so this backend answers
        # differently from the CPU path on every file with an overlay or a band step, and a count test
        # passes it.
        ([1, 0, 3], 40, 0.005, False, False),
    ],
    ids=["same-boxes-and-faster", "same-boxes-and-slower", "different-counts", "same-counts-other-places"],
)
def test_the_self_test_needs_the_same_boxes_whatever_the_speed(gpu_counts, gpu_shift, gpu_s, use_gpu, same):
    clock = FakeClock()
    frames = np.zeros((3, 180, 320), np.uint8)
    gpu = FakeDetector(gpu_counts, gpu_s, clock, shift=gpu_shift)
    result = th.self_test(gpu, FakeDetector([1, 0, 3], 0.018, clock), frames, clock=clock, warmup=1)
    assert (result.use_gpu, result.same_boxes) == (use_gpu, same)
    assert result.cpu_ms == pytest.approx(18.0) and result.gpu_ms == pytest.approx(gpu_s * 1000)


@pytest.mark.parametrize(("large_shift", "use_gpu"), [(0, True), (40, False)], ids=["same", "other-places"])
def test_the_self_test_compares_the_boxes_at_640x360_too(large_shift, use_gpu):
    # The detector's larger reading sends 640x360 frames to the same helper. A GPU that finds the CPU's boxes at
    # 320x180 and other ones at 640x360 would answer differently from the CPU on every file read at that size.
    clock = FakeClock()
    frames = np.zeros((3, 180, 320), np.uint8)
    large = np.zeros((2, 360, 640), np.uint8)
    gpu = FakeDetector([1, 0, 3], 0.005, clock, large_shift=large_shift)
    result = th.self_test(gpu, FakeDetector([1, 0, 3], 0.018, clock), frames, clock=clock, warmup=1,
                          large_frames=large)  # fmt: skip
    assert (result.same_boxes, result.same_boxes_large, result.use_gpu) == (True, not large_shift, use_gpu)
    assert result.gpu_ms == pytest.approx(5.0)  # timed on the 320x180 frames only: what the helper mostly serves
    if not use_gpu:
        assert result.cpu_reason() == "the GPU was finding different boxes than the CPU at 640x360"


def test_the_self_tests_larger_frames_are_the_detectors_larger_reading():
    from media_preview_generator.markers.credits import detector

    assert th.SELFTEST_LARGE_SCALE == detector.RETRY_SCALE


def test_the_self_test_warms_both_detectors_before_timing():
    clock = FakeClock()
    frames = np.zeros((6, 180, 320), np.uint8)
    gpu, cpu = FakeDetector([0] * 6, 0.001, clock), FakeDetector([0] * 6, 0.002, clock)
    seen: list[int] = []
    for detector in (gpu, cpu):
        detected = detector.detect
        detector.detect = lambda frames, detected=detected: (seen.append(len(frames)), detected(frames))[1]
    th.self_test(gpu, cpu, frames, clock=clock, warmup=2)
    assert th.SELFTEST_ROUNDS == 7
    assert seen == [2, 2] + [6, 6] * 7  # warm-up on both, then one timed pair per round


class TestAvailability:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch, tmp_path):
        th.forget_text_detection_state()
        self.model = tmp_path / "model.onnx"
        self.model.write_bytes(b"m")
        monkeypatch.setenv(th.MODEL_ENV, str(self.model))
        monkeypatch.setattr(th, "_find_spec", lambda name: object())
        self.now = [1000.0]
        monkeypatch.setattr(th, "_monotonic", lambda: self.now[0])
        self.runs: list[tuple[list[str], dict]] = []
        yield
        th.forget_text_detection_state()

    def _run(self, monkeypatch, result):
        def run(cmd, **kwargs):
            self.runs.append((cmd, kwargs))
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(th.subprocess, "run", run)

    def test_a_passing_check_is_available_and_kept(self, monkeypatch):
        self._run(monkeypatch, SimpleNamespace(returncode=0, stdout="", stderr=""))
        assert th.text_detection_state() is TextDetState.AVAILABLE
        assert th.text_detection_status() == (True, "")
        self.now[0] += 10_000
        assert th.text_detection_state() is TextDetState.AVAILABLE
        assert len(self.runs) == 1
        command, kwargs = self.runs[0]
        assert command[1:] == ["-m", th.MODULE, "--check", "--model", str(self.model)]
        # A check that never returns would hold _state_lock, and with it every Settings readiness call in the
        # single gunicorn worker.
        assert kwargs["timeout"] == th.CHECK_TIMEOUT_S
        assert kwargs["capture_output"] is True and kwargs["text"] is True

    @pytest.mark.parametrize("missing", ["onnxruntime", "cv2", "pyclipper"])
    def test_missing_packages_are_absent_without_running_anything(self, monkeypatch, missing):
        monkeypatch.setattr(th, "_find_spec", lambda name: None if name == missing else object())
        self._run(monkeypatch, AssertionError("must not run"))
        assert th.text_detection_status() == (
            False,
            "Needs ONNX Runtime and OpenCV, which the Docker image includes; they aren't installed here",
        )
        assert th.text_detection_state() is TextDetState.ABSENT

    def test_a_missing_model_is_absent(self, monkeypatch):
        self.model.unlink()
        self._run(monkeypatch, AssertionError("must not run"))
        assert th.text_detection_status() == (
            False,
            f"Needs the text detection model, which the Docker image includes; it isn't at {self.model}",
        )

    def test_a_check_that_says_absent_is_absent_with_its_reason(self, monkeypatch):
        self._run(
            monkeypatch,
            SimpleNamespace(
                returncode=th.CHECK_ABSENT_CODE,
                stdout=f"The text detection model at {self.model} isn't the expected file\n",
                stderr="",
            ),
        )
        assert th.text_detection_status() == (
            False,
            f"The text detection model at {self.model} isn't the expected file",
        )
        self.now[0] += 10_000
        th.text_detection_state()
        assert len(self.runs) == 1  # absent is kept for the process

    @pytest.mark.parametrize(
        "result",
        [
            subprocess.TimeoutExpired("python", 30),
            OSError("exec"),
            SimpleNamespace(returncode=1, stdout="", stderr="Traceback"),
        ],
    )
    def test_a_check_that_does_not_answer_is_unknown_and_asked_again_after_10_minutes(self, monkeypatch, result):
        self._run(monkeypatch, result)
        assert th.text_detection_status() == (
            False,
            "The text detection check didn't answer; it is checked again in 10 minutes",
        )
        self.now[0] += 599
        assert th.text_detection_state() is TextDetState.UNKNOWN and len(self.runs) == 1
        self.now[0] += 2
        th.text_detection_state()
        assert len(self.runs) == 2


def test_importing_the_helper_module_loads_no_onnxruntime_or_opencv():
    heavy = "('onnxruntime', 'cv2', 'pyclipper', 'onnxruntime_ep_webgpu')"
    code = ("import sys, media_preview_generator.markers.credits.textdet_helper; "
            f"print(sorted(m for m in {heavy} if m in sys.modules))")  # fmt: skip
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


class PacedDetector:
    """A detector whose per-frame cost and box counts can change round by round.

    The warm-up call takes the first value of each list; the last value repeats once the list runs out.
    """

    def __init__(self, counts, per_frame_s, clock):
        self.counts, self.per_frame_s, self.clock = list(counts), list(per_frame_s), clock

    def detect(self, frames):
        cost = self.per_frame_s.pop(0) if len(self.per_frame_s) > 1 else self.per_frame_s[0]
        counts = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        self.clock.now += cost * len(frames)
        return [boxes_for(n) for n in counts[: len(frames)]]


@pytest.mark.parametrize("ratio", [0.1, 0.9, 0.901, 1.0, 1.1, 2.0, 30.0])
@pytest.mark.parametrize(("same_boxes", "same_large"), [(True, True), (False, True), (True, False)])
def test_the_speed_ratio_never_decides(ratio, same_boxes, same_large):
    # The timing is logged for information; only the boxes decide, at both sizes.
    assert th.SelfTest(5.0, 10.0, ratio, same_boxes, same_large).use_gpu is (same_boxes and same_large)


def _self_test(gpu_rounds_s: list[float], cpu_rounds_s: list[float]) -> th.SelfTest:
    """The self-test on detectors whose timed rounds cost these seconds per frame (warm-up free)."""
    clock = FakeClock()
    clock.now = 0.0
    frames = np.zeros((3, 180, 320), np.uint8)
    gpu = PacedDetector([[1, 0, 3]], [0.0, *gpu_rounds_s], clock)
    cpu = PacedDetector([[1, 0, 3]], [0.0, *cpu_rounds_s], clock)
    return th.self_test(gpu, cpu, frames, clock=clock, warmup=1)


@pytest.mark.parametrize(
    ("gpu_rounds_s", "cpu_rounds_s", "expected"),
    [
        # One slow GPU round: a single-sample test would say CPU; the other six rounds outvote it.
        ([0.100, *[0.001] * 6], [0.010] * 7, (1.0, 10.0, 0.1, True)),
        # A GPU fast in one round only: six of seven back-to-back rounds say 0.95 of the CPU's time, so that is what
        # the log reports, not its luckiest round.
        ([0.004, 0.0095, 0.100, *[0.0095] * 4], [0.010] * 7, (9.5, 10.0, 0.95, True)),
        # Host load slows both sides in round 3: its ratio (0.5) is the others', so nothing changes.
        ([0.005, 0.005, 0.050, *[0.005] * 4], [0.010, 0.010, 0.100, *[0.010] * 4], (5.0, 10.0, 0.5, True)),
        # The same rounds with no load at all: the same verdict.
        ([0.005] * 7, [0.010] * 7, (5.0, 10.0, 0.5, True)),
        # Each round's own ratio, not the ratio of the two medians: rounds 4-7 slow the GPU (another job on the
        # card), rounds 5-7 the whole host. Six of seven back-to-back rounds say the GPU takes half the CPU's time;
        # the medians say 10 vs 10.
        ([*[0.005] * 3, *[0.010] * 4], [*[0.010] * 4, *[0.020] * 3], (10.0, 10.0, 0.5, True)),
        # The CPU busy in six rounds of seven while the GPU isn't: six back-to-back rounds say 0.475.
        ([0.0095] * 7, [0.020, 0.020, 0.010, *[0.020] * 4], (9.5, 20.0, 0.475, True)),
    ],
    ids=[
        "one-slow-gpu-round",
        "one-lucky-gpu-round",
        "load-on-both-sides",
        "no-load",
        "per-round-not-medians",
        "cpu-busy-in-most-rounds",
    ],
)
def test_the_logged_ratio_is_the_median_per_round_ratio(gpu_rounds_s, cpu_rounds_s, expected):
    result = _self_test(gpu_rounds_s, cpu_rounds_s)
    assert (result.gpu_ms, result.cpu_ms, result.ratio, result.use_gpu) == expected


@pytest.mark.parametrize(
    ("result", "reason"),
    [
        (th.SelfTest(1.0, 10.0, 0.1, False), "the GPU was finding different boxes than the CPU"),
        (th.SelfTest(20.0, 10.0, 2.0, False, False), "the GPU was finding different boxes than the CPU"),
        (th.SelfTest(1.0, 10.0, 0.1, True, False), "the GPU was finding different boxes than the CPU at 640x360"),
    ],
    ids=["different-boxes", "different-at-both-sizes", "different-at-640x360"],
)
def test_the_cpu_reason_says_which_boxes_differed(result, reason):
    assert result.cpu_reason() == reason


def test_boxes_that_differ_in_any_round_fail_the_self_test():
    clock = FakeClock()
    clock.now = 0.0
    frames = np.zeros((3, 180, 320), np.uint8)
    gpu = PacedDetector([[1, 0, 3], [1, 0, 3], [1, 0, 3], [9, 9, 9]], [0.001], clock)
    cpu = PacedDetector([[1, 0, 3]], [0.010], clock)
    result = th.self_test(gpu, cpu, frames, clock=clock, warmup=1)
    assert result.same_boxes is False and result.use_gpu is False


class StubDetector:
    """A stand-in :class:`textdet.TextDetector` that costs a fixed time per frame on a fake clock."""

    def __init__(self, counts, ms_per_frame, clock, *, backend, fail_after=None, shift=0, large_shift=0):
        self.counts, self.ms_per_frame, self.clock = counts, ms_per_frame, clock
        self.backend, self._fail_after, self._calls, self.shift = backend, fail_after, 0, shift
        self.large_shift = large_shift

    def detect(self, frames):
        self._calls += 1
        if self._fail_after is not None and self._calls > self._fail_after:
            raise RuntimeError("the device was lost")
        self.clock.now += self.ms_per_frame * len(frames) / 1000.0
        shift = self.large_shift if frames.shape[1] > 180 else self.shift
        return [boxes_for(n, shift) for n in self.counts[: len(frames)]]


class StubTextDet:
    """The helper process's whole view of :mod:`textdet`, so ``_start_detector``'s branches can be driven directly."""

    class TextDetError(Exception):
        pass

    class ModelError(TextDetError):
        pass

    class WebGpuSessionError(TextDetError):
        pass

    def __init__(
        self,
        clock,
        *,
        addresses=("0000:02:00.0",),
        gpu_ms=1.0,
        cpu_ms=10.0,
        gpu_counts=None,
        gpu_shift=0,
        gpu_large_shift=0,
        webgpu_error=None,
        cpu_error=None,
        gpu_fail_after=None,
    ):
        self.clock = clock
        self.devices = [SimpleNamespace(device=SimpleNamespace(metadata={"pci_bus_id": pci})) for pci in addresses]
        self.gpu_ms, self.cpu_ms = gpu_ms, cpu_ms
        self.gpu_counts = gpu_counts if gpu_counts is not None else list(range(th.SELFTEST_FRAMES))
        self.gpu_shift, self.gpu_large_shift = gpu_shift, gpu_large_shift
        self.cpu_counts = list(range(th.SELFTEST_FRAMES))
        self.frame_sizes: list[tuple] = []
        self.webgpu_error, self.cpu_error, self.gpu_fail_after = webgpu_error, cpu_error, gpu_fail_after
        self.cpu_sessions: list[tuple] = []
        self.webgpu_sessions: list = []
        self.listed = 0

    def cpu_session(self, model, threads=2):
        if self.cpu_error is not None:
            raise self.cpu_error
        self.cpu_sessions.append((model, threads))
        return "cpu-session"

    def webgpu_devices(self):
        self.listed += 1
        return self.devices

    def webgpu_session(self, model, device, threads=2):
        self.webgpu_sessions.append(device)
        if self.webgpu_error is not None:
            raise self.webgpu_error
        return "gpu-session"

    def TextDetector(self, session, *, backend):  # noqa: N802 - the name textdet exports
        if backend == "cpu":
            return StubDetector(self.cpu_counts, self.cpu_ms, self.clock, backend="cpu")
        return StubDetector(self.gpu_counts, self.gpu_ms, self.clock, backend="webgpu",
                            fail_after=self.gpu_fail_after, shift=self.gpu_shift,
                            large_shift=self.gpu_large_shift)  # fmt: skip

    def synthetic_frames(self, count=20, scale=1):
        self.frame_sizes.append((count, scale))
        return np.zeros((count, 180 * scale, 320 * scale), np.uint8)


@pytest.mark.parametrize("error", [OSError("no libvulkan.so.1"), ValueError("a ctypes surprise")])
def test_a_vulkan_listing_that_breaks_is_left_to_the_session(monkeypatch, error):
    # Anything going wrong in the check itself skips it: it must never read as the GPU failing.
    import ctypes

    def broken(name):
        raise error

    monkeypatch.setattr(ctypes, "CDLL", broken)
    assert th._vulkan_adapters() is None


class TestHelperProcessBackendChoice:
    """``_start_detector``: what the helper process serves with, and the reason its ready line gives."""

    @pytest.fixture
    def clock(self, monkeypatch):
        clock = FakeClock()
        clock.now = 0.0
        monkeypatch.setattr(th, "_perf_counter", clock)
        return clock

    @pytest.fixture(autouse=True)
    def adapters(self, monkeypatch):
        """The Vulkan devices the helper's own environment exposes: one hardware GPU unless a test says otherwise."""
        listed = [[("Test GPU", th.VK_DISCRETE_GPU)]]
        monkeypatch.setattr(th, "_vulkan_adapters", lambda: listed[0])
        return listed

    def _start(self, stub, **overrides):
        args = SimpleNamespace(
            backend="webgpu", model="/models/det.onnx", threads=2, pci_bus_id="0000:02:00.0", selftest=True
        )
        for name, value in overrides.items():
            setattr(args, name, value)
        return th._start_detector(stub, args)

    def test_a_gpu_that_is_clearly_faster_wins(self, clock):
        stub = StubTextDet(clock, gpu_ms=1.0, cpu_ms=10.0)
        detector, ready = self._start(stub)
        assert detector.backend == "webgpu"
        assert ready == {
            "backend": "webgpu",
            "selftest": {"gpu_ms": 1.0, "cpu_ms": 10.0, "ratio": 0.1, "same_boxes": True, "same_boxes_large": True},
            "reason": "",
        }
        # 20 frames at 320x180 timed, and 8 at 640x360 compared.
        assert stub.frame_sizes == [(th.SELFTEST_FRAMES, 1), (th.SELFTEST_LARGE_FRAMES, th.SELFTEST_LARGE_SCALE)]

    def test_a_slower_gpu_still_serves_from_the_gpu(self, clock):
        # The worker is a GPU worker: its text detection runs on its GPU when the GPU finds the CPU's boxes.
        stub = StubTextDet(clock, gpu_ms=20.0, cpu_ms=10.0)
        detector, ready = self._start(stub)
        assert detector.backend == "webgpu"
        assert ready == {
            "backend": "webgpu",
            "selftest": {"gpu_ms": 20.0, "cpu_ms": 10.0, "ratio": 2.0, "same_boxes": True, "same_boxes_large": True},
            "reason": "",
        }

    def test_a_gpu_that_only_ties_serves_from_the_gpu(self, clock):
        stub = StubTextDet(clock, gpu_ms=10.0, cpu_ms=10.0)
        detector, ready = self._start(stub)
        assert detector.backend == "webgpu"
        assert ready["selftest"]["ratio"] == 1.0

    @pytest.mark.parametrize("selftest", [True, False], ids=["first-start", "restart"])
    @pytest.mark.parametrize(
        "listed",
        [
            [("llvmpipe (LLVM 19.1.1, 256 bits)", th.VK_CPU)],
            # Named as a software renderer whatever type the driver reports.
            [("SwiftShader Device (Subzero)", th.VK_OTHER)],
        ],
        ids=["llvmpipe", "swiftshader"],
    )
    def test_a_software_adapter_is_not_a_gpu(self, clock, adapters, listed, selftest):
        # Dawn runs on whichever adapter the helper's Vulkan environment leaves it; llvmpipe finds the CPU's boxes, so
        # only this check keeps a GPU worker's text detection off a software renderer.
        adapters[0] = listed
        stub = StubTextDet(clock)
        detector, ready = self._start(stub, selftest=selftest)
        assert detector.backend == "cpu"
        assert ready == {
            "backend": "cpu",
            "selftest": None,
            "reason": f"WebGPU would run on {listed[0][0]}, a software renderer, not on this GPU",
        }
        assert stub.webgpu_sessions == []  # checked before a session is built on it

    @pytest.mark.parametrize(
        "listed",
        [
            [("Test GPU", th.VK_DISCRETE_GPU), ("llvmpipe (LLVM 19.1.1, 256 bits)", th.VK_CPU)],
            [("Test iGPU", th.VK_INTEGRATED_GPU)],
            [("Virtual GPU", th.VK_VIRTUAL_GPU)],
            # Nothing listed, or Vulkan couldn't be asked: the WebGPU session itself says whether there's an adapter.
            [],
            None,
        ],
        ids=["hardware-beside-llvmpipe", "integrated", "virtual", "none-listed", "unknown"],
    )
    def test_anything_but_a_lone_software_renderer_is_left_to_the_session(self, clock, adapters, listed):
        adapters[0] = listed
        detector, ready = self._start(StubTextDet(clock))
        assert detector.backend == "webgpu" and ready["backend"] == "webgpu"

    def test_a_gpu_that_finds_different_boxes_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, gpu_ms=1.0, cpu_ms=10.0, gpu_counts=[99] * th.SELFTEST_FRAMES)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["reason"] == "the GPU was finding different boxes than the CPU"
        assert ready["selftest"]["same_boxes"] is False

    def test_a_gpu_that_finds_the_same_boxes_elsewhere_serves_from_the_cpu(self, clock):
        # Same count, other places. Rule J version 3 reads where a frame's text is, so this backend would
        # answer differently from the CPU path on every file with an overlay or a band step -- and the
        # self-test is the only runtime check there is.
        stub = StubTextDet(clock, gpu_ms=1.0, cpu_ms=10.0, gpu_shift=40)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["reason"] == "the GPU was finding different boxes than the CPU"
        assert ready["selftest"]["same_boxes"] is False

    def test_a_gpu_that_finds_other_boxes_at_640x360_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, gpu_ms=1.0, cpu_ms=10.0, gpu_large_shift=40)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["reason"] == "the GPU was finding different boxes than the CPU at 640x360"
        assert (ready["selftest"]["same_boxes"], ready["selftest"]["same_boxes_large"]) == (True, False)

    def test_a_self_test_that_fails_part_way_through_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, gpu_fail_after=2)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["selftest"] is None
        assert ready["reason"] == "the WebGPU session failed: RuntimeError: the device was lost"
        # A failure, not a verdict on the GPU: the parent tries the GPU again later.
        assert ready["failed"] is True

    @pytest.mark.parametrize(
        "listed", [[("Test GPU", th.VK_DISCRETE_GPU)], [], None], ids=["hardware-listed", "none-listed", "unknown"]
    )
    def test_a_webgpu_session_error_is_a_cpu_fallback_not_a_crash(self, clock, adapters, listed):
        # Dawn gave no adapter, whatever Vulkan lists (a GPU Dawn rejects, say): the GPU can't run text detection here,
        # a verdict for the process rather than a failure retried every hour. A GPU that has served and then comes back
        # this way is a failure all the same (_accept_gpu_helper).
        adapters[0] = listed
        stub = StubTextDet(clock, webgpu_error=StubTextDet.WebGpuSessionError("providers: ['CPUExecutionProvider']"))
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["backend"] == "cpu"
        assert ready["reason"].startswith("this GPU has no usable WebGPU adapter: ")
        assert "failed" not in ready

    def test_no_ep_device_for_this_gpu_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, addresses=("0000:07:00.0", "0000:65:00.0"))
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["reason"] == "no WebGPU device is this GPU (2 found)"
        assert stub.webgpu_sessions == []  # no session was built on someone else's device

    def test_the_session_is_built_on_the_workers_own_ep_device(self, clock):
        stub = StubTextDet(clock, addresses=("0000:07:00.0", "0000:02:00.0"))
        detector, ready = self._start(stub)
        assert detector.backend == "webgpu" and ready["backend"] == "webgpu"
        assert stub.webgpu_sessions == [stub.devices[1]]

    def test_a_cpu_helper_never_lists_the_vulkan_adapters(self, clock, monkeypatch):
        monkeypatch.setattr(th, "_vulkan_adapters", lambda: pytest.fail("a CPU helper has no adapter to check"))
        detector, _ready = self._start(StubTextDet(clock), backend="cpu")
        assert detector.backend == "cpu"

    def test_no_selftest_keeps_the_gpu_and_builds_no_cpu_session(self, clock):
        stub = StubTextDet(clock, gpu_ms=99.0, cpu_ms=1.0)
        detector, ready = self._start(stub, selftest=False)
        assert detector.backend == "webgpu"
        assert ready == {"backend": "webgpu", "selftest": None, "reason": ""}
        assert stub.cpu_sessions == []

    def test_a_cpu_helper_never_looks_at_the_gpu(self, clock):
        stub = StubTextDet(clock)
        detector, ready = self._start(stub, backend="cpu")
        assert detector.backend == "cpu"
        assert ready == {"backend": "cpu", "selftest": None, "reason": ""}
        assert stub.listed == 0
        assert stub.cpu_sessions == [("/models/det.onnx", 2)]

    def test_a_bad_model_is_reported_not_swallowed(self, clock):
        stub = StubTextDet(clock, webgpu_error=StubTextDet.ModelError("isn't the expected file"))
        with pytest.raises(StubTextDet.ModelError):
            self._start(stub)


class TestShutdown:
    """close_all() is the end of the pool: nothing it kills counts as that device failing (I1, I2)."""

    def test_close_all_racing_an_in_flight_request_never_demotes_the_device(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": "hang-on-request"}, request_timeout_s=30.0)
        failed: list[BaseException] = []

        def work():
            try:
                env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0")
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                failed.append(exc)

        worker = threading.Thread(target=work)
        worker.start()
        deadline = time.monotonic() + 20
        while "cuda:0" not in env.pool._helpers and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)  # let the request reach the helper, which then hangs
        env.pool.close_all()
        worker.join(30)
        assert not worker.is_alive()
        assert len(failed) == 1 and isinstance(failed[0], th.TextDetShuttingDownError)
        assert "shutting down" in str(failed[0])
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_close_all_racing_an_in_flight_cpu_request_says_shutting_down(self, envs):
        # The CPU helper's request killed by close_all is the pool ending, not text detection failing.
        env = envs(modes={"cpu": "hang-on-request"}, request_timeout_s=30.0)
        failed: list[BaseException] = []

        def work():
            try:
                env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
            except BaseException as exc:  # noqa: BLE001 - collected for the assertion
                failed.append(exc)

        worker = threading.Thread(target=work)
        worker.start()
        deadline = time.monotonic() + 20
        while "cpu" not in env.pool._helpers and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.3)  # let the request reach the helper, which then hangs
        env.pool.close_all()
        worker.join(30)
        assert not worker.is_alive()
        assert len(failed) == 1 and isinstance(failed[0], th.TextDetShuttingDownError)
        assert str(failed[0]) == "Text detection is shutting down"

    @pytest.mark.parametrize(("gpu", "device"), [(None, None), ("NVIDIA", "cuda:0")], ids=["cpu", "gpu"])
    def test_a_helper_that_comes_up_after_close_all_is_stopped_not_kept(self, envs, monkeypatch, gpu, device):
        # close_all takes its list of helpers while another thread is still starting one: that helper must not be
        # added to a pool nobody will close again (a WebGPU helper can outlive the app when nothing closes it).
        env = envs()
        real_start = th._start

        def start_then_close(spec, **kwargs):
            helper = real_start(spec, **kwargs)
            env.pool.close_all()
            return helper

        monkeypatch.setattr(th, "_start", start_then_close)
        with pytest.raises(th.TextDetShuttingDownError, match="shutting down"):
            env.pool.detect_boxes(PLANES, gpu=gpu, gpu_device_path=device)
        assert env.pool._helpers == {}
        assert env.procs[0].wait(timeout=10) is not None

    def test_a_request_after_close_all_starts_no_new_helper(self, envs):
        env = envs()
        assert env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        env.pool.close_all()
        with pytest.raises(th.TextDetShuttingDownError, match="shutting down"):
            env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
        with pytest.raises(th.TextDetShuttingDownError, match="shutting down"):
            env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0")
        assert len(env.specs) == 1


@pytest.mark.parametrize("mode", ["selftest-cpu", "crash-on-request"])
def test_a_second_worker_on_one_device_never_acts_on_a_stale_gpu_verdict(envs, monkeypatch, mode):
    """Two workers per GPU is the normal setting; the second must not overtake the first's self-test."""
    env = envs(modes={"webgpu": mode})
    started = threading.Event()
    release = threading.Event()
    real_start = th._start

    def slow_start(spec, **kwargs):
        if spec.backend == "webgpu":
            started.set()
            release.wait(20)
        return real_start(spec, **kwargs)

    monkeypatch.setattr(th, "_start", slow_start)
    results: list[list[int]] = []
    errors: list[BaseException] = []

    def work():
        try:
            results.append(env.pool.detect_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0"))
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    first = threading.Thread(target=work)
    first.start()
    assert started.wait(20)
    second = threading.Thread(target=work)
    second.start()
    time.sleep(0.3)  # the second worker is now asking for this device while the first is still starting its helper
    release.set()
    for thread in (first, second):
        thread.join(60)
    assert errors == [] and results == [ANSWER, ANSWER]
    gpu_starts = [spec for spec in env.specs if spec.backend == "webgpu"]
    assert env.specs[0] is gpu_starts[0] and gpu_starts[0].selftest is True
    if mode == "selftest-cpu":
        # The first's self-test gave the device its CPU verdict: the second never starts a GPU helper.
        assert gpu_starts == [env.specs[0]]
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
    else:
        # The first's self-test passed and its request then failed: the second, asking inside the back-off that
        # failure started, is read on the CPU rather than starting a second GPU helper.
        assert gpu_starts == [env.specs[0]]
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"


def test_a_helper_that_goes_idle_on_every_request_is_not_restarted_forever(envs):
    env = envs(modes={"cpu": "idle-exit-on-request"})
    with pytest.raises(th.TextDetUnavailableError, match="went idle twice in a row"):
        env.pool.detect_boxes(PLANES, gpu=None, gpu_device_path=None)
    assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]


def test_get_textdet_pool_is_one_pool_for_the_process_closed_at_exit(monkeypatch):
    monkeypatch.setattr(th, "_pool", None)
    registered: list = []
    monkeypatch.setattr(th.atexit, "register", lambda func, *args, **kwargs: registered.append(func))
    pool = th.get_textdet_pool()
    assert th.get_textdet_pool() is pool
    assert registered == [pool.close_all]
    # The app's pool sizes its CPU helpers by the saved CPU worker count.
    assert pool._cpu_limit is th._configured_cpu_workers


@pytest.mark.parametrize(("saved", "expected"), [(0, 0), (1, 1), (6, 6)])
def test_the_app_reads_the_saved_cpu_worker_count(monkeypatch, saved, expected):
    from media_preview_generator.web import settings_manager

    monkeypatch.setattr(settings_manager, "peek_settings_manager", lambda: SimpleNamespace(cpu_threads=saved))
    assert th._configured_cpu_workers() == expected


def test_a_saved_count_that_cant_be_read_means_one_cpu_helper(monkeypatch):
    from media_preview_generator.web import settings_manager

    class Broken:
        @property
        def cpu_threads(self):
            raise OSError("settings.json unreadable")

    monkeypatch.setattr(settings_manager, "peek_settings_manager", Broken)
    assert th._configured_cpu_workers() == 1


def test_outside_the_app_the_count_is_one_and_no_settings_are_loaded(monkeypatch):
    # The eval harness uses the process pool with no settings loaded; reading the count mustn't load /config's.
    from media_preview_generator.web import settings_manager

    monkeypatch.setattr(settings_manager, "_settings_manager", None)
    monkeypatch.setattr(settings_manager, "get_settings_manager", lambda *a: pytest.fail("loaded the settings"))
    assert th._configured_cpu_workers() == 1
    assert settings_manager._settings_manager is None


def test_reconciling_the_cpu_helpers_before_any_text_detection_starts_no_pool(monkeypatch):
    monkeypatch.setattr(th, "_pool", None)
    th.reconcile_textdet_cpu_helpers()
    assert th._pool is None


def test_reconciling_the_cpu_helpers_reaches_the_apps_pool(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(th, "_pool", SimpleNamespace(reconcile_cpu_helpers=lambda: calls.append("reconciled")))
    th.reconcile_textdet_cpu_helpers()
    assert calls == ["reconciled"]


class TestProtocol:
    """The wire between helper and parent carries each frame's own boxes (spec §5.4 rows), not just how many."""

    PLANES = np.stack([np.zeros((2, 3), np.uint8), np.full((2, 3), 9, np.uint8)])
    BOXES = [((1, 2, 3, 4), (5, 6, 7, 8)), ()]

    def _helper(self, reply: bytes) -> th._Helper:
        proc = SimpleNamespace(stdout=io.BytesIO(reply), stdin=io.BytesIO(), poll=lambda: None, wait=lambda timeout: 0)
        return th._Helper(proc, io.BytesIO(), 5.0)

    def test_the_helper_writes_each_frames_boxes_and_the_parent_reads_them_back(self, monkeypatch):
        request = json.dumps({"id": 1, "frames": 2, "height": 2, "width": 3}).encode()
        read_fd, write_fd = os.pipe()
        os.write(write_fd, request + b"\n" + self.PLANES.tobytes())
        os.close(write_fd)
        stdin = os.fdopen(read_fd, "rb", buffering=0)
        monkeypatch.setattr(th, "sys", SimpleNamespace(stdin=SimpleNamespace(buffer=stdin)))
        answered: list[np.ndarray] = []
        detector = SimpleNamespace(detect=lambda planes: answered.append(planes) or self.BOXES)
        written = io.BytesIO()

        assert th._serve(detector, written, idle_exit_s=5.0) == 0
        assert np.array_equal(answered[0], self.PLANES)
        assert json.loads(written.getvalue()) == {"id": 1, "boxes": [[[1, 2, 3, 4], [5, 6, 7, 8]], []]}

        helper = self._helper(written.getvalue())
        assert helper.request(self.PLANES) == self.BOXES
        sent = helper.proc.stdin.getvalue()
        assert json.loads(sent.split(b"\n", 1)[0]) == {"id": 1, "frames": 2, "height": 2, "width": 3}

    def _serve_stdin(self, monkeypatch, read_fd: int, idle_exit_s: float) -> tuple[int, bytes]:
        written = io.BytesIO()
        detector = SimpleNamespace(detect=lambda planes: self.BOXES)
        with os.fdopen(read_fd, "rb", buffering=0) as stdin:
            monkeypatch.setattr(th, "sys", SimpleNamespace(stdin=SimpleNamespace(buffer=stdin)))
            code = th._serve(detector, written, idle_exit_s=idle_exit_s)
        return code, written.getvalue()

    def test_the_helper_serves_a_stdin_numbered_past_1024_when_many_files_are_open(self, monkeypatch):
        # select() can't watch a descriptor at or above FD_SETSIZE (1024) and raises ValueError instead.
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft <= 1100:
            if hard != resource.RLIM_INFINITY and hard <= 1100:
                pytest.skip("the open-file limit is too low to number a descriptor past 1024")
            resource.setrlimit(
                resource.RLIMIT_NOFILE, (4096 if hard == resource.RLIM_INFINITY else min(hard, 4096), hard)
            )
        try:
            request = json.dumps({"id": 1, "frames": 2, "height": 2, "width": 3}).encode()
            read_fd, write_fd = os.pipe()
            os.write(write_fd, request + b"\n" + self.PLANES.tobytes())
            os.close(write_fd)
            high_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD, 1024)
            os.close(read_fd)

            code, written = self._serve_stdin(monkeypatch, high_fd, idle_exit_s=5.0)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

        assert high_fd >= 1024
        assert code == 0
        assert json.loads(written) == {"id": 1, "boxes": [[[1, 2, 3, 4], [5, 6, 7, 8]], []]}

    def test_the_helper_exits_idle_when_no_request_arrives_in_time(self, monkeypatch):
        read_fd, write_fd = os.pipe()
        try:
            started = time.monotonic()
            code, written = self._serve_stdin(monkeypatch, read_fd, idle_exit_s=0.2)
            waited = time.monotonic() - started
        finally:
            os.close(write_fd)

        assert code == th.IDLE_EXIT_CODE
        assert written == b""
        assert 0.15 <= waited < 5.0

    def test_the_helper_exits_0_when_the_parent_closes_stdin(self, monkeypatch):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)

        code, written = self._serve_stdin(monkeypatch, read_fd, idle_exit_s=5.0)

        assert code == 0
        assert written == b""

    @pytest.mark.parametrize(
        ("boxes", "why"),
        [
            ([2, 0], "counts, as helpers before positions answered"),
            ([[[1, 2, 3]], []], "three numbers in a box"),
            ([[["left", 2, 3, 4]], []], "a corner that isn't a number"),
            ([[1, 2, 3, 4], []], "one frame's boxes flattened into the frame's own list"),
        ],
    )
    def test_an_answer_that_is_not_four_numbers_per_box_is_refused(self, boxes, why):
        # Taken as rows, any of these would reach rule J as a frame whose boxes it can't read; the request fails
        # instead, which moves the device to the CPU helper (a GPU) or fails the run (the CPU helper).
        helper = self._helper(json.dumps({"id": 1, "boxes": boxes}).encode() + b"\n")
        with pytest.raises(th.HelperError, match="bad answer"):
            helper.request(self.PLANES)

    def test_an_answer_for_the_wrong_number_of_frames_is_refused(self):
        helper = self._helper(json.dumps({"id": 1, "boxes": [[]]}).encode() + b"\n")
        with pytest.raises(th.HelperError, match="bad answer"):
            helper.request(self.PLANES)
