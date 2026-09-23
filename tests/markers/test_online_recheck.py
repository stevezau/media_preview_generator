"""The weekly online re-check: which files it lists, the one job it queues, and when it is due."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.job_kinds import JOB_KIND_INTRO_CREDITS
from media_preview_generator.markers import job_runner, pipeline, triggers
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.store import MarkerStore
from tests.markers.test_triggers import _server

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
WEEK = timedelta(days=7)
ALL_ONLINE = ("theintrodb", "introdb", "skipdb")


def _settings(*online: str):
    """Global settings with only these online sources on (chapters and season audio always on)."""
    sources = [{"id": "chapters", "enabled": True}, {"id": "season_audio", "enabled": True}]
    sources += [{"id": source, "enabled": source in online} for source in ALL_ONLINE]
    return load_global(validate_global({"sources": sources}, None)[0])


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return _Clock(NOW)


@pytest.fixture
def store(tmp_path, clock):
    store = MarkerStore(str(tmp_path / "markers.db"), clock=clock)
    yield store
    store.close()


def _add(
    store,
    clock,
    tmp_path,
    name,
    *,
    lookups=((Source.THEINTRODB, False, 15),),
    decided_by=None,
    status=DecisionStatus.NEEDS_REVIEW,
    locked=False,
    on_disk=True,
):
    """A file with its online lookups (source, found, days old) and one intro decision.

    ``decided_by`` is the decided intro's sources (for DECIDED); None stores no decision at all when ``status`` is None.
    """
    path = tmp_path / "media" / f"{name}.mkv"
    path.parent.mkdir(parents=True, exist_ok=True)
    if on_disk:
        path.write_bytes(b"x")
    rec = store.upsert_file(FileIdentity(str(path), 1, 1), duration_ms=1_000_000, season_key=None, is_movie=True)
    for source, found, days in lookups:
        clock.now = NOW - timedelta(days=days)
        candidates = [Candidate(MarkerType.INTRO, 10_000, 40_000, source)] if found else []
        store.replace_evidence(rec.id, source, candidates, version=pipeline.PARSER_VERSIONS[source])
    clock.now = NOW
    if status is not None:
        marker = Marker(MarkerType.INTRO, 10_000, 40_000, tuple(decided_by or ("chapters",)))
        shown = (
            {"marker": marker, "proposed": None}
            if status is DecisionStatus.DECIDED
            else {"marker": None, "proposed": marker}
        )
        decision = TypeDecision(MarkerType.INTRO, status, reason="x", **shown)
        store.save_decisions(rec.id, {MarkerType.INTRO: decision}, settings_fingerprint="f")
        if locked:
            store.lock_marker(rec.id, marker)
    return str(path)


class TestFilesWithOldEmptyLookups:
    """The store's half: a stored "no entry" of one of the given sources, older than the cutoff."""

    @pytest.mark.parametrize(
        ("lookups", "listed"),
        [
            (((Source.THEINTRODB, False, 15),), True),
            (((Source.THEINTRODB, False, 13),), False),  # not due yet
            (((Source.THEINTRODB, True, 30),), False),  # an entry is never asked again for being old
            (((Source.INTRODB, False, 15),), False),  # a source not asked about
            (((Source.INTRODB, False, 15), (Source.THEINTRODB, False, 1)), False),
            (((Source.INTRODB, True, 15), (Source.THEINTRODB, False, 20)), True),  # per source
        ],
        ids=["old-no-entry", "fresh-no-entry", "old-entry", "other-source", "only-other-source-old", "per-source"],
    )
    def test_only_an_old_no_entry_of_a_given_source_is_listed(self, store, clock, tmp_path, lookups, listed):
        path = _add(store, clock, tmp_path, "a", lookups=lookups)
        assert store.files_with_old_empty_lookups([Source.THEINTRODB], NOW - timedelta(days=14)) == (
            [path] if listed else []
        )

    def test_no_source_lists_nothing(self, store, clock, tmp_path):
        _add(store, clock, tmp_path, "a")
        assert store.files_with_old_empty_lookups([], NOW) == []


class TestOnlineRecheckFiles:
    """Which files the weekly job lists: a due "no entry" of an enabled online source, on a file whose decision an
    online answer could still change."""

    @pytest.mark.parametrize(
        ("status", "decided_by", "locked", "listed"),
        [
            (DecisionStatus.NEEDS_REVIEW, None, False, True),
            (DecisionStatus.NO_EVIDENCE, None, False, True),
            (None, None, False, True),  # never decided: treated as undecided, as the TheIntroDB recheck does
            (DecisionStatus.DECIDED, ("season_audio",), False, True),
            (DecisionStatus.DECIDED, ("season_audio", "server_markers"), False, True),
            (DecisionStatus.DECIDED, ("season_audio_previous", "season_audio"), False, True),
            (DecisionStatus.DECIDED, ("chapters",), False, False),
            (DecisionStatus.DECIDED, ("chapters", "server_markers"), False, False),
            (DecisionStatus.DECIDED, ("chapters", "theintrodb"), False, False),
            (DecisionStatus.DECIDED, ("season_audio", "credits_text"), False, False),
            (DecisionStatus.DECIDED, ("season_audio",), True, False),  # the user's lock: detection never changes it
        ],
        ids=[
            "needs-review",
            "nothing-found",
            "never-decided",
            "season-audio-alone",
            "season-audio-and-server",
            "season-audio-and-previous-season",
            "chapters-alone",
            "chapters-and-server",
            "chapters-and-online",
            "season-audio-and-another-source",
            "locked",
        ],
    )
    def test_a_due_file_is_listed_when_an_online_answer_could_change_its_decision(
        self, store, clock, tmp_path, status, decided_by, locked, listed
    ):
        path = _add(store, clock, tmp_path, "a", status=status, decided_by=decided_by, locked=locked)
        assert list(pipeline.online_recheck_files(store, _settings(*ALL_ONLINE), NOW)) == ([path] if listed else [])

    @pytest.mark.parametrize(
        ("lookups", "listed"),
        [
            (((Source.THEINTRODB, False, 15),), True),
            (((Source.THEINTRODB, False, 14),), False),  # _needs_lookup asks again only once it is OLDER than 14 days
            (((Source.THEINTRODB, False, 13),), False),
            (((Source.THEINTRODB, True, 40),), False),
            ((), False),  # never asked: any run asks it, nothing to re-check
        ],
        ids=["due", "exactly-14-days", "not-due", "has-an-entry", "never-asked"],
    )
    def test_only_a_no_entry_older_than_the_retry_is_due(self, store, clock, tmp_path, lookups, listed):
        path = _add(store, clock, tmp_path, "a", lookups=lookups)
        assert list(pipeline.online_recheck_files(store, _settings(*ALL_ONLINE), NOW)) == ([path] if listed else [])

    @pytest.mark.parametrize(
        ("online", "listed"),
        [(("introdb",), True), (("theintrodb", "skipdb"), False), ((), False)],
        ids=["its-source-on", "its-source-off", "every-online-source-off"],
    )
    def test_only_an_enabled_sources_no_entry_counts(self, store, clock, tmp_path, online, listed):
        path = _add(store, clock, tmp_path, "a", lookups=((Source.INTRODB, False, 20), (Source.THEINTRODB, False, 2)))
        assert list(pipeline.online_recheck_files(store, _settings(*online), NOW)) == ([path] if listed else [])

    def test_a_file_gone_from_disk_is_not_listed(self, store, clock, tmp_path):
        kept = _add(store, clock, tmp_path, "kept")
        _add(store, clock, tmp_path, "gone", on_disk=False)
        assert list(pipeline.online_recheck_files(store, _settings(*ALL_ONLINE), NOW)) == [kept]


class TestSubmit:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch, store, clock):
        from media_preview_generator.web.jobs import JobManager

        state = {"media_servers": [_server("jf-1", "jellyfin")]}
        sm = MagicMock()
        sm.get.side_effect = lambda key, default=None: state.get(key, default)
        monkeypatch.setattr(triggers, "get_settings_manager", lambda: sm)
        online = {"settings": _settings(*ALL_ONLINE)}
        monkeypatch.setattr(triggers, "get_global_settings", lambda: online["settings"])
        monkeypatch.setattr(triggers, "get_marker_store", lambda: store)
        monkeypatch.setattr(triggers, "_utcnow", lambda: NOW)
        jm = JobManager(config_dir=str(tmp_path))
        monkeypatch.setattr(triggers, "get_job_manager", lambda: jm)
        with patch.object(triggers, "start_intro_credits_job_async") as start:
            yield SimpleNamespace(jm=jm, state=state, online=online, store=store, clock=clock, start=start)

    @staticmethod
    def _jobs(jm):
        return [job for job in jm.get_all_jobs() if job.kind == JOB_KIND_INTRO_CREDITS]

    def test_it_queues_one_low_priority_job_that_lists_the_due_files_when_it_runs(self, env, tmp_path):
        due = _add(env.store, env.clock, tmp_path, "due")
        _add(env.store, env.clock, tmp_path, "not-due", lookups=((Source.THEINTRODB, False, 3),))
        _add(env.store, env.clock, tmp_path, "chapters", status=DecisionStatus.DECIDED, decided_by=("chapters",))

        job_id = triggers.submit_online_recheck()

        (job,) = self._jobs(env.jm)
        assert job.id == job_id
        assert (job.library_name, job.priority) == ("Intro & Credits: weekly online re-check", 3)
        assert job.config == {
            "kind": JOB_KIND_INTRO_CREDITS,
            "source": "online_recheck",
            "libraries": [],
            "file_paths": [],
            "follows_job_id": None,
            "force": False,
            "webhook_item_id_hints": {},
            "online_recheck": True,
        }
        env.start.assert_called_once_with(job_id)
        ctx = SimpleNamespace(store=env.store, settings=env.online["settings"], now=lambda: NOW)
        assert [item.canonical_path for item in job_runner._items_for_online_recheck(ctx, lambda: False)] == [due]

    def test_nothing_is_queued_when_no_file_is_due(self, env, tmp_path):
        _add(env.store, env.clock, tmp_path, "not-due", lookups=((Source.THEINTRODB, False, 3),))
        assert triggers.submit_online_recheck() is None
        assert self._jobs(env.jm) == []

    def test_nothing_is_queued_when_every_online_source_is_off(self, env, tmp_path):
        _add(env.store, env.clock, tmp_path, "due")
        env.online["settings"] = _settings()
        assert triggers.submit_online_recheck() is None
        assert self._jobs(env.jm) == []

    def test_nothing_is_queued_when_intro_and_credits_is_off_everywhere(self, env, tmp_path):
        _add(env.store, env.clock, tmp_path, "due")
        env.state["media_servers"] = [_server("jf-1", "jellyfin", markers={"enabled": False})]
        assert triggers.submit_online_recheck() is None
        assert self._jobs(env.jm) == []

    @pytest.mark.parametrize("state", ["pending", "running"])
    def test_one_waiting_or_running_is_returned_and_nothing_else_queued(self, env, tmp_path, state):
        _add(env.store, env.clock, tmp_path, "due")
        first = triggers.submit_online_recheck()
        if state == "running":
            env.jm.start_job(first)
        assert triggers.submit_online_recheck() == first
        assert [job.id for job in self._jobs(env.jm)] == [first]

    @pytest.mark.parametrize("end", ["complete", "cancel", "fail"])
    def test_one_that_ended_doesnt_stop_the_next(self, env, tmp_path, end):
        _add(env.store, env.clock, tmp_path, "due")
        first = triggers.submit_online_recheck()
        env.jm.start_job(first)
        if end == "complete":
            env.jm.complete_job(first)
        elif end == "cancel":
            env.jm.cancel_job(first)
        else:
            env.jm.complete_job(first, error="boom")
        second = triggers.submit_online_recheck()
        assert second not in (None, first)
        assert [job.id for job in env.jm.get_pending_jobs()] == [second]

    def test_listing_stops_at_the_first_due_file(self, env, tmp_path, monkeypatch):
        _add(env.store, env.clock, tmp_path, "a")
        _add(env.store, env.clock, tmp_path, "b")
        reached = []

        def listed(store, settings, now):
            for path in ("/a.mkv", "/b.mkv"):
                reached.append(path)
                yield path

        monkeypatch.setattr(triggers, "online_recheck_files", listed)
        assert triggers.submit_online_recheck() is not None
        assert reached == ["/a.mkv"]

    def test_other_waiting_jobs_are_not_reused(self, env, tmp_path):
        _add(env.store, env.clock, tmp_path, "due")
        other = triggers.create_intro_credits_job(
            library_name="x", priority=3, source="decide_again", decide_again=True
        )
        assert triggers.submit_online_recheck() not in (None, other.id)

    def test_it_takes_the_theintrodb_rechecks_lock_to_find_or_create_the_job(self, env, tmp_path):
        _add(env.store, env.clock, tmp_path, "due")
        results = []
        with job_runner.FOLLOW_UP_LOCK:
            thread = threading.Thread(target=lambda: results.append(triggers.submit_online_recheck()))
            thread.start()
            thread.join(timeout=0.3)
            assert thread.is_alive() and self._jobs(env.jm) == []
        thread.join(timeout=5)
        assert len(results) == 1 and [job.id for job in self._jobs(env.jm)] == results

    def test_two_requests_at_once_queue_one_job(self, env, tmp_path, monkeypatch):
        _add(env.store, env.clock, tmp_path, "due")
        real_create_job = env.jm.create_job

        def slow_create_job(**kwargs):
            threading.Event().wait(0.05)  # widen the check-then-create window
            return real_create_job(**kwargs)

        monkeypatch.setattr(env.jm, "create_job", slow_create_job)
        results = []
        start = threading.Barrier(2)

        def submit():
            start.wait()
            results.append(triggers.submit_online_recheck())

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert len(results) == 2 and len(set(results)) == 1
        assert len(self._jobs(env.jm)) == 1


class _FakeTimer:
    armed: list[_FakeTimer] = []

    def __init__(self, interval, function):
        self.interval = interval
        self.function = function
        self.cancelled = False
        self.started = False
        self.daemon = False
        self.name = ""
        _FakeTimer.armed.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True


class TestSchedule:
    @pytest.fixture
    def timers(self, monkeypatch, store):
        _FakeTimer.armed = []
        monkeypatch.setattr(triggers, "threading", SimpleNamespace(Timer=_FakeTimer))
        monkeypatch.setattr(triggers, "_online_recheck_timer", None)
        monkeypatch.setattr(triggers, "_utcnow", lambda: NOW)
        monkeypatch.setattr(triggers, "get_marker_store", lambda: store)
        return _FakeTimer.armed

    def test_the_first_start_sets_it_a_week_ahead(self, timers, store):
        triggers.schedule_online_recheck()
        assert store.online_recheck_due() == NOW + WEEK
        (timer,) = timers
        assert (timer.interval, timer.function, timer.started, timer.daemon) == (
            WEEK.total_seconds(),
            triggers._run_online_recheck,
            True,
            True,
        )

    def test_the_due_time_survives_a_restart(self, timers, store, tmp_path, monkeypatch):
        store.set_online_recheck_due(NOW + timedelta(days=3))
        store.close()
        reopened = MarkerStore(str(tmp_path / "markers.db"))  # the next process
        monkeypatch.setattr(triggers, "get_marker_store", lambda: reopened)
        try:
            triggers.schedule_online_recheck()
            assert reopened.online_recheck_due() == NOW + timedelta(days=3)
        finally:
            reopened.close()
        assert [timer.interval for timer in timers] == [timedelta(days=3).total_seconds()]

    def test_one_that_passed_while_the_app_was_down_fires_at_once(self, timers, store):
        store.set_online_recheck_due(NOW - timedelta(days=2))
        triggers.schedule_online_recheck()
        assert [timer.interval for timer in timers] == [0.0]

    @pytest.mark.parametrize(
        "stored",
        ["not a time", "2026-09-30T12:00:00"],  # the second has no time zone: comparing it with now would raise
        ids=["unreadable", "no-time-zone"],
    )
    def test_a_stored_time_that_cant_be_used_fires_at_once(self, timers, store, stored):
        with store._tx() as conn:
            conn.execute("INSERT INTO meta(key, value) VALUES ('online_recheck_due', ?)", (stored,))
        triggers.schedule_online_recheck()  # never raises
        assert [timer.interval for timer in timers] == [0.0]

    def test_one_further_off_than_a_week_is_brought_to_a_week(self, timers, store):
        store.set_online_recheck_due(NOW + timedelta(days=400))  # the clock was set back since it was stored
        triggers.schedule_online_recheck()
        assert [timer.interval for timer in timers] == [WEEK.total_seconds()]

    def test_arming_again_cancels_the_previous_timer(self, timers):
        triggers.schedule_online_recheck()
        triggers.schedule_online_recheck()
        assert [timer.cancelled for timer in timers] == [True, False]

    def test_a_store_that_cant_be_read_arms_nothing(self, timers, monkeypatch):
        broken = MagicMock()
        broken.online_recheck_due.side_effect = OSError("markers.db is locked")
        monkeypatch.setattr(triggers, "get_marker_store", lambda: broken)
        triggers.schedule_online_recheck()  # never raises
        assert timers == []

    @pytest.mark.parametrize("submit_fails", [False, True], ids=["queued", "submit-failed"])
    def test_each_run_queues_the_job_and_sets_the_next_a_week_later(self, timers, store, monkeypatch, submit_fails):
        store.set_online_recheck_due(NOW - timedelta(minutes=1))
        submit = MagicMock(side_effect=RuntimeError("boom") if submit_fails else None, return_value="job-1")
        monkeypatch.setattr(triggers, "submit_online_recheck", submit)
        triggers._run_online_recheck()
        submit.assert_called_once_with()
        assert store.online_recheck_due() == NOW + WEEK
        assert [timer.interval for timer in timers] == [WEEK.total_seconds()]

    def test_the_next_due_time_is_stored_before_the_job_is_queued(self, timers, store, monkeypatch):
        store.set_online_recheck_due(NOW - timedelta(minutes=1))
        seen = []

        def submit():
            seen.append(store.online_recheck_due())
            # Another start arming it meanwhile gets next week's time, not this run's again.
            triggers.schedule_online_recheck()
            return "job-1"

        monkeypatch.setattr(triggers, "submit_online_recheck", submit)
        triggers._run_online_recheck()
        assert seen == [NOW + WEEK]
        assert [timer.interval for timer in timers] == [WEEK.total_seconds(), WEEK.total_seconds()]

    def test_a_next_due_time_that_cant_be_stored_queues_and_arms_nothing(self, timers, store, monkeypatch):
        # Armed again, the old (past) due time would fire at once, over and over.
        store.set_online_recheck_due(NOW - timedelta(minutes=1))
        submit = MagicMock(return_value=None)
        monkeypatch.setattr(triggers, "submit_online_recheck", submit)
        monkeypatch.setattr(store, "set_online_recheck_due", MagicMock(side_effect=OSError("disk full")))
        triggers._run_online_recheck()
        submit.assert_not_called()
        assert timers == []
