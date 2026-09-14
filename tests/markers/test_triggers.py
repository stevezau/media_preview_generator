"""Creating Intro & Credits jobs: the markers switch, the job config, and webhook follow-ups."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_PREVIEWS
from media_preview_generator.markers import triggers
from media_preview_generator.web.jobs import JobStatus

PLEX_CONFIRMED = {"enabled": True, "plex": {"db_write_confirmed_at": "2026-09-13T00:00:00+00:00"}}


def _server(sid, stype, *, markers=None, enabled=True, libraries=None, path_mappings=None, exclude_paths=None):
    return {
        "id": sid,
        "type": stype,
        "name": sid.upper(),
        "enabled": enabled,
        "markers": markers if markers is not None else {"enabled": True},
        "libraries": libraries
        if libraries is not None
        else [{"id": "1", "name": "TV Shows", "remote_paths": ["/media/tv"], "enabled": True}],
        "path_mappings": path_mappings or [],
        "exclude_paths": exclude_paths or [],
    }


@pytest.fixture
def settings(monkeypatch):
    state = {"media_servers": []}
    sm = MagicMock()
    sm.get.side_effect = lambda key, default=None: state.get(key, default)
    monkeypatch.setattr(triggers, "get_settings_manager", lambda: sm)
    return state


@pytest.mark.parametrize(
    ("servers", "expected"),
    [
        ([], False),
        ([{"type": "plex", "enabled": True, "markers": {"enabled": False}}], False),
        ([{"type": "jellyfin", "enabled": False, "markers": {"enabled": True}}], False),
        ([{"type": "plex", "enabled": True, "markers": {"enabled": True}}], False),  # unconfirmed Plex = off
        ([{"type": "plex", "enabled": True, "markers": PLEX_CONFIRMED}], True),
        ([{"type": "jellyfin", "enabled": True, "markers": {"enabled": True}}], True),
        ([{"type": "emby", "enabled": True, "markers": {"enabled": True}}], True),
        ([{"type": "emby", "markers": {"enabled": True}}], True),  # enabled defaults to True
        (["not-a-dict", {"type": "jellyfin", "enabled": True}], False),  # no markers block = off
    ],
)
def test_markers_enabled_anywhere_matrix(servers, expected, settings):
    settings["media_servers"] = servers
    assert triggers.markers_enabled_anywhere() is expected


class TestMarkerOwnedPaths:
    def test_path_on_a_server_with_markers_off_is_not_owned(self, settings):
        # The full ownership matrix lives in test_marker_ownership.py; this row checks the settings wiring.
        settings["media_servers"] = [_server("jf-1", "jellyfin", markers={"enabled": False})]
        assert triggers.marker_owned_paths(["/media/tv/Show/S01E01.mkv"]) == []

    def test_sender_path_is_translated_with_any_servers_webhook_prefixes(self, settings):
        # Plex (markers off) carries the Sonarr prefix mapping; Jellyfin (markers on) sees the same local disk.
        settings["media_servers"] = [
            _server(
                "plex-1",
                "plex",
                markers={"enabled": False},
                path_mappings=[
                    {"remote_prefix": "/plexmedia", "local_prefix": "/media", "webhook_prefixes": ["/data"]}
                ],
            ),
            _server("jf-1", "jellyfin"),
        ]
        assert triggers.marker_owned_paths(["/data/tv/Show/S01E01.mkv"]) == ["/data/tv/Show/S01E01.mkv"]

    def test_server_view_path_is_translated_with_path_mappings(self, settings):
        settings["media_servers"] = [
            _server(
                "jf-1",
                "jellyfin",
                libraries=[{"id": "1", "name": "TV", "remote_paths": ["/jfmedia/tv"], "enabled": True}],
                path_mappings=[{"remote_prefix": "/jfmedia", "local_prefix": "/media"}],
            )
        ]
        assert triggers.marker_owned_paths(["/jfmedia/tv/Show/S01E01.mkv"]) == ["/jfmedia/tv/Show/S01E01.mkv"]

    def test_unsupported_server_entries_are_ignored(self, settings):
        settings["media_servers"] = [{"id": "x", "type": "kodi", "markers": {"enabled": True}}, "junk"]
        assert triggers.marker_owned_paths(["/media/tv/a.mkv"]) == []

    def test_never_touches_the_disk(self, settings):
        settings["media_servers"] = [_server("jf-1", "jellyfin")]
        with (
            patch("os.path.exists", side_effect=AssertionError("disk")),
            patch("os.path.isdir", side_effect=AssertionError("disk")),
            patch("os.stat", side_effect=AssertionError("disk")),
        ):
            assert triggers.marker_owned_paths(["/media/tv/a.mkv"]) == ["/media/tv/a.mkv"]


class TestCreateIntroCreditsJob:
    @pytest.mark.parametrize(
        ("kwargs", "config"),
        [
            (
                {
                    "library_name": "Intro & Credits · R&M S01",
                    "priority": 2,
                    "source": "sonarr",
                    "file_paths": ["/data/tv/a.mkv"],
                    "follows_job_id": "prev-1",
                    "item_id_hints": {"/data/tv/a.mkv": {"jf-1": "x"}},
                },
                {
                    "kind": "intro_credits",
                    "source": "sonarr",
                    "libraries": [],
                    "file_paths": ["/data/tv/a.mkv"],
                    "follows_job_id": "prev-1",
                    "force": False,
                    "webhook_item_id_hints": {"/data/tv/a.mkv": {"jf-1": "x"}},
                },
            ),
            (
                {
                    "library_name": "Intro & Credits: TV Shows",
                    "priority": 3,
                    "source": "schedule",
                    "libraries": [{"server_id": "plex-1", "library_id": "1"}],
                    "parent_schedule_id": "sched-1",
                    "force": True,
                },
                {
                    "kind": "intro_credits",
                    "source": "schedule",
                    "libraries": [{"server_id": "plex-1", "library_id": "1"}],
                    "file_paths": [],
                    "follows_job_id": None,
                    "force": True,
                    "webhook_item_id_hints": {},
                },
            ),
        ],
    )
    def test_job_config_kind_priority_and_start(self, monkeypatch, kwargs, config):
        jm = MagicMock()
        jm.create_job.return_value = MagicMock(id="new-job-id")
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async") as start:
            job = triggers.create_intro_credits_job(**kwargs)
        assert job is jm.create_job.return_value
        created = jm.create_job.call_args.kwargs
        assert created == {
            "library_name": kwargs["library_name"],
            "config": config,
            "priority": kwargs["priority"],
            "kind": JOB_KIND_INTRO_CREDITS,
            "parent_schedule_id": kwargs.get("parent_schedule_id", ""),
        }
        start.assert_called_once_with("new-job-id")

    def test_retry_job_config_carries_its_attempt_and_due_time(self, monkeypatch):
        from datetime import datetime, timezone

        jm = MagicMock()
        jm.create_job.return_value = MagicMock(id="retry-1")
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        monkeypatch.setattr(triggers, "_utcnow", lambda: datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc))
        with patch.object(triggers, "start_intro_credits_job_async") as start:
            triggers.create_intro_credits_job(
                library_name="Retry: a",
                priority=3,
                source="sonarr",
                file_paths=["/m/a.mkv"],
                retry_attempt=2,
                retry_delay_s=120,
            )
        assert jm.create_job.call_args.kwargs["config"] == {
            "kind": "intro_credits",
            "source": "sonarr",
            "libraries": [],
            "file_paths": ["/m/a.mkv"],
            "follows_job_id": None,
            "force": False,
            "webhook_item_id_hints": {},
            "retry_attempt": 2,
            "retry_delay": 120,
            "retry_not_before": "2026-09-14T10:02:00+00:00",
        }
        assert jm.create_job.call_args.kwargs["parent_schedule_id"] == ""
        start.assert_called_once_with("retry-1")

    def test_created_job_row_is_an_intro_credits_job(self, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        jm = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async"):
            job = triggers.create_intro_credits_job(library_name="x", priority=3, source="manual")
        stored = JobManager(config_dir=str(tmp_path)).get_job(job.id)
        assert stored.kind == JOB_KIND_INTRO_CREDITS and stored.priority == 3
        assert stored.config["kind"] == JOB_KIND_INTRO_CREDITS


class TestFollowUpDedupe:
    """A file already listed in a follow-up that hasn't started isn't queued again (real JobManager)."""

    @pytest.fixture
    def jm(self, settings, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        settings["media_servers"] = [
            _server(
                "jf-1",
                "jellyfin",
                path_mappings=[{"remote_prefix": "/jf", "local_prefix": "/media", "webhook_prefixes": ["/data"]}],
            )
        ]
        manager = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: manager)
        monkeypatch.setattr(triggers, "start_intro_credits_job_async", lambda job_id: None)
        return manager

    def _existing(self, jm, paths, *, kind=JOB_KIND_INTRO_CREDITS, follows="prev-0", force=False):
        return jm.create_job(
            library_name="existing",
            kind=kind,
            config={"kind": kind, "file_paths": paths, "follows_job_id": follows, "force": force},
        )

    def _submit(self, paths, preview="prev-2"):
        return triggers.submit_webhook_follow_up(preview_job_id=preview, paths=paths, source="plex")

    def _new_follow_ups(self, jm, before):
        return [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS and j.id not in before]

    def test_file_already_queued_in_a_follow_up_that_hasnt_started_is_not_queued_again(self, jm):
        # Sonarr and Plex both report the same import: different sender paths, same local file.
        waiting = self._existing(jm, ["/data/tv/a.mkv"])
        out = self._submit(["/media/tv/a.mkv", "/media/tv/b.mkv"])
        new = self._new_follow_ups(jm, {waiting.id})
        assert [j.id for j in new] == [out]
        assert new[0].config["file_paths"] == ["/media/tv/b.mkv"]

    def test_batch_fully_covered_by_a_waiting_follow_up_creates_nothing(self, jm):
        waiting = self._existing(jm, ["/media/tv/a.mkv"])
        assert self._submit(["/media/tv/a.mkv"]) is None
        assert self._new_follow_ups(jm, {waiting.id}) == []

    @pytest.mark.parametrize("state", ["running", "revived", "finished"])
    def test_follow_up_that_already_ran_does_not_absorb(self, jm, state):
        existing = self._existing(jm, ["/media/tv/a.mkv"])
        jm.start_job(existing.id)
        if state == "revived":
            # A restart put it back to PENDING; it may already have published the old file.
            existing.status = JobStatus.PENDING
        elif state == "finished":
            jm.complete_job(existing.id)
        out = self._submit(["/media/tv/a.mkv"])
        new = self._new_follow_ups(jm, {existing.id})
        assert [j.id for j in new] == [out] and new[0].config["file_paths"] == ["/media/tv/a.mkv"]

    @pytest.mark.parametrize(
        "other",
        [
            {"kind": JOB_KIND_PREVIEWS},
            {"follows": None},  # a manual / schedule job isn't a webhook follow-up
            {"force": True},  # a forced re-detect is a different request
        ],
    )
    def test_other_pending_jobs_do_not_absorb_the_follow_up(self, jm, other):
        existing = self._existing(jm, ["/media/tv/a.mkv"], **other)
        out = self._submit(["/media/tv/a.mkv"])
        new = self._new_follow_ups(jm, {existing.id})
        assert [j.id for j in new] == [out] and new[0].config["file_paths"] == ["/media/tv/a.mkv"]


class TestWebhookFollowUp:
    @pytest.fixture
    def env(self, settings, monkeypatch):
        settings["media_servers"] = [_server("jf-1", "jellyfin")]
        jm = MagicMock()
        jm.get_job.return_value = MagicMock(library_name="Rick and Morty S01 · 2 files", priority=1)
        jm.get_pending_jobs.return_value = []
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        create = MagicMock(return_value=MagicMock(id="ic-1"))
        monkeypatch.setattr(triggers, "create_intro_credits_job", create)
        return jm, create

    @pytest.mark.parametrize(("preview_priority", "expected"), [(1, 2), (2, 2), (3, 3)])
    def test_follow_up_for_owned_files_never_outranks_its_preview_job(self, env, preview_priority, expected):
        jm, create = env
        jm.get_job.return_value.priority = preview_priority
        paths = ["/media/tv/R&M/S01/a.mkv", "/media/tv/R&M/S01/b.mkv"]
        out = triggers.submit_webhook_follow_up(
            preview_job_id="prev-1", paths=paths, source="sonarr", item_id_hints={paths[0]: {"jf-1": "x"}}
        )
        assert out == "ic-1"
        jm.get_job.assert_called_once_with("prev-1")
        create.assert_called_once_with(
            library_name="Intro & Credits · Rick and Morty S01 · 2 files",
            priority=expected,
            source="sonarr",
            file_paths=paths,
            follows_job_id="prev-1",
            item_id_hints={paths[0]: {"jf-1": "x"}},
        )

    @pytest.mark.parametrize(
        "servers",
        [[], [_server("jf-1", "jellyfin", markers={"enabled": False})], [_server("plex-1", "plex")]],
    )
    def test_no_follow_up_when_markers_are_off_everywhere(self, env, settings, servers):
        settings["media_servers"] = servers
        _jm, create = env
        # Installs that don't use Intro & Credits pay no path work on each webhook.
        with patch.object(triggers, "marker_owned_paths", side_effect=AssertionError("path work with markers off")):
            out = triggers.submit_webhook_follow_up(preview_job_id="p", paths=["/media/tv/a.mkv"], source="plex")
        assert out is None
        create.assert_not_called()

    def test_no_follow_up_for_files_no_marker_library_holds(self, env):
        _jm, create = env
        out = triggers.submit_webhook_follow_up(preview_job_id="p", paths=["/media/movies/M.mkv"], source="radarr")
        assert out is None
        create.assert_not_called()

    def test_no_follow_up_for_an_empty_batch(self, env):
        _jm, create = env
        assert triggers.submit_webhook_follow_up(preview_job_id="p", paths=[], source="sonarr") is None
        create.assert_not_called()

    def test_only_owned_files_are_queued_and_their_hints_kept(self, env):
        _jm, create = env
        owned, other = "/media/tv/Show/S01E01.mkv", "/media/movies/M.mkv"
        triggers.submit_webhook_follow_up(
            preview_job_id="prev-1",
            paths=[other, owned],
            source="jellyfin",
            item_id_hints={owned: {"jf-1": "a"}, other: {"jf-1": "b"}},
        )
        kwargs = create.call_args.kwargs
        assert kwargs["file_paths"] == [owned]
        assert kwargs["item_id_hints"] == {owned: {"jf-1": "a"}}
        assert kwargs["library_name"] == "Intro & Credits · S01E01.mkv"

    @pytest.mark.parametrize(
        ("preview", "expected"), [(None, "Intro & Credits · a.mkv"), ("", "Intro & Credits · a.mkv")]
    )
    def test_name_falls_back_to_the_file_when_the_preview_job_has_no_name(self, env, preview, expected):
        jm, create = env
        jm.get_job.return_value = None if preview is None else MagicMock(library_name=preview, priority=3)
        triggers.submit_webhook_follow_up(preview_job_id="p", paths=["/media/tv/a.mkv"], source="plex")
        assert create.call_args.kwargs["library_name"] == expected
        # A preview job that is gone gives no priority to follow: NORMAL.
        assert create.call_args.kwargs["priority"] == (2 if preview is None else 3)

    def test_several_owned_files_of_a_partly_owned_batch_are_counted_in_the_name(self, env):
        _jm, create = env
        paths = ["/media/tv/a.mkv", "/media/tv/b.mkv", "/media/movies/c.mkv"]
        triggers.submit_webhook_follow_up(preview_job_id="p", paths=paths, source="custom")
        assert create.call_args.kwargs["library_name"] == "Intro & Credits · 2 files"

    def test_two_webhooks_for_the_same_file_at_once_queue_one_follow_up(self, settings, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        settings["media_servers"] = [_server("jf-1", "jellyfin")]
        jm = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        release = threading.Event()
        real_create_job = jm.create_job

        def slow_create_job(**kwargs):
            release.wait(0.05)  # widen the check-then-create window
            return real_create_job(**kwargs)

        monkeypatch.setattr(jm, "create_job", slow_create_job)
        results = []
        start = threading.Barrier(2)

        def webhook(preview_id):
            start.wait()
            results.append(
                triggers.submit_webhook_follow_up(preview_job_id=preview_id, paths=["/media/tv/a.mkv"], source="plex")
            )

        with patch.object(triggers, "start_intro_credits_job_async"):
            threads = [threading.Thread(target=webhook, args=(f"prev-{i}",)) for i in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
        assert len(results) == 2 and results.count(None) == 1
        assert len([j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]) == 1
