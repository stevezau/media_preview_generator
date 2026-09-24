"""Shared fixtures for Intro & Credits tests."""

import logging

import pytest
from loguru import logger


@pytest.fixture(autouse=True)
def _no_background_fingerprint_sweep(monkeypatch):
    """Jobs these tests run start no fingerprint cache sweep thread; the tests of the sweep put it back."""
    from media_preview_generator.markers import job_runner

    monkeypatch.setattr(job_runner, "start_fingerprint_sweep", lambda store, **_kw: False)


@pytest.fixture
def app(tmp_path):
    """Same app fixture as tests/test_routes.py (setup complete, fixed API token)."""
    import json
    import os
    from unittest.mock import patch

    from media_preview_generator.web import settings_manager as sm_mod
    from media_preview_generator.web.app import create_app

    sm_mod.reset_settings_manager()
    (tmp_path / "settings.json").write_text(json.dumps({"setup_complete": True}))
    with patch.dict(os.environ, {"CONFIG_DIR": str(tmp_path), "WEB_AUTH_TOKEN": "test-token-12345678"}):
        flask_app = create_app(config_dir=str(tmp_path))
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        yield flask_app
    sm_mod.reset_settings_manager()


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
    return c


def api_headers():
    return {"Authorization": "Bearer test-token-12345678", "Content-Type": "application/json"}


@pytest.fixture
def loguru_caplog(caplog):
    """Forward loguru records into pytest's caplog so tests can assert on them.

    Same pattern as ``tests/test_version_check.py``'s fixture of the same name — loguru doesn't
    feed stdlib ``logging`` by default, so pytest's ``caplog`` sees nothing without this bridge.
    """

    class _PropagateHandler(logging.Handler):
        def emit(self, record):  # pragma: no cover - handler glue
            logging.getLogger(record.name).handle(record)

    handler_id = logger.add(_PropagateHandler(), level="DEBUG", format="{message}")
    caplog.set_level(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.remove(handler_id)


@pytest.fixture(autouse=True)
def _reset_marker_singletons():
    yield
    # Each singleton is guarded on its own: earlier tasks land before later ones (e.g. store.py
    # ships before sources/ratelimit.py), so one missing module must never skip resetting another.
    try:
        from media_preview_generator.markers.store import reset_marker_store
    except ImportError:
        pass
    else:
        reset_marker_store()
    try:
        from media_preview_generator.markers.sources.ratelimit import reset_limiters
    except ImportError:
        pass
    else:
        reset_limiters()
    try:
        from media_preview_generator.markers.inspect import clear_capability_cache
    except ImportError:
        pass
    else:
        clear_capability_cache()


@pytest.fixture(autouse=True)
def _no_text_detection_check(monkeypatch, request):
    """Jobs these tests build don't start the text detection check (a subprocess); tests of the check itself live in
    tests/markers/credits and patch what they need."""
    if request.node.get_closest_marker("integration"):
        return
    from media_preview_generator.markers import pipeline
    from media_preview_generator.markers.credits.textdet_helper import TextDetState

    monkeypatch.setattr(pipeline, "text_detection_state", lambda: TextDetState.ABSENT)
