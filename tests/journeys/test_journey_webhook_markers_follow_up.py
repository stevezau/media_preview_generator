"""A webhook's files get their Intro & Credits job however the preview job starts: fired, revived, or both.

The preview job is saved when its batch opens, so a restart during the debounce revives it. The batch now also asks
for its Intro & Credits follow-up at batch open (``INTRO_CREDITS_FOLLOW_UP`` in the job's config), and the preview
runner queues that follow-up when it starts the job. Before, the follow-up was queued only when the debounce timer
fired, so a restart during the debounce ran the previews and silently dropped the markers.

Real app, real JobManager (jobs.db), real webhook handlers, real ``_start_job_async`` and restart revival, real
``submit_webhook_follow_up``. Only the preview work (``run_processing``) and the markers job's own run are stubbed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import INTRO_CREDITS_FOLLOW_UP, JOB_KIND_INTRO_CREDITS
from media_preview_generator.web.settings_manager import reset_settings_manager

pytestmark = pytest.mark.journey

EPISODE = "/data/tv/Show (2020)/Season 01/Show (2020) - S01E01.mkv"


def _server(sid, stype, *, markers=True):
    entry = {
        "id": sid,
        "type": stype,
        "name": sid.upper(),
        "enabled": True,
        "url": f"http://{sid}:8096",
        "auth": {"method": "api_key", "api_key": "k"} if stype != "plex" else {"token": "tok"},
        "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/data/tv"], "enabled": True}],
        "markers": {"enabled": markers, "library_ids": None},
    }
    return entry


@pytest.fixture(autouse=True)
def _reset_singletons():
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.webhooks as wh_mod

    def reset():
        reset_settings_manager()
        with jobs_mod._job_lock:
            jobs_mod._job_manager = None
        wh_mod._recent_dispatches.clear()
        wh_mod._pending_batches.clear()
        wh_mod._pending_timers.clear()

    reset()
    yield
    reset()


@pytest.fixture()
def app(tmp_path, monkeypatch):
    from media_preview_generator.web.app import create_app

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plex_cfg = tmp_path / "plex_cfg"
    (plex_cfg / "Media" / "localhost").mkdir(parents=True)
    plex = _server("plex-1", "plex", markers=False)
    plex["output"] = {"adapter": "plex_bundle", "plex_config_folder": str(plex_cfg)}
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "setup_complete": True,
                "webhook_enabled": True,
                "webhook_delay": 60,
                "max_concurrent_jobs": 10,
                "media_servers": [plex, _server("jf-1", "jellyfin"), _server("emby-1", "emby")],
            }
        )
    )
    (config_dir / "auth.json").write_text(json.dumps({"token": "test-token-12345678"}))
    monkeypatch.setenv("WEB_AUTH_TOKEN", "test-token-12345678")
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    reset_settings_manager()
    flask_app = create_app(config_dir=str(config_dir))
    flask_app.config_dir = str(config_dir)
    return flask_app


@pytest.fixture()
def previews():
    """Stub the preview work; record what each run was asked to do."""
    runs: list[dict] = []

    def fake_run_processing(config, selected_gpus, **kwargs):
        runs.append(
            {
                "job_id": kwargs.get("job_id"),
                "webhook_paths": list(config.webhook_paths or []),
                "server_id_filter": config.server_id_filter,
            }
        )
        return {"outcome": {"generated": len(config.webhook_paths or [])}}

    with patch("media_preview_generator.jobs.orchestrator.run_processing", side_effect=fake_run_processing):
        yield runs


@pytest.fixture()
def markers_runs():
    """Markers jobs are created and started for real, but their run is out of scope here (also when revived)."""
    with patch("media_preview_generator.markers.job_runner.run_intro_credits_job") as run:
        yield run


def _markers_jobs():
    from media_preview_generator.web.jobs import get_job_manager

    return [j for j in get_job_manager().get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]


def _open_batch(source, path, server_id=None):
    """Deliver one debounced webhook; the debounce timer never fires (the container restarts first)."""
    from media_preview_generator.web import webhooks as wh

    with patch.object(wh.threading, "Timer", MagicMock()):
        assert wh._schedule_webhook_job(source, "Show S01E01", path, server_id=server_id, early_scan=False)
    (batch,) = wh._pending_batches.values()
    return batch["job_id"]


def _restart(app):
    """What a container restart leaves: jobs.db on disk, every in-memory batch, timer and job manager gone."""
    import media_preview_generator.web.jobs as jobs_mod
    import media_preview_generator.web.webhooks as wh_mod
    from media_preview_generator.web import app as app_mod

    wh_mod._pending_batches.clear()
    wh_mod._pending_timers.clear()
    with jobs_mod._job_lock:
        jobs_mod._job_manager = None
    jobs_mod.get_job_manager(app.config_dir)
    app_mod._requeue_interrupted_on_startup(app.config_dir)


class TestRestartDuringTheDebounce:
    @pytest.mark.parametrize("pin", [None, "jf-1", "emby-1"], ids=["unpinned", "jellyfin-pin", "emby-pin"])
    def test_the_revived_preview_job_and_its_markers_job_both_exist(self, app, previews, markers_runs, pin):
        from media_preview_generator.web.jobs import JobStatus, get_job_manager

        with app.app_context():
            preview_id = _open_batch("sonarr", EPISODE, server_id=pin)
            assert _markers_jobs() == []  # nothing queued before the batch runs
            _restart(app)

        jm = get_job_manager()
        preview = jm.get_job(preview_id)
        assert preview.status is JobStatus.COMPLETED
        assert previews == [{"job_id": preview_id, "webhook_paths": [EPISODE], "server_id_filter": pin}]
        (markers,) = _markers_jobs()
        assert markers.config["follows_job_id"] == preview_id
        assert markers.config["file_paths"] == [EPISODE]
        assert markers.config["source"] == "sonarr"
        assert markers.config.get("server_id") == pin
        markers_runs.assert_called_once_with(markers.id)
        assert INTRO_CREDITS_FOLLOW_UP not in preview.config  # asked once, queued once

    def test_a_second_restart_queues_no_second_markers_job(self, app, previews, markers_runs):
        with app.app_context():
            _open_batch("radarr", EPISODE)
            _restart(app)
            _restart(app)
        assert len(_markers_jobs()) == 1


class TestTheBatchFires:
    @pytest.mark.parametrize("pin", [None, "emby-1"], ids=["unpinned", "emby-pin"])
    def test_one_markers_job_after_the_preview_job_started(self, app, previews, markers_runs, pin):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            preview_id = _open_batch("sonarr", EPISODE, server_id=pin)
            (key,) = wh._pending_batches
            wh._execute_webhook_job(key)

        assert [r["job_id"] for r in previews] == [preview_id]
        (markers,) = _markers_jobs()
        assert markers.config["follows_job_id"] == preview_id
        assert markers.config.get("server_id") == pin


class TestVendorWebhooks:
    """``/api/webhooks/incoming`` (unpinned) and ``/api/webhooks/server/<id>`` (pinned): the markers job publishes
    where the preview job does (``resolve_per_item_pin``)."""

    @pytest.mark.parametrize(
        ("source", "hints", "server_id_filter", "expected_pin"),
        [
            ("jellyfin", {"jf-1": "abc"}, None, "jf-1"),  # a non-Plex originator publishes to itself
            ("emby", {"emby-1": "42"}, None, "emby-1"),
            ("plex", {"plex-1": "9"}, None, None),  # a Plex originator fans out
            ("plex", None, None, None),
            ("jellyfin", {"jf-1": "abc"}, "emby-1", "emby-1"),  # the explicit pin wins
        ],
        ids=["jellyfin", "emby", "plex", "plex-no-hint", "explicit-pin"],
    )
    def test_the_markers_job_follows_the_preview_pin(
        self, app, previews, markers_runs, source, hints, server_id_filter, expected_pin
    ):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            preview_id = wh.create_vendor_webhook_job(
                source=source,
                canonical_path=EPISODE,
                item_id_by_server=hints,
                server_id=next(iter(hints or {}), None),
                server_id_filter=server_id_filter,
            )

        assert [(r["job_id"], r["server_id_filter"]) for r in previews] == [(preview_id, server_id_filter)]
        (markers,) = _markers_jobs()
        assert markers.config["follows_job_id"] == preview_id
        assert markers.config.get("server_id") == expected_pin
        assert markers.config["webhook_item_id_hints"] == ({EPISODE: hints} if hints else {})

    def test_a_pin_to_a_server_with_markers_off_queues_no_markers_job(self, app, previews, markers_runs):
        from media_preview_generator.web import webhooks as wh

        with app.app_context():
            wh.create_vendor_webhook_job(source="plex", canonical_path=EPISODE, server_id_filter="plex-1")
        assert len(previews) == 1
        assert _markers_jobs() == []


class TestThePreviewRunnerQueuesItOnce:
    def test_a_later_start_of_the_same_job_queues_nothing_more(self, app, previews, markers_runs):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        with app.app_context():
            preview_id = _open_batch("sonarr", EPISODE)
            job = get_job_manager().get_job(preview_id)
            _start_job_async(preview_id, dict(job.config))
            _start_job_async(preview_id, dict(job.config))  # e.g. Reprocess of a stale snapshot, resume drain

        assert len(previews) == 2
        assert len(_markers_jobs()) == 1

    def test_a_job_that_never_asked_queues_nothing(self, app, previews, markers_runs):
        from media_preview_generator.web.jobs import get_job_manager
        from media_preview_generator.web.routes.job_runner import _start_job_async

        with app.app_context():
            job = get_job_manager().create_job(library_name="manual", config={"webhook_paths": [EPISODE]})
            _start_job_async(job.id, dict(job.config))
        assert len(previews) == 1
        assert _markers_jobs() == []

    def test_a_follow_up_that_cant_be_queued_never_costs_the_previews(self, app, previews, markers_runs):
        from media_preview_generator.web.jobs import JobStatus, get_job_manager

        with (
            app.app_context(),
            patch(
                "media_preview_generator.markers.triggers.submit_follow_ups", side_effect=RuntimeError("boom")
            ) as submit,
        ):
            preview_id = _open_batch("sonarr", EPISODE)
            _restart(app)

        submit.assert_called_once()
        preview = get_job_manager().get_job(preview_id)
        assert preview.status is JobStatus.COMPLETED
        assert [r["job_id"] for r in previews] == [preview_id]
