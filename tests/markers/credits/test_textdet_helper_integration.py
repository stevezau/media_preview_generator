"""The real helper process with the real model: the protocol round trip gives the in-process detector's counts."""

import os
import shutil

import pytest

from media_preview_generator.markers.credits import textdet_helper as th

pytestmark = pytest.mark.integration


@pytest.fixture
def model(monkeypatch):
    path = os.environ.get(th.MODEL_ENV, th.DEFAULT_MODEL_PATH)
    if not os.path.isfile(path):
        pytest.skip("no text detection model (set MEDIA_PREVIEW_TEXTDET_MODEL)")
    monkeypatch.setenv(th.MODEL_ENV, path)
    return path


def test_the_cpu_helper_counts_what_the_detector_counts(model):
    textdet = pytest.importorskip("media_preview_generator.markers.credits.textdet")

    frames = textdet.synthetic_frames(20)
    expected = textdet.TextDetector(textdet.cpu_session(model), backend="cpu").count(frames)
    pool = th.TextDetectorPool()
    try:
        assert pool.count_boxes(frames, gpu=None, gpu_device_path=None) == expected
    finally:
        pool.close_all()


def test_the_check_passes(model):
    pytest.importorskip("onnxruntime")
    th.forget_text_detection_state()
    try:
        assert th.text_detection_status() == (True, "")
    finally:
        th.forget_text_detection_state()


@pytest.mark.gpu
def test_webgpu_on_this_hosts_nvidia_gpu_matches_the_cpu(model):
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    textdet = pytest.importorskip("media_preview_generator.markers.credits.textdet")

    frames = textdet.synthetic_frames(20)
    expected = textdet.TextDetector(textdet.cpu_session(model), backend="cpu").count(frames)
    pool = th.TextDetectorPool()
    try:
        assert pool.count_boxes(frames, gpu="NVIDIA", gpu_device_path="cuda:0") == expected
        backend = pool.backend_of("NVIDIA", "cuda:0")
        # The self-test may pick the CPU on a busy host (the log line says why); EXPECT_WEBGPU=1 on storage demands
        # the GPU.
        assert backend == "webgpu" if os.environ.get("EXPECT_WEBGPU") == "1" else backend in ("webgpu", "cpu")
    finally:
        pool.close_all()
