"""Local detectors in the pipeline: versioned answers, due checks, inline runs, per-source forced refresh, follow-ups."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FILE_BUSY, FileOutcome, ServerStatus
from media_preview_generator.markers.pipeline import DetectorUnavailableError, LocalDetectorSpec
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import ready_publisher, server_config
from tests.markers.test_pipeline import DUR, INTRO_ONLY, TIDB_INTRO, _clients, _ctx, _media_root, _registry, _run

# test_pipeline's fixtures, shared by name (an import of them reads as unused to the linter).
media = test_pipeline.media
store = test_pipeline.store

T = MarkerType
AUDIO_INTRO = Candidate(T.INTRO, 126_000, 158_000, Source.SEASON_AUDIO, 1.0, "2/2")
HINT_INTRO = Candidate(T.INTRO, 126_000, 158_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4")
TEXT_CREDITS = Candidate(T.CREDITS, 1_300_000, DUR, Source.CREDITS_TEXT, 1.0, "")


def _spec(detector, **kwargs):
    return LocalDetectorSpec(Source.SEASON_AUDIO, frozenset({T.INTRO}), detector, **kwargs)


def _pubs():
    return {"plex-1": ready_publisher()}


class TestStoredAnswers:
    def test_answer_is_stored_under_each_source_with_the_detector_version(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO, TEXT_CREDITS])
        spec = _spec(detector, stores=frozenset({Source.SEASON_AUDIO, Source.CREDITS_TEXT}), version=4)
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) == 4
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == 4
        assert {AUDIO_INTRO, TEXT_CREDITS} <= set(store.get_evidence(rec.id))

    def test_empty_answer_records_nothing_there_for_every_stored_source(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        spec = _spec(MagicMock(return_value=[]), stores=frozenset({Source.SEASON_AUDIO, Source.CREDITS_TEXT}))
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        rec = store.get_file(media)
        rows = {(r.source, r.type) for r in store.evidence_rows(rec.id)}
        assert {(Source.SEASON_AUDIO, None), (Source.CREDITS_TEXT, None)} <= rows

    def test_a_current_answer_is_not_asked_again_while_the_type_stays_undecided(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        # The previous season's hint alone: it never decides, so the intro stays in review.
        hint = Candidate(T.INTRO, 126_000, 158_000, Source.SEASON_AUDIO_PREVIOUS, 1.0, "4/4")
        detector = MagicMock(return_value=[hint])
        spec = _spec(detector, stores=frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}))
        ctx = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY)
        first, _ = _run(ctx, media, _pubs(), stage="process")
        second, _ = _run(ctx, media, _pubs(), stage="process")
        assert first.outcome_key == second.outcome_key == FileOutcome.NEEDS_REVIEW.value
        assert detector.call_count == 1

    def test_candidates_for_a_source_it_does_not_store_are_dropped(self, store, media, loguru_caplog):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO, TEXT_CREDITS])
        _run(_ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        rec = store.get_file(media)
        assert store.get_evidence(rec.id) == [AUDIO_INTRO]
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) is None
        warnings = [r.getMessage() for r in loguru_caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1 and "season_audio" in warnings[0] and "credits_text" in warnings[0]

    def test_stores_replaces_its_own_source_instead_of_adding_to_it(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO, TEXT_CREDITS])
        spec = _spec(detector, stores=frozenset({Source.CREDITS_TEXT}))
        assert spec.stored_sources == frozenset({Source.CREDITS_TEXT})
        ctx = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process")
        rec = store.get_file(media)
        assert store.get_evidence(rec.id) == [TEXT_CREDITS]
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) is None
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == 1
        _run(ctx, media, _pubs(), stage="process")
        assert detector.call_count == 1  # its answer is current although nothing is stored under season audio

    def test_an_answer_stored_under_only_some_of_its_sources_is_asked_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        spec = _spec(detector, stores=frozenset({Source.SEASON_AUDIO, Source.CREDITS_TEXT}))
        ctx = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process")
        rec = store.get_file(media)
        store.replace_evidence(rec.id, Source.CREDITS_TEXT, [])  # the second write never landed: no version
        _run(ctx, media, _pubs(), stage="process")
        assert detector.call_count == 2
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == 1

    def test_a_new_detector_version_runs_it_again(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        _run(_ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        spec2 = _spec(detector, version=2)
        _run(_ctx(store, reg, detectors=(spec2,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        assert detector.call_count == 2

    def test_a_new_detector_version_runs_it_again_for_a_type_it_helped_decide(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))

        def ctx_for(version):
            return _ctx(
                store, reg, detectors=(_spec(detector, version=version),), settings_raw=INTRO_ONLY, clients=clients
            )

        _run(ctx_for(1), media, _pubs(), stage="process")
        rec = store.get_file(media)
        assert store.get_decisions(rec.id)[T.INTRO].status is DecisionStatus.DECIDED
        out, _ = _run(ctx_for(1), media, _pubs())
        assert out.outcome_key == FileOutcome.UP_TO_DATE.value and detector.call_count == 1
        assert _run(ctx_for(2), media, _pubs())[0] is None  # the check hands the decided file to a worker
        _run(ctx_for(2), media, _pubs(), stage="process")
        assert detector.call_count == 2
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) == 2

    def test_a_type_decided_without_the_detector_does_not_run_it(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        due = MagicMock(return_value=True)
        intro_by_two = _clients(
            theintrodb=LookupResult("ok", (TIDB_INTRO,)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 127_500, 156_500, Source.SKIPDB),)),
        )
        ctx = _ctx(store, reg, detectors=(_spec(detector, due=due),), settings_raw=INTRO_ONLY, clients=intro_by_two)
        _run(ctx, media, _pubs(), stage="process")
        assert store.get_decisions(store.get_file(media).id)[T.INTRO].status is DecisionStatus.DECIDED
        out, _ = _run(ctx, media, _pubs())
        assert out is not None and out.outcome_key == FileOutcome.UP_TO_DATE.value
        detector.assert_not_called()
        due.assert_not_called()  # a decision made without it costs nothing more

    @pytest.mark.parametrize("due", [False, True], ids=["answer-current", "answer-due"])
    @pytest.mark.parametrize("answer", [AUDIO_INTRO, HINT_INTRO], ids=["season-audio", "previous-season-hint"])
    def test_a_type_decided_with_the_detectors_answer_runs_it_again_when_the_answer_is_due(
        self, store, media, answer, due
    ):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[answer])
        is_due = MagicMock(return_value=False)
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        spec = _spec(detector, stores=frozenset({Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS}), due=is_due)
        ctx = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY, clients=clients)
        _run(ctx, media, _pubs(), stage="process")
        marker = store.get_markers(store.get_file(media).id)[T.INTRO]
        assert marker.decided_by == ("theintrodb", answer.source.value)
        is_due.return_value = due
        out, _ = _run(ctx, media, _pubs())
        if due:
            assert out is None  # every type is decided, and still the check hands it to a worker
            _run(ctx, media, _pubs(), stage="process")
        else:
            assert out.outcome_key == FileOutcome.UP_TO_DATE.value
        assert detector.call_count == (2 if due else 1)

    def test_a_locked_marker_crediting_the_detector_does_not_run_it(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        ctx = _ctx(store, reg, detectors=(_spec(detector, due=lambda _r, _c: True),), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process")
        store.lock_marker(store.get_file(media).id, Marker(T.INTRO, 126_000, 158_000, ("season_audio",)))
        out, _ = _run(ctx, media, _pubs())
        assert out is not None and detector.call_count == 1

    def test_due_hook_runs_it_again_and_gets_the_record_and_context(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        due = MagicMock(return_value=True)
        ctx = _ctx(store, reg, detectors=(_spec(detector, due=due),), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process")
        _run(ctx, media, _pubs(), stage="process")
        assert detector.call_count == 2
        rec, passed_ctx = due.call_args.args
        assert rec.canonical_path == media and passed_ctx is ctx

    def test_unavailable_detector_stores_nothing_and_is_asked_next_run(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(side_effect=[DetectorUnavailableError("no chromaprint"), [AUDIO_INTRO]])
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY)
        out, _ = _run(ctx, media, _pubs(), stage="process")
        rec = store.get_file(media)
        assert out.outcome_key == FileOutcome.NO_MARKERS.value
        assert store.evidence_version(rec.id, Source.SEASON_AUDIO) is None
        _run(ctx, media, _pubs(), stage="process")
        assert detector.call_count == 2 and AUDIO_INTRO in store.get_evidence(rec.id)


class TestWhereItRuns:
    def test_detector_that_needs_no_worker_runs_on_the_checking_thread(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        needs = MagicMock(return_value=False)
        cancel = MagicMock(return_value=False)
        ctx = _ctx(store, reg, detectors=(_spec(detector, needs_worker=needs),), settings_raw=INTRO_ONLY)
        out, _ = _run(ctx, media, _pubs(), cancel_check=cancel)
        # Season audio decides an intro alone (owner 2026-09-24): the file is done on the checking thread.
        assert out is not None and out.outcome_key == FileOutcome.PUBLISHED.value
        kwargs = detector.call_args.kwargs
        assert (kwargs["gpu"], kwargs["gpu_device_path"], kwargs["pause_check"]) == (None, None, None)
        assert kwargs["cancel_check"] is cancel and kwargs["ctx"] is ctx
        rec, passed_ctx = needs.call_args.args
        assert rec.canonical_path == media and passed_ctx is ctx

    @pytest.mark.parametrize("needs_worker", [None, lambda _r, _c: True], ids=["unset", "says-yes"])
    def test_detector_that_needs_a_worker_defers_the_check(self, store, media, needs_worker):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        ctx = _ctx(store, reg, detectors=(_spec(detector, needs_worker=needs_worker),), settings_raw=INTRO_ONLY)
        assert _run(ctx, media, _pubs())[0] is None
        detector.assert_not_called()

    def test_check_defers_when_any_pending_detector_needs_a_worker(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        inline, worker = MagicMock(return_value=[]), MagicMock(return_value=[])
        specs = (
            _spec(inline, needs_worker=lambda _r, _c: False),
            _spec(worker, stores=frozenset({Source.CREDITS_TEXT})),
        )
        out, _ = _run(_ctx(store, reg, detectors=specs, settings_raw=INTRO_ONLY), media, _pubs())
        assert out is None
        inline.assert_not_called()
        worker.assert_not_called()


class TestWhatTheWorkerHandsTheDetector:
    """The worker stage hands a detector the worker's own ffmpeg threads and fallback flag, and the pause that freezes
    running work: everything paused or the job's schedule's stop time (``PipelineContext.freeze_check``), never the
    job's own pause, which lets the running file finish."""

    def test_the_freeze_check_the_threads_and_the_fallback_flag_reach_the_detector(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY)
        ctx.freeze_check = MagicMock(return_value=False)
        flag = MagicMock()
        _run(ctx, media, _pubs(), stage="process", gpu="NVIDIA", gpu_device_path="cuda:0", ffmpeg_threads=3,
             fallback_callback=flag, pause_check=MagicMock(return_value=True))  # fmt: skip
        kwargs = detector.call_args.kwargs
        assert kwargs["pause_check"] is ctx.freeze_check  # the job's own pause (True here) never reaches it
        assert (kwargs["gpu"], kwargs["gpu_device_path"], kwargs["ffmpeg_threads"]) == ("NVIDIA", "cuda:0", 3)
        assert kwargs["fallback_callback"] is flag

    def test_a_cpu_worker_hands_no_thread_cap(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process", gpu=None, gpu_device_path=None, ffmpeg_threads=None)
        kwargs = detector.call_args.kwargs
        assert (kwargs["gpu"], kwargs["ffmpeg_threads"], kwargs["pause_check"]) == (None, None, None)

    def test_a_detector_on_the_checking_thread_is_never_frozen(self, store, media):
        # A checking thread isn't a worker (no GPU, no threads to cap, no row), and its detector runs are the cheap ones
        # (a season step from cached fingerprints): a job frozen by its schedule's stop time must not keep them, or its
        # frozen checks would hold the kind's whole share of checking threads from every other job until the next
        # start.
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        ctx = _ctx(store, reg, detectors=(_spec(detector, needs_worker=lambda _r, _c: False),), settings_raw=INTRO_ONLY)
        ctx.freeze_check = MagicMock(return_value=True)  # the job's schedule's stop time has passed
        started = time.monotonic()
        out, _ = _run(ctx, media, _pubs())
        assert time.monotonic() - started < 2 and out is not None
        kwargs = detector.call_args.kwargs
        assert (kwargs["gpu"], kwargs["ffmpeg_threads"], kwargs["fallback_callback"]) == (None, None, None)
        assert kwargs["pause_check"] is None

    # Only a worker waits: the checking stage hands a file another job runs to a worker at once
    # (test_pipeline's test_the_checking_stage_hands_a_file_another_job_runs_to_a_worker_without_waiting).
    @pytest.mark.parametrize("stage", ["process"])
    def test_a_cancel_ends_the_wait_for_another_jobs_run_of_the_file(self, store, media, stage):
        # Another job's run holds the file; one frozen by its schedule's stop time holds it until the next start.
        from media_preview_generator.markers import pipeline

        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[AUDIO_INTRO])
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY)
        holding, release, cancelled = threading.Event(), threading.Event(), threading.Event()

        def other_job():
            with pipeline._PATH_LOCKS.hold(media):
                holding.set()
                release.wait(10)

        holder = threading.Thread(target=other_job, daemon=True)
        holder.start()
        try:
            assert holding.wait(5)
            threading.Timer(0.2, cancelled.set).start()
            started = time.monotonic()
            out, _ = _run(ctx, media, _pubs(), stage=stage, cancel_check=cancelled.is_set)
            assert time.monotonic() - started < 2
        finally:
            release.set()
            holder.join(5)
        assert (out.outcome_key, out.message) == (FileOutcome.FAILED.value, "cancelled by user")
        detector.assert_not_called()
        assert media not in pipeline._PATH_LOCKS._locks


class TestForcedRefresh:
    def test_forced_run_reads_markers_on_servers_again_in_the_worker_stage(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        root = _media_root(media)
        reg.configs_by_id["jellyfin-1"] = server_config(
            "jellyfin-1", ServerType.JELLYFIN, root=root, markers={"enabled": False, "library_ids": None}
        )
        detector = MagicMock(return_value=[])
        spec = _spec(detector)
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        reads = reg.get("jellyfin-1").get_media_segments
        assert reads.call_count == 1
        forced = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY, force=True)
        assert _run(forced, media, _pubs())[0] is None  # handed to a worker at season audio, before server markers
        _run(forced, media, _pubs(), stage="process")
        assert reads.call_count == 2
        assert detector.call_count == 2  # its stored answer was current, and the forced worker stage still ran it

    def test_forced_check_that_fails_before_storing_chapters_reads_them_again_on_the_worker(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        spec = _spec(MagicMock(return_value=[]))
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        forced = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY, force=True)
        upsert = store.upsert_file
        with patch.object(store, "upsert_file", side_effect=[RuntimeError("database is locked"), upsert]):
            with pytest.raises(RuntimeError):
                _run(forced, media, _pubs())
        _, probe = _run(forced, media, _pubs(), stage="process")
        assert probe.call_count == 1

    def test_forced_run_with_chapters_turned_off_does_not_probe_again_on_the_worker(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        raw = {**INTRO_ONLY, "sources": [{"id": "chapters", "enabled": False}, {"id": "theintrodb", "enabled": True}]}
        forced = _ctx(store, reg, detectors=(_spec(MagicMock(return_value=[])),), settings_raw=raw, force=True)
        assert "chapters" not in forced.settings.ordered_enabled_sources()
        first, probe = _run(forced, media, _pubs())
        assert first is None and probe.call_count == 1
        _, probe = _run(forced, media, _pubs(), stage="process")
        assert probe.call_count == 0

    def test_forced_run_does_not_run_a_detector_the_checking_thread_ran_again_on_the_worker(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        inline, worker = MagicMock(return_value=[AUDIO_INTRO]), MagicMock(return_value=[])
        specs = (
            _spec(inline, needs_worker=lambda _r, _c: False),
            LocalDetectorSpec(Source.CREDITS_TEXT, frozenset({T.CREDITS}), worker),
        )
        forced = _ctx(store, reg, detectors=specs, force=True)
        assert _run(forced, media, _pubs())[0] is None  # season audio ran here; credit text needs a worker
        assert (inline.call_count, worker.call_count) == (1, 0)
        _run(forced, media, _pubs(), stage="process", gpu="NVIDIA", gpu_device_path="cuda:0")
        assert (inline.call_count, worker.call_count) == (1, 1)
        assert (worker.call_args.kwargs["gpu"], worker.call_args.kwargs["gpu_device_path"]) == ("NVIDIA", "cuda:0")

    def test_forced_worker_stage_runs_an_inline_detector_again_when_its_answer_turned_due(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        inline, worker = MagicMock(return_value=[AUDIO_INTRO]), MagicMock(return_value=[])
        specs = (
            _spec(inline, needs_worker=lambda _r, _c: False, due=lambda _r, _c: True),
            LocalDetectorSpec(Source.CREDITS_TEXT, frozenset({T.CREDITS}), worker),
        )
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        forced = _ctx(store, reg, detectors=specs, clients=clients, force=True)
        assert _run(forced, media, _pubs())[0] is None
        _run(forced, media, _pubs(), stage="process")
        assert store.get_decisions(store.get_file(media).id)[T.INTRO].status is DecisionStatus.DECIDED
        assert (inline.call_count, worker.call_count) == (2, 1)  # the intro was decided when the worker started

    def test_a_forced_job_forgets_what_it_refreshed_for_a_file_once_the_file_is_done(self, store, media):
        # A library-wide forced job must not keep an entry for every file it ran; a handed-off file keeps its own
        # until the worker stage has used it.
        reg = _registry(media, ServerType.PLEX)
        forced = _ctx(store, reg, detectors=(_spec(MagicMock(return_value=[])),), settings_raw=INTRO_ONLY, force=True)
        assert _run(forced, media, _pubs())[0] is None
        # Refreshed up to season audio, where it was handed to a worker.
        assert forced._refreshed == {media: {Source.CHAPTERS, Source.THEINTRODB, Source.INTRODB, Source.SKIPDB}}
        _run(forced, media, _pubs(), stage="process")
        assert forced._refreshed == {}

    def test_a_forced_file_done_on_the_checking_thread_leaves_nothing_behind(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        forced = _ctx(store, reg, settings_raw=INTRO_ONLY, force=True)
        out, probe = _run(forced, media, _pubs())
        assert out is not None and probe.call_count == 1
        assert forced._refreshed == {}

    def test_forced_run_runs_a_detector_whose_answer_is_current(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        detector = MagicMock(return_value=[])
        spec = _spec(detector, due=lambda _r, _c: False)
        _run(_ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY), media, _pubs(), stage="process")
        forced = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY, force=True)
        _run(forced, media, _pubs(), stage="process")
        assert detector.call_count == 2


class TestFollowups:
    def test_requests_are_merged_sorted_and_taken_once(self, store, media):
        ctx = _ctx(store, _registry(media, ServerType.PLEX))
        ctx.request_followups(["/tv/b.mkv", "/tv/a.mkv"])
        ctx.request_followups(["/tv/a.mkv"])
        assert ctx.take_followups() == ["/tv/a.mkv", "/tv/b.mkv"]
        assert ctx.take_followups() == []

    def test_a_detector_can_request_followups_through_its_context(self, store, media):
        reg = _registry(media, ServerType.PLEX)

        def detect(rec, *, ctx, **_kw):
            ctx.request_followups(["/tv/sibling.mkv"])
            return []

        ctx = _ctx(store, reg, detectors=(_spec(detect),), settings_raw=INTRO_ONLY)
        _run(ctx, media, _pubs(), stage="process")
        assert ctx.take_followups() == ["/tv/sibling.mkv"]

    def test_the_followups_hook_is_asked_before_the_worker_handoff(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        followups = MagicMock(return_value=["/tv/sibling.mkv"])
        ctx = _ctx(
            store, reg, detectors=(_spec(MagicMock(return_value=[]), followups=followups),), settings_raw=INTRO_ONLY
        )
        assert _run(ctx, media, _pubs())[0] is None  # handed to a worker
        assert ctx.take_followups() == ["/tv/sibling.mkv"]
        rec, passed_ctx = followups.call_args.args
        assert rec.canonical_path == media and passed_ctx is ctx

    def test_the_followups_hook_is_asked_when_every_type_is_decided(self, store, media):
        reg = _registry(media, ServerType.PLEX)
        followups = MagicMock(return_value=["/tv/sibling.mkv"])
        detector = MagicMock(return_value=[AUDIO_INTRO])
        clients = _clients(theintrodb=LookupResult("ok", (TIDB_INTRO,)))
        spec = _spec(detector, followups=followups)
        ctx = _ctx(store, reg, detectors=(spec,), settings_raw=INTRO_ONLY, clients=clients)
        _run(ctx, media, _pubs(), stage="process")
        ctx.take_followups()
        followups.reset_mock()
        assert _run(ctx, media, _pubs())[0].outcome_key == FileOutcome.UP_TO_DATE.value
        assert followups.call_count == 1 and ctx.take_followups() == ["/tv/sibling.mkv"]
        assert detector.call_count == 1

    def test_the_followups_hook_is_not_asked_for_a_file_without_the_detectors_types(self, store, tmp_path):
        folder = tmp_path / "media" / "movies" / "Toy Story (1995) {tmdb-862}"
        folder.mkdir(parents=True)
        path = str(folder / "Toy Story (1995).mkv")
        open(path, "wb").close()
        followups = MagicMock(return_value=["/tv/sibling.mkv"])
        ctx = _ctx(store, _registry(path, ServerType.PLEX), detectors=(_spec(MagicMock(), followups=followups),))
        _run(ctx, path, _pubs())
        followups.assert_not_called()

    def test_the_followups_hook_is_not_asked_while_its_source_is_off(self, store, media):
        followups = MagicMock(return_value=["/tv/sibling.mkv"])
        raw = {**INTRO_ONLY, "sources": [{"id": "season_audio", "enabled": False}]}
        spec = _spec(MagicMock(return_value=[]), followups=followups)
        ctx = _ctx(store, _registry(media, ServerType.PLEX), detectors=(spec,), settings_raw=raw)
        assert _run(ctx, media, _pubs())[0] is not None
        followups.assert_not_called()


class TestRunMemo:
    def test_a_run_memo_is_kept_for_the_run_of_the_file_on_its_own_thread(self, store, media):
        ctx = _ctx(store, _registry(media, ServerType.PLEX))
        ctx.run_memo(media)["listing"] = 1
        assert ctx.run_memo(media) == {}  # outside a run nothing is kept
        with ctx._running(media):
            memo = ctx.run_memo(media)
            memo["listing"] = 2
            seen_elsewhere = []
            thread = threading.Thread(target=lambda: seen_elsewhere.append(dict(ctx.run_memo(media))))
            thread.start()
            thread.join()
            assert ctx.run_memo(media) is memo and seen_elsewhere == [{}]
        assert ctx.run_memo(media) == {}


class TestAnotherJobsRunOfTheFile:
    """A worker whose file another job's worker is running (the file run lock): how long it waits and how it gives the
    file back. The holder is a real worker run whose detector blocks until the test lets it go."""

    NEXT_RUN_ROW = "Another Intro & Credits job is running this file; trying again on the next run"
    RETRY_ROW = "Another Intro & Credits job is running this file; this job tries again in a few minutes"

    @pytest.fixture
    def scene(self, store, media):
        from media_preview_generator.markers import pipeline

        reg = _registry(media, ServerType.PLEX)
        entered, release = threading.Event(), threading.Event()
        threads: list[threading.Thread] = []

        def blocking_detector(rec, **kwargs):
            entered.set()
            release.wait(20)
            return [AUDIO_INTRO]

        def hold(*, frozen: bool) -> None:
            holder = _ctx(store, reg, detectors=(_spec(blocking_detector),), settings_raw=INTRO_ONLY)
            holder.freeze_check = lambda: frozen
            thread = threading.Thread(
                target=pipeline.process_item, args=(test_pipeline._item(media),), kwargs={"ctx": holder}, daemon=True
            )
            thread.start()
            threads.append(thread)
            assert entered.wait(5)

        def wait_for_it(waiter, **kwargs) -> tuple[list, threading.Thread]:
            out: list = []
            thread = threading.Thread(
                target=lambda: out.append(pipeline.process_item(test_pipeline._item(media), ctx=waiter, **kwargs)),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
            return out, thread

        def waiting_now() -> bool:
            entry = pipeline._PATH_LOCKS._locks.get(media)
            return entry is not None and entry[1] >= 2  # the holder and the waiter

        with (
            patch.object(pipeline, "probe_media", return_value=test_pipeline._probe()),
            patch.object(pipeline, "publisher_for", side_effect=lambda server, cfg, **kw: ready_publisher()),
        ):
            yield SimpleNamespace(reg=reg, hold=hold, wait_for_it=wait_for_it, release=release, waiting_now=waiting_now)
            release.set()
            for thread in threads:
                thread.join(10)

    def _waiter(self, store, scene, *, retries: bool, cap: int = 500):
        waiter = _ctx(
            store, scene.reg, detectors=(_spec(MagicMock(return_value=[AUDIO_INTRO])),), settings_raw=INTRO_ONLY
        )
        waiter.busy_writes_retried = retries
        waiter.retry_file_cap = cap
        waiter.freeze_check = lambda: False
        return waiter

    def _given_back(self, out, media, wording):
        (outcome,) = out
        assert outcome.outcome_key == FileOutcome.WAITING.value
        (row,) = outcome.publisher_rows
        assert (row["server_id"], row["status"], row["reason_code"]) == (
            "plex-1",
            ServerStatus.WAITING.value,
            FILE_BUSY,
        )
        assert row["message"] == wording

    def test_a_frozen_holder_with_no_retry_gets_the_file_back_at_once_for_the_next_run(self, store, media, scene):
        scene.hold(frozen=True)  # frozen by its schedule's stop time: it holds the file until the next start
        waiter = self._waiter(store, scene, retries=False)
        started = time.monotonic()
        out, thread = scene.wait_for_it(waiter)
        thread.join(3)
        assert not thread.is_alive() and time.monotonic() - started < 3
        self._given_back(out, media, self.NEXT_RUN_ROW)
        assert waiter.busy_promised() == set()

    def test_a_running_holder_with_no_retry_is_waited_for_at_most_the_cap(self, store, media, scene, monkeypatch):
        from media_preview_generator.markers import pipeline

        monkeypatch.setattr(pipeline, "WORKER_FILE_WAIT_NO_RETRY_S", 0.6)
        scene.hold(frozen=False)
        waiter = self._waiter(store, scene, retries=False)
        started = time.monotonic()
        out, thread = scene.wait_for_it(waiter)
        thread.join(5)
        assert not thread.is_alive()
        assert time.monotonic() - started >= 0.6
        self._given_back(out, media, self.NEXT_RUN_ROW)

    def test_a_frozen_holder_with_a_retry_gets_the_file_back_at_once_with_the_retry(self, store, media, scene):
        scene.hold(frozen=True)
        waiter = self._waiter(store, scene, retries=True)
        out, thread = scene.wait_for_it(waiter)
        thread.join(3)
        assert not thread.is_alive()
        self._given_back(out, media, self.RETRY_ROW)
        assert waiter.busy_promised() == {media}

    def test_the_checking_stage_hands_the_file_on_without_taking_a_retry_promise(self, store, media, scene):
        # The check stage never waits and never gives a file back: a promise taken there would hold one of the job's
        # retry places for a file its worker may still run.
        from media_preview_generator.markers import pipeline

        scene.hold(frozen=True)
        waiter = self._waiter(store, scene, retries=True, cap=1)
        assert pipeline.check_item(test_pipeline._item(media), ctx=waiter) is None
        assert waiter.busy_promised() == set()
        assert waiter.may_promise_busy_retry("/media/other.mkv")

    def test_a_retry_promise_gone_by_the_give_back_keeps_the_worker_waiting(self, store, media, scene):
        # The promise is taken when the file is given back, not when the wait starts: the job's last retry place went
        # to another file meanwhile, so giving this one back would leave it with no retry at all.
        scene.hold(frozen=False)
        waiter = self._waiter(store, scene, retries=True, cap=1)
        paused = threading.Event()
        out, thread = scene.wait_for_it(waiter, pause_check=paused.is_set)
        deadline = time.monotonic() + 5
        while not scene.waiting_now() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert scene.waiting_now()
        assert waiter.promise_busy_retry("/media/other.mkv")  # the only place left
        paused.set()  # a paused job gives a file back at once when it can promise a retry
        thread.join(1.5)
        assert thread.is_alive(), "gave the file back with no retry to run it"
        scene.release.set()
        thread.join(10)
        (outcome,) = out
        # It ran the file once the holder let go (the holder had just sent the same markers).
        assert outcome.outcome_key == FileOutcome.UP_TO_DATE.value
        assert waiter.busy_promised() == {"/media/other.mkv"}

    def test_with_no_promise_left_the_wait_still_ends_at_the_cap(self, store, media, scene, monkeypatch):
        from media_preview_generator.markers import pipeline

        monkeypatch.setattr(pipeline, "WORKER_FILE_WAIT_NO_RETRY_S", 0.6)
        monkeypatch.setattr(pipeline, "WORKER_FILE_WAIT_S", 0.2)
        scene.hold(frozen=False)
        waiter = self._waiter(store, scene, retries=True, cap=1)
        assert waiter.promise_busy_retry("/media/other.mkv")
        out, thread = scene.wait_for_it(waiter, pause_check=lambda: True)
        thread.join(5)
        assert not thread.is_alive()
        self._given_back(out, media, self.NEXT_RUN_ROW)
        assert waiter.busy_promised() == {"/media/other.mkv"}
