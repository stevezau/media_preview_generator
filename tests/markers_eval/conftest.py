import logging

import pytest
from loguru import logger

from tools.markers_eval import credits_text, decode_cache


@pytest.fixture(autouse=True)
def one_ffmpeg_build(monkeypatch):
    """The harness's caches key on the ffmpeg build; the tests' "/ff" isn't a binary to ask, so this answers for it."""
    for module in (decode_cache, credits_text):
        monkeypatch.setattr(module, "ffmpeg_build", lambda ffmpeg: "ffmpeg version 8.0.1-3ubuntu2")


@pytest.fixture
def loguru_caplog(caplog):
    """Forward loguru records into pytest's caplog (same bridge as ``tests/markers/conftest.py``)."""

    class _PropagateHandler(logging.Handler):
        def emit(self, record):  # pragma: no cover - handler glue
            logging.getLogger(record.name).handle(record)

    handler_id = logger.add(_PropagateHandler(), level="DEBUG", format="{message}")
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.remove(handler_id)
