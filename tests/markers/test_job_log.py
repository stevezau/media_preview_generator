"""Intro & Credits job logs: per file, one block of lines (each its own record) saying what each source answered, what
was decided, what was sent where and how long it took; one line per file whose answer didn't change; one line per
season for a Season job; a start line and a totals line."""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from media_preview_generator.markers import job_runner, pipeline, titles
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision
from media_preview_generator.markers.job_log import (
    ALREADY_DECIDED,
    RunNotes,
    SeasonEpisode,
    ServerResult,
    clock,
    compact_line,
    decide_again_line,
    display_name,
    done_line,
    duration,
    file_title,
    online_recheck_line,
    pickup_line,
    read_phrase,
    season_line,
    sent_line,
    source_lines,
    start_line,
    totals_line,
    type_phrase,
    write_lines,
)
from media_preview_generator.markers.models import (
    STALE_SERVER_MARKERS_DETAIL,
    Candidate,
    FileIdentity,
    Marker,
    MarkerType,
    Source,
)
from media_preview_generator.markers.outcomes import NOT_IN_LIBRARY, PLEX_PASS_UNKNOWN, FileOutcome, ServerStatus
from media_preview_generator.markers.pipeline import DetectorUnavailableError, LocalDetectorSpec
from media_preview_generator.markers.probe import Chapter, ProbeError
from media_preview_generator.markers.publishers.base import PublishError
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.markers.store import EvidenceRow
from media_preview_generator.processing.types import ProcessableItem
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import FakeClient, ready_publisher
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
TIDB = LookupResult("ok", (TIDB_CREDITS,))
# TheIntroDB never decides alone (rule 6): its lone answer is the review case these lines are checked with.
ONLINE_ONLY = "needs review (only TheIntroDB has the credits; an online answer needs a check against the file)"
FILES_PANEL_ONLINE_ONLY = (
    "credits needs review (only TheIntroDB has the credits; an online answer needs a check against the file): "
    "21:36–22:00 from theintrodb"
)
SETTINGS = {
    "detect": {"intro": True, "credits": True, "recap": False},
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
HEAD = f"{EPISODE}: checking intro and credits"
DONE = f"{EPISODE}: done in 0 s, no worker needed"


class FakeClock:
    """The job's monotonic clock, moved on only by the test's own steps."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def job_log():
    """The job log's records the pipeline writes (``job_log.write_lines``), as ``(level, message)``."""
    records: list[tuple[str, str]] = []
    handler = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="INFO",
        format="{message}",
        filter=lambda record: record["module"] == "job_log" and record["function"] == "write_lines",
    )
    yield records
    logger.remove(handler)


def _messages(records: list[tuple[str, str]]) -> list[str]:
    return [message for _, message in records]


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
    ctx = _ctx(store, reg, settings_raw=SETTINGS, clients=_clients(theintrodb=theintrodb), detectors=detectors, now=now)
    ctx.monotonic = FakeClock()  # every step takes no time unless a test says otherwise
    return ctx


# ---------------------------------------------------------------------------------------------------------------------
# The two layouts the owner approved, built from fixtures and asserted line for line.
# ---------------------------------------------------------------------------------------------------------------------

MOVIE_SETTINGS = {
    "detect": {"intro": True, "credits": True, "recap": False},
    "sources": [
        {"id": "chapters", "enabled": True},
        {"id": "theintrodb", "enabled": False},
        {"id": "introdb", "enabled": True},
        {"id": "skipdb", "enabled": True},
        {"id": "season_audio", "enabled": True},
        {"id": "credits_text", "enabled": True},
        {"id": "server_markers", "enabled": True},
    ],
}
EPISODE_SETTINGS = {
    **MOVIE_SETTINGS,
    "sources": [
        {"id": "chapters", "enabled": True},
        {"id": "theintrodb", "enabled": False},
        {"id": "introdb", "enabled": True},
        {"id": "skipdb", "enabled": False},
        {"id": "season_audio", "enabled": True},
        {"id": "credits_text", "enabled": True},
        {"id": "server_markers", "enabled": True},
    ],
}
MOVIE_DURATION = 7_419_000  # 2:03:39
ACCUSED_DURATION = 2_590_000  # 43:10
MOVIE_IDS = {
    "kind": "movie",
    "tmdb": "1215020",
    "imdb": "tt31806037",
    "tvdb": None,
    "season": None,
    "episode": None,
    "title": "32 Frames: A 9/11 Mystery",
    "year": "2026",
}


def _accused(tmp_path, episode=5):
    folder = tmp_path / "media" / "tv" / "Accused" / "Season 04"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"Accused - S04E{episode:02d} - Episode.mkv"
    path.write_bytes(b"x" * 100)
    return str(path)


def _accused_job(store, reg, clock):
    season_audio = LocalDetectorSpec(
        Source.SEASON_AUDIO,
        frozenset({T.INTRO}),
        # 8 of the episode's 9 partners share its theme: 9 of the 10 episodes compared have it.
        MagicMock(return_value=[Candidate(T.INTRO, 41_000, 72_000, Source.SEASON_AUDIO, origin="8/9")]),
        needs_worker=lambda rec, ctx: False,
    )

    def read_credit_text(*args, **kwargs):
        clock.advance(13)
        return [Candidate(T.CREDITS, 2_508_000, None, Source.CREDITS_TEXT)]

    credit_text = LocalDetectorSpec(
        Source.CREDITS_TEXT, frozenset({T.CREDITS}), MagicMock(side_effect=read_credit_text)
    )
    introdb = FakeClient(LookupResult("ok", (Candidate(T.INTRO, 41_000, 72_000, Source.INTRODB),)))
    ctx = _ctx(
        store,
        reg,
        settings_raw=EPISODE_SETTINGS,
        clients={"introdb": introdb, "skipdb": FakeClient(NO_DATA), "theintrodb": FakeClient(NO_DATA)},
        detectors=(season_audio, credit_text),
    )
    ctx.monotonic = clock
    return ctx


class TestApprovedLayouts:
    def test_a_film_the_checking_thread_finished(self, tmp_path, store, job_log):
        folder = tmp_path / "media" / "movies" / "32 Frames A 9 11 Mystery (2026)"
        folder.mkdir(parents=True)
        path = folder / "32 Frames A 9 11 Mystery (2026) - [AMZN][WEBDL-1080p][EAC3 5.1][h264]-cinepth.mkv"
        path.write_bytes(b"x" * 100)
        movie_path = str(path)
        reg = _plex(movie_path)
        reg.get("plex-1").get_external_ids.return_value = dict(MOVIE_IDS)
        clock = FakeClock()
        skipdb = FakeClient(NO_DATA)
        ctx = _ctx(
            store,
            reg,
            settings_raw=MOVIE_SETTINGS,
            clients={"introdb": FakeClient(NO_DATA), "skipdb": skipdb, "theintrodb": FakeClient(NO_DATA)},
            detectors=(_credit_text(),),
        )
        ctx.monotonic = clock
        publisher = ready_publisher()
        publisher.write.side_effect = lambda *a, **k: (clock.advance(0.5), publisher.succeed(*a, **k))[1]
        chapters = (Chapter(0, 7_211_000, "Movie"), Chapter(7_211_000, MOVIE_DURATION, "Credits"))

        out, _ = _run(ctx, movie_path, {"plex-1": publisher}, probe=_probe(chapters, duration=MOVIE_DURATION))

        job = SimpleNamespace(parent_schedule_id="")
        cfg = {"source": "radarr", "follows_job_id": "c7ca6327-1111-2222-3333-444455556666"}
        first = start_line("6742472e-b9a6-470d-ad4d-64b9442bac6e", 1, job_runner.trigger_words(job, cfg))
        log = [first, *_messages(job_log), *ctx.summary_lines({out.outcome_key: 1})]
        assert log == [
            "Intro & Credits job 6742472e started: 1 file, follow-up to preview job c7ca6327 (Radarr import)",
            "32 Frames: A 9/11 Mystery (2026): checking credits (films get credits only)",
            '  Chapters: "Credits" chapter at 2:00:11–2:03:39',
            "  SkipDB: no entry",
            "  Plex's own markers: none",
            "  Credit text: not read (a chapter named Credits is used as-is)",
            '  Decided: credits 2:00:11–2:03:39, from the "Credits" chapter',
            "  Sent to Plex: credits 2:00:11–2:03:39",
            "32 Frames: A 9/11 Mystery (2026): done in 0.5 s, no worker needed",
            "Done: 1 file · 1 sent to Plex · 0 need review · 0 nothing found",
        ]
        assert {level for level, _ in job_log} == {"INFO"}
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert len(skipdb.calls) == 1
        ctx.local_detectors[0].detect.assert_not_called()

    def test_a_tv_episode_a_gpu_worker_scanned(self, tmp_path, store, job_log):
        episode = _accused(tmp_path)
        clock = FakeClock()
        ctx = _accused_job(store, _plex(episode), clock)
        publisher = ready_publisher()
        publisher.write.side_effect = lambda *a, **k: (clock.advance(12), publisher.succeed(*a, **k))[1]
        probe = _probe(duration=ACCUSED_DURATION)

        handed_on, _ = _run(ctx, episode, {"plex-1": publisher}, probe=probe)
        assert handed_on is None and job_log == []
        worker = "GPU Worker 2 (Intel UHD 770)"
        pipeline.log_pickup(ProcessableItem(canonical_path=episode, server_id="plex-1"), worker, ctx=ctx)
        out, _ = _run(
            ctx, episode, {"plex-1": publisher}, probe=probe, stage="process", gpu="intel", worker_name=worker,
            gpu_worker=True,
        )  # fmt: skip

        assert _messages(job_log) == [
            "GPU Worker 2 (Intel UHD 770) picked up Accused S04E05: checking intro and credits",
            "  Chapters: none",
            "  IntroDB: intro 0:41–1:12",
            "  Season audio: intro 0:41–1:12 (same theme found in 9 of 10 episodes)",
            "  Credit text: credits start at 41:48 (read on the GPU in 13 s)",
            "  Plex's own markers: none",
            "  Decided: intro 0:41–1:12 (IntroDB and season audio agree) · credits 41:48–43:10 (credit text)",
            "  Sent to Plex: intro 0:41–1:12 · credits 41:48–43:10",
            "Accused S04E05: done in 25 s on GPU Worker 2",
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value


class TestFileBlocks:
    """One cell per kind of result, run through the pipeline; every line is asserted as the job log shows it."""

    def test_an_online_answer_alone_needs_review_and_nothing_is_sent(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=TIDB, found=())
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log) == [
            HEAD,
            "  Chapters: none",
            "  TheIntroDB: credits 21:36–22:00",
            "  Credit text: none found (read on the CPU in 0 s)",
            "  Plex's own markers: none",
            f"  Decided: intro nothing found · credits 21:36–22:00 from TheIntroDB {ONLINE_ONLY}",
            "  Sent to Plex: nothing to send",
            DONE,
        ]
        # Needs review keeps the block at INFO.
        assert {level for level, _ in job_log} == {"INFO"}
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        # The Files panel's reason names the proposal and why it needs review.
        assert out.message == f"intro: none; {FILES_PANEL_ONLINE_ONLY}"

    def test_credit_text_alone_is_sent(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log) == [
            HEAD,
            "  Chapters: none",
            "  TheIntroDB: no entry",
            "  Credit text: credits start at 21:31 (read on the CPU in 0 s)",
            "  Plex's own markers: none",
            "  Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            "  Sent to Plex: credits 21:31–22:01",
            DONE,
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_two_sources_that_agree_publish_and_both_are_named(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=TIDB)
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log)[5:7] == [
            "  Decided: intro nothing found · credits 21:36–22:00 (TheIntroDB and credit text agree)",
            "  Sent to Plex: credits 21:36–22:00",
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_type_every_server_keeps_its_own_of_is_named_and_not_read(self, store, media, job_log):
        ctx = _job(store, media, _plex(media, keeps_own_credits=True))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(INTRO_CHAPTERS))

        assert _messages(job_log) == [
            HEAD,
            '  Chapters: "Intro" chapter at 2:06–2:37',
            "  TheIntroDB: no entry",
            "  Plex's own markers: credits start at 21:30",
            "  Credit text: not read (every server keeps its own credits)",
            '  Decided: intro 2:06–2:37, from the "Intro" chapter · credits kept Plex\'s own',
            "  Sent to Plex: intro 2:06–2:37; kept Plex's own credits",
            DONE,
        ]
        ctx.local_detectors[0].detect.assert_not_called()
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_plexs_marker_made_for_an_earlier_file_says_so(self, store, media, job_log):
        # Plex's credits were detected against the file this one replaced: the file is read, and the line says why
        # Plex's credits confirmed nothing.
        plex = ready_publisher()
        plex.types_not_made_for_file.return_value = frozenset({T.CREDITS})
        ctx = _job(store, media, _plex(media, keeps_own_credits=True))
        _run(ctx, media, {"plex-1": plex}, probe=_probe(INTRO_CHAPTERS))

        assert "  Plex's own markers: credits start at 21:30 (made for an earlier file)" in _messages(job_log)
        ctx.local_detectors[0].detect.assert_called_once()

    def test_a_decided_type_the_server_keeps_its_own_of_says_ours_wasnt_written(self, store, media, job_log):
        plex = ready_publisher()

        def keeps_plexs_credits(item_id, markers, **kwargs):
            plex.last_kept_types, plex.last_write_changed = frozenset({T.CREDITS}), True
            return [m for m in plex.project(markers) if m.type is not T.CREDITS]

        plex.write.side_effect = keeps_plexs_credits
        ctx = _job(store, media, _plex(media, keeps_own_credits=True))
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(test_pipeline.CHAPTERS_BOTH))

        assert _messages(job_log) == [
            HEAD,
            '  Chapters: "Intro" chapter at 2:06–2:37, "Credits" chapter from 21:35',
            "  TheIntroDB: no entry",
            "  Plex's own markers: credits start at 21:30",
            "  Credit text: not read (a chapter named Credits is used as-is)",
            '  Decided: intro 2:06–2:37, from the "Intro" chapter · credits 21:35–22:01, from the "Credits" chapter',
            "  Sent to Plex: intro 2:06–2:37; kept Plex's own credits instead of ours (\"Keep Plex's\")",
            DONE,
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_server_that_hasnt_indexed_the_file_is_waited_for_even_with_a_type_in_review(self, store, media, job_log):
        ctx = _job(store, media, _plex(media, indexed=False), theintrodb=TIDB, found=())
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(INTRO_CHAPTERS))

        assert _messages(job_log) == [
            HEAD,
            '  Chapters: "Intro" chapter at 2:06–2:37',
            "  TheIntroDB: credits 21:36–22:00",
            "  Credit text: none found (read on the CPU in 0 s)",
            "  Plex's own markers: not read",
            f'  Decided: intro 2:06–2:37, from the "Intro" chapter · credits 21:36–22:00 from TheIntroDB {ONLINE_ONLY}',
            "  Sent to Plex: not in Plex's library yet (waiting for it to add the file)",
            DONE,
        ]
        row = out.publisher_rows[0]
        assert (row["status"], row["reason_code"]) == (ServerStatus.WAITING.value, NOT_IN_LIBRARY)
        # The job retries the file, so it counts as waiting, not as needing review.
        assert out.outcome_key == FileOutcome.WAITING.value

    def test_an_online_source_out_of_its_daily_budget_is_named_as_skipped_and_counted(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=BUDGET)
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert "  TheIntroDB: skipped (daily limit reached, resets 00:00 UTC)" in _messages(job_log)
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert ctx.summary_lines({FileOutcome.PUBLISHED.value: 1}) == [
            "Done: 1 file · 1 sent to Plex · 0 need review · 0 nothing found · TheIntroDB skipped for 1 file"
        ]

    def test_a_file_whose_answer_didnt_change_gets_one_line(self, store, media, job_log):
        reg = _plex(media)
        for expected in (FileOutcome.PUBLISHED, FileOutcome.UP_TO_DATE):
            ctx = _job(store, media, reg, now=lambda: datetime.now(UTC))
            out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())
            assert out.outcome_key == expected.value

        assert len(job_log) == 8 + 1
        assert _messages(job_log)[-1] == f"{EPISODE}: unchanged, Plex already has our credits"

    def test_nothing_found_twice_says_so_in_one_line_the_second_time(self, store, media, job_log):
        reg = _plex(media)
        for _ in range(2):
            ctx = _job(store, media, reg, found=(), now=lambda: datetime.now(UTC))
            out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())
            assert out.outcome_key == FileOutcome.NO_MARKERS.value

        assert _messages(job_log) == [
            HEAD,
            "  Chapters: none",
            "  TheIntroDB: no entry",
            "  Credit text: none found (read on the CPU in 0 s)",
            "  Plex's own markers: none",
            "  Decided: intro nothing found · credits nothing found",
            "  Sent to Plex: nothing to send",
            DONE,
            f"{EPISODE}: unchanged, nothing sent to Plex; nothing found",
        ]

    def test_a_movie_without_a_server_title_is_named_by_its_file(self, store, movie, job_log):
        chapters = (Chapter(0, 1_295_324, "Movie"), Chapter(1_295_324, None, "Credits"))
        ctx = _job(store, movie, _plex(movie))
        out, _ = _run(ctx, movie, {"plex-1": ready_publisher()}, probe=_probe(chapters))

        assert _messages(job_log) == [
            "Heat (1995): checking credits (films get credits only)",
            '  Chapters: "Credits" chapter from 21:35',
            "  Plex's own markers: none",
            "  TheIntroDB: not asked (no server confirmed whether it's a movie or an episode)",
            "  Credit text: not read (a chapter named Credits is used as-is)",
            '  Decided: credits 21:35–22:01, from the "Credits" chapter',
            "  Sent to Plex: credits 21:35–22:01",
            "Heat (1995): done in 0 s, no worker needed",
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_source_without_an_answer_this_time_says_why(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=LookupResult("unavailable", detail="TheIntroDB HTTP 503"))
        ctx.local_detectors[0].detect.side_effect = DetectorUnavailableError("the decode timed out")
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log)[1:5] == [
            "  Chapters: none",
            "  TheIntroDB: unavailable (HTTP 503)",
            "  Credit text: no answer this time (the decode timed out)",
            "  Plex's own markers: none",
        ]

    def test_a_file_the_worker_finishes_opens_with_the_pickup_line_and_names_the_checking_stages_answers(
        self, store, media, job_log
    ):
        ctx = _job(store, media, _plex(media), on_worker=True)
        publishers = {"plex-1": ready_publisher()}
        handed_on, _ = _run(ctx, media, publishers, probe=_probe())
        assert handed_on is None and job_log == []

        pipeline.log_pickup(ProcessableItem(canonical_path=media, server_id="plex-1"), "CPU Worker 1", ctx=ctx)
        _run(ctx, media, publishers, probe=_probe(), stage="process", worker_name="CPU Worker 1")

        assert _messages(job_log) == [
            f"CPU Worker 1 picked up {EPISODE}: checking intro and credits",
            "  Chapters: none",
            "  TheIntroDB: no entry",
            "  Credit text: credits start at 21:31 (read on the CPU in 0 s)",
            "  Plex's own markers: none",
            "  Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            "  Sent to Plex: credits 21:31–22:01",
            f"{EPISODE}: done in 0 s on CPU Worker 1",
        ]

    def test_a_worker_times_the_file_from_when_it_picked_it_up(self, store, media, job_log):
        # The checking stage's 5 s aren't the worker's: its "done" line counts from its own start.
        ctx = _job(store, media, _plex(media), on_worker=True)
        clock = ctx.monotonic

        def probe(*args, **kwargs):
            clock.advance(5)
            return _probe()

        def detect(*args, **kwargs):
            clock.advance(7)
            return [TEXT_CREDITS]

        ctx.local_detectors[0].detect.side_effect = detect
        handed_on, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe_effect=probe)
        assert handed_on is None
        _run(ctx, media, {"plex-1": ready_publisher()}, probe_effect=probe, stage="process", worker_name="CPU Worker 1")

        assert "  Credit text: credits start at 21:31 (read on the CPU in 7 s)" in _messages(job_log)
        assert _messages(job_log)[-1] == f"{EPISODE}: done in 7 s on CPU Worker 1"

    def test_a_gpu_workers_rerun_on_the_cpu_counts_from_its_first_try(self, store, media, job_log):
        # The GPU try's 4 s and the CPU rerun's 6 s are one file on one worker.
        from media_preview_generator.processing.generator import CodecNotSupportedError

        ctx = _job(store, media, _plex(media), on_worker=True)
        clock = ctx.monotonic

        def detect(*args, gpu=None, **kwargs):
            clock.advance(4 if gpu else 6)
            if gpu:
                raise CodecNotSupportedError("hevc")
            return [TEXT_CREDITS]

        ctx.local_detectors[0].detect.side_effect = detect
        worker = "GPU Worker 1 (NVIDIA TITAN RTX)"
        with pytest.raises(CodecNotSupportedError):
            _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu="nvidia",
                 gpu_worker=True, worker_name=worker)  # fmt: skip
        assert job_log == []
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu=None, gpu_worker=True,
             worker_name=worker)  # fmt: skip

        assert "  Credit text: credits start at 21:31 (read on the CPU in 6 s)" in _messages(job_log)
        assert _messages(job_log)[-1] == f"{EPISODE}: done in 10 s on GPU Worker 1, rerun on the CPU"

    def test_a_file_a_newer_file_replaced_gets_one_line(self, store, media, job_log):
        stale = os.path.join(os.path.dirname(media), "Rick and Morty (2013) - S01E01 - Pilot-CAKES.mkv")
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, stale, {"plex-1": ready_publisher()}, probe=_probe())

        assert out.outcome_key == FileOutcome.SOURCE_GONE.value
        assert job_log == [
            ("INFO", f"{EPISODE}: Skipped: replaced by a newer file (Rick and Morty (2013) - S01E01 - Pilot.mkv)")
        ]

    def test_a_file_cut_short_says_how_much_of_it_can_be_read(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), on_worker=True)
        cut_short = "the file ends before its stated length (10:00 of 22:01 readable)"
        ctx.local_detectors[0].detect.side_effect = DetectorUnavailableError(cut_short)
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu="nvidia",
             gpu_worker=True, worker_name="GPU Worker 1")  # fmt: skip

        assert f"  Credit text: {cut_short}" in _messages(job_log)

    def test_a_tail_the_gpu_read_nothing_from_says_it_was_read_on_the_cpu(self, store, media, job_log):
        from media_preview_generator.markers.credits.detector import CPU_RECHECK_PHASE

        ctx = _job(store, media, _plex(media), on_worker=True)

        def detect(*args, phase_callback=None, fallback_callback=None, **kwargs):
            ctx.monotonic.advance(5)
            phase_callback(CPU_RECHECK_PHASE)  # "the GPU read no frames in that part of the file; checking on CPU"
            ctx.monotonic.advance(15)
            fallback_callback(
                "The GPU read no frames in the end of a.mkv, but the CPU did; its credits were read on the CPU"
            )
            return [TEXT_CREDITS]

        ctx.local_detectors[0].detect.side_effect = detect
        shown = []
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu="nvidia", gpu_worker=True,
             worker_name="GPU Worker 1", phase_callback=shown.append, fallback_callback=shown.append)  # fmt: skip

        assert (
            "  Credit text: credits start at 21:31 (read on the CPU after the GPU read nothing (20 s in all))"
        ) in _messages(job_log)
        # The worker row still shows the step.
        assert CPU_RECHECK_PHASE in shown

    def test_a_step_that_fell_back_to_the_cpu_says_why(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), on_worker=True)

        def detect(*args, fallback_callback=None, **kwargs):
            fallback_callback("Credit text detection on the CPU: the GPU ran out of memory")
            ctx.monotonic.advance(20)
            return [TEXT_CREDITS]

        ctx.local_detectors[0].detect.side_effect = detect
        shown = []
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu="nvidia",
             gpu_worker=True, worker_name="GPU Worker 1", fallback_callback=shown.append)  # fmt: skip

        assert (
            "  Credit text: credits start at 21:31 (read on the GPU in 20 s; credit text detection on the CPU: the GPU "
            "ran out of memory)"
        ) in _messages(job_log)
        # The worker row still hears of it.
        assert shown == ["Credit text detection on the CPU: the GPU ran out of memory"]

    def test_a_failed_write_logs_the_block_at_warning(self, store, media, job_log):
        plex = ready_publisher()
        plex.write.side_effect = PublishError("Plex refused the write")
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe())

        assert out.outcome_key == FileOutcome.FAILED.value
        messages = _messages(job_log)
        assert messages[0] == HEAD
        assert messages[-2].startswith("  Sent to Plex: failed (")
        assert messages[-1] == f"{EPISODE}: failed after 0 s"
        assert {level for level, _ in job_log} == {"WARNING"}

    def test_a_file_that_couldnt_be_read_logs_why_at_warning(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe_effect=ProbeError("ffprobe exited 1"))

        assert out.outcome_key == FileOutcome.FAILED.value
        assert job_log == [
            ("WARNING", HEAD),
            ("WARNING", "  Failed: Couldn't read the file: ffprobe exited 1"),
            ("WARNING", f"{EPISODE}: failed after 0 s"),
        ]

    def test_a_cancelled_file_logs_nothing(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), cancel_check=lambda: True)
        assert job_log == []


class TestConcurrentWorkers:
    def test_each_files_block_stays_together_while_two_workers_finish_at_once(self, store, tmp_path):
        paths = [_accused(tmp_path, episode) for episode in (1, 2)]
        reg = _plex(paths[0])
        ctx = _job(store, paths[0], reg, on_worker=True)
        records: list[str] = []

        def slow_sink(message):
            records.append(message.record["message"])
            time.sleep(0.002)  # a slow sink widens the gap another thread's record could fall into

        handler = logger.add(
            slow_sink,
            level="INFO",
            format="{message}",
            filter=lambda record: record["module"] == "job_log",
        )
        both_written = threading.Barrier(2, timeout=10)

        def write(publisher):
            def written(*args, **kwargs):
                both_written.wait()  # both files reach their lines at the same moment
                return publisher.succeed(*args, **kwargs)

            return written

        publishers = {}
        for path in paths:
            publisher = ready_publisher()
            publisher.write.side_effect = write(publisher)
            publishers[path] = publisher
        errors: list[BaseException] = []

        def worker(path, name):
            try:
                hints = {"plex-1": f"item-{paths.index(path)}"}
                pipeline.process_item(
                    ProcessableItem(canonical_path=path, server_id="plex-1", item_id_by_server=hints),
                    ctx=ctx,
                    worker_name=name,
                )
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        try:
            with (
                patch.object(pipeline, "probe_media", return_value=_probe()),
                patch.object(pipeline, "publisher_for", side_effect=_publisher_by_item(publishers, paths)),
            ):
                threads = [
                    threading.Thread(target=worker, args=(path, f"CPU Worker {n}")) for n, path in enumerate(paths, 1)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)
        finally:
            logger.remove(handler)

        assert errors == []
        assert len(records) == 2 * 7
        for name in ("Accused S04E01", "Accused S04E02"):
            at = [n for n, line in enumerate(records) if line.startswith(f"{name}: ")]
            assert len(at) == 1
            end = at[0]
            # Its detail lines are the 6 records right before its "done" line, with no other file's line between.
            block = records[end - 6 : end + 1]
            assert all(line.startswith("  ") for line in block[:-1]), records
            assert block[-1].startswith(f"{name}: done in"), records
        assert records[6].startswith("Accused S04E0") and records[13].startswith("Accused S04E0")

    def test_write_lines_keeps_each_callers_lines_together(self):
        records: list[str] = []

        def slow_sink(message):
            records.append(message.record["message"])
            time.sleep(0.001)

        handler = logger.add(slow_sink, level="INFO", format="{message}", filter=lambda r: r["module"] == "job_log")
        start = threading.Barrier(2, timeout=10)

        def write(tag):
            start.wait()
            write_lines([f"{tag} {n}" for n in range(20)])

        try:
            threads = [threading.Thread(target=write, args=(tag,)) for tag in ("a", "b")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        finally:
            logger.remove(handler)

        tags = [line.split()[0] for line in records]
        assert tags in (["a"] * 20 + ["b"] * 20, ["b"] * 20 + ["a"] * 20)


def _publisher_by_item(publishers, paths):
    """``publisher_for`` for two files on one Plex server: each file's own publisher, told apart by its item id."""
    by_item = {f"item-{n}": publishers[path] for n, path in enumerate(paths)}
    shared = ready_publisher()

    def write(item_id, markers, **kwargs):
        return by_item[item_id].write(item_id, markers, **kwargs)

    shared.write.side_effect = write
    return lambda server, cfg, **kw: shared


# ---------------------------------------------------------------------------------------------------------------------
# Line builders, cell by cell.
# ---------------------------------------------------------------------------------------------------------------------


def _row(source, mtype=None, start=None, end=None, *, origin="", label="", detail=""):
    return EvidenceRow(source, origin, mtype, start, end, None, detail, "2026-09-27T00:00:00+00:00", label)


def _decided(mtype, start, end, *sources, reason="", locked=False):
    marker = Marker(mtype, start, end, tuple(sources), locked=locked)
    return TypeDecision(mtype, DecisionStatus.DECIDED, marker, None, reason or f"single source ({sources[0]})")


NONE_DECIDED = {
    T.INTRO: TypeDecision(T.INTRO, DecisionStatus.NO_EVIDENCE, None, None, "no evidence"),
    T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NO_EVIDENCE, None, None, "no evidence"),
}
BOTH_DECIDED = {
    T.INTRO: _decided(T.INTRO, 41_000, 72_000, "introdb", "season_audio", reason="sources agree"),
    T.CREDITS: _decided(T.CREDITS, 2_508_000, 2_590_000, "credits_text"),
}
BOTH = frozenset({T.INTRO, T.CREDITS})
UNUSABLE = {"unusable": "couldn't be used (unreadable, another cut, or its library hides a type in Plex)"}


def _lines_for(source_ids, rows, notes, *, skipped=None, decisions=NONE_DECIDED, is_episode=True, servers=()):
    return source_lines(
        source_ids,
        rows,
        servers,
        notes,
        skipped or {},
        UNUSABLE,
        decisions=decisions,
        types=BOTH if is_episode else frozenset({T.CREDITS}),
        is_episode=is_episode,
    )


def _asked(*sources: Source, server: str = "") -> RunNotes:
    notes = RunNotes()
    for source in sources:
        notes.answered(source, server if source is Source.SERVER_MARKERS else "")
    return notes


class TestSourceLines:
    """Each state a source's line can be in."""

    @pytest.mark.parametrize(
        ("source", "rows", "expected"),
        [
            (Source.CHAPTERS, [_row(Source.CHAPTERS)], "  Chapters: none"),
            (
                Source.CHAPTERS,
                [_row(Source.CHAPTERS, T.INTRO, 41_000, 72_000, label="Opening")],
                '  Chapters: "Opening" chapter at 0:41–1:12',
            ),
            (
                Source.CHAPTERS,
                [_row(Source.CHAPTERS, T.INTRO, 41_000, 72_000)],
                "  Chapters: intro chapter at 0:41–1:12",
            ),
            (Source.THEINTRODB, [_row(Source.THEINTRODB)], "  TheIntroDB: no entry"),
            (Source.SKIPDB, [_row(Source.SKIPDB, T.CREDITS, 2_508_000, 2_590_000)], "  SkipDB: credits 41:48–43:10"),
            (Source.INTRODB, [_row(Source.INTRODB, T.INTRO, 41_000, 72_000)], "  IntroDB: intro 0:41–1:12"),
            (Source.SEASON_AUDIO, [_row(Source.SEASON_AUDIO)], "  Season audio: no match"),
            (
                Source.SEASON_AUDIO,
                [_row(Source.SEASON_AUDIO, T.INTRO, 41_000, 72_000, label="9/9")],
                "  Season audio: intro 0:41–1:12 (same theme found in 10 of 10 episodes)",
            ),
            (Source.CREDITS_TEXT, [_row(Source.CREDITS_TEXT)], "  Credit text: none found"),
            (
                Source.CREDITS_TEXT,
                [_row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, 2_590_000)],
                "  Credit text: credits 41:48–43:10",
            ),
        ],
        ids=[
            "chapters-none",
            "chapter-named",
            "chapter-unnamed",
            "online-no-entry",
            "online-answer",
            "introdb-answer",
            "season-audio-no-match",
            "season-audio-match",
            "credit-text-none",
            "credit-text-with-end",
        ],
    )
    def test_an_answer_this_job(self, source, rows, expected):
        assert _lines_for([source.value], rows, _asked(source)) == [expected]

    def test_an_answer_stored_earlier_says_so(self):
        rows = [
            _row(Source.CHAPTERS),
            _row(Source.SEASON_AUDIO, T.INTRO, 41_000, 72_000, label="8/9"),
            _row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, None),
        ]
        assert _lines_for(["chapters", "season_audio", "credits_text"], rows, RunNotes()) == [
            "  Chapters: none (saved earlier)",
            "  Season audio: intro 0:41–1:12 (same theme found in 9 of 10 episodes; saved earlier)",
            "  Credit text: credits start at 41:48 (saved earlier)",
        ]

    def test_last_seasons_audio_follows_season_audio(self):
        rows = [_row(Source.SEASON_AUDIO), _row(Source.SEASON_AUDIO_PREVIOUS, T.INTRO, 41_000, 72_000, label="7/8")]
        notes = _asked(Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS)
        assert _lines_for(["season_audio"], rows, notes) == [
            "  Season audio: no match",
            "  Last season's audio: intro 0:41–1:12 (same theme found in 7 of last season's 8 episodes)",
        ]

    @pytest.mark.parametrize(
        ("read", "expected"),
        [
            (read_phrase(True, 13.2), "read on the GPU in 13 s"),
            (read_phrase(False, 0.46), "read on the CPU in 0.5 s"),
            (
                read_phrase(True, 95, "Credit text detection on the CPU: no GPU memory"),
                "read on the GPU in 1 min 35 s; credit text detection on the CPU: no GPU memory",
            ),
            (
                read_phrase(
                    True, 40, "The GPU read no frames in the end of a.mkv, but the CPU did", gpu_read_nothing=True
                ),
                "read on the CPU after the GPU read nothing (40 s in all)",
            ),
        ],
        ids=["gpu", "cpu", "fallback", "gpu-read-nothing"],
    )
    def test_credit_text_read_this_job_says_where_and_how_long(self, read, expected):
        notes = _asked(Source.CREDITS_TEXT)
        notes.reads[Source.CREDITS_TEXT] = read
        rows = [_row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, None)]
        assert _lines_for(["credits_text"], rows, notes) == [f"  Credit text: credits start at 41:48 ({expected})"]

    def test_a_source_skipped_for_the_whole_job_says_why(self):
        skipped = {Source.THEINTRODB: "TheIntroDB budget_exhausted", Source.SKIPDB: "SkipDB rejected the API key"}
        assert _lines_for(["theintrodb", "skipdb"], [], RunNotes(), skipped=skipped) == [
            "  TheIntroDB: skipped (daily limit reached, resets 00:00 UTC)",
            "  SkipDB: skipped (rejected the API key)",
        ]

    def test_a_source_asked_without_an_answer_to_store_says_what_happened(self):
        notes = RunNotes(unanswered={Source.SKIPDB: "unavailable (HTTP 503)"})
        assert _lines_for(["skipdb"], [], notes) == ["  SkipDB: unavailable (HTTP 503)"]

    @pytest.mark.parametrize(
        ("source", "note", "expected"),
        [
            (Source.SKIPDB, "not asked (not set up)", "  SkipDB: not asked (not set up)"),
            (Source.SKIPDB, None, "  SkipDB: not asked"),
            (Source.CREDITS_TEXT, None, "  Credit text: not read"),
            (Source.SEASON_AUDIO, "not available here", "  Season audio: not available here"),
            (
                Source.CREDITS_TEXT,
                "not read (every server keeps its own credits)",
                "  Credit text: not read (every server keeps its own credits)",
            ),
            (Source.CREDITS_TEXT, ALREADY_DECIDED, "  Credit text: not read (already decided)"),
            (Source.SKIPDB, ALREADY_DECIDED, "  SkipDB: not asked (already decided)"),
        ],
        ids=["reason", "online-no-note", "local-no-note", "not-available", "kept-everywhere", "decided-local",
             "decided-online"],
    )  # fmt: skip
    def test_a_source_not_asked_says_why(self, source, note, expected):
        notes = RunNotes(not_asked={source: note} if note else {})
        decisions = {**NONE_DECIDED, T.CREDITS: _decided(T.CREDITS, 2_508_000, 2_590_000, "skipdb", "introdb")}
        assert _lines_for([source.value], [], notes, decisions=decisions) == [expected]

    def test_a_source_not_needed_after_a_chapter_named_for_the_type_names_it(self):
        rows = [_row(Source.CHAPTERS, T.CREDITS, 2_508_000, 2_590_000, label="End Credits")]
        decisions = {
            **NONE_DECIDED,
            T.CREDITS: _decided(T.CREDITS, 2_508_000, 2_590_000, "chapters", reason="chapters"),
        }
        notes = _asked(Source.CHAPTERS)
        notes.not_asked[Source.CREDITS_TEXT] = ALREADY_DECIDED
        assert _lines_for(["chapters", "credits_text"], rows, notes, decisions=decisions) == [
            '  Chapters: "End Credits" chapter at 41:48–43:10',
            "  Credit text: not read (a chapter named End Credits is used as-is)",
        ]

    def test_an_online_source_not_needed_after_chapters_named_for_every_type_names_them(self):
        rows = [
            _row(Source.CHAPTERS, T.INTRO, 41_000, 72_000, label="Intro"),
            _row(Source.CHAPTERS, T.CREDITS, 2_508_000, 2_590_000, label="Credits"),
        ]
        decisions = {
            T.INTRO: _decided(T.INTRO, 41_000, 72_000, "chapters", reason="chapters"),
            T.CREDITS: _decided(T.CREDITS, 2_508_000, 2_590_000, "chapters", reason="chapters"),
        }
        notes = RunNotes(not_asked={Source.SKIPDB: ALREADY_DECIDED})
        assert _lines_for(["skipdb"], rows, notes, decisions=decisions) == [
            "  SkipDB: not asked (chapters named Intro and Credits are used as-is)"
        ]

    @pytest.mark.parametrize(
        ("rows", "asked", "expected"),
        [
            ([_row(Source.SERVER_MARKERS, origin="plex-1")], True, "  Plex's own markers: none"),
            (
                [_row(Source.SERVER_MARKERS, T.CREDITS, 2_508_000, 2_590_000, origin="plex-1")],
                True,
                "  Plex's own markers: credits 41:48–43:10",
            ),
            (
                [
                    _row(
                        Source.SERVER_MARKERS, T.CREDITS, 2_508_000, 2_590_000, origin="plex-1",
                        detail=STALE_SERVER_MARKERS_DETAIL,
                    )
                ],
                True,
                "  Plex's own markers: credits 41:48–43:10 (made for an earlier file)",
            ),
            (
                [_row(Source.SERVER_MARKERS, origin="plex-1", detail="unusable")],
                True,
                f"  Plex's own markers: {UNUSABLE['unusable']}",
            ),
            (
                [_row(Source.SERVER_MARKERS_IMPORTED, T.INTRO, 41_000, 72_000, origin="plex-1")],
                True,
                "  Plex's imported markers: intro 0:41–1:12",
            ),
            ([_row(Source.SERVER_MARKERS, origin="plex-1")], False, "  Plex's own markers: none (saved earlier)"),
        ],
        ids=["none", "answer", "made-for-an-earlier-file", "unusable", "imported", "saved-earlier"],
    )  # fmt: skip
    def test_each_servers_own_markers(self, rows, asked, expected):
        notes = _asked(Source.SERVER_MARKERS, server="plex-1") if asked else RunNotes()
        assert _lines_for(["server_markers"], rows, notes, servers=[("plex-1", "Plex")]) == [expected]

    def test_a_server_never_read_comes_after_the_answers(self):
        rows = [_row(Source.CHAPTERS), _row(Source.SERVER_MARKERS, origin="plex-1")]
        notes = _asked(Source.CHAPTERS, Source.SERVER_MARKERS, server="plex-1")
        notes.not_asked[Source.CREDITS_TEXT] = "not available here"
        servers = [("plex-1", "Plex"), ("emby-1", "Emby")]
        order = ["server_markers", "credits_text", "chapters"]  # the user's order doesn't move the lines
        assert _lines_for(order, rows, notes, servers=servers) == [
            "  Chapters: none",
            "  Plex's own markers: none",
            "  Credit text: not available here",
            "  Emby's own markers: not read",
        ]

    def test_a_film_leaves_out_the_sources_that_describe_episodes_only(self):
        notes = _asked(Source.CHAPTERS, Source.SKIPDB)
        notes.unanswered[Source.INTRODB] = "not asked (needs a TV episode with an imdb id)"
        notes.not_asked[Source.SEASON_AUDIO] = "doesn't apply to this file"
        rows = [_row(Source.CHAPTERS), _row(Source.SKIPDB)]
        enabled = ["chapters", "introdb", "skipdb", "season_audio"]
        assert _lines_for(enabled, rows, notes, is_episode=False) == ["  Chapters: none", "  SkipDB: no entry"]


class TestDecidedLine:
    @pytest.mark.parametrize(
        ("decision", "expected"),
        [
            (_decided(T.INTRO, 41_000, 72_000, "introdb", "season_audio", reason="sources agree: introdb, season_audio"),
             "intro 0:41–1:12 (IntroDB and season audio agree)"),
            (_decided(T.INTRO, 41_000, 72_000, "introdb", "skipdb", "season_audio", reason="sources agree"),
             "intro 0:41–1:12 (IntroDB, SkipDB and season audio agree)"),
            (_decided(T.CREDITS, 2_508_000, 2_590_000, "credits_text"), "credits 41:48–43:10 (credit text)"),
            (_decided(T.CREDITS, 2_508_000, 2_590_000, "chapters", reason="chapters"),
             "credits 41:48–43:10, from the chapters"),
            (_decided(T.CREDITS, 2_508_000, 2_590_000, "credits_text", locked=True), "credits 41:48–43:10 (locked by you)"),
            (_decided(T.INTRO, 0, 92_000, "carried_over"), "intro 0:00–1:32 (from the file it replaced)"),
            (TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None,
                          Marker(T.CREDITS, 2_508_000, 2_590_000, ("credits_text",)), "only credit text found it"),
             "credits 41:48–43:10 from credit text needs review (only credit text found it)"),
            (TypeDecision(T.INTRO, DecisionStatus.NEEDS_REVIEW, None, None, "sources disagree: chapters, introdb"),
             "intro needs review (sources disagree: chapters, introdb)"),
            (NONE_DECIDED[T.INTRO], "intro nothing found"),
            (TypeDecision(T.CREDITS, DecisionStatus.NO_EVIDENCE, None, None, "2 candidate(s) failed sanity checks"),
             "credits nothing found (2 candidate(s) failed sanity checks)"),
            (TypeDecision(T.CREDITS, DecisionStatus.DISABLED, None, None, "kept Plex's and Emby's own markers"),
             "credits kept Plex's and Emby's own"),
            (TypeDecision(T.RECAP, DecisionStatus.DISABLED, None, None, "detection off"), "recap: detection off"),
        ],
        ids=["two-agree", "three-agree", "one-source", "chapters-unnamed", "locked", "carried-over", "review",
             "review-no-proposal", "nothing-found", "nothing-found-why", "kept-own", "other"],
    )  # fmt: skip
    def test_type_phrase(self, decision, expected):
        assert type_phrase(decision) == expected

    def test_a_marker_the_server_shortened_or_kept_through_a_rule_change_says_so(self):
        shortened = _decided(
            T.CREDITS,
            2_508_000,
            2_590_000,
            "chapters",
            "server_markers",
            reason="chapters; start shortened to the server's own marker (plex-1)",
        )
        assert type_phrase(shortened, {T.CREDITS: {2_500_000: "Credits"}}) == (
            'credits 41:48–43:10 (the "Credits" chapter and server markers agree; start moved to the server\'s own '
            "marker)"
        )
        kept = _decided(T.INTRO, 41_000, 72_000, "introdb", "season_audio", reason="kept: published before a rule "
                        "change; today's rules: sources disagree")  # fmt: skip
        assert type_phrase(kept) == (
            "intro 0:41–1:12 (IntroDB and season audio agree; kept: published before a rule change)"
        )


def _server(status, ours=(), *, kept=frozenset(), withheld=frozenset(), message="", reason_code=None):
    row = {"server_id": "plex-1", "server_name": "Plex", "server_type": "plex", "status": status.value,
           "message": message, "reason_code": reason_code}  # fmt: skip
    return ServerResult(row, tuple(ours), frozenset(kept), frozenset(withheld))


INTRO_M = Marker(T.INTRO, 41_000, 72_000, ("introdb",))
CREDITS_M = Marker(T.CREDITS, 2_508_000, 2_590_000, ("credits_text",))


class TestSentLine:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (_server(ServerStatus.WRITTEN, (CREDITS_M, INTRO_M)), "intro 0:41–1:12 · credits 41:48–43:10"),
            (_server(ServerStatus.WRITTEN), "cleared our markers"),
            (_server(ServerStatus.UP_TO_DATE, (INTRO_M,)), "already up to date (intro 0:41–1:12)"),
            (_server(ServerStatus.NONE), "nothing to send"),
            (_server(ServerStatus.UP_TO_DATE, kept={T.CREDITS}), "kept Plex's own credits"),
            (_server(ServerStatus.WRITTEN, (INTRO_M,), kept={T.CREDITS}, withheld={T.CREDITS}),
             "intro 0:41–1:12; kept Plex's own credits instead of ours (\"Keep Plex's\")"),
            (_server(ServerStatus.FAILED, message="Plex's database was busy"), "failed (Plex's database was busy)"),
            (_server(ServerStatus.SKIPPED, message="Plex Pass needed"), "skipped (Plex Pass needed)"),
            (_server(ServerStatus.WAITING, reason_code=NOT_IN_LIBRARY),
             "not in Plex's library yet (waiting for it to add the file)"),
            (_server(ServerStatus.WAITING, reason_code=PLEX_PASS_UNKNOWN), "waiting (couldn't confirm Plex Pass)"),
            (_server(ServerStatus.WAITING, (INTRO_M,), message="Waiting for the other versions"),
             "intro 0:41–1:12; waiting for the other versions"),
        ],
        ids=["sent", "cleared", "up-to-date", "nothing", "kept-own", "kept-instead-of-ours", "failed", "skipped",
             "not-in-library", "plex-pass-unknown", "waiting"],
    )  # fmt: skip
    def test_one_line_per_server(self, result, expected):
        assert sent_line(result) == f"  Sent to Plex: {expected}"


class TestCompactLines:
    @pytest.mark.parametrize(
        ("servers", "decisions", "expected"),
        [
            ([_server(ServerStatus.UP_TO_DATE, (INTRO_M, CREDITS_M))], BOTH_DECIDED,
             "Plex already has our intro and credits"),
            ([_server(ServerStatus.UP_TO_DATE, (INTRO_M,), kept={T.CREDITS})], BOTH_DECIDED,
             "Plex already has our intro; Plex keeps its own credits"),
            ([_server(ServerStatus.NEEDS_REVIEW)],
             {**NONE_DECIDED, T.CREDITS: TypeDecision(T.CREDITS, DecisionStatus.NEEDS_REVIEW, None,
                                                      Marker(T.CREDITS, 1, 2, ("credits_text",)), "alone")},
             "nothing sent to Plex; still needs review (credits from credit text only)"),
            ([_server(ServerStatus.NONE)], NONE_DECIDED, "nothing sent to Plex; nothing found"),
        ],
        ids=["up-to-date", "kept-own", "in-review", "nothing-found"],
    )  # fmt: skip
    def test_wording(self, servers, decisions, expected):
        assert compact_line("Accused S04E05", servers, decisions, BOTH) == f"Accused S04E05: unchanged, {expected}"

    def test_a_hundred_file_recheck_logs_a_line_per_file(self, store, tmp_path, job_log):
        paths = [_accused(tmp_path, episode) for episode in range(1, 6)]
        reg = _plex(paths[0])

        def run_all():
            ctx = _job(store, paths[0], reg, now=lambda: datetime.now(UTC))
            for n, path in enumerate(paths):
                _run(ctx, path, {"plex-1": ready_publisher()}, probe=_probe(), hints={"plex-1": f"item-{n}"})

        run_all()
        job_log.clear()
        run_all()

        assert _messages(job_log) == [
            f"Accused S04E{n:02d}: unchanged, Plex already has our credits" for n in range(1, 6)
        ]


class TestTitles:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/tv/Brave New World {tvdb-1}/Season 01/Brave New World - S01E05 - Swallow.mkv", "Brave New World S01E05"),
            ("/tv/The Fall (2013) [imdbid-tt2294189]/Season 3/The Fall - s03e05.mkv", "The Fall (2013) S03E05"),
            ("/tv/Flat Show/Flat Show S02E10.mkv", "Flat Show S02E10"),
            ("/movies/Heat (1995) {tmdb-949}/Heat (1995) {edition-Director's Cut}.mkv", "Heat (1995)"),
            (
                "/movies/32 Frames/32 Frames A 9 11 Mystery (2026) - [AMZN][WEBDL-1080p][EAC3 5.1][h264]-cinepth.mkv",
                "32 Frames A 9 11 Mystery (2026)",
            ),
            ("/movies/X/Movie (2020) [1080p]-GROUP.mkv", "Movie (2020)"),
            ("/movies/X/Movie (2020) -- [1080p] - [x265].mkv", "Movie (2020)"),
            ("/movies/X/Spider-Man (2002).mkv", "Spider-Man (2002)"),
        ],
        ids=["season-folder", "imdb-tag", "flat-folder", "movie-edition", "release-group", "group-no-space",
             "dash-runs", "dash-in-title"],
    )  # fmt: skip
    def test_display_name(self, path, expected):
        assert display_name(path) == expected

    @pytest.mark.parametrize(
        ("path", "title", "year", "expected"),
        [
            ("/m/32 Frames A 9 11 Mystery (2026) [h264]-cinepth.mkv", "32 Frames: A 9/11 Mystery", "2026",
             "32 Frames: A 9/11 Mystery (2026)"),
            ("/m/Heat (1995).mkv", "Heat", 1995, "Heat (1995)"),
            ("/m/Heat (1995).mkv", "Heat (1995)", 1995, "Heat (1995)"),
            ("/m/Heat (1995).mkv", "Heat", None, "Heat"),
            ("/m/Heat (1995) [1080p].mkv", None, None, "Heat (1995)"),
            ("/tv/Accused/Season 04/Accused - S04E05 - Ava.mkv", "Ava's Story", 2023, "Accused S04E05"),
        ],
        ids=["server-title", "int-year", "year-in-title", "no-year", "no-server-title", "episode-keeps-sxxeyy"],
    )  # fmt: skip
    def test_file_title(self, path, title, year, expected):
        assert file_title(path, title, year) == expected

    @staticmethod
    def _quiet_job(store, reg):
        """A job with no online source, so nothing but the job log's title could ask the server for ids."""
        raw = {
            "detect": {"intro": True, "credits": True, "recap": False},
            "sources": [{"id": source, "enabled": source in ("chapters", "credits_text", "server_markers")} for source
                        in ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "credits_text",
                            "server_markers")],
        }  # fmt: skip
        ctx = _ctx(store, reg, settings_raw=raw, clients=_clients(), detectors=(_credit_text(),))
        ctx.monotonic = FakeClock()
        return ctx

    @staticmethod
    def _known_movie(store, path):
        """A film whose kind an earlier process stored: this process has no server answer for it."""
        st = os.stat(path)
        rec = store.upsert_file(
            FileIdentity(path, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=True
        )
        store.set_server_kind(rec.id, "movie")

    @staticmethod
    def _check(ctx, item, publisher=None):
        with (
            patch.object(pipeline, "probe_media", return_value=_probe((Chapter(1_295_324, None, "Credits"),))),
            patch.object(pipeline, "publisher_for", return_value=publisher or ready_publisher()),
            patch.object(pipeline, "look_up_title", wraps=titles.look_up) as lookups,
        ):
            return pipeline.check_item(item, ctx=ctx), lookups

    def test_an_episode_never_looks_anything_up(self, store, media, job_log):
        reg = _plex(media)
        server = reg.get("plex-1")
        server.get_external_ids.return_value = {**MOVIE_IDS, "kind": "episode", "title": "Pilot"}
        out, lookups = self._check(self._quiet_job(store, reg), ProcessableItem(canonical_path=media, server_id=""))

        assert _messages(job_log)[0] == HEAD
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        lookups.assert_not_called()
        server.get_external_ids.assert_not_called()

    def test_a_film_is_named_from_the_runs_own_answer_without_a_lookup(self, store, movie, job_log):
        reg = _plex(movie)
        server = reg.get("plex-1")
        server.get_external_ids.return_value = {**MOVIE_IDS, "title": "Heat", "year": "1995"}
        _, lookups = self._check(self._quiet_job(store, reg), ProcessableItem(canonical_path=movie, server_id=""))

        assert _messages(job_log)[0] == "Heat (1995): checking credits (films get credits only)"
        # One ask, for the film's kind; its title came with that answer.
        assert server.get_external_ids.call_count == 1
        lookups.assert_not_called()

    def test_a_film_without_an_answer_is_looked_up_once_per_process(self, store, movie, job_log):
        self._known_movie(store, movie)
        reg = _plex(movie)
        server = reg.get("plex-1")
        server.get_external_ids.return_value = {**MOVIE_IDS, "title": "Heat", "year": "1995"}
        item = ProcessableItem(canonical_path=movie, server_id="")

        first, lookups = self._check(self._quiet_job(store, reg), item)
        assert first.outcome_key == FileOutcome.PUBLISHED.value
        assert _messages(job_log)[0] == "Heat (1995): checking credits (films get credits only)"
        assert lookups.call_count == 1
        assert server.get_external_ids.call_args.args == ("item-plex-1",)
        job_log.clear()

        # A later job over the same film (decide again, a version re-run, a Season job) asks nothing.
        second, lookups = self._check(self._quiet_job(store, reg), item)
        assert second.outcome_key == FileOutcome.UP_TO_DATE.value
        assert _messages(job_log) == ["Heat (1995): unchanged, Plex already has our credits"]
        lookups.assert_not_called()
        assert server.get_external_ids.call_count == 1

    @pytest.mark.parametrize(
        ("item_title", "expected", "lookups"),
        [
            # A library listing names the item: its title, the year from the file name, and no lookup.
            ("Heat", "Heat (1995)", 0),
            # A sender or the store named the file by its file name: one lookup for the server's title and year.
            ("Heat (1995).mkv", "Heat: The Director's Cut (1995)", 1),
            # A listing without a title of its own names the item by its path: one lookup too.
            ("/data/movies/Heat (1995).mkv", "Heat: The Director's Cut (1995)", 1),
        ],
        ids=["listing", "sender", "listing-without-a-title"],
    )
    def test_a_listings_own_title_needs_no_lookup(self, store, movie, job_log, item_title, expected, lookups):
        self._known_movie(store, movie)
        reg = _plex(movie)
        server = reg.get("plex-1")
        server.get_external_ids.return_value = {**MOVIE_IDS, "title": "Heat: The Director's Cut", "year": "1995"}
        item = ProcessableItem(canonical_path=movie, server_id="plex-1", title=item_title)

        _, looked_up = self._check(self._quiet_job(store, reg), item)

        assert _messages(job_log)[0] == f"{expected}: checking credits (films get credits only)"
        assert looked_up.call_count == lookups
        assert server.get_external_ids.call_count == lookups

    @pytest.mark.parametrize("failure", ["times-out", "raises", "no-title"])
    def test_a_lookup_that_fails_never_holds_or_fails_the_file(self, store, movie, job_log, monkeypatch, failure):
        self._known_movie(store, movie)
        reg = _plex(movie)
        server = reg.get("plex-1")
        release = threading.Event()
        answered = threading.Event()

        def hangs(item_id):
            release.wait(10)
            answered.set()
            return {**MOVIE_IDS, "title": "Heat", "year": "1995"}

        if failure == "times-out":
            server.get_external_ids.side_effect = hangs
        elif failure == "raises":
            server.get_external_ids.side_effect = RuntimeError("Plex refused the connection")
        else:
            server.get_external_ids.return_value = {k: v for k, v in MOVIE_IDS.items() if k not in ("title", "year")}
        monkeypatch.setattr(titles, "LOOKUP_TIMEOUT_S", 0.2)
        publisher = ready_publisher()
        item = ProcessableItem(canonical_path=movie, server_id="")
        try:
            started = time.monotonic()
            out, _ = self._check(self._quiet_job(store, reg), item, publisher)
            took = time.monotonic() - started

            assert out.outcome_key == FileOutcome.PUBLISHED.value
            assert publisher.write.call_count == 1
            # Named by its cleaned file name; the file waited no longer than the lookup's timeout.
            assert _messages(job_log)[0] == "Heat (1995): checking credits (films get credits only)"
            assert took < 0.2 + 1.0, took
            assert server.get_external_ids.call_count == 1
        finally:
            release.set()
        if failure == "times-out":
            # The answer that came after the wait is kept: a later job names the film by it, without asking.
            assert answered.wait(5)
            assert _wait_for(lambda: titles.TITLE_CACHE.get(movie) == ("Heat", "1995"))
        job_log.clear()
        self._check(self._quiet_job(store, reg), item)
        assert server.get_external_ids.call_count == 1
        name = "Heat (1995)"
        assert _messages(job_log) == [f"{name}: unchanged, Plex already has our credits"]


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestTitleCache:
    def test_it_keeps_the_most_recently_used_files_up_to_its_bound(self):
        cache = titles.TitleCache(max_entries=2)
        cache.put("/a.mkv", ("A", 2001))
        cache.put("/b.mkv", ("B", 2002))
        assert cache.get("/a.mkv") == ("A", 2001)  # /a.mkv is now the most recently used
        cache.put("/c.mkv", titles.NO_TITLE)

        assert (cache.get("/a.mkv"), cache.get("/b.mkv"), cache.get("/c.mkv")) == (("A", 2001), None, titles.NO_TITLE)
        assert len(cache) == 2

    def test_a_lookup_with_no_server_to_ask_asks_nothing_and_keeps_nothing(self):
        assert titles.look_up("/m/x.mkv", []) == titles.NO_TITLE
        assert titles.TITLE_CACHE.get("/m/x.mkv") is None

    def test_while_every_lookup_slot_is_held_a_file_skips_its_lookup_at_once(self, monkeypatch):
        monkeypatch.setattr(titles, "_IN_FLIGHT", threading.BoundedSemaphore(1))
        titles._IN_FLIGHT.acquire()
        try:
            ask = MagicMock(return_value={"title": "X"})
            started = time.monotonic()
            assert titles.look_up("/m/x.mkv", [ask], timeout_s=5) == titles.NO_TITLE
            assert time.monotonic() - started < 1
            ask.assert_not_called()
            # Not asked, so not counted: a later job may still ask once.
            assert titles.TITLE_CACHE.get("/m/x.mkv") is None
        finally:
            titles._IN_FLIGHT.release()

    def test_the_first_server_with_a_title_names_the_film(self):
        asks = [MagicMock(side_effect=RuntimeError("down")), MagicMock(return_value=None),
                MagicMock(return_value={"title": " Heat ", "year": 1995}), MagicMock()]  # fmt: skip
        assert titles.look_up("/m/heat.mkv", asks, timeout_s=5) == ("Heat", 1995)
        asks[3].assert_not_called()
        assert titles.TITLE_CACHE.get("/m/heat.mkv") == ("Heat", 1995)


class TestLineWords:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0 s"), (0.46, "0.5 s"), (3.0, "3 s"), (9.94, "9.9 s"), (13.4, "13 s"), (60, "1 min"), (125, "2 min 5 s"),
         (3_600, "1 h"), (3_840, "1 h 4 min")],
    )  # fmt: skip
    def test_duration(self, seconds, expected):
        assert duration(seconds) == expected

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"seconds": 0.5}, "X: done in 0.5 s, no worker needed"),
            ({"seconds": 25, "worker": "GPU Worker 2 (Intel UHD 770)"}, "X: done in 25 s on GPU Worker 2"),
            ({"seconds": 25, "worker": "GPU Worker 2 (Intel UHD 770)", "cpu_rerun": True},
             "X: done in 25 s on GPU Worker 2, rerun on the CPU"),
            ({"seconds": 3, "worker": "CPU Worker 1", "failed": True}, "X: failed after 3 s on CPU Worker 1"),
            ({"seconds": 3, "failed": True}, "X: failed after 3 s"),
            ({"seconds": None}, "X: done, no worker needed"),
        ],
        ids=["checking-thread", "worker", "cpu-rerun", "failed-on-worker", "failed-on-checking-thread", "no-time"],
    )  # fmt: skip
    def test_done_line(self, kwargs, expected):
        seconds = kwargs.pop("seconds")
        assert done_line("X", seconds, **kwargs) == expected

    @pytest.mark.parametrize(
        ("types", "is_episode", "expected"),
        [
            (BOTH, True, "checking intro and credits"),
            (frozenset({T.INTRO, T.CREDITS, T.RECAP}), True, "checking intro, credits and recap"),
            (frozenset({T.CREDITS}), False, "checking credits (films get credits only)"),
            (frozenset(), False, "nothing to check (detection of every type it could have is off)"),
        ],
        ids=["episode", "three-types", "film", "nothing"],
    )
    def test_pickup_line(self, types, is_episode, expected):
        assert pickup_line("CPU Worker 1", "X", types, is_episode=is_episode) == f"CPU Worker 1 picked up X: {expected}"

    def test_a_worker_that_finds_no_checking_stage_notes_names_the_file_from_its_path(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        pipeline.log_pickup(ProcessableItem(canonical_path=media, server_id="plex-1"), "CPU Worker 3", ctx=ctx)
        assert _messages(job_log) == [f"CPU Worker 3 picked up {EPISODE}: checking intro and credits"]


class TestStartLine:
    @pytest.mark.parametrize(
        ("cfg", "trigger"),
        [
            ({"source": "radarr", "follows_job_id": "c7ca6327-aaaa"}, "follow-up to preview job c7ca6327 (Radarr import)"),
            ({"source": "sonarr", "follows_job_id": "d1e2f3a4-bbbb"}, "follow-up to preview job d1e2f3a4 (Sonarr import)"),
            ({"source": "emby", "follows_job_id": "d1e2f3a4-bbbb"}, "follow-up to preview job d1e2f3a4 (Emby webhook)"),
            ({"source": "recently_added", "follows_job_id": "d1e2f3a4-bbbb"},
             "follow-up to preview job d1e2f3a4 (Recently Added scan)"),
            ({"source": "season", "file_paths": ["/tv/Accused/Season 02/Accused - S02E01.mkv",
                                                 "/tv/Accused/Season 02/Accused - S02E03.mkv"]},
             "Season job for Accused S02"),
            ({"source": "season", "file_paths": ["/tv/A/Season 01/A - S01E01.mkv", "/tv/B/Season 02/B - S02E01.mkv"]},
             "Season job for 2 seasons"),
            ({"source": "version_rerun", "version_rerun": True}, "re-checking files after an update"),
            ({"source": "decide_again", "decide_again": True}, "deciding files again after the update"),
            ({"source": "online_recheck", "online_recheck": True}, "weekly online re-check"),
            ({"source": "theintrodb_recheck"}, "checking files again after TheIntroDB's daily limit reset"),
            ({"source": "reconcile", "reconcile": True}, "Check servers"),
            ({"source": "inspector", "force": True}, "manual Re-detect"),
            ({"source": "inspector", "force": False}, "publishing your saved markers"),
            ({"source": "inspector_season"}, "Publish from the Season view"),
            ({"source": "manual"}, "manual run"),
            ({"source": "sonarr", "retry_attempt": 2, "max_retries": 3, "parent_job_id": "6742472e-cccc"},
             "retry 2 of 3 for job 6742472e"),
            ({"source": "sonarr", "verify": True}, "checking again files published just after they were replaced"),
            ({"source": "schedule"}, "scheduled"),
        ],
        ids=["radarr", "sonarr", "vendor-webhook", "recently-added", "season", "seasons", "version-rerun",
             "decide-again", "online-recheck", "budget-recheck", "check-servers", "redetect", "publish-retry",
             "season-view", "manual", "retry", "verify", "schedule-gone"],
    )  # fmt: skip
    def test_each_trigger_in_plain_words(self, cfg, trigger):
        job = SimpleNamespace(parent_schedule_id="")
        assert start_line("0123456789", 3, job_runner.trigger_words(job, cfg)) == (
            f"Intro & Credits job 01234567 started: 3 files, {trigger}"
        )

    def test_a_scheduled_job_names_its_schedule(self, monkeypatch):
        from media_preview_generator.web import scheduler

        monkeypatch.setattr(scheduler, "schedule_name", lambda schedule_id: {"s-1": "Nightly"}.get(schedule_id))
        job = SimpleNamespace(parent_schedule_id="s-1")
        assert job_runner.trigger_words(job, {"source": "schedule"}) == 'scheduled "Nightly"'
        assert start_line("0123456789", 1, 'scheduled "Nightly"').endswith('started: 1 file, scheduled "Nightly"')


class TestSeasonJob:
    def test_unchanged_episodes_share_one_line_and_a_changed_one_gets_its_block(self, store, tmp_path, job_log):
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

        # Credit text decides every episode's credits alone; TheIntroDB has no entry for any of them.
        first = _job(store, paths[0], reg)
        for path in paths:
            assert run(first, path).outcome_key == FileOutcome.PUBLISHED.value
        os.utime(paths[1], ns=(5, 5))  # E02 was replaced by a file with an intro chapter
        job_log.clear()

        season = _job(store, paths[0], reg)
        season.season_recheck = True
        outcomes = [run(season, path, INTRO_CHAPTERS if path == paths[1] else ()).outcome_key for path in paths]

        # The first job's 4 "no entry" answers paused TheIntroDB for the show, so the replaced E02 isn't asked again.
        paused_until = store.series_lookups_paused_until(
            Source.THEINTRODB, "tvdb:275274", pause=pipeline.SERIES_NO_ENTRY_PAUSE
        )
        assert _messages(job_log) == [
            "Rick and Morty (2013) S01E02: checking intro and credits",
            '  Chapters: "Intro" chapter at 2:06–2:37',
            f"  TheIntroDB: skipped (no data for this show; asked again after {paused_until:%Y-%m-%d})",
            "  Credit text: credits start at 21:31 (read on the CPU in 0 s)",
            "  Plex's own markers: not read",
            '  Decided: intro 2:06–2:37, from the "Intro" chapter · credits 21:31–22:01 (credit text)',
            "  Sent to Plex: intro 2:06–2:37 · credits 21:31–22:01",
            "Rick and Morty (2013) S01E02: done in 0 s, no worker needed",
        ]
        counts = {key: outcomes.count(key) for key in set(outcomes)}
        assert counts == {FileOutcome.UP_TO_DATE.value: 3, FileOutcome.PUBLISHED.value: 1}
        assert season.summary_lines(counts) == [
            "Season re-check, Rick and Morty (2013) S01 (4 episodes): E02 changed (logged above); no change for "
            "E01/E03/E04",
            "Done: 4 files · 1 sent to Plex · 0 need review · 0 nothing found · 3 already up to date",
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


class TestDecideAgainJob:
    """The one job after settings v16 decides files again, reusing answers that aren't due: like a Season job, it logs
    the files whose decisions changed and one line for the rest."""

    def test_changed_files_get_their_lines_and_the_rest_one_line(self, store, media, job_log, monkeypatch):
        monkeypatch.setattr(pipeline, "APP_PUBLISH_WHEN", "high")  # how an older build left credit text alone
        out, _ = _run(_job(store, media, _plex(media)), media, {"plex-1": ready_publisher()}, probe=_probe())
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        monkeypatch.undo()
        job_log.clear()

        again = _job(store, media, _plex(media))
        again.decide_again = True
        out, _ = _run(again, media, {"plex-1": ready_publisher()})
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert _messages(job_log) == [
            HEAD,
            "  Chapters: none (saved earlier)",
            "  TheIntroDB: no entry (saved earlier)",
            "  Credit text: credits start at 21:31 (saved earlier)",
            "  Plex's own markers: none (saved earlier)",
            "  Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            "  Sent to Plex: credits 21:31–22:01",
            DONE,
        ]
        assert again.summary_lines({FileOutcome.PUBLISHED.value: 1}) == [
            "Decided again after the update (1 file): 1 changed (logged above)",
            "Done: 1 file · 1 sent to Plex · 0 need review · 0 nothing found",
        ]

        job_log.clear()
        quiet = _job(store, media, _plex(media))
        quiet.decide_again = True
        _run(quiet, media, {"plex-1": ready_publisher()})
        assert job_log == []
        assert quiet.summary_lines({FileOutcome.UP_TO_DATE.value: 1})[0] == (
            "Decided again after the update (1 file): 1 unchanged"
        )

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            (
                [(True, False), (False, True), (False, False)],
                "3 files): 1 changed (logged above); 2 unchanged, 1 still needs review",
            ),
            ([(False, True), (False, True)], "2 files): 2 unchanged, still need review"),
            ([(True, True)], "1 file): 1 changed (logged above)"),
            ([], "0 files): nothing to decide"),
        ],
        ids=["mixed", "all-still-in-review", "only-changed", "none"],
    )
    def test_decide_again_line_wording(self, files, expected):
        assert decide_again_line(files) == f"Decided again after the update ({expected}"


class TestOnlineRecheckJob:
    """The weekly online re-check asks only the due online lookups, reads nothing it doesn't have to, logs the files an
    online database now has (or whose decisions changed) and one line for them all."""

    @staticmethod
    def _first_run(store, media):
        out, _ = _run(_job(store, media, _plex(media), found=()), media, {"plex-1": ready_publisher()}, probe=_probe())
        assert out.outcome_key == FileOutcome.NO_MARKERS.value  # TheIntroDB: no entry; credit text: none found

    @staticmethod
    def _weekly(store, media, *, theintrodb, days_later):
        later = datetime.now(UTC) + timedelta(days=days_later)
        ctx = _job(store, media, _plex(media), theintrodb=theintrodb, found=(), now=lambda: later)
        ctx.online_recheck = True
        return ctx

    def test_a_database_that_now_has_the_file_is_logged_and_counted(self, store, media, job_log):
        self._first_run(store, media)
        job_log.clear()

        weekly = self._weekly(store, media, theintrodb=TIDB, days_later=15)
        out, _ = _run(weekly, media, {"plex-1": ready_publisher()})

        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert len(weekly.clients["theintrodb"].calls) == 1
        # Its credit text answer isn't due: the file isn't read again.
        weekly.local_detectors[0].detect.assert_not_called()
        assert _messages(job_log) == [
            HEAD,
            "  Chapters: none (saved earlier)",
            "  TheIntroDB: credits 21:36–22:00",
            "  Credit text: none found (saved earlier)",
            # A server's "none" a day old is read again while a type is undecided, as on any run.
            "  Plex's own markers: none",
            f"  Decided: intro nothing found · credits 21:36–22:00 from TheIntroDB {ONLINE_ONLY}",
            "  Sent to Plex: nothing to send",
            DONE,
        ]
        assert weekly.summary_lines({FileOutcome.NEEDS_REVIEW.value: 1}) == [
            "Weekly online re-check (1 file): 1 newly found online, 0 unchanged",
            "Done: 1 file · 0 sent to Plex · 1 needs review · 0 nothing found",
        ]

    def test_a_file_still_not_found_logs_only_the_summary_line(self, store, media, job_log):
        self._first_run(store, media)
        job_log.clear()

        weekly = self._weekly(store, media, theintrodb=NO_DATA, days_later=15)
        _run(weekly, media, {"plex-1": ready_publisher()})

        assert len(weekly.clients["theintrodb"].calls) == 1  # due, so asked
        assert job_log == []
        assert weekly.summary_lines({FileOutcome.NO_MARKERS.value: 1})[0] == (
            "Weekly online re-check (1 file): 0 newly found online, 1 unchanged"
        )

    def test_a_no_entry_that_isnt_due_is_not_asked(self, store, media, job_log):
        self._first_run(store, media)
        job_log.clear()

        weekly = self._weekly(store, media, theintrodb=TIDB, days_later=13)
        _run(weekly, media, {"plex-1": ready_publisher()})

        assert weekly.clients["theintrodb"].calls == []
        assert job_log == []
        assert weekly.summary_lines({FileOutcome.NO_MARKERS.value: 1})[0] == (
            "Weekly online re-check (1 file): 0 newly found online, 1 unchanged"
        )

    def test_any_other_job_has_no_such_line(self, store, media):
        ctx = _job(store, media, _plex(media), found=())
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())
        assert not any(line.startswith("Weekly") for line in ctx.summary_lines({FileOutcome.NO_MARKERS.value: 1}))

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            ([(True, True), (False, False), (False, False)], "3 files): 1 newly found online, 2 unchanged"),
            ([(True, False)], "1 file): 1 newly found online, 0 unchanged"),
            ([(False, True), (True, True), (False, False)], "3 files): 1 newly found online, 1 changed otherwise, 1 "
             "unchanged"),
            ([], "0 files): 0 newly found online, 0 unchanged"),
        ],
        ids=["found-and-unchanged", "found-without-a-change", "changed-otherwise", "none"],
    )  # fmt: skip
    def test_online_recheck_line_wording(self, files, expected):
        assert online_recheck_line(files) == f"Weekly online re-check ({expected}"


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

    @pytest.mark.parametrize(
        ("ms", "expected"), [(0, "0:00"), (59_999, "0:59"), (2_856_000, "47:36"), (3_725_000, "1:02:05")]
    )
    def test_clock_uses_hours_from_an_hour_on(self, ms, expected):
        assert clock(ms) == expected
