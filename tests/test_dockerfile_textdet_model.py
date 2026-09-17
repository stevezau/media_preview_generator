"""The image gets the pinned text detection model where the app looks for it, verified by sha256 at build."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_textdet_model", ROOT / "scripts/fetch_textdet_model.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def test_pins_match_the_detector_and_the_helper():
    from media_preview_generator.markers.credits import textdet, textdet_helper

    assert fetch.MODEL_SHA256 == textdet.MODEL_SHA256
    assert fetch.MODEL_SIZE == textdet.MODEL_SIZE
    assert fetch.MODEL_MEMBER.endswith("/" + textdet.MODEL_FILE)
    assert textdet_helper.DEFAULT_MODEL_PATH == f"/app/models/{textdet.MODEL_FILE}"
    assert re.fullmatch(r"[0-9a-f]{64}", fetch.WHEEL_SHA256)


def test_the_dockerfile_fetches_in_the_builder_and_copies_to_app_models():
    text = (ROOT / "Dockerfile").read_text()
    builder, runtime = text.split("# Stage 2: Runtime", 1)
    assert "COPY scripts/fetch_textdet_model.py /tmp/fetch_textdet_model.py" in builder
    assert "RUN python3 /tmp/fetch_textdet_model.py --out /models" in builder
    assert "COPY --from=builder /models/ch_PP-OCRv4_det_infer.onnx /app/models/ch_PP-OCRv4_det_infer.onnx" in runtime


def _wheel(model: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(fetch.MODEL_MEMBER, model)
    return buffer.getvalue()


def test_verified_wheel_and_model_are_written(tmp_path, monkeypatch):
    model = b"model-bytes"
    wheel = _wheel(model)
    monkeypatch.setattr(fetch, "WHEEL_SHA256", hashlib.sha256(wheel).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SHA256", hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SIZE", len(model))
    monkeypatch.setattr(fetch, "_download", lambda url: wheel)
    assert fetch.main(["--out", str(tmp_path)]) == 0
    assert (tmp_path / "ch_PP-OCRv4_det_infer.onnx").read_bytes() == model


@pytest.mark.parametrize("broken", ["wheel", "model"])
def test_a_hash_mismatch_fails_the_build(tmp_path, monkeypatch, broken):
    model = b"model-bytes"
    wheel = _wheel(model)
    monkeypatch.setattr(fetch, "WHEEL_SHA256", "0" * 64 if broken == "wheel" else hashlib.sha256(wheel).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SHA256", "0" * 64 if broken == "model" else hashlib.sha256(model).hexdigest())
    monkeypatch.setattr(fetch, "MODEL_SIZE", len(model))
    monkeypatch.setattr(fetch, "_download", lambda url: wheel)
    with pytest.raises(SystemExit, match="sha256"):
        fetch.main(["--out", str(tmp_path)])
    assert not (tmp_path / "ch_PP-OCRv4_det_infer.onnx").exists()
