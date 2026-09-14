"""Webhook batches start the preview job first, then queue its Intro & Credits follow-up (spec §6.4 item 9)."""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.web import webhooks as wh

SONARR_PATH = "/data/TV Shows/Show (2023)/Season 02/Show - S02E04.mkv"
FOLLOW_UP = "media_preview_generator.markers.triggers.submit_webhook_follow_up"


@pytest.fixture
def calls():
    return []


@pytest.fixture
def captured_overrides(monkeypatch, calls):
    """Patch ``_start_job_async`` and capture every (job_id, overrides) call."""
    captured: list[dict] = []

    def fake_start(job_id, overrides):
        calls.append("preview_started")
        captured.append({"job_id": job_id, "overrides": dict(overrides or {})})

    monkeypatch.setattr("media_preview_generator.web.routes._start_job_async", fake_start)
    return captured


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    import media_preview_generator.web.settings_manager as sm_mod

    sm_mod._settings_manager = None
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    yield
    sm_mod._settings_manager = None


@pytest.fixture(autouse=True)
def _isolate_jobs(tmp_path):
    import media_preview_generator.web.jobs as jobs_mod

    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    yield
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None


def _fire_debounced(source, path, **kwargs):
    captured = []

    def fake_timer(_delay, fn, *positional, args=None, kwargs=None):
        t = MagicMock()
        t.start = lambda: captured.append((fn, list(positional) + list(args or []), kwargs or {}))
        return t

    with (
        patch.object(wh, "_check_and_record_dedup", return_value=None),
        patch("media_preview_generator.web.webhooks.threading.Timer", side_effect=fake_timer),
    ):
        assert wh._schedule_webhook_job(source, "Show S02E04", path, **kwargs) is True
        fn, args, fn_kwargs = captured[-1]
        fn(*args, **fn_kwargs)


def _recording_follow_up(calls, **behaviour):
    def follow_up(**kwargs):
        calls.append("follow_up")
        if "raises" in behaviour:
            raise behaviour["raises"]
        return "ic-1"

    return MagicMock(side_effect=follow_up)


@pytest.mark.parametrize(
    ("source", "server_id"),
    [
        ("sonarr", None),
        ("radarr", None),
        ("sportarr", None),
        ("plex", "plex-1"),  # Plex library.new via /api/webhooks/plex, pinned to the resolved server
        ("custom", None),  # Tdarr / scripts
        ("recently_added", None),
    ],
)
def test_debounced_batch_queues_follow_up_after_preview_job(captured_overrides, calls, source, server_id):
    follow_up = _recording_follow_up(calls)
    with patch(FOLLOW_UP, follow_up):
        _fire_debounced(source, SONARR_PATH, **({"server_id": server_id} if server_id else {}))
    preview = captured_overrides[-1]
    assert calls == ["preview_started", "follow_up"]
    follow_up.assert_called_once_with(
        preview_job_id=preview["job_id"], paths=[SONARR_PATH], source=source, item_id_hints=None
    )
    assert preview["overrides"]["webhook_paths"] == [SONARR_PATH]


@pytest.mark.parametrize("error", [RuntimeError("boom"), ImportError("markers missing")])
def test_debounced_follow_up_failure_never_breaks_preview_job(captured_overrides, calls, loguru_caplog, error):
    with (
        patch(FOLLOW_UP, _recording_follow_up(calls, raises=error)),
        patch.object(wh, "_add_history_entry") as history,
    ):
        _fire_debounced("radarr", "/data/Movies/Foo (2024)/Foo (2024).mkv")
    assert calls == ["preview_started", "follow_up"]
    assert captured_overrides[-1]["overrides"]["source"] == "radarr"
    assert [c.args[3] for c in history.call_args_list] == ["triggered"]
    assert "Could not queue the Intro & Credits job" in loguru_caplog.text


@pytest.mark.parametrize(
    ("source", "hints", "expected_hints"),
    [
        ("jellyfin", {"jf-1": "abc"}, {"/data/Movies/Foo.mkv": {"jf-1": "abc"}}),
        ("emby", {"emby-1": "42"}, {"/data/Movies/Foo.mkv": {"emby-1": "42"}}),
        ("plex", {"plex-1": "9"}, {"/data/Movies/Foo.mkv": {"plex-1": "9"}}),
        ("plex", None, None),
        ("jellyfin", {"jf-1": ""}, None),  # empty ids are dropped before they reach the preview job
    ],
)
def test_vendor_webhook_follow_up_carries_item_id_hints(captured_overrides, calls, source, hints, expected_hints):
    follow_up = _recording_follow_up(calls)
    with patch.object(wh, "_check_and_record_dedup", return_value=None), patch(FOLLOW_UP, follow_up):
        job_id = wh.create_vendor_webhook_job(
            source=source,
            title="Foo",
            canonical_path="/data/Movies/Foo.mkv",
            item_id_by_server=hints,
            server_id=None,
        )
    assert calls == ["preview_started", "follow_up"]
    follow_up.assert_called_once_with(
        preview_job_id=job_id, paths=["/data/Movies/Foo.mkv"], source=source, item_id_hints=expected_hints
    )


def test_vendor_follow_up_uses_the_sanitised_source(captured_overrides, calls):
    follow_up = _recording_follow_up(calls)
    with patch.object(wh, "_check_and_record_dedup", return_value=None), patch(FOLLOW_UP, follow_up):
        wh.create_vendor_webhook_job(source="  JellyFin ", canonical_path="/data/Movies/Foo.mkv", server_id=None)
    assert follow_up.call_args.kwargs["source"] == "jellyfin"


def test_vendor_follow_up_failure_never_breaks_preview_job(captured_overrides, calls, loguru_caplog):
    with (
        patch.object(wh, "_check_and_record_dedup", return_value=None),
        patch(FOLLOW_UP, _recording_follow_up(calls, raises=RuntimeError("boom"))),
    ):
        job_id = wh.create_vendor_webhook_job(source="plex", title="Foo", canonical_path="/data/Movies/Foo.mkv")
    assert job_id is not None
    assert captured_overrides[-1]["job_id"] == job_id
    assert calls == ["preview_started", "follow_up"]
    assert "Could not queue the Intro & Credits job" in loguru_caplog.text


def test_duplicate_vendor_webhook_queues_no_follow_up(captured_overrides, calls):
    follow_up = _recording_follow_up(calls)
    with patch.object(wh, "_check_and_record_dedup", return_value=12), patch(FOLLOW_UP, follow_up):
        assert wh.create_vendor_webhook_job(source="plex", canonical_path="/data/Movies/Foo.mkv") is None
    follow_up.assert_not_called()


def test_vendor_webhook_creates_a_real_follow_up_job_behind_the_preview_job(captured_overrides, tmp_path):
    from media_preview_generator.web.jobs import PRIORITY_NORMAL, get_job_manager
    from media_preview_generator.web.settings_manager import get_settings_manager

    get_settings_manager().set(
        "media_servers",
        [
            {
                "id": "jf-1",
                "type": "jellyfin",
                "name": "Jellyfin",
                "enabled": True,
                "libraries": [{"id": "1", "name": "Movies", "remote_paths": ["/data/Movies"], "enabled": True}],
                "markers": {"enabled": True, "library_ids": None},
            }
        ],
    )
    with (
        patch.object(wh, "_check_and_record_dedup", return_value=None),
        patch("media_preview_generator.markers.triggers.start_intro_credits_job_async") as start,
    ):
        preview_id = wh.create_vendor_webhook_job(
            source="jellyfin", title="Foo", canonical_path="/data/Movies/Foo.mkv", item_id_by_server={"jf-1": "abc"}
        )
    follow_ups = [j for j in get_job_manager().get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]
    assert len(follow_ups) == 1
    job = follow_ups[0]
    assert job.priority == PRIORITY_NORMAL
    assert job.config["follows_job_id"] == preview_id
    assert job.config["file_paths"] == ["/data/Movies/Foo.mkv"]
    assert job.config["webhook_item_id_hints"] == {"/data/Movies/Foo.mkv": {"jf-1": "abc"}}
    assert job.config["source"] == "jellyfin"
    start.assert_called_once_with(job.id)
