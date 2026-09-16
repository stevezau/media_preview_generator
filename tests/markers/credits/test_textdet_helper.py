"""Text detection helpers: GPU/CPU choice, self-test, crashes, hangs, idle exits, serialisation, availability."""

from __future__ import annotations

import os
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
ANSWER = [0, 9]
NVIDIA_ICD = "/etc/vulkan/icd.d/nvidia_icd.json"
# One address per worker device, so a test can tell two helpers' GPU pins apart.
WORKER_PCI = {"cuda:0": "0000:02:00.0", "cuda:1": "0000:65:00.0", "/dev/dri/renderD128": "0000:00:02.0"}
PIN_ENV = ("DRI_PRIME", "MESA_VK_DEVICE_SELECT", "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE", "NODEVICE_SELECT")


class Env:
    """A pool whose helpers are the fake, with a mode per backend that tests can change between calls."""

    def __init__(self, monkeypatch, *, modes=None, vulkan=HARDWARE, idle_exit_s=600.0, **timeouts):
        self.modes = {"cpu": "ok", "webgpu": "ok", **(modes or {})}
        self.specs: list[th.HelperSpec] = []
        self.procs: list[subprocess.Popen] = []
        self.started: list[dict] = []  # the kwargs each helper's popen really got
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

        def popen(*args, **kwargs):
            self.started.append(kwargs)
            proc = subprocess.Popen(*args, **kwargs)
            self.procs.append(proc)
            return proc

        self.pool = th.TextDetectorPool(
            command=command,
            popen=popen,
            vulkan_info=lambda: vulkan,
            vulkan_env=lambda: {"VK_DRIVER_FILES": NVIDIA_ICD},
            start_timeout_s=timeouts.get("start_timeout_s", 20.0),
            request_timeout_s=timeouts.get("request_timeout_s", 20.0),
        )

    def backends(self):
        return [(s.key, s.backend, s.selftest) for s in self.specs]


@pytest.fixture
def envs(monkeypatch):
    made: list[Env] = []

    def make(**kwargs):
        made.append(Env(monkeypatch, **kwargs))
        return made[-1]

    yield make
    for env in made:
        env.pool.close_all()


class TestRouting:
    def test_a_cpu_worker_uses_the_cpu_helper(self, envs):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]
        assert env.specs[0].pci_bus_id is None
        assert "VK_DRIVER_FILES" not in env.started[0]["env"]
        assert "DRI_PRIME" not in env.started[0]["env"]

    def test_an_nvidia_worker_starts_one_webgpu_helper_with_the_vulkan_env_and_keeps_it(self, envs, loguru_caplog):
        env = envs()
        for _ in range(3):
            assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True)]
        assert env.specs[0].pci_bus_id == "0000:02:00.0"
        started = env.started[0]["env"]
        assert started["VK_DRIVER_FILES"] == NVIDIA_ICD
        assert started["DRI_PRIME"] == "pci-0000_02_00_0"
        assert started["MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE"] == "1"
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert (
            "Credit text detection on cuda:0: GPU (9.0 ms per frame, CPU 18.0 ms), pinned to 0000:02:00.0"
            in loguru_caplog.text
        )

    @pytest.mark.parametrize("gpu", ["INTEL", "AMD"])
    def test_a_vaapi_worker_gets_no_nvidia_overrides(self, envs, gpu):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu=gpu, gpu_device_path="/dev/dri/renderD128") == ANSWER
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
        assert env.pool.count_boxes(PLANES, gpu=gpu, gpu_device_path=path) == ANSWER
        assert env.backends() == [("cpu", "cpu", False)]
        assert env.pool.backend_of(gpu, path) == "cpu"

    @pytest.mark.parametrize(("vulkan", "why"), [(SOFTWARE, "software"), (NO_VULKAN, "no device at all")])
    def test_software_vulkan_never_starts_a_gpu_helper(self, envs, loguru_caplog, vulkan, why):
        env = envs(vulkan=vulkan)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cpu", "cpu", False)], why
        assert "Credit text detection on cuda:0: CPU (Vulkan reports no hardware GPU)" in loguru_caplog.text

    def test_the_verdict_line_names_the_pin_even_when_there_is_no_address(self, envs, monkeypatch, loguru_caplog):
        env = envs()
        monkeypatch.setattr(th, "worker_pci_bus_id", lambda gpu, path: None)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert "pinned to no address (unpinned)" in loguru_caplog.text

    def test_backend_of_answers_before_any_request(self, envs):
        env = envs()
        assert env.pool.backend_of(None, None) == "cpu"  # a CPU worker never has anything else
        assert env.pool.backend_of("NVIDIA", "cuda:0") is None  # not started yet

    def test_two_gpu_workers_get_a_helper_each(self, envs):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:1") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:1", "webgpu", True)]
        assert env.pool.backend_of("NVIDIA", "cuda:1") == "webgpu"
        # Controller note N1: only the environment puts a helper on its own card, so the two must differ.
        assert [k["env"]["DRI_PRIME"] for k in env.started] == ["pci-0000_02_00_0", "pci-0000_65_00_0"]


class TestFallback:
    def test_a_self_test_that_picks_the_cpu_moves_the_device_to_the_cpu_helper(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": "selftest-cpu"})
        for _ in range(2):
            assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.procs[0].wait(timeout=10) is not None  # the GPU helper was closed
        assert "Credit text detection on cuda:0: CPU (the GPU was slower than the CPU)" in loguru_caplog.text

    @pytest.mark.parametrize(
        ("mode", "timeouts"),
        [
            ("crash-on-request", {}),
            ("hang-on-request", {"request_timeout_s": 1.0}),
            # The budget is the pool's, so it also has to cover the healthy CPU helper this cell falls back to.
            ("hang-start", {"start_timeout_s": 4.0}),
            ("bad-ready", {}),
            ("error-reply", {}),
        ],
    )
    def test_a_failing_gpu_helper_hands_the_same_frames_to_the_cpu_for_good(self, envs, loguru_caplog, mode, timeouts):
        env = envs(modes={"webgpu": mode}, **timeouts)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.procs[0].poll() is not None  # killed, not left running
        warnings = [r.getMessage() for r in loguru_caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1 and "cuda:0" in warnings[0] and "CPU" in warnings[0]

    def test_a_failing_cpu_helper_is_unavailable_and_the_next_call_starts_a_new_one(self, envs):
        env = envs(modes={"cpu": "crash-on-request"})
        with pytest.raises(th.TextDetUnavailableError, match="Text detection failed"):
            env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None)
        env.modes["cpu"] = "ok"
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]

    def test_a_gpu_helper_that_exits_between_requests_moves_the_device_to_the_cpu_for_good(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": "crash-after-reply"})
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == 9
        for _ in range(2):
            assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cpu", "cpu", False)]
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
        warnings = [r.getMessage() for r in loguru_caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1 and "exited 9 between requests" in warnings[0] and "CPU" in warnings[0]

    def test_a_cpu_helper_that_went_idle_is_started_again(self, envs, loguru_caplog):
        env = envs(idle_exit_s=0.3)
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_a_cpu_helper_that_exits_between_requests_is_started_again(self, envs):
        env = envs(modes={"cpu": "crash-after-reply"})
        for _ in range(2):
            assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
            env.procs[-1].wait(timeout=10)
        assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]

    def test_an_idle_exit_as_a_request_arrives_starts_the_helper_again_on_the_gpu(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": ["idle-exit-on-request", "ok"]})
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_a_helper_still_leaving_on_its_idle_timer_is_not_taken_for_a_crash(self, envs, loguru_caplog):
        env = envs(modes={"webgpu": ["idle-exit-slow-shutdown", "ok"]})
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_a_helper_close_to_its_idle_exit_is_replaced_before_a_request(self, envs):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        helper = env.pool._helpers["cuda:0"]
        helper.last_used -= th.IDLE_EXIT_S
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
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
                env.pool.count_boxes(big, gpu=None, gpu_device_path=None)
            assert time.monotonic() - started < 1.0 + th.KILL_WAIT_S + th.EXIT_CODE_WAIT_S + 3
            assert env.procs[0].poll() is not None
        finally:
            if pid_file.exists():
                os.kill(int(pid_file.read_text()), signal.SIGKILL)

    def test_an_idle_exit_starts_the_helper_again_without_a_new_self_test(self, envs, loguru_caplog):
        env = envs(idle_exit_s=0.3)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
        assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False)]
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]


def test_requests_for_one_device_never_overlap(envs):
    env = envs()
    results: list[list[int]] = []
    errors: list[BaseException] = []

    def work():
        try:
            for _ in range(15):
                results.append(env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0"))
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
    env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None)
    started = time.monotonic()
    env.pool.close_all()
    assert env.procs[0].poll() is not None and time.monotonic() - started < 10


class TestGpuPinning:
    """Only the helper's environment puts it on a physical GPU (controller note N1, measured 2026-09-16)."""

    def test_an_nvidia_worker_without_a_known_address_is_not_pinned(self, envs, monkeypatch):
        env = envs()
        monkeypatch.setattr(th, "worker_pci_bus_id", lambda gpu, path: None)
        assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
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
        assert env.pool.count_boxes(PLANES, gpu=gpu, gpu_device_path=path) == ANSWER
        started = env.started[0]["env"]
        assert "MESA_VK_DEVICE_SELECT" not in started
        assert "NODEVICE_SELECT" not in started
        assert started["DRI_PRIME"] == f"pci-{WORKER_PCI[path].replace(':', '_').replace('.', '_')}"

    def test_the_cpu_helper_inherits_no_gpu_selection(self, envs, monkeypatch):
        env = envs()
        monkeypatch.setenv("DRI_PRIME", "pci-0000_99_00_0")
        monkeypatch.setenv("MESA_VK_DEVICE_SELECT", "10005:0")
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
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


class FakeDetector:
    def __init__(self, counts, per_frame_s, clock):
        self.counts, self.per_frame_s, self.clock = counts, per_frame_s, clock

    def count(self, frames):
        self.clock.now += self.per_frame_s * len(frames)
        return self.counts[: len(frames)]


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize(
    ("gpu_counts", "gpu_s", "use_gpu", "same"),
    [([1, 0, 3], 0.005, True, True), ([1, 0, 3], 0.030, False, True), ([1, 1, 3], 0.005, False, False)],
)
def test_self_test_needs_the_same_boxes_and_more_speed(gpu_counts, gpu_s, use_gpu, same):
    clock = Clock()
    frames = np.zeros((3, 180, 320), np.uint8)
    result = th.self_test(
        FakeDetector(gpu_counts, gpu_s, clock), FakeDetector([1, 0, 3], 0.018, clock), frames, clock=clock, warmup=1
    )
    assert (result.use_gpu, result.same_boxes) == (use_gpu, same)
    assert result.cpu_ms == pytest.approx(18.0) and result.gpu_ms == pytest.approx(gpu_s * 1000)


def test_the_self_test_warms_both_detectors_before_timing():
    clock = Clock()
    frames = np.zeros((6, 180, 320), np.uint8)
    gpu, cpu = FakeDetector([0] * 6, 0.001, clock), FakeDetector([0] * 6, 0.002, clock)
    seen: list[int] = []
    for detector in (gpu, cpu):
        counted = detector.count
        detector.count = lambda frames, counted=counted: (seen.append(len(frames)), counted(frames))[1]
    th.self_test(gpu, cpu, frames, clock=clock, warmup=2)
    assert seen == [2, 2] + [6, 6] * th.SELFTEST_ROUNDS  # warm-up on both, then one timed pair per round


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

    def count(self, frames):
        cost = self.per_frame_s.pop(0) if len(self.per_frame_s) > 1 else self.per_frame_s[0]
        counts = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        self.clock.now += cost * len(frames)
        return counts[: len(frames)]


@pytest.mark.parametrize(
    ("gpu_ms", "use_gpu"),
    [(8.9, True), (9.0, True), (9.01, False), (9.1, False), (10.0, False), (11.0, False)],
)
def test_the_self_test_needs_a_ten_percent_win_not_just_a_win(gpu_ms, use_gpu):
    assert th.SelfTest(gpu_ms, 10.0, True).use_gpu is use_gpu


def test_the_self_test_reports_the_median_of_three_pairs_not_the_first():
    clock = Clock()
    clock.now = 0.0
    frames = np.zeros((3, 180, 320), np.uint8)
    # The first timed GPU round is a 100 ms outlier; its median is 1 ms, so only a single-sample test would say CPU.
    gpu = PacedDetector([[1, 0, 3]], [0.0, 0.100, 0.001, 0.001], clock)
    cpu = PacedDetector([[1, 0, 3]], [0.0, 0.010], clock)
    result = th.self_test(gpu, cpu, frames, clock=clock, warmup=1)
    assert (result.gpu_ms, result.cpu_ms, result.use_gpu) == (1.0, 10.0, True)


def test_boxes_that_differ_in_any_round_fail_the_self_test():
    clock = Clock()
    clock.now = 0.0
    frames = np.zeros((3, 180, 320), np.uint8)
    gpu = PacedDetector([[1, 0, 3], [1, 0, 3], [1, 0, 3], [9, 9, 9]], [0.001], clock)
    cpu = PacedDetector([[1, 0, 3]], [0.010], clock)
    result = th.self_test(gpu, cpu, frames, clock=clock, warmup=1)
    assert result.same_boxes is False and result.use_gpu is False


class StubDetector:
    """A stand-in :class:`textdet.TextDetector` that costs a fixed time per frame on a fake clock."""

    def __init__(self, counts, ms_per_frame, clock, *, backend, fail_after=None):
        self.counts, self.ms_per_frame, self.clock = counts, ms_per_frame, clock
        self.backend, self._fail_after, self._calls = backend, fail_after, 0

    def count(self, frames):
        self._calls += 1
        if self._fail_after is not None and self._calls > self._fail_after:
            raise RuntimeError("the device was lost")
        self.clock.now += self.ms_per_frame * len(frames) / 1000.0
        return self.counts[: len(frames)]


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
        webgpu_error=None,
        cpu_error=None,
        gpu_fail_after=None,
    ):
        self.clock = clock
        self.devices = [SimpleNamespace(device=SimpleNamespace(metadata={"pci_bus_id": pci})) for pci in addresses]
        self.gpu_ms, self.cpu_ms = gpu_ms, cpu_ms
        self.gpu_counts = gpu_counts if gpu_counts is not None else list(range(th.SELFTEST_FRAMES))
        self.cpu_counts = list(range(th.SELFTEST_FRAMES))
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
        return StubDetector(self.gpu_counts, self.gpu_ms, self.clock, backend="webgpu", fail_after=self.gpu_fail_after)

    def synthetic_frames(self, count=20):
        return np.zeros((count, 180, 320), np.uint8)


class TestHelperProcessBackendChoice:
    """``_start_detector``: what the helper process serves with, and the reason its ready line gives."""

    @pytest.fixture
    def clock(self, monkeypatch):
        clock = Clock()
        clock.now = 0.0
        monkeypatch.setattr(th, "_perf_counter", clock)
        return clock

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
            "selftest": {"gpu_ms": 1.0, "cpu_ms": 10.0, "same_boxes": True},
            "reason": "",
        }

    def test_a_slower_gpu_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, gpu_ms=20.0, cpu_ms=10.0)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["backend"] == "cpu" and ready["reason"] == "the GPU was slower than the CPU"

    def test_a_gpu_that_only_ties_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, gpu_ms=10.0, cpu_ms=10.0)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["selftest"] == {"gpu_ms": 10.0, "cpu_ms": 10.0, "same_boxes": True}
        assert ready["reason"] == "the GPU was slower than the CPU"

    def test_a_gpu_that_counts_different_boxes_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, gpu_ms=1.0, cpu_ms=10.0, gpu_counts=[99] * th.SELFTEST_FRAMES)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["reason"] == "the GPU was counting different boxes than the CPU"
        assert ready["selftest"]["same_boxes"] is False

    def test_a_self_test_that_fails_part_way_through_serves_from_the_cpu(self, clock):
        stub = StubTextDet(clock, gpu_fail_after=2)
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["selftest"] is None
        assert ready["reason"] == "the WebGPU session failed: RuntimeError: the device was lost"

    def test_a_webgpu_session_error_is_a_cpu_fallback_not_a_crash(self, clock):
        stub = StubTextDet(clock, webgpu_error=StubTextDet.WebGpuSessionError("providers: ['CPUExecutionProvider']"))
        detector, ready = self._start(stub)
        assert detector.backend == "cpu"
        assert ready["backend"] == "cpu"
        assert ready["reason"].startswith("this GPU has no usable WebGPU adapter: ")

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
                env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0")
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
        assert len(failed) == 1 and isinstance(failed[0], th.TextDetUnavailableError)
        assert "shutting down" in str(failed[0])
        assert env.pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
        assert not [r for r in loguru_caplog.records if r.levelname == "WARNING"]

    def test_a_request_after_close_all_starts_no_new_helper(self, envs):
        env = envs()
        assert env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None) == ANSWER
        env.pool.close_all()
        with pytest.raises(th.TextDetUnavailableError, match="shutting down"):
            env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None)
        with pytest.raises(th.TextDetUnavailableError, match="shutting down"):
            env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0")
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
            results.append(env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0"))
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
    assert [spec for spec in env.specs if spec.backend == "webgpu"] == [env.specs[0]]
    assert env.specs[0].selftest is True
    assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"


def test_a_helper_that_restarts_on_the_cpu_says_so(envs, loguru_caplog):
    env = envs(modes={"webgpu": ["ok", "selftest-cpu"]}, idle_exit_s=0.3)
    assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
    assert env.procs[0].wait(timeout=10) == th.IDLE_EXIT_CODE
    assert env.pool.count_boxes(PLANES, gpu="NVIDIA", gpu_device_path="cuda:0") == ANSWER
    assert env.backends() == [("cuda:0", "webgpu", True), ("cuda:0", "webgpu", False), ("cpu", "cpu", False)]
    assert env.pool.backend_of("NVIDIA", "cuda:0") == "cpu"
    warnings = [r.getMessage() for r in loguru_caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "cuda:0 restarted on the CPU" in warnings[0]


def test_a_helper_that_goes_idle_on_every_request_is_not_restarted_forever(envs):
    env = envs(modes={"cpu": "idle-exit-on-request"})
    with pytest.raises(th.TextDetUnavailableError, match="went idle twice in a row"):
        env.pool.count_boxes(PLANES, gpu=None, gpu_device_path=None)
    assert env.backends() == [("cpu", "cpu", False), ("cpu", "cpu", False)]


def test_get_textdet_pool_is_one_pool_for_the_process_closed_at_exit(monkeypatch):
    monkeypatch.setattr(th, "_pool", None)
    registered: list = []
    monkeypatch.setattr(th.atexit, "register", lambda func, *args, **kwargs: registered.append(func))
    pool = th.get_textdet_pool()
    assert th.get_textdet_pool() is pool
    assert registered == [pool.close_all]
