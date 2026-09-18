"""Credits text in the pipeline: worker hand-off, stored versioned answers, forced runs, the tri-state, registration."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.credits import detector, frames
from media_preview_generator.markers.credits.textdet_helper import TextDetState
from media_preview_generator.markers.decide import DecisionStatus
from media_preview_generator.markers.models import Candidate, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.probe import Chapter
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.processing.generator import CodecNotSupportedError
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import ready_publisher
from tests.markers.test_pipeline import DUR, _clients, _ctx, _probe, _registry, _run

media = test_pipeline.media
store = test_pipeline.store
T = MarkerType
START_S = 1_290.25


def settings(level="high", **sources):
    enabled = {"chapters": True, "theintrodb": False, "introdb": False, "skipdb": False, "season_audio": False,
               "credits_text": True, "server_markers": False, **sources}  # fmt: skip
    return {"detect": {"intro": False, "credits": True}, "publish_when": level,
            "sources": [{"id": k, "enabled": v} for k, v in enabled.items()]}  # fmt: skip


@pytest.fixture
def find(monkeypatch):
    """The detector's decode + rule J, answering START_S with no end; tests change ``find.answer`` (a start, a
    ``(start, end)`` pair, None or an exception)."""
    calls: list[dict] = []

    def fake(path, **kwargs):
        calls.append({"path": path, **kwargs})
        if isinstance(fake.answer, BaseException):
            raise fake.answer
        start, end = fake.answer if isinstance(fake.answer, tuple) else (fake.answer, None)
        return detector.CreditsTextResult(start, end, (), (), ())

    fake.answer = START_S
    fake.calls = calls
    monkeypatch.setattr(detector, "find_credits", fake)
    monkeypatch.setattr(detector, "get_textdet_pool", MagicMock)
    return fake


def ctx_for(store, media, level="high", *, force=False, clients=None, **sources):
    return _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings(level, **sources),
                detectors=(detector.credits_text_spec(),), force=force, clients=clients)  # fmt: skip


def pubs():
    return {"plex-1": ready_publisher()}


class TestWorkerHandOff:
    def test_the_check_stage_hands_an_undecided_file_to_a_worker(self, store, media, find):
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="check")
        assert out is None and find.calls == []

    def test_the_worker_runs_it_on_its_gpu_and_stores_a_versioned_answer(self, store, media, find):
        ctx = ctx_for(store, media)
        ctx.config.ffmpeg_path = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
        out, _ = _run(ctx, media, pubs(), stage="process", gpu="NVIDIA", gpu_device_path="cuda:0")
        (call,) = find.calls
        assert (call["path"], call["duration_ms"], call["is_episode"], call["ffmpeg"], call["gpu"], call["gpu_device_path"]) == (
            media, DUR, True, "/usr/lib/jellyfin-ffmpeg/ffmpeg", "NVIDIA", "cuda:0")  # fmt: skip
        rec = store.get_file(media)
        assert store.get_evidence(rec.id) == [Candidate(T.CREDITS, 1_290_250, None, Source.CREDITS_TEXT)]
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION
        assert out.outcome_key == FileOutcome.NEEDS_REVIEW.value  # one source at High

    def test_medium_publishes_it_alone_q1(self, store, media, find):
        plex = ready_publisher()
        out, _ = _run(ctx_for(store, media, "medium"), media, {"plex-1": plex}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == ("item-plex-1", [Marker(T.CREDITS, 1_290_250, DUR, ("credits_text",))])

    def test_a_scene_after_the_credits_is_kept_q3(self, store, media, find):
        find.answer = (1_200.0, 1_260.0)  # the roll ends 61 s before the end of the file
        plex = ready_publisher()
        out, _ = _run(ctx_for(store, media, "medium"), media, {"plex-1": plex}, stage="process")
        assert store.get_evidence(store.get_file(media).id) == [
            Candidate(T.CREDITS, 1_200_000, 1_260_000, Source.CREDITS_TEXT)
        ]
        assert plex.write.call_args.args == (
            "item-plex-1",
            [Marker(T.CREDITS, 1_200_000, 1_260_000, ("credits_text",))],
        )

    def test_emby_gets_the_credits_start_and_skips_to_the_end_of_the_file_r1(self, store, media, find, tmp_path):
        # Spec §6.3 R1 is unchanged by Q3: Emby has no credits end, so it gets the start and the row says so.
        from media_preview_generator.markers.publishers.emby import CREDITS_BEFORE_END_NOTE
        from tests.markers.test_emby_publisher import FakeEmby, _publisher, _write

        find.answer = (1_200.0, 1_260.0)
        plex = ready_publisher()
        _run(ctx_for(store, media, "medium"), media, {"plex-1": plex}, stage="process")
        (decided,) = plex.write.call_args.args[1]
        emby = FakeEmby(str(media))
        emby.publisher = _publisher(emby)
        assert _write(emby, [decided]) == [decided]
        sent = emby.server.put_emby_markers.call_args.kwargs
        assert sent["credits_start_ticks"] == 12_000_000_000 and not any(
            "end" in k for k in sent if k.startswith("credits")
        )
        assert emby.publisher.projection_note([decided], duration_ms=DUR) == CREDITS_BEFORE_END_NOTE

    def test_credits_decided_by_other_sources_never_take_a_worker(self, store, media, find):
        clients = _clients(skipdb=LookupResult("ok", (Candidate(T.CREDITS, 1_291_000, None, Source.SKIPDB),)),
                           introdb=LookupResult("ok", (Candidate(T.CREDITS, 1_289_000, None, Source.INTRODB),)))  # fmt: skip
        out, _ = _run(ctx_for(store, media, clients=clients, skipdb=True, introdb=True), media, pubs(), stage="check")
        assert out is not None and out.outcome_key == FileOutcome.PUBLISHED.value
        assert find.calls == []

    def test_credits_a_chapter_decided_never_take_a_worker(self, store, media, find):
        # Local detectors run only for undecided types or answers they supplied (``_detector_pending``); online sources
        # still confirm or veto the chapter (rule 3) without a worker.
        chapters = _probe((Chapter(0, 1_290_000, "Episode"), Chapter(1_290_000, None, "Credits")))
        out, _ = _run(ctx_for(store, media), media, pubs(), probe=chapters, stage="check")
        assert out is not None and out.outcome_key == FileOutcome.PUBLISHED.value
        assert find.calls == []


class TestStoredAnswers:
    def test_a_current_answer_is_not_read_again_but_a_forced_run_reads_it(self, store, media, find):
        _run(ctx_for(store, media), media, pubs(), stage="process")
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="check")
        assert out is not None and len(find.calls) == 1
        _run(ctx_for(store, media, force=True), media, pubs(), stage="process")
        assert len(find.calls) == 2

    def test_a_changed_file_is_read_again(self, store, media, find):
        _run(ctx_for(store, media), media, pubs(), stage="process")
        with open(media, "ab") as fh:
            fh.write(b"more")
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert len(find.calls) == 2

    def test_no_roll_is_stored_as_nothing_found_and_not_read_again(self, store, media, find):
        find.answer = None
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="process")
        rec = store.get_file(media)
        assert [(r.source, r.type) for r in store.evidence_rows(rec.id) if r.source is Source.CREDITS_TEXT] == [
            (Source.CREDITS_TEXT, None)
        ]
        assert _run(ctx_for(store, media), media, pubs(), stage="check")[0] is not None
        assert len(find.calls) == 1

    @pytest.mark.parametrize(
        "error",
        [frames.FrameDecodeError("timed out"), frames.DecodeCancelledError("cancelled"),
         detector.TextDetUnavailableError("helper gone")],
    )  # fmt: skip
    def test_no_answer_this_time_stores_nothing(self, store, media, find, error):
        find.answer = error
        _run(ctx_for(store, media), media, pubs(), stage="process")
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) is None
        assert _run(ctx_for(store, media), media, pubs(), stage="check")[0] is None  # asked again

    def test_a_timed_out_file_is_not_decoded_again_for_a_day_unless_forced(self, store, media, find):
        find.answer = frames.DecodeTimeoutError("decoding S01E01.mkv timed out after 600 s")
        _run(ctx_for(store, media), media, pubs(), stage="process")
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert len(find.calls) == 1
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) is None
        _run(ctx_for(store, media, force=True), media, pubs(), stage="process")
        assert len(find.calls) == 2

    def test_a_gpu_decode_failure_reaches_the_workers_cpu_rerun(self, store, media, find):
        find.answer = frames.GpuDecodeError("the GPU decoded no frames")
        with pytest.raises(CodecNotSupportedError):
            _run(ctx_for(store, media), media, pubs(), stage="process", gpu="NVIDIA", gpu_device_path="cuda:0")
        find.answer = START_S
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="process", gpu=None, gpu_device_path=None)
        assert find.calls[-1]["gpu"] is None and out.outcome_key == FileOutcome.NEEDS_REVIEW.value


class TestAvailability:
    def _stored(self, store, media, find):
        _run(ctx_for(store, media, "medium"), media, pubs(), stage="process")
        return store.get_file(media)

    @pytest.mark.parametrize(
        ("state", "expected"),
        [(TextDetState.AVAILABLE, DecisionStatus.DECIDED), (TextDetState.UNKNOWN, DecisionStatus.DECIDED),
         (TextDetState.ABSENT, DecisionStatus.NO_EVIDENCE)],
    )  # fmt: skip
    def test_stored_answers_decide_only_while_detection_can_run_or_its_check_did_not_answer(
        self, store, media, find, state, expected
    ):
        rec = self._stored(store, media, find)
        ctx = _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings("medium"),
                   detectors=(detector.credits_text_spec(),) if state is TextDetState.AVAILABLE else ())  # fmt: skip
        ctx.credits_text = state
        assert pipeline._decide(ctx, rec, frozenset({T.CREDITS}))[T.CREDITS].status is expected

    @pytest.mark.parametrize("state", list(TextDetState))
    @pytest.mark.parametrize(("source_on", "credits_on"), [(True, True), (False, True), (True, False)])
    def test_the_detector_is_registered_only_when_it_can_run(self, loguru_caplog, state, source_on, credits_on):
        raw = {
            "detect": {"intro": False, "credits": credits_on},
            "sources": [{"id": "credits_text", "enabled": source_on}, {"id": "season_audio", "enabled": False}],
        }
        found = pipeline.default_local_detectors(
            load_global(validate_global(raw, None)[0]), MagicMock(), credits_text=state
        )
        registered = source_on and credits_on and state is TextDetState.AVAILABLE
        assert [s.source for s in found] == ([Source.CREDITS_TEXT] if registered else [])
        warned = source_on and credits_on and state is not TextDetState.AVAILABLE
        assert ("On-screen credit text is on" in loguru_caplog.text) is warned

    @pytest.mark.parametrize(
        ("source_on", "credits_on", "asked"), [(True, True, True), (False, True, False), (True, False, False)]
    )
    def test_build_context_checks_text_detection_only_when_it_could_run(self, store, source_on, credits_on, asked):
        raw = {
            "detect": {"intro": False, "credits": credits_on},
            "sources": [{"id": "credits_text", "enabled": source_on}, {"id": "season_audio", "enabled": False}],
        }
        with (
            patch.object(pipeline, "get_global_settings", return_value=load_global(validate_global(raw, None)[0])),
            patch.object(pipeline, "get_marker_store", return_value=store),
            patch.object(pipeline, "build_clients", return_value={}),
            patch.object(pipeline, "text_detection_state", return_value=TextDetState.AVAILABLE) as checked,
        ):
            ctx = pipeline.build_context(registry=MagicMock(), config=MagicMock(ffmpeg_path=None), priority=3)
        assert checked.called is asked
        assert ctx.credits_text is (TextDetState.AVAILABLE if asked else TextDetState.ABSENT)
        assert [s.source for s in ctx.local_detectors] == ([Source.CREDITS_TEXT] if asked else [])


@pytest.mark.timeout(150)  # the subprocess may take up to 120 s on a slow runner; addopts has --timeout=30
def test_the_web_app_and_the_pipeline_never_load_onnxruntime_or_opencv(tmp_path):
    code = (
        "import sys, os\n"
        f"os.environ['CONFIG_DIR'] = {str(tmp_path)!r}\n"
        "import media_preview_generator.markers.pipeline\n"
        "from media_preview_generator.web.app import create_app\n"
        f"create_app(config_dir={str(tmp_path)!r})\n"
        "print(sorted(m for m in ('onnxruntime', 'onnxruntime_ep_webgpu', 'cv2', 'pyclipper') if m in sys.modules))\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"  # the app's background threads would keep the interpreter alive
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "[]", out.stderr[-2000:]
