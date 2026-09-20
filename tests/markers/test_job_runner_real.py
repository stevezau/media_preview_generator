"""Intro & Credits jobs end to end on the real JobManager, JobGate and dispatcher (fake servers and publishers)."""

import os
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, ItemOutcome, KindHandlers
from media_preview_generator.jobs.dispatcher import reset_dispatcher
from media_preview_generator.markers import job_runner, pipeline, triggers
from media_preview_generator.markers.pipeline import PipelineContext
from media_preview_generator.markers.probe import Chapter, MediaProbe
from media_preview_generator.markers.publishers.base import ItemNotFoundError, PublishError
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.source_counts import DecidedByTally
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType
from media_preview_generator.web.job_gate import JobGate
from media_preview_generator.web.jobs import JobManager, JobStatus
from tests.markers.fakes import FakeRegistry, ready_publisher, server_config
from tests.markers.test_external_ids import EXTRA_SUFFIXES, EXTRAS_FOLDERS

DURATION = 1_321_472
CHAPTERS = (
    Chapter(0, 126_771, "Chapter 1"),
    Chapter(126_771, 157_068, "Intro"),
    Chapter(157_068, 1_295_324, "Chapter 2"),
    Chapter(1_295_324, None, "Credits"),
)


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """Real JobManager + JobGate + shared dispatcher (1 CPU worker); settings, config and GPUs stubbed."""
    reset_dispatcher()
    jm = JobManager(config_dir=str(tmp_path / "config"))
    gate = JobGate(lambda: 1)
    settings = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
    sm = MagicMock(processing_paused=False, gpu_config=[])
    sm.get.side_effect = lambda key, default=None: settings.get(key, default)
    config = SimpleNamespace(cpu_threads=1, gpu_threads=0, scan_workers=4, ffmpeg_path="ffmpeg")
    monkeypatch.setattr(job_runner, "get_job_manager", lambda: jm)
    monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
    monkeypatch.setattr("media_preview_generator.web.jobs.get_job_manager", lambda *a, **k: jm)
    monkeypatch.setattr(job_runner, "get_settings_manager", lambda: sm)
    monkeypatch.setattr(job_runner, "get_job_gate", lambda: gate)
    monkeypatch.setattr(job_runner, "load_config", lambda: config)
    monkeypatch.setattr(job_runner, "_build_selected_gpus", lambda s: [])
    yield SimpleNamespace(jm=jm, gate=gate, settings=settings)
    reset_dispatcher()


def _outcome(jm, job_id):
    return {k: v for k, v in (jm.get_job(job_id).progress.outcome or {}).items() if v}


class TestRetryThroughThePipeline:
    """Files a server hasn't indexed yet: the real pipeline marks them, the job queues a retry, the retry publishes."""

    @pytest.fixture
    def setup(self, engine, tmp_path, monkeypatch):
        folder = tmp_path / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        media = folder / "Rick and Morty (2013) - S01E01 - Pilot.mkv"
        media.write_bytes(b"x" * 100)
        root = str(tmp_path / "tv")
        store = MarkerStore(str(tmp_path / "markers.db"))
        settings = load_global(
            validate_global(
                {
                    "sources": [
                        {"id": "chapters", "enabled": True},
                        {"id": "theintrodb", "enabled": False},
                        {"id": "introdb", "enabled": False},
                        {"id": "skipdb", "enabled": False},
                    ]
                },
                None,
            )[0]
        )

        def make(servers):
            registry = FakeRegistry({sid: server_config(sid, stype, root=root) for sid, stype in servers})
            publishers = {
                sid: ready_publisher("plex_db" if stype is ServerType.PLEX else "jellyfin_bridge")
                for sid, stype in servers
            }

            def build_context(*, registry, config, priority, force=False, recheck_empty_server_markers=False):
                return PipelineContext(
                    registry=registry,
                    config=config,
                    settings=settings,
                    store=store,
                    priority=priority,
                    ffprobe="ffprobe",
                    force=force,
                    clients={},
                    live_config=registry.get_config,  # the fake registry stands in for the saved servers
                    recheck_empty_server_markers=recheck_empty_server_markers,
                )

            monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda config: registry)
            monkeypatch.setattr(job_runner, "build_context", build_context)
            monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
            return registry, publishers

        yield SimpleNamespace(path=str(media), make=make, store=store)
        store.close()

    def _run_pipeline(self, publishers):
        return (
            patch.object(pipeline, "probe_media", return_value=MediaProbe(DURATION, CHAPTERS)),
            patch.object(pipeline, "publisher_for", side_effect=lambda srv, cfg, **kw: publishers[cfg.id]),
        )

    def _retries(self, jm):
        return [j for j in jm.get_all_jobs() if j.config.get("retry_attempt")]

    def _run_retry(self, monkeypatch, retry):
        due = datetime.fromisoformat(retry.config["retry_not_before"])
        monkeypatch.setattr(job_runner, "_utcnow", lambda: due + timedelta(seconds=1))
        job_runner.run_intro_credits_job(retry.id)

    @pytest.mark.parametrize("cause", ["no_item_id", "item_not_found"])
    def test_file_the_server_adds_later_is_published_by_the_retry(self, engine, setup, monkeypatch, cause):
        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        server, publisher = registry.get("jf-1"), publishers["jf-1"]
        if cause == "no_item_id":
            server.resolve_remote_path_to_item_id.return_value = None  # Sonarr imported before Jellyfin scanned
        else:
            publisher.write.side_effect = ItemNotFoundError("Jellyfin has no item item-jf-1 yet")
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = triggers.create_intro_credits_job(
                library_name="Intro & Credits · Pilot", priority=2, source="sonarr", file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(first)

            assert engine.jm.get_job(first).status is JobStatus.COMPLETED
            assert _outcome(engine.jm, first) == {"markers_waiting": 1}
            retries = self._retries(engine.jm)
            assert len(retries) == 1
            retry = retries[0]
            assert retry.kind == JOB_KIND_INTRO_CREDITS and retry.status is JobStatus.PENDING
            assert retry.config["file_paths"] == [setup.path]
            assert (retry.config["retry_attempt"], retry.config["retry_delay"]) == (1, 60)
            assert retry.priority == engine.jm.get_job(first).priority

            # Jellyfin has scanned the file now.
            server.resolve_remote_path_to_item_id.return_value = "item-jf-1"
            publisher.write.side_effect = publisher.succeed
            self._run_retry(monkeypatch, retry)

        assert engine.jm.get_job(retry.id).status is JobStatus.COMPLETED
        assert _outcome(engine.jm, retry.id) == {"markers_published": 1}
        assert publisher.write.call_args.args[0] == "item-jf-1"
        assert [j.id for j in self._retries(engine.jm)] == [retry.id]

    def test_retry_for_one_server_does_not_write_the_server_that_already_has_the_markers(
        self, engine, setup, monkeypatch
    ):
        registry, publishers = setup.make([("plex-1", ServerType.PLEX), ("jf-1", ServerType.JELLYFIN)])
        registry.get("jf-1").resolve_remote_path_to_item_id.return_value = None
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = triggers.create_intro_credits_job(
                library_name="x", priority=2, source="sonarr", file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(first)
            assert _outcome(engine.jm, first) == {"markers_waiting": 1}  # Plex written, Jellyfin waiting
            retries = self._retries(engine.jm)
            assert len(retries) == 1
            registry.get("jf-1").resolve_remote_path_to_item_id.return_value = "item-jf-1"
            self._run_retry(monkeypatch, retries[0])
        assert publishers["plex-1"].write.call_count == 1
        assert publishers["jf-1"].write.call_count == 1
        assert [c.args[0] for c in publishers["jf-1"].write.call_args_list] == ["item-jf-1"]

    def test_the_finished_job_carries_which_sources_decided_its_markers(self, engine, setup, tmp_path):
        _, publishers = setup.make([("plex-1", ServerType.PLEX)])
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job_id = triggers.create_intro_credits_job(
                library_name="x", priority=2, source="manual", file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(job_id)

        expected = {"intro": {"chapters": 1}, "credits": {"chapters": 1}}
        assert engine.jm.get_job(job_id).to_dict()["progress"]["marker_sources"] == expected
        # Stored with the job, so the finished job's summary still shows them after a restart.
        reloaded = JobManager(config_dir=str(tmp_path / "config"))
        assert reloaded.get_job(job_id).progress.marker_sources == expected

    def test_a_failed_server_on_the_file_still_retries_the_server_that_hasnt_indexed_it(self, engine, setup):
        registry, publishers = setup.make([("plex-1", ServerType.PLEX), ("jf-1", ServerType.JELLYFIN)])
        registry.get("jf-1").resolve_remote_path_to_item_id.return_value = None
        publishers["plex-1"].write.side_effect = PublishError("database is locked")
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = triggers.create_intro_credits_job(
                library_name="x", priority=2, source="sonarr", file_paths=[setup.path]
            ).id
            job_runner.run_intro_credits_job(first)
        assert _outcome(engine.jm, first) == {"failed": 1}
        [row] = engine.jm.get_file_results(first)
        assert {s["id"]: s["status"] for s in row["servers"]} == {"plex-1": "failed", "jf-1": "markers_waiting"}
        retries = self._retries(engine.jm)
        assert [r.config["file_paths"] for r in retries] == [[setup.path]]

    def _with_extras(self, episode):
        """Every extra shape next to the episode; the server lists only the episode as an item, like a real one."""
        season = os.path.dirname(episode)
        extras = [os.path.join(season, f"Rick and Morty (2013) - S01E01 - Pilot-{s}.mkv") for s in EXTRA_SUFFIXES]
        extras += [os.path.join(season, folder, "Making Of.mkv") for folder in EXTRAS_FOLDERS]
        for path in extras:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"x" * 100)
        return extras

    def _assert_only_the_episode_was_checked(self, engine, job_id, episode, extras, publisher, server):
        assert engine.jm.get_job(job_id).status is JobStatus.COMPLETED
        assert _outcome(engine.jm, job_id) == {"markers_published": 1, "markers_skipped": len(extras)}
        rows = {r["file"]: r for r in engine.jm.get_file_results(job_id)}
        assert set(rows) == {episode, *extras}
        for path in extras:
            assert (rows[path]["outcome"], rows[path]["reason"]) == (
                "markers_skipped",
                "Extras aren't checked for markers",
            )
            assert not rows[path].get("servers")
        assert rows[episode]["outcome"] == "markers_published"
        assert [c.kwargs["canonical_path"] for c in publisher.write.call_args_list] == [episode]
        assert [c.args[0] for c in server.resolve_remote_path_to_item_id.call_args_list] == [episode]
        assert [j.id for j in engine.jm.get_all_jobs()] == [job_id]  # no retry, no verify

    @pytest.mark.parametrize("source", ["manual", "radarr"])
    def test_a_folder_job_skips_its_extras_and_queues_no_retry_for_them(self, engine, setup, source):
        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        server = registry.get("jf-1")
        server.resolve_remote_path_to_item_id.side_effect = lambda path, **kw: (
            "item-jf-1" if path == setup.path else None
        )
        extras = self._with_extras(setup.path)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job = triggers.create_intro_credits_job(
                library_name="Season 01", priority=2, source=source, file_paths=[os.path.dirname(setup.path)]
            ).id
            job_runner.run_intro_credits_job(job)
        self._assert_only_the_episode_was_checked(engine, job, setup.path, extras, publishers["jf-1"], server)

    def test_a_library_job_skips_the_extras_the_listing_returns(self, engine, setup, monkeypatch):
        from media_preview_generator.jobs import orchestrator

        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        server = registry.get("jf-1")
        server.resolve_remote_path_to_item_id.side_effect = lambda path, **kw: (
            "item-jf-1" if path == setup.path else None
        )
        extras = self._with_extras(setup.path)
        listed = [setup.path, *extras]
        enumerate_items = MagicMock(
            side_effect=lambda candidates, **kw: ([(candidates[0], ProcessableItem(p, "jf-1")) for p in listed], [])
        )
        monkeypatch.setattr(orchestrator, "_enumerate_items_for_servers", enumerate_items)
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            job = triggers.create_intro_credits_job(
                library_name="TV Shows",
                priority=3,
                source="schedule",
                libraries=[{"server_id": "jf-1", "library_id": "1"}],
            ).id
            job_runner.run_intro_credits_job(job)
        assert [cfg.id for cfg in enumerate_items.call_args.args[0]] == ["jf-1"]
        self._assert_only_the_episode_was_checked(engine, job, setup.path, extras, publishers["jf-1"], server)


class TestCheckServersThroughThePipeline(TestRetryThroughThePipeline):
    """Check servers on the real runner and pipeline: only a server that dropped our markers is written again."""

    # The retry tests aren't run again under this class.
    test_file_the_server_adds_later_is_published_by_the_retry = None
    test_retry_for_one_server_does_not_write_the_server_that_already_has_the_markers = None
    test_a_failed_server_on_the_file_still_retries_the_server_that_hasnt_indexed_it = None
    test_a_folder_job_skips_its_extras_and_queues_no_retry_for_them = None
    test_a_library_job_skips_the_extras_the_listing_returns = None

    def _check_servers(self, engine, setup, monkeypatch, publishers):
        from media_preview_generator.markers import reconcile
        from media_preview_generator.markers.publishers.base import MarkerPublisher

        for pub in publishers.values():
            pub.shows_many.side_effect = lambda items, cancel_check=None, pub=pub: MarkerPublisher.shows_many(
                pub, items, cancel_check=cancel_check
            )
        monkeypatch.setattr(reconcile, "publisher_for", lambda server, cfg, **kw: publishers[cfg.id])
        monkeypatch.setattr(reconcile, "get_job_manager", lambda: engine.jm)
        monkeypatch.setattr("media_preview_generator.markers.store.get_marker_store", lambda: setup.store)
        monkeypatch.setattr(triggers, "markers_enabled_anywhere", lambda: True)
        queued = reconcile.run_markers_reconcile()
        assert queued.created
        job_runner.run_intro_credits_job(queued.job_id)
        return engine.jm.get_job(queued.job_id)

    def test_a_server_that_dropped_our_markers_gets_them_again_and_nothing_else_is_written(
        self, engine, setup, monkeypatch
    ):
        from media_preview_generator.markers.publishers.base import Shown

        registry, publishers = setup.make([("plex-1", ServerType.PLEX), ("jf-1", ServerType.JELLYFIN)])
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = triggers.create_intro_credits_job(library_name="TV", priority=2, source="manual",
                                                      file_paths=[setup.path]).id  # fmt: skip
            job_runner.run_intro_credits_job(first)
            assert _outcome(engine.jm, first) == {"markers_published": 1}

            jellyfin = publishers["jf-1"]
            jellyfin.shows.return_value = Shown.MISSING  # a Jellyfin rescan dropped the segments

            def rewrite(item_id, markers, **kwargs):
                jellyfin.last_write_changed = True  # the plugin serves them again
                return jellyfin.project(markers)

            jellyfin.write.side_effect = rewrite
            job = self._check_servers(engine, setup, monkeypatch, publishers)

        assert job.library_name == "Intro & Credits · Check servers" and job.priority == 3
        assert job.status is JobStatus.COMPLETED and job.error is None  # a warning would be kept in error
        assert _outcome(engine.jm, job.id) == {"markers_published": 1}
        [row] = engine.jm.get_file_results(job.id)
        assert row["file"] == setup.path
        assert {s["id"]: s["status"] for s in row["servers"]} == {
            "plex-1": "markers_up_to_date",
            "jf-1": "markers_written",
        }
        assert publishers["plex-1"].write.call_count == 1
        assert publishers["jf-1"].write.call_count == 2
        assert publishers["jf-1"].write.call_args.args[0] == "item-jf-1"
        assert {j.id for j in engine.jm.get_all_jobs()} == {job.id, first}  # no retry, no verify

    @pytest.mark.parametrize("case", ["server-confirms-it-is-gone", "lookup-failed", "cancelled-after-confirming"])
    def test_an_item_the_server_dropped_leaves_check_servers_only_once_the_server_confirms_it(
        self, engine, setup, monkeypatch, case
    ):
        from media_preview_generator.markers.publishers.base import Shown

        registry, publishers = setup.make([("plex-1", ServerType.PLEX), ("jf-1", ServerType.JELLYFIN)])
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = triggers.create_intro_credits_job(library_name="TV", priority=2, source="manual",
                                                      file_paths=[setup.path]).id  # fmt: skip
            job_runner.run_intro_credits_job(first)
            # Jellyfin no longer lists the file; its old item reads back empty.
            publishers["jf-1"].shows.return_value = Shown.MISSING
            registry.get("jf-1").resolve_remote_path_to_item_id.return_value = None
            if case == "lookup-failed":
                publishers["jf-1"].item_missing.side_effect = TimeoutError("read timed out")
            elif case == "cancelled-after-confirming":

                def confirmed_then_cancelled(item_id):
                    (running,) = [j for j in engine.jm.get_running_jobs() if j.config.get("reconcile")]
                    engine.jm.request_cancellation(running.id)
                    return True

                publishers["jf-1"].item_missing.side_effect = confirmed_then_cancelled
            else:
                publishers["jf-1"].item_missing.return_value = True
            job = self._check_servers(engine, setup, monkeypatch, publishers)
            [row] = engine.jm.get_file_results(job.id)
            assert {s["id"]: (s["status"], s.get("reason_code")) for s in row["servers"]} == {
                "plex-1": ("markers_up_to_date", None),
                "jf-1": ("markers_waiting", "not_in_library"),
            }
            publishers["jf-1"].item_missing.assert_called_once_with("item-jf-1")
            assert job.status is (JobStatus.CANCELLED if case == "cancelled-after-confirming" else JobStatus.COMPLETED)
            status = setup.store.get_item_publish_state("jf-1", "item-jf-1").status
            # Marked gone only once its retry was queued: a cancelled run leaves it to be confirmed again.
            assert status == ("gone" if case == "server-confirms-it-is-gone" else "written")
            others = [j for j in engine.jm.get_all_jobs() if j.id not in {first, job.id}]
            if case == "server-confirms-it-is-gone":
                # The file gets the retry any job queues (Jellyfin may not have added its new item yet); the retry
                # lists the file, not Check servers' drift.
                (retry,) = others
                assert (retry.config["retry_attempt"], retry.config["file_paths"]) == (1, [setup.path])
                assert (retry.config["source"], retry.config.get("reconcile")) == ("reconcile", None)
            else:
                assert others == []
            publishers["jf-1"].item_missing.side_effect = None
            publishers["jf-1"].item_missing.return_value = False
            again = self._check_servers(engine, setup, monkeypatch, publishers)
        assert again.status is JobStatus.COMPLETED and again.error is None
        listed_again = [] if case == "server-confirms-it-is-gone" else [setup.path]
        assert [r["file"] for r in engine.jm.get_file_results(again.id)] == listed_again
        assert publishers["jf-1"].write.call_count == 1

    def test_nothing_drifted_completes_at_once(self, engine, setup, monkeypatch):
        registry, publishers = setup.make([("jf-1", ServerType.JELLYFIN)])
        first_patch, second_patch = self._run_pipeline(publishers)
        with first_patch, second_patch:
            first = triggers.create_intro_credits_job(library_name="TV", priority=2, source="manual",
                                                      file_paths=[setup.path]).id  # fmt: skip
            job_runner.run_intro_credits_job(first)
            job = self._check_servers(engine, setup, monkeypatch, publishers)
        assert job.status is JobStatus.COMPLETED and job.error is None
        assert engine.jm.get_file_results(job.id) == []
        logs = engine.jm.get_logs(job.id)
        assert any("INFO - Every server checked still shows what this app published" in line for line in logs), logs
        assert publishers["jf-1"].write.call_count == 1


@pytest.mark.real_job_async
class TestRealJobThread:
    def test_paused_job_hands_its_slot_to_a_high_job_then_finishes(self, engine, monkeypatch):
        release_item = threading.Event()
        checked = []

        def check_fn(item, *, cancel_check=None):
            checked.append(item.canonical_path)
            if item.canonical_path.endswith("slow.mkv"):
                release_item.wait(10)
            return ItemOutcome("markers_published", "ok", [])

        handlers = KindHandlers(
            check_fn=check_fn,
            process_fn=lambda item, **kw: ItemOutcome("failed", "unexpected worker stage"),
            outcome_keys=("markers_published", "failed"),
            check_share=0.25,
        )
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda config: MagicMock())
        # The real job manager stores the context's counts on the job, so those have to be real.
        monkeypatch.setattr(job_runner, "build_context", lambda **kw: MagicMock(decided_by=DecidedByTally()))
        monkeypatch.setattr(job_runner, "kind_handlers", lambda ctx: handlers)
        items = [ProcessableItem(f"/m/{name}.mkv", "") for name in ("a", "slow", "c")]
        monkeypatch.setattr(job_runner, "build_items", lambda cfg, **kw: (items, [], {}))

        job = triggers.create_intro_credits_job(library_name="real thread", priority=3, source="manual")
        jm, gate = engine.jm, engine.gate
        try:
            assert _wait_for(lambda: "/m/slow.mkv" in checked), "the job thread never dispatched"
            assert jm.get_job(job.id).status is JobStatus.RUNNING and gate.snapshot()[0] == 1
            assert jm.request_pause(job.id)
            assert _wait_for(lambda: gate.snapshot()[0] == 0), "paused job kept its slot"
            assert gate.acquire(1, cancel_check=lambda: False) is True  # a HIGH preview job gets in at cap 1
            gate.release(1)
            release_item.set()
            assert jm.request_resume(job.id)
            assert _wait_for(lambda: jm.get_job(job.id).status is JobStatus.COMPLETED), jm.get_job(job.id).status
        finally:
            release_item.set()
        assert _outcome(jm, job.id) == {"markers_published": 3}
        # The job is marked completed before its thread's teardown gives the slot back.
        assert _wait_for(lambda: gate.snapshot()[0] == 0), "the finished job kept its slot"
        assert sorted(r["file"] for r in jm.get_file_results(job.id)) == ["/m/a.mkv", "/m/c.mkv", "/m/slow.mkv"]
        assert _wait_for(lambda: job.id not in job_runner._inflight_jobs)


class TestCreditTextOnTheWorkers:
    """Worker → pipeline → credit text detector on the real runner, gate, dispatcher and worker (spec §6.4 items 4 and
    7): the text is read on the worker's own GPU, a GPU decode failure reruns the file on the CPU in the same worker,
    and a cancel or a text detection failure gives every worker and job slot back. Only the decode (with its start-time
    probe) and the helper pool are faked (the frame and helper boundaries)."""

    @pytest.fixture
    def setup(self, engine, tmp_path, monkeypatch):
        import numpy as np

        from media_preview_generator.markers.credits import detector, frames
        from media_preview_generator.markers.credits.textdet_helper import TextDetState
        from media_preview_generator.markers.pipeline import default_local_detectors

        folder = tmp_path / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        media = folder / "Rick and Morty (2013) - S01E01 - Pilot.mkv"
        media.write_bytes(b"x" * 100)
        store = MarkerStore(str(tmp_path / "markers.db"))
        raw = {
            "detect": {"intro": False, "credits": True},
            "publish_when": "medium",
            "sources": [{"id": source, "enabled": source == "credits_text"} for source in (
                "chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text", "server_markers")],
        }  # fmt: skip
        settings = load_global(validate_global(raw, None)[0])
        registry = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX, root=str(tmp_path / "tv"))})
        publisher = ready_publisher()
        # One GPU worker and no CPU worker: the CPU rerun can only be that same worker's.
        config = SimpleNamespace(cpu_threads=0, gpu_threads=1, scan_workers=4, ffmpeg_path="ffmpeg")
        monkeypatch.setattr(job_runner, "load_config", lambda: config)
        monkeypatch.setattr(job_runner, "_build_selected_gpus", lambda s: [("NVIDIA", "cuda:0", {"name": "Test GPU"})])
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda cfg: registry)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)

        def build_context(*, registry, config, priority, force=False, recheck_empty_server_markers=False):
            return PipelineContext(
                registry=registry,
                config=config,
                settings=settings,
                store=store,
                priority=priority,
                ffprobe="ffprobe",
                force=force,
                clients={},
                local_detectors=default_local_detectors(settings, config, credits_text=TextDetState.AVAILABLE),
                credits_text=TextDetState.AVAILABLE,
                live_config=registry.get_config,
            )

        monkeypatch.setattr(job_runner, "build_context", build_context)
        decodes: list[tuple] = []
        effects: dict = {}

        def decode_rows(path, *, gpu, gpu_device_path, detect_boxes, cancel_check, **kwargs):
            decodes.append((gpu, gpu_device_path))
            effect = effects.get("gpu" if gpu else "cpu")
            if callable(effect):
                effect()
            elif isinstance(effect, BaseException):
                raise effect
            detect_boxes(np.zeros((1, frames.FRAME_H, frames.FRAME_W), np.uint8))
            return []  # no credit roll in the tail: "nothing found"

        pool = MagicMock()
        pool.detect_boxes.return_value = [()]
        monkeypatch.setattr(frames, "decode_rows", decode_rows)
        # The detector reads the container's start time and which packets its keyframe pass drops once per file before
        # its decodes: part of the faked decode.
        monkeypatch.setattr(frames, "container_start_s", lambda *args, **kwargs: 0.0)
        monkeypatch.setattr(frames, "keyframe_thinning", lambda *args, **kwargs: frames.KeyframeThinning())
        monkeypatch.setattr(detector, "get_textdet_pool", lambda: pool)
        yield SimpleNamespace(path=str(media), store=store, publisher=publisher, decodes=decodes, effects=effects,
                              pool=pool)  # fmt: skip
        store.close()

    def _run(self, engine, setup):
        with (
            patch.object(pipeline, "probe_media", return_value=MediaProbe(DURATION, ())),
            patch.object(pipeline, "publisher_for", return_value=setup.publisher),
        ):
            job = triggers.create_intro_credits_job(library_name="TV", priority=2, source="manual",
                                                    file_paths=[setup.path])  # fmt: skip
            job_runner.run_intro_credits_job(job.id)
        return engine.jm.get_job(job.id)

    @staticmethod
    def _worker():
        from media_preview_generator.jobs.dispatcher import get_dispatcher

        (worker,) = get_dispatcher().worker_pool._snapshot_workers()
        return worker

    def _released(self, engine):
        worker = self._worker()
        return engine.gate.snapshot()[0] == 0 and _wait_for(lambda: not worker.is_busy)

    def test_the_worker_reads_the_text_on_its_gpu(self, engine, setup):
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0")]
        assert setup.pool.detect_boxes.call_args.kwargs == {"gpu": "NVIDIA", "gpu_device_path": "cuda:0"}
        assert job.status is JobStatus.COMPLETED and _outcome(engine.jm, job.id) == {"markers_none": 1}
        assert self._worker().fallback_active is False
        assert self._released(engine)

    def test_a_gpu_decode_failure_reruns_the_file_on_the_cpu_in_the_same_worker(self, engine, setup):
        from media_preview_generator.markers.credits import detector, frames
        from media_preview_generator.markers.models import Source

        setup.effects["gpu"] = frames.GpuDecodeError("the GPU decoded no frames from S01E01.mkv")
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0"), (None, None)]
        assert setup.pool.detect_boxes.call_args.kwargs == {"gpu": None, "gpu_device_path": None}
        worker = self._worker()
        assert worker.fallback_active is True and "GPU decoded no frames" in worker.fallback_reason
        rec = setup.store.get_file(setup.path)
        assert setup.store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION
        # Counted once, as the CPU rerun ended.
        assert job.status is JobStatus.COMPLETED and _outcome(engine.jm, job.id) == {"markers_none": 1}
        [row] = engine.jm.get_file_results(job.id)
        assert row["worker"] == "GPU Worker 1 (Test GPU)"
        assert self._released(engine)

    def test_a_cancel_during_the_decode_frees_the_worker_and_the_slot_without_a_cpu_rerun(self, engine, setup):
        from media_preview_generator.markers.credits import frames

        def cancel_then_stop():
            (running,) = engine.jm.get_running_jobs()
            engine.jm.request_cancellation(running.id)
            raise frames.DecodeCancelledError("cancelled while decoding S01E01.mkv")  # what run_decode then raises

        setup.effects["gpu"] = cancel_then_stop
        job = self._run(engine, setup)
        assert setup.decodes == [("NVIDIA", "cuda:0")]
        assert job.status is JobStatus.CANCELLED
        assert setup.publisher.write.call_count == 0
        assert self._released(engine)

    @pytest.mark.parametrize("where", ["gpu-helper", "cpu-helper-after-gpu-decode-failure"])
    def test_a_text_detection_failure_leaves_the_file_unanswered_and_frees_every_slot(self, engine, setup, where):
        from media_preview_generator.markers.credits import frames
        from media_preview_generator.markers.credits.textdet_helper import TextDetUnavailableError
        from media_preview_generator.markers.models import Source

        # The pool itself moves a crashed GPU helper's device to the CPU; what reaches the detector is the CPU helper
        # failing too (``TextDetectorPool.detect_boxes``).
        setup.pool.detect_boxes.side_effect = TextDetUnavailableError("Text detection failed: the helper exited (-9)")
        if where != "gpu-helper":
            setup.effects["gpu"] = frames.GpuDecodeError("ffmpeg exited 1 decoding S01E01.mkv on the GPU")
        job = self._run(engine, setup)
        rec = setup.store.get_file(setup.path)
        assert setup.store.evidence_version(rec.id, Source.CREDITS_TEXT) is None  # asked again next run
        assert job.status is JobStatus.COMPLETED and _outcome(engine.jm, job.id) == {"markers_none": 1}
        assert self._released(engine)
