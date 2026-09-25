"""``_run_recently_added_multi_server``: the files a scheduled Recently Added scan lists get their Intro & Credits job.

Webhooks queue a follow-up for their files; scheduled Recently Added scans queued none, so files that arrived without a
webhook got previews and never got markers. The scan now queues the same deduped follow-up
(``markers.triggers.submit_follow_ups``) for the files it listed, before it dispatches them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.jobs import orchestrator
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType

FOLLOW_UPS = "media_preview_generator.markers.triggers.submit_follow_ups"


def _cfg(sid, stype):
    return SimpleNamespace(id=sid, name=sid.upper(), type=stype, enabled=True)


@pytest.fixture
def scan(monkeypatch):
    """A registry of a Plex and an Emby server whose Recently Added listings are canned; the dispatch is recorded."""
    plex, emby = _cfg("plex-1", ServerType.PLEX), _cfg("emby-1", ServerType.EMBY)
    registry = MagicMock()
    registry.configs.return_value = [plex, emby]
    monkeypatch.setattr(orchestrator, "_build_multi_server_registry", lambda config: registry)
    listed = {
        "plex-1": [ProcessableItem("/media/tv/S/a.mkv", "plex-1", {"plex-1": "11"})],
        "emby-1": [ProcessableItem("/media/tv/S/b.mkv", "emby-1", {"emby-1": "22"})],
    }
    seen_lookback: list[int] = []

    def processor_for(stype):
        processor = MagicMock()

        def scan_recently_added(server_cfg, *, lookback_hours, library_ids):
            seen_lookback.append(lookback_hours)
            return iter(listed[server_cfg.id])

        processor.scan_recently_added.side_effect = scan_recently_added
        return processor

    monkeypatch.setattr("media_preview_generator.processing.get_processor_for", processor_for)
    order: list[str] = []
    dispatch = MagicMock(side_effect=lambda items, **kw: order.append("dispatch") or {"generated": len(items)})
    monkeypatch.setattr(orchestrator, "_dispatch_processable_items", dispatch)
    return SimpleNamespace(listed=listed, dispatch=dispatch, order=order, lookback=seen_lookback)


def _run(**kwargs):
    params = {"selected_gpus": [], "lookback_hours": 1.0, "job_id": "ra-1"}
    params.update(kwargs)
    return orchestrator._run_recently_added_multi_server(SimpleNamespace(server_id_filter=None), **params)


@pytest.mark.parametrize("pin", [None, "emby-1"])
def test_the_listed_files_get_their_follow_ups_before_the_dispatch(scan, pin):
    with patch(FOLLOW_UPS, side_effect=lambda **kw: scan.order.append("follow_ups") or ["ic-1"]) as follow_ups:
        counts = _run(server_id_filter=pin)

    listed = scan.listed["plex-1"] + scan.listed["emby-1"] if pin is None else scan.listed["emby-1"]
    follow_ups.assert_called_once_with(preview_job_id="ra-1", items=listed, source="recently_added", pin=pin)
    assert scan.order == ["follow_ups", "dispatch"]
    assert counts == {"generated": len(listed)}


def test_a_scan_without_a_job_queues_no_follow_up(scan):
    with patch(FOLLOW_UPS) as follow_ups:
        _run(job_id=None)
    follow_ups.assert_not_called()
    scan.dispatch.assert_called_once()


def test_an_empty_window_queues_no_follow_up(scan):
    scan.listed["plex-1"].clear()
    scan.listed["emby-1"].clear()
    with patch(FOLLOW_UPS) as follow_ups:
        _run()
    follow_ups.assert_not_called()
    scan.dispatch.assert_not_called()


def test_a_follow_up_that_cant_be_queued_never_costs_the_previews(scan):
    from loguru import logger

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="ERROR")
    try:
        with patch(FOLLOW_UPS, side_effect=RuntimeError("boom")):
            counts = _run()
    finally:
        logger.remove(sink)
    scan.dispatch.assert_called_once()
    assert counts == {"generated": 2}
    assert any("Could not queue the Intro & Credits jobs" in m for m in messages)


@pytest.mark.parametrize(("hours", "listed"), [(0.25, 1.0), (1.0, 1.0), (1.5, 1.5), (4.02, 4.02)])
def test_a_partial_hour_of_the_window_is_listed_not_dropped(scan, hours, listed):
    with patch(FOLLOW_UPS):
        _run(lookback_hours=hours)
    assert set(scan.lookback) == {listed}


def test_classify_a_recently_added_job():
    config = SimpleNamespace(
        webhook_paths=None,
        webhook_source="scheduled_recently_added",
        recently_added_since=datetime(2026, 9, 25, tzinfo=UTC),
    )
    assert orchestrator._classify_processing_mode(config) == "recently_added"
    # Its retries run their waiting files as paths.
    config.webhook_paths = ["/media/tv/S/a.mkv"]
    assert orchestrator._classify_processing_mode(config) == "webhook_paths"
    # A webhook job that lost its paths is still refused, never scanned.
    assert (
        orchestrator._classify_processing_mode(
            SimpleNamespace(webhook_paths=None, webhook_source="sonarr", recently_added_since=None)
        )
        == "refuse_malformed_webhook"
    )


def test_markers_follow_ups_publish_where_each_files_previews_do(tmp_path, monkeypatch):
    """End to end through the real triggers: the Plex-listed file's markers fan out; the Emby-listed one's go to Emby
    only (``resolve_per_item_pin``), as their previews do."""
    from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
    from media_preview_generator.markers import triggers
    from media_preview_generator.web.jobs import JobManager

    servers = [
        {
            "id": sid,
            "type": stype,
            "name": sid,
            "enabled": True,
            "markers": markers,
            "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/media/tv"], "enabled": True}],
        }
        for sid, stype, markers in (
            ("plex-1", "plex", {"enabled": True, "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00"}}),
            ("emby-1", "emby", {"enabled": True}),
        )
    ]
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: servers if key == "media_servers" else default
    monkeypatch.setattr(triggers, "get_settings_manager", lambda: sm)
    jm = JobManager(config_dir=str(tmp_path))
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
    preview = jm.create_job(library_name="Recently added: TV", config={"source": "scheduled_recently_added"})

    orchestrator._queue_intro_credits_follow_ups(
        preview.id,
        [
            ProcessableItem("/media/tv/S/a.mkv", "plex-1", {"plex-1": "11"}),
            ProcessableItem("/media/tv/S2/b.mkv", "emby-1", {"emby-1": "22"}),
        ],
        None,
    )

    jobs = {tuple(j.config["file_paths"]): j.config for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS}
    assert set(jobs) == {("/media/tv/S/a.mkv",), ("/media/tv/S2/b.mkv",)}
    assert jobs[("/media/tv/S/a.mkv",)].get("server_id") is None
    assert jobs[("/media/tv/S2/b.mkv",)]["server_id"] == "emby-1"
    assert all(cfg["follows_job_id"] == preview.id and cfg["source"] == "recently_added" for cfg in jobs.values())
