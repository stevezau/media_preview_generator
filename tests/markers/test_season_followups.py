"""Season matching waits for explicit runs; webhook episode grouping remains available."""

from __future__ import annotations

import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import job_runner, triggers
from media_preview_generator.markers.audio import season
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from media_preview_generator.web.jobs import JobManager, JobStatus
from tests.markers import test_job_runner, test_job_runner_real, test_triggers
from tests.markers.audio import test_season
from tests.markers.audio.test_season import (
    SEASON_RAW,
    _Audio,
    _Chapters,
    _evidence,
    _intro_decision,
    _season_ctx,
    _write,
)
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import _run

# Fixtures shared with the runner, trigger and detector tests.
env, _item = test_job_runner.env, test_job_runner._item
engine = test_job_runner_real.engine
settings, _server = test_triggers.settings, test_triggers._server
store, show = test_season.store, test_season.show

SHOW = "/media/tv/Show (2020) {tvdb-1}"
S1, S2 = f"{SHOW}/Season 01", f"{SHOW}/Season 02"


def ep(season_folder: str, e: int) -> str:
    n = int(season_folder[-2:])
    return f"{season_folder}/Show (2020) - S{n:02d}E{e:02d}.mkv"


class TestNoAutomaticSeasonFollowUps:
    @pytest.mark.parametrize("source", ["manual", "schedule", "sonarr", "inspector", "inspector_season", "season"])
    @pytest.mark.parametrize("ending", ["completed", "cancelled", "failed", "empty", "already-finished"])
    def test_sibling_requests_never_create_another_job(self, env, monkeypatch, source, ending):
        own, sibling = ep(S1, 1), ep(S1, 2)
        env.job.config = {"source": source, "file_paths": [own], "late_requests": {sibling: 7}}
        env.ctx.take_followups.return_value = [sibling]
        env.ctx.take_changed_siblings_left_out.return_value = [own]
        env.ctx.ran_since.return_value = False
        items = [] if ending == "empty" else [_item(own)]
        if ending == "cancelled":
            env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        elif ending == "failed":
            env.dispatcher.submit_items.side_effect = RuntimeError("boom")
        elif ending == "already-finished":
            monkeypatch.setattr(
                job_runner,
                "_skip_finished_before_restart",
                lambda *args: ([], {"markers_published": 1}, set(), {}),
            )
        with (
            patch.object(job_runner, "build_items", return_value=(items, [], {})),
            patch.object(triggers, "create_intro_credits_job") as create,
        ):
            job_runner.run_intro_credits_job("j1")
        create.assert_not_called()
        env.jm.create_job.assert_not_called()
        if ending == "cancelled":
            env.jm.cancel_job.assert_called_once_with("j1")
        else:
            assert env.jm.complete_job.call_args.args == ("j1",)
            if ending == "failed":
                assert env.jm.complete_job.call_args.kwargs["error"] == "RuntimeError: boom"
            else:
                assert not env.jm.complete_job.call_args.kwargs.get("error")

    @pytest.mark.parametrize(("source", "retried"), [("season", False), ("sonarr", True)])
    def test_a_season_job_doesnt_retry_a_file_missing_from_disk(self, env, monkeypatch, source, retried):
        path = ep(S1, 1)
        env.job.config = {"file_paths": [path], "source": source}
        captured = {}
        monkeypatch.setattr(job_runner, "set_file_result_callback", lambda fn, job_id: captured.setdefault("fn", fn))

        def submit(**kwargs):
            captured["fn"](path, "skipped_file_not_found", "File not found on disk", "worker")
            return env.tracker

        env.dispatcher.submit_items.side_effect = submit
        env.ctx.take_followups.return_value = []
        with (
            patch.object(job_runner, "build_items", return_value=([_item(path)], [], {path: path})),
            patch.object(job_runner, "_queue_retry") as retry,
        ):
            job_runner.run_intro_credits_job("j1")
        assert retry.called is retried

    @pytest.mark.parametrize(("source", "verified"), [("season", False), ("sonarr", True)])
    def test_a_season_job_queues_no_verify(self, env, monkeypatch, source, verified):
        path = ep(S1, 1)
        env.job.config = {"file_paths": [path], "source": source}
        captured = {}
        monkeypatch.setattr(job_runner, "set_file_result_callback", lambda fn, job_id: captured.setdefault("fn", fn))

        def submit(**kwargs):
            row = {"server_id": "jf-1", "status": "markers_written", "verify_later": True}
            captured["fn"](path, "markers_published", "", "worker", servers=[row])
            return env.tracker

        env.dispatcher.submit_items.side_effect = submit
        env.ctx.take_followups.return_value = []
        with (
            patch.object(job_runner, "build_items", return_value=([_item(path)], [], {path: path})),
            patch.object(job_runner, "_queue_verify") as verify,
        ):
            job_runner.run_intro_credits_job("j1")
        assert verify.called is verified


class TestExplicitSeasonRefresh:
    def test_next_selected_run_refreshes_an_episode_after_its_sibling_changed(self, store, show):
        e1, e2 = show(1, 2)
        with _Audio():
            for path in (e1, e2):
                _run(_season_ctx(store, path), path, {"plex-1": ready_publisher()}, stage="process")
        assert _evidence(store, e1, season.Source.SEASON_AUDIO)[0].origin == "1/1"
        _write(e2, 999)
        ctx = _season_ctx(store, e1)
        with _Audio():
            for path in (e1, e2):
                _run(ctx, path, {"plex-1": ready_publisher()}, stage="process")
        assert _evidence(store, e1, season.Source.SEASON_AUDIO) == []
        assert _evidence(store, e2, season.Source.SEASON_AUDIO)[0].origin == "1/1"
        with _Audio():
            _run(_season_ctx(store, e1), e1, {"plex-1": ready_publisher()}, stage="process")
        assert _evidence(store, e1, season.Source.SEASON_AUDIO)[0].origin == "1/1"


class TestFollowUpConfigIsReadWhenItsFilesAreListed:
    def test_episodes_that_joined_while_waiting_are_listed_and_the_job_is_sealed(self, env, monkeypatch):
        env.job.config = {"file_paths": [ep(S1, 1)], "follows_job_id": "p1", "source": "plex"}

        def joined_while_waiting(job_id, follows_job_id, cancel_check):
            env.job.config = {**env.job.config, "file_paths": [ep(S1, 1), ep(S1, 2)]}
            return True

        monkeypatch.setattr(job_runner, "wait_for_preceding_job", joined_while_waiting)
        env.ctx.take_followups.return_value = []
        with patch.object(job_runner, "build_items", return_value=([_item(ep(S1, 1))], [], {})) as build:
            job_runner.run_intro_credits_job("j1")
        listed = build.call_args.args[0]
        assert listed["file_paths"] == [ep(S1, 1), ep(S1, 2)] and listed[job_runner.FILES_SEALED] is True
        # Only the seal is written: a key another thread set on the job meanwhile stays.
        env.jm.merge_job_config.assert_called_once_with("j1", {job_runner.FILES_SEALED: True})
        env.jm.update_job_config.assert_not_called()

    def test_a_season_job_reads_the_episodes_later_requests_added_and_is_sealed(self, env):
        env.job.config = {"file_paths": [ep(S1, 1)], "source": "season"}
        added = {"file_paths": [ep(S1, 1), ep(S1, 2)], "source": "season"}
        env.jm.get_job.side_effect = [env.job, SimpleNamespace(config=added)]
        env.ctx.take_followups.return_value = []
        with patch.object(job_runner, "build_items", return_value=([_item(ep(S1, 1))], [], {})) as build:
            job_runner.run_intro_credits_job("j1")
        assert build.call_args.args[0] == {**added, job_runner.FILES_SEALED: True}

    def test_jobs_that_follow_no_preview_job_are_not_sealed(self, env):
        env.ctx.take_followups.return_value = []
        with patch.object(job_runner, "build_items", return_value=([_item()], [], {})):
            job_runner.run_intro_credits_job("j1")
        env.jm.update_job_config.assert_not_called()
        env.jm.merge_job_config.assert_not_called()

    def test_an_episode_joining_while_the_runner_reads_the_files_is_listed(self, env, monkeypatch):
        env.job.config = {"file_paths": [ep(S1, 1)], "follows_job_id": "p1", "source": "plex"}
        monkeypatch.setattr(job_runner, "wait_for_preceding_job", lambda *args: True)
        env.ctx.take_followups.return_value = []
        about_to_read = threading.Event()

        def registry(config):  # the runner's last step before it reads the files
            about_to_read.set()
            return env.registry

        monkeypatch.setattr(job_runner, "_build_multi_server_registry", registry)
        with patch.object(job_runner, "build_items", return_value=([_item(ep(S1, 1))], [], {})) as build:
            with job_runner.FOLLOW_UP_LOCK:  # a webhook is joining an episode
                thread = threading.Thread(target=job_runner.run_intro_credits_job, args=("j1",))
                thread.start()
                assert about_to_read.wait(5)
                assert not build.called
                env.job.config = {**env.job.config, "file_paths": [ep(S1, 1), ep(S1, 2)]}
            thread.join(5)
        assert not thread.is_alive()
        assert build.call_args.args[0]["file_paths"] == [ep(S1, 1), ep(S1, 2)]


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """A real JobManager behind both job_runner and triggers; created jobs don't start."""
    manager = JobManager(config_dir=str(tmp_path / "jobs"))
    monkeypatch.setattr(job_runner, "get_job_manager", lambda: manager)
    monkeypatch.setattr(triggers, "get_job_manager", lambda: manager)
    monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
    return manager


def _slow_config_updates(jm, monkeypatch):
    # Every config writer the joins and the seal use: widen the read-then-write window.
    for name in ("update_job_config", "update_job_config_if_pending", "merge_job_config"):
        real = getattr(jm, name)

        def slow(*args, _real=real, **kwargs):
            time.sleep(0.002)
            return _real(*args, **kwargs)

        monkeypatch.setattr(jm, name, slow)


def _run_threads(threads):
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)


class TestWebhookSeasonGrouping:
    @pytest.fixture
    def jm(self, settings, queue):
        settings["media_servers"] = [
            _server(
                "jf-1",
                "jellyfin",
                path_mappings=[{"remote_prefix": "/jf", "local_prefix": "/media", "webhook_prefixes": ["/data"]}],
            )
        ]
        return queue

    def _existing(self, jm, paths, **extra):
        config = {"kind": JOB_KIND_INTRO_CREDITS, "file_paths": paths, "follows_job_id": "prev-0", "force": False}
        return jm.create_job(library_name="existing", kind=JOB_KIND_INTRO_CREDITS, config={**config, **extra})

    def _submit(self, paths, hints=None):
        return triggers.submit_webhook_follow_up(
            preview_job_id="prev-2", paths=paths, source="plex", item_id_hints=hints
        )

    def _new(self, jm, before):
        return [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS and j.id not in before]

    def test_a_sibling_episode_joins_the_waiting_follow_up_of_its_season(self, jm):
        waiting = self._existing(jm, [ep(S1, 1)])
        out = self._submit([ep(S1, 2)], hints={ep(S1, 2): {"jf-1": "abc"}})
        assert out == waiting.id and self._new(jm, {waiting.id}) == []
        config = jm.get_job(waiting.id).config
        assert config["file_paths"] == [ep(S1, 1), ep(S1, 2)]
        assert config["webhook_item_id_hints"] == {ep(S1, 2): {"jf-1": "abc"}}
        assert jm.get_job(waiting.id).library_name == "Intro & Credits · 2 files"

    def test_an_episode_sent_under_another_view_joins_by_its_local_season_folder(self, jm):
        waiting = self._existing(jm, [ep(S1, 1)])
        sonarr_view = ep(S1, 2).replace("/media/", "/data/", 1)
        assert self._submit([sonarr_view]) == waiting.id
        assert jm.get_job(waiting.id).config["file_paths"] == [ep(S1, 1), sonarr_view]

    def test_a_waiting_follow_up_sent_under_another_view_takes_an_episode_of_its_local_season_folder(self, jm):
        waiting = self._existing(jm, [ep(S1, 1).replace("/media/", "/data/", 1)])
        assert self._submit([ep(S1, 2)]) == waiting.id
        assert jm.get_job(waiting.id).config["file_paths"] == [ep(S1, 1).replace("/media/", "/data/", 1), ep(S1, 2)]

    def test_an_episode_joins_one_of_two_waiting_follow_ups_of_its_season(self, jm):
        first, second = self._existing(jm, [ep(S1, 1)]), self._existing(jm, [ep(S1, 2)])
        assert self._submit([ep(S1, 3)]) == first.id
        assert jm.get_job(first.id).config["file_paths"] == [ep(S1, 1), ep(S1, 3)]
        assert jm.get_job(second.id).config["file_paths"] == [ep(S1, 2)]

    def test_a_batch_joining_two_follow_ups_returns_the_first_one_joined(self, jm):
        s1, s2 = self._existing(jm, [ep(S1, 1)]), self._existing(jm, [ep(S2, 1)])
        assert self._submit([ep(S2, 2), ep(S1, 2)]) == s1.id
        assert self._new(jm, {s1.id, s2.id}) == []
        assert jm.get_job(s2.id).config["file_paths"] == [ep(S2, 1), ep(S2, 2)]

    @pytest.mark.parametrize(("listed", "joins"), [(499, True), (500, False)], ids=["reaches-500", "would-be-501"])
    def test_a_follow_up_takes_episodes_only_while_it_stays_within_500_files(self, jm, listed, joins):
        waiting = self._existing(jm, [f"{S1}/Show (2020) - S01E{e:03d}.mkv" for e in range(1, listed + 1)])
        arriving = f"{S1}/Show (2020) - S01E777.mkv"
        out = self._submit([arriving])
        assert len(jm.get_job(waiting.id).config["file_paths"]) == listed + joins
        if joins:
            assert out == waiting.id and self._new(jm, {waiting.id}) == []
        else:
            (new,) = self._new(jm, {waiting.id})
            assert out == new.id and new.config["file_paths"] == [arriving]

    def test_an_episode_doesnt_join_a_follow_up_cancelled_after_it_was_listed(self, jm, monkeypatch):
        waiting = self._existing(jm, [ep(S1, 1)])
        joinable = triggers._joinable_follow_ups

        def listed_then_cancelled(manager, configs):
            listed = joinable(manager, configs)
            manager.cancel_job(waiting.id)
            return listed

        monkeypatch.setattr(triggers, "_joinable_follow_ups", listed_then_cancelled)
        out = self._submit([ep(S1, 2)])
        (new,) = self._new(jm, {waiting.id})
        assert out == new.id and new.config["file_paths"] == [ep(S1, 2)]
        assert jm.get_job(waiting.id).config["file_paths"] == [ep(S1, 1)]

    def test_an_episode_doesnt_join_a_follow_up_cancelled_between_its_read_and_the_write(self, jm, monkeypatch):
        waiting = self._existing(jm, [ep(S1, 1)])
        real_get = jm.get_job

        def read_then_cancelled(job_id):
            live = real_get(job_id)
            if job_id != waiting.id or live is None or live.status is not JobStatus.PENDING:
                return live
            snapshot = copy.copy(live)  # what the join read: still pending
            jm.cancel_job(job_id)
            return snapshot

        monkeypatch.setattr(jm, "get_job", read_then_cancelled)
        out = self._submit([ep(S1, 2)])
        (new,) = self._new(jm, {waiting.id})
        assert out == new.id and new.config["file_paths"] == [ep(S1, 2)]
        assert real_get(waiting.id).config["file_paths"] == [ep(S1, 1)]
        assert real_get(waiting.id).library_name == "existing"

    def test_another_season_gets_its_own_job(self, jm):
        waiting = self._existing(jm, [ep(S1, 1)])
        out = self._submit([ep(S2, 1)])
        (new,) = self._new(jm, {waiting.id})
        assert out == new.id and new.config["file_paths"] == [ep(S2, 1)]
        assert jm.get_job(waiting.id).config["file_paths"] == [ep(S1, 1)]

    @pytest.mark.parametrize(
        "extra",
        [
            {job_runner.FILES_SEALED: True},
            {"retry_attempt": 1},
            {"verify": True},
            {"force": True},
            {"follows_job_id": None, "source": "schedule"},  # only webhook follow-ups take episodes
            {"follows_job_id": None, "source": "season"},  # a legacy Season job does not take webhook requests
        ],
        ids=["sealed", "retry", "verify", "forced", "no-preview-job", "season-job"],
    )
    def test_sealed_retry_verify_and_forced_jobs_take_no_more_files(self, jm, extra):
        existing = self._existing(jm, [ep(S1, 1)], **extra)
        out = self._submit([ep(S1, 2)])
        (new,) = self._new(jm, {existing.id})
        assert out == new.id and new.config["file_paths"] == [ep(S1, 2)]

    @pytest.mark.parametrize("state", ["running", "finished"])
    def test_a_follow_up_that_started_takes_no_more_files(self, jm, state):
        existing = self._existing(jm, [ep(S1, 1)])
        jm.start_job(existing.id)
        if state == "finished":
            jm.complete_job(existing.id)
        out = self._submit([ep(S1, 2)])
        (new,) = self._new(jm, {existing.id})
        assert out == new.id and new.config["file_paths"] == [ep(S1, 2)]

    def test_files_that_arent_episodes_are_not_grouped(self, jm):
        existing = self._existing(jm, ["/media/tv/a.mkv"])
        out = self._submit(["/media/tv/b.mkv"])
        (new,) = self._new(jm, {existing.id})
        assert out == new.id

    def test_a_batch_across_seasons_joins_where_it_can_and_queues_the_rest(self, jm):
        waiting = self._existing(jm, [ep(S1, 1)])
        out = self._submit([ep(S1, 2), ep(S2, 1)], hints={ep(S1, 2): {"jf-1": "a"}, ep(S2, 1): {"jf-1": "b"}})
        (new,) = self._new(jm, {waiting.id})
        assert out == new.id and new.config["file_paths"] == [ep(S2, 1)]
        assert new.library_name == "Intro & Credits · Show (2020) - S02E01.mkv"
        assert new.config["webhook_item_id_hints"] == {ep(S2, 1): {"jf-1": "b"}}
        joined = jm.get_job(waiting.id).config
        assert joined["file_paths"] == [ep(S1, 1), ep(S1, 2)]
        assert joined["webhook_item_id_hints"] == {ep(S1, 2): {"jf-1": "a"}}

    def test_episodes_arriving_while_the_follow_up_reads_its_files_each_land_in_one_job(self, jm, monkeypatch):
        waiting = self._existing(jm, [ep(S1, 1)])
        _slow_config_updates(jm, monkeypatch)
        sealed = {}
        start = threading.Barrier(11)

        def webhook(e):
            start.wait()
            triggers.submit_webhook_follow_up(preview_job_id=f"prev-{e}", paths=[ep(S1, e)], source="plex")

        def read_files():
            start.wait()
            time.sleep(0.005)
            sealed.update(job_runner._seal_files(jm, waiting.id, waiting, dict(waiting.config)))

        _run_threads(
            [threading.Thread(target=webhook, args=(e,)) for e in range(2, 12)] + [threading.Thread(target=read_files)]
        )
        assert jm.get_job(waiting.id).config["file_paths"] == sealed["file_paths"]
        every = [p for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS for p in j.config["file_paths"]]
        assert sorted(every) == [ep(S1, e) for e in range(1, 12)]


class TestJobsStartWhileTheirCallerHoldsTheFollowUpLock:
    """The real start function under the callers of create_intro_credits_job, which hold FOLLOW_UP_LOCK."""

    @pytest.fixture
    def real_start(self, settings, tmp_path, monkeypatch):
        settings["media_servers"] = [_server("jf-1", "jellyfin")]
        manager = JobManager(config_dir=str(tmp_path / "jobs"))
        monkeypatch.setattr(job_runner, "get_job_manager", lambda: manager)
        monkeypatch.setattr(triggers, "get_job_manager", lambda: manager)
        ran = []
        monkeypatch.setattr(job_runner, "run_intro_credits_job", ran.append)
        return SimpleNamespace(jm=manager, ran=ran)

    def test_creating_a_job_under_the_lock_returns(self, real_start):
        def create():
            triggers.submit_webhook_follow_up(preview_job_id="prev-1", paths=[ep(S1, 1)], source="plex")

        thread = threading.Thread(target=create, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "starting the job waited for FOLLOW_UP_LOCK, which its caller holds"
        (created,) = real_start.jm.get_all_jobs()
        for _ in range(50):
            if real_start.ran:
                break
            time.sleep(0.02)
        assert real_start.ran == [created.id]


class TestResumeKeepsJoinedFiles:
    """Global resume and boot revival start pending jobs with a snapshot of their config as overrides."""

    def test_an_episode_that_joined_after_the_snapshot_is_kept(self, queue, monkeypatch):
        config = {
            "kind": JOB_KIND_INTRO_CREDITS,
            "source": "plex",
            "file_paths": [ep(S1, 1)],
            "follows_job_id": "p1",
            "force": False,
            "webhook_item_id_hints": {},
        }
        job = queue.create_job(library_name="A", kind=JOB_KIND_INTRO_CREDITS, config=config)
        snapshot = {**job.config, "force": True}  # what resume read (and a key an override does change)
        with job_runner.FOLLOW_UP_LOCK:
            assert triggers._join(queue, job, [ep(S1, 2)], {ep(S1, 2): {"jf-1": "x"}})
        queue.update_job_config(job.id, {**queue.get_job(job.id).config, job_runner.FILES_SEALED: True})
        ran = threading.Event()
        monkeypatch.setattr(job_runner, "run_intro_credits_job", lambda job_id: ran.set())
        job_runner.start_intro_credits_job_async(job.id, snapshot)
        assert ran.wait(5)
        merged = queue.get_job(job.id).config
        assert merged["file_paths"] == [ep(S1, 1), ep(S1, 2)]
        assert merged["webhook_item_id_hints"] == {ep(S1, 2): {"jf-1": "x"}}
        assert merged[job_runner.FILES_SEALED] is True and merged["force"] is True

    def test_a_join_racing_the_resume_merge_waits_for_it(self, queue, monkeypatch):
        config = {"kind": JOB_KIND_INTRO_CREDITS, "source": "plex", "file_paths": [ep(S1, 1)], "follows_job_id": "p1"}
        job = queue.create_job(library_name="A", kind=JOB_KIND_INTRO_CREDITS, config=config)
        snapshot = {**job.config, "force": True}
        real_merge = queue.merge_job_config
        join_done = threading.Event()

        def join_episode():
            with job_runner.FOLLOW_UP_LOCK:
                triggers._join(queue, job, [ep(S1, 2)], None)
            join_done.set()

        def resume_write(job_id, updates, **kwargs):
            if not join_done.is_set() and updates.get("force"):
                # A webhook episode arrives between the resume's read of the job and its write.
                threading.Thread(target=join_episode).start()
                join_done.wait(0.2)  # returns at once without the lock; with it, the join waits for this write
            return real_merge(job_id, updates, **kwargs)

        monkeypatch.setattr(queue, "merge_job_config", resume_write)
        ran = threading.Event()
        monkeypatch.setattr(job_runner, "run_intro_credits_job", lambda job_id: ran.set())
        job_runner.start_intro_credits_job_async(job.id, snapshot)
        assert ran.wait(5) and join_done.wait(5)
        assert queue.get_job(job.id).config["file_paths"] == [ep(S1, 1), ep(S1, 2)]


class TestExplicitSeasonRefreshThroughTheRealEngine:
    """Real workers leave sibling decisions until a user-selected run includes them."""

    def test_a_new_episode_leaves_its_sibling_until_the_next_scheduled_run(self, engine, tmp_path, monkeypatch):
        from media_preview_generator.markers import pipeline
        from media_preview_generator.markers.decide import LONG_INTRO_CHAPTER_REASON, DecisionStatus
        from media_preview_generator.markers.pipeline import PipelineContext
        from media_preview_generator.markers.settings import load_global, validate_global
        from tests.markers.fakes import FakeRegistry, server_config

        folder = tmp_path / "media" / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
        folder.mkdir(parents=True)
        e1, e2, e3 = (str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in (1, 2, 3))
        store = MarkerStore(str(tmp_path / "markers.db"))
        offline = [{"id": sid, "enabled": False} for sid in ("theintrodb", "introdb", "skipdb")]
        raw = {**SEASON_RAW, "sources": [{"id": "chapters", "enabled": True}, *offline]}
        marker_settings = load_global(validate_global(raw, None)[0])
        registry = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX, root=str(tmp_path / "media"))})
        publisher = ready_publisher()
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda config: registry)

        def build_context(*, registry, config, priority, force=False, **kwargs):
            return PipelineContext(
                registry=registry,
                config=config,
                settings=marker_settings,
                store=store,
                priority=priority,
                ffprobe="ffprobe",
                force=force,
                clients={},
                live_config=registry.get_config,
                **kwargs,
            )

        monkeypatch.setattr(job_runner, "build_context", build_context)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        chapters = _Chapters({1: 10_000, 2: 126_000, 3: 12_000})
        jm = engine.jm

        def run(**kwargs):
            job = triggers.create_intro_credits_job(**kwargs)
            job_runner.run_intro_credits_job(job.id)
            assert jm.get_job(job.id).status is JobStatus.COMPLETED
            return job

        try:
            with (
                chapters,
                patch.object(pipeline, "probe_media", side_effect=chapters.probe),
                patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: publisher),
            ):
                for path in (e1, e2):
                    _write(path, 100)
                run(library_name="first two", priority=3, source="schedule", file_paths=[e1, e2])
                assert _intro_decision(store, e2)[0] is DecisionStatus.DECIDED  # one other intro chapter: no check yet
                assert len(jm.get_all_jobs()) == 1
                _write(e3, 103)
                arrival = run(library_name="E3", priority=2, source="sonarr", file_paths=[e3], follows_job_id="prev-3")
                assert len(jm.get_all_jobs()) == 2
                assert _intro_decision(store, e2)[0] is DecisionStatus.DECIDED
                assert jm.get_job(arrival.id).config[job_runner.FILES_SEALED] is True
                run(library_name="My season schedule", priority=3, source="schedule", file_paths=[e1, e2, e3])
            assert _intro_decision(store, e2) == (DecisionStatus.NO_EVIDENCE, LONG_INTRO_CHAPTER_REASON, None)
            assert len(jm.get_all_jobs()) == 3
            assert all(job.config.get("source") != "season" for job in jm.get_all_jobs())

        finally:
            store.close()
