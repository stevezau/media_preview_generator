"""Season follow-up jobs after a job, and season grouping of one-episode webhook follow-ups."""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from random import Random
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
from tests.markers.audio.test_season import (
    COLD_OPEN_AT,
    HIGH,
    SEASON_INTRO_AT,
    _Audio,
    _chapter_probe,
    _chapter_season,
    _Chapters,
    _cold_open_points,
    _cold_open_season,
    _episode_noise,
    _fuzz_cases,
    _intro_decision,
    _introdb_answer,
    _point_ms,
    _spec,
    _write,
)
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import _ctx, _registry, _run

# Fixtures and helpers shared with the job runner and trigger tests.
env, _item = test_job_runner.env, test_job_runner._item
engine = test_job_runner_real.engine
settings, _server = test_triggers.settings, test_triggers._server

SHOW = "/media/tv/Show (2020) {tvdb-1}"
S1, S2 = f"{SHOW}/Season 01", f"{SHOW}/Season 02"


def ep(season_folder: str, e: int) -> str:
    n = int(season_folder[-2:])
    return f"{season_folder}/Show (2020) - S{n:02d}E{e:02d}.mkv"


class TestSeasonFollowUpJob:
    def _run(self, env, items, followups):
        env.ctx.take_followups.return_value = followups
        with (
            patch.object(job_runner, "build_items", return_value=(items, [], {})),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job") as create,
        ):
            job_runner.run_intro_credits_job("j1")
        return create

    def test_other_episodes_of_the_season_get_one_season_job(self, env):
        create = self._run(env, [_item(ep(S1, 3))], [ep(S1, 2), ep(S1, 1), ep(S1, 3)])
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        assert kwargs["file_paths"] == [ep(S1, 1), ep(S1, 2)]  # the job's own episode is left out, sorted
        assert (kwargs["source"], kwargs["priority"]) == ("season", 3)
        assert kwargs["library_name"] == "Season: Show (2020) {tvdb-1} · Season 01"
        assert "force" not in kwargs and "follows_job_id" not in kwargs

    def test_several_seasons_are_named_by_count(self, env):
        create = self._run(env, [_item(ep(S1, 9))], [ep(S1, 1), ep(S2, 1)])
        assert create.call_args.kwargs["library_name"] == "Season: 2 seasons"

    def test_a_flat_show_folder_is_named_after_the_show(self, env):
        flat = "/media/tv/Show (2020) {tvdb-1}"
        create = self._run(env, [_item(f"{flat}/Show - S01E03.mkv")], [f"{flat}/Show - S01E01.mkv"])
        assert create.call_args.kwargs["library_name"] == "Season: Show (2020) {tvdb-1}"

    @pytest.mark.parametrize(
        ("asker", "job_priority", "expected"),
        [
            ({"source": "plex", "follows_job_id": "prev-1"}, 1, 2),  # webhook rule: NORMAL...
            ({"source": "plex", "follows_job_id": "prev-1"}, 2, 2),
            ({"source": "plex", "follows_job_id": "prev-1"}, 3, 3),  # ...never ahead of the job that asked
            ({"source": "sonarr", "retry_attempt": 1}, 2, 3),  # a webhook follow-up's retry errs low
            ({"source": "plex", "verify": True, "chain_attempt": 1}, 2, 3),  # and so does its verify job
            ({"source": "manual"}, 2, 3),
            ({"source": "schedule"}, 2, 3),
            ({"source": "schedule"}, 3, 3),
            ({"source": "inspector", "force": True}, 1, 3),  # a re-detect's siblings don't take its HIGH slot
            ({"source": "inspector_season"}, 2, 3),
        ],
        ids=[
            "webhook-high",
            "webhook-normal",
            "webhook-low",
            "retry",
            "verify",
            "manual",
            "schedule-normal",
            "schedule-low",
            "inspector",
            "season-publish",
        ],
    )
    def test_season_jobs_run_low_unless_a_webhook_follow_up_asked(
        self, env, monkeypatch, asker, job_priority, expected
    ):
        monkeypatch.setattr(job_runner, "_wait_for_preceding_job", lambda *args: True)
        env.job.priority = job_priority
        env.job.config = {"file_paths": [ep(S1, 3)], **asker}
        create = self._run(env, [_item(ep(S1, 3))], [ep(S1, 1)])
        assert create.call_args.kwargs["priority"] == expected

    def test_the_season_job_is_queued_once_the_job_has_completed(self, env):
        # The asker shows as finished before its Season job appears in the queue behind it.
        completed_first = []
        env.ctx.take_followups.return_value = [ep(S1, 1)]
        with (
            patch.object(job_runner, "build_items", return_value=([_item(ep(S1, 3))], [], {})),
            patch(
                "media_preview_generator.markers.triggers.create_intro_credits_job",
                side_effect=lambda **kw: completed_first.append(env.jm.complete_job.called) or SimpleNamespace(id="s1"),
            ),
        ):
            job_runner.run_intro_credits_job("j1")
        assert completed_first == [True]

    def test_nothing_outside_the_job_queues_nothing(self, env):
        create = self._run(env, [_item(ep(S1, 1)), _item(ep(S1, 2))], [ep(S1, 2)])
        create.assert_not_called()

    def test_files_the_job_skipped_as_finished_before_a_restart_still_count_as_its_own(self, env, monkeypatch):
        monkeypatch.setattr(
            job_runner, "_skip_finished_before_restart", lambda jm, job_id, items, store: (items[1:], {"x": 1})
        )
        create = self._run(env, [_item(ep(S1, 1)), _item(ep(S1, 2))], [ep(S1, 1), ep(S1, 3)])
        assert create.call_args.kwargs["file_paths"] == [ep(S1, 3)]

    def test_a_season_job_never_queues_another(self, env):
        env.job.config = {"file_paths": [ep(S1, 1)], "source": "season"}
        create = self._run(env, [_item(ep(S1, 1))], [ep(S1, 2)])
        create.assert_not_called()

    def test_a_cancelled_job_queues_nothing(self, env):
        env.tracker.get_result.return_value = {**env.tracker.get_result.return_value, "cancelled": True}
        create = self._run(env, [_item(ep(S1, 1))], [ep(S1, 2)])
        create.assert_not_called()

    def test_a_huge_season_backlog_is_capped(self, env):
        many = [f"{S1}/Show (2020) - S01E{e:03d}.mkv" for e in range(1, 603)]
        create = self._run(env, [_item(ep(S1, 1))], many)
        assert len(create.call_args.kwargs["file_paths"]) == job_runner.MAX_RETRY_FILES
        assert any("more episode" in c.args[1] for c in env.jm.add_log.call_args_list)

    def test_a_season_job_that_cant_be_created_leaves_the_job_completed(self, env):
        env.ctx.take_followups.return_value = [ep(S1, 2)]
        with (
            patch.object(job_runner, "build_items", return_value=([_item(ep(S1, 1))], [], {})),
            patch("media_preview_generator.markers.triggers.create_intro_credits_job", side_effect=OSError("disk")),
        ):
            job_runner.run_intro_credits_job("j1")
        env.jm.complete_job.assert_called_once_with("j1", warning=None)

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


class TestFollowUpConfigIsReadWhenItsFilesAreListed:
    def test_episodes_that_joined_while_waiting_are_listed_and_the_job_is_sealed(self, env, monkeypatch):
        env.job.config = {"file_paths": [ep(S1, 1)], "follows_job_id": "p1", "source": "plex"}

        def joined_while_waiting(job_id, follows_job_id, cancel_check):
            env.job.config = {**env.job.config, "file_paths": [ep(S1, 1), ep(S1, 2)]}
            return True

        monkeypatch.setattr(job_runner, "_wait_for_preceding_job", joined_while_waiting)
        env.ctx.take_followups.return_value = []
        with patch.object(job_runner, "build_items", return_value=([_item(ep(S1, 1))], [], {})) as build:
            job_runner.run_intro_credits_job("j1")
        listed = build.call_args.args[0]
        assert listed["file_paths"] == [ep(S1, 1), ep(S1, 2)] and listed[job_runner.FILES_SEALED] is True
        env.jm.update_job_config.assert_called_once_with("j1", listed)

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

    def test_an_episode_joining_while_the_runner_reads_the_files_is_listed(self, env, monkeypatch):
        env.job.config = {"file_paths": [ep(S1, 1)], "follows_job_id": "p1", "source": "plex"}
        monkeypatch.setattr(job_runner, "_wait_for_preceding_job", lambda *args: True)
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


def _finished_job(jm, *, priority=3, follows=None):
    config = {"kind": JOB_KIND_INTRO_CREDITS, "file_paths": [ep(S1, 9)], "follows_job_id": follows, "source": "x"}
    job = jm.create_job(library_name="done", kind=JOB_KIND_INTRO_CREDITS, priority=priority, config=config)
    jm.start_job(job.id)
    jm.complete_job(job.id)
    return job


def _season_jobs(jm):
    return [j for j in jm.get_all_jobs() if (j.config or {}).get("source") == "season"]


class TestNoDuplicateSeasonJobs:
    def test_two_jobs_in_a_row_over_one_season_queue_one_season_job_holding_the_union(self, queue):
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1), ep(S1, 3)])
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 2), ep(S1, 3)])
        (season_job,) = _season_jobs(queue)
        assert season_job.config["file_paths"] == [ep(S1, 1), ep(S1, 2), ep(S1, 3)]
        assert season_job.priority == 3 and season_job.status is JobStatus.PENDING

    def test_a_file_a_running_season_job_already_read_waits_for_the_next_season_job(self, queue):
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1), ep(S1, 2)])
        (running,) = _season_jobs(queue)
        queue.start_job(running.id)
        job_runner._seal_files(queue, running.id, running, running.config)
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 2)])
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 2), ep(S1, 3)])
        assert running.config["file_paths"] == [ep(S1, 1), ep(S1, 2)]
        (_, following) = _season_jobs(queue)
        assert following.config["file_paths"] == [ep(S1, 2), ep(S1, 3)]

    def test_a_file_already_waiting_at_another_priority_is_not_queued_twice(self, queue):
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1)])
        job_runner._queue_season_followups(_finished_job(queue, priority=2, follows="prev-1"), [ep(S1, 1), ep(S1, 2)])
        low, normal = _season_jobs(queue)
        assert (low.priority, low.config["file_paths"]) == (3, [ep(S1, 1)])
        assert (normal.priority, normal.config["file_paths"]) == (2, [ep(S1, 2)])

    def test_every_file_already_waiting_creates_nothing(self, queue):
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1), ep(S1, 2)])
        asker = _finished_job(queue)
        job_runner._queue_season_followups(asker, [ep(S1, 2)])
        assert len(_season_jobs(queue)) == 1
        assert any("already queued" in line for line in queue.get_logs(asker.id))

    @pytest.mark.parametrize("extra", [{job_runner.FILES_SEALED: True}, {"retry_attempt": 1}, {"verify": True}])
    def test_sealed_retry_and_verify_season_jobs_take_no_more_files(self, queue, extra):
        config = {"kind": JOB_KIND_INTRO_CREDITS, "source": "season", "file_paths": [ep(S1, 1)], **extra}
        existing = queue.create_job(library_name="s", kind=JOB_KIND_INTRO_CREDITS, priority=3, config=config)
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1), ep(S1, 2)])
        new = [j for j in _season_jobs(queue) if j.id != existing.id]
        assert [j.config["file_paths"] for j in new] == [[ep(S1, 1), ep(S1, 2)]]
        assert existing.config["file_paths"] == [ep(S1, 1)]

    def test_a_joined_season_job_is_named_after_all_its_files(self, queue):
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1)])
        job_runner._queue_season_followups(_finished_job(queue), [ep(S2, 1)])
        (season_job,) = _season_jobs(queue)
        assert season_job.library_name == "Season: 2 seasons"

    def test_a_waiting_season_job_too_full_for_the_request_leaves_it_to_a_new_job(self, queue):
        first = [f"{S1}/Show (2020) - S01E{e:03d}.mkv" for e in range(1, 500)]
        job_runner._queue_season_followups(_finished_job(queue), first)
        job_runner._queue_season_followups(_finished_job(queue), [ep(S2, 1), ep(S2, 2)])
        full, new = _season_jobs(queue)
        assert len(full.config["file_paths"]) == 499 and new.config["file_paths"] == [ep(S2, 1), ep(S2, 2)]

    def test_a_request_joins_the_oldest_waiting_season_job_it_fits_in(self, queue):
        job_runner._queue_season_followups(
            _finished_job(queue), [f"{S1}/Show (2020) - S01E{e:03d}.mkv" for e in range(1, 499)]
        )
        job_runner._queue_season_followups(_finished_job(queue), [ep(S2, 1), ep(S2, 2), ep(S2, 3)])  # 501: a new job
        job_runner._queue_season_followups(_finished_job(queue), [ep(S2, 4), ep(S2, 5)])  # exactly 500: joins the first
        oldest, second = _season_jobs(queue)
        assert len(oldest.config["file_paths"]) == 500 and oldest.config["file_paths"][-2:] == [ep(S2, 4), ep(S2, 5)]
        assert second.config["file_paths"] == [ep(S2, 1), ep(S2, 2), ep(S2, 3)]

    def test_a_request_doesnt_join_a_season_job_cancelled_after_it_was_listed(self, queue, monkeypatch):
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1)])
        (cancelled,) = _season_jobs(queue)
        listed = job_runner._waiting_season_jobs

        def listed_then_cancelled(jm):
            waiting = listed(jm)
            jm.cancel_job(cancelled.id)
            return waiting

        monkeypatch.setattr(job_runner, "_waiting_season_jobs", listed_then_cancelled)
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 2)])
        assert cancelled.config["file_paths"] == [ep(S1, 1)]
        assert [j.config["file_paths"] for j in _season_jobs(queue) if j.id != cancelled.id] == [[ep(S1, 2)]]

    @pytest.mark.parametrize(
        ("state", "queued_again"), [("never-started", False), ("revived", True), ("running", True)]
    )
    def test_a_file_a_waiting_webhook_follow_up_will_read_is_not_queued_again(
        self, queue, settings, state, queued_again
    ):
        # The follow-up holds Sonarr's view of E2; the season step asks for the local path.
        mapping = {"remote_prefix": "/jf", "local_prefix": "/media", "webhook_prefixes": ["/data"]}
        settings["media_servers"] = [_server("jf-1", "jellyfin", path_mappings=[mapping])]
        config = {
            "kind": JOB_KIND_INTRO_CREDITS,
            "source": "sonarr",
            "file_paths": [ep(S1, 2).replace("/media/", "/data/", 1)],
            "follows_job_id": "prev-1",
        }
        follow_up = queue.create_job(library_name="E2", kind=JOB_KIND_INTRO_CREDITS, priority=2, config=config)
        if state != "never-started":
            queue.start_job(follow_up.id)
        if state == "revived":
            follow_up.status = JobStatus.PENDING  # a restart put it back; it may already have run E2
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1), ep(S1, 2)])
        (season_job,) = _season_jobs(queue)
        assert season_job.config["file_paths"] == ([ep(S1, 1), ep(S1, 2)] if queued_again else [ep(S1, 1)])

    def test_requests_while_a_season_job_reads_its_files_put_each_file_in_one_job(self, queue, monkeypatch):
        job_runner._queue_season_followups(_finished_job(queue), [ep(S1, 1)])
        (waiting,) = _season_jobs(queue)
        askers = [_finished_job(queue) for _ in range(10)]
        _slow_config_updates(queue, monkeypatch)
        sealed = {}
        start = threading.Barrier(11)

        def ask(i):
            start.wait()
            job_runner._queue_season_followups(askers[i], [ep(S1, i + 2)])

        def read_files():
            start.wait()
            time.sleep(0.005)
            sealed.update(job_runner._seal_files(queue, waiting.id, waiting, dict(waiting.config)))

        _run_threads(
            [threading.Thread(target=ask, args=(i,)) for i in range(10)] + [threading.Thread(target=read_files)]
        )
        assert queue.get_job(waiting.id).config["file_paths"] == sealed["file_paths"]  # nothing joined after the read
        every = [p for j in _season_jobs(queue) for p in j.config["file_paths"]]
        assert sorted(every) == [ep(S1, e) for e in range(1, 12)]


def _slow_config_updates(jm, monkeypatch):
    real = jm.update_job_config

    def slow(job_id, config):
        time.sleep(0.002)  # widen the read-then-write window
        real(job_id, config)

    monkeypatch.setattr(jm, "update_job_config", slow)


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
            {"follows_job_id": None, "source": "season"},  # a waiting Season job takes Season requests only
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

    @pytest.mark.parametrize("caller", ["webhook-follow-up", "season-follow-up"])
    def test_creating_a_job_under_the_lock_returns(self, real_start, caller):
        asker = _finished_job(real_start.jm)

        def create():
            if caller == "webhook-follow-up":
                triggers.submit_webhook_follow_up(preview_job_id="prev-1", paths=[ep(S1, 1)], source="plex")
            else:
                job_runner._queue_season_followups(asker, [ep(S1, 2)])

        thread = threading.Thread(target=create, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "starting the job waited for FOLLOW_UP_LOCK, which its caller holds"
        (created,) = [j for j in real_start.jm.get_all_jobs() if j.id != asker.id]
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
        real_update = queue.update_job_config
        join_done = threading.Event()

        def join_episode():
            with job_runner.FOLLOW_UP_LOCK:
                triggers._join(queue, job, [ep(S1, 2)], None)
            join_done.set()

        def resume_write(job_id, merged):
            if not join_done.is_set() and "force" in merged and merged["force"]:
                # A webhook episode arrives between the resume's read of the job and its write.
                threading.Thread(target=join_episode).start()
                join_done.wait(0.2)  # returns at once without the lock; with it, the join waits for this write
            real_update(job_id, merged)

        monkeypatch.setattr(queue, "update_job_config", resume_write)
        ran = threading.Event()
        monkeypatch.setattr(job_runner, "run_intro_credits_job", lambda job_id: ran.set())
        job_runner.start_intro_credits_job_async(job.id, snapshot)
        assert ran.wait(5) and join_done.wait(5)
        assert queue.get_job(job.id).config["file_paths"] == [ep(S1, 1), ep(S1, 2)]


class TestSeasonJobThroughTheRealEngine:
    """Real JobManager, gate, dispatcher and pipeline: the requests a job's worker threads make reach its Season job."""

    def test_a_new_episode_queues_a_season_job_that_decides_its_sibling_again(self, engine, tmp_path, monkeypatch):
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
        raw = {**HIGH, "sources": [{"id": "chapters", "enabled": True}, *offline]}
        marker_settings = load_global(validate_global(raw, None)[0])
        registry = FakeRegistry({"plex-1": server_config("plex-1", ServerType.PLEX, root=str(tmp_path / "media"))})
        publisher = ready_publisher()
        monkeypatch.setattr(job_runner, "_build_multi_server_registry", lambda config: registry)
        monkeypatch.setattr(
            job_runner,
            "build_context",
            lambda *, registry, config, priority, force=False, recheck_empty_server_markers=False: PipelineContext(
                registry=registry,
                config=config,
                settings=marker_settings,
                store=store,
                priority=priority,
                ffprobe="ffprobe",
                force=force,
                clients={},
                live_config=registry.get_config,
                recheck_empty_server_markers=recheck_empty_server_markers,
            ),
        )
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
                assert _season_jobs(jm) == []
                _write(e3, 103)
                arrival = run(library_name="E3", priority=2, source="sonarr", file_paths=[e3], follows_job_id="prev-3")
                (season_job,) = _season_jobs(jm)
                assert season_job.config["file_paths"] == [e2]
                assert (season_job.priority, season_job.library_name) == (2, "Season: Show (2020) {tvdb-1} · Season 01")
                assert jm.get_job(arrival.id).config[job_runner.FILES_SEALED] is True
                job_runner.run_intro_credits_job(season_job.id)
            assert jm.get_job(season_job.id).status is JobStatus.COMPLETED
            assert _intro_decision(store, e2)[:2] == (DecisionStatus.NEEDS_REVIEW, LONG_INTRO_CHAPTER_REASON)
            assert len(_season_jobs(jm)) == 1
        finally:
            store.close()


# Fuzz: the job engine's queue with jobs interleaved. Arrivals come as webhook follow-ups (which may join a waiting
# follow-up of their season) or as plain jobs, re-runs as manual jobs; up to three jobs run at once, one file at a time
# in any interleaving, and each finished job queues its Season job through the real job_runner and triggers code. Once
# every queued job has run, the decisions must equal the all-at-once decisions of tests/markers/audio/test_season.py.

QUEUE_FUZZ_SEASONS, QUEUE_FUZZ_CHUNKS = 60, 3
_MAX_RUNNING = 3
# Jobs started, Season jobs started, files they listed, episodes joined to a waiting follow-up, and files run while
# another job had started and not finished.
_COUNTS = ("jobs", "season_jobs", "season_files", "joined_episodes", "interleaved_files")


def _queued_season(root, settings, rng, mode, inputs, order, reruns):
    """Run one fuzz season through the queue. Returns the final intro decisions and the job counts."""
    media = root / "media"
    folder = media / "tv" / "Show (2020) {tvdb-1}" / "Season 01"
    folder.mkdir(parents=True)
    store = MarkerStore(str(root / "markers.db"))
    jm = JobManager(config_dir=str(root / "jobs"))
    paths = {e: str(folder / f"Show (2020) - S01E{e:02d}.mkv") for e in inputs}
    registry = _registry(paths[min(paths)], ServerType.PLEX)
    settings["media_servers"] = [
        _server(
            "jf-1", "jellyfin", libraries=[{"id": "1", "name": "TV", "remote_paths": [str(media)], "enabled": True}]
        )
    ]
    counts = dict.fromkeys(_COUNTS, 0)
    if mode == "cold-open":
        clients, detectors = _introdb_answer(*_point_ms(COLD_OPEN_AT)), (_spec(),)

        def probe(path, **kwargs):
            chapter = inputs[int(re.search(r"E(\d+)", os.path.basename(path)).group(1))]["chapter"]
            if chapter is None:
                return _chapter_probe(None)
            start, end = _point_ms(SEASON_INTRO_AT if chapter == "x" else COLD_OPEN_AT)
            return _chapter_probe(end - start, at_ms=start)

        audio, probes = _Audio(points=_cold_open_points(inputs)), patch.object(season, "probe_media", side_effect=probe)
    else:
        chapters = _Chapters(inputs)
        clients, detectors, probe = None, ((_spec(),) if mode == "season-audio" else ()), chapters.probe
        audio, probes = _Audio(points=_episode_noise), chapters

    def run_file(ctx, path):
        if _run(ctx, path, {"plex-1": ready_publisher()}, probe_effect=probe)[0] is None:
            _run(ctx, path, {"plex-1": ready_publisher()}, stage="process", probe_effect=probe)

    running = []  # [job, cfg, ctx, items, next index]
    arrivals = list(zip(order, reruns, strict=True))
    arrived = []
    try:
        with (
            audio,
            probes,
            patch.object(job_runner, "get_job_manager", lambda: jm),
            patch.object(triggers, "get_job_manager", lambda: jm),
            patch.object(triggers, "start_intro_credits_job_async", lambda job_id: None),
        ):
            while True:
                waiting = jm.get_pending_jobs()
                actions = (["arrive"] if arrivals else []) + (["step"] * 2 if running else [])
                actions += ["start"] if waiting and len(running) < _MAX_RUNNING else []
                if not actions:
                    break
                action = rng.choice(actions)
                if action == "arrive":
                    e, rerun = arrivals.pop(0)
                    if rerun is not None and arrived:
                        path = paths[arrived[rerun % len(arrived)]]
                        triggers.create_intro_credits_job(
                            library_name="re-run", priority=rng.choice([2, 3]), source="manual", file_paths=[path]
                        )
                    _write(paths[e], 100 + e)
                    arrived.append(e)
                    if rng.random() < 0.7:
                        before = {j.id for j in jm.get_all_jobs()}
                        job_id = triggers.submit_webhook_follow_up(
                            preview_job_id=f"prev-{e}", paths=[paths[e]], source="plex"
                        )
                        counts["joined_episodes"] += job_id in before
                    else:
                        triggers.create_intro_credits_job(
                            library_name="listing", priority=3, source="schedule", file_paths=[paths[e]]
                        )
                elif action == "start":
                    job = rng.choice(waiting)
                    jm.start_job(job.id)
                    cfg = job_runner._seal_files(jm, job.id, job, dict(job.config or {}))
                    items = sorted(set(cfg["file_paths"]))
                    ctx = _ctx(store, registry, settings_raw=HIGH, detectors=detectors, clients=clients)
                    running.append([job, cfg, ctx, items, 0])
                    counts["jobs"] += 1
                    if cfg.get("source") == job_runner.SEASON_SOURCE:
                        counts["season_jobs"] += 1
                        counts["season_files"] += len(items)
                else:
                    entry = rng.choice(running)
                    job, cfg, ctx, items, index = entry
                    if index < len(items):
                        counts["interleaved_files"] += len(running) > 1
                        run_file(ctx, items[index])
                        entry[4] += 1
                    else:
                        running.remove(entry)
                        # Sealed: nothing may join a job once it has read its files.
                        assert jm.get_job(job.id).config["file_paths"] == cfg["file_paths"]
                        jm.complete_job(job.id)
                        job_runner._queue_season_followups_after(job, cfg, ctx, set(items))
                _assert_no_file_waits_twice(jm)
        return {e: _intro_decision(store, paths[e]) for e in inputs}, counts
    finally:
        store.close()


def _assert_no_file_waits_twice(jm):
    waiting = [p for j in job_runner._waiting_season_jobs(jm) for p in j.config["file_paths"]]
    assert len(waiting) == len(set(waiting))


@pytest.mark.parametrize("chunk", range(QUEUE_FUZZ_CHUNKS))
@pytest.mark.parametrize("mode", ["weekly", "reruns", "season-audio", "cold-open"])
def test_queued_season_jobs_end_with_the_all_at_once_decisions(tmp_path, settings, mode, chunk):
    size = QUEUE_FUZZ_SEASONS // QUEUE_FUZZ_CHUNKS
    rng = Random(20260915 + chunk)
    mismatches = []
    for i, (inputs, order, reruns) in enumerate(_fuzz_cases(mode)[chunk * size : (chunk + 1) * size]):
        if mode == "cold-open":
            want, _, _ = _cold_open_season(tmp_path / f"all{i}", inputs, [sorted(inputs)])
        else:
            detectors = (_spec(),) if mode == "season-audio" else ()
            want = _chapter_season(tmp_path / f"all{i}", inputs, [sorted(inputs)], [None], detectors)
        got, _counts = _queued_season(tmp_path / f"queued{i}", settings, rng, mode, inputs, order, reruns)
        if got != want:
            mismatches.append((inputs, order, reruns, {e: (want[e], got[e]) for e in want if want[e] != got[e]}))
    assert mismatches == []


def test_the_fuzz_queue_really_interleaves_joins_and_queues_season_jobs(tmp_path, settings):
    rng = Random(1)
    counts = dict.fromkeys(_COUNTS, 0)
    for i, (inputs, order, reruns) in enumerate(_fuzz_cases("reruns")[:10]):
        _got, one = _queued_season(tmp_path / f"q{i}", settings, rng, "reruns", inputs, order, reruns)
        counts = {k: counts[k] + one[k] for k in counts}
    assert all(counts.values()), counts
