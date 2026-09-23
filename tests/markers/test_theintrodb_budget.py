"""TheIntroDB's daily budget: series it has no entries for stop being asked, and files its used-up budget refused are
checked again after the reset (production 2026-09-24: the 1,000 lookups ran out by 04:42 local time, 790 of them on
talk shows with no entries, and 198 files refused in 25 jobs were never asked again)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.job_log import BUDGET_RECHECK_LABEL
from media_preview_generator.markers.models import Candidate, FileIdentity, MarkerType, MediaIds, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.sources.theintrodb import series_key
from media_preview_generator.markers.store import MarkerStore
from media_preview_generator.servers.base import ServerType
from tests.markers.fakes import FakeClient, ready_publisher
from tests.markers.test_pipeline import (
    INTRO_ONLY,
    KEY_REJECTED,
    TIDB_BUDGET_EXHAUSTED,
    TIDB_INTRO,
    _clients,
    _ctx,
    _registry,
    _run,
)

NO_DATA = LookupResult("no_data")
SHOW = "Rick and Morty (2013) {tvdb-275274}"
PAUSE = pipeline.SERIES_NO_ENTRY_PAUSE


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return _Clock(datetime(2026, 9, 24, 4, 0, tzinfo=UTC))


@pytest.fixture
def store(tmp_path, clock):
    s = MarkerStore(str(tmp_path / "markers.db"), clock=clock)
    yield s
    s.close()


@pytest.fixture
def episodes(tmp_path):
    """Six episodes of one show; the show folder carries its tvdb id, as Sonarr names it."""
    folder = tmp_path / "media" / "tv" / SHOW / "Season 01"
    folder.mkdir(parents=True)
    paths = []
    for number in range(1, 7):
        f = folder / f"Rick and Morty (2013) - S01E{number:02d} - Episode {number}.mkv"
        f.write_bytes(b"x" * 100)
        paths.append(str(f))
    return paths


class TestSeriesKey:
    @pytest.mark.parametrize(
        ("ids", "expected"),
        [
            (MediaIds("episode", tmdb="1433", tvdb="275274", imdb="tt2861424", season=1, episode=1), "tmdb:1433"),
            (MediaIds("episode", tvdb="275274", imdb="tt2861424", season=1, episode=1), "tvdb:275274"),
            (MediaIds("episode", imdb="tt2861424", season=1, episode=1), "imdb:tt2861424"),
            (MediaIds("episode", imdb="tt123", season=1, episode=1), None),  # too short for TheIntroDB
            (MediaIds("episode", season=1, episode=1), None),
            (MediaIds("movie", tmdb="603", imdb="tt0133093"), None),
            (MediaIds("unknown", tvdb="275274"), None),
        ],
        ids=["tmdb-first", "tvdb", "imdb", "short-imdb", "no-id", "movie", "unknown-kind"],
    )
    def test_the_key_is_the_id_the_lookup_is_sent_with(self, ids, expected):
        assert series_key(ids) == expected


class TestStoreSeriesLookups:
    KEY = "tvdb:275274"

    def _miss(self, store, episode, show_folder=""):
        return store.record_series_lookup(
            Source.THEINTRODB, self.KEY, episode, found=False, misses=3, pause=PAUSE, show_folder=show_folder
        )

    def _paused(self, store, show_folder=""):
        return store.series_lookups_paused_until(Source.THEINTRODB, self.KEY, pause=PAUSE, show_folder=show_folder)

    def _store_answer(self, store, path, source=Source.THEINTRODB, found=(TIDB_INTRO,)):
        rec = store.upsert_file(FileIdentity(path, 100, 1), duration_ms=1_000_000, season_key=None, is_movie=False)
        store.replace_evidence(rec.id, source, list(found), version=1)

    def test_the_third_episode_with_no_entry_starts_a_pause(self, store, clock):
        assert self._miss(store, "S01E01") is None
        assert self._miss(store, "S01E02") is None
        assert self._paused(store) is None
        clock.now += timedelta(hours=1)
        assert self._miss(store, "S01E03") == clock.now + PAUSE
        assert self._paused(store) == clock.now + PAUSE

    def test_the_same_episode_answered_twice_counts_once(self, store):
        for _ in range(3):
            self._miss(store, "S01E01")
        assert self._paused(store) is None

    def test_one_found_episode_keeps_the_series_asked(self, store):
        store.record_series_lookup(Source.THEINTRODB, self.KEY, "S01E01", found=True, misses=3, pause=PAUSE)
        for episode in ("S01E02", "S01E03", "S01E04"):
            self._miss(store, episode)
        assert self._paused(store) is None

    def test_an_answer_found_during_a_pause_ends_it(self, store):
        for episode in ("S01E01", "S01E02", "S01E03"):
            self._miss(store, episode)
        store.record_series_lookup(Source.THEINTRODB, self.KEY, "S01E04", found=True, misses=3, pause=PAUSE)
        assert self._paused(store) is None

    def test_no_entry_during_a_pause_doesnt_extend_it(self, store, clock):
        """Review MED: a pause counted from the latest "no entry" never ended for an airing show."""
        for episode in ("S01E01", "S01E02", "S01E03"):
            self._miss(store, episode)
        started = clock.now
        clock.now += timedelta(days=5)
        assert self._miss(store, "S01E04") is None  # a forced re-detect's answer
        assert self._paused(store) == started + PAUSE

    def test_after_a_pause_it_takes_as_many_new_no_entries_to_pause_again(self, store, clock):
        for episode in ("S01E01", "S01E02", "S01E03"):
            self._miss(store, episode)
        clock.now += PAUSE
        assert self._paused(store) is None
        assert self._miss(store, "S01E04") is None
        assert self._miss(store, "S01E05") is None
        assert self._paused(store) is None
        assert self._miss(store, "S01E01") == clock.now + PAUSE  # E01 asked again counts as a new "no entry"

    def test_a_stored_answer_for_any_file_of_the_show_means_it_has_data(self, store, episodes):
        """Answers stored before series_lookups existed (the upgrade) count: the show is never paused."""
        show_folder = os.path.dirname(os.path.dirname(episodes[0]))
        self._store_answer(store, episodes[5])
        for episode in ("S01E01", "S01E02", "S01E03"):
            assert self._miss(store, episode, show_folder) is None
        assert self._paused(store, show_folder) is None

    def test_a_stored_answer_ends_a_pause_already_running(self, store, episodes):
        show_folder = os.path.dirname(os.path.dirname(episodes[0]))
        for episode in ("S01E01", "S01E02", "S01E03"):
            self._miss(store, episode, show_folder)
        assert self._paused(store, show_folder) is not None
        self._store_answer(store, episodes[5])
        assert self._paused(store, show_folder) is None

    @pytest.mark.parametrize(
        ("source", "found"),
        [
            (Source.THEINTRODB, ()),  # "looked it up, nothing there" is no answer
            (Source.INTRODB, (Candidate(MarkerType.INTRO, 1000, 2000, Source.INTRODB),)),  # another source's
        ],
        ids=["no-entry-row", "another-source"],
    )
    def test_only_the_same_sources_answers_with_a_marker_count(self, store, episodes, source, found):
        show_folder = os.path.dirname(os.path.dirname(episodes[0]))
        self._store_answer(store, episodes[5], source, found)
        for episode in ("S01E01", "S01E02", "S01E03"):
            self._miss(store, episode, show_folder)
        assert self._paused(store, show_folder) is not None

    def test_a_show_whose_name_extends_another_is_kept_apart(self, store, tmp_path):
        """ "/tv/Show" must not read "/tv/Show 2"'s answers (the range ends before "/tv/Show0")."""
        self._store_answer(store, str(tmp_path / "tv" / "Show 2" / "Season 01" / "Show 2 - S01E01.mkv"))
        show_folder = str(tmp_path / "tv" / "Show")
        for episode in ("S01E01", "S01E02", "S01E03"):
            self._miss(store, episode, show_folder)
        assert self._paused(store, show_folder) is not None

    def test_series_and_sources_are_kept_apart(self, store):
        for episode in ("S01E01", "S01E02", "S01E03"):
            store.record_series_lookup(Source.THEINTRODB, "tvdb:1", episode, found=False, misses=3, pause=PAUSE)
        assert store.series_lookups_paused_until(Source.THEINTRODB, "tvdb:2", pause=PAUSE) is None
        assert store.series_lookups_paused_until(Source.INTRODB, "tvdb:1", pause=PAUSE) is None


class TestSeriesWithNoEntriesIsPaused:
    def _run_all(self, store, paths, client):
        reg = _registry(paths[0], ServerType.PLEX)
        ctx = _ctx(store, reg, clients={"theintrodb": client}, settings_raw=INTRO_ONLY)
        for path in paths:
            _run(ctx, path, {"plex-1": ready_publisher()})
        return ctx

    def test_theintrodb_isnt_asked_after_three_episodes_had_no_entry(self, store, episodes):
        client = FakeClient(NO_DATA)
        self._run_all(store, episodes, client)

        assert [(c["ids"].season, c["ids"].episode) for c in client.calls] == [(1, 1), (1, 2), (1, 3)]
        for path in episodes[3:]:
            assert store.evidence_fetched_at(store.get_file(path).id, Source.THEINTRODB) is None

    def test_a_paused_series_isnt_counted_as_a_budget_skip(self, store, episodes):
        ctx = self._run_all(store, episodes, FakeClient(NO_DATA))

        assert pipeline.budget_exhausted_warnings(ctx) == []
        assert ctx.take_budget_rechecks() == ([], None)

    def test_an_answer_for_any_episode_keeps_the_series_asked(self, store, episodes):
        client = FakeClient(LookupResult("ok", (TIDB_INTRO,)))
        self._run_all(store, episodes[:1], client)
        client.result = NO_DATA
        self._run_all(store, episodes[1:], client)

        assert len(client.calls) == len(episodes)

    def test_refusals_and_errors_dont_count_as_no_entry(self, store, episodes):
        client = FakeClient(TIDB_BUDGET_EXHAUSTED)
        self._run_all(store, episodes[:3], client)
        client.result = LookupResult("unavailable", detail="TheIntroDB HTTP 503")
        self._run_all(store, episodes[3:5], client)
        client.result = NO_DATA
        self._run_all(store, episodes[5:], client)

        assert len(client.calls) == len(episodes)

    def test_the_series_is_asked_again_after_the_pause(self, store, clock, episodes):
        client = FakeClient(NO_DATA)
        self._run_all(store, episodes[:4], client)
        assert len(client.calls) == 3

        clock.now += PAUSE + timedelta(minutes=1)
        self._run_all(store, episodes[4:], client)

        # After the pause it takes 3 new "no entry" answers to pause again: E05 and E06 are both asked.
        assert [c["ids"].episode for c in client.calls] == [1, 2, 3, 5, 6]

    def test_an_airing_shows_older_episode_is_asked_again_when_due(self, store, clock, episodes):
        """Review MED: TheIntroDB lags on a weekly show's new episode, so each air day added a "no entry". Counted from
        the latest one, the pause never ended and an older episode due its 14-day re-ask was never asked."""
        client = FakeClient(NO_DATA)
        for path in episodes[:3]:  # E01-E03 air a week apart; E03's "no entry" starts the pause
            self._run_all(store, [path], client)
            clock.now += timedelta(days=7)
        clock.now -= timedelta(days=7)
        for path in episodes[3:5]:  # E04 and E05 air after the pause ended
            clock.now += timedelta(days=7, minutes=1)
            self._run_all(store, [path], client)
        clock.now += timedelta(hours=1)
        client.result = LookupResult("ok", (TIDB_INTRO,))  # the crowd has caught up with E01
        forced_reask = _registry(episodes[0], ServerType.PLEX)
        # E01's stored "no entry" is due again (NO_DATA_RETRY): the job's clock is past it.
        ctx = _ctx(store, forced_reask, clients={"theintrodb": client}, settings_raw=INTRO_ONLY, now=lambda: clock.now)

        _run(ctx, episodes[0], {"plex-1": ready_publisher()})

        assert [c["ids"].episode for c in client.calls] == [1, 2, 3, 4, 5, 1]
        assert store.series_lookups_paused_until(Source.THEINTRODB, "tvdb:275274", pause=PAUSE) is None

    def test_a_forced_redetect_asks_a_paused_series(self, store, episodes):
        client = FakeClient(NO_DATA)
        self._run_all(store, episodes[:3], client)
        reg = _registry(episodes[0], ServerType.PLEX)
        forced = _ctx(store, reg, clients={"theintrodb": client}, settings_raw=INTRO_ONLY, force=True)

        _run(forced, episodes[3], {"plex-1": ready_publisher()})

        assert [c["ids"].episode for c in client.calls] == [1, 2, 3, 4]

    def test_the_pause_is_logged_once_at_info(self, store, episodes, loguru_caplog):
        self._run_all(store, episodes, FakeClient(NO_DATA))

        lines = [r for r in loguru_caplog.records if "has no entry for" in r.getMessage()]
        assert len(lines) == 1
        assert lines[0].levelname == "INFO"
        message = lines[0].getMessage()
        assert "TheIntroDB has no entry for 3 or more episodes of Rick and Morty (2013) (tvdb:275274)" in message
        assert "aren't looked up there until 2026-10-01 04:00 UTC" in message

    def test_each_skipped_episode_says_why_in_its_sources_line(self, store, episodes, loguru_caplog):
        self._run_all(store, episodes, FakeClient(NO_DATA))

        sources = {
            r.getMessage().split(":", 1)[0]: r.getMessage().split("\n  sources: ", 1)[1].split(" · ")[0]
            for r in loguru_caplog.records
            if "\n  sources: " in r.getMessage()
        }
        assert sources == {
            **{f"Rick and Morty (2013) S01E0{n}": "TheIntroDB no entry" for n in (1, 2, 3)},
            **{
                f"Rick and Morty (2013) S01E0{n}": (
                    "TheIntroDB skipped (no data for this show; asked again after 2026-10-01)"
                )
                for n in (4, 5, 6)
            },
        }


class TestBudgetRecheckJobLog:
    """A TheIntroDB recheck logs like a Season job: one line per season for episodes whose decisions didn't change,
    its own lines for a file that changed and for a movie (which has no season)."""

    def test_unchanged_episodes_share_one_recheck_line(self, store, episodes, loguru_caplog):
        reg = _registry(episodes[0], ServerType.PLEX)
        first = _ctx(store, reg, clients=_clients(theintrodb=TIDB_BUDGET_EXHAUSTED), settings_raw=INTRO_ONLY)
        for path in episodes[:3]:
            _run(first, path, {"plex-1": ready_publisher()})
        loguru_caplog.clear()

        recheck = _ctx(store, reg, clients=_clients(theintrodb=TIDB_BUDGET_EXHAUSTED), settings_raw=INTRO_ONLY)
        recheck.season_recheck = True
        recheck.recheck_label = BUDGET_RECHECK_LABEL
        for path in episodes[:3]:
            _run(recheck, path, {"plex-1": ready_publisher()})

        assert not [r for r in loguru_caplog.records if "\n  sources: " in r.getMessage()]
        assert recheck.summary_lines({FileOutcome.NO_MARKERS.value: 3}) == [
            "TheIntroDB recheck, Rick and Morty (2013) S01 (3 episodes): no change",
            "Done: 3 files · 0 sent to PLEX-1 · 0 need review · 3 nothing found · TheIntroDB skipped for 3 files",
        ]

    def test_a_movie_logs_its_own_lines(self, store, tmp_path, loguru_caplog):
        folder = tmp_path / "media" / "movies" / "Heat (1995) {tmdb-949}"
        folder.mkdir(parents=True)
        movie = str(folder / "Heat (1995).mkv")
        with open(movie, "wb") as f:
            f.write(b"x" * 100)
        reg = _registry(movie, ServerType.PLEX)
        raw = {"sources": [{"id": "theintrodb", "enabled": True}], "detect": {"credits": True}}
        recheck = _ctx(store, reg, clients=_clients(theintrodb=TIDB_BUDGET_EXHAUSTED), settings_raw=raw)
        recheck.season_recheck = True
        recheck.recheck_label = BUDGET_RECHECK_LABEL
        _run(recheck, movie, {"plex-1": ready_publisher()})

        lines = [r.getMessage() for r in loguru_caplog.records if "\n  sources: " in r.getMessage()]
        assert [line.split(":", 1)[0] for line in lines] == ["Heat (1995)"]
        assert recheck.summary_lines({FileOutcome.NO_MARKERS.value: 1})[:-1] == []


class TestBudgetRechecks:
    """Files TheIntroDB's used-up budget refused are handed to job_runner to be checked again after the reset, only
    while a type is still undecided."""

    def test_an_undecided_file_is_noted_with_when_it_was_refused(self, store, episodes):
        reg = _registry(episodes[0], ServerType.PLEX)
        ctx = _ctx(store, reg, clients=_clients(theintrodb=TIDB_BUDGET_EXHAUSTED), settings_raw=INTRO_ONLY)
        for path in episodes[:2]:
            _run(ctx, path, {"plex-1": ready_publisher()})

        assert ctx.take_budget_rechecks() == (sorted(episodes[:2]), ctx.now())
        assert ctx.take_budget_rechecks() == ([], None), "taking them forgets them"

    def test_a_file_other_sources_decided_is_not_noted(self, store, episodes):
        reg = _registry(episodes[0], ServerType.PLEX)
        raw = {
            "sources": [
                {"id": "theintrodb", "enabled": True},
                {"id": "introdb", "enabled": True},
                {"id": "skipdb", "enabled": True},
            ],
            "detect": {"intro": True, "credits": False},
        }
        clients = _clients(
            theintrodb=TIDB_BUDGET_EXHAUSTED,
            introdb=LookupResult("ok", (Candidate(MarkerType.INTRO, 128_000, 157_000, Source.INTRODB),)),
            skipdb=LookupResult("ok", (Candidate(MarkerType.INTRO, 129_000, 157_800, Source.SKIPDB),)),
        )
        ctx = _ctx(store, reg, clients=clients, settings_raw=raw)
        _run(ctx, episodes[0], {"plex-1": ready_publisher()})

        assert ctx.take_budget_rechecks() == ([], None)

    @pytest.mark.parametrize(
        "answer",
        [KEY_REJECTED, LookupResult("unavailable", detail="TheIntroDB HTTP 503"), NO_DATA],
        ids=["key-refused", "http-error", "no-entry"],
    )
    def test_only_a_used_up_budget_is_noted(self, store, episodes, answer):
        reg = _registry(episodes[0], ServerType.PLEX)
        ctx = _ctx(store, reg, clients=_clients(theintrodb=answer), settings_raw=INTRO_ONLY)
        _run(ctx, episodes[0], {"plex-1": ready_publisher()})

        assert ctx.take_budget_rechecks() == ([], None)

    def test_a_file_refused_only_by_another_sources_budget_is_not_noted(self, store, episodes):
        reg = _registry(episodes[0], ServerType.PLEX)
        raw = {"sources": [{"id": "skipdb", "enabled": True}], "detect": {"intro": True, "credits": False}}
        clients = _clients(skipdb=LookupResult("unavailable", detail="SkipDB budget_exhausted"))
        ctx = _ctx(store, reg, clients=clients, settings_raw=raw)
        _run(ctx, episodes[0], {"plex-1": ready_publisher()})

        assert ctx.take_budget_rechecks() == ([], None)
