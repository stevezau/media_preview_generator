"""Credits text in the pipeline: worker hand-off, stored versioned answers, forced runs, the tri-state, registration."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.markers import pipeline
from media_preview_generator.markers.credits import detector, frames
from media_preview_generator.markers.credits.textdet_helper import TextDetState
from media_preview_generator.markers.decide import TEXT_CHECKS_CHAPTER_REASON, DecisionStatus
from media_preview_generator.markers.models import Candidate, FileIdentity, Marker, MarkerType, Source
from media_preview_generator.markers.outcomes import FileOutcome
from media_preview_generator.markers.probe import Chapter
from media_preview_generator.markers.settings import load_global, validate_global
from media_preview_generator.markers.sources.online import LookupResult
from media_preview_generator.processing.generator import CodecNotSupportedError
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import ready_publisher, server_config
from tests.markers.test_pipeline import DUR, _clients, _ctx, _probe, _registry, _run
from tests.markers.test_pipeline import _rows as _rows_by_server

media = test_pipeline.media
store = test_pipeline.store
T = MarkerType
START_S = 1_290.25


def settings(credits_window=None, **sources):
    enabled = {"chapters": True, "theintrodb": False, "introdb": False, "skipdb": False, "season_audio": False,
               "credits_text": True, "server_markers": False, **sources}  # fmt: skip
    block = {"detect": {"intro": False, "credits": True},
             "sources": [{"id": k, "enabled": v} for k, v in enabled.items()]}  # fmt: skip
    if credits_window is not None:
        block["credits_window"] = credits_window
    return block


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


def ctx_for(store, media, *, force=False, clients=None, credits_window=None, **sources):
    return _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings(credits_window, **sources),
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
        assert out.outcome_key == FileOutcome.PUBLISHED.value  # credit text decides credits alone (rule 6)

    def test_it_publishes_the_credits_alone_q1(self, store, media, find):
        plex = ready_publisher()
        out, _ = _run(ctx_for(store, media), media, {"plex-1": plex}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == ("item-plex-1", [Marker(T.CREDITS, 1_290_250, DUR, ("credits_text",))])

    def test_a_scene_after_the_credits_is_kept_q3(self, store, media, find):
        find.answer = (1_200.0, 1_260.0)  # the roll ends 61 s before the end of the file
        plex = ready_publisher()
        out, _ = _run(ctx_for(store, media), media, {"plex-1": plex}, stage="process")
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
        _run(ctx_for(store, media), media, {"plex-1": plex}, stage="process")
        (decided,) = plex.write.call_args.args[1]
        emby = FakeEmby(str(media))
        emby.publisher = _publisher(emby)
        assert _write(emby, [decided]) == [decided]
        sent = emby.server.put_emby_markers.call_args.kwargs
        assert sent["credits_start_ticks"] == 12_000_000_000 and not any(
            "end" in k for k in sent if k.startswith("credits")
        )
        assert emby.publisher.projection_note([decided], duration_ms=DUR) == CREDITS_BEFORE_END_NOTE

    def test_it_publishes_with_a_servers_own_agreeing_marker_q2(self, store, media, find):
        # End to end: a second server (Intro & Credits off there, so evidence only) serves its own Outro 1.75 s after
        # the text's start; the two agree, and the published start is the text's own (rule 7: never the server's).
        ctx = ctx_for(store, media, server_markers=True)
        ctx.registry.configs_by_id["jellyfin-1"] = server_config(
            "jellyfin-1",
            ServerType.JELLYFIN,
            root=test_pipeline._media_root(media),
            markers={"enabled": False, "library_ids": None},
        )
        ctx.registry.get("jellyfin-1").get_media_segments.return_value = [
            {"Type": "Outro", "StartTicks": 1_292_000 * 10_000, "EndTicks": DUR * 10_000}
        ]
        plex = ready_publisher()
        out, _ = _run(ctx, media, {"plex-1": plex}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        decided = Marker(T.CREDITS, 1_290_250, DUR, ("credits_text", "server_markers"))
        assert plex.write.call_args.args == ("item-plex-1", [decided])
        assert list(_rows_by_server(out)) == ["plex-1"]  # nothing is written to the evidence-only server

    def test_it_publishes_with_an_agreeing_online_source(self, store, media, find):
        clients = _clients(skipdb=LookupResult("ok", (Candidate(T.CREDITS, 1_295_000, None, Source.SKIPDB),)))
        # SkipDB alone never decides credits (rule 6); with the text agreeing it does, and as the first of the two in
        # the source order its start is published.
        ctx = ctx_for(store, media, clients=clients, skipdb=True)
        plex = ready_publisher()
        out, _ = _run(ctx, media, {"plex-1": plex}, stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == (
            "item-plex-1",
            [Marker(T.CREDITS, 1_295_000, DUR, ("skipdb", "credits_text"))],
        )

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

    @staticmethod
    def _stored_by_the_one_step_build(store, media, answer, *, duration, window=None):
        """What a build whose look-back stopped after one step stored: this version's answer, with no basis."""
        one_step = dataclasses.replace(detector.credits_text_spec(), detect=lambda rec, **kwargs: list(answer))
        ctx = _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings(credits_window=window),
                   detectors=(one_step,))  # fmt: skip
        _run(ctx, media, pubs(), probe=_probe(duration=duration), stage="process")
        rec = store.get_file(media)
        assert store.get_detector_run(rec.id, Source.CREDITS_TEXT) is None
        return rec

    @pytest.mark.parametrize(
        ("kind", "answer", "duration", "window", "asked"),
        [
            # An episode on Automatic: the one step starts 570 s before the end, and a later one (never under 30 s) fits
            # only while that is at or after the last 25 % -- from a 38 min episode on.
            ("episode", (), 2_280_000, None, True),
            ("episode", (), 2_279_000, None, False),
            # A 300 s TV window: the one step starts 420 s before the end, so from a 28 min episode on.
            ("episode", (), 1_680_000, {"tv_s": 300}, True),
            ("episode", (), 1_679_000, {"tv_s": 300}, False),
            # No season and not a movie: the movie's 900 s tail, but no 900 s cap -- the last 25 % bounds it, so a 70 min
            # file is asked where a movie of the same length is not.
            ("unknown", (), 4_200_000, None, True),
            ("movie", (), 4_200_000, None, False),
            # A start that was found had story before it within the one step: the steps never change it.
            ("episode", (Candidate(T.CREDITS, 2_000_000, None, Source.CREDITS_TEXT),), 2_700_000, None, False),
            # A movie on Automatic never: its one step is already all before the 900 s cap.
            ("movie", (), 10_800_000, None, False),
            # With a 5 min movie window the one step is 420 s out and the cap still 900 s, so it is read again ...
            ("movie", (), 10_800_000, {"movie_s": 300}, True),
            # ... and with a 30 min one the cap is the window itself, which the tail already starts at.
            ("movie", (), 10_800_000, {"movie_s": 1800}, False),
        ],
    )
    def test_a_nothing_found_from_the_one_step_build_is_read_again_only_where_a_later_step_can_be_read(
        self, request, store, find, kind, answer, duration, window, asked
    ):
        path = request.getfixturevalue({"episode": "media", "movie": "movie", "unknown": "unknown_kind"}[kind])
        rec = self._stored_by_the_one_step_build(store, path, answer, duration=duration, window=window)
        assert (rec.season_key is not None, rec.is_movie) == {"episode": (True, False), "movie": (False, True),
                                                               "unknown": (False, False)}[kind]  # fmt: skip
        version = store.evidence_version(rec.id, Source.CREDITS_TEXT)
        ctx = ctx_for(store, path, credits_window=window)
        out, _ = _run(ctx, path, pubs(), probe=_probe(duration=duration), stage="check")
        assert (out is None) is asked  # None: handed to a worker to decode
        assert find.calls == []
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == version  # no version was bumped to get here

    def test_it_is_read_again_once(self, store, media, find):
        rec = self._stored_by_the_one_step_build(store, media, (), duration=2_700_000)
        find.answer = None
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert len(find.calls) == 1
        assert store.get_detector_run(rec.id, Source.CREDITS_TEXT) == detector.LOOK_BACK_BASIS
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="check")
        assert out is not None and len(find.calls) == 1

    @pytest.mark.parametrize(
        "answer", [(), (Candidate(T.CREDITS, 2_000_000, None, Source.CREDITS_TEXT),)], ids=["nothing-found", "found"]
    )
    def test_every_answer_of_version_3_is_read_again(self, monkeypatch, store, media, find, answer):
        # Version 4 scales every decode path the same way and reads a tail without an answer again at 640x360, so a
        # found start can move as well as a "nothing found". A 2_279_000 ms episode is one the look-back's own re-ask
        # passes over (test_a_nothing_found_from_the_one_step_build_...): only the version sends it back.
        with monkeypatch.context() as patched:
            patched.setattr(detector, "CREDITS_TEXT_VERSION", 3)
            rec = self._stored_by_the_one_step_build(store, media, answer, duration=2_279_000)
            assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == 3
        out, _ = _run(ctx_for(store, media), media, pubs(), probe=_probe(duration=2_279_000), stage="check")
        assert out is None and find.calls == []  # None: handed to a worker to decode

    def test_a_gpu_decode_failure_reaches_the_workers_cpu_rerun(self, store, media, find):
        find.answer = frames.GpuDecodeError("the GPU decoded no frames")
        with pytest.raises(CodecNotSupportedError):
            _run(ctx_for(store, media), media, pubs(), stage="process", gpu="NVIDIA", gpu_device_path="cuda:0")
        find.answer = START_S
        out, _ = _run(ctx_for(store, media), media, pubs(), stage="process", gpu=None, gpu_device_path=None)
        assert find.calls[-1]["gpu"] is None and out.outcome_key == FileOutcome.PUBLISHED.value


class TestAvailability:
    def _stored(self, store, media, find):
        _run(ctx_for(store, media), media, pubs(), stage="process")
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
        ctx = _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings(),
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


@pytest.fixture
def movie(tmp_path):
    folder = tmp_path / "media" / "movies" / "Heat (1995) {imdb-tt0113277}"
    folder.mkdir(parents=True)
    f = folder / "Heat (1995).mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


ambiguous = test_pipeline.ambiguous


@pytest.fixture
def unknown_kind(tmp_path):
    """A file whose path names no id and no episode: neither in a season nor a movie until a server says so."""
    folder = tmp_path / "media" / "other" / "Home Videos"
    folder.mkdir(parents=True)
    f = folder / "Summer Trip.mkv"
    f.write_bytes(b"x" * 100)
    return str(f)


class TestCreditsWindow:
    """The window the user chose reaches the frame extractor, per kind; Automatic is the old 450 s / 900 s."""

    @pytest.fixture(params=["episode", "movie", "unknown_kind"])
    def kind(self, request, media, movie, ambiguous):
        """(kind, path): an SxxEyy path is an episode, a path with an id and no episode is a movie, and a file whose
        show folder only has a tmdb id reads as a movie too until a server says otherwise (unknown kind)."""
        return request.param, {"episode": media, "movie": movie, "unknown_kind": ambiguous}[request.param]

    @pytest.mark.parametrize(
        ("window", "tail_by_kind"),
        [
            (None, {"episode": 450.0, "movie": 900.0, "unknown_kind": 900.0}),
            ({"tv_s": None, "movie_s": None}, {"episode": 450.0, "movie": 900.0, "unknown_kind": 900.0}),
            ({"tv_s": 1200}, {"episode": 1200.0, "movie": 900.0, "unknown_kind": 900.0}),
            ({"movie_s": 1800}, {"episode": 450.0, "movie": 1800.0, "unknown_kind": 1800.0}),
            ({"tv_s": 300, "movie_s": 600}, {"episode": 300.0, "movie": 600.0, "unknown_kind": 600.0}),
        ],
    )
    def test_the_extractor_is_asked_for_the_tail_of_the_files_kind(self, store, find, kind, window, tail_by_kind):
        name, path = kind
        _run(ctx_for(store, path, credits_window=window), path, pubs(), stage="process")
        (call,) = find.calls
        assert call["tail_s"] == tail_by_kind[name]
        assert call["is_episode"] is (name == "episode")

    def test_the_start_the_extractor_reads_from_is_the_end_minus_the_tail(self, store, media, find):
        # End to end down to the frame extractor's own arithmetic: DUR is the file's length in ms.
        ctx = ctx_for(store, media, credits_window={"tv_s": 600})
        _run(ctx, media, pubs(), stage="process")
        tail_s = find.calls[0]["tail_s"]
        assert frames.tail_start_s(DUR, tail_s=tail_s) == DUR / 1000.0 - 600.0

    def test_the_kinds_a_window_does_not_name_are_still_automatic_in_the_stored_version(self, store, media, movie):
        ctx_tv = ctx_for(store, media, credits_window={"tv_s": 600})
        ctx_movie = ctx_for(store, movie, credits_window={"tv_s": 600})
        spec = detector.credits_text_spec()
        ep, mv = (
            store.upsert_file(*_identity(media), duration_ms=DUR, season_key="s", is_movie=False),
            store.upsert_file(*_identity(movie), duration_ms=DUR, season_key=None, is_movie=True),
        )
        assert spec.answer_version(ep, ctx_tv) == detector.CREDITS_TEXT_VERSION + 600_000
        assert spec.answer_version(mv, ctx_movie) == detector.CREDITS_TEXT_VERSION

    def test_automatic_stores_the_answer_under_the_version_it_always_had(self, store, media):
        spec = detector.credits_text_spec()
        rec = store.upsert_file(*_identity(media), duration_ms=DUR, season_key="s", is_movie=False)
        assert detector.CREDITS_TEXT_VERSION == 5
        assert spec.answer_version(rec, ctx_for(store, media)) == 5
        assert spec.answer_version(rec, ctx_for(store, media, credits_window={"tv_s": None, "movie_s": None})) == 5

    def test_an_answer_read_on_another_window_is_read_again_and_stored_under_the_new_one(self, store, media, find):
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert len(find.calls) == 1
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION

        wider = ctx_for(store, media, credits_window={"tv_s": 1200})
        _run(wider, media, pubs(), stage="process")
        assert len(find.calls) == 2 and find.calls[1]["tail_s"] == 1200.0
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION + 1_200_000

    def test_the_answer_for_a_window_is_not_read_again_while_that_window_stays(self, store, media, find):
        window = {"tv_s": 1200}
        _run(ctx_for(store, media, credits_window=window), media, pubs(), stage="process")
        out, _ = _run(ctx_for(store, media, credits_window=window), media, pubs(), stage="check")
        assert out is not None and len(find.calls) == 1

    def test_going_back_to_automatic_reads_the_file_again(self, store, media, find):
        _run(ctx_for(store, media, credits_window={"tv_s": 1200}), media, pubs(), stage="process")
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert len(find.calls) == 2 and find.calls[1]["tail_s"] == 450.0

    def test_an_answer_stored_before_the_setting_existed_is_not_read_again_on_automatic(self, store, media, find):
        # An install that upgrades and keeps Automatic: CREDITS_TEXT_VERSION alone, exactly what a build from before
        # the setting stored.
        _run(ctx_for(store, media), media, pubs(), stage="process")
        assert store.evidence_version(store.get_file(media).id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION
        out, _ = _run(ctx_for(store, media, credits_window={"tv_s": None}), media, pubs(), stage="check")
        assert out is not None and len(find.calls) == 1

    def test_changing_the_window_of_the_other_kind_does_not_read_a_file_again(self, store, media, find):
        _run(ctx_for(store, media), media, pubs(), stage="process")
        out, _ = _run(ctx_for(store, media, credits_window={"movie_s": 1800}), media, pubs(), stage="check")
        assert out is not None and len(find.calls) == 1


def _identity(path):
    import os

    from media_preview_generator.markers.models import FileIdentity

    st = os.stat(path)
    return (FileIdentity(path, st.st_size, st.st_mtime_ns),)


class TestMovieWindowReachesTheDecision:
    LONG_MOVIE_MS = 10_800_000
    START_S = 9_300.0  # 1500 s before the end: past the default 900 s cap, inside a 30 min window

    @pytest.mark.parametrize(
        ("window", "published"), [(None, False), ({"movie_s": 1200}, False), ({"movie_s": 1800}, True)]
    )
    def test_a_movie_credits_start_beyond_15_min_publishes_only_within_the_window(
        self, store, movie, find, window, published
    ):
        find.answer = self.START_S
        plex = ready_publisher()
        ctx = ctx_for(store, movie, credits_window=window)
        out, _ = _run(ctx, movie, {"plex-1": plex}, probe=_probe(duration=self.LONG_MOVIE_MS), stage="process")
        assert (out.outcome_key == FileOutcome.PUBLISHED.value) is published
        if published:
            assert plex.write.call_args.args == (
                "item-plex-1",
                [Marker(T.CREDITS, 9_300_000, self.LONG_MOVIE_MS, ("credits_text",))],
            )

    def test_a_movie_window_under_15_min_keeps_the_900_s_floor(self, store, movie, find):
        # The cap is max(900 s, window): a small window must not lower it, or a credits start 800 s out (found by
        # another source) would be refused although nothing changed for someone who narrowed the window.
        find.answer = 10_000.0  # 800 s before the end of a 3 h movie
        plex = ready_publisher()
        ctx = ctx_for(store, movie, credits_window={"movie_s": 300})
        out, _ = _run(ctx, movie, {"plex-1": plex}, probe=_probe(duration=self.LONG_MOVIE_MS), stage="process")
        assert out.outcome_key == FileOutcome.PUBLISHED.value


class TestTvWindowReachesTheDecision:
    """The last-25% rule yields to a TV window the user chose: without it a wider window would decode more and then
    have its answer discarded."""

    START_S = 820.0  # 500 s before the end of the 22 min episode: before its last 25% (330 s)

    @pytest.mark.parametrize(("window", "published"), [(None, False), ({"tv_s": 300}, False), ({"tv_s": 600}, True)])
    def test_a_credits_start_before_the_last_quarter_publishes_only_within_the_window(
        self, store, media, find, window, published
    ):
        find.answer = self.START_S
        plex = ready_publisher()
        ctx = ctx_for(store, media, credits_window=window)
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(duration=DUR), stage="process")
        assert (out.outcome_key == FileOutcome.PUBLISHED.value) is published
        if published:
            assert plex.write.call_args.args == (
                "item-plex-1",
                [Marker(T.CREDITS, 820_000, DUR, ("credits_text",))],
            )


class TestSkipDbAgainstACreditsChapter:
    """Spec §5.5 rule 3 (2026-09-25 audit, Somebody Somewhere S03): a SkipDB answer against a credits chapter nothing
    else agrees with has credit text read the file in the same run (SkipDB is asked before credit text); credit text
    agreeing with SkipDB outvotes the chapter at its own start, agreeing with the chapter keeps it. Where credit text
    can't run here and has stored nothing, the chapter decides as before: nothing would ever answer."""

    CHAPTERS = (Chapter(0, 1_200_000, "Episode"), Chapter(1_200_000, None, "Credits"))
    SKIPDB = LookupResult("ok", (Candidate(T.CREDITS, 1_140_000, DUR, Source.SKIPDB),))

    def _run(self, store, media, ctx):
        plex = ready_publisher()
        out, _ = _run(ctx, media, {"plex-1": plex}, probe=_probe(self.CHAPTERS), stage="process")
        return out, plex

    @pytest.mark.parametrize(
        ("text_s", "published"),
        [
            (1_141.0, Marker(T.CREDITS, 1_141_000, DUR, ("skipdb", "credits_text"))),
            (1_201.0, Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))),
        ],
        ids=["text-agrees-with-skipdb", "text-agrees-with-the-chapter"],
    )
    def test_credit_text_is_read_and_settles_it(self, store, media, find, text_s, published):
        find.answer = text_s
        out, plex = self._run(store, media, ctx_for(store, media, clients=_clients(skipdb=self.SKIPDB), skipdb=True))
        assert len(find.calls) == 1
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == ("item-plex-1", [published])

    def test_credit_text_that_finds_no_roll_leaves_the_chapter_deciding(self, store, media, find):
        # It answered and found nothing: nothing more will come, so the chapter isn't left waiting.
        find.answer = None
        out, plex = self._run(store, media, ctx_for(store, media, clients=_clients(skipdb=self.SKIPDB), skipdb=True))
        assert len(find.calls) == 1
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == ("item-plex-1", [Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))])
        again, _ = self._run(store, media, ctx_for(store, media, clients=_clients(skipdb=self.SKIPDB), skipdb=True))
        assert len(find.calls) == 1 and again.outcome_key == FileOutcome.UP_TO_DATE.value

    @pytest.mark.parametrize(
        ("stored_version", "status", "reason"),
        [
            (detector.CREDITS_TEXT_VERSION, DecisionStatus.DECIDED, "chapters"),
            (detector.CREDITS_TEXT_VERSION - 1, DecisionStatus.NEEDS_REVIEW, TEXT_CHECKS_CHAPTER_REASON),
        ],
        ids=["this-version", "older-version"],
    )
    def test_only_this_versions_answer_that_found_no_roll_leaves_the_chapter_deciding(
        self, store, media, find, stored_version, status, reason
    ):
        # An older version's answer is read again (``pipeline._detector_pending``), so the chapter waits for it.
        find.answer = None
        ctx = ctx_for(store, media, clients=_clients(skipdb=self.SKIPDB), skipdb=True)
        self._run(store, media, ctx)
        rec = store.get_file(media)
        store.replace_evidence(rec.id, Source.CREDITS_TEXT, [], version=stored_version)

        decision = pipeline._decide(ctx, rec, frozenset({T.CREDITS}), None)[T.CREDITS]

        assert (decision.status, decision.reason) == (status, reason)

    CHAPTER = Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))
    WAITING = (DecisionStatus.NEEDS_REVIEW, TEXT_CHECKS_CHAPTER_REASON)
    DECIDED_BY_THE_CHAPTER = (DecisionStatus.DECIDED, "chapters")

    def _ctx(self, store, media):
        return ctx_for(store, media, clients=_clients(skipdb=self.SKIPDB), skipdb=True)

    def _credits(self, ctx, store, media):
        decision = pipeline._decide(ctx, store.get_file(media), frozenset({T.CREDITS}), None)[T.CREDITS]
        return decision.status, decision.reason

    def _checked(self, store, media):
        """The check stage stores the chapter and SkipDB's answer, and hands credit text to a worker."""
        ctx = self._ctx(store, media)
        out, _ = _run(ctx, media, {"plex-1": ready_publisher()}, probe=_probe(self.CHAPTERS), stage="check")
        assert out is None
        return ctx

    @pytest.mark.parametrize("failure", [None, "decode error", "timeout"])
    def test_a_failure_recorded_for_the_file_as_it_is_ends_the_wait(self, store, media, find, failure):
        # The owner's rule: decisions are automatic, never an open-ended wait in Needs review. Credit text that can't
        # read this file won't answer the next run either, so the chapter decides as it did before rule 3.
        ctx = self._checked(store, media)
        rec = store.get_file(media)
        if failure == "decode error":
            store.set_detector_failure(rec.id, Source.CREDITS_TEXT, "ffmpeg exited 1")
        elif failure == "timeout":
            now = ctx.now()
            store.record_credits_text_timeout(
                FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns),
                now,
                forget_before=now - detector.TIMEOUT_RETRY,
            )

        assert self._credits(ctx, store, media) == (self.WAITING if failure is None else self.DECIDED_BY_THE_CHAPTER)

    @pytest.mark.parametrize(
        "error",
        [frames.FrameDecodeError("ffmpeg exited 1"), frames.DecodeTimeoutError("decoding timed out after 600 s")],
        ids=["decode-error", "timeout"],
    )
    def test_a_read_that_fails_lets_the_chapter_decide_on_the_same_run_and_a_new_file_waits_again(
        self, store, media, find, error
    ):
        find.answer = error
        out, plex = self._run(store, media, self._ctx(store, media))

        assert len(find.calls) == 1
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == ("item-plex-1", [self.CHAPTER])
        rec = store.get_file(media)
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) is None  # still no answer of its own

        with open(media, "ab") as fh:  # replaced: the failure was the old file's
            fh.write(b"more")
        ctx = self._checked(store, media)
        assert self._credits(ctx, store, media) == self.WAITING

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (frames.FrameDecodeError("ffmpeg exited 1"), DECIDED_BY_THE_CHAPTER),
            (detector.TextDetUnavailableError("helper gone"), WAITING),  # nothing about the file: read next run
        ],
        ids=["decode-error", "text-detection-down"],
    )
    def test_an_older_answer_read_again_without_an_answer(self, store, media, find, error, expected):
        # An older version's "nothing" is no answer (it is read again), so it can't end the wait; a failure to read
        # the file can.
        find.answer = None
        self._run(store, media, self._ctx(store, media))
        rec = store.get_file(media)
        store.replace_evidence(rec.id, Source.CREDITS_TEXT, [], version=detector.CREDITS_TEXT_VERSION - 1)
        find.answer = error

        self._run(store, media, self._ctx(store, media))

        assert len(find.calls) == 2
        assert store.evidence_version(rec.id, Source.CREDITS_TEXT) == detector.CREDITS_TEXT_VERSION - 1
        decision = store.get_decisions(rec.id)[T.CREDITS]
        assert (decision.status, decision.reason) == expected

    def test_without_credit_text_here_the_chapter_decides(self, store, media, find):
        ctx = _ctx(store, _registry(media, ServerType.PLEX), settings_raw=settings(skipdb=True),
                   clients=_clients(skipdb=self.SKIPDB), detectors=())  # fmt: skip
        ctx.credits_text = TextDetState.ABSENT
        out, plex = self._run(store, media, ctx)
        assert find.calls == []
        assert out.outcome_key == FileOutcome.PUBLISHED.value
        assert plex.write.call_args.args == ("item-plex-1", [Marker(T.CREDITS, 1_200_000, DUR, ("chapters",))])
