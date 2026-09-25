"""Webhook preview jobs ask for their Intro & Credits follow-up in their saved config (spec §6.4 item 9).

The batch asks when it opens, so a job revived after a restart during the debounce asks too; the preview runner
queues the follow-up when it starts the job (``markers.triggers.submit_pending_follow_up``) and takes the request
off. The end-to-end journeys (fire, restart revival, vendor pins) are in
tests/journeys/test_journey_webhook_markers_follow_up.py.
"""

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import INTRO_CREDITS_FOLLOW_UP
from media_preview_generator.markers import triggers
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.web import webhooks as wh

SONARR_PATH = "/data/TV Shows/Show (2023)/Season 02/Show - S02E04.mkv"
SUBMIT = "media_preview_generator.markers.triggers.submit_follow_ups"
SERVERS = [
    {"id": "plex-1", "type": "plex", "name": "Plex", "enabled": True},
    {"id": "jf-1", "type": "jellyfin", "name": "Jellyfin", "enabled": True},
]


@pytest.fixture
def started(monkeypatch):
    """Patch ``_start_job_async`` and capture every (job_id, overrides) call."""
    captured: list[dict] = []

    def fake_start(job_id, overrides):
        captured.append({"job_id": job_id, "overrides": dict(overrides or {})})

    monkeypatch.setattr("media_preview_generator.web.routes._start_job_async", fake_start)
    return captured


@pytest.fixture(autouse=True)
def _isolate_settings(tmp_path, monkeypatch):
    import media_preview_generator.web.settings_manager as sm_mod

    sm_mod._settings_manager = None
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    sm_mod.get_settings_manager().set("media_servers", SERVERS)
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


def _saved_config(job_id):
    from media_preview_generator.web.jobs import get_job_manager

    return get_job_manager().get_job(job_id).config


def _open_batch(source, path, *, fire, **kwargs):
    captured = []

    def fake_timer(_delay, fn, *positional, args=None, kwargs=None):
        t = MagicMock()
        t.start = lambda: captured.append((fn, list(positional) + list(args or []), kwargs or {}))
        return t

    with (
        patch.object(wh, "_check_and_record_dedup", return_value=None),
        patch("media_preview_generator.web.webhooks.threading.Timer", side_effect=fake_timer),
    ):
        assert wh._schedule_webhook_job(source, "Show S02E04", path, early_scan=False, **kwargs) is True
        fn, args, fn_kwargs = captured[-1]
        if fire:
            fn(*args, **fn_kwargs)
    return next(iter(wh._pending_batches.values()))["job_id"] if not fire else None


@pytest.mark.parametrize(
    ("source", "server_id", "pin"),
    [
        ("sonarr", None, None),
        ("radarr", None, None),
        ("sportarr", None, None),
        ("plex", "plex-1", "plex-1"),  # Plex library.new via /api/webhooks/plex, pinned to the resolved server
        ("sonarr", "jf-1", "jf-1"),  # ?server_id= on an Arr webhook
        ("sonarr", "gone-1", None),  # an unknown server id is treated as unpinned, for previews and markers alike
        ("custom", None, None),  # Tdarr / scripts
        ("recently_added", None, None),
    ],
)
def test_the_batch_asks_when_it_opens_and_the_runner_queues_it_with_the_preview_pin(
    started, monkeypatch, source, server_id, pin
):
    import media_preview_generator.web.webhooks as wh_mod

    wh_mod._pending_batches.clear()
    job_id = _open_batch(source, SONARR_PATH, fire=False, **({"server_id": server_id} if server_id else {}))
    opened = _saved_config(job_id)
    assert opened[INTRO_CREDITS_FOLLOW_UP] is True
    assert opened["webhook_paths"] == [SONARR_PATH]
    assert opened.get("server_id") == pin  # a revival publishes previews and markers where the fire would have

    (key,) = wh_mod._pending_batches
    wh_mod._execute_webhook_job(key)
    (start,) = started
    assert start["job_id"] == job_id
    assert start["overrides"].get("server_id") == pin
    assert _saved_config(job_id)[INTRO_CREDITS_FOLLOW_UP] is True  # still asking when the runner starts it

    with patch(SUBMIT, return_value=["ic-1"]) as submit:
        assert triggers.submit_pending_follow_up(job_id, start["overrides"]) == ["ic-1"]
    submit.assert_called_once_with(
        preview_job_id=job_id,
        items=[ProcessableItem(canonical_path=SONARR_PATH, server_id="", item_id_by_server={})],
        source=source,
        pin=pin,
    )
    assert INTRO_CREDITS_FOLLOW_UP not in _saved_config(job_id)


def test_a_batch_that_grows_keeps_asking_and_keeps_its_pin(started):
    import media_preview_generator.web.webhooks as wh_mod

    wh_mod._pending_batches.clear()
    job_id = _open_batch("sonarr", SONARR_PATH, fire=False, server_id="jf-1")
    other = SONARR_PATH.replace("S02E04", "S02E05")
    _open_batch("sonarr", other, fire=False, server_id="jf-1")
    saved = _saved_config(job_id)
    assert saved["webhook_paths"] == sorted([SONARR_PATH, other])
    assert saved[INTRO_CREDITS_FOLLOW_UP] is True
    assert saved["server_id"] == "jf-1"
    assert started == []


@pytest.mark.parametrize(
    ("source", "hints", "server_id_filter", "items", "pin"),
    [
        ("jellyfin", {"jf-1": "abc"}, None, [("jf-1", {"jf-1": "abc"})], None),
        ("emby", {"emby-1": "42"}, None, [("emby-1", {"emby-1": "42"})], None),
        ("plex", {"plex-1": "9"}, None, [("plex-1", {"plex-1": "9"})], None),
        ("plex", None, None, [("", {})], None),
        ("jellyfin", {"jf-1": ""}, None, [("", {})], None),  # empty ids are dropped before they reach the job
        ("jellyfin", {"jf-1": "abc"}, "jf-1", [("jf-1", {"jf-1": "abc"})], "jf-1"),  # /api/webhooks/server/<id>
    ],
)
def test_a_vendor_webhook_asks_and_hands_its_item_ids_and_pin_to_the_runner(
    started, source, hints, server_id_filter, items, pin
):
    with patch.object(wh, "_check_and_record_dedup", return_value=None):
        job_id = wh.create_vendor_webhook_job(
            source=source,
            title="Foo",
            canonical_path="/data/Movies/Foo.mkv",
            item_id_by_server=hints,
            server_id=None,
            server_id_filter=server_id_filter,
        )
    assert _saved_config(job_id)[INTRO_CREDITS_FOLLOW_UP] is True
    (start,) = started
    with patch(SUBMIT, return_value=[]) as submit:
        triggers.submit_pending_follow_up(job_id, start["overrides"])
    submit.assert_called_once_with(
        preview_job_id=job_id,
        items=[ProcessableItem("/data/Movies/Foo.mkv", origin, dict(ids)) for origin, ids in items],
        source=source,
        pin=pin,
    )


def test_the_vendor_request_uses_the_sanitised_source(started):
    with patch.object(wh, "_check_and_record_dedup", return_value=None):
        job_id = wh.create_vendor_webhook_job(source="  JellyFin ", canonical_path="/data/Movies/Foo.mkv")
    with patch(SUBMIT, return_value=[]) as submit:
        triggers.submit_pending_follow_up(job_id, started[0]["overrides"])
    assert submit.call_args.kwargs["source"] == "jellyfin"


def test_a_duplicate_vendor_webhook_creates_no_job(started):
    with patch.object(wh, "_check_and_record_dedup", return_value=12):
        assert wh.create_vendor_webhook_job(source="plex", canonical_path="/data/Movies/Foo.mkv") is None
    assert started == []


class TestSubmitPendingFollowUp:
    def _job(self, config):
        from media_preview_generator.web.jobs import get_job_manager

        return get_job_manager().create_job(library_name="x", config=config)

    def test_a_job_that_never_asked_queues_nothing(self):
        job = self._job({"webhook_paths": [SONARR_PATH], "source": "sonarr"})
        with patch(SUBMIT) as submit:
            assert triggers.submit_pending_follow_up(job.id, {"webhook_paths": [SONARR_PATH]}) == []
        submit.assert_not_called()

    def test_a_stale_copy_of_the_request_asks_nothing(self):
        # A revival's snapshot of the config still holds the request the first start already took.
        job = self._job({"webhook_paths": [SONARR_PATH], "source": "sonarr"})
        with patch(SUBMIT) as submit:
            triggers.submit_pending_follow_up(job.id, {INTRO_CREDITS_FOLLOW_UP: True, "webhook_paths": [SONARR_PATH]})
        submit.assert_not_called()

    def test_it_asks_once(self):
        job = self._job({INTRO_CREDITS_FOLLOW_UP: True, "webhook_paths": [SONARR_PATH], "source": "sonarr"})
        with patch(SUBMIT, return_value=["ic-1"]) as submit:
            assert triggers.submit_pending_follow_up(job.id) == ["ic-1"]
            assert triggers.submit_pending_follow_up(job.id) == []
        submit.assert_called_once()

    def test_a_failure_takes_the_request_off_too(self):
        job = self._job({INTRO_CREDITS_FOLLOW_UP: True, "webhook_paths": [SONARR_PATH], "source": "sonarr"})
        with patch(SUBMIT, side_effect=RuntimeError("boom")), pytest.raises(RuntimeError):
            triggers.submit_pending_follow_up(job.id)
        assert INTRO_CREDITS_FOLLOW_UP not in _saved_config(job.id)

    def test_a_request_it_cant_read_is_taken_off_too(self):
        job = self._job(
            {
                INTRO_CREDITS_FOLLOW_UP: True,
                "webhook_paths": [SONARR_PATH],
                "webhook_item_id_hints": {SONARR_PATH: "not-a-mapping"},
            }
        )
        with patch(SUBMIT) as submit, pytest.raises(ValueError):
            triggers.submit_pending_follow_up(job.id)
        submit.assert_not_called()
        assert INTRO_CREDITS_FOLLOW_UP not in _saved_config(job.id)

    def test_files_a_batch_merge_added_meanwhile_keep_the_request(self):
        """A resume drain started the job during its debounce; a webhook merged a second file into the batch while
        the follow-up for the first was being queued. The request stays, so the fire (or a revival) queues it."""
        from media_preview_generator.web.jobs import get_job_manager

        other = SONARR_PATH.replace("S02E04", "S02E05")
        job = self._job({INTRO_CREDITS_FOLLOW_UP: True, "webhook_paths": [SONARR_PATH], "source": "sonarr"})

        def merged_meanwhile(**_kwargs):
            get_job_manager().merge_job_config(
                job.id, {"webhook_paths": [SONARR_PATH, other], INTRO_CREDITS_FOLLOW_UP: True}
            )
            return ["ic-1"]

        with patch(SUBMIT, side_effect=merged_meanwhile):
            triggers.submit_pending_follow_up(job.id)
        assert _saved_config(job.id)[INTRO_CREDITS_FOLLOW_UP] is True

    def test_the_fire_writing_the_paths_meanwhile_takes_the_request(self):
        """The fire's own write of the batch's paths (the job thread merging its start overrides) is no new file."""
        from media_preview_generator.web.jobs import get_job_manager

        job = self._job({INTRO_CREDITS_FOLLOW_UP: True, "source": "sonarr"})  # the fire replaced webhook_paths

        def thread_merged_the_overrides(**_kwargs):
            get_job_manager().merge_job_config(job.id, {"webhook_paths": [SONARR_PATH]})
            return ["ic-1"]

        with patch(SUBMIT, side_effect=thread_merged_the_overrides):
            triggers.submit_pending_follow_up(job.id, {"webhook_paths": [SONARR_PATH]})
        assert INTRO_CREDITS_FOLLOW_UP not in _saved_config(job.id)

    def test_the_overrides_fill_in_what_the_fired_batch_gave_the_job(self):
        job = self._job({INTRO_CREDITS_FOLLOW_UP: True, "source": "sonarr"})  # the fire replaced webhook_paths
        with patch(SUBMIT, return_value=[]) as submit:
            triggers.submit_pending_follow_up(job.id, {"webhook_paths": [SONARR_PATH], "server_id": "jf-1"})
        kwargs = submit.call_args.kwargs
        assert [i.canonical_path for i in kwargs["items"]] == [SONARR_PATH]
        assert kwargs["pin"] == "jf-1"
