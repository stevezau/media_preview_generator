"""The real helper process with the real model: the protocol round trip gives the in-process detector's boxes."""

import json
import os
import shutil
import subprocess
import sys

import pytest

from media_preview_generator.markers.credits import textdet_helper as th

pytestmark = pytest.mark.integration

LAVAPIPE_ICD = "/usr/share/vulkan/icd.d/lvp_icd.json"


@pytest.fixture
def model(monkeypatch):
    path = os.environ.get(th.MODEL_ENV, th.DEFAULT_MODEL_PATH)
    if not os.path.isfile(path):
        pytest.skip("no text detection model (set MEDIA_PREVIEW_TEXTDET_MODEL)")
    monkeypatch.setenv(th.MODEL_ENV, path)
    return path


def test_the_cpu_helper_finds_what_the_detector_finds(model):
    textdet = pytest.importorskip("media_preview_generator.markers.credits.textdet")

    frames = textdet.synthetic_frames(20)
    expected = textdet.TextDetector(textdet.cpu_session(model), backend="cpu").detect(frames)
    pool = th.TextDetectorPool()
    try:
        assert pool.detect_boxes(frames, gpu=None, gpu_device_path=None) == expected
    finally:
        pool.close_all()


def test_the_check_passes(model):
    pytest.importorskip("onnxruntime")
    th.forget_text_detection_state()
    try:
        assert th.text_detection_status() == (True, "")
    finally:
        th.forget_text_detection_state()


def _adapters_in(env: dict[str, str]) -> list:
    """What ``_vulkan_adapters`` lists in a process started with ``env`` (the Vulkan loader reads it once)."""
    code = f"import json, {th.MODULE} as th; print(json.dumps(th._vulkan_adapters()))"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_lavapipe_only_environment_lists_a_software_renderer():
    if not os.path.isfile(LAVAPIPE_ICD):
        pytest.skip("no lavapipe Vulkan driver")
    listed = _adapters_in({**os.environ, "VK_DRIVER_FILES": LAVAPIPE_ICD})
    if listed is None:
        pytest.skip("no Vulkan loader")
    assert listed and all(kind == th.VK_CPU for _name, kind in listed)
    assert "llvmpipe" in th._software_adapter([tuple(entry) for entry in listed])


def _nvidia_pci_bus_id() -> str:
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    from media_preview_generator.markers.credits.devices import worker_pci_bus_id

    pci_bus_id = worker_pci_bus_id("NVIDIA", "cuda:0")
    if pci_bus_id is None:
        pytest.skip("the NVIDIA GPU's PCI address isn't known")
    return pci_bus_id


def _nvidia_helper_env(pci_bus_id: str) -> dict[str, str]:
    """The environment the pool starts an NVIDIA worker's helper with."""
    from media_preview_generator.gpu.vulkan_probe import get_vulkan_env_overrides
    from media_preview_generator.markers.credits.devices import pin_env_to_gpu

    return pin_env_to_gpu({**os.environ, **get_vulkan_env_overrides()}, pci_bus_id)


@pytest.mark.gpu
@pytest.mark.parametrize("software", [False, True], ids=["nvidia-vulkan", "lavapipe-only"])
def test_the_real_helper_serves_from_the_gpu_only_on_a_hardware_adapter(model, software):
    # Dawn takes whichever adapter the helper's Vulkan environment leaves it: with only lavapipe's driver it runs on the
    # CPU's software renderer, which finds the CPU's boxes, so no self-test would ever turn it away.
    pytest.importorskip("onnxruntime_ep_webgpu")
    pci_bus_id = _nvidia_pci_bus_id()
    env = _nvidia_helper_env(pci_bus_id)
    if software:
        if not os.path.isfile(LAVAPIPE_ICD):
            pytest.skip("no lavapipe Vulkan driver")
        env["VK_DRIVER_FILES"] = LAVAPIPE_ICD
    command = [sys.executable, "-m", th.MODULE, "--backend", "webgpu", "--model", model, "--pci-bus-id", pci_bus_id,
               "--no-selftest"]  # fmt: skip
    proc = subprocess.run(command, env=env, input=b"", capture_output=True, timeout=120, check=True)
    ready = json.loads(proc.stdout.splitlines()[0])
    if software:
        assert ready["backend"] == "cpu"
        assert "a software renderer, not on this GPU" in ready["reason"]
        assert "failed" not in ready
    else:
        assert ready == {"ready": True, "backend": "webgpu", "selftest": None, "reason": ""}


@pytest.mark.gpu
def test_webgpu_on_an_nvidia_gpu_matches_the_cpu_and_is_used(model):
    textdet = pytest.importorskip("media_preview_generator.markers.credits.textdet")
    _nvidia_pci_bus_id()
    from media_preview_generator.gpu.vulkan_probe import get_vulkan_device_info

    info = get_vulkan_device_info()
    if info.device is None or info.is_software:
        pytest.skip("Vulkan has no hardware GPU in this environment")

    frames = textdet.synthetic_frames(20)
    expected = textdet.TextDetector(textdet.cpu_session(model), backend="cpu").detect(frames)
    pool = th.TextDetectorPool()
    try:
        assert pool.detect_boxes(frames, gpu="NVIDIA", gpu_device_path="cuda:0") == expected
        # Its speed doesn't decide: a GPU that finds the CPU's boxes is the GPU worker's text detection device.
        assert pool.backend_of("NVIDIA", "cuda:0") == "webgpu"
    finally:
        pool.close_all()
