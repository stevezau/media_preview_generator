"""Intro & Credits job logs: per file, what each source answered, what was decided and what was sent where; one line
per season for a Season job; a totals line at the end."""

from __future__ import annotations

import dataclasses
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from loguru import logger

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.job_log import (
    SeasonEpisode,
    clock,
    display_name,
    season_line,
    totals_line,
)
from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.outcomes import NOT_IN_LIBRARY, FileOutcome, ServerStatus
from media_preview_generator.markers.pipeline import DetectorUnavailableError, LocalDetectorSpec
from media_preview_generator.markers.probe import Chapter
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import DUR, NO_DATA, TIDB_CREDITS, _clients, _ctx, _probe, _registry, _run

# test_pipeline's fixtures, shared by name (an import of them reads as unused to the linter).
media = test_pipeline.media
store = test_pipeline.store

T = MarkerType
TEXT_CREDITS = Candidate(T.CREDITS, 1_291_000, None, Source.CREDITS_TEXT)
PLEX_CREDITS = {"type": "credits", "start_ms": 1_290_000, "end_ms": DUR, "final": True}
INTRO_CHAPTERS = (
    Chapter(0, 126_771, "Chapter 1"),
    Chapter(126_771, 157_068, "Intro"),
    Chapter(157_068, None, "Chapter 2"),
)
BUDGET = LookupResult("unavailable", detail="TheIntroDB budget_exhausted")
REVIEW_AT_HIGH = "needs review (sources don't agree yet; at Publish when: High one source isn't enough)"
SETTINGS = {
    "detect": {"intro": True, "credits": True, "recap": False},
    "publish_when": "high",
    "sources": [
        {"id": "chapters", "enabled": True},
        {"id": "theintrodb", "enabled": True},
        {"id": "introdb", "enabled": False},
        {"id": "skipdb", "enabled": False},
        {"id": "season_audio", "enabled": False},
        {"id": "credits_text", "enabled": True},
        {"id": "server_markers", "enabled": True},
    ],
}
EPISODE = "Rick and Morty (2013) S01E01"


@pytest.fixture
def job_log():
    """The pipeline's per-file job log entries (a file's two lines are one entry)."""
    entries: list[str] = []
    handler = logger.add(
        lambda message: entries.append(message.record["message"]),
        level="INFO",
        format="{message}",
        filter=lambda record: "\n  sources: " in record["message"],
    )
    yield entries
    logger.remove(handler)


@pytest.fixture
def movie(tmp_path):
    folder = tmp_path / "media" / "movies" / "Heat (1995) {tmdb-949}"
    folder.mkdir(parents=True)
    f = folder / "Heat (1995).mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


def _plex(path, *, keeps_own_credits=False, indexed=True):
    """One Plex server named "Plex" holding the file."""
    reg = _registry(path, ServerType.PLEX)
    reg.configs_by_id["plex-1"] = dataclasses.replace(reg.configs_by_id["plex-1"], name="Plex")
    server = reg.get("plex-1")
    if keeps_own_credits:
        reg.configs_by_id["plex-1"].markers["plex"]["on_plex_redetect"] = "keep_plex"
        server.get_markers.return_value = [PLEX_CREDITS]
        server.get_part_durations.return_value = [DUR]  # one version, one part
    if not indexed:
        server.resolve_remote_path_to_item_id.return_value = None
    return reg


def _credit_text(found=(TEXT_CREDITS,), *, on_worker=False):
    return LocalDetectorSpec(
        Source.CREDITS_TEXT,
        frozenset({T.CREDITS}),
        MagicMock(return_value=list(found)),
        needs_worker=None if on_worker else (lambda rec, ctx: False),
    )


def _job(store, path, reg, *, theintrodb=NO_DATA, found=(TEXT_CREDITS,), on_worker=False, now=None):
    detectors = (_credit_text(found, on_worker=on_worker),)
    return _ctx(
        store, reg, settings_raw=SETTINGS, clients=_clients(theintrodb=theintrodb), detectors=detectors, now=now
    )


def _lines(first: str, sources: str) -> str:
    return f"{first}\n  sources: {sources}"


class TestFileLines:
    """One cell per kind of result; every line is asserted as the job log shows it."""

    def test_credit_text_alone_needs_review_and_nothing_is_sent(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert job_log == [
            _lines(
                f"{EPISODE}: nothing sent to Plex · intro not found · credits 21:31–22:01 found by credit text → "
                f"{REVIEW_AT_HIGH}",
                "chapters none · TheIntroDB no entry · credit text credits from 21:31 · Plex's own none",
            )
        ]
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        # The Files panel's reason names the proposal and why it needs review.
        assert (
            out.message == "intro: none; credits needs review (sources don't agree yet): 21:31–22:01 from credits_text"
        )

    def test_two_sources_that_agree_publish_and_both_are_named(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=LookupResult("ok", (TIDB_CREDITS,)))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert job_log == [
            _lines(
                f"{EPISODE}: sent credits to Plex · intro not found · credits 21:36–22:00 (TheIntroDB + credit text agree)",
                "chapters none · TheIntroDB credits 21:36–22:00 · credit text credits from 21:31 · Plex's own none",
            )
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_type_every_server_keeps_its_own_of_is_named_and_not_read(self, store, media, job_log):
        ctx = _job(store, media, _plex(media, keeps_own_credits=True))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(INTRO_CHAPTERS))

        assert job_log == [
            _lines(
                f'{EPISODE}: sent intro to Plex; Plex keeps its own credits ("Keep Plex\'s") · intro 2:06–2:37 (from '
                "chapters) · credits: kept Plex's own marker",
                "chapters intro 2:06–2:37 · TheIntroDB no entry · credit text not read (every server keeps its own "
                "credits) · Plex's own credits from 21:30",
            )
        ]
        ctx.local_detectors[0].detect.assert_not_called()
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_decided_type_the_server_keeps_its_own_of_says_ours_wasnt_written(self, store, media, job_log):
        plex = ready_publisher()

        def keeps_plexs_credits(item_id, markers, **kwargs):
            plex.last_kept_types, plex.last_write_changed = frozenset({T.CREDITS}), True
            return [m for m in plex.project(markers) if m.type is not T.CREDITS]

        plex.write.side_effect = keeps_plexs_credits
        ctx = _job(store, media, _plex(media, keeps_own_credits=True))
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(test_pipeline.CHAPTERS_BOTH))

        assert job_log == [
            _lines(
                f'{EPISODE}: sent intro to Plex; Plex keeps its own credits ("Keep Plex\'s"), our credits not written · '
                "intro 2:06–2:37 (from chapters) · credits 21:35–22:01 (from chapters)",
                "chapters intro 2:06–2:37, credits from 21:35 · TheIntroDB no entry · credit text not needed (already "
                "decided) · Plex's own credits from 21:30",
            )
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_server_that_hasnt_indexed_the_file_is_waited_for_even_with_a_type_in_review(self, store, media, job_log):
        ctx = _job(store, media, _plex(media, indexed=False), theintrodb=LookupResult("ok", (TIDB_CREDITS,)), found=())
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(INTRO_CHAPTERS))

        assert job_log == [
            _lines(
                f"{EPISODE}: waiting for Plex to add the file to its library · intro 2:06–2:37 (from chapters) · "
                f"credits 21:36–22:00 found by TheIntroDB → {REVIEW_AT_HIGH}",
                "chapters intro 2:06–2:37 · TheIntroDB credits 21:36–22:00 · credit text none found · Plex's own not "
                "read",
            )
        ]
        row = out.publisher_rows[0]
        assert (row["status"], row["reason_code"]) == (ServerStatus.WAITING.value, NOT_IN_LIBRARY)
        # The job retries the file, so it counts as waiting, not as needing review.
        assert out.outcome_key == FileOutcome.WAITING.value

    def test_an_online_source_out_of_its_daily_budget_is_named_as_skipped_and_counted(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=BUDGET)
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert job_log == [
            _lines(
                f"{EPISODE}: nothing sent to Plex · intro not found · credits 21:31–22:01 found by credit text → "
                f"{REVIEW_AT_HIGH}",
                "chapters none · TheIntroDB skipped (daily limit reached, resets 00:00 UTC) · credit text credits from "
                "21:31 · Plex's own none",
            )
        ]
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert ctx.summary_lines({FileOutcome.NEEDS_REVIEW.value: 1}) == [
            "Done: 1 file · 0 sent to Plex · 1 needs review · 0 nothing found · TheIntroDB skipped for 1 file"
        ]

    def test_a_written_type_counts_the_file_as_written_with_the_other_type_in_review(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(INTRO_CHAPTERS))

        assert job_log == [
            _lines(
                f"{EPISODE}: sent intro to Plex · intro 2:06–2:37 (from chapters) · credits 21:31–22:01 found by "
                f"credit text → {REVIEW_AT_HIGH}",
                "chapters intro 2:06–2:37 · TheIntroDB no entry · credit text credits from 21:31 · Plex's own none",
            )
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert "credits needs review (sources don't agree yet): 21:31–22:01 from credits_text" in out.message

    def test_nothing_found_and_a_second_run_names_the_saved_answers(self, store, media, job_log):
        reg = _plex(media)
        for _ in range(2):
            ctx = _job(store, media, reg, found=(), now=lambda: datetime.now(UTC))
            out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())
            assert out.outcome_key == FileOutcome.NO_MARKERS.value

        first = f"{EPISODE}: nothing sent to Plex · intro not found · credits not found"
        assert job_log == [
            _lines(first, "chapters none · TheIntroDB no entry · credit text none found · Plex's own none"),
            _lines(
                first,
                "chapters none (saved earlier) · TheIntroDB no entry (saved earlier) · credit text none found (saved "
                "earlier) · Plex's own none (saved earlier)",
            ),
        ]

    def test_a_movie_is_named_by_its_title(self, store, movie, job_log):
        chapters = (Chapter(0, 1_295_324, "Movie"), Chapter(1_295_324, None, "Credits"))
        ctx = _job(store, movie, _plex(movie))
        out, _ = _run(ctx, movie, {"plex-1": ready_publisher()}, probe=_probe(chapters))

        assert job_log == [
            _lines(
                "Heat (1995): sent credits to Plex · credits 21:35–22:01 (from chapters)",
                "chapters credits from 21:35 · TheIntroDB not asked (no server confirmed whether it's a movie or an "
                "episode) · credit text not needed (already decided) · Plex's own none",
            )
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_source_without_an_answer_this_time_says_why(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=LookupResult("unavailable", detail="TheIntroDB HTTP 503"))
        ctx.local_detectors[0].detect.side_effect = DetectorUnavailableError("the decode timed out")
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert job_log == [
            _lines(
                f"{EPISODE}: nothing sent to Plex · intro not found · credits not found",
                "chapters none · TheIntroDB unavailable (HTTP 503) · credit text no answer this time (the decode "
                "timed out) · Plex's own none",
            )
        ]

    def test_a_file_the_worker_finishes_names_what_its_checking_stage_asked(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), on_worker=True)
        publishers = {"plex-1": ready_publisher()}
        handed_on, _ = _run(ctx, media, publishers, probe=_probe())
        assert handed_on is None and job_log == []

        _run(ctx, media, publishers, probe=_probe(), stage="process")

        assert job_log == [
            _lines(
                f"{EPISODE}: nothing sent to Plex · intro not found · credits 21:31–22:01 found by credit text → "
                f"{REVIEW_AT_HIGH}",
                "chapters none · TheIntroDB no entry · credit text credits from 21:31 · Plex's own none",
            )
        ]


class TestSeasonJob:
    def test_unchanged_episodes_share_one_line_and_a_changed_one_gets_its_own(self, store, tmp_path, job_log):
        folder = tmp_path / "media" / "tv" / "Rick and Morty (2013) {tvdb-275274}" / "Season 01"
        folder.mkdir(parents=True)
        paths = []
        for episode in (1, 2, 3, 4):
            f = folder / f"Rick and Morty (2013) - S01E{episode:02d} - Episode.mkv"
            f.write_bytes(b"x" * 100)
            paths.append(str(f))
        reg = _plex(paths[0])

        def run(ctx, path, chapters=()):
            hints = {"plex-1": f"item-{paths.index(path)}"}  # each episode its own Plex item
            return _run(ctx, path, {"plex-1": ready_publisher()}, probe=_probe(chapters), hints=hints)[0]

        first = _job(store, paths[0], reg)
        for path in paths:
            assert run(first, path).outcome_key == FileOutcome.NEEDS_REVIEW.value
        os.utime(paths[1], ns=(5, 5))  # E02 was replaced by a file with an intro chapter
        job_log.clear()

        season = _job(store, paths[0], reg)
        season.season_recheck = True
        outcomes = [run(season, path, INTRO_CHAPTERS if path == paths[1] else ()).outcome_key for path in paths]

        # The first job's 4 "no entry" answers paused TheIntroDB for the show, so the replaced E02 isn't asked again.
        paused_until = store.series_lookups_paused_until(
            Source.THEINTRODB,
            "tvdb:275274",
            misses=pipeline.SERIES_NO_ENTRY_MISSES,
            pause=pipeline.SERIES_NO_ENTRY_PAUSE,
        )
        assert job_log == [
            _lines(
                "Rick and Morty (2013) S01E02: sent intro to Plex · intro 2:06–2:37 (from chapters) · credits "
                f"21:31–22:01 found by credit text → {REVIEW_AT_HIGH}",
                "chapters intro 2:06–2:37 · TheIntroDB skipped (no data for this show; asked again after "
                f"{paused_until:%Y-%m-%d}) · credit text credits from 21:31 · Plex's own none",
            )
        ]
        counts = {key: outcomes.count(key) for key in set(outcomes)}
        assert counts == {FileOutcome.NEEDS_REVIEW.value: 3, FileOutcome.PUBLISHED.value: 1}
        assert season.summary_lines(counts) == [
            "Season re-check, Rick and Morty (2013) S01 (4 episodes): E02 changed (logged above); no change for "
            "E01/E03/E04, still need review (credits from credit text only)",
            "Done: 4 files · 1 sent to Plex · 3 need review · 0 nothing found",
        ]

    @pytest.mark.parametrize(
        ("episodes", "expected"),
        [
            (
                [SeasonEpisode("E03", False, "credits from credit text only"), SeasonEpisode("E01", False, "")],
                "Season re-check, Show S01 (2 episodes): no change, E03 still needs review (credits from credit text "
                "only)",
            ),
            (
                [
                    SeasonEpisode("E01", False, "credits from credit text only"),
                    SeasonEpisode("E02", False, "intro from TheIntroDB only"),
                ],
                "Season re-check, Show S01 (2 episodes): no change, E01/E02 still need review",
            ),
            ([SeasonEpisode("E01", False, "")], "Season re-check, Show S01 (1 episode): no change"),
            ([SeasonEpisode("E01", True, "")], "Season re-check, Show S01 (1 episode): E01 changed (logged above)"),
            (
                [SeasonEpisode("E02", True, ""), SeasonEpisode("E01", False, ""), SeasonEpisode("E03", False, "x")],
                "Season re-check, Show S01 (3 episodes): E02 changed (logged above); no change for E01/E03, E03 still "
                "needs review (x)",
            ),
        ],
        ids=["one-in-review", "different-reasons", "one-episode", "only-changed", "changed-and-some-review"],
    )
    def test_season_line_wording(self, episodes, expected):
        assert season_line("Show S01", episodes) == expected


class TestTotalsLine:
    def test_every_outcome_a_file_had_is_counted_and_the_skipped_sources_last(self):
        outcome = {
            FileOutcome.PUBLISHED.value: 2,
            FileOutcome.UP_TO_DATE.value: 1,
            FileOutcome.WAITING.value: 1,
            FileOutcome.NEEDS_REVIEW.value: 2,
            FileOutcome.NO_MARKERS.value: 1,
            FileOutcome.FAILED.value: 1,
        }
        line = totals_line(outcome, {"Plex": 2, "Jellyfin": 1}, {"TheIntroDB": 3, "SkipDB": 0})
        assert line == (
            "Done: 8 files · 1 sent to Jellyfin · 2 sent to Plex · 2 need review · 1 nothing found · 1 already up to "
            "date · 1 waiting for a server · 1 failed · TheIntroDB skipped for 3 files"
        )

    def test_a_job_with_no_files_says_so(self):
        assert totals_line({}, {}, {}) == "Done: 0 files · 0 need review · 0 nothing found"


class TestNames:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/tv/Brave New World {tvdb-1}/Season 01/Brave New World - S01E05 - Swallow.mkv", "Brave New World S01E05"),
            ("/tv/The Fall (2013) [imdbid-tt2294189]/Season 3/The Fall - s03e05.mkv", "The Fall (2013) S03E05"),
            ("/tv/Flat Show/Flat Show S02E10.mkv", "Flat Show S02E10"),
            ("/movies/Heat (1995) {tmdb-949}/Heat (1995) {edition-Director's Cut}.mkv", "Heat (1995)"),
        ],
        ids=["season-folder", "imdb-tag", "flat-folder", "movie"],
    )
    def test_display_name(self, path, expected):
        assert display_name(path) == expected

    @pytest.mark.parametrize(
        ("ms", "expected"), [(0, "0:00"), (59_999, "0:59"), (2_856_000, "47:36"), (3_725_000, "1:02:05")]
    )
    def test_clock_uses_hours_from_an_hour_on(self, ms, expected):
        assert clock(ms) == expected
