"""Creating Intro & Credits jobs: the markers switch, the job config, and webhook follow-ups."""

import os
import threading
from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS, JOB_KIND_PREVIEWS
from media_preview_generator.markers import triggers
from media_preview_generator.markers.decide import DecisionStatus
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
        from datetime import datetime

        jm = MagicMock()
        jm.create_job.return_value = MagicMock(id="retry-1")
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        monkeypatch.setattr(triggers, "_utcnow", lambda: datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
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

    def test_verify_job_config_carries_its_due_time_and_no_attempt(self, monkeypatch):
        from datetime import datetime

        jm = MagicMock()
        jm.create_job.return_value = MagicMock(id="verify-1")
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        monkeypatch.setattr(triggers, "_utcnow", lambda: datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
        with patch.object(triggers, "start_intro_credits_job_async"):
            triggers.create_intro_credits_job(
                library_name="Verify: a",
                priority=2,
                source="sonarr",
                file_paths=["/data/tv/a.mkv"],
                item_id_hints={"/data/tv/a.mkv": {"jf-1": "abc"}},
                retry_delay_s=600,
                verify=True,
            )
        assert jm.create_job.call_args.kwargs["config"] == {
            "kind": "intro_credits",
            "source": "sonarr",
            "libraries": [],
            "file_paths": ["/data/tv/a.mkv"],
            "follows_job_id": None,
            "force": False,
            "webhook_item_id_hints": {"/data/tv/a.mkv": {"jf-1": "abc"}},
            "verify": True,
            "retry_delay": 600,
            "retry_not_before": "2026-09-14T10:10:00+00:00",
        }

    @pytest.mark.parametrize(("delay", "delayed"), [(68_700, True), (0, False)], ids=["delayed", "due-now"])
    def test_a_theintrodb_recheck_carries_its_due_time_only_when_delayed(self, monkeypatch, delay, delayed):
        from datetime import datetime

        jm = MagicMock()
        jm.create_job.return_value = MagicMock(id="recheck-1")
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        monkeypatch.setattr(triggers, "_utcnow", lambda: datetime(2026, 9, 24, 5, 0, tzinfo=UTC))
        with patch.object(triggers, "start_intro_credits_job_async"):
            triggers.create_intro_credits_job(
                library_name="TheIntroDB recheck: 1 files",
                priority=3,
                source="theintrodb_recheck",
                file_paths=["/m/a.mkv"],
                retry_delay_s=delay,
            )
        config = jm.create_job.call_args.kwargs["config"]
        timing = {k: config[k] for k in ("retry_delay", "retry_not_before") if k in config}
        assert timing == ({"retry_delay": 68_700, "retry_not_before": "2026-09-25T00:05:00+00:00"} if delayed else {})
        assert "retry_attempt" not in config and "is_retry" not in config

    @pytest.mark.parametrize(
        ("kwargs", "stored"),
        [
            ({"retry_attempt": 2, "verify_chain": True}, {"retry_attempt": 2, "verify_chain": True}),
            ({"retry_attempt": 2, "verify_chain": False}, {"retry_attempt": 2}),
            ({"verify": True, "chain_attempt": 2}, {"verify": True, "chain_attempt": 2}),
            ({"verify": True, "chain_attempt": 0}, {"verify": True}),
        ],
        ids=["retry-in-verify-chain", "retry", "verify-after-retries", "verify"],
    )
    def test_verify_chain_fields_are_stored_only_when_set(self, monkeypatch, kwargs, stored):
        jm = MagicMock()
        jm.create_job.return_value = MagicMock(id="job-1")
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async"):
            triggers.create_intro_credits_job(
                library_name="a", priority=2, source="sonarr", file_paths=["/m/a.mkv"], retry_delay_s=60, **kwargs
            )
        config = jm.create_job.call_args.kwargs["config"]
        chain_keys = {"retry_attempt", "verify", "verify_chain", "chain_attempt"}
        assert {k: v for k, v in config.items() if k in chain_keys} == stored

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

    def test_follow_up_for_a_markers_only_library_reaches_the_pipeline_with_its_owner(
        self, settings, tmp_path, monkeypatch
    ):
        # Previews off on "Anime", Intro & Credits on; Sonarr's /data view maps to this app's <tmp>/media.
        from types import SimpleNamespace

        from media_preview_generator.markers import job_runner, pipeline
        from media_preview_generator.servers.registry import ServerRegistry
        from media_preview_generator.web.jobs import JobManager

        episode = tmp_path / "media" / "anime" / "Show" / "Show - S01E01.mkv"
        episode.parent.mkdir(parents=True)
        episode.write_bytes(b"x")
        settings["media_servers"] = [
            _server(
                "jf-1",
                "jellyfin",
                libraries=[{"id": "a", "name": "Anime", "remote_paths": ["/jfmedia/anime"], "enabled": False}],
                path_mappings=[
                    {
                        "remote_prefix": "/jfmedia",
                        "local_prefix": str(tmp_path / "media"),
                        "webhook_prefixes": ["/data"],
                    }
                ],
            )
        ]
        jm = JobManager(config_dir=str(tmp_path / "config"))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        preview = jm.create_job(library_name="Show S01E01", config={"webhook_paths": ["/data/anime/Show/x.mkv"]})

        with patch.object(triggers, "start_intro_credits_job_async"):
            job_id = triggers.submit_webhook_follow_up(
                preview_job_id=preview.id, paths=["/data/anime/Show/Show - S01E01.mkv"], source="sonarr"
            )
        assert job_id is not None

        registry = ServerRegistry.from_settings(settings["media_servers"])
        items, _warnings, _sent = job_runner.build_items(jm.get_job(job_id).config, registry=registry)
        owning = pipeline._owning_servers(items[0], SimpleNamespace(registry=registry))
        owners = pipeline._marker_owners(owning, items[0].canonical_path)

        assert items[0].canonical_path == str(episode)
        assert [owner.config.id for owner in owners] == ["jf-1"]

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


class TestRedetect:
    """Inspector re-detect: one forced HIGH job per file while one is queued or running."""

    PATH = "/media/tv/Show/S01E01.mkv"

    @pytest.fixture
    def jm(self, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        jm = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async"):
            yield jm

    def _ic_jobs(self, jm):
        return [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]

    def test_first_request_creates_a_forced_high_priority_single_file_job(self, jm):
        job_id = triggers.submit_redetect(self.PATH)
        (job,) = self._ic_jobs(jm)
        assert job.id == job_id
        assert job.priority == 1
        assert job.library_name == "Intro & Credits: S01E01.mkv"
        assert job.config["file_paths"] == [self.PATH]
        assert job.config["force"] is True and job.config["source"] == "inspector"

    @pytest.mark.parametrize("state", ["pending", "running", "paused"])
    def test_request_while_one_is_queued_or_running_reuses_it(self, jm, state):
        first = triggers.submit_redetect(self.PATH)
        if state != "pending":
            jm.start_job(first)
        if state == "paused":
            jm.request_pause(first)
        assert triggers.submit_redetect(self.PATH) == first
        assert len(self._ic_jobs(jm)) == 1

    @pytest.mark.parametrize("end", ["complete", "fail", "cancel"])
    def test_request_after_the_last_one_ended_creates_a_new_job(self, jm, end):
        first = triggers.submit_redetect(self.PATH)
        jm.start_job(first)
        {
            "complete": lambda: jm.complete_job(first),
            "fail": lambda: jm.complete_job(first, error="boom"),
            "cancel": lambda: jm.cancel_job(first),
        }[end]()
        second = triggers.submit_redetect(self.PATH)
        assert second != first
        assert len(self._ic_jobs(jm)) == 2

    @pytest.mark.parametrize(
        "other",
        [
            {"source": "inspector", "file_paths": ["/media/tv/Show/S01E02.mkv"], "force": True},  # another file
            {"source": "sonarr", "file_paths": [PATH], "force": False, "follows_job_id": "p"},  # webhook follow-up
            {"source": "manual", "file_paths": [PATH], "force": True},  # API job, maybe Low priority
            {"source": "inspector", "file_paths": [PATH], "force": False},
            {"source": "inspector", "file_paths": [PATH, "/media/tv/Show/S01E02.mkv"], "force": True},
        ],
        ids=["other-file", "webhook-follow-up", "not-inspector", "inspector-not-forced", "several-files"],
    )
    def test_other_queued_jobs_listing_the_file_are_not_reused(self, jm, other):
        existing = jm.create_job(
            library_name="x", kind=JOB_KIND_INTRO_CREDITS, config={"kind": JOB_KIND_INTRO_CREDITS, **other}
        )
        job_id = triggers.submit_redetect(self.PATH)
        assert job_id != existing.id
        assert jm.get_job(job_id).config["file_paths"] == [self.PATH]

    def test_preview_job_is_never_reused(self, jm):
        preview = jm.create_job(
            library_name="x", config={"source": "inspector", "file_paths": [self.PATH], "force": True}
        )
        assert triggers.submit_redetect(self.PATH) != preview.id

    def test_a_job_only_counting_down_to_its_retry_is_not_reused(self, jm):
        # Its forced run is done; its hidden retry forces nothing, so a second Re-detect gets a job of its own.
        first = triggers.submit_redetect(self.PATH)
        jm.start_job(first)
        _schedule_retry(jm, first)
        second = triggers.submit_redetect(self.PATH)
        assert second != first
        assert (jm.get_job(second).config["force"], jm.get_job(second).config["file_paths"]) == (True, [self.PATH])

    def test_double_click_creates_one_job(self, jm, monkeypatch):
        real_create_job = jm.create_job

        def slow_create_job(**kwargs):
            threading.Event().wait(0.05)  # widen the check-then-create window
            return real_create_job(**kwargs)

        monkeypatch.setattr(jm, "create_job", slow_create_job)
        start = threading.Barrier(2)
        results = []

        def click():
            start.wait()
            results.append(triggers.submit_redetect(self.PATH))

        threads = [threading.Thread(target=click) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(results) == 2 and results[0] == results[1]
        assert len(self._ic_jobs(jm)) == 1


def _schedule_retry(jm, job_id):
    """Put a job into the state markers/job_runner.py leaves it in when its files still wait: a scheduled retry."""
    jm.upsert_retry_chain_job(
        canonical_path="",
        basename="",
        attempt=1,
        max_attempts=3,
        next_run_at="2026-09-23T10:01:00+00:00",
        wait_seconds=60,
        outcome="scheduled",
        originating_job_id=job_id,
    )
    assert jm.get_job(job_id).status.value == "pending"


class TestSeasonPublish:
    """Season view "Publish": one NORMAL-priority, not forced job over the season group's episodes while one is queued
    or running."""

    @pytest.fixture
    def jm(self, tmp_path, monkeypatch):
        from media_preview_generator.web.jobs import JobManager

        jm = JobManager(config_dir=str(tmp_path / "config"))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async"):
            yield jm

    @staticmethod
    def _files(folder, *names):
        folder.mkdir(parents=True, exist_ok=True)
        for name in names:
            (folder / name).write_bytes(b"x")
        return [str(folder / name) for name in names]

    @pytest.fixture
    def season(self, tmp_path):
        folder = tmp_path / "tv" / "Show" / "Season 01"
        return self._files(folder, "Show - S01E01.mkv", "Show - S01E02.mkv", "Show - S01E03.mkv")

    def _ic_jobs(self, jm):
        return [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]

    def test_creates_a_normal_priority_job_for_the_seasons_episodes(self, jm, season):
        job_id = triggers.submit_season_publish(season[1])
        (job,) = self._ic_jobs(jm)
        assert (job.id, job.priority, job.library_name) == (job_id, 2, "Intro & Credits: Show · Season 1")
        assert job.config["file_paths"] == season
        assert job.config["source"] == "inspector_season"
        assert job.config["force"] is False

    def test_a_flat_folder_queues_only_the_asked_seasons_episodes(self, jm, tmp_path):
        folder = tmp_path / "tv" / "Flat Show"
        s01 = self._files(folder, "Flat Show - S01E01.mkv", "Flat Show - S01E02.mkv")
        s02 = self._files(folder, "Flat Show - S02E01.mkv", "Flat Show - S02E02.mkv")
        self._files(folder, "Flat Show - S01E01-trailer.mkv")

        first = triggers.submit_season_publish(s01[1])
        second = triggers.submit_season_publish(s02[0])

        assert jm.get_job(first).config["file_paths"] == s01
        assert jm.get_job(second).config["file_paths"] == s02
        assert second != first
        # Named by the parsed season, so the two jobs of one folder can be told apart.
        assert jm.get_job(first).library_name == "Intro & Credits: Flat Show · Season 1"
        assert jm.get_job(second).library_name == "Intro & Credits: Flat Show · Season 2"

    @pytest.mark.parametrize(
        ("folder", "name", "expected"),
        [
            ("Specials", "Show - S00E01.mkv", "Intro & Credits: Show · Specials"),
            ("Season 00", "Show - S00E01.mkv", "Intro & Credits: Show · Specials"),
            ("Series 3", "Show - S03E01.mkv", "Intro & Credits: Show · Season 3"),
            (None, "Show - S00E01.mkv", "Intro & Credits: Show · Specials"),  # specials in a flat show folder
        ],
        ids=["specials-folder", "season-00-folder", "series-folder", "flat-specials"],
    )
    def test_job_name_is_the_show_and_the_parsed_season(self, jm, tmp_path, folder, name, expected):
        show = tmp_path / "tv" / "Show"
        (episode,) = self._files(show / folder if folder else show, name)
        assert jm.get_job(triggers.submit_season_publish(episode)).library_name == expected

    @pytest.mark.parametrize("state", ["pending", "running", "paused"])
    def test_any_episode_of_the_season_clicked_again_while_queued_or_running_returns_the_same_job(
        self, jm, season, state
    ):
        first = triggers.submit_season_publish(season[0])
        if state != "pending":
            jm.start_job(first)
        if state == "paused":
            jm.request_pause(first)
        assert triggers.submit_season_publish(season[2]) == first
        assert len(self._ic_jobs(jm)) == 1

    def test_a_finished_job_is_not_reused(self, jm, season):
        first = triggers.submit_season_publish(season[0])
        jm.start_job(first)
        jm.complete_job(first)
        assert triggers.submit_season_publish(season[0]) != first

    def test_a_season_whose_episodes_changed_since_gets_a_new_job(self, jm, season, tmp_path):
        first = triggers.submit_season_publish(season[0])
        (e04,) = self._files(tmp_path / "tv" / "Show" / "Season 01", "Show - S01E04.mkv")
        second = triggers.submit_season_publish(season[0])
        assert second != first
        assert jm.get_job(second).config["file_paths"] == [*season, e04]

    @pytest.mark.parametrize(
        ("source", "paths", "force"),
        [
            ("manual", "season", False),  # an API job for the same files
            ("inspector", "season", True),  # a re-detect
            ("inspector_season", "folder", False),  # an older publish of the whole folder
            ("inspector_season", "fewer", False),  # the season before an episode arrived
        ],
        ids=["api-job", "redetect", "folder", "other-paths"],
    )
    def test_other_queued_jobs_are_not_reused(self, jm, season, source, paths, force):
        file_paths = {"season": season, "folder": [os.path.dirname(season[0])], "fewer": season[:2]}[paths]
        config = {"kind": JOB_KIND_INTRO_CREDITS, "source": source, "file_paths": file_paths, "force": force}
        existing = jm.create_job(library_name="x", kind=JOB_KIND_INTRO_CREDITS, config=config)
        assert triggers.submit_season_publish(season[0]) != existing.id

    def test_a_preview_job_is_never_reused(self, jm, season):
        preview = jm.create_job(library_name="x", config={"source": "inspector_season", "file_paths": season})
        assert triggers.submit_season_publish(season[0]) != preview.id

    def test_a_job_only_counting_down_to_its_retry_is_not_reused(self, jm, season):
        # It has published; its hidden retry lists only the files still waiting.
        first = triggers.submit_season_publish(season[0])
        jm.start_job(first)
        _schedule_retry(jm, first)
        second = triggers.submit_season_publish(season[0])
        assert second != first
        assert jm.get_job(second).config["file_paths"] == season


class TestInReviewRedecide:
    """The one job queued after settings v16 removed "Publish when: High": every file in Needs review, decided again
    from its stored answers (no lookup, no detector, no probe)."""

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        from media_preview_generator.markers.store import MarkerStore

        store = MarkerStore(str(tmp_path / "markers.db"))
        monkeypatch.setattr(triggers, "get_marker_store", lambda: store)
        yield store
        store.close()

    @pytest.fixture
    def jm(self, tmp_path, monkeypatch, settings):
        from media_preview_generator.web.jobs import JobManager

        settings["media_servers"] = [_server("jf-1", "jellyfin")]
        jm = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async"):
            yield jm

    @staticmethod
    def _decided(store, path, status):
        from media_preview_generator.markers.decide import TypeDecision
        from media_preview_generator.markers.models import FileIdentity, Marker, MarkerType

        rec = store.upsert_file(FileIdentity(path, 1, 1), duration_ms=1_000_000, season_key=None, is_movie=True)
        marker = Marker(MarkerType.CREDITS, 900_000, 1_000_000, ("credits_text",))
        shown = (
            {"marker": marker, "proposed": None}
            if status is DecisionStatus.DECIDED
            else {"marker": None, "proposed": marker}
        )
        decision = TypeDecision(MarkerType.CREDITS, status, reason="x", **shown)
        store.save_decisions(rec.id, {MarkerType.CREDITS: decision}, settings_fingerprint="old")
        return rec

    @classmethod
    def _last_row(cls, store, path, status, message):
        rec = cls._decided(store, path, DecisionStatus.DECIDED)
        store.set_publish_state(rec.id, "plex-1", item_id="7", markers=None, status=status, message=message)

    def _ic_jobs(self, jm):
        return [j for j in jm.get_all_jobs() if j.kind == JOB_KIND_INTRO_CREDITS]

    def test_it_queues_one_job_for_exactly_the_files_in_review_or_waiting_for_other_versions(self, jm, store):
        from media_preview_generator.markers import job_runner

        self._decided(store, "/media/tv/B/S01/e2.mkv", DecisionStatus.NEEDS_REVIEW)
        self._decided(store, "/media/movies/A/a.mkv", DecisionStatus.NEEDS_REVIEW)
        self._decided(store, "/media/tv/B/S01/e1.mkv", DecisionStatus.DECIDED)
        self._decided(store, "/media/tv/B/S01/e3.mkv", DecisionStatus.NO_EVIDENCE)
        versions = "Waiting for this item's other versions to agree on: credits"
        self._last_row(store, "/media/movies/C/c - 4K.mkv", "waiting", versions)
        self._last_row(store, "/media/movies/D/d.mkv", "waiting", "Not in this server's library yet")  # retried
        self._last_row(store, "/media/movies/E/e.mkv", "written", "1 marker(s)")

        job_id = triggers.submit_decide_again()

        (job,) = self._ic_jobs(jm)
        assert job.id == job_id
        assert (job.library_name, job.priority) == ("Intro & Credits: Needs review and waiting files, decided again", 2)
        assert job.config == {
            "kind": JOB_KIND_INTRO_CREDITS,
            "source": "decide_again",
            "libraries": [],
            "file_paths": [],
            "follows_job_id": None,
            "force": False,
            "webhook_item_id_hints": {},
            "decide_again": True,
            "stored_answers_only": True,
        }
        # The job lists them when it runs: exactly the files with a type in Needs review, and the files whose last
        # row waits for their item's other versions.
        assert [i.canonical_path for i in job_runner._items_to_decide_again(store)] == [
            "/media/movies/A/a.mkv",
            "/media/movies/C/c - 4K.mkv",
            "/media/tv/B/S01/e2.mkv",
        ]

    def test_files_waiting_for_their_items_other_versions_alone_are_enough(self, jm, store):
        self._last_row(
            store, "/media/movies/C/c - 4K.mkv", "waiting", "Waiting for this item's other versions to agree on: intro"
        )
        assert triggers.submit_decide_again() == self._ic_jobs(jm)[0].id

    @pytest.mark.parametrize("state", ["pending", "running"])
    def test_a_second_request_while_it_is_queued_or_running_reuses_it(self, jm, store, state):
        self._decided(store, "/media/movies/A/a.mkv", DecisionStatus.NEEDS_REVIEW)
        first = triggers.submit_decide_again()
        if state == "running":
            jm.start_job(first)
        assert triggers.submit_decide_again() == first
        assert len(self._ic_jobs(jm)) == 1

    def test_nothing_is_queued_when_no_file_is_in_review_or_waiting_for_other_versions(self, jm, store):
        self._decided(store, "/media/movies/A/a.mkv", DecisionStatus.DECIDED)
        self._last_row(store, "/media/movies/D/d.mkv", "waiting", "Not in this server's library yet")
        assert triggers.submit_decide_again() is None
        assert self._ic_jobs(jm) == []

    def test_nothing_is_queued_when_intro_and_credits_is_off_everywhere(self, jm, store, settings):
        settings["media_servers"] = [_server("jf-1", "jellyfin", markers={"enabled": False})]
        self._decided(store, "/media/movies/A/a.mkv", DecisionStatus.NEEDS_REVIEW)
        assert triggers.submit_decide_again() is None
        assert self._ic_jobs(jm) == []
