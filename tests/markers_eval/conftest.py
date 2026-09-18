import logging

import pytest
from loguru import logger


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
