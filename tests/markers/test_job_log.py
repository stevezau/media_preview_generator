"""Intro & Credits job logs: each line its own record, logged live as the file's work happens -- picked up, each
source checked, what was decided, then one line per server for what was sent, then done -- rather than one block
written after the file finishes. A worker doesn't repeat a source's line the checking thread already logged for it.
Every line after pickup carries its own title, so two files' (or workers') lines can interleave safely. One line per
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
from media_preview_generator.markers.decide import (
    AUDIO_OVER_CHAPTER_REASON,
    TEXT_MOVES_CHAPTER_REASON,
    TEXT_OVER_CHAPTER_REASON,
    DecisionStatus,
    TypeDecision,
    chapter_hint,
)
from media_preview_generator.markers.job_log import (
    ALREADY_DECIDED,
    EPISODE_ONLY_SOURCES,
    RunNotes,
    SeasonEpisode,
    ServerResult,
    clock,
    decide_again_line,
    display_name,
    done_line,
    duration,
    file_start_line,
    file_title,
    last_seasons_audio_line,
    make_source_view,
    nothing_was_sent,
    online_recheck_line,
    read_result_line,
    reading_line,
    season_line,
    server_result_line,
    server_source_line,
    source_line,
    start_line,
    totals_line,
    type_phrase,
    worker_completed_line,
    worker_device,
    write_lines,
)
from media_preview_generator.markers.models import (
    STALE_SERVER_MARKERS_DETAIL,
    Candidate,
    FileIdentity,
    Marker,
    MarkerType,
    MediaIds,
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
from tests.markers.test_pipeline import DUR, NO_DATA, TIDB_CREDITS, TIDB_INTRO, _clients, _ctx, _probe, _registry, _run

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
# TheIntroDB never decides alone (rule 6): its lone answer is the undecided case these lines are checked with.
ONLINE_ONLY_REASON = "only TheIntroDB has the credits; an online answer needs a check against the file"
ONLINE_ONLY = f"nothing found ({ONLINE_ONLY_REASON})"
FILES_PANEL_ONLINE_ONLY = f"credits: none ({ONLINE_ONLY_REASON})"
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
DONE = f"{EPISODE} · done in 0 s"


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
    """The job log's records the pipeline writes (``job_log.write_line``), as ``(level, message)``."""
    records: list[tuple[str, str]] = []
    handler = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="INFO",
        format="{message}",
        filter=lambda record: record["module"] == "job_log",
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
    def test_a_film_whose_credits_chapter_a_gpu_worker_checked(self, tmp_path, store, job_log):
        # Credit text reads a film a "Credits" chapter decided alone (spec §5.5 rule 3, 2026-09-27): here the chapter
        # sits 39 s into the roll, so credit text moves its start to the first card.
        folder = tmp_path / "media" / "movies" / "32 Frames A 9 11 Mystery (2026)"
        folder.mkdir(parents=True)
        path = folder / "32 Frames A 9 11 Mystery (2026) - [AMZN][WEBDL-1080p][EAC3 5.1][h264]-cinepth.mkv"
        path.write_bytes(b"x" * 100)
        movie_path = str(path)
        reg = _plex(movie_path)
        reg.get("plex-1").get_external_ids.return_value = dict(MOVIE_IDS)
        clock = FakeClock()
        skipdb = FakeClient(NO_DATA)

        def read_credit_text(*args, **kwargs):
            clock.advance(9)
            hint = chapter_hint(7_211_000, moves=True)
            return [Candidate(T.CREDITS, 7_172_000, None, Source.CREDITS_TEXT, origin=hint)]

        credit_text = LocalDetectorSpec(
            Source.CREDITS_TEXT, frozenset({T.CREDITS}), MagicMock(side_effect=read_credit_text), checks_chapters=True
        )
        ctx = _ctx(
            store,
            reg,
            settings_raw=MOVIE_SETTINGS,
            clients={"introdb": FakeClient(NO_DATA), "skipdb": skipdb, "theintrodb": FakeClient(NO_DATA)},
            detectors=(credit_text,),
        )
        ctx.monotonic = clock
        publisher = ready_publisher()
        publisher.write.side_effect = lambda *a, **k: (clock.advance(0.5), publisher.succeed(*a, **k))[1]
        chapters = (Chapter(0, 7_211_000, "Movie"), Chapter(7_211_000, MOVIE_DURATION, "Credits"))
        probe = _probe(chapters, duration=MOVIE_DURATION)

        handed_on, _ = _run(ctx, movie_path, {"plex-1": publisher}, probe=probe)
        assert handed_on is None
        worker = "GPU Worker 1 (NVIDIA GeForce RTX 3060)"
        pipeline.log_pickup(ProcessableItem(canonical_path=movie_path, server_id="plex-1"), worker, ctx=ctx)
        out, _ = _run(
            ctx, movie_path, {"plex-1": publisher}, probe=probe, stage="process", gpu="cuda", worker_name=worker,
            gpu_worker=True,
        )  # fmt: skip

        job = SimpleNamespace(parent_schedule_id="")
        cfg = {"source": "radarr", "follows_job_id": "c7ca6327-1111-2222-3333-444455556666"}
        first = start_line("6742472e-b9a6-470d-ad4d-64b9442bac6e", 1, job_runner.trigger_words(job, cfg))
        log = [first, *_messages(job_log), *ctx.summary_lines({out.outcome_key: 1})]
        assert log == [
            "Intro & Credits job 6742472e started: 1 file, follow-up to preview job c7ca6327 (Radarr import)",
            "32 Frames: A 9/11 Mystery (2026): checking credits (films get credits only)",
            '32 Frames: A 9/11 Mystery (2026) · Checking chapters… "Credits" chapter at 2:00:11–2:03:39 (asked now)',
            "32 Frames: A 9/11 Mystery (2026) · Checking SkipDB… no entry (asked now)",
            "GPU Worker 1 (NVIDIA GeForce RTX 3060) picked up: 32 Frames: A 9/11 Mystery (2026), checking credits "
            "(films get credits only)",
            "32 Frames: A 9/11 Mystery (2026) · Reading credit text on the GPU (NVIDIA GeForce RTX 3060)…",
            '32 Frames: A 9/11 Mystery (2026) · Credit text: credits start at 1:59:32 (moves the "Credits" chapter '
            "at 2:00:11 to the first credit card; 9 s)",
            "32 Frames: A 9/11 Mystery (2026) · Checking Plex's own markers… none (asked now)",
            '32 Frames: A 9/11 Mystery (2026) · Decided: credits 1:59:32–2:03:39 (the "Credits" chapter, moved to '
            "the first credit card by credit text)",
            "32 Frames: A 9/11 Mystery (2026) · [Plex] Added credits 1:59:32–2:03:39",
            "GPU Worker 1 (NVIDIA GeForce RTX 3060) completed: 32 Frames: A 9/11 Mystery (2026) (success, 9.5 s)",
            "Done: 1 file · 1 sent to Plex · 0 nothing found",
        ]
        assert {level for level, _ in job_log} == {"INFO"}
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert len(skipdb.calls) == 1
        credit_text.detect.assert_called_once()

    def test_a_tv_episode_a_gpu_worker_scanned(self, tmp_path, store, job_log):
        episode = _accused(tmp_path)
        clock = FakeClock()
        ctx = _accused_job(store, _plex(episode), clock)
        publisher = ready_publisher()
        publisher.write.side_effect = lambda *a, **k: (clock.advance(12), publisher.succeed(*a, **k))[1]
        probe = _probe(duration=ACCUSED_DURATION)

        handed_on, _ = _run(ctx, episode, {"plex-1": publisher}, probe=probe)
        assert handed_on is None
        worker = "GPU Worker 2 (Intel UHD 770)"
        pipeline.log_pickup(ProcessableItem(canonical_path=episode, server_id="plex-1"), worker, ctx=ctx)
        out, _ = _run(
            ctx, episode, {"plex-1": publisher}, probe=probe, stage="process", gpu="intel", worker_name=worker,
            gpu_worker=True,
        )  # fmt: skip

        assert _messages(job_log) == [
            "Accused S04E05: checking intro and credits",
            "Accused S04E05 · Checking chapters… none (asked now)",
            "Accused S04E05 · Checking IntroDB… intro 0:41–1:12 (asked now)",
            "Accused S04E05 · Reading season audio on the CPU…",
            "Accused S04E05 · Season audio: intro 0:41–1:12 (same theme found in 9 of 10 episodes; 0 s)",
            "GPU Worker 2 (Intel UHD 770) picked up: Accused S04E05, checking intro and credits",
            "Accused S04E05 · Reading credit text on the GPU (Intel UHD 770)…",
            "Accused S04E05 · Credit text: credits start at 41:48 (13 s)",
            "Accused S04E05 · Checking Plex's own markers… none (asked now)",
            "Accused S04E05 · Decided: intro 0:41–1:12 (IntroDB and season audio agree) · credits 41:48–43:10 "
            "(credit text)",
            "Accused S04E05 · [Plex] Added intro 0:41–1:12 and credits 41:48–43:10",
            "GPU Worker 2 (Intel UHD 770) completed: Accused S04E05 (success, 25 s)",
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value


class TestFileBlocks:
    """One cell per kind of result, run through the pipeline; every line is asserted as the job log shows it."""

    def test_an_online_answer_alone_is_nothing_found_with_its_reason_and_nothing_is_sent(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=TIDB, found=())
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (asked now)",
            f"{EPISODE} · Checking TheIntroDB… credits 21:36–22:00 (asked now)",
            f"{EPISODE} · Reading credit text on the CPU…",
            f"{EPISODE} · Credit text: none found (0 s)",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits {ONLINE_ONLY}",
            f"{EPISODE} · [Plex] Nothing to send",
            f"{DONE} (nothing new to send)",
        ]
        # An undecided type keeps the block at INFO.
        assert {level for level, _ in job_log} == {"INFO"}
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        # The Files panel says why nothing was decided; the closest answer is kept as the proposal.
        assert out.message == f"intro: none; {FILES_PANEL_ONLINE_ONLY}"
        credits = store.get_decisions(store.get_file(media).id)[T.CREDITS]
        assert (credits.status, credits.reason) == (DecisionStatus.NO_EVIDENCE, ONLINE_ONLY_REASON)
        assert (credits.proposed_start_ms, credits.proposed_end_ms, credits.decided_by) == (
            TIDB_CREDITS.start_ms,
            TIDB_CREDITS.end_ms,
            ("theintrodb",),
        )

    def test_credit_text_alone_is_sent(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (asked now)",
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            f"{EPISODE} · Reading credit text on the CPU…",
            f"{EPISODE} · Credit text: credits start at 21:31 (0 s)",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            f"{EPISODE} · [Plex] Added credits 21:31–22:01",
            DONE,
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_two_sources_that_agree_publish_and_both_are_named(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=TIDB)
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log)[6:8] == [
            f"{EPISODE} · Decided: intro nothing found · credits 21:36–22:00 (TheIntroDB and credit text agree)",
            f"{EPISODE} · [Plex] Added credits 21:36–22:00",
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_type_every_server_keeps_its_own_of_is_named_and_not_read(self, store, media, job_log):
        ctx = _job(store, media, _plex(media, keeps_own_credits=True))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(INTRO_CHAPTERS))

        assert _messages(job_log) == [
            HEAD,
            f'{EPISODE} · Checking chapters… "Intro" chapter at 2:06–2:37 (asked now)',
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            f"{EPISODE} · Checking credit text… not read (every server keeps its own credits)",
            f"{EPISODE} · Checking Plex's own markers… credits start at 21:30 (asked now)",
            f'{EPISODE} · Decided: intro 2:06–2:37, from the "Intro" chapter · credits kept Plex\'s own',
            f"{EPISODE} · [Plex] Added intro 2:06–2:37; kept Plex's own credits",
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

        assert (
            f"{EPISODE} · Checking Plex's own markers… credits start at 21:30 (made for an earlier file; asked now)"
            in _messages(job_log)
        )
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
            f'{EPISODE} · Checking chapters… "Intro" chapter at 2:06–2:37, "Credits" chapter from 21:35 (asked now)',
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            f"{EPISODE} · Checking credit text… not read (a chapter named Credits is used as-is)",
            f"{EPISODE} · Checking Plex's own markers… credits start at 21:30 (asked now)",
            f'{EPISODE} · Decided: intro 2:06–2:37, from the "Intro" chapter · credits 21:35–22:01, from the '
            '"Credits" chapter',
            f"{EPISODE} · [Plex] Added intro 2:06–2:37; kept Plex's own credits instead of ours (\"Keep Plex's\")",
            DONE,
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_server_that_hasnt_indexed_the_file_is_waited_for_even_with_a_type_undecided(self, store, media, job_log):
        ctx = _job(store, media, _plex(media, indexed=False), theintrodb=TIDB, found=())
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(INTRO_CHAPTERS))

        assert _messages(job_log) == [
            HEAD,
            f'{EPISODE} · Checking chapters… "Intro" chapter at 2:06–2:37 (asked now)',
            f"{EPISODE} · Checking TheIntroDB… credits 21:36–22:00 (asked now)",
            f"{EPISODE} · Reading credit text on the CPU…",
            f"{EPISODE} · Credit text: none found (0 s)",
            f"{EPISODE} · Checking Plex's own markers… not read (not in this server's library yet)",
            f'{EPISODE} · Decided: intro 2:06–2:37, from the "Intro" chapter · credits {ONLINE_ONLY}',
            f"{EPISODE} · [Plex] Not in Plex's library yet (waiting for it to add the file)",
            DONE,
        ]
        row = out.publisher_rows[0]
        assert (row["status"], row["reason_code"]) == (ServerStatus.WAITING.value, NOT_IN_LIBRARY)
        # The job retries the file, so it counts as waiting, not as nothing found.
        assert out.outcome_key == FileOutcome.WAITING.value

    def test_an_online_source_out_of_its_daily_budget_is_named_as_skipped_and_counted(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=BUDGET)
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert f"{EPISODE} · Checking TheIntroDB… skipped (daily limit reached, resets 00:00 UTC)" in _messages(job_log)
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert ctx.summary_lines({FileOutcome.PUBLISHED.value: 1}) == [
            "Done: 1 file · 1 sent to Plex · 0 nothing found · TheIntroDB skipped for 1 file"
        ]

    def test_a_file_whose_answer_didnt_change_gets_a_full_block_too(self, store, media, job_log):
        reg = _plex(media)
        for expected in (FileOutcome.PUBLISHED, FileOutcome.UP_TO_DATE):
            ctx = _job(store, media, reg, now=lambda: datetime.now(UTC))
            out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())
            assert out.outcome_key == expected.value

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (asked now)",
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            f"{EPISODE} · Reading credit text on the CPU…",
            f"{EPISODE} · Credit text: credits start at 21:31 (0 s)",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            f"{EPISODE} · [Plex] Added credits 21:31–22:01",
            DONE,
            HEAD,
            f"{EPISODE} · Checking chapters… none (saved {today})",
            f"{EPISODE} · Checking TheIntroDB… no entry (saved {today})",
            f"{EPISODE} · Checking credit text… credits start at 21:31 (saved {today})",
            f"{EPISODE} · Checking Plex's own markers… none (saved {today})",
            f"{EPISODE} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            f"{EPISODE} · [Plex] Already up to date (credits 21:31–22:01)",
            f"{DONE} (nothing new to send)",
        ]

    def test_nothing_found_twice_still_logs_a_full_block_the_second_time(self, store, media, job_log):
        reg = _plex(media)
        for _ in range(2):
            ctx = _job(store, media, reg, found=(), now=lambda: datetime.now(UTC))
            out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())
            assert out.outcome_key == FileOutcome.NO_MARKERS.value

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (asked now)",
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            f"{EPISODE} · Reading credit text on the CPU…",
            f"{EPISODE} · Credit text: none found (0 s)",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits nothing found",
            f"{EPISODE} · [Plex] Nothing to send",
            f"{DONE} (nothing new to send)",
            HEAD,
            f"{EPISODE} · Checking chapters… none (saved {today})",
            f"{EPISODE} · Checking TheIntroDB… no entry (saved {today})",
            f"{EPISODE} · Checking credit text… none found (saved {today})",
            f"{EPISODE} · Checking Plex's own markers… none (saved {today})",
            f"{EPISODE} · Decided: intro nothing found · credits nothing found",
            f"{EPISODE} · [Plex] Nothing to send",
            f"{DONE} (nothing new to send)",
        ]

    def test_a_movie_without_a_server_title_is_named_by_its_file(self, store, movie, job_log):
        chapters = (Chapter(0, 1_295_324, "Movie"), Chapter(1_295_324, None, "Credits"))
        ctx = _job(store, movie, _plex(movie))
        out, _ = _run(ctx, movie, {"plex-1": ready_publisher()}, probe=_probe(chapters))

        assert _messages(job_log) == [
            "Heat (1995): checking credits (films get credits only)",
            'Heat (1995) · Checking chapters… "Credits" chapter from 21:35 (asked now)',
            "Heat (1995) · Checking TheIntroDB… not asked (no server confirmed whether it's a movie or an episode)",
            "Heat (1995) · Checking credit text… not read (a chapter named Credits is used as-is)",
            "Heat (1995) · Checking Plex's own markers… none (asked now)",
            'Heat (1995) · Decided: credits 21:35–22:01, from the "Credits" chapter',
            "Heat (1995) · [Plex] Added credits 21:35–22:01",
            "Heat (1995) · done in 0 s",
        ]
        assert out.outcome_key == FileOutcome.PUBLISHED.value

    def test_a_source_without_an_answer_this_time_says_why(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), theintrodb=LookupResult("unavailable", detail="TheIntroDB HTTP 503"))
        ctx.local_detectors[0].detect.side_effect = DetectorUnavailableError("the decode timed out")
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert _messages(job_log)[1:6] == [
            f"{EPISODE} · Checking chapters… none (asked now)",
            f"{EPISODE} · Checking TheIntroDB… unavailable (HTTP 503)",
            f"{EPISODE} · Reading credit text on the CPU…",
            f"{EPISODE} · Credit text: no answer this time (the decode timed out)",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
        ]

    def test_a_worker_picking_up_where_the_checking_stage_left_off_doesnt_repeat_its_lines(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), on_worker=True)
        publishers = {"plex-1": ready_publisher()}
        handed_on, _ = _run(ctx, media, publishers, probe=_probe())
        assert handed_on is None
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} \u00b7 Checking chapters\u2026 none (asked now)",
            f"{EPISODE} \u00b7 Checking TheIntroDB\u2026 no entry (asked now)",
        ]

        pipeline.log_pickup(ProcessableItem(canonical_path=media, server_id="plex-1"), "CPU Worker 1", ctx=ctx)
        _run(ctx, media, publishers, probe=_probe(), stage="process", worker_name="CPU Worker 1")

        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (asked now)",
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            "CPU Worker 1 picked up: Rick and Morty (2013) S01E01, checking intro and credits",
            f"{EPISODE} · Reading credit text on the CPU…",
            f"{EPISODE} · Credit text: credits start at 21:31 (0 s)",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            f"{EPISODE} · [Plex] Added credits 21:31–22:01",
            "CPU Worker 1 completed: Rick and Morty (2013) S01E01 (success, 0 s)",
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

        assert f"{EPISODE} · Credit text: credits start at 21:31 (7 s)" in _messages(job_log)
        assert _messages(job_log)[-1] == "CPU Worker 1 completed: Rick and Morty (2013) S01E01 (success, 7 s)"

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
        # The GPU try's own steps are already live-logged: a later CPU rerun doesn't erase them.
        assert _messages(job_log) == [
            f"{EPISODE} · Checking chapters… none (asked now)",
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            f"{EPISODE} · Reading credit text on the GPU (NVIDIA TITAN RTX)…",
        ]
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu=None, gpu_worker=True,
             worker_name=worker)  # fmt: skip

        assert f"{EPISODE} · Credit text: credits start at 21:31 (6 s)" in _messages(job_log)
        assert _messages(job_log)[-1] == f"{worker} completed: {EPISODE} (success, rerun on the CPU, 10 s)"

    FFMPEG_SAID = (
        "[hevc @ 0x1] Failed to sync surface 0x5 (operation failed).",
        "[vist#0:0/hevc @ 0x2] Decoding error: Input/output error",
    )

    def test_a_gpu_read_that_fails_logs_ffmpegs_own_lines_once_before_the_cpu_rerun(self, store, media, job_log):
        # Production: 54 of 718 credit-text reads on the Intel GPU fell back to the CPU, and why 53 of them did was
        # unknown: ffmpeg's lines were logged at DEBUG only.
        from media_preview_generator.markers.credits import frames
        from media_preview_generator.processing.generator import CodecNotSupportedError

        ctx = _job(store, media, _plex(media), on_worker=True)
        reason = "the GPU's decoder hit a hardware or driver error (ffmpeg exited 251)"

        def detect(*args, gpu=None, **kwargs):
            if not gpu:
                return [TEXT_CREDITS]
            try:
                raise frames.GpuDecodeError(reason, stderr_tail=self.FFMPEG_SAID)
            except frames.GpuDecodeError as exc:  # as ``detector.detect_credits_text`` raises it
                raise CodecNotSupportedError(str(exc)) from exc

        ctx.local_detectors[0].detect.side_effect = detect
        worker = "GPU Worker 2 (Intel UHD 770)"
        with pytest.raises(CodecNotSupportedError):
            _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu="intel",
                 gpu_worker=True, worker_name=worker)  # fmt: skip
        warnings = [message for level, message in job_log if level == "WARNING"]
        assert warnings == [
            f"{EPISODE} · Credit text: couldn't be read on the GPU ({reason}). FFmpeg's last lines: "
            f"{self.FFMPEG_SAID[0]} | {self.FFMPEG_SAID[1]}"
        ]
        assert _messages(job_log)[-2:] == [f"{EPISODE} · Reading credit text on the GPU (Intel UHD 770)…", warnings[0]]

        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu=None, gpu_worker=True,
             worker_name=worker)  # fmt: skip
        assert [message for level, message in job_log if level == "WARNING"] == warnings  # the CPU rerun adds none
        assert f"{EPISODE} · Credit text: credits start at 21:31 (0 s)" in _messages(job_log)

    def test_a_gpu_read_that_works_logs_no_warning(self, store, media, job_log):
        ctx = _job(store, media, _plex(media), on_worker=True)
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), stage="process", gpu="intel", gpu_worker=True,
             worker_name="GPU Worker 2 (Intel UHD 770)")  # fmt: skip
        assert f"{EPISODE} · Credit text: credits start at 21:31 (0 s)" in _messages(job_log)
        assert [message for level, message in job_log if level == "WARNING"] == []

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

        assert f"{EPISODE} · Credit text: {cut_short}" in _messages(job_log)

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
            f"{EPISODE} · Credit text: credits start at 21:31 (read on the CPU after the GPU read nothing (20 s "
            "in all))"
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
            f"{EPISODE} · Credit text: credits start at 21:31 (20 s; credit text detection on the CPU: the GPU "
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
        assert messages[-2].startswith(f"{EPISODE} · [Plex] Failed (")
        assert messages[-1] == f"{EPISODE} · failed after 0 s"
        # Every step logs live, at the level it's known at; only the final line waits for the file's outcome.
        assert [level for level, _ in job_log][-1] == "WARNING"

    def test_a_file_that_couldnt_be_read_logs_why_at_warning(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe_effect=ProbeError("ffprobe exited 1"))

        assert out.outcome_key == FileOutcome.FAILED.value
        assert job_log == [
            ("INFO", HEAD),
            ("WARNING", f"{EPISODE} · Failed: Couldn't read the file: ffprobe exited 1"),
            ("WARNING", f"{EPISODE} · failed after 0 s"),
        ]

    def test_a_cancelled_file_logs_nothing(self, store, media, job_log):
        ctx = _job(store, media, _plex(media))
        _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(), cancel_check=lambda: True)
        assert job_log == []

    def test_a_bug_describing_one_server_doesnt_stop_the_next_owners_write(self, store, media, job_log):
        # A raising line builder (an unexpected shape in a step's own data) mustn't fail the file or hold up
        # another owner's write: each owner's own ``_publish_to`` already ran and stored its row before its line
        # is even attempted.
        reg = _registry(media, ServerType.PLEX, ServerType.EMBY)
        for sid in ("plex-1", "emby-1"):
            reg.get(sid).resolve_remote_path_to_item_id.return_value = f"item-{sid}"
        ctx = _job(store, media, reg)
        publishers = {"plex-1": ready_publisher(), "emby-1": ready_publisher()}
        with patch.object(pipeline, "server_result_line", side_effect=RuntimeError("boom")):
            out, _ = _run(ctx, media, publishers, probe=_probe())

        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert publishers["plex-1"].write.call_count == 1
        assert publishers["emby-1"].write.call_count == 1
        # The bug is swallowed at debug, not raised into the file's own outcome; no line for either server logs.
        assert not any("[Plex]" in m or "[Emby]" in m for m in _messages(job_log))

    def test_a_raising_source_view_doesnt_change_the_files_outcome(self, store, media, job_log):
        # ``make_source_view`` runs inside the guarded lambda (``_log_source``), so a bug building it is swallowed
        # the same way a bug in the line itself would be -- it can't fail the file or stop its markers being sent.
        ctx = _job(store, media, _plex(media))
        publisher = ready_publisher()
        with patch.object(pipeline, "make_source_view", side_effect=RuntimeError("boom")):
            out, _ = _run(ctx, media, {"plex-1": publisher}, probe=_probe())

        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert publisher.write.call_count == 1
        # Every source line that would have gone through ``make_source_view`` is swallowed, but the file's own
        # decision and Sent lines (which don't build a source view) still log.
        messages = _messages(job_log)
        assert any(m.startswith(f"{EPISODE} · Decided:") for m in messages)
        assert any("[Plex]" in m for m in messages)

    def test_a_waiting_rows_carried_over_markers_are_still_our_last_sent(self, store, media, job_log):
        # A row's ``markers`` field keeps whatever an older WRITTEN attempt stored even once a later attempt only
        # got as far as WAITING (``_publish_to``'s own ``_finish``: "rows that don't know the item keep the last
        # one"). The server never learned of the wait: what it has is still that older write.
        reg = _plex(media)
        ctx = _job(store, media, reg)
        st = os.stat(media)
        rec = ctx.store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        last_sent = (Marker(T.CREDITS, 999_000, 1_000_000, ()),)
        ctx.store.set_publish_state(rec.id, "plex-1", item_id="item-plex-1", markers=list(last_sent), status="written")
        ctx.store.set_publish_state(rec.id, "plex-1", item_id="item-plex-1", markers=None, status="waiting")

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert out.outcome_key == FileOutcome.PUBLISHED.value
        sent = next(m for m in _messages(job_log) if "[Plex]" in m)
        assert sent == "Rick and Morty (2013) S01E01 · [Plex] Replaced credits 16:39–16:40 → 21:31–22:01"

    def test_a_failed_rows_last_written_set_is_still_our_last_sent(self, store, media, job_log):
        # Reproduces the review's own case: credits written, the row then set to failed (a busy database, say) --
        # the server was never told to remove them, so the next write must say what changed, not "added".
        reg = _plex(media)
        ctx = _job(store, media, reg)
        st = os.stat(media)
        rec = ctx.store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        last_sent = (Marker(T.CREDITS, 999_000, 1_000_000, ()),)
        ctx.store.set_publish_state(rec.id, "plex-1", item_id="item-plex-1", markers=list(last_sent), status="written")
        ctx.store.set_publish_state(rec.id, "plex-1", item_id="item-plex-1", markers=None, status="failed")

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert out.outcome_key == FileOutcome.PUBLISHED.value
        sent = next(m for m in _messages(job_log) if "[Plex]" in m)
        assert sent == "Rick and Morty (2013) S01E01 · [Plex] Replaced credits 16:39–16:40 → 21:31–22:01"

    def test_a_row_moved_to_a_different_item_treats_its_markers_as_unread(self, store, media, job_log):
        # A merge or split can move the file to a different item on the same server: the old row's markers were
        # never sent to *this* item, so they aren't "our last sent" for it.
        reg = _plex(media)
        ctx = _job(store, media, reg)
        st = os.stat(media)
        rec = ctx.store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        old = (Marker(T.CREDITS, 999_000, 1_000_000, ()),)
        ctx.store.set_publish_state(rec.id, "plex-1", item_id="item-old", markers=list(old), status="written")
        reg.get("plex-1").resolve_remote_path_to_item_id.return_value = "item-new"

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        assert out.outcome_key == FileOutcome.PUBLISHED.value
        sent = next(m for m in _messages(job_log) if "[Plex]" in m)
        assert (
            sent == "Rick and Morty (2013) S01E01 · [Plex] Sent credits 21:31–22:01 (what Plex had before wasn't read)"
        )


class TestConcurrentWorkers:
    def test_two_workers_finishing_at_once_interleave_but_every_line_still_carries_its_own_title(self, store, tmp_path):
        paths = [_accused(tmp_path, episode) for episode in (1, 2)]
        titles = ["Accused S04E01", "Accused S04E02"]
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
        # No block, no lock: both files' steps interleave line by line, one worker's record between the other's.
        assert any(titles[0] in a and titles[1] in b for a, b in zip(records, records[1:], strict=False))
        for n, title in enumerate(titles, 1):
            own = [line for line in records if title in line]
            assert own == [
                f"{title} · Checking chapters… none (asked now)",
                f"{title} · Checking TheIntroDB… no entry (asked now)",
                f"{title} · Reading credit text on the CPU…",
                f"{title} · Credit text: credits start at 21:31 (0 s)",
                f"{title} · Checking Plex's own markers… none (asked now)",
                f"{title} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
                f"{title} · [Plex] Added credits 21:31–22:01",
                f"CPU Worker {n} completed: {title} (success, 0 s)",
            ]

    def test_write_lines_logs_each_line_as_its_own_record_in_order(self):
        # No lock, no block: write_lines is just write_line per line, so two callers' lines can interleave -- each
        # caller's own lines still come out in the order it gave them.
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

        assert len(records) == 40
        for tag in ("a", "b"):
            own = [line for line in records if line.startswith(f"{tag} ")]
            assert own == [f"{tag} {n}" for n in range(20)]


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
BOTH = frozenset({T.INTRO, T.CREDITS})
UNUSABLE = {"unusable": "couldn't be used (unreadable, another cut, or its library hides a type in Plex)"}


def _lines_for(source_ids, rows, notes, *, skipped=None, decisions=NONE_DECIDED, is_episode=True, servers=()):
    """Every requested source's (or server's) own line, in the order given -- the shape ``_attempt`` logs them in,
    live, one at a time, rather than the old batched-and-regrouped rendering."""
    types = BOTH if is_episode else frozenset({T.CREDITS})
    view = make_source_view(rows, notes, skipped or {}, decisions, types)
    lines = []
    for source_id in source_ids:
        source = Source(source_id)
        if not is_episode and source in EPISODE_ONLY_SOURCES:
            continue
        if source is Source.SERVER_MARKERS:
            for server_id, name in servers:
                lines.append(server_source_line(server_id, name, view, UNUSABLE))
        else:
            lines.append(source_line(source, view))
            if source is Source.SEASON_AUDIO:
                extra = last_seasons_audio_line(view)
                if extra:
                    lines.append(extra)
    return lines


def _asked(*sources: Source, server: str = "") -> RunNotes:
    notes = RunNotes()
    for source in sources:
        notes.answered(source, server if source is Source.SERVER_MARKERS else "")
    return notes


class TestRunNotesAnswered:
    """A source offered again (its "not needed" line already logged) whose answer changes this stage: the fresh
    answer earns its own line, not the "not needed" one it already had."""

    def test_answering_a_source_clears_its_already_logged_line(self):
        notes = RunNotes()
        notes.not_asked[Source.INTRODB] = ALREADY_DECIDED
        notes.logged_sources.add((Source.INTRODB, ""))

        notes.answered(Source.INTRODB)

        assert (Source.INTRODB, "") not in notes.logged_sources
        assert Source.INTRODB not in notes.not_asked

    def test_answering_a_servers_own_markers_clears_only_that_servers_key(self):
        notes = RunNotes()
        notes.logged_sources.add((Source.SERVER_MARKERS, "plex-1"))
        notes.logged_sources.add((Source.SERVER_MARKERS, "emby-1"))

        notes.answered(Source.SERVER_MARKERS, "plex-1")

        assert (Source.SERVER_MARKERS, "plex-1") not in notes.logged_sources
        assert (Source.SERVER_MARKERS, "emby-1") in notes.logged_sources


class TestASourceOfferedAgain:
    """End to end: IntroDB is logged "not needed" (intro is already decided from TheIntroDB and Plex's own markers
    agreeing), then Plex's stored answer turns out to have been read by an older reader and is dropped -- intro is
    left undecided, IntroDB is asked for real, and its answer gets its own line, not held back by the "not needed" one
    already logged for the same key."""

    def test_a_source_offered_again_gets_its_real_answer_logged(self, store, media, job_log):
        reg = _plex(media)
        raw = {
            "sources": [
                {"id": "theintrodb", "enabled": True},
                {"id": "introdb", "enabled": True},
                {"id": "server_markers", "enabled": True},
            ],
            "detect": {"intro": True, "credits": False},
        }
        server_candidate = Candidate(T.INTRO, 127_000, 157_000, Source.SERVER_MARKERS, origin="plex-1")
        introdb_candidate = Candidate(T.INTRO, 200_000, 230_000, Source.INTRODB)
        clients = _clients(introdb=LookupResult("ok", (introdb_candidate,)))
        ctx = _ctx(store, reg, clients=clients, settings_raw=raw)
        ctx.monotonic = FakeClock()

        st = os.stat(media)
        rec = ctx.store.upsert_file(
            FileIdentity(media, st.st_size, st.st_mtime_ns), duration_ms=DUR, season_key=None, is_movie=False
        )
        ctx.store.replace_evidence(
            rec.id, Source.THEINTRODB, [TIDB_INTRO], version=pipeline.PARSER_VERSIONS[Source.THEINTRODB]
        )
        # Stored by a reader older than ``PLEX_CHECKED_SINCE``: due to be dropped, not read again this run, once the
        # server shows our markers (``_drop_older_reader_answer``).
        ctx.store.replace_evidence(rec.id, Source.SERVER_MARKERS, [server_candidate], origin="plex-1", version=4)
        ctx.store.set_publish_state(
            rec.id, "plex-1", item_id="item-plex-1", markers=[Marker(T.INTRO, 127_894, 156_824, ())], status="written"
        )

        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe())

        messages = _messages(job_log)
        # The "not needed" line logs first, from the stored (still agreeing) evidence -- IntroDB isn't asked yet.
        assert f"{EPISODE} · Checking IntroDB… not asked (already decided)" in messages
        # Plex's stored answer, read by an older reader, is dropped without ever contacting Plex this run: its line
        # says "dropped now", not "asked now" (which would claim a contact that never happened).
        assert (
            f"{EPISODE} · Checking Plex's own markers… not used (read by an older version; it shows our markers "
            "now; dropped now)" in messages
        )
        # Intro is undecided again (only one online source, unconfirmed): IntroDB is asked for real, and its own
        # fresh answer logs its own line -- not suppressed as a repeat of the "not needed" line already logged.
        assert clients["introdb"].calls  # really asked, not reused
        assert f"{EPISODE} · Checking IntroDB… intro 3:20–3:50 (asked now)" in messages
        # Two online answers can't decide between them; nothing is sent, and the intro Plex shows is left as it is.
        intro = store.get_decisions(rec.id)[T.INTRO]
        assert (intro.status, intro.reason) == (
            DecisionStatus.NO_EVIDENCE,
            "only TheIntroDB and IntroDB have the intro; an online answer needs a check against the file",
        )
        assert (intro.proposed_start_ms, intro.proposed_end_ms, intro.decided_by) == (127_894, 156_824, ("theintrodb",))
        assert out.outcome_key == FileOutcome.NO_MARKERS.value


class TestSourceLines:
    """Each state a source's line can be in, right when it's logged, one source at a time."""

    @pytest.mark.parametrize(
        ("source", "rows", "expected"),
        [
            (Source.CHAPTERS, [_row(Source.CHAPTERS)], "Checking chapters… none (asked now)"),
            (
                Source.CHAPTERS,
                [_row(Source.CHAPTERS, T.INTRO, 41_000, 72_000, label="Opening")],
                'Checking chapters… "Opening" chapter at 0:41–1:12 (asked now)',
            ),
            (
                Source.CHAPTERS,
                [_row(Source.CHAPTERS, T.INTRO, 41_000, 72_000)],
                "Checking chapters… intro chapter at 0:41–1:12 (asked now)",
            ),
            (Source.THEINTRODB, [_row(Source.THEINTRODB)], "Checking TheIntroDB… no entry (asked now)"),
            (
                Source.SKIPDB,
                [_row(Source.SKIPDB, T.CREDITS, 2_508_000, 2_590_000)],
                "Checking SkipDB… credits 41:48–43:10 (asked now)",
            ),
            (
                Source.INTRODB,
                [_row(Source.INTRODB, T.INTRO, 41_000, 72_000)],
                "Checking IntroDB… intro 0:41–1:12 (asked now)",
            ),
            (Source.SEASON_AUDIO, [_row(Source.SEASON_AUDIO)], "Checking season audio… no match (asked now)"),
            (
                Source.SEASON_AUDIO,
                [_row(Source.SEASON_AUDIO, T.INTRO, 41_000, 72_000, label="9/9")],
                "Checking season audio… intro 0:41–1:12 (same theme found in 10 of 10 episodes; asked now)",
            ),
            (Source.CREDITS_TEXT, [_row(Source.CREDITS_TEXT)], "Checking credit text… none found (asked now)"),
            (
                Source.CREDITS_TEXT,
                [_row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, 2_590_000)],
                "Checking credit text… credits 41:48–43:10 (asked now)",
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

    @pytest.mark.parametrize(
        ("moves", "note"),
        [
            (True, 'moves the "End Credits" chapter at 41:10 to the first credit card'),
            (False, 'keeps the "End Credits" chapter at 41:10'),
        ],
        ids=["moves", "keeps"],
    )
    def test_credit_text_read_against_a_credits_chapter_says_what_it_found(self, moves, note):
        rows = [
            _row(Source.CHAPTERS, T.CREDITS, 2_470_000, 2_590_000, label="End Credits"),
            _row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, 2_590_000, label=chapter_hint(2_470_000, moves=moves)),
        ]
        assert _lines_for(["credits_text"], rows, _asked(Source.CREDITS_TEXT)) == [
            f"Checking credit text… credits 41:48–43:10 ({note}; asked now)"
        ]

    def test_a_chapter_moved_to_the_first_text_after_it_says_where(self):
        rows = [
            _row(Source.CHAPTERS, T.CREDITS, 2_470_000, 2_590_000, label="End Credits"),
            _row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, 2_590_000,
                 label=chapter_hint(2_470_000, moves=True, to_ms=2_485_000)),
        ]  # fmt: skip
        assert _lines_for(["credits_text"], rows, _asked(Source.CREDITS_TEXT)) == [
            'Checking credit text… credits 41:48–43:10 (moves the "End Credits" chapter at 41:10 to the first text '
            "after it, 41:25; asked now)"
        ]

    def test_credit_text_read_against_an_unnamed_credits_chapter_calls_it_the_credits_chapter(self):
        rows = [_row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, None, label=chapter_hint(2_470_000, moves=True))]
        assert _lines_for(["credits_text"], rows, RunNotes()) == [
            "Checking credit text… credits start at 41:48 (moves the credits chapter at 41:10 to the first credit "
            "card; saved 2026-09-27)"
        ]

    def test_an_answer_stored_earlier_says_so(self):
        rows = [
            _row(Source.CHAPTERS),
            _row(Source.SEASON_AUDIO, T.INTRO, 41_000, 72_000, label="8/9"),
            _row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, None),
        ]
        assert _lines_for(["chapters", "season_audio", "credits_text"], rows, RunNotes()) == [
            "Checking chapters… none (saved 2026-09-27)",
            "Checking season audio… intro 0:41–1:12 (same theme found in 9 of 10 episodes; saved 2026-09-27)",
            "Checking credit text… credits start at 41:48 (saved 2026-09-27)",
        ]

    def test_last_seasons_audio_follows_season_audio(self):
        rows = [_row(Source.SEASON_AUDIO), _row(Source.SEASON_AUDIO_PREVIOUS, T.INTRO, 41_000, 72_000, label="7/8")]
        notes = _asked(Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS)
        assert _lines_for(["season_audio"], rows, notes) == [
            "Checking season audio… no match (asked now)",
            "Checking last season's audio… intro 0:41–1:12 (same theme found in 7 of last season's 8 episodes; "
            "asked now)",
        ]

    @pytest.mark.parametrize(
        ("source", "on_gpu", "device", "expected"),
        [
            (Source.CREDITS_TEXT, True, "", "Reading credit text on the GPU…"),
            (Source.CREDITS_TEXT, True, "NVIDIA TITAN RTX", "Reading credit text on the GPU (NVIDIA TITAN RTX)…"),
            (Source.CREDITS_TEXT, False, "", "Reading credit text on the CPU…"),
            (Source.SEASON_AUDIO, False, "", "Reading season audio on the CPU…"),
        ],
        ids=["gpu-unnamed", "gpu-named", "cpu", "season-audio"],
    )
    def test_reading_line_says_where_it_reads(self, source, on_gpu, device, expected):
        assert reading_line(source, on_gpu=on_gpu, device=device) == expected

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"seconds": 13.2}, "Credit text: credits start at 41:48 (13 s)"),
            ({"seconds": 0.46}, "Credit text: credits start at 41:48 (0.5 s)"),
            (
                {"seconds": 95, "fallback": "Credit text detection on the CPU: no GPU memory"},
                "Credit text: credits start at 41:48 (1 min 35 s; credit text detection on the CPU: no GPU memory)",
            ),
            (
                {"seconds": 40, "gpu_read_nothing": True},
                "Credit text: credits start at 41:48 (read on the CPU after the GPU read nothing (40 s in all))",
            ),
        ],
        ids=["gpu", "cpu", "fallback", "gpu-read-nothing"],
    )
    def test_read_result_line_says_the_answer_and_how_long(self, kwargs, expected):
        rows = [_row(Source.CREDITS_TEXT, T.CREDITS, 2_508_000, None)]
        assert read_result_line(Source.CREDITS_TEXT, None, rows, {}, **kwargs) == expected

    def test_read_result_line_with_no_answer_to_store_says_why_and_not_how_long(self):
        assert read_result_line(Source.CREDITS_TEXT, "the decode timed out", [], {}, seconds=7.0) == (
            "Credit text: the decode timed out"
        )

    def test_a_source_skipped_for_the_whole_job_says_why(self):
        skipped = {Source.THEINTRODB: "TheIntroDB budget_exhausted", Source.SKIPDB: "SkipDB rejected the API key"}
        assert _lines_for(["theintrodb", "skipdb"], [], RunNotes(), skipped=skipped) == [
            "Checking TheIntroDB… skipped (daily limit reached, resets 00:00 UTC)",
            "Checking SkipDB… skipped (rejected the API key)",
        ]

    def test_a_source_asked_without_an_answer_to_store_says_what_happened(self):
        notes = RunNotes(unanswered={Source.SKIPDB: "unavailable (HTTP 503)"})
        assert _lines_for(["skipdb"], [], notes) == ["Checking SkipDB… unavailable (HTTP 503)"]

    @pytest.mark.parametrize(
        ("source", "note", "expected"),
        [
            (Source.SKIPDB, "not asked (not set up)", "Checking SkipDB… not asked (not set up)"),
            (Source.SKIPDB, None, "Checking SkipDB… not asked"),
            (Source.CREDITS_TEXT, None, "Checking credit text… not read"),
            (Source.SEASON_AUDIO, "not available here", "Checking season audio… not available here"),
            (
                Source.CREDITS_TEXT,
                "not read (every server keeps its own credits)",
                "Checking credit text… not read (every server keeps its own credits)",
            ),
            (Source.CREDITS_TEXT, ALREADY_DECIDED, "Checking credit text… not read (already decided)"),
            (Source.SKIPDB, ALREADY_DECIDED, "Checking SkipDB… not asked (already decided)"),
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
            'Checking chapters… "End Credits" chapter at 41:48–43:10 (asked now)',
            "Checking credit text… not read (a chapter named End Credits is used as-is)",
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
            "Checking SkipDB… not asked (chapters named Intro and Credits are used as-is)"
        ]

    @pytest.mark.parametrize(
        ("rows", "asked", "expected"),
        [
            ([_row(Source.SERVER_MARKERS, origin="plex-1")], True, "Checking Plex's own markers… none (asked now)"),
            (
                [_row(Source.SERVER_MARKERS, T.CREDITS, 2_508_000, 2_590_000, origin="plex-1")],
                True,
                "Checking Plex's own markers… credits 41:48–43:10 (asked now)",
            ),
            (
                [
                    _row(
                        Source.SERVER_MARKERS, T.CREDITS, 2_508_000, 2_590_000, origin="plex-1",
                        detail=STALE_SERVER_MARKERS_DETAIL,
                    )
                ],
                True,
                "Checking Plex's own markers… credits 41:48–43:10 (made for an earlier file; asked now)",
            ),
            (
                [_row(Source.SERVER_MARKERS, origin="plex-1", detail="unusable")],
                True,
                f"Checking Plex's own markers… {UNUSABLE['unusable'][:-1]}; asked now)",
            ),
            (
                # A reason and "(saved <date>)" join inside the reason's own bracket, not stack a second one.
                [_row(Source.SERVER_MARKERS, origin="plex-1", detail="unusable")],
                False,
                "Checking Plex's own markers… couldn't be used (unreadable, another cut, or its library hides a "
                "type in Plex; saved 2026-09-27)",
            ),
            (
                [_row(Source.SERVER_MARKERS_IMPORTED, T.INTRO, 41_000, 72_000, origin="plex-1")],
                True,
                "Checking Plex's imported markers… intro 0:41–1:12 (asked now)",
            ),
            (
                [_row(Source.SERVER_MARKERS, origin="plex-1")],
                False,
                "Checking Plex's own markers… none (saved 2026-09-27)",
            ),
        ],
        ids=["none", "answer", "made-for-an-earlier-file", "unusable", "unusable-saved-date", "imported",
             "saved-date"],
    )  # fmt: skip
    def test_each_servers_own_markers(self, rows, asked, expected):
        notes = _asked(Source.SERVER_MARKERS, server="plex-1") if asked else RunNotes()
        assert _lines_for(["server_markers"], rows, notes, servers=[("plex-1", "Plex")]) == [expected]

    @pytest.mark.parametrize(
        ("server_not_read", "reason"),
        [
            ({"plex-1": "this server shows our markers"}, "this server shows our markers"),
            ({}, "not due this run"),
        ],
        ids=["known", "unknown"],
    )
    def test_a_server_never_read_says_why(self, server_not_read, reason):
        notes = RunNotes(server_not_read=server_not_read)
        assert _lines_for(["server_markers"], [], notes, servers=[("plex-1", "Plex")]) == [
            f"Checking Plex's own markers… not read ({reason})"
        ]

    def test_a_film_leaves_out_the_sources_that_describe_episodes_only(self):
        notes = _asked(Source.CHAPTERS, Source.SKIPDB)
        notes.unanswered[Source.INTRODB] = "not asked (needs a TV episode with an imdb id)"
        notes.not_asked[Source.SEASON_AUDIO] = "doesn't apply to this file"
        rows = [_row(Source.CHAPTERS), _row(Source.SKIPDB)]
        enabled = ["chapters", "introdb", "skipdb", "season_audio"]
        assert _lines_for(enabled, rows, notes, is_episode=False) == [
            "Checking chapters… none (asked now)",
            "Checking SkipDB… no entry (asked now)",
        ]


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
            # The closest answer kept as a proposal isn't named: nothing was decided.
            (TypeDecision(T.CREDITS, DecisionStatus.NO_EVIDENCE, None,
                          Marker(T.CREDITS, 2_508_000, 2_590_000, ("theintrodb",)), ONLINE_ONLY_REASON),
             f"credits nothing found ({ONLINE_ONLY_REASON})"),
            (TypeDecision(T.INTRO, DecisionStatus.NO_EVIDENCE, None, None, "sources disagree: chapters, introdb"),
             "intro nothing found (sources disagree: chapters, introdb)"),
            (NONE_DECIDED[T.INTRO], "intro nothing found"),
            (TypeDecision(T.CREDITS, DecisionStatus.NO_EVIDENCE, None, None, "2 candidate(s) failed sanity checks"),
             "credits nothing found (2 candidate(s) failed sanity checks)"),
            (TypeDecision(T.CREDITS, DecisionStatus.DISABLED, None, None, "kept Plex's and Emby's own markers"),
             "credits kept Plex's and Emby's own"),
            (TypeDecision(T.RECAP, DecisionStatus.DISABLED, None, None, "detection off"), "recap: detection off"),
        ],
        ids=["two-agree", "three-agree", "one-source", "chapters-unnamed", "locked", "carried-over",
             "undecided-proposal", "undecided-no-proposal", "nothing-found", "nothing-found-why", "kept-own", "other"],
    )  # fmt: skip
    def test_type_phrase(self, decision, expected):
        assert type_phrase(decision) == expected

    @pytest.mark.parametrize(
        ("decision", "chapters", "expected"),
        [
            pytest.param(
                _decided(T.CREDITS, 5_572_000, 5_854_000, "chapters", "credits_text", reason=TEXT_MOVES_CHAPTER_REASON),
                {T.CREDITS: {5_529_000: "Credits"}},
                'credits 1:32:52–1:37:34 (the "Credits" chapter, moved to the first credit card by credit text)',
                id="text-moves-the-chapter",
            ),
            pytest.param(
                _decided(T.CREDITS, 5_572_000, 5_854_000, "chapters", "credits_text", reason=TEXT_MOVES_CHAPTER_REASON),
                {},
                "credits 1:32:52–1:37:34 (the chapters, moved to the first credit card by credit text)",
                id="text-moves-an-unnamed-chapter",
            ),
            pytest.param(
                _decided(T.CREDITS, 5_590_000, 5_854_000, "chapters", "credits_text", "server_markers",
                         reason=TEXT_MOVES_CHAPTER_REASON + "; start shortened to the server's own marker (plex-1)"),
                {T.CREDITS: {5_529_000: "Credits"}},
                'credits 1:33:10–1:37:34 (the "Credits" chapter, moved to the first credit card by credit text; start '
                "moved to the server's own marker)",
                id="text-moves-the-chapter-then-a-server-shortens-it",
            ),
            pytest.param(
                _decided(T.CREDITS, 2_577_000, 2_618_000, "introdb", "credits_text",
                         reason="sources agree: introdb, credits_text; start from credit text"),
                {},
                "credits 42:57–43:38 (IntroDB and credit text agree; start from credit text)",
                id="text-sets-the-start-of-agreeing-answers",
            ),
            pytest.param(
                _decided(T.INTRO, 11_000, 104_000, "introdb", "season_audio",
                         reason=AUDIO_OVER_CHAPTER_REASON + "introdb, season_audio"),
                {T.INTRO: {0: "Intro"}},
                'intro 0:11–1:44 (IntroDB and season audio agree; the "Intro" chapter runs on into the episode)',
                id="season-audio-over-an-intro-chapter",
            ),
            pytest.param(
                _decided(T.CREDITS, 2_508_000, 2_590_000, "skipdb", "credits_text",
                         reason=TEXT_OVER_CHAPTER_REASON + "skipdb, credits_text"),
                {T.CREDITS: {2_470_000: "Credits"}},
                'credits 41:48–43:10 (SkipDB and credit text agree; the "Credits" chapter is off the credit roll)',
                id="credit-text-over-a-credits-chapter",
            ),
        ],
    )  # fmt: skip
    def test_the_2026_09_27_rules_say_what_they_did(self, decision, chapters, expected):
        assert type_phrase(decision, chapters) == expected

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


def _server(status, ours=(), *, kept=frozenset(), withheld=frozenset(), message="", reason_code=None, had=None,
            had_is_ours=True):  # fmt: skip
    row = {"server_id": "plex-1", "server_name": "Plex", "server_type": "plex", "status": status.value,
           "message": message, "reason_code": reason_code}  # fmt: skip
    return ServerResult(row, tuple(ours), frozenset(kept), frozenset(withheld), had, had_is_ours)


INTRO_M = Marker(T.INTRO, 41_000, 72_000, ("introdb",))
CREDITS_M = Marker(T.CREDITS, 2_508_000, 2_590_000, ("credits_text",))
CREDITS_M_OLD = Marker(T.CREDITS, 2_470_000, 2_500_000, ("credits_text",))


class TestSentLine:
    """``server_result_line``'s non-``WRITTEN`` shapes, in the preview log's ``[<server>] …`` style."""

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (_server(ServerStatus.UP_TO_DATE, (INTRO_M,)), "Already up to date (intro 0:41–1:12)"),
            (_server(ServerStatus.NONE), "Nothing to send"),
            (_server(ServerStatus.UP_TO_DATE, kept={T.CREDITS}), "Kept Plex's own credits"),
            (_server(ServerStatus.WRITTEN, (INTRO_M,), had=(), kept={T.CREDITS}, withheld={T.CREDITS}),
             "Added intro 0:41–1:12; kept Plex's own credits instead of ours (\"Keep Plex's\")"),
            (_server(ServerStatus.FAILED, message="Plex's database was busy"), "Failed (Plex's database was busy)"),
            (_server(ServerStatus.SKIPPED, message="Plex Pass needed"), "Skipped (Plex Pass needed)"),
            (_server(ServerStatus.WAITING, reason_code=NOT_IN_LIBRARY),
             "Not in Plex's library yet (waiting for it to add the file)"),
            (_server(ServerStatus.WAITING, reason_code=PLEX_PASS_UNKNOWN), "Waiting (couldn't confirm Plex Pass)"),
            (_server(ServerStatus.WAITING, (INTRO_M,), message="Waiting for the other versions"),
             "Intro 0:41–1:12; waiting for the other versions"),
        ],
        ids=["up-to-date", "nothing", "kept-own", "kept-instead-of-ours", "failed", "skipped", "not-in-library",
             "plex-pass-unknown", "waiting"],
    )  # fmt: skip
    def test_one_line_per_server(self, result, expected):
        assert server_result_line(result) == f"[Plex] {expected}"

    def test_added_names_a_type_the_server_never_had(self):
        result = _server(ServerStatus.WRITTEN, (CREDITS_M, INTRO_M), had=())
        assert server_result_line(result) == "[Plex] Added intro 0:41–1:12 and credits 41:48–43:10"

    def test_replaced_names_the_old_span_and_the_new_one(self):
        result = _server(ServerStatus.WRITTEN, (CREDITS_M,), had=(CREDITS_M_OLD,))
        assert server_result_line(result) == "[Plex] Replaced credits 41:10–41:40 → 41:48–43:10"

    def test_removed_names_a_type_the_server_had_that_no_longer_holds(self):
        result = _server(ServerStatus.WRITTEN, (INTRO_M,), had=(CREDITS_M_OLD,))
        assert server_result_line(result) == "[Plex] Added intro 0:41–1:12; removed credits (it no longer holds)"

    def test_cleared_when_nothing_is_ours_there_any_more(self):
        assert server_result_line(_server(ServerStatus.WRITTEN, (), had=(CREDITS_M_OLD,))) == (
            "[Plex] Cleared our markers"
        )

    def test_the_prior_value_unknown_says_so_instead_of_guessing(self):
        # ``had=None`` (the ``ServerResult`` default): we've never published there and never read its own markers.
        result = _server(ServerStatus.WRITTEN, (CREDITS_M,))
        assert server_result_line(result) == ("[Plex] Sent credits 41:48–43:10 (what Plex had before wasn't read)")

    def test_a_span_unchanged_from_what_we_sent_before_is_called_restored(self):
        # A WRITTEN row only happens when the write really changed the server (a basis mismatch): if every type
        # still matches what we last sent, the server must have drifted since (a Plex rescan dropping markers, say)
        # and this write restored them -- not "added" (nothing's new) and not merely "unchanged" (bytes were sent).
        result = _server(ServerStatus.WRITTEN, (INTRO_M,), had=(INTRO_M,))
        assert server_result_line(result) == "[Plex] Restored intro 0:41–1:12"

    def test_a_type_only_in_the_servers_own_evidence_isnt_called_removed(self):
        # ``had`` fell back to the server's own evidence (we've never published there): a type it shows that we
        # never sent isn't ours to call removed -- it's simply left out, not claimed as a change we made. Intro's
        # span happens to match that same evidence (it was decided from it), but we've never sent it before: it's
        # this type's first send, not "Restored" -- that word is only for a span that matches what we sent before.
        result = _server(ServerStatus.WRITTEN, (INTRO_M,), had=(INTRO_M, CREDITS_M_OLD), had_is_ours=False)
        assert server_result_line(result) == "[Plex] Added intro 0:41–1:12 (same as Plex's own)"

    def test_a_type_the_last_sent_no_longer_holds_is_called_removed(self):
        # The same shape, but ``had`` really is our own record: this one is ours to call removed.
        result = _server(ServerStatus.WRITTEN, (INTRO_M,), had=(INTRO_M, CREDITS_M_OLD), had_is_ours=True)
        assert server_result_line(result) == "[Plex] Unchanged intro 0:41–1:12; removed credits (it no longer holds)"


class TestUnchangedFilesGetFullBlocks:
    """A file whose answer didn't change and whose servers are up to date logs its full block too, not a one-line
    summary: every source's answer (a reused one saying when it was last stored), what was decided and what was sent."""

    def test_a_hundred_file_recheck_logs_a_block_per_file(self, store, tmp_path, job_log):
        paths = [_accused(tmp_path, episode) for episode in range(1, 6)]
        reg = _plex(paths[0])

        def run_all():
            ctx = _job(store, paths[0], reg, now=lambda: datetime.now(UTC))
            for n, path in enumerate(paths):
                _run(ctx, path, {"plex-1": ready_publisher()}, probe=_probe(), hints={"plex-1": f"item-{n}"})

        run_all()
        job_log.clear()
        run_all()

        heads = [m for m in _messages(job_log) if m.startswith("Accused S04E") and ": checking" in m]
        assert heads == [f"Accused S04E{n:02d}: checking intro and credits" for n in range(1, 6)]
        dones = [m for m in _messages(job_log) if " · done in" in m]
        assert dones == [f"Accused S04E{n:02d} · done in 0 s (nothing new to send)" for n in range(1, 6)]
        # Every source's line is still there, its reused answer saying when it was stored rather than "asked now".
        first_block = [m for m in _messages(job_log) if m.startswith("Accused S04E01")]
        assert first_block[0] == "Accused S04E01: checking intro and credits"
        assert [line.split("…", 1)[0] for line in first_block[1:5]] == [
            "Accused S04E01 · Checking chapters",
            "Accused S04E01 · Checking TheIntroDB",
            "Accused S04E01 · Checking credit text",
            "Accused S04E01 · Checking Plex's own markers",
        ]
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert first_block[3] == f"Accused S04E01 · Checking credit text… credits start at 21:31 (saved {today})"
        assert first_block[6] == "Accused S04E01 · [Plex] Already up to date (credits 21:31–22:01)"


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
        # A hint gives ``title_asks`` a known item id (a webhook payload, say): naming the file never searches for
        # one itself (that's ``_publish_to``'s job, later in the same run).
        self._known_movie(store, movie)
        reg = _plex(movie)
        server = reg.get("plex-1")
        server.get_external_ids.return_value = {**MOVIE_IDS, "title": "Heat", "year": "1995"}
        item = ProcessableItem(canonical_path=movie, server_id="", item_id_by_server={"plex-1": "item-plex-1"})

        first, lookups = self._check(self._quiet_job(store, reg), item)
        assert first.outcome_key == FileOutcome.PUBLISHED.value
        assert _messages(job_log)[0] == "Heat (1995): checking credits (films get credits only)"
        assert lookups.call_count == 1
        assert server.get_external_ids.call_args.args == ("item-plex-1",)
        job_log.clear()

        # A later job over the same film (decide again, a version re-run, a Season job) asks nothing.
        second, lookups = self._check(self._quiet_job(store, reg), item)
        assert second.outcome_key == FileOutcome.UP_TO_DATE.value
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert _messages(job_log) == [
            "Heat (1995): checking credits (films get credits only)",
            f'Heat (1995) · Checking chapters… "Credits" chapter from 21:35 (saved {today})',
            "Heat (1995) · Checking credit text… not read (a chapter named Credits is used as-is)",
            f"Heat (1995) · Checking Plex's own markers… none (saved {today})",
            'Heat (1995) · Decided: credits 21:35–22:01, from the "Credits" chapter',
            "Heat (1995) · [Plex] Already up to date (credits 21:35–22:01)",
            "Heat (1995) · done in 0 s (nothing new to send)",
        ]
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
        item = ProcessableItem(
            canonical_path=movie, server_id="plex-1", title=item_title, item_id_by_server={"plex-1": "item-plex-1"}
        )

        _, looked_up = self._check(self._quiet_job(store, reg), item)

        assert _messages(job_log)[0] == f"{expected}: checking credits (films get credits only)"
        assert looked_up.call_count == lookups
        assert server.get_external_ids.call_count == lookups

    def test_without_a_known_item_id_the_title_never_asks_a_server(self, store, movie):
        # No hint, and nothing else this run resolved the owner's item id yet (naming the file happens before the
        # per-source loop, so before ``_publish_to`` or server_markers could): searching for one is ``_publish_to``'s
        # job, with a file outcome to justify the wait. A log line never causes that search.
        reg = _plex(movie)
        server = reg.get("plex-1")
        ctx = self._quiet_job(store, reg)
        item = ProcessableItem(canonical_path=movie, server_id="")
        owning = pipeline._owning_servers(item, ctx)
        owners = pipeline._publishing_owners(item, ctx, owning)
        servers = pipeline._ItemServers(item, owning)

        title = pipeline._note_title(ctx, RunNotes(), item, MediaIds("movie"), servers, owners)

        assert title == "Heat (1995)"
        server.resolve_remote_path_to_item_id.assert_not_called()
        server.get_external_ids.assert_not_called()

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
        item = ProcessableItem(canonical_path=movie, server_id="", item_id_by_server={"plex-1": "item-plex-1"})
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
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert _messages(job_log) == [
            f"{name}: checking credits (films get credits only)",
            f'{name} · Checking chapters… "Credits" chapter from 21:35 (saved {today})',
            f"{name} · Checking credit text… not read (a chapter named Credits is used as-is)",
            f"{name} · Checking Plex's own markers… none (saved {today})",
            f'{name} · Decided: credits 21:35–22:01, from the "Credits" chapter',
            f"{name} · [Plex] Already up to date (credits 21:35–22:01)",
            f"{name} · done in 0 s (nothing new to send)",
        ]


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestTitleTellApart:
    """Two files in one job that resolve to the same title (two copies of one film): the second gets a short
    tell-apart from data already in its own name, the first stays plain."""

    def test_the_first_file_with_a_title_keeps_it_plain(self, store, movie):
        ctx = _job(store, movie, _plex(movie))
        assert pipeline._claim_title(ctx, "Heat (1995)", "/movies/Heat (1995) [1080p].mkv") == "Heat (1995)"

    def test_a_second_file_with_the_same_title_gets_its_own_resolution_tag(self, store, movie):
        ctx = _job(store, movie, _plex(movie))
        assert pipeline._claim_title(ctx, "Heat (1995)", "/movies/Heat (1995) [2160p].mkv") == "Heat (1995)"
        assert pipeline._claim_title(ctx, "Heat (1995)", "/movies/Heat (1995) [1080p].mkv") == "Heat (1995) (1080p)"

    def test_a_second_file_with_no_tag_in_its_name_keeps_the_plain_title(self, store, movie):
        # No data at hand to tell them apart with: left as it is, same as the file it collides with.
        ctx = _job(store, movie, _plex(movie))
        assert pipeline._claim_title(ctx, "Heat (1995)", "/movies/Heat (1995) [2160p].mkv") == "Heat (1995)"
        assert pipeline._claim_title(ctx, "Heat (1995)", "/movies/Heat (1995) copy.mkv") == "Heat (1995)"

    def test_a_different_title_is_never_held_back_by_anothers_count(self, store, movie):
        ctx = _job(store, movie, _plex(movie))
        pipeline._claim_title(ctx, "Heat (1995)", "/movies/Heat (1995) [2160p].mkv")
        assert pipeline._claim_title(ctx, "Se7en (1995)", "/movies/Se7en (1995) [2160p].mkv") == "Se7en (1995)"

    def test_two_versions_of_one_episode_get_told_apart_too(self, store, tmp_path):
        # Episodes return before the film branch (``SxxEyy`` alone), but still go through ``_claim_title``: two
        # versions of one episode would otherwise share the exact same title.
        folder = tmp_path / "media" / "tv" / "Accused" / "Season 04"
        folder.mkdir(parents=True)
        plain = folder / "Accused - S04E01 - Episode.mkv"
        plain.write_bytes(b"x" * 100)
        tagged = folder / "Accused - S04E01 - Episode [1080p].mkv"
        tagged.write_bytes(b"x" * 100)
        ctx = _job(store, str(plain), _plex(str(plain)))

        def title_of(path):
            item = ProcessableItem(canonical_path=path, server_id="")
            owning = pipeline._owning_servers(item, ctx)
            owners = pipeline._publishing_owners(item, ctx, owning)
            servers = pipeline._ItemServers(item, owning)
            return pipeline._note_title(ctx, RunNotes(), item, MediaIds("episode"), servers, owners)

        assert title_of(str(plain)) == "Accused S04E01"
        assert title_of(str(tagged)) == "Accused S04E01 (1080p)"


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
            ({"seconds": 0.5}, "done in 0.5 s"),
            ({"seconds": 3, "failed": True}, "failed after 3 s"),
            ({"seconds": None}, "done"),
            ({"seconds": 0.5, "nothing_sent": True}, "done in 0.5 s (nothing new to send)"),
            # A failed file's line never gets the note: it already says it failed, not what it sent.
            ({"seconds": 3, "failed": True, "nothing_sent": True}, "failed after 3 s"),
        ],
        ids=["no-worker", "failed", "no-time", "nothing-sent", "failed-ignores-nothing-sent"],
    )  # fmt: skip
    def test_done_line(self, kwargs, expected):
        seconds = kwargs.pop("seconds")
        assert done_line(seconds, **kwargs) == expected

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"seconds": 26, "status": "success"}, "GPU Worker 2 (Intel UHD 770) completed: X (success, 26 s)"),
            ({"seconds": 3, "status": "failed", "reason": "Plex refused the write"},
             "GPU Worker 2 (Intel UHD 770) completed: X (failed: Plex refused the write, 3 s)"),
            ({"seconds": 5, "status": "skipped", "reason": "replaced by a newer file"},
             "GPU Worker 2 (Intel UHD 770) completed: X (skipped: replaced by a newer file, 5 s)"),
            ({"seconds": 10, "status": "success", "cpu_rerun": True},
             "GPU Worker 2 (Intel UHD 770) completed: X (success, rerun on the CPU, 10 s)"),
            ({"seconds": None, "status": "success"}, "GPU Worker 2 (Intel UHD 770) completed: X (success)"),
        ],
        ids=["success", "failed", "skipped", "cpu-rerun", "no-time"],
    )  # fmt: skip
    def test_worker_completed_line(self, kwargs, expected):
        seconds = kwargs.pop("seconds")
        assert worker_completed_line("GPU Worker 2 (Intel UHD 770)", "X", seconds, **kwargs) == expected

    @pytest.mark.parametrize(
        ("types", "is_episode", "worker", "expected"),
        [
            (BOTH, True, "", "X: checking intro and credits"),
            (frozenset({T.INTRO, T.CREDITS, T.RECAP}), True, "", "X: checking intro, credits and recap"),
            (frozenset({T.CREDITS}), False, "", "X: checking credits (films get credits only)"),
            (frozenset(), False, "", "X: nothing to check (detection of every type it could have is off)"),
            (BOTH, True, "GPU Worker 2 (Intel UHD 770)",
             "GPU Worker 2 (Intel UHD 770) picked up: X, checking intro and credits"),
        ],
        ids=["episode", "three-types", "film", "nothing", "worker"],
    )  # fmt: skip
    def test_file_start_line(self, types, is_episode, worker, expected):
        assert file_start_line("X", types, is_episode=is_episode, worker=worker) == expected

    @pytest.mark.parametrize(
        ("worker", "expected"),
        [
            ("GPU Worker 2 (Intel UHD 770)", "Intel UHD 770"),
            ("CPU Worker 1", ""),
            ("", ""),
        ],
        ids=["gpu", "cpu", "checking-thread"],
    )
    def test_worker_device(self, worker, expected):
        assert worker_device(worker) == expected

    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            ([{"status": ServerStatus.WRITTEN.value}], False),
            ([{"status": ServerStatus.UP_TO_DATE.value}, {"status": ServerStatus.WRITTEN.value}], False),
            ([{"status": ServerStatus.UP_TO_DATE.value}], True),
            ([{"status": ServerStatus.NONE.value}], True),
            ([{"status": ServerStatus.UP_TO_DATE.value}, {"status": ServerStatus.NONE.value}], True),
            ([], True),
            # Waiting, failed or skipped: something's unsettled, not simply "nothing new" -- never this phrase.
            ([{"status": ServerStatus.WAITING.value}], False),
            ([{"status": ServerStatus.FAILED.value}], False),
            ([{"status": ServerStatus.SKIPPED.value}], False),
            ([{"status": ServerStatus.UP_TO_DATE.value}, {"status": ServerStatus.WAITING.value}], False),
        ],
        ids=["written", "one-written", "up-to-date", "none", "up-to-date-and-none", "no-servers", "waiting",
             "failed", "skipped", "up-to-date-and-waiting"],
    )  # fmt: skip
    def test_nothing_was_sent(self, rows, expected):
        assert nothing_was_sent(rows) is expected

    def test_a_worker_that_finds_no_checking_stage_notes_names_the_file_from_its_path(self, store, media, job_log):
        # No checking stage ran first (so no ``RunNotes`` to read its types from): its own settings still say what
        # this file's checked for, rather than wrongly saying every type it could have is off.
        ctx = _job(store, media, _plex(media))
        pipeline.log_pickup(ProcessableItem(canonical_path=media, server_id="plex-1"), "CPU Worker 3", ctx=ctx)
        assert _messages(job_log) == [f"CPU Worker 3 picked up: {EPISODE}, checking intro and credits"]


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
    def test_unchanged_episodes_get_full_blocks_too_and_the_season_total_comes_last(self, store, tmp_path, job_log):
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
        # The store's own clock (real time, not the job's fixed one): whatever day the first job actually ran on.
        saved = store.evidence_rows(store.get_file(paths[0]).id)[0].fetched_at[:10]
        skip_note = f"skipped (no data for this show; asked again after {paused_until:%Y-%m-%d})"

        def unchanged_block(episode: int, *, theintrodb: str) -> list[str]:
            title = f"Rick and Morty (2013) S01E{episode:02d}"
            return [
                f"{title}: checking intro and credits",
                f"{title} · Checking chapters… none (saved {saved})",
                f"{title} · Checking TheIntroDB… {theintrodb}",
                f"{title} · Checking credit text… credits start at 21:31 (saved {saved})",
                f"{title} · Checking Plex's own markers… none (saved {saved})",
                f"{title} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
                f"{title} · [Plex] Already up to date (credits 21:31–22:01)",
                f"{title} · done in 0 s (nothing new to send)",
            ]

        changed_block = [
            "Rick and Morty (2013) S01E02: checking intro and credits",
            'Rick and Morty (2013) S01E02 · Checking chapters… "Intro" chapter at 2:06–2:37 (asked now)',
            f"Rick and Morty (2013) S01E02 · Checking TheIntroDB… {skip_note}",
            "Rick and Morty (2013) S01E02 · Reading credit text on the CPU…",
            "Rick and Morty (2013) S01E02 · Credit text: credits start at 21:31 (0 s)",
            "Rick and Morty (2013) S01E02 · Checking Plex's own markers… not read (this server shows our markers)",
            'Rick and Morty (2013) S01E02 · Decided: intro 2:06–2:37, from the "Intro" chapter · credits 21:31–22:01 '
            "(credit text)",
            "Rick and Morty (2013) S01E02 · [Plex] Added intro 2:06–2:37; unchanged credits 21:31–22:01",
            "Rick and Morty (2013) S01E02 · done in 0 s",
        ]
        # E01/E03 aren't due for anything, so TheIntroDB's stored "no entry" is reused; E04 is still due (nothing else
        # about it changed) and finds the pause in place, same as the replaced E02.
        expected = [
            *unchanged_block(1, theintrodb=f"no entry (saved {saved})"),
            *changed_block,
            *unchanged_block(3, theintrodb=f"no entry (saved {saved})"),
            *unchanged_block(4, theintrodb=skip_note),
        ]
        assert _messages(job_log) == expected

        counts = {key: outcomes.count(key) for key in set(outcomes)}
        assert counts == {FileOutcome.UP_TO_DATE.value: 3, FileOutcome.PUBLISHED.value: 1}
        assert season.summary_lines(counts) == [
            "Season re-check, Rick and Morty (2013) S01 (4 episodes): E02 changed (logged above); no change for "
            "E01/E03/E04",
            "Done: 4 files · 1 sent to Plex · 0 nothing found · 3 already up to date",
        ]

    @pytest.mark.parametrize(
        ("episodes", "expected"),
        [
            (
                [SeasonEpisode("E03", False), SeasonEpisode("E01", False)],
                "Season re-check, Show S01 (2 episodes): no change",
            ),
            ([SeasonEpisode("E01", False)], "Season re-check, Show S01 (1 episode): no change"),
            ([SeasonEpisode("E01", True)], "Season re-check, Show S01 (1 episode): E01 changed (logged above)"),
            (
                [SeasonEpisode("E02", True), SeasonEpisode("E01", False), SeasonEpisode("E03", False)],
                "Season re-check, Show S01 (3 episodes): E02 changed (logged above); no change for E01/E03",
            ),
        ],
        ids=["none-changed", "one-episode", "only-changed", "changed-and-unchanged"],
    )
    def test_season_line_wording(self, episodes, expected):
        assert season_line("Show S01", episodes) == expected


class TestDecideAgainJob:
    """The one job after settings v16 decides files again, reusing answers that aren't due: like a Season job, it logs
    every file's full block, changed or not, then its own one summary line of totals."""

    def test_changed_and_unchanged_files_both_get_full_blocks(self, store, media, job_log, monkeypatch):
        monkeypatch.setattr(pipeline, "APP_PUBLISH_WHEN", "high")  # how an older build left credit text alone
        out, _ = _run(_job(store, media, _plex(media)), media, {"plex-1": ready_publisher()}, probe=_probe())
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        credits = store.get_decisions(store.get_file(media).id)[T.CREDITS]
        assert (credits.status, credits.proposed_start_ms, credits.proposed_end_ms, credits.decided_by) == (
            DecisionStatus.NO_EVIDENCE,
            1_291_000,
            DUR,
            ("credits_text",),
        )
        monkeypatch.undo()
        job_log.clear()
        today = datetime.now(UTC).strftime("%Y-%m-%d")

        again = _job(store, media, _plex(media))
        again.decide_again = True
        out, _ = _run(again, media, {"plex-1": ready_publisher()})
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (saved {today})",
            f"{EPISODE} · Checking TheIntroDB… no entry (saved {today})",
            f"{EPISODE} · Checking credit text… credits start at 21:31 (saved {today})",
            f"{EPISODE} · Checking Plex's own markers… none (saved {today})",
            f"{EPISODE} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            f"{EPISODE} · [Plex] Added credits 21:31–22:01",
            DONE,
        ]
        assert again.summary_lines({FileOutcome.PUBLISHED.value: 1}) == [
            "Decided again after the update (1 file): 1 changed (logged above)",
            "Done: 1 file · 1 sent to Plex · 0 nothing found",
        ]

        job_log.clear()
        quiet = _job(store, media, _plex(media))
        quiet.decide_again = True
        _run(quiet, media, {"plex-1": ready_publisher()})
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (saved {today})",
            f"{EPISODE} · Checking TheIntroDB… no entry (saved {today})",
            f"{EPISODE} · Checking credit text… credits start at 21:31 (saved {today})",
            f"{EPISODE} · Checking Plex's own markers… none (saved {today})",
            f"{EPISODE} · Decided: intro nothing found · credits 21:31–22:01 (credit text)",
            f"{EPISODE} · [Plex] Already up to date (credits 21:31–22:01)",
            f"{DONE} (nothing new to send)",
        ]
        # Credits are decided: a file is "still nothing found" only with no marker at all.
        assert quiet.summary_lines({FileOutcome.UP_TO_DATE.value: 1})[0] == (
            "Decided again after the update (1 file): 1 unchanged"
        )

    @pytest.mark.parametrize(
        ("files", "expected"),
        [
            (
                [(True, False), (False, True), (False, False)],
                "3 files): 1 changed (logged above); 2 unchanged, 1 still nothing found",
            ),
            ([(False, True), (False, True)], "2 files): 2 unchanged, still nothing found"),
            ([(True, True)], "1 file): 1 changed (logged above)"),
            ([], "0 files): nothing to decide"),
        ],
        ids=["mixed", "all-still-undecided", "only-changed", "none"],
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

        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        credits = store.get_decisions(store.get_file(media).id)[T.CREDITS]
        assert (credits.status, credits.reason) == (DecisionStatus.NO_EVIDENCE, ONLINE_ONLY_REASON)
        assert (credits.proposed_start_ms, credits.proposed_end_ms) == (TIDB_CREDITS.start_ms, TIDB_CREDITS.end_ms)
        assert len(weekly.clients["theintrodb"].calls) == 1
        # Its credit text answer isn't due: the file isn't read again.
        weekly.local_detectors[0].detect.assert_not_called()
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (saved {today})",
            f"{EPISODE} · Checking TheIntroDB… credits 21:36–22:00 (asked now)",
            f"{EPISODE} · Checking credit text… none found (saved {today})",
            # A server's "none" a day old is read again while a type is undecided, as on any run.
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits {ONLINE_ONLY}",
            f"{EPISODE} · [Plex] Nothing to send",
            f"{DONE} (nothing new to send)",
        ]
        assert weekly.summary_lines({FileOutcome.NO_MARKERS.value: 1}) == [
            "Weekly online re-check (1 file): 1 newly found online, 0 unchanged",
            "Done: 1 file · 0 sent to Plex · 1 nothing found",
        ]

    def test_a_file_still_not_found_gets_a_full_block_too(self, store, media, job_log):
        self._first_run(store, media)
        job_log.clear()

        weekly = self._weekly(store, media, theintrodb=NO_DATA, days_later=15)
        _run(weekly, media, {"plex-1": ready_publisher()})

        assert len(weekly.clients["theintrodb"].calls) == 1  # due, so asked
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (saved {today})",
            f"{EPISODE} · Checking TheIntroDB… no entry (asked now)",
            f"{EPISODE} · Checking credit text… none found (saved {today})",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits nothing found",
            f"{EPISODE} · [Plex] Nothing to send",
            f"{DONE} (nothing new to send)",
        ]
        assert weekly.summary_lines({FileOutcome.NO_MARKERS.value: 1})[0] == (
            "Weekly online re-check (1 file): 0 newly found online, 1 unchanged"
        )

    def test_a_no_entry_that_isnt_due_is_not_asked(self, store, media, job_log):
        self._first_run(store, media)
        job_log.clear()

        weekly = self._weekly(store, media, theintrodb=TIDB, days_later=13)
        _run(weekly, media, {"plex-1": ready_publisher()})

        assert weekly.clients["theintrodb"].calls == []
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert _messages(job_log) == [
            HEAD,
            f"{EPISODE} · Checking chapters… none (saved {today})",
            f"{EPISODE} · Checking TheIntroDB… no entry (saved {today})",
            f"{EPISODE} · Checking credit text… none found (saved {today})",
            f"{EPISODE} · Checking Plex's own markers… none (asked now)",
            f"{EPISODE} · Decided: intro nothing found · credits nothing found",
            f"{EPISODE} · [Plex] Nothing to send",
            f"{DONE} (nothing new to send)",
        ]
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
            FileOutcome.NO_MARKERS.value: 1,
            FileOutcome.FAILED.value: 1,
        }
        line = totals_line(outcome, {"Plex": 2, "Jellyfin": 1}, {"TheIntroDB": 3, "SkipDB": 0})
        assert line == (
            "Done: 6 files · 1 sent to Jellyfin · 2 sent to Plex · 1 nothing found · 1 already up to date · "
            "1 waiting for a server · 1 failed · TheIntroDB skipped for 3 files"
        )

    def test_a_job_with_no_files_says_so(self):
        assert totals_line({}, {}, {}) == "Done: 0 files · 0 nothing found"

    @pytest.mark.parametrize(
        ("ms", "expected"), [(0, "0:00"), (59_999, "0:59"), (2_856_000, "47:36"), (3_725_000, "1:02:05")]
    )
    def test_clock_uses_hours_from_an_hour_on(self, ms, expected):
        assert clock(ms) == expected
