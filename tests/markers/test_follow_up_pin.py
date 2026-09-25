"""Markers from a pinned trigger follow the pin, as its previews do (``jobs.worker.resolve_per_item_pin``).

A webhook pinned to one server (``/api/webhooks/server/<id>``, ``?server_id=``), an Emby or Jellyfin webhook through
``/api/webhooks/incoming`` and a scheduled "Recently added" scan publish previews to one server only; the Intro &
Credits job that follows them carries that server as ``server_id`` and publishes there only. Unpinned jobs publish to
every server with Intro & Credits on, as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import job_runner, triggers
from media_preview_generator.markers.outcomes import FileOutcome, ServerStatus
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType
from tests.markers import test_job_runner, test_pipeline, test_triggers
from tests.markers.fakes import ready_publisher
from tests.markers.test_job_runner import NOT_IN_LIBRARY_ROW, _row
from tests.markers.test_job_runner import _item as _job_item
from tests.markers.test_pipeline import CHAPTERS_BOTH, _ctx, _probe, _registry, _run
from tests.markers.test_triggers import _server

# Fixtures shared with the job runner, pipeline and trigger tests.
env = test_job_runner.env
media, store = test_pipeline.media, test_pipeline.store
settings = test_triggers.settings


class TestPipelinePublishesWhereTheJobIsPinned:
    """The matrix: each vendor pinned, no pin, and a pin to a server with Intro & Credits off."""

    SERVERS = (ServerType.PLEX, ServerType.JELLYFIN, ServerType.EMBY)

    def _publishers(self):
        return {
            "plex-1": ready_publisher(),
            "jellyfin-1": ready_publisher("jellyfin_bridge"),
            "emby-1": ready_publisher("emby_bridge"),
        }

    @pytest.mark.parametrize("stage", ["check", "worker"])
    @pytest.mark.parametrize(
        ("pin", "published"),
        [
            ("plex-1", ["plex-1"]),
            ("emby-1", ["emby-1"]),
            ("jellyfin-1", ["jellyfin-1"]),
            (None, ["plex-1", "jellyfin-1", "emby-1"]),
        ],
        ids=["plex-pin", "emby-pin", "jellyfin-pin", "no-pin"],
    )
    def test_only_the_pinned_server_gets_a_row_and_a_write(self, store, media, pin, published, stage):
        ctx = _ctx(store, _registry(media, *self.SERVERS))
        ctx.config = SimpleNamespace(server_id_filter=pin)
        publishers = self._publishers()

        out, _ = _run(ctx, media, publishers, probe=_probe(CHAPTERS_BOTH), stage=stage)

        assert [r["server_id"] for r in out.publisher_rows] == published
        assert {r["status"] for r in out.publisher_rows} == {ServerStatus.WRITTEN.value}
        for sid, pub in publishers.items():
            assert pub.write.called is (sid in published), sid
        rec = store.get_file(media)
        assert {sid for sid in publishers if store.get_publish_state(rec.id, sid) is not None} == set(published)

    def test_a_pin_to_a_server_with_markers_off_publishes_nowhere(self, store, media):
        reg = _registry(media, *self.SERVERS)
        reg.configs_by_id["emby-1"].markers["enabled"] = False
        ctx = _ctx(store, reg)
        ctx.config = SimpleNamespace(server_id_filter="emby-1")
        publishers = self._publishers()

        out, probe = _run(ctx, media, publishers, probe=_probe(CHAPTERS_BOTH))

        assert out.outcome_key == FileOutcome.NO_OWNERS.value
        assert "EMBY-1" in out.message
        assert out.publisher_rows == []
        assert not any(pub.write.called for pub in publishers.values())
        probe.assert_not_called()

    @pytest.mark.parametrize("config", [None, MagicMock(), SimpleNamespace(server_id_filter="")])
    def test_no_usable_pin_publishes_to_every_owner(self, store, media, config):
        # publish_now passes no Config, and most tests pass a MagicMock: neither is a pin.
        ctx = _ctx(store, _registry(media, *self.SERVERS))
        ctx.config = config
        out, _ = _run(ctx, media, self._publishers(), probe=_probe(CHAPTERS_BOTH))
        assert [r["server_id"] for r in out.publisher_rows] == ["plex-1", "jellyfin-1", "emby-1"]


class TestCreateJobStoresThePin:
    @pytest.mark.parametrize(("server_id", "stored"), [("emby-1", {"server_id": "emby-1"}), (None, {}), ("", {})])
    def test_server_id_is_stored_only_when_pinned(self, monkeypatch, server_id, stored):
        jm = MagicMock()
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async"):
            triggers.create_intro_credits_job(
                library_name="x", priority=2, source="emby", file_paths=["/media/tv/a.mkv"], server_id=server_id
            )
        config = jm.create_job.call_args.kwargs["config"]
        assert {k: v for k, v in config.items() if k == "server_id"} == stored


class TestSubmitWithAPin:
    @pytest.fixture
    def create(self, settings, monkeypatch):
        settings["media_servers"] = [
            _server("plex-1", "plex", markers={"enabled": False}),
            _server("jf-1", "jellyfin"),
            _server("emby-1", "emby"),
        ]
        jm = MagicMock()
        jm.get_job.return_value = MagicMock(library_name="S01E01", priority=2)
        jm.get_pending_jobs.return_value = []
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        create = MagicMock(return_value=MagicMock(id="ic-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        return create

    @pytest.mark.parametrize("pin", ["jf-1", "emby-1", None])
    def test_the_pin_reaches_the_job(self, create, pin):
        out = triggers.submit_webhook_follow_up(
            preview_job_id="prev-1", paths=["/media/tv/S/S01E01.mkv"], source="emby", server_id=pin
        )
        assert out == "ic-1"
        assert create.call_args.kwargs["server_id"] == pin
        assert create.call_args.kwargs["file_paths"] == ["/media/tv/S/S01E01.mkv"]

    @pytest.mark.parametrize("pin", ["plex-1", "gone-1"], ids=["markers-off", "not-configured"])
    def test_a_pin_to_a_server_that_takes_no_markers_queues_nothing(self, create, pin):
        out = triggers.submit_webhook_follow_up(
            preview_job_id="prev-1", paths=["/media/tv/S/S01E01.mkv"], source="plex", server_id=pin
        )
        assert out is None
        create.assert_not_called()

    def test_a_pinned_server_that_doesnt_hold_the_file_queues_nothing(self, create, settings):
        settings["media_servers"][2]["libraries"] = [
            {"id": "9", "name": "Films", "remote_paths": ["/media/movies"], "enabled": True}
        ]
        out = triggers.submit_webhook_follow_up(
            preview_job_id="prev-1", paths=["/media/tv/S/S01E01.mkv"], source="emby", server_id="emby-1"
        )
        assert out is None
        create.assert_not_called()


class TestFollowUpsForItems:
    """``submit_follow_ups`` resolves each item's pin exactly as the preview worker does, then queues one follow-up per
    pin."""

    @pytest.fixture
    def submit(self, settings, monkeypatch):
        settings["media_servers"] = [
            _server("plex-1", "plex"),
            _server("jf-1", "jellyfin"),
            _server("emby-1", "emby"),
        ]
        calls = []

        def fake_submit(**kwargs):
            calls.append(kwargs)
            return f"ic-{len(calls)}"

        monkeypatch.setattr(triggers, "submit_webhook_follow_up", fake_submit)
        return calls

    @staticmethod
    def _item(path, origin="", hints=None):
        return ProcessableItem(canonical_path=path, server_id=origin, item_id_by_server=dict(hints or {}))

    @pytest.mark.parametrize(
        ("origin", "pin", "expected"),
        [
            ("plex-1", None, None),  # a Plex originator fans out
            ("emby-1", None, "emby-1"),  # a non-Plex originator publishes to itself
            ("jf-1", None, "jf-1"),
            ("", None, None),  # no originator (a Sonarr path): fan out
            ("emby-1", "plex-1", "plex-1"),  # an explicit pin always wins
            ("", "jf-1", "jf-1"),
        ],
    )
    def test_each_cell_of_the_pin_rule(self, submit, origin, pin, expected):
        item = self._item("/media/tv/S/a.mkv", origin, {origin: "7"} if origin else None)
        ids = triggers.submit_follow_ups(preview_job_id="prev-1", items=[item], source="emby", pin=pin)
        assert ids == ["ic-1"]
        (call,) = submit
        assert call == {
            "preview_job_id": "prev-1",
            "paths": ["/media/tv/S/a.mkv"],
            "source": "emby",
            "server_id": expected,
            "item_id_hints": {"/media/tv/S/a.mkv": {origin: "7"}} if origin else None,
        }

    def test_items_of_different_pins_get_one_follow_up_each(self, submit):
        items = [
            self._item("/media/tv/S/a.mkv", "plex-1"),
            self._item("/media/tv/S/b.mkv", "emby-1", {"emby-1": "5"}),
            self._item("/media/tv/S/c.mkv", "plex-1"),
        ]
        ids = triggers.submit_follow_ups(preview_job_id="prev-1", items=items, source="recently_added", pin=None)
        assert ids == ["ic-1", "ic-2"]
        assert [(c["server_id"], c["paths"], c["item_id_hints"]) for c in submit] == [
            (None, ["/media/tv/S/a.mkv", "/media/tv/S/c.mkv"], None),
            ("emby-1", ["/media/tv/S/b.mkv"], {"/media/tv/S/b.mkv": {"emby-1": "5"}}),
        ]

    def test_no_items_queue_nothing(self, submit):
        assert triggers.submit_follow_ups(preview_job_id="prev-1", items=[], source="x", pin=None) == []
        assert submit == []


class TestWaitingFollowUpsCoverOnlyWhatTheyPublish:
    """A waiting follow-up covers a request when it publishes at least where the request would: an unpinned one
    covers any request, a pinned one only a request with the same pin. Episodes join only a follow-up with the same
    pin."""

    @pytest.fixture
    def jm(self, settings, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        settings["media_servers"] = [_server("jf-1", "jellyfin"), _server("emby-1", "emby")]
        manager = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: manager)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        return manager

    def _waiting(self, jm, paths, pin):
        config = {"kind": JOB_KIND_INTRO_CREDITS, "file_paths": paths, "follows_job_id": "prev-0", "force": False}
        if pin:
            config["server_id"] = pin
        return jm.create_job(library_name="waiting", kind=JOB_KIND_INTRO_CREDITS, config=config)

    def _new(self, jm, before):
        return [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS and j.id not in before]

    @pytest.mark.parametrize(
        ("waiting_pin", "request_pin", "covered"),
        [
            (None, None, True),
            (None, "emby-1", True),  # the unpinned job publishes to Emby too
            ("emby-1", "emby-1", True),
            ("emby-1", None, False),  # the pinned job would leave every other server out
            ("emby-1", "jf-1", False),
        ],
    )
    def test_the_same_file(self, jm, waiting_pin, request_pin, covered):
        waiting = self._waiting(jm, ["/media/tv/S/Season 01/S01E01.mkv"], waiting_pin)
        out = triggers.submit_webhook_follow_up(
            preview_job_id="prev-2", paths=["/media/tv/S/Season 01/S01E01.mkv"], source="emby", server_id=request_pin
        )
        new = self._new(jm, {waiting.id})
        if covered:
            assert out is None and new == []
        else:
            assert [j.id for j in new] == [out]
            assert new[0].config.get("server_id") == request_pin

    @pytest.mark.parametrize(
        ("waiting_pin", "request_pin", "joins"),
        [(None, None, True), ("emby-1", "emby-1", True), (None, "emby-1", False), ("emby-1", None, False)],
    )
    def test_another_episode_of_the_season(self, jm, waiting_pin, request_pin, joins):
        waiting = self._waiting(jm, ["/media/tv/S/Season 01/S01E01.mkv"], waiting_pin)
        out = triggers.submit_webhook_follow_up(
            preview_job_id="prev-2", paths=["/media/tv/S/Season 01/S01E02.mkv"], source="emby", server_id=request_pin
        )
        listed = jm.get_job(waiting.id).config["file_paths"]
        if joins:
            assert out == waiting.id and self._new(jm, {waiting.id}) == []
            assert listed == ["/media/tv/S/Season 01/S01E01.mkv", "/media/tv/S/Season 01/S01E02.mkv"]
        else:
            (new,) = self._new(jm, {waiting.id})
            assert out == new.id and new.config.get("server_id") == request_pin
            assert listed == ["/media/tv/S/Season 01/S01E01.mkv"]


class TestJobRunnerKeepsThePin:
    def test_the_jobs_pin_is_the_pipelines_publish_scope(self, env):
        env.job.config = {"libraries": [], "file_paths": ["/data/tv/a.mkv"], "source": "emby", "server_id": "emby-1"}
        with patch.object(job_runner, "build_items", return_value=([_job_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.build_context.call_args.kwargs["config"] is env.config
        assert env.config.server_id_filter == "emby-1"

    def test_an_unpinned_job_has_no_scope(self, env):
        env.config.server_id_filter = "left-over"
        env.job.config = {"libraries": [], "file_paths": ["/data/tv/a.mkv"], "source": "sonarr"}
        with patch.object(job_runner, "build_items", return_value=([_job_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        assert env.config.server_id_filter is None

    @pytest.fixture
    def later(self, env, monkeypatch):
        saved = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
        env.sm.get.side_effect = lambda key, default=None: saved.get(key, default)
        env.job.library_name = "S01E01"
        create = MagicMock(side_effect=lambda **kw: MagicMock(id="later-1", config={}))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        set_cb = MagicMock()
        monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)
        rows = []

        def during_wait(timeout=None):
            for row in rows:
                set_cb.call_args_list[0].args[0]("/m/a.mkv", row["status"], "", "Lookup", servers=[row])
            return True

        env.tracker.wait.side_effect = during_wait
        return SimpleNamespace(create=create, rows=rows)

    @pytest.mark.parametrize("pin", ["emby-1", None])
    @pytest.mark.parametrize(
        ("row", "name"),
        [(NOT_IN_LIBRARY_ROW, "Retry: "), (_row("markers_written", "2 marker(s)", verify_later=True), "Verify: ")],
        ids=["retry", "verify"],
    )
    def test_its_retry_and_verify_jobs_publish_where_it_does(self, env, later, pin, row, name):
        env.job.config = {"libraries": [], "file_paths": ["/data/tv/a.mkv"], "source": "emby"}
        if pin:
            env.job.config["server_id"] = pin
        later.rows.append(row)
        with patch.object(job_runner, "build_items", return_value=([_job_item("/m/a.mkv")], [], {})):
            job_runner.run_intro_credits_job("j1")
        (call,) = later.create.call_args_list
        assert call.kwargs["library_name"].startswith(name)
        assert call.kwargs.get("server_id") == pin

    @pytest.mark.parametrize(
        ("job_pin", "waiting_pin", "joins"),
        [(None, None, True), ("emby-1", "emby-1", True), ("emby-1", None, False), (None, "emby-1", False)],
    )
    def test_its_theintrodb_recheck_joins_only_a_recheck_with_the_same_pin(
        self, monkeypatch, job_pin, waiting_pin, joins
    ):
        from datetime import UTC, datetime

        jm = MagicMock()
        config = {
            "source": job_runner.BUDGET_RECHECK_SOURCE,
            "file_paths": ["/m/x.mkv"],
            "retry_not_before": "2026-09-25T00:05:00+00:00",
        }
        if waiting_pin:
            config["server_id"] = waiting_pin
        jm.get_pending_jobs.return_value = [MagicMock(id="w1", kind=JOB_KIND_INTRO_CREDITS, config=config)]
        jm.update_job_config_if_pending.return_value = True
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
        monkeypatch.setattr(job_runner, "_utcnow", lambda: datetime(2026, 9, 24, 5, 0, tzinfo=UTC))
        ctx = SimpleNamespace(
            take_budget_rechecks=lambda: (["/m/a.mkv"], datetime(2026, 9, 24, 4, 42, tzinfo=UTC)),
            settings=SimpleNamespace(source_enabled=lambda source_id: True),
        )
        job = MagicMock(id="j1", config={"server_id": job_pin} if job_pin else {})
        with patch.object(triggers, "create_intro_credits_job", return_value=MagicMock(id="r1")) as create:
            job_runner._queue_budget_recheck(job, ctx)
        if joins:
            create.assert_not_called()
            assert jm.update_job_config_if_pending.call_args.args[1]["file_paths"] == ["/m/x.mkv", "/m/a.mkv"]
        else:
            jm.update_job_config_if_pending.assert_not_called()
            assert create.call_args.kwargs.get("server_id") == job_pin
