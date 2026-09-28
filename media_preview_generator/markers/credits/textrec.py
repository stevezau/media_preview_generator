# The crop, pre- and post-processing below are adapted from rapidocr_onnxruntime 1.4.4 (ch_ppocr_rec/text_recognize.py,
# ch_ppocr_rec/utils.py and utils.py's get_rotate_crop_image): Copyright (c) 2020 PaddlePaddle Authors, Licensed under
# the Apache License, Version 2.0 (http://www.apache.org/licenses/LICENSE-2.0). See PP-OCRv5-rec-NOTICE.txt.
"""Reading a credits card's words: PP-OCRv5's Latin recognition model on ONNX Runtime (spec §5.4, "A prose card").

Each line the detection model boxes on a full-size frame is cropped, scaled to 48 px tall and read by greedy CTC over
the model's own character list (stored in the model file). Only the credits detector's card reading uses it, on the
few frames at a credits start, in the same helper process and on the same device as the detection model.

Imported only by the text detection helper process, the harness and tests: never by the web app.
"""

from __future__ import annotations

import hashlib
import math
import os

import cv2
import numpy as np
import onnxruntime as ort

from .textdet import ModelError, TextDetector, WebGpuSessionError, _session_options

MODEL_FILE = "latin_PP-OCRv5_rec_mobile.onnx"
MODEL_SHA256 = "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a"
MODEL_SIZE = 7_904_513
LINE_HEIGHT = 48
MIN_LINE_WIDTH = 320
# A line read with a lower mean confidence than this is left out: text in a script the Latin model can't read comes out
# as words at 0.5-0.8 (777 Charlie's Kannada crawl read as lower-case "prose"), while Latin lines, even cut short by
# their boxes, read at 0.95 or more.
MIN_CONFIDENCE = 0.9


def _options(intra_op_threads: int) -> ort.SessionOptions:
    # Basic graph optimisations on every device, so both run the same graph: the extended level fuses each Conv with
    # its activation, a fused kernel ONNX Runtime's WebGPU provider can't build (EP_FAIL in conv.h on the P5000).
    opts = _session_options(intra_op_threads)
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    return opts


def verify_model(path: str) -> None:
    """Check the recognition model is the pinned file (size, then sha256).

    Raises:
        ModelError: Missing, unreadable, or another file.
    """
    if not os.path.isfile(path):
        raise ModelError(f"Needs the text recognition model, which the Docker image includes; it isn't at {path}")
    try:
        if os.path.getsize(path) != MODEL_SIZE:
            raise ModelError(f"The text recognition model at {path} isn't the expected file")
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
    except OSError as exc:
        raise ModelError(f"The text recognition model at {path} can't be read: {exc}") from exc
    if digest.hexdigest() != MODEL_SHA256:
        raise ModelError(f"The text recognition model at {path} isn't the expected file")


def cpu_session(model_path: str, intra_op_threads: int) -> ort.InferenceSession:
    """An ONNX Runtime CPU session of the recognition model (verified first).

    Raises:
        ModelError: The model is missing or not the pinned file.
    """
    verify_model(model_path)
    return ort.InferenceSession(
        model_path,
        sess_options=_options(intra_op_threads),
        providers=[("CPUExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})],
    )


def webgpu_session(model_path: str, device, intra_op_threads: int) -> ort.InferenceSession:
    """A session of the recognition model on the WebGPU EP device the detection model runs on.

    Raises:
        ModelError: The model is missing or not the pinned file.
        WebGpuSessionError: The session came up without the WebGPU provider.
    """
    verify_model(model_path)
    opts = _options(intra_op_threads)
    opts.add_provider_for_devices([device], {})
    session = ort.InferenceSession(model_path, sess_options=opts)
    providers = session.get_providers()
    if device.ep_name not in providers:
        raise WebGpuSessionError(
            f"ONNX Runtime didn't start {device.ep_name} for text recognition (providers: {providers})"
        )
    return session


def characters(session: ort.InferenceSession) -> list[str]:
    """The model's CTC classes: blank, its stored character list, then a space (rapidocr's ``use_space_char``)."""
    return ["blank", *session.get_modelmeta().custom_metadata_map["character"].splitlines(), " "]


def crop(image: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """rapidocr's ``get_rotate_crop_image``: the quadrilateral straightened, turned upright when it is tall."""
    points = quad.astype(np.float32)
    width = int(max(np.linalg.norm(points[0] - points[1]), np.linalg.norm(points[2] - points[3])))
    height = int(max(np.linalg.norm(points[0] - points[3]), np.linalg.norm(points[1] - points[2])))
    target = np.float32([[0, 0], [width, 0], [width, height], [0, height]])
    out = cv2.warpPerspective(
        image,
        cv2.getPerspectiveTransform(points, target),
        (width, height),
        borderMode=cv2.BORDER_REPLICATE,
        flags=cv2.INTER_CUBIC,
    )
    if out.shape[0] * 1.0 / max(out.shape[1], 1) >= 1.5:
        out = np.rot90(out)
    return out


def preprocess(line: np.ndarray) -> np.ndarray | None:
    """rapidocr's ``resize_norm_img`` for one line: 48 px tall, at least 320 px wide, normalised, padded.

    Returns:
        (1, 3, 48, W) float32, or None for a crop with no area.
    """
    h, w = line.shape[:2]
    if h < 2 or w < 2:
        return None
    ratio = w / float(h)
    width = max(MIN_LINE_WIDTH, int(LINE_HEIGHT * ratio))
    resized_w = min(width, int(math.ceil(LINE_HEIGHT * ratio)))
    resized = cv2.resize(line, (resized_w, LINE_HEIGHT)).astype("float32").transpose((2, 0, 1)) / 255
    padded = np.zeros((3, LINE_HEIGHT, width), dtype=np.float32)
    padded[:, :, :resized_w] = (resized - 0.5) / 0.5
    return padded[np.newaxis]


def ctc_decode(pred: np.ndarray, chars: list[str]) -> tuple[str, float]:
    """rapidocr's ``CTCLabelDecode``: the best class per step, repeats and blanks dropped, and the mean confidence."""
    index, prob = pred.argmax(axis=1), pred.max(axis=1)
    keep = np.ones(len(index), dtype=bool)
    keep[1:] = index[1:] != index[:-1]
    keep &= index != 0
    text = "".join(chars[i] for i in index[keep] if i < len(chars))
    return text, float(prob[keep].mean()) if keep.any() else 0.0


def reading_order(quads: np.ndarray) -> list[int]:
    """The boxes top to bottom, then left to right (indices), as the research read them (M2)."""
    return sorted(
        range(len(quads)), key=lambda i: (round(float(quads[i][:, 1].mean()) / 8), float(quads[i][:, 0].min()))
    )


class TextReader:
    """A card's lines of text from one detection session and one recognition session on the same device."""

    def __init__(self, detector: TextDetector, session: ort.InferenceSession) -> None:
        """Wrap the sessions.

        Args:
            detector: The text detector (boxes at the frame's own size).
            session: From :func:`cpu_session` or :func:`webgpu_session`.
        """
        self._detector = detector
        self._session = session
        self._input = session.get_inputs()[0].name
        self._chars = characters(session)

    def read_line(self, image: np.ndarray) -> tuple[str, float]:
        """One cropped line's text and mean confidence (image (h, w, 3) uint8)."""
        tensor = preprocess(image)
        if tensor is None:
            return "", 0.0
        return ctc_decode(self._session.run(None, {self._input: tensor})[0][0], self._chars)

    def read(self, planes: np.ndarray) -> list[list[str]]:
        """Each plane's text, one entry per box the detection model finds, top to bottom, for (n, H, W) uint8 luma
        planes fed as three equal channels. A box read below ``MIN_CONFIDENCE`` or as nothing is left out."""
        cards = []
        for plane in planes:
            image = np.stack([plane] * 3, axis=-1)
            quads = self._detector.boxes(image)
            read = (self.read_line(crop(image, quads[i])) for i in reading_order(quads))
            cards.append([text for text, conf in read if text.strip() and conf >= MIN_CONFIDENCE])
        return cards
