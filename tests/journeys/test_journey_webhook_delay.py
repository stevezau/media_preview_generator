"""Per-request webhook delay across receipt, recovery, and dispatch."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.web import jobs
from media_preview_generator.web import webhooks as wh
from media_preview_generator.web.app import _requeue_interrupted_on_startup
from media_preview_generator.web.routes import job_runner
from media_preview_generator.web.settings_manager import get_settings_manager

from . import test_journey_start_job_async_branches as runner_fixtures

pytestmark = pytest.mark.journey
app = runner_fixtures.app
_reset_singletons = runner_fixtures._reset_singletons

_HEADERS = {"X-Auth-Token": "test-token-12345678"}
_SOURCES = ("radarr", "sonarr", "sportarr", "custom", "plex")


def _payload(source: str, path: str = "/data/show/one.mkv", *, test: bool = False) -> dict:
    if test:
        return {"event": "test.ping"} if source == "plex" else {"eventType": "Test"}
    if source == "radarr":
        return {"eventType": "Download", "movie": {"title": "Movie"}, "movieFile": {"path": path}}
    if source in ("sonarr", "sportarr"):
        return {"eventType": "Download", "series": {"title": "Show"}, "episodeFile": {"path": path}}
    if source == "plex":
        return {
            "event": "library.new",
            "Metadata": {"ratingKey": "42", "type": "movie", "title": "Movie", "Media": [{"Part": [{"file": path}]}]},
        }
    return {"file_path": path}


@pytest.fixture
def delay_flow(app) -> Iterator[SimpleNamespace]:
    """Exercise the real routes and runner with controlled time and external work."""
    timers: list[SimpleNamespace] = []
    clock = SimpleNamespace(now=datetime.now(UTC))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None) -> datetime:
            return clock.now if tz else clock.now.replace(tzinfo=None)

    def make_timer(interval: float, target, args=None, kwargs=None) -> SimpleNamespace:
        timer = SimpleNamespace(interval=interval, target=target, args=args or [], kwargs=kwargs or {}, cancelled=False)

        def cancel() -> None:
            timer.cancelled = True

        timer.cancel = cancel
        timer.start = MagicMock()
        timer.fire = lambda: target(*timer.args, **timer.kwargs)
        timers.append(timer)
        return timer

    settings = get_settings_manager()
    settings.set("webhook_delay", 300)
    with (
        app.app_context(),
        patch.object(wh, "datetime", Clock),
        patch.object(wh, "threading", SimpleNamespace(Timer=make_timer)),
        patch.object(wh, "_kick_early_scan") as scans,
        patch("media_preview_generator.web.routes._start_job_async", side_effect=job_runner._start_job_async) as start,
        patch(
            "media_preview_generator.jobs.orchestrator.run_processing", return_value={"outcome": {"generated": 1}}
        ) as run,
    ):
        yield SimpleNamespace(
            client=app.test_client(),
            timers=timers,
            clock=clock,
            scans=scans,
            run=run,
            start=start,
            settings=settings,
        )
        wh.reset_webhook_debounce()


def _post(flow: SimpleNamespace, source: str, query: str = "", path: str = "/data/show/one.mkv", *, test: bool = False):
    return flow.client.post(f"/api/webhooks/{source}{query}", json=_payload(source, path, test=test), headers=_HEADERS)


@pytest.mark.parametrize("source", _SOURCES)
@pytest.mark.parametrize(
    "query,expected",
    [("", 300), ("?delay=1", 1), ("?delay=30", 30), ("?delay=600", 600), ("?delay=601", 601), ("?delay=3600", 3600)],
)
def test_request_delay_reaches_timer_for_each_source(delay_flow, source: str, query: str, expected: int) -> None:
    assert _post(delay_flow, source, query).status_code == 202
    assert delay_flow.timers[-1].interval == expected
    job = jobs.get_job_manager().get_all_jobs()[0]
    assert job.config["webhook_debounce_pending"] is True
    assert datetime.fromisoformat(job.config["webhook_fire_at"]) == delay_flow.clock.now + timedelta(seconds=expected)
    assert delay_flow.settings.get("webhook_delay") == 300
    delay_flow.run.assert_not_called()


@pytest.mark.parametrize("source", _SOURCES)
def test_invalid_delay_rejected_without_scheduling_including_test_events(delay_flow, source: str) -> None:
    invalid_queries = (
        "delay=",
        "delay=0",
        "delay=3601",
        "delay=-1",
        "delay=1.5",
        "delay=abc",
        "delay=%2B30",
        "delay=%2030",
        "delay=３０",
        "delay=30&delay=40",
    )
    for query in invalid_queries:
        for test in (False, True):
            response = _post(delay_flow, source, f"?{query}", test=test)
            assert response.status_code == 400, (source, query, test, response.get_json())
            assert "delay" in response.get_json()["error"].lower()
    assert not jobs.get_job_manager().get_all_jobs()
    assert not delay_flow.timers
    delay_flow.scans.assert_not_called()


@pytest.mark.parametrize("source", _SOURCES)
def test_valid_test_event_does_not_create_job_or_timer(delay_flow, source: str) -> None:
    assert _post(delay_flow, source, "?delay=30", test=True).status_code == 200
    assert not jobs.get_job_manager().get_all_jobs()
    assert not delay_flow.timers
    delay_flow.scans.assert_not_called()


def test_sources_have_independent_delays_and_new_path_resets_only_its_batch(delay_flow) -> None:
    flow = delay_flow
    assert _post(flow, "radarr", "?delay=30").status_code == 202
    assert _post(flow, "sonarr", "?delay=300").status_code == 202
    original_radarr, original_sonarr = flow.timers
    flow.clock.now += timedelta(seconds=10)
    assert _post(flow, "sonarr", "?delay=60", "/data/show/two.mkv").status_code == 202
    assert original_sonarr.cancelled
    assert not original_radarr.cancelled
    assert flow.timers[-1].interval == 60
    sonarr_job = next(job for job in jobs.get_job_manager().get_all_jobs() if job.config["source"] == "sonarr")
    assert datetime.fromisoformat(sonarr_job.config["webhook_fire_at"]) == flow.clock.now + timedelta(seconds=60)
    assert sorted(sonarr_job.config["webhook_paths"]) == ["/data/show/one.mkv", "/data/show/two.mkv"]
    deadline = sonarr_job.config["webhook_fire_at"]
    flow.clock.now += timedelta(seconds=5)
    assert _post(flow, "sonarr", "?delay=600", "/data/show/two.mkv").status_code == 200
    assert len(flow.timers) == 3
    assert sonarr_job.config["webhook_fire_at"] == deadline
    assert flow.scans.call_count == 3
    flow.run.assert_not_called()


def test_omitted_delay_uses_global_and_early_callback_rearms_until_deadline(delay_flow) -> None:
    flow = delay_flow
    assert _post(flow, "sonarr", "?delay=30").status_code == 202
    flow.clock.now += timedelta(seconds=10)
    assert _post(flow, "sonarr", path="/data/show/two.mkv").status_code == 202
    assert flow.timers[-1].interval == 300
    timer = flow.timers[-1]
    timer.fire()
    flow.run.assert_not_called()
    assert flow.timers[-1] is not timer
    assert flow.timers[-1].interval == 300
    flow.clock.now += timedelta(seconds=300)
    flow.timers[-1].fire()
    assert flow.run.call_count == 1
    assert sorted(flow.run.call_args.args[0].webhook_paths) == ["/data/show/one.mkv", "/data/show/two.mkv"]


@pytest.mark.parametrize("remaining", [20, -10])
@pytest.mark.parametrize("server_id", [None, "plex-1"])
def test_restart_restores_remaining_wait_paths_pin_and_deleted_paths(
    delay_flow, tmp_path, monkeypatch, remaining: int, server_id: str | None
) -> None:
    flow = delay_flow
    assert wh._schedule_webhook_job(
        "sonarr", "Show", "/data/show/one.mkv", server_id=server_id, deleted_paths=["/data/show/old.mkv"], delay=30
    )
    original = jobs.get_job_manager().get_all_jobs()[0]
    original_config = dict(original.config)
    wh.reset_webhook_debounce()
    flow.scans.reset_mock()
    flow.clock.now += timedelta(seconds=30 - remaining)
    reloaded = jobs.JobManager(config_dir=str(tmp_path / "config"))
    monkeypatch.setattr(jobs, "_job_manager", reloaded)
    try:
        assert reloaded.get_job(original.id).config == original_config
        _requeue_interrupted_on_startup(str(tmp_path / "config"))
        flow.run.assert_not_called()
        assert flow.timers[-1].interval == pytest.approx(max(0, remaining))
        flow.clock.now += timedelta(seconds=max(0, remaining))
        flow.timers[-1].fire()
        assert flow.run.call_count == 1
        config = flow.run.call_args.args[0]
        assert config.webhook_paths == ["/data/show/one.mkv"]
        assert config.webhook_deleted_paths == ["/data/show/old.mkv"]
        assert config.server_id_filter == server_id
        flow.scans.assert_not_called()
    finally:
        if reloaded._retention_timer:
            reloaded._retention_timer.cancel()


def test_same_path_after_dedup_ttl_resets_delay_and_merges_deleted_paths(delay_flow) -> None:
    flow = delay_flow
    assert wh._schedule_webhook_job(
        "sonarr", "Show", "/data/show/one.mkv", delay=900, deleted_paths=["/data/show/old-one.mkv"]
    )
    first_timer = flow.timers[-1]
    flow.clock.now += timedelta(seconds=500)
    assert _post(flow, "sonarr", "?delay=600", "/data/show/two.mkv").status_code == 202
    flow.clock.now += timedelta(seconds=101)
    assert wh._schedule_webhook_job(
        "sonarr", "Show upgraded", "/data/show/one.mkv", delay=30, deleted_paths=["/data/show/old-two.mkv"]
    )
    assert first_timer.cancelled
    assert flow.timers[-1].interval == 30
    all_jobs = jobs.get_job_manager().get_all_jobs()
    assert len(all_jobs) == 1
    job = all_jobs[0]
    assert sorted(job.config["webhook_paths"]) == ["/data/show/one.mkv", "/data/show/two.mkv"]
    assert sorted(job.config["webhook_deleted_paths"]) == ["/data/show/old-one.mkv", "/data/show/old-two.mkv"]
    assert datetime.fromisoformat(job.config["webhook_fire_at"]) == flow.clock.now + timedelta(seconds=30)
    assert flow.scans.call_count == 3
    flow.run.assert_not_called()


def test_resume_and_stale_timer_cannot_bypass_updated_deadline(delay_flow) -> None:
    flow = delay_flow
    _post(flow, "sonarr", "?delay=30")
    stale = flow.timers[-1]
    flow.clock.now += timedelta(seconds=10)
    _post(flow, "sonarr", "?delay=60", "/data/show/two.mkv")
    current = flow.timers[-1]
    job_runner.resume_running_and_drain_pending()
    stale.fire()
    flow.run.assert_not_called()
    assert not current.cancelled
    flow.clock.now += timedelta(seconds=60)
    current.fire()
    assert flow.run.call_count == 1
    assert sorted(flow.run.call_args.args[0].webhook_paths) == ["/data/show/one.mkv", "/data/show/two.mkv"]


def test_next_start_after_lookup_failure_preserves_pending_deadline(delay_flow) -> None:
    flow = delay_flow
    assert _post(flow, "sonarr", "?delay=30&server_id=plex-1").status_code == 202
    manager = jobs.get_job_manager()
    job = manager.get_all_jobs()[0]
    deadline = job.config["webhook_fire_at"]
    timer = flow.timers[-1]
    real_get_job = manager.get_job
    lookup_failed = False

    def fail_once(job_id: str):
        nonlocal lookup_failed
        if not lookup_failed:
            lookup_failed = True
            raise RuntimeError("temporary job lookup failure")
        return real_get_job(job_id)

    with patch.object(manager, "get_job", side_effect=fail_once):
        job_runner._start_job_async(job.id, None)
        flow.run.assert_not_called()
        assert job.id not in job_runner._inflight_jobs
        job_runner._start_job_async(job.id, None)
    flow.run.assert_not_called()
    assert job.id not in job_runner._inflight_jobs
    assert job.status == jobs.JobStatus.PENDING
    assert job.config["webhook_fire_at"] == deadline
    assert not timer.cancelled
    flow.clock.now += timedelta(seconds=30)
    timer.fire()
    assert flow.run.call_count == 1
    config = flow.run.call_args.args[0]
    assert config.webhook_paths == ["/data/show/one.mkv"]
    assert config.webhook_source == "sonarr"
    assert config.server_id_filter == "plex-1"


@pytest.mark.parametrize(
    "first_delay,next_delay,elapsed,cap,wait",
    [(30, 30, 590, 600, 10), (30, 3600, 20, 3600, 3580), (3600, 30, 3590, 3600, 10), (300, 900, 10, 900, 890)],
)
def test_mixed_delays_bound_batch_age_from_first_import(
    delay_flow, first_delay: int, next_delay: int, elapsed: int, cap: int, wait: int
) -> None:
    flow = delay_flow
    opened = flow.clock.now
    assert _post(flow, "sonarr", f"?delay={first_delay}").status_code == 202
    flow.clock.now += timedelta(seconds=elapsed)
    assert _post(flow, "sonarr", f"?delay={next_delay}", "/data/show/two.mkv").status_code == 202
    job = jobs.get_job_manager().get_all_jobs()[0]
    assert flow.timers[-1].interval == wait
    assert job.config["webhook_batch_opened_at"] == opened.timestamp()
    assert job.config["webhook_batch_max_wait"] == cap
    assert datetime.fromisoformat(job.config["webhook_fire_at"]) == opened + timedelta(seconds=cap)
    flow.run.assert_not_called()


def test_restart_preserves_longest_delay_and_original_batch_age(delay_flow, tmp_path, monkeypatch) -> None:
    flow = delay_flow
    opened = flow.clock.now
    assert _post(flow, "sonarr", "?delay=3600&server_id=plex-1").status_code == 202
    original = jobs.get_job_manager().get_all_jobs()[0]
    wh.reset_webhook_debounce()
    flow.clock.now += timedelta(seconds=100)
    reloaded = jobs.JobManager(config_dir=str(tmp_path / "config"))
    monkeypatch.setattr(jobs, "_job_manager", reloaded)
    try:
        _requeue_interrupted_on_startup(str(tmp_path / "config"))
        assert flow.timers[-1].interval == 3500
        flow.clock.now += timedelta(seconds=3400)
        assert _post(flow, "sonarr", "?delay=300&server_id=plex-1", "/data/show/two.mkv").status_code == 202
        assert flow.timers[-1].interval == 100
        job = reloaded.get_job(original.id)
        assert job.config["webhook_batch_opened_at"] == opened.timestamp()
        assert job.config["webhook_batch_max_wait"] == 3600
        assert datetime.fromisoformat(job.config["webhook_fire_at"]) == opened + timedelta(seconds=3600)
        flow.clock.now += timedelta(seconds=100)
        flow.timers[-1].fire()
        assert flow.run.call_count == 1
        config = flow.run.call_args.args[0]
        assert sorted(config.webhook_paths) == ["/data/show/one.mkv", "/data/show/two.mkv"]
        assert config.webhook_source == "sonarr"
        assert config.server_id_filter == "plex-1"
    finally:
        if reloaded._retention_timer:
            reloaded._retention_timer.cancel()


@pytest.mark.parametrize("source", ["sonarr", "sportarr"])
def test_import_complete_forwards_delay_to_every_path_and_scans_folder_once(delay_flow, source: str) -> None:
    flow = delay_flow
    payload = {
        "eventType": "Download",
        "series": {"title": "Show", "path": "/data/show"},
        "episodeFiles": [{"path": "/data/show/one.mkv"}, {"path": "/data/show/two.mkv"}],
    }
    response = flow.client.post(f"/api/webhooks/{source}?delay=3600", json=payload, headers=_HEADERS)
    assert response.status_code == 202
    assert [timer.interval for timer in flow.timers] == [3600, 3600]
    job = jobs.get_job_manager().get_all_jobs()[0]
    assert sorted(job.config["webhook_paths"]) == ["/data/show/one.mkv", "/data/show/two.mkv"]
    assert datetime.fromisoformat(job.config["webhook_fire_at"]) == flow.clock.now + timedelta(seconds=3600)
    flow.scans.assert_called_once_with("/data/show/one.mkv", None, job.id)
    flow.run.assert_not_called()


def test_resume_uses_finalized_config_after_snapshot_races_timer(delay_flow) -> None:
    flow = delay_flow
    _post(flow, "sonarr", "?delay=30")
    manager = jobs.get_job_manager()
    job = manager.get_all_jobs()[0]
    stale_snapshot = dict(job.config)
    _post(flow, "sonarr", "?delay=30", "/data/show/two.mkv")
    flow.settings.processing_paused = True
    flow.clock.now += timedelta(seconds=30)
    flow.timers[-1].fire()
    flow.run.assert_not_called()
    flow.start.assert_not_called()
    assert job.status == jobs.JobStatus.PENDING
    assert not job.config.get("webhook_debounce_pending")
    flow.settings.processing_paused = False
    job_runner._start_job_async(job.id, stale_snapshot)
    assert flow.run.call_count == 1
    assert sorted(flow.run.call_args.args[0].webhook_paths) == ["/data/show/one.mkv", "/data/show/two.mkv"]
    assert not job.config.get("webhook_debounce_pending")


@pytest.mark.parametrize("action", ["cancel", "delete"])
def test_cancelled_or_deleted_job_is_not_recreated_by_timer(delay_flow, action: str) -> None:
    flow = delay_flow
    _post(flow, "sonarr", "?delay=30")
    manager = jobs.get_job_manager()
    job = manager.get_all_jobs()[0]
    timer = flow.timers[-1]
    if action == "cancel":
        manager.cancel_job(job.id)
    else:
        assert manager.delete_job(job.id)
    flow.clock.now += timedelta(seconds=30)
    timer.fire()
    flow.run.assert_not_called()
    assert not wh._pending_batches
    assert len(manager.get_all_jobs()) == (1 if action == "cancel" else 0)


def test_fire_now_runs_once_and_stale_timer_does_not_dispatch_again(delay_flow) -> None:
    flow = delay_flow
    _post(flow, "sonarr", "?delay=300&server_id=plex-1")
    job = jobs.get_job_manager().get_all_jobs()[0]
    timer = flow.timers[-1]
    response = flow.client.post(f"/api/jobs/{job.id}/fire-webhook-now", headers=_HEADERS)
    assert response.status_code == 202
    assert flow.run.call_count == 1
    config = flow.run.call_args.args[0]
    assert config.webhook_paths == ["/data/show/one.mkv"]
    assert config.webhook_source == "sonarr"
    assert config.server_id_filter == "plex-1"
    assert timer.cancelled
    assert not job.config.get("webhook_debounce_pending")
    timer.fire()
    assert flow.run.call_count == 1


def test_cancelled_batch_cannot_absorb_new_path_or_fire_new_job(delay_flow) -> None:
    flow = delay_flow
    _post(flow, "sonarr", "?delay=30")
    manager = jobs.get_job_manager()
    old_job = manager.get_all_jobs()[0]
    old_timer = flow.timers[-1]
    manager.cancel_job(old_job.id)
    assert _post(flow, "sonarr", "?delay=60", "/data/show/two.mkv").status_code == 202
    new_jobs = [job for job in manager.get_all_jobs() if job.id != old_job.id]
    assert len(new_jobs) == 1
    assert new_jobs[0].config["webhook_paths"] == ["/data/show/two.mkv"]
    response = flow.client.post(f"/api/jobs/{old_job.id}/fire-webhook-now", headers=_HEADERS)
    assert response.status_code == 404
    old_timer.fire()
    flow.run.assert_not_called()
    flow.clock.now += timedelta(seconds=60)
    flow.timers[-1].fire()
    assert flow.run.call_count == 1
    assert flow.run.call_args.args[0].webhook_paths == ["/data/show/two.mkv"]


def test_fire_now_does_not_claim_replacement_after_job_lookup(delay_flow) -> None:
    flow = delay_flow
    _post(flow, "sonarr", "?delay=30")
    manager = jobs.get_job_manager()
    old_job = manager.get_all_jobs()[0]
    find_pending = wh.find_pending_batch_key_for_job

    def replace_after_lookup(job_id: str) -> str:
        key = find_pending(job_id)
        assert key is not None
        manager.cancel_job(job_id)
        assert wh._schedule_webhook_job("sonarr", "Replacement", "/data/show/two.mkv", delay=60)
        return key

    with (
        patch.object(wh, "find_pending_batch_key_for_job", side_effect=replace_after_lookup),
        patch.object(wh, "_fire_pending_batch_now", wraps=wh._fire_pending_batch_now) as claim,
    ):
        response = flow.client.post(f"/api/jobs/{old_job.id}/fire-webhook-now", headers=_HEADERS)
    assert response.status_code == 404
    claim.assert_called_once_with(wh._debounce_key("sonarr"), job_id=old_job.id)
    flow.run.assert_not_called()
    new_job = next(job for job in manager.get_all_jobs() if job.id != old_job.id)
    assert new_job.status == jobs.JobStatus.PENDING
    assert new_job.config["webhook_paths"] == ["/data/show/two.mkv"]
    assert new_job.config["webhook_debounce_pending"] is True
    assert not flow.timers[-1].cancelled
    flow.clock.now += timedelta(seconds=60)
    flow.timers[-1].fire()
    assert flow.run.call_count == 1
    config = flow.run.call_args.args[0]
    assert config.webhook_paths == ["/data/show/two.mkv"]
    assert config.webhook_source == "sonarr"
    assert config.server_id_filter is None


def test_manual_reprocess_starts_immediately_without_initial_delay_state(delay_flow) -> None:
    flow = delay_flow
    _post(flow, "sonarr", "?delay=300")
    manager = jobs.get_job_manager()
    original = manager.get_all_jobs()[0]
    manager.cancel_job(original.id)
    response = flow.client.post(f"/api/jobs/{original.id}/reprocess", headers=_HEADERS)
    assert response.status_code == 201
    assert flow.run.call_count == 1
    config = flow.run.call_args.args[0]
    assert config.webhook_paths == ["/data/show/one.mkv"]
    assert config.webhook_source == "sonarr"
    assert config.server_id_filter is None
    replay = manager.get_job(response.get_json()["id"])
    assert replay.config["webhook_paths"] == ["/data/show/one.mkv"]
    assert replay.config["source"] == "sonarr"
    assert "webhook_debounce_pending" not in replay.config
    assert "webhook_fire_at" not in replay.config


def test_automatic_retry_uses_its_own_backoff_not_global_webhook_delay(delay_flow) -> None:
    flow = delay_flow
    scheduled_at = (flow.clock.now - timedelta(seconds=60)).isoformat()
    job = jobs.get_job_manager().create_job(
        library_name="Retry",
        config={
            "is_retry": True,
            "retry_delay": 7,
            "scheduled_at": scheduled_at,
            "webhook_paths": ["/data/show/one.mkv"],
            "source": "sonarr",
            "server_id": "plex-1",
        },
    )
    with patch("time.sleep") as sleep, patch.object(flow.settings, "get", wraps=flow.settings.get) as get:
        job_runner._start_job_async(job.id, job.config)
    assert flow.run.call_count == 1
    config = flow.run.call_args.args[0]
    assert config.webhook_paths == ["/data/show/one.mkv"]
    assert config.webhook_source == "sonarr"
    assert config.server_id_filter == "plex-1"
    assert [call.args[0] for call in sleep.call_args_list if call.args[0] >= 1] == [2, 2, 2, 1]
    assert not any(call.args[0] == "webhook_delay" for call in get.call_args_list)
    assert job.config["scheduled_at"] == scheduled_at
    assert job.config["retry_delay"] == 7
