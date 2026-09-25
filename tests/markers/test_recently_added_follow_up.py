"""The Intro & Credits job that follows a scheduled Recently Added scan treats its files as a server listing.

A file a server listed as recently added that isn't on disk here won't appear by waiting (a path mapping problem, as
for a library listing), and the server has already rescanned it, so it gets no missing-file retry and no later verify:
the preview job doesn't retry such files either. A file a server hasn't indexed everywhere yet still gets its retry.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import job_runner, triggers
from tests.markers import test_job_runner
from tests.markers.test_job_runner import NOT_IN_LIBRARY_ROW, _row
from tests.markers.test_job_runner import _item as _job_item

env = test_job_runner.env


@pytest.fixture
def run(env, monkeypatch):
    saved = {"log_level": "INFO", "webhook_retry_count": 3, "webhook_retry_delay": 30}
    env.sm.get.side_effect = lambda key, default=None: saved.get(key, default)
    env.job.library_name = "Intro & Credits · 2 files"
    create = MagicMock(side_effect=lambda **kw: MagicMock(id="later-1", config={}))
    monkeypatch.setattr(triggers, "create_intro_credits_job", create)
    set_cb = MagicMock()
    monkeypatch.setattr(job_runner, "set_file_result_callback", set_cb)
    results: list[tuple] = []

    def during_wait(timeout=None):
        for outcome, rows in results:
            set_cb.call_args_list[0].args[0]("/m/a.mkv", outcome, "", "Lookup", servers=rows)
        return True

    env.tracker.wait.side_effect = during_wait

    def go(source, outcome, rows):
        env.job.config = {"libraries": [], "file_paths": ["/data/tv/a.mkv"], "source": source}
        results.append((outcome, rows))
        with patch.object(job_runner, "build_items", return_value=([_job_item("/m/a.mkv")], [], {})):
            job_runner.run_intro_credits_job("j1")
        return create

    return SimpleNamespace(go=go)


@pytest.mark.parametrize(("source", "later"), [("recently_added", False), ("sonarr", True)])
@pytest.mark.parametrize(
    ("outcome", "rows"),
    [
        ("skipped_file_not_found", []),
        ("markers_published", [_row("markers_written", "2 marker(s)", verify_later=True)]),
    ],
    ids=["missing-from-disk", "replaced"],
)
def test_a_listed_file_gets_no_retry_for_missing_and_no_verify(run, source, later, outcome, rows):
    create = run.go(source, outcome, rows)
    assert create.called is later


def test_a_file_a_server_hasnt_indexed_yet_still_gets_its_retry(run):
    create = run.go("recently_added", "markers_waiting", [NOT_IN_LIBRARY_ROW])
    (call,) = create.call_args_list
    assert call.kwargs["library_name"].startswith("Retry: ")
    assert call.kwargs["source"] == "recently_added"


class TestListingAndSentFollowUpsStayApart:
    """A webhook's file keeps the missing-from-disk retry and the later verify a Recently Added follow-up doesn't give
    its files, and doesn't wait behind a long Recently Added scan: a listing's follow-up never covers or takes a
    webhook's files. A webhook's follow-up does cover a Recently Added scan's file (it gives it more, not less)."""

    E1 = "/media/tv/Show/Season 01/Show - S01E01.mkv"
    E2 = "/media/tv/Show/Season 01/Show - S01E02.mkv"

    @pytest.fixture
    def jm(self, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        servers = [
            {
                "id": "jf-1",
                "type": "jellyfin",
                "name": "JF",
                "enabled": True,
                "markers": {"enabled": True},
                "libraries": [{"id": "1", "name": "TV", "remote_paths": ["/media/tv"], "enabled": True}],
            }
        ]
        sm = MagicMock()
        sm.get.side_effect = lambda key, default=None: servers if key == "media_servers" else default
        monkeypatch.setattr(triggers, "get_settings_manager", lambda: sm)
        manager = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: manager)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        return manager

    def _waiting(self, jm, source):
        config = {"kind": "intro_credits", "source": source, "file_paths": [self.E1], "follows_job_id": "prev-0"}
        return jm.create_job(library_name="waiting", kind="intro_credits", config=config)

    @pytest.mark.parametrize(
        ("waiting_source", "source", "episode", "new_job"),
        [
            ("recently_added", "sonarr", 1, True),  # not covered: it would lose its retries
            ("recently_added", "sonarr", 2, True),  # doesn't join
            ("sonarr", "recently_added", 1, False),  # covered
            ("sonarr", "recently_added", 2, True),  # doesn't join: the listing's file would get sent-file retries
            ("recently_added", "recently_added", 2, False),  # joins
            ("sonarr", "plex", 2, False),  # joins, as before
        ],
    )
    def test_cover_and_join(self, jm, waiting_source, source, episode, new_job):
        path = self.E1 if episode == 1 else self.E2
        waiting = self._waiting(jm, waiting_source)
        out = triggers.submit_webhook_follow_up(preview_job_id="prev-2", paths=[path], source=source)
        new = [j for j in jm.get_all_jobs() if j.id != waiting.id]
        if new_job:
            (job,) = new
            assert out == job.id
            assert job.config["source"] == source and job.config["file_paths"] == [path]
            assert jm.get_job(waiting.id).config["file_paths"] == [self.E1]
        else:
            assert new == []
            assert out in (None, waiting.id)
