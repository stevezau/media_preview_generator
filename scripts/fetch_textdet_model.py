#!/usr/bin/env python3
"""Fetch the credit text models for the Docker image (builder stage; stdlib only).

Downloads the pinned rapidocr_onnxruntime 1.4.4 wheel from PyPI, checks its sha256, takes the PP-OCRv4 detection model
out of it, checks that too, and writes it to --out. Then downloads PP-OCRv5's Latin recognition model from RapidOCR's
model repository at a pinned tag and checks its sha256 (spec §5.4, "A prose card"). Any mismatch fails the build.

    python3 scripts/fetch_textdet_model.py --out /models
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

WHEEL_URL = (
    "https://files.pythonhosted.org/packages/ba/12/1e5497183bdbe782dbb91bad1d0d2297dba4d2831b2652657f7517bfc6df/"
    "rapidocr_onnxruntime-1.4.4-py3-none-any.whl"
)
WHEEL_SHA256 = "971d7d5f223a7a808662229df1ef69893809d8457d834e6373d3854bc1782cbf"
MODEL_MEMBER = "rapidocr_onnxruntime/models/ch_PP-OCRv4_det_infer.onnx"
MODEL_SHA256 = "d2a7720d45a54257208b1e13e36a8479894cb74155a5efe29462512d42f49da9"
MODEL_SIZE = 4_745_517
# RapidOCR's ONNX export of PaddleOCR's PP-OCRv5 Latin recognition model (both Apache-2.0), at the repository's v3.9.2
# tag: the character list is stored in the model, so it is the only file needed.
REC_URL = (
    "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2/onnx/PP-OCRv5/rec/latin_PP-OCRv5_rec_mobile.onnx"
)
REC_FILE = "latin_PP-OCRv5_rec_mobile.onnx"
REC_SHA256 = "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a"
REC_SIZE = 7_904_513


def _download(url: str) -> bytes:
    """Download ``url``, retrying transient network errors with backoff.

    Args:
        url: HTTPS URL to fetch.

    Returns:
        The response body.

    Raises:
        OSError: If all attempts fail.
    """
    for attempt in range(1, 6):
        try:
            # The only callers pass the module-level WHEEL_URL and REC_URL constants (pinned https), never user
            # input, and each response is sha256-checked before anything is written to disk.
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310  # nosec B310
                return response.read()
        except OSError:
            if attempt == 5:
                raise
            time.sleep(3 * attempt)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    """Fetch, verify, and write both credit text models.

    Args:
        argv: Command-line arguments (excluding the program name); defaults to ``sys.argv[1:]``.

    Returns:
        0 on success.

    Raises:
        SystemExit: If the wheel, the detection model or the recognition model fails its sha256 check.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    out = Path(parser.parse_args(argv).out)
    wheel = _download(WHEEL_URL)
    if hashlib.sha256(wheel).hexdigest() != WHEEL_SHA256:
        sys.exit("rapidocr_onnxruntime wheel sha256 mismatch")
    model = zipfile.ZipFile(io.BytesIO(wheel)).read(MODEL_MEMBER)
    if len(model) != MODEL_SIZE or hashlib.sha256(model).hexdigest() != MODEL_SHA256:
        sys.exit("text detection model sha256 mismatch")
    rec = _download(REC_URL)
    if len(rec) != REC_SIZE or hashlib.sha256(rec).hexdigest() != REC_SHA256:
        sys.exit("text recognition model sha256 mismatch")
    out.mkdir(parents=True, exist_ok=True)
    (out / MODEL_MEMBER.rsplit("/", 1)[1]).write_bytes(model)
    (out / REC_FILE).write_bytes(rec)
    print(f"text detection model: {len(model)} bytes, sha256 {MODEL_SHA256}")
    print(f"text recognition model: {len(rec)} bytes, sha256 {REC_SHA256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
