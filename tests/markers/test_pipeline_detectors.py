"""Local detectors in the pipeline: versioned answers, due checks, inline runs, per-source forced refresh, follow-ups."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import Candidate, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
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
        detector = MagicMock(return_value=[AUDIO_INTRO])  # one source: the intro stays in review at "High"
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY)
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
        intro_by_two = _clients(
            theintrodb=LookupResult("ok", (TIDB_INTRO,)),
            skipdb=LookupResult("ok", (Candidate(T.INTRO, 127_500, 156_500, Source.SKIPDB),)),
        )
        ctx = _ctx(store, reg, detectors=(_spec(detector),), settings_raw=INTRO_ONLY, clients=intro_by_two)
        _run(ctx, media, _pubs(), stage="process")
        assert store.get_decisions(store.get_file(media).id)[T.INTRO].status is DecisionStatus.DECIDED
        out, _ = _run(ctx, media, _pubs())
        assert out is not None and out.outcome_key == FileOutcome.UP_TO_DATE.value
        detector.assert_not_called()

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
        assert out is not None and out.outcome_key == FileOutcome.NEEDS_REVIEW.value
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
