#!/usr/bin/env python3
"""Fetch the credit text detection model for the Docker image (builder stage; stdlib only).

Downloads the pinned rapidocr_onnxruntime 1.4.4 wheel from PyPI, checks its sha256, takes the PP-OCRv4 detection model
out of it, checks that too, and writes it to --out. Any mismatch fails the build.

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
            # The only caller passes the module-level WHEEL_URL constant (pinned https), never user input,
            # and the response is sha256-checked before anything is written to disk.
            with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310  # nosec B310
                return response.read()
        except OSError:
            if attempt == 5:
                raise
            time.sleep(3 * attempt)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None) -> int:
    """Fetch, verify, and extract the text detection model.

    Args:
        argv: Command-line arguments (excluding the program name); defaults to ``sys.argv[1:]``.

    Returns:
        0 on success.

    Raises:
        SystemExit: If the wheel or the extracted model fails its sha256 check.
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
    out.mkdir(parents=True, exist_ok=True)
    (out / MODEL_MEMBER.rsplit("/", 1)[1]).write_bytes(model)
    print(f"text detection model: {len(model)} bytes, sha256 {MODEL_SHA256}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
