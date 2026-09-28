"""Reading a card's words (``textrec``): the pinned model, rapidocr's line pre- and post-processing, the reading order,
and the reader over a detector's boxes (spec §5.4, "Prose cards")."""

from __future__ import annotations

import hashlib
import os

import numpy as np
import pytest

textrec = pytest.importorskip("media_preview_generator.markers.credits.textrec")
from media_preview_generator.markers.credits import textdet, textdet_helper  # noqa: E402

REC_BYTES = b"rec-model"


class TestModel:
    @pytest.mark.parametrize(
        ("exists", "pin_size", "pin_hash", "error"),
        [
            (False, True, True, "isn't at"),
            (True, False, True, "isn't the expected file"),
            (True, True, False, "isn't the expected file"),
            (True, True, True, None),
        ],
        ids=["missing", "wrong-size", "right-size-wrong-hash", "match"],
    )
    def test_only_the_pinned_file_passes(self, tmp_path, monkeypatch, exists, pin_size, pin_hash, error):
        model = tmp_path / "rec.onnx"
        if exists:
            model.write_bytes(REC_BYTES)
        if pin_size:
            monkeypatch.setattr(textrec, "MODEL_SIZE", len(REC_BYTES))
        if pin_hash:
            monkeypatch.setattr(textrec, "MODEL_SHA256", hashlib.sha256(REC_BYTES).hexdigest())
        if error is None:
            textrec.verify_model(str(model))
        else:
            with pytest.raises(textdet.ModelError, match=error):
                textrec.verify_model(str(model))

    def test_the_helper_looks_for_the_pinned_file_where_the_image_puts_it(self):
        assert textdet_helper.DEFAULT_REC_MODEL_PATH == f"/app/models/{textrec.MODEL_FILE}"

    def test_both_sessions_run_the_same_basic_graph(self, monkeypatch):
        # The extended level fuses each Conv with its activation, which ONNX Runtime's WebGPU provider can't build:
        # the GPU and the CPU must run the same graph for the self-test to compare like with like.
        opts = textrec._options(2)
        assert opts.graph_optimization_level == textrec.ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        assert opts.intra_op_num_threads == 2


class TestPreprocess:
    def test_a_line_is_48_px_tall_at_least_320_wide_normalised_and_padded(self):
        line = np.full((24, 60, 3), 255, np.uint8)  # ratio 2.5: 120 px wide at 48 tall, padded to 320
        tensor = textrec.preprocess(line)
        assert tensor.shape == (1, 3, 48, 320)
        assert np.allclose(tensor[0, :, :, :120], 1.0) and np.allclose(tensor[0, :, :, 120:], 0.0)

    def test_a_long_line_keeps_its_ratio(self):
        tensor = textrec.preprocess(np.zeros((20, 400, 3), np.uint8))
        assert tensor.shape == (1, 3, 48, 960)
        assert np.allclose(tensor, -1.0)  # black is -1 once normalised

    def test_a_crop_with_no_area_is_not_read(self):
        assert textrec.preprocess(np.zeros((1, 50, 3), np.uint8)) is None


class TestCtcDecode:
    def test_repeats_and_blanks_are_dropped_and_the_confidence_is_the_kept_steps_mean(self):
        chars = ["blank", "A", "B", " "]
        steps = [(1, 0.9), (1, 0.8), (0, 0.99), (1, 0.7), (2, 0.6), (3, 0.5), (0, 0.9)]
        pred = np.zeros((len(steps), 4), np.float32)
        for i, (index, prob) in enumerate(steps):
            pred[i, index] = prob
        text, confidence = textrec.ctc_decode(pred, chars)
        assert text == "AAB "
        assert confidence == pytest.approx(np.mean([0.9, 0.7, 0.6, 0.5]))

    def test_all_blank_is_nothing(self):
        pred = np.zeros((5, 3), np.float32)
        pred[:, 0] = 1.0
        assert textrec.ctc_decode(pred, ["blank", "A", " "]) == ("", 0.0)

    def test_the_classes_are_blank_the_models_characters_and_a_space(self):
        class Meta:
            custom_metadata_map = {"character": "a\nb\nc"}

        class Session:
            def get_modelmeta(self):
                return Meta()

        assert textrec.characters(Session()) == ["blank", "a", "b", "c", " "]


def quad(left, top, right, bottom):
    return np.array([[left, top], [right, top], [right, bottom], [left, bottom]], np.float32)


def test_boxes_are_read_top_to_bottom_then_left_to_right():
    quads = np.array([quad(200, 100, 300, 120), quad(10, 10, 100, 30), quad(10, 102, 150, 121)])
    assert textrec.reading_order(quads) == [1, 2, 0]


class FakeDetector:
    def __init__(self, quads):
        self.quads, self.images = quads, []

    def boxes(self, image):
        self.images.append(image)
        return self.quads


class FakeRecSession:
    """One line's classes: the crop's width decides the text, so each box reads apart."""

    class _Input:
        name = "x"

    class _Meta:
        custom_metadata_map = {"character": "\n".join("ABCDEFGHIJKLMNOPQRSTUVWXYZ.")}

    def get_inputs(self):
        return [self._Input()]

    def get_modelmeta(self):
        return self._Meta()

    def run(self, _outputs, feed):
        width = feed["x"].shape[3]
        steps = np.zeros((1, 3, 29), np.float32)
        letter = 1 + (width // 16) % 26
        sure = 0.95 if width != 336 else 0.85  # the crop 336 px wide reads as another script's text does
        steps[0, 0, letter] = sure
        steps[0, 1, 0] = 0.95
        steps[0, 2, 27] = sure
        return [steps]


def test_the_reader_reads_each_box_of_each_plane_and_leaves_out_unsure_ones():
    quads = np.array([quad(10, 10, 200, 30), quad(10, 50, 110, 70), quad(10, 90, 150, 110)])
    detector = FakeDetector(quads)
    reader = textrec.TextReader(detector, FakeRecSession())
    planes = np.zeros((2, 180, 320), np.uint8)
    read = reader.read(planes)
    assert len(read) == 2 and read[0] == read[1]
    assert len(read[0]) == 2  # the box read with low confidence is left out
    assert all(line.endswith(".") for line in read[0])
    # Luma fed as three equal channels, as detection is.
    assert detector.images[0].shape == (180, 320, 3)


@pytest.mark.integration
def test_the_real_model_reads_a_rendered_sentence():
    det = os.environ.get(textdet_helper.MODEL_ENV, textdet_helper.DEFAULT_MODEL_PATH)
    rec = os.environ.get(textdet_helper.REC_MODEL_ENV, textdet_helper.DEFAULT_REC_MODEL_PATH)
    if not (os.path.isfile(det) and os.path.isfile(rec)):
        pytest.skip("no text models (set MEDIA_PREVIEW_TEXTDET_MODEL and MEDIA_PREVIEW_TEXTREC_MODEL)")
    cv2 = pytest.importorskip("cv2")
    plane = np.full((720, 1280), 16, np.uint8)
    cv2.putText(plane, "The investigation is now closed.", (160, 360), cv2.FONT_HERSHEY_SIMPLEX, 1.6, 235, 3,
                cv2.LINE_AA)  # fmt: skip
    detector = textdet.TextDetector(textdet.cpu_session(det), backend="cpu")
    reader = textrec.TextReader(detector, textrec.cpu_session(rec, 2))
    [lines] = reader.read(plane[None])
    assert lines == ["The investigation is now closed."]
