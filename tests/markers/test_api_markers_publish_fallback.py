"""An Inspector save that a server didn't take queues the job that delivers it.

The save publishes inside the request (``publish_now``), bounded so the page answers quickly; a server it couldn't
write (the file busy in a running job, the deadline, a busy Plex database, a server down or not indexed yet) was left
for "the next run", and nothing queued one: only a scheduled Check servers ever delivered it. Now any row not written
queues one single-file HIGH Intro & Credits job (not forced: it publishes the saved markers and asks no source again),
whose retry chain delivers what can't be written yet. A job for the file that hasn't started yet is reused.
"""

from __future__ import annotations

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import triggers
from media_preview_generator.markers.outcomes import ServerStatus
from media_preview_generator.web.jobs import PRIORITY_HIGH, get_job_manager
from tests.markers import test_api_markers_edit
from tests.markers.test_api_markers_edit import _row, _save

# Fixtures shared with the editor's write API tests.
media, episode, servers = test_api_markers_edit.media, test_api_markers_edit.episode, test_api_markers_edit.servers
known, published = test_api_markers_edit.known, test_api_markers_edit.published

INTRO = [{"type": "intro", "start_ms": 60_000, "end_ms": 90_000}]


@pytest.fixture(autouse=True)
def _jobs_are_queued_not_run(monkeypatch):
    monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)


def _markers_jobs():
    return [j for j in get_job_manager().get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]


class TestASaveThatWasntWrittenEverywhere:
    @pytest.mark.parametrize(
        ("server_id", "status", "result", "queued"),
        [
            ("jf-1", ServerStatus.WAITING, "waiting", True),  # busy file, busy database, not indexed, Plex Pass unknown
            ("jf-1", ServerStatus.FAILED, "failed", True),  # deadline, server error
            # Intro & Credits is on there but the server couldn't take it (down, plugin missing): shown as failed.
            ("jf-1", ServerStatus.SKIPPED, "failed", True),
            ("jf-9", ServerStatus.SKIPPED, "not_enabled", False),  # Intro & Credits off there
            ("jf-1", ServerStatus.WRITTEN, "written", False),
            ("jf-1", ServerStatus.UP_TO_DATE, "unchanged", False),
            ("jf-1", ServerStatus.NEEDS_REVIEW, "needs_review", False),  # a job can't settle what only the user can
            ("jf-1", ServerStatus.NONE, "nothing_to_publish", False),
        ],
        ids=["waiting", "failed", "not-ready", "off", "written", "unchanged", "needs-review", "nothing"],
    )
    def test_queues_one_job_only_when_the_editor_shows_a_server_waiting_or_failed(
        self, client, servers, known, episode, published, server_id, status, result, queued
    ):
        published.rows = [
            _row("plex-1", "plex", ServerStatus.WRITTEN.value),
            _row(server_id, "jellyfin", status.value),
        ]

        resp = _save(client, episode, INTRO)

        assert resp.status_code == 200
        assert [row["result"] for row in resp.get_json()["servers"]] == ["written", result]
        jobs = _markers_jobs()
        if not queued:
            assert jobs == []
            assert resp.get_json().get("queued_job_id") is None
            return
        (job,) = jobs
        assert resp.get_json()["queued_job_id"] == job.id
        assert job.priority == PRIORITY_HIGH
        assert job.config["file_paths"] == [episode]
        assert job.config["force"] is False  # publishes the saved markers; asks no source again
        assert job.config["source"] == "inspector"
        assert not job.config.get("follows_job_id")

    def test_saving_again_before_that_job_starts_reuses_it(self, client, servers, known, episode, published):
        published.rows = [_row("jf-1", "jellyfin", ServerStatus.WAITING.value)]
        first = _save(client, episode, INTRO).get_json()["queued_job_id"]
        second = _save(client, episode, INTRO).get_json()["queued_job_id"]
        assert first == second
        assert len(_markers_jobs()) == 1

    def test_a_waiting_re_detect_of_the_file_is_reused(self, client, servers, known, episode, published):
        redetect = triggers.submit_redetect(episode)
        published.rows = [_row("jf-1", "jellyfin", ServerStatus.WAITING.value)]
        assert _save(client, episode, INTRO).get_json()["queued_job_id"] == redetect
        assert len(_markers_jobs()) == 1

    def test_a_job_already_running_the_file_is_not_reused(self, client, servers, known, episode, published):
        # It decided from the markers as they were before the save.
        running = triggers.submit_redetect(episode)
        get_job_manager().start_job(running)
        published.rows = [_row("jf-1", "jellyfin", ServerStatus.WAITING.value)]
        queued = _save(client, episode, INTRO).get_json()["queued_job_id"]
        assert queued not in (None, running)

    def test_a_retry_counting_down_is_not_reused(self, client, servers, known, episode, published):
        retry = triggers.create_intro_credits_job(
            library_name="Retry: x",
            priority=PRIORITY_HIGH,
            source="inspector",
            file_paths=[episode],
            retry_attempt=1,
            retry_delay_s=3600,
            parent_job_id="head-1",
            max_retries=3,
        )
        published.rows = [_row("jf-1", "jellyfin", ServerStatus.FAILED.value)]
        queued = _save(client, episode, INTRO).get_json()["queued_job_id"]
        assert queued not in (None, retry.id)

    def test_a_job_that_cant_be_queued_never_fails_the_save(
        self, client, servers, known, episode, published, monkeypatch
    ):
        def broken(**_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(triggers, "create_intro_credits_job", broken)
        published.rows = [_row("jf-1", "jellyfin", ServerStatus.WAITING.value)]
        resp = _save(client, episode, INTRO)
        assert resp.status_code == 200
        assert resp.get_json()["queued_job_id"] is None
        assert resp.get_json()["servers"][0]["result"] == "waiting"
