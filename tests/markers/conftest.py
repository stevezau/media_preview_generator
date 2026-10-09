"""Shared fixtures for Intro & Credits tests."""

import logging
import os

import pytest
from loguru import logger

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.inspect import clear_capability_cache
from media_preview_generator.markers.models import FileIdentity
from media_preview_generator.markers.publishers import plex_db
from media_preview_generator.markers.publishers.plex_db import LocalPlexDb
from media_preview_generator.markers.sources.ratelimit import reset_limiters
from media_preview_generator.markers.store import MarkerStore, get_marker_store, reset_marker_store
from tests.markers.fakes import EDITOR_DURATION_MS, editor_server


@pytest.fixture(autouse=True)
def _no_background_fingerprint_sweep(monkeypatch):
    """Jobs these tests run start no fingerprint cache sweep thread; the tests of the sweep put it back."""
    from media_preview_generator.markers import job_runner

    monkeypatch.setattr(job_runner, "start_fingerprint_sweep", lambda store, **_kw: False)


@pytest.fixture(autouse=True)
def _fresh_title_cache():
    """Each test starts in a process that has named no film yet (``titles.TITLE_CACHE``)."""
    from media_preview_generator.markers.titles import TITLE_CACHE

    TITLE_CACHE.clear()
    yield
    TITLE_CACHE.clear()


@pytest.fixture(autouse=True)
def _fresh_gpu_decode_checks(monkeypatch):
    """Each test starts with the process's GPU decode checks unrun, on a fake decoder instead of ffmpeg where every
    device decodes like the CPU (the check only logs; it moves nothing). Tests of the check install their own
    (``decode_check._checks``)."""
    from media_preview_generator.markers.credits import decode_check

    def like_the_cpu(ffmpeg, clip, **_kwargs):
        return ((0.0, clip.name),)

    monkeypatch.setattr(decode_check, "_checks", decode_check.DecodeChecks(decode=like_the_cpu))


@pytest.fixture(autouse=True)
def _readable_video_unknown(monkeypatch):
    """A credits tail that gave no frame isn't measured with a real ffprobe (``frames.readable_video_s`` can't tell),
    since faked decodes give no frames for "nothing found". Tests of files cut short install their own answer, and the
    tests of the function itself import it before this runs."""
    from media_preview_generator.markers.credits import frames

    monkeypatch.setattr(frames, "readable_video_s", lambda *args, **kwargs: None)


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Same app fixture as tests/test_routes.py (setup complete, fixed API token)."""
    import json

    from media_preview_generator.web import settings_manager as sm_mod
    from media_preview_generator.web.app import create_app

    sm_mod.reset_settings_manager()
    (tmp_path / "settings.json").write_text(json.dumps({"setup_complete": True}))
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
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
def store(tmp_path):
    s = MarkerStore(str(tmp_path / "markers.db"))
    yield s
    s.close()


@pytest.fixture
def sql_log(monkeypatch):
    """Every statement the Plex publisher runs, with bound values expanded."""
    log: list[str] = []
    original = LocalPlexDb._connect

    def connect(self, *, read_only, **kwargs):
        conn = original(self, read_only=read_only, **kwargs)
        conn.set_trace_callback(log.append)
        return conn

    monkeypatch.setattr(LocalPlexDb, "_connect", connect)
    return log


@pytest.fixture
def plex_holds_the_database(monkeypatch):
    """Stand in for Plex having its DB open, which every write proves before it opens the file. Not autouse."""
    monkeypatch.setattr(plex_db, "shm_lock_held_elsewhere", lambda _db, **_kw: True)


class _CreatedJob:
    id = "job-123"

    def __init__(self, kwargs):
        self.kwargs = kwargs

    def to_dict(self):
        return {"id": self.id, "kind": "intro_credits", "priority": self.kwargs["priority"]}


@pytest.fixture
def created(monkeypatch):
    """The keyword arguments of every ``triggers.create_intro_credits_job`` call; no real job is created."""
    from media_preview_generator.markers import triggers

    calls = []
    monkeypatch.setattr(triggers, "create_intro_credits_job", lambda **kw: calls.append(kw) or _CreatedJob(kw))
    return calls


@pytest.fixture
def media(tmp_path):
    """A ``media/tv/Show/S01E01.mkv`` tree, plus a file outside it."""
    root = tmp_path.resolve() / "media"
    (root / "tv" / "Show").mkdir(parents=True)
    (root / "tv" / "Show" / "S01E01.mkv").write_bytes(b"x" * 100)
    (tmp_path / "outside.mkv").write_bytes(b"x")
    return root


@pytest.fixture
def episode(media):
    return str(media / "tv" / "Show" / "S01E01.mkv")


@pytest.fixture
def servers(app, media):
    """Plex, Jellyfin and Emby over ``media/tv``, Intro & Credits on for all three."""
    from media_preview_generator.web.settings_manager import get_settings_manager

    entries = [
        editor_server("plex-1", "plex", media),
        editor_server("jf-1", "jellyfin", media),
        editor_server("emby-1", "emby", media),
    ]
    get_settings_manager().set("media_servers", entries)
    return entries


@pytest.fixture
def known(app, episode):
    """The episode, already in the marker store."""
    st = os.stat(episode)
    return get_marker_store().upsert_file(
        FileIdentity(episode, st.st_size, st.st_mtime_ns),
        duration_ms=EDITOR_DURATION_MS,
        season_key=None,
        is_movie=False,
    )


@pytest.fixture
def published(monkeypatch):
    """Captures every ``publish_now`` call and answers with the rows a test asks for."""

    class Recorder:
        def __init__(self):
            self.calls = []
            self.rows = []

        def __call__(self, path, **kwargs):
            self.calls.append({"path": path, **kwargs})
            return self.rows

    recorder = Recorder()
    monkeypatch.setattr(pipeline, "publish_now", recorder)
    return recorder


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
    reset_marker_store()
    reset_limiters()
    clear_capability_cache()


@pytest.fixture(autouse=True)
def _no_text_detection_check(monkeypatch, request):
    """Jobs these tests build don't start the text detection check (a subprocess); tests of the check itself live in
    tests/markers/credits and patch what they need."""
    if request.node.get_closest_marker("integration"):
        return
    from media_preview_generator.markers.credits.textdet_helper import TextDetState

    monkeypatch.setattr(pipeline, "text_detection_state", lambda: TextDetState.ABSENT)
