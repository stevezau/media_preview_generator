"""The vendored detector against a verbatim copy of rapidocr_onnxruntime 1.4.4 on generated inputs, and its wiring."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import types

import cv2
import numpy as np
import onnxruntime as ort
import pytest
from shapely.geometry import Polygon

from media_preview_generator.markers.credits import textdet
from tests.markers.credits import rapidocr_reference as ref
from tools.markers_eval import textdet_bench

SHAPES = ((192, 320), (96, 160), (64, 96))
MODEL_BYTES = b"not a model"


def _probability_maps(count: int, seed: int = 241):
    rng = np.random.default_rng(seed)
    for _ in range(count):
        h, w = SHAPES[int(rng.integers(len(SHAPES)))]
        prob = (rng.random((h, w)) * 0.2).astype(np.float32)
        for _ in range(int(rng.integers(0, 12))):
            center = (float(rng.uniform(0, w)), float(rng.uniform(0, h)))
            size = (float(rng.uniform(1, 60)), float(rng.uniform(1, 20)))
            box = cv2.boxPoints((center, size, float(rng.uniform(-30, 30)))).astype(np.int32)
            cv2.fillPoly(prob, [box], float(rng.uniform(0.25, 0.95)))
        if rng.random() < 0.5:
            prob = cv2.GaussianBlur(prob, (3, 3), 0)
        yield prob[None, None, :, :], (int(rng.integers(90, 400)), int(rng.integers(160, 700)))


def _same_boxes(ours: np.ndarray, theirs: np.ndarray) -> bool:
    # rapidocr returns np.array([]) when nothing is kept; the vendored code always returns (n, 4, 2) float32.
    assert ours.dtype == np.float32 and ours.shape[1:] == (4, 2)
    theirs = np.asarray(theirs).reshape(-1, 4, 2)
    return ours.shape == theirs.shape and np.array_equal(ours, theirs)


def _bridged_blocks():
    # Two blocks joined by a bridge of exactly float32 0.3: the threshold is strict, so they stay two boxes.
    prob = np.zeros((192, 320), np.float32)
    prob[40:70, 20:120] = 0.9
    prob[40:70, 160:260] = 0.9
    prob[50:60, 120:160] = 0.3
    return prob, (192, 320), 2


def _flat_half():
    # A box score of exactly 0.5 is kept, and the box runs to every edge of the image.
    return np.full((192, 320), 0.5, np.float32), (192, 320), 1


def _dot_grid():
    # 1,728 separate dots: only the first 1,000 contours are scored.
    dots = np.zeros((192, 320), np.float32)
    dots[1::6, 1::6] = 0.9
    return cv2.dilate(dots, np.ones((3, 3), np.uint8)), (180, 320), 981


def _ring_with_island():
    # The island sits inside the ring's hole: every contour counts, not only the outer ones.
    prob = np.zeros((192, 320), np.float32)
    prob[20:172, 20:300] = 0.9
    prob[50:142, 60:260] = 0.0
    prob[80:110, 110:210] = 0.9
    return prob, (192, 320), 2


def _smallest_blob():
    # A 3 px square is the smallest contour that passes the minimum size.
    prob = np.zeros((64, 96), np.float32)
    prob[20:23, 30:33] = 0.9
    return prob, (64, 96), 1


def _narrow_at_destination():
    # A 3×4 px block in a map scaled to a 40 px wide image: its box is 3 px wide there, and 3 px is dropped.
    prob = np.zeros((64, 96), np.float32)
    prob[20:23, 30:34] = 0.9
    return prob, (64, 40), 0


def _tilted_score_edge():
    # A tilted box with fractional corners and a fast score of 0.5002: the score window's edges decide it.
    prob = np.zeros((96, 160), np.float32)
    cv2.fillPoly(prob, [np.array([[51, 45], [52, 35], [89, 40], [88, 49]], np.int32)], 0.65)
    return prob, (96, 160), 1


FIXED_MAPS = [_bridged_blocks, _flat_half, _dot_grid, _ring_with_island, _smallest_blob, _narrow_at_destination,
              _tilted_score_edge]  # fmt: skip


class TestAgainstRapidocr:
    @pytest.mark.parametrize(
        "shape",
        [(180, 320, 3), (192, 320, 3), (1080, 1920, 3), (100, 50, 3), (2160, 3840, 3), (540, 960, 3), (960, 540, 3),
         (400, 700, 3), (961, 961, 3), (1500, 1500, 3), (2001, 1000, 3)],
    )  # fmt: skip
    def test_preprocess_is_identical(self, shape):
        image = np.random.default_rng(7).integers(0, 256, shape, dtype=np.uint8)
        ours, theirs = textdet.preprocess(image), ref.reference_preprocess(image)
        assert ours.dtype == theirs.dtype == np.float32
        assert ours.shape == theirs.shape and np.array_equal(ours, theirs)

    def test_a_320x180_frame_is_fed_at_320x192(self):
        assert textdet.preprocess(np.zeros((180, 320, 3), np.uint8)).shape == (1, 3, 192, 320)

    def test_postprocess_gives_identical_boxes_on_2000_generated_maps(self):
        differing = []
        for n, (pred, src_hw) in enumerate(_probability_maps(2000)):
            if not _same_boxes(textdet.postprocess(pred, src_hw), ref.reference_boxes(pred, src_hw)):
                differing.append(n)
        assert differing == []

    @pytest.mark.parametrize("make_map", FIXED_MAPS, ids=[f.__name__.strip("_") for f in FIXED_MAPS])
    def test_fixed_maps_give_rapidocrs_boxes(self, make_map):
        prob, src_hw, expected = make_map()
        pred = prob[None, None]
        ours = textdet.postprocess(pred, src_hw)
        assert _same_boxes(ours, ref.reference_boxes(pred, src_hw))
        assert len(ours) == expected

    def test_a_box_that_unclips_under_5_px_is_dropped(self):
        # Fed straight to the bitmap step (no dilation): this contour passes the size and score checks, but its box
        # unclips to a short side between 4 and 5 px.
        bitmap = np.zeros((32, 32), np.uint8)
        cv2.fillPoly(bitmap, [np.array([[16, 15], [15, 12], [11, 14], [14, 15]], np.int32)], 1)
        pred = bitmap.astype(np.float32)
        [contour] = cv2.findContours(bitmap * 255, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0]
        points, short_side = textdet._mini_box(contour)
        assert short_side >= 3 and textdet._box_score_fast(pred, points.reshape(-1, 2)) >= 0.5
        assert 4 <= textdet._mini_box(textdet._unclip(points))[1] < 5
        post = ref.DBPostProcess(thresh=0.3, box_thresh=0.5, max_candidates=1000, unclip_ratio=1.6, score_mode="fast",
                                 use_dilation=True)  # fmt: skip
        theirs, _ = post.boxes_from_bitmap(pred, bitmap, 32, 32)
        assert textdet._boxes_from_bitmap(pred, bitmap, 32, 32).shape == theirs.shape == (0,)

    def test_area_and_perimeter_equal_shapely(self):
        rng = np.random.default_rng(11)
        for _ in range(20_000):
            box = rng.uniform(0, 400, (4, 2)).astype(np.float32)
            box = cv2.boxPoints(cv2.minAreaRect(box))  # a real rotated rectangle, as the unclip step receives
            polygon = Polygon(box)
            assert textdet._area_and_length(box) == (polygon.area, polygon.length)


class FakeSession:
    def __init__(self, pred: np.ndarray) -> None:
        self.pred = pred
        self.feeds: list[np.ndarray] = []

    def get_inputs(self):
        return [type("Input", (), {"name": "x"})()]

    def run(self, outputs, feed):
        assert outputs is None
        self.feeds.append(feed["x"])
        return [self.pred]


class TestTextDetector:
    def test_count_feeds_each_luma_plane_as_three_equal_channels(self):
        pred = np.zeros((1, 1, 192, 320), np.float32)
        pred[0, 0, 40:60, 30:200] = 0.9
        session = FakeSession(pred)
        detector = textdet.TextDetector(session, backend="cpu")
        planes = np.stack([np.full((180, 320), 10, np.uint8), np.full((180, 320), 200, np.uint8)])
        assert detector.count(planes) == [1, 1]
        assert [f.shape for f in session.feeds] == [(1, 3, 192, 320)] * 2
        first = session.feeds[0][0]
        assert np.array_equal(first[0], first[1]) and np.array_equal(first[1], first[2])
        assert detector.backend == "cpu"

    def test_count_feeds_each_plane_through_preprocess_unchanged(self):
        planes = np.random.default_rng(5).integers(0, 256, (2, 180, 320), dtype=np.uint8)
        session = FakeSession(np.zeros((1, 1, 192, 320), np.float32))
        textdet.TextDetector(session, backend="cpu").count(planes)
        assert len(session.feeds) == 2
        for feed, plane in zip(session.feeds, planes, strict=True):
            assert np.array_equal(feed, textdet.preprocess(np.stack([plane] * 3, axis=-1)))

    def test_boxes_are_scaled_to_the_image_not_the_tensor(self):
        pred = np.zeros((1, 1, 192, 320), np.float32)
        pred[0, 0, 150:170, 30:200] = 0.9
        boxes = textdet.TextDetector(FakeSession(pred), backend="cpu").boxes(np.zeros((180, 320, 3), np.uint8))
        assert _same_boxes(boxes, ref.reference_boxes(pred, (180, 320)))
        assert not np.array_equal(boxes, textdet.postprocess(pred, (192, 320)))

    def test_an_image_too_small_to_resize_gives_no_boxes_without_running_the_model(self):
        session = FakeSession(np.zeros((1, 1, 32, 32), np.float32))
        boxes = textdet.TextDetector(session, backend="cpu").boxes(np.zeros((10, 10, 3), np.uint8))
        assert boxes.shape == (0, 4, 2) and boxes.dtype == np.float32
        assert session.feeds == []

    def test_no_text_gives_zero(self):
        detector = textdet.TextDetector(FakeSession(np.zeros((1, 1, 192, 320), np.float32)), backend="webgpu")
        assert detector.backend == "webgpu"
        assert detector.count(np.zeros((3, 180, 320), np.uint8)) == [0, 0, 0]
        assert detector.detect(np.zeros((3, 180, 320), np.uint8)) == [(), (), ()]

    @pytest.mark.parametrize(
        ("bands", "expected"),
        [
            ([], ()),
            ([(40, 60)], ((16, 24, 214, 69),)),
            # In contour order, which is the model's, not top to bottom: the rows keep whatever order the detector
            # gives, as they keep ffmpeg's own row order.
            ([(40, 60), (90, 110), (140, 160)], ((16, 118, 214, 163), (16, 71, 214, 116), (16, 24, 214, 69))),
        ],
    )
    def test_a_frame_with_no_one_or_several_boxes_gives_each_box_its_place(self, bands, expected):
        # Rule J's own view (how many) and the positions the next rules read come from one model run: the count is
        # the length of the boxes, and each box is its quadrilateral's bounds in the frame's own 320x180 pixels.
        pred = np.zeros((1, 1, 192, 320), np.float32)
        for top, bottom in bands:
            pred[0, 0, top:bottom, 30:200] = 0.9
        detector = textdet.TextDetector(FakeSession(pred), backend="cpu")
        planes = np.zeros((1, 180, 320), np.uint8)
        assert detector.detect(planes) == [expected]
        assert detector.count(planes) == [len(expected)]
        # Python ints, not numpy's: the helper answers these over a JSON pipe, where a numpy int is an error the
        # parent reads as a failed request (and == wouldn't tell the two apart).
        assert json.dumps(detector.detect(planes)[0])
        assert all(type(value) is int for box in detector.detect(planes)[0] for value in box)

    def test_bounds_are_the_corners_own_whole_pixels(self):
        # postprocess has already rounded and clipped every corner, so the bounds lose nothing.
        quads = np.array([[[10.0, 20.0], [60.0, 20.0], [60.0, 41.0], [10.0, 41.0]]], np.float32)
        assert textdet.bounds(quads) == ((10, 20, 60, 41),)
        assert textdet.bounds(np.zeros((0, 4, 2), np.float32)) == ()

    def test_a_box_across_the_whole_frame_ends_at_the_last_pixel(self):
        # rule_j.Box is inclusive corner indices: postprocess clips to width - 1 and height - 1, so a band across the
        # frame reads 319 and 179, never 320 or 180. A rule reading these gets its width as right - left + 1.
        pred = np.zeros((1, 1, 192, 320), np.float32)
        pred[0, 0, 40:60, :] = 0.9
        detector = textdet.TextDetector(FakeSession(pred), backend="cpu")
        [(left, top, right, bottom)] = detector.detect(np.zeros((1, 180, 320), np.uint8))[0]
        assert (left, right) == (0, 319) and right - left + 1 == 320
        assert 0 <= top <= bottom <= 179

    def test_bounds_hold_a_rotated_box_whole(self):
        # A tilted caption's corners aren't axis-aligned; its bounds are the smallest box that holds all four.
        quads = np.array([[[10.0, 25.0], [60.0, 20.0], [62.0, 40.0], [12.0, 45.0]]], np.float32)
        assert textdet.bounds(quads) == ((10, 20, 62, 45),)


class FakeOptions:
    """Stands in for ``ort.SessionOptions``, which rejects a fake device."""

    def __init__(self) -> None:
        self.devices: list[tuple[list, dict]] = []

    def add_provider_for_devices(self, devices: list, options: dict) -> None:
        self.devices.append((devices, options))


def _device(ep_name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(ep_name=ep_name)


@pytest.fixture
def fake_ort(monkeypatch):
    """Records the sessions textdet builds, after a model check that always passes."""
    record = types.SimpleNamespace(verified=[], sessions=[], providers=["CPUExecutionProvider"])

    class Session:
        def __init__(self, path, sess_options=None, providers=None):
            record.sessions.append(types.SimpleNamespace(path=path, options=sess_options, providers=providers))

        def get_providers(self):
            return list(record.providers)

    monkeypatch.setattr(textdet, "verify_model", record.verified.append)
    monkeypatch.setattr(textdet.ort, "InferenceSession", Session)
    return record


def _assert_rapidocr_options(opts, intra_op_threads: int = 2) -> None:
    assert opts.graph_optimization_level == ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    assert (opts.intra_op_num_threads, opts.inter_op_num_threads) == (intra_op_threads, 1)
    assert opts.enable_cpu_mem_arena is False and opts.log_severity_level == 4


class TestSessions:
    def test_cpu_session_verifies_the_model_and_uses_rapidocrs_options(self, fake_ort):
        textdet.cpu_session("/models/det.onnx")
        assert fake_ort.verified == ["/models/det.onnx"]
        [session] = fake_ort.sessions
        assert session.path == "/models/det.onnx"
        assert session.providers == [("CPUExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})]
        _assert_rapidocr_options(session.options)

    def test_cpu_session_takes_the_thread_count(self, fake_ort):
        textdet.cpu_session("/models/det.onnx", intra_op_threads=5)
        _assert_rapidocr_options(fake_ort.sessions[0].options, intra_op_threads=5)

    def test_webgpu_session_adds_the_device_to_rapidocrs_options(self, fake_ort, monkeypatch):
        monkeypatch.setattr(textdet.ort, "SessionOptions", FakeOptions)
        fake_ort.providers = ["WebGpuExecutionProvider", "CPUExecutionProvider"]
        gpu = _device("WebGpuExecutionProvider")
        textdet.webgpu_session("/models/det.onnx", gpu, intra_op_threads=3)
        assert fake_ort.verified == ["/models/det.onnx"]
        [session] = fake_ort.sessions
        assert session.path == "/models/det.onnx" and session.providers is None
        assert session.options.devices == [([gpu], {})]
        _assert_rapidocr_options(session.options, intra_op_threads=3)

    def test_a_webgpu_session_that_came_up_on_the_cpu_is_refused(self, fake_ort, monkeypatch):
        monkeypatch.setattr(textdet.ort, "SessionOptions", FakeOptions)
        fake_ort.providers = ["CPUExecutionProvider"]
        with pytest.raises(textdet.WebGpuSessionError, match="didn't start WebGpuExecutionProvider"):
            textdet.webgpu_session("/models/det.onnx", _device("WebGpuExecutionProvider"))


class TestWebGpuDevices:
    def test_the_plugin_registers_once_and_only_its_devices_are_listed(self, monkeypatch):
        plugin = types.ModuleType("onnxruntime_ep_webgpu")
        plugin.get_library_path = lambda: "/plugins/libonnxruntime_providers_webgpu.so"
        plugin.get_ep_name = lambda: "WebGpuExecutionProvider"
        gpu = _device("WebGpuExecutionProvider")
        registered = []
        monkeypatch.setitem(sys.modules, "onnxruntime_ep_webgpu", plugin)
        monkeypatch.setattr(textdet, "_webgpu_registered", False)
        monkeypatch.setattr(textdet.ort, "register_execution_provider_library", lambda *args: registered.append(args))
        monkeypatch.setattr(textdet.ort, "get_ep_devices", lambda: [_device("CPUExecutionProvider"), gpu,
                                                                     _device("CUDAExecutionProvider")])  # fmt: skip
        assert textdet.webgpu_devices() == [gpu]
        assert textdet.webgpu_devices() == [gpu]
        assert registered == [("webgpu", "/plugins/libonnxruntime_providers_webgpu.so")]

    def test_no_plugin_means_no_devices(self, monkeypatch):
        registered = []
        monkeypatch.setitem(sys.modules, "onnxruntime_ep_webgpu", None)  # makes the import raise ImportError
        monkeypatch.setattr(textdet, "_webgpu_registered", False)
        monkeypatch.setattr(textdet.ort, "register_execution_provider_library", lambda *args: registered.append(args))
        assert textdet.webgpu_devices() == []
        assert registered == []


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
        model = tmp_path / "m.onnx"
        if exists:
            model.write_bytes(MODEL_BYTES)
        if pin_size:
            monkeypatch.setattr(textdet, "MODEL_SIZE", len(MODEL_BYTES))
        if pin_hash:
            monkeypatch.setattr(textdet, "MODEL_SHA256", hashlib.sha256(MODEL_BYTES).hexdigest())
        if error is None:
            textdet.verify_model(str(model))
        else:
            with pytest.raises(textdet.ModelError, match=error):
                textdet.verify_model(str(model))

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read a chmod 000 file")
    def test_an_unreadable_model_is_a_model_error(self, tmp_path, monkeypatch):
        model = tmp_path / "m.onnx"
        model.write_bytes(MODEL_BYTES)
        monkeypatch.setattr(textdet, "MODEL_SIZE", len(MODEL_BYTES))
        model.chmod(0)
        try:
            with pytest.raises(textdet.ModelError, match="can't be read") as caught:
                textdet.verify_model(str(model))
        finally:
            model.chmod(0o600)
        assert isinstance(caught.value.__cause__, PermissionError)


def test_the_self_test_frames_are_the_size_the_decoder_hands_over():
    # textdet can't import frames (it would pull loguru and the probe into the helper process), so the two size
    # constants are kept apart; a self-test timed at another size would measure a workload the app never runs.
    from media_preview_generator.markers.credits import frames

    assert (textdet.FRAME_HEIGHT, textdet.FRAME_WIDTH) == (frames.FRAME_H, frames.FRAME_W)
    assert textdet.synthetic_frames(1).shape[1:] == (frames.FRAME_H, frames.FRAME_W)


def test_synthetic_frames_are_deterministic_and_mixed():
    first, second = textdet.synthetic_frames(20), textdet.synthetic_frames(20)
    assert first.shape == (20, 180, 320) and first.dtype == np.uint8
    assert np.array_equal(first, second)
    means = first.reshape(20, -1).mean(axis=1)
    assert (means < 30).sum() >= 8 and (means >= 30).sum() >= 8


@pytest.mark.parametrize(
    "argv",
    [
        ["extract", "--out", "/data_16tb2/bench"],
        ["counts", "--impl", "vendored", "--model", "m.onnx", "--frames", "f.npy", "--out", "/data/counts.json"],
    ],
)
def test_bench_refuses_an_explicit_output_under_data(argv, monkeypatch):
    def must_not_run(*args, **kwargs):
        raise AssertionError("the bench ran before checking its output path")

    monkeypatch.setattr(textdet_bench, "extract", must_not_run)
    monkeypatch.setattr(textdet_bench, "detect", must_not_run)
    with pytest.raises(SystemExit, match=r"bench output must not be written under /data\*"):
        textdet_bench.main(argv)


def test_bench_gate_fails_when_a_box_moves_but_the_counts_match(tmp_path, capsys):
    def result(name: str, frames: list) -> str:
        path = tmp_path / name
        path.write_text(json.dumps({"impl": name, "frames": len(frames), "counts": [len(f) for f in frames],
                                    "boxes": frames}))  # fmt: skip
        return str(path)

    frames = [[] for _ in range(textdet_bench.EXPECTED_FRAMES)]
    frames[7] = [[[10.0, 20.0], [60.0, 20.0], [60.0, 30.0], [10.0, 30.0]]]
    moved = list(frames)
    moved[7] = [[[11.0, 20.0], [60.0, 20.0], [60.0, 30.0], [11.0, 30.0]]]
    assert textdet_bench.main(["compare", result("a.json", frames), result("same.json", frames)]) == 0
    capsys.readouterr()
    assert textdet_bench.main(["compare", result("a.json", frames), result("moved.json", moved)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "289 of 289 frames identical; differing frames: []",
        "288 of 289 frames with identical box corners; differing frames: [7]",
    ]


def test_bench_gate_fails_when_both_runs_are_identical_but_truncated(tmp_path, capsys):
    """A short extraction must not pass the equivalence gate: it proves nothing about the frames it never saw."""
    frames = [[] for _ in range(5)]
    paths = []
    for name in ("short_a.json", "short_b.json"):
        path = tmp_path / name
        path.write_text(json.dumps({"impl": name, "frames": len(frames), "counts": [0] * len(frames),
                                    "boxes": frames}))  # fmt: skip
        paths.append(str(path))
    assert textdet_bench.main(["compare", *paths]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "5 of 5 frames identical; differing frames: []",
        "5 of 5 frames with identical box corners; differing frames: []",
    ]


@pytest.mark.integration
def test_real_model_finds_text_on_the_dark_synthetic_cards():
    path = os.environ.get("MEDIA_PREVIEW_TEXTDET_MODEL", "/app/models/ch_PP-OCRv4_det_infer.onnx")
    if not os.path.isfile(path):
        pytest.skip("no text detection model (set MEDIA_PREVIEW_TEXTDET_MODEL)")
    detector = textdet.TextDetector(textdet.cpu_session(path), backend="cpu")
    frames = textdet.synthetic_frames(20)
    counts = detector.count(frames)
    dark = [c for c, f in zip(counts, frames, strict=True) if f.mean() < 30]
    assert all(c >= 1 for c in dark)
    # The bright gradient frames: every third one carries a dark "CAPTION", the rest have no text at all.
    captioned = [c for i, c in enumerate(counts) if i % 2 == 1 and i % 3 == 0]
    plain = [c for i, c in enumerate(counts) if i % 2 == 1 and i % 3 != 0]
    assert len(captioned) == 3 and all(c >= 1 for c in captioned)
    assert len(plain) == 7 and all(c == 0 for c in plain)
