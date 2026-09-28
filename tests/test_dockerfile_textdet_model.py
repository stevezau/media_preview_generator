"""The image gets the pinned text detection and recognition models where the app looks for them, verified by sha256
at build."""

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


def test_recognition_pins_match_the_reader_and_the_helper():
    from media_preview_generator.markers.credits import textdet_helper, textrec

    assert (fetch.REC_FILE, fetch.REC_SHA256, fetch.REC_SIZE) == (
        textrec.MODEL_FILE,
        textrec.MODEL_SHA256,
        textrec.MODEL_SIZE,
    )
    assert textdet_helper.DEFAULT_REC_MODEL_PATH == f"/app/models/{textrec.MODEL_FILE}"
    # A tag, not a branch: the file behind the URL can't change under the pin.
    assert fetch.REC_URL.startswith("https://") and "/resolve/v3.9.2/" in fetch.REC_URL
    assert fetch.REC_URL.endswith("/" + fetch.REC_FILE)


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
    assert (
        "COPY --from=model /models/latin_PP-OCRv5_rec_mobile.onnx /app/models/latin_PP-OCRv5_rec_mobile.onnx" in runtime
    )


def test_both_models_licences_ship_with_the_code():
    folder = ROOT / "media_preview_generator/markers/credits"
    for notice in ("PP-OCRv4-det-NOTICE.txt", "PP-OCRv5-rec-NOTICE.txt"):
        assert (folder / notice).is_file(), notice
    assert "Apache License" in (folder / "PP-OCRv5-rec-LICENSE-Apache-2.0.txt").read_text()
    assert fetch.REC_SHA256 in (folder / "PP-OCRv5-rec-NOTICE.txt").read_text()


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


def _serve(monkeypatch, *, broken: str | None = None) -> tuple[bytes, bytes, list[str]]:
    """Pin a fake wheel and recognition model (one of the three hashes wrong when ``broken`` names it) and serve them
    by URL; returns the two models and the URLs asked for."""
    model, rec = b"model-bytes", b"rec-model-bytes"
    wheel = _wheel(model)
    asked: list[str] = []

    def pin(name: str, data: bytes) -> str:
        return "0" * 64 if broken == name else hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(fetch, "WHEEL_SHA256", pin("wheel", wheel))
    monkeypatch.setattr(fetch, "MODEL_SHA256", pin("model", model))
    monkeypatch.setattr(fetch, "MODEL_SIZE", len(model))
    monkeypatch.setattr(fetch, "REC_SHA256", pin("rec", rec))
    monkeypatch.setattr(fetch, "REC_SIZE", len(rec))
    monkeypatch.setattr(
        fetch, "_download", lambda url: asked.append(url) or {fetch.WHEEL_URL: wheel, fetch.REC_URL: rec}[url]
    )
    return model, rec, asked


def test_verified_wheel_and_models_are_written(tmp_path, monkeypatch):
    model, rec, asked = _serve(monkeypatch)
    assert fetch.main(["--out", str(tmp_path)]) == 0
    assert asked == [fetch.WHEEL_URL, fetch.REC_URL]
    assert (tmp_path / "ch_PP-OCRv4_det_infer.onnx").read_bytes() == model
    assert (tmp_path / "latin_PP-OCRv5_rec_mobile.onnx").read_bytes() == rec


@pytest.mark.parametrize("broken", ["wheel", "model", "rec"])
def test_a_hash_mismatch_fails_the_build_and_writes_neither_model(tmp_path, monkeypatch, broken):
    _serve(monkeypatch, broken=broken)
    with pytest.raises(SystemExit, match="sha256"):
        fetch.main(["--out", str(tmp_path)])
    assert not (tmp_path / "ch_PP-OCRv4_det_infer.onnx").exists()
    assert not (tmp_path / "latin_PP-OCRv5_rec_mobile.onnx").exists()
