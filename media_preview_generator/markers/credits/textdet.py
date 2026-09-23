# Pre- and post-processing below are adapted from rapidocr_onnxruntime 1.4.4 (ch_ppocr_det/text_detect.py and
# ch_ppocr_det/utils.py): Copyright (c) 2020 PaddlePaddle Authors, Licensed under the Apache License, Version 2.0
# (http://www.apache.org/licenses/LICENSE-2.0). See PP-OCRv4-det-NOTICE.txt.
"""Text boxes on credit frames: PP-OCRv4's detection model on ONNX Runtime (spec §5.4).

Vendored because rapidocr_onnxruntime 1.4.4 caps Python below 3.13 and is no longer maintained. It must give the same
boxes: ``tests/markers/credits/test_textdet.py`` compares it with a verbatim copy on generated inputs and
``tools/markers_eval/textdet_bench.py`` on the 289-frame bench set. Left out: recognition, angle classification, the
"slow" box score. Shapely's polygon area and length are computed directly (a box is a four-point polygon).

Imported only by the text detection helper process, the harness, the bench and tests: never by the web app.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading

import cv2
import numpy as np
import onnxruntime as ort
import pyclipper

from .rule_j import Box

MODEL_FILE = "ch_PP-OCRv4_det_infer.onnx"
MODEL_SHA256 = "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"
MODEL_SIZE = 4_745_517
FRAME_HEIGHT = 180
FRAME_WIDTH = 320
INTRA_OP_THREADS = 2
MEAN = (0.5, 0.5, 0.5)
STD = (0.5, 0.5, 0.5)
THRESH = 0.3
BOX_THRESH = 0.5
MAX_CANDIDATES = 1000
UNCLIP_RATIO = 1.6
MIN_SIZE = 3
_DILATION_KERNEL = np.array([[1, 1], [1, 1]])
_webgpu_lock = threading.Lock()
_webgpu_registered = False


class TextDetError(Exception):
    """Text detection can't run."""


class ModelError(TextDetError):
    """The model file is missing, unreadable or isn't the pinned file."""


class WebGpuSessionError(TextDetError):
    """ONNX Runtime didn't put the session on the WebGPU device."""


def verify_model(path: str) -> None:
    """Check the model is the pinned file (size, then sha256).

    Raises:
        ModelError: Missing, unreadable, or another file.
    """
    if not os.path.isfile(path):
        raise ModelError(
            f"Needs the text detection model, which the Docker image includes; it isn't at {path}. "
            "Set MEDIA_PREVIEW_TEXTDET_MODEL to point at it."
        )
    try:
        if os.path.getsize(path) != MODEL_SIZE:
            raise ModelError(f"The text detection model at {path} isn't the expected file")
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise ModelError(f"The text detection model at {path} can't be read: {exc}") from exc
    if digest.hexdigest() != MODEL_SHA256:
        raise ModelError(f"The text detection model at {path} isn't the expected file")


def _resize_limit(max_side: int) -> int:
    # rapidocr's TextDetector.get_preprocess: under limit type "max" the configured 320 is replaced by 960, 1500 or
    # 2000, so frames under 960 px keep their own size (spec §5.4's "at the frame's own 320 px").
    if max_side < 960:
        return 960
    return 1500 if max_side < 1500 else 2000


def preprocess(image: np.ndarray) -> np.ndarray | None:
    """rapidocr's DetPreProcess under limit type "max": resize to multiples of 32, normalise, CHW, batch of one.

    Args:
        image: (H, W, 3) uint8.

    Returns:
        (1, 3, H', W') float32, or None when a side would be 0.
    """
    h, w = image.shape[:2]
    limit = _resize_limit(max(h, w))
    ratio = (float(limit) / h if h > w else float(limit) / w) if max(h, w) > limit else 1.0
    resize_h = int(round(int(h * ratio) / 32) * 32)
    resize_w = int(round(int(w * ratio) / 32) * 32)
    if resize_w <= 0 or resize_h <= 0:
        return None
    resized = cv2.resize(image, (resize_w, resize_h))
    normalised = (resized.astype("float32") * (1 / 255.0) - np.array(MEAN)) / np.array(STD)
    return np.expand_dims(normalised.transpose((2, 0, 1)), axis=0).astype(np.float32)


def _mini_box(contour: np.ndarray) -> tuple[np.ndarray, float]:
    bounding_box = cv2.minAreaRect(contour)
    points = sorted(list(cv2.boxPoints(bounding_box)), key=lambda x: x[0])
    first, fourth = (0, 1) if points[1][1] > points[0][1] else (1, 0)
    second, third = (2, 3) if points[3][1] > points[2][1] else (3, 2)
    return np.array([points[first], points[second], points[third], points[fourth]]), min(bounding_box[1])


def _box_score_fast(bitmap: np.ndarray, points: np.ndarray) -> float:
    h, w = bitmap.shape[:2]
    box = points.copy()
    xmin = np.clip(np.floor(box[:, 0].min()).astype(np.int32), 0, w - 1)
    xmax = np.clip(np.ceil(box[:, 0].max()).astype(np.int32), 0, w - 1)
    ymin = np.clip(np.floor(box[:, 1].min()).astype(np.int32), 0, h - 1)
    ymax = np.clip(np.ceil(box[:, 1].max()).astype(np.int32), 0, h - 1)
    mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
    box[:, 0] = box[:, 0] - xmin
    box[:, 1] = box[:, 1] - ymin
    cv2.fillPoly(mask, box.reshape(1, -1, 2).astype(np.int32), 1)
    return cv2.mean(bitmap[ymin : ymax + 1, xmin : xmax + 1], mask)[0]


def _area_and_length(points: np.ndarray) -> tuple[float, float]:
    """A simple polygon's area and perimeter with the same float operations as shapely 2.1 (GEOS): shoelace relative to
    the first x, and summed ``sqrt(dx² + dy²)``. Other orderings (``np.hypot``, the textbook shoelace) differ in the last
    bit on 9–347 of 20,000 boxes, which moves pyclipper's offset."""
    ring = [(float(p[0]), float(p[1])) for p in points]
    ring.append(ring[0])
    x0 = ring[0][0]
    area = 0.0
    for i in range(1, len(ring) - 1):
        area += (ring[i][0] - x0) * (ring[i - 1][1] - ring[i + 1][1])
    length = 0.0
    for i in range(1, len(ring)):
        dx, dy = ring[i][0] - ring[i - 1][0], ring[i][1] - ring[i - 1][1]
        length += math.sqrt(dx * dx + dy * dy)
    return abs(area / 2.0), length


def _unclip(box: np.ndarray) -> np.ndarray:
    area, length = _area_and_length(box)
    offset = pyclipper.PyclipperOffset()
    offset.AddPath(box, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    return np.array(offset.Execute(area * UNCLIP_RATIO / length)).reshape((-1, 1, 2))


def _boxes_from_bitmap(pred: np.ndarray, bitmap: np.ndarray, dest_width: int, dest_height: int) -> np.ndarray:
    height, width = bitmap.shape
    found = cv2.findContours((bitmap * 255).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = found[1] if len(found) == 3 else found[0]
    boxes = []
    for contour in contours[:MAX_CANDIDATES]:
        points, short_side = _mini_box(contour)
        if short_side < MIN_SIZE:
            continue
        if BOX_THRESH > _box_score_fast(pred, points.reshape(-1, 2)):
            continue
        box, short_side = _mini_box(_unclip(points))
        if short_side < MIN_SIZE + 2:
            continue
        box[:, 0] = np.clip(np.round(box[:, 0] / width * dest_width), 0, dest_width)
        box[:, 1] = np.clip(np.round(box[:, 1] / height * dest_height), 0, dest_height)
        boxes.append(box.astype(np.int32))
    return np.array(boxes, dtype=np.int32)


def _order_points_clockwise(pts: np.ndarray) -> np.ndarray:
    x_sorted = pts[np.argsort(pts[:, 0]), :]
    left, right = x_sorted[:2, :], x_sorted[2:, :]
    top_left, bottom_left = left[np.argsort(left[:, 1]), :]
    top_right, bottom_right = right[np.argsort(right[:, 1]), :]
    return np.array([top_left, top_right, bottom_right, bottom_left], dtype="float32")


def _clip(points: np.ndarray, img_height: int, img_width: int) -> np.ndarray:
    for n in range(points.shape[0]):
        points[n, 0] = int(min(max(points[n, 0], 0), img_width - 1))
        points[n, 1] = int(min(max(points[n, 1], 0), img_height - 1))
    return points


def postprocess(pred: np.ndarray, src_hw: tuple[int, int]) -> np.ndarray:
    """rapidocr's DBPostProcess (threshold 0.3, box threshold 0.5, unclip 1.6, dilation, fast score) and its box filter.

    Args:
        pred: The model's probability map, (1, 1, H', W').
        src_hw: The original image's height and width.

    Returns:
        The kept boxes, (n, 4, 2) float32; (0, 4, 2) when none.
    """
    src_h, src_w = src_hw
    prob = pred[:, 0, :, :]
    mask = cv2.dilate(np.array(prob[0] > THRESH).astype(np.uint8), _DILATION_KERNEL)
    kept = []
    for box in _boxes_from_bitmap(prob[0], mask, src_w, src_h):
        box = _clip(_order_points_clockwise(box), src_h, src_w)
        if int(np.linalg.norm(box[0] - box[1])) <= 3 or int(np.linalg.norm(box[0] - box[3])) <= 3:
            continue
        kept.append(box)
    return np.array(kept) if kept else np.zeros((0, 4, 2), dtype=np.float32)


def _session_options(intra_op_threads: int) -> ort.SessionOptions:
    # rapidocr's OrtInferSession options: graph optimisations and arena settings change the numbers at the margin.
    opts = ort.SessionOptions()
    opts.log_severity_level = 4
    opts.enable_cpu_mem_arena = False
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = intra_op_threads
    opts.inter_op_num_threads = 1
    return opts


def cpu_session(model_path: str, intra_op_threads: int = INTRA_OP_THREADS) -> ort.InferenceSession:
    """An ONNX Runtime CPU session of the model (verified first).

    Raises:
        ModelError: The model is missing or not the pinned file.
    """
    verify_model(model_path)
    return ort.InferenceSession(
        model_path,
        sess_options=_session_options(intra_op_threads),
        providers=[("CPUExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})],
    )


def webgpu_devices() -> list:
    """The WebGPU plugin EP's devices (``OrtEpDevice``), or [] when the plugin isn't installed (arm64)."""
    global _webgpu_registered
    try:
        import onnxruntime_ep_webgpu as webgpu_ep
    except ImportError:
        return []
    with _webgpu_lock:
        if not _webgpu_registered:
            ort.register_execution_provider_library("webgpu", webgpu_ep.get_library_path())
            _webgpu_registered = True
    return [d for d in ort.get_ep_devices() if d.ep_name == webgpu_ep.get_ep_name()]


def webgpu_session(model_path: str, device, intra_op_threads: int = INTRA_OP_THREADS) -> ort.InferenceSession:
    """A session of the model on one WebGPU EP device.

    ``device`` selects the execution provider, not the GPU: Dawn runs on the adapter the process's Vulkan environment
    leaves it (``MESA_VK_DEVICE_SELECT`` and the ICD overrides), which the caller sets before this process starts.

    Raises:
        ModelError: The model is missing or not the pinned file.
        WebGpuSessionError: The session came up without the WebGPU provider.
    """
    verify_model(model_path)
    opts = _session_options(intra_op_threads)
    opts.add_provider_for_devices([device], {})
    session = ort.InferenceSession(model_path, sess_options=opts)
    # With no usable adapter (no Vulkan driver, a device id past the first) ONNX Runtime quietly builds a CPU session.
    providers = session.get_providers()
    if device.ep_name not in providers:
        raise WebGpuSessionError(
            f"ONNX Runtime didn't start {device.ep_name} for text detection (providers: {providers})"
        )
    return session


def bounds(quads: np.ndarray) -> tuple[Box, ...]:
    """Each quadrilateral's axis-aligned bounds as whole pixels.

    Args:
        quads: Boxes as (n, 4, 2) corners, from :meth:`TextDetector.boxes`.

    Returns:
        ``(left, top, right, bottom)`` per box, in the frame's own pixels. :func:`postprocess` has already rounded and
        clipped every corner to a whole pixel inside the frame, so nothing is lost here.
    """
    return tuple(
        (int(quad[:, 0].min()), int(quad[:, 1].min()), int(quad[:, 0].max()), int(quad[:, 1].max())) for quad in quads
    )


class TextDetector:
    """Text boxes per frame from one ONNX Runtime session."""

    def __init__(self, session: ort.InferenceSession, *, backend: str) -> None:
        """Wrap a session.

        Args:
            session: From :func:`cpu_session` or :func:`webgpu_session`.
            backend: ``cpu`` or ``webgpu`` (for logs and the helper's ready line).
        """
        self._session = session
        self._input = session.get_inputs()[0].name
        self.backend = backend

    def boxes(self, image: np.ndarray) -> np.ndarray:
        """Text boxes on one (H, W, 3) uint8 image."""
        tensor = preprocess(image)
        if tensor is None:
            return np.zeros((0, 4, 2), dtype=np.float32)
        return postprocess(self._session.run(None, {self._input: tensor})[0], image.shape[:2])

    def detect(self, planes: np.ndarray) -> list[tuple[Box, ...]]:
        """Each frame's text boxes for (n, H, W) uint8 luma planes, fed as three equal channels (as rule J was
        measured). The same model run as :meth:`count`, keeping where the boxes are as well as how many."""
        return [bounds(self.boxes(np.stack([plane] * 3, axis=-1))) for plane in planes]

    def count(self, planes: np.ndarray) -> list[int]:
        """How many text boxes each of (n, H, W) uint8 luma planes holds.

        The GPU self-test compares the boxes themselves, not this (``textdet_helper.self_test``).
        """
        return [len(found) for found in self.detect(planes)]


def synthetic_frames(count: int = 20) -> np.ndarray:
    """Deterministic 320×180 luma frames for the GPU self-test: dark cards with white names, bright gradients with and
    without a caption. No real footage."""
    frames = np.zeros((count, FRAME_HEIGHT, FRAME_WIDTH), dtype=np.uint8)
    for i in range(count):
        frame = frames[i]
        if i % 2 == 0:
            frame[:] = 8
            for line in range(1 + i % 4):
                cv2.putText(frame, f"NAME {i:02d} {line}", (40 + 3 * line, 40 + 30 * line), cv2.FONT_HERSHEY_SIMPLEX,
                            0.6, 235, 1, cv2.LINE_AA)  # fmt: skip
        else:
            frame[:] = np.linspace(60, 200, FRAME_WIDTH, dtype=np.uint8)[None, :]
            if i % 3 == 0:
                cv2.putText(frame, "CAPTION", (100, 160), cv2.FONT_HERSHEY_DUPLEX, 0.7, 20, 2, cv2.LINE_AA)
    return frames
