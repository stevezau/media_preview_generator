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


def _stages() -> dict[str, str]:
    """Each build stage's instructions (comments left out) by stage name (``""`` for the unnamed final stage)."""
    stages = {}
    for block in re.split(r"(?m)^FROM ", (ROOT / "Dockerfile").read_text())[1:]:
        header, _, body = block.partition("\n")
        instructions = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        stages[header.split(" AS ", 1)[1].strip() if " AS " in header else ""] = instructions
    return stages


def test_the_model_is_fetched_in_its_own_stage_and_copied_to_app_models():
    stages = _stages()
    assert "COPY scripts/fetch_textdet_model.py /tmp/fetch_textdet_model.py" in stages["model"]
    assert "RUN python3 /tmp/fetch_textdet_model.py --out /models" in stages["model"]
    # An edit to the fetch script must not invalidate the dependency wheel cache the builder's Layer A keeps.
    assert "fetch_textdet_model" not in stages["toolchain"] + stages["builder"]
    runtime = stages[""]
    assert "COPY --from=model /models/ch_PP-OCRv4_det_infer.onnx /app/models/ch_PP-OCRv4_det_infer.onnx" in runtime


def test_the_version_arg_comes_after_the_dependency_wheels():
    # Every RUN after an ARG sees it as an environment variable, and CI passes a new version on every build, so
    # declared any earlier it would miss the dependency wheel cache (Layer A) every time.
    stages = _stages()
    builder = stages["builder"]
    assert builder.index("pip3 wheel --wheel-dir=/wheels --no-cache-dir . &&") < builder.index(
        "ARG SETUPTOOLS_SCM_PRETEND_VERSION"
    )
    assert "SETUPTOOLS_SCM_PRETEND_VERSION" not in stages["toolchain"] + stages["model"]


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
