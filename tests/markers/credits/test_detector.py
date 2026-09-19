"""The credit text detector: which decodes it asks for, what it stores, and how failures leave the detector."""

from __future__ import annotations

import math
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from media_preview_generator.markers.credits import detector, frames, rule_j
from media_preview_generator.markers.credits.textdet_helper import TextDetUnavailableError
from media_preview_generator.markers.models import Candidate, FileIdentity, MarkerType, Source
from media_preview_generator.markers.pipeline import DetectorUnavailableError
from media_preview_generator.markers.probe import MediaProbe, ProbeStalledError, ProbeTimeoutError
from media_preview_generator.markers.store import FileRecord, MarkerStore
from media_preview_generator.processing.generator import CodecNotSupportedError
from media_preview_generator.servers.base import ServerType
from tests.markers import test_pipeline
from tests.markers.fakes import ready_publisher

media = test_pipeline.media
store = test_pipeline.store
MOVIE = FileRecord(7, "/media/movies/Movie (2020)/Movie (2020).mkv", 100, 1, 6_000_000, None, True)
EPISODE = FileRecord(
    8, "/media/tv/Show/Season 01/Show - S01E01.mkv", 100, 1, 1_320_000, "/media/tv/Show/Season 01", False
)
STORY = [(5100.0 + 2 * i, 0, 120.0) for i in range(300)]  # 5100–5698 s, bright, no text
ROLL = [(5700.0 + 2 * i, 2, 12.0) for i in range(100)]  # 5700–5898 s, dark cards
FINE = [(float(t), 0, 120.0) for t in range(5680, 5690)] + [(float(t), 2, 12.0) for t in range(5690, 5702)]
SCENE = [(5900.0 + 2 * i, 0, 120.0) for i in range(50)]  # 5900–5998 s: a scene after the roll
END = [(5897.0, 2, 12.0), (5898.0, 2, 12.0), (5899.0, 1, 12.0)] + [(float(t), 0, 120.0) for t in range(5900, 5918)]


def count(planes):
    return [0] * len(planes)


class Decodes:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def __call__(self, path, **kwargs):
        self.calls.append({"path": path, **kwargs})
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


START_TIME_S = 1.4  # the container's own first timestamp, as ffprobe gives it


class _Probes(list):
    """Every probe call's arguments; the start time probe answers ``START_TIME_S``, the packet probe ``thinning``."""

    thinning = frames.KeyframeThinning()
    thinning_calls: list[dict]


@pytest.fixture
def probes(monkeypatch):
    calls = _Probes()
    calls.thinning_calls = []

    def probe(path, ffmpeg, **kwargs):
        calls.append({"path": path, "ffmpeg": ffmpeg, **kwargs})
        return START_TIME_S

    def keyframe_thinning(path, ffmpeg, **kwargs):
        calls.thinning_calls.append({"path": path, "ffmpeg": ffmpeg, **kwargs})
        return calls.thinning

    monkeypatch.setattr(detector.frames, "container_start_s", probe)
    monkeypatch.setattr(detector.frames, "keyframe_thinning", keyframe_thinning)
    return calls


class TestFindCredits:
    def test_a_file_without_a_roll_decodes_only_the_tail(self, monkeypatch, probes):
        decodes = Decodes(STORY)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0", phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.fine_rows, result.end_rows) == (None, None, (), ())
        (call,) = decodes.calls
        assert call == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                        "keyframes_only": True, "fps": None, "gpu": "NVIDIA", "gpu_device_path": "cuda:0",
                        "count_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S,
                        "keep_every": None, "drop_non_key": False}  # fmt: skip
        assert phases == ["Reading the credits…"]
        assert probes == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": None}]
        assert probes.thinning_calls == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": None}]

    def test_a_roll_to_the_end_of_the_file_is_refined_before_it_and_left_open_ended(self, monkeypatch, probes):
        cancel = lambda: False  # noqa: E731
        decodes = Decodes(STORY + ROLL, FINE)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        # The roll's last keyframe is at 5898 s and the file ends at 5910 s: no scene follows, no end decode (Q3).
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu=None, gpu_device_path=None, cancel_check=cancel,
                                       phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s) == (rule_j.credits_start(STORY + ROLL, FINE), None) == (5690.0, None)
        assert len(decodes.calls) == 2
        # Whole-dict equality, like the tail decode above: every kwarg the refine decode forwards is the detector's to
        # get right. Every decode gets the start time probed once in this run: a stale or defaulted one would put a
        # recording's refine rows tens of thousands of seconds out and silently drop the refinement (Task 6).
        assert decodes.calls[1] == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5680.0,
                                    "length_s": 21.0, "keyframes_only": False, "fps": 1, "gpu": None,
                                    "gpu_device_path": None, "count_boxes": count, "cancel_check": cancel,
                                    "start_time_s": START_TIME_S}  # fmt: skip
        tail = decodes.calls[0]
        assert (tail["start_time_s"], tail["keep_every"], tail["drop_non_key"]) == (START_TIME_S, None, False)
        assert probes == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": cancel}]
        assert probes.thinning_calls == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": cancel}]
        assert phases == ["Reading the credits…", "Refining the credits start…"]

    def test_a_scene_after_the_roll_is_read_for_where_the_credits_end(self, monkeypatch, probes):
        decodes = Decodes(STORY + ROLL + SCENE, FINE, END)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0", phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.end_rows) == (5690.0, 5899.0, tuple(END))
        # Both refine decodes by whole-dict equality: they stay on the worker's GPU (a refine decode quietly dropped to
        # the CPU would raise FrameDecodeError instead of the GpuDecodeError the worker's CPU rerun needs), and all three
        # decodes read the one start time this run probed.
        gpu_kwargs = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "keyframes_only": False, "fps": 1,
                      "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "count_boxes": count, "cancel_check": None,
                      "start_time_s": START_TIME_S}  # fmt: skip
        assert decodes.calls[1] == {**gpu_kwargs, "start_s": 5680.0, "length_s": 21.0}
        assert decodes.calls[2] == {**gpu_kwargs, "start_s": 5897.0, "length_s": 21.0}
        assert phases == ["Reading the credits…", "Refining the credits start…", "Finding where the credits end…"]
        assert len(probes) == 1

    def test_text_on_screen_all_through_the_tail_is_no_answer_and_nothing_more_is_decoded(self, monkeypatch, probes):
        # A running timecode on every keyframe (1 box on a lit frame), reading 3 boxes now and then after the first
        # 100 s: rule J finds a run 100 s into the tail, but every keyframe before it carries text, so there is no roll
        # and no refine window is decoded.
        timecode = [(5100.0 + 2 * i, 3 if i >= 50 and i % 5 == 0 else 1, 120.0) for i in range(450)]
        decodes = Decodes(timecode)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert rule_j.coarse_start(timecode).pts_s == 5200.0
        assert (result.start_s, result.end_s, result.fine_rows, result.end_rows) == (None, None, (), ())
        assert len(decodes.calls) == 1

    def test_the_end_window_is_read_around_the_rolls_last_card_when_scene_text_joined_the_run(
        self, monkeypatch, probes
    ):
        # Spec §13 item 13: a lit text frame in the scene, 12 s after the roll's last card, joins the run. The end window
        # is decoded from the roll's last card (5898 s) to 20 s past the scene's text (5910 s): whether there is an end
        # is decided from the latest credit keyframe exactly as before, and the skip then stops on the roll.
        glued = [
            (5900.0, 0, 120.0),
            (5904.0, 0, 120.0),
            (5910.0, 3, 120.0),
            *[(5912.0 + 2 * i, 0, 120.0) for i in range(40)],
        ]
        decodes = Decodes(STORY + ROLL + glued, FINE, END)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        coarse = rule_j.coarse_start(STORY + ROLL + glued)
        assert rule_j.coarse_end_s(STORY + ROLL + glued, coarse) == 5910.0
        assert (decodes.calls[2]["start_s"], decodes.calls[2]["length_s"]) == (5897.0, 5910.0 + 20.0 - 5897.0)
        assert result.end_s == 5899.0

    def test_the_decoded_windows_cover_everything_the_refinements_read(self, monkeypatch, probes):
        # Rule J may read [coarse − REFINE_BEFORE_S, coarse + REFINE_AFTER_S] for the start and
        # [end − REFINE_END_BEFORE_S, end + REFINE_END_AFTER_S] for the end. With 1 fps credit frames everywhere that was
        # decoded and nowhere else, both walks reach the far edge of what rule J reads only if the decode covered it.
        def decode(path, *, start_s, length_s, keyframes_only, **kwargs):
            if keyframes_only:
                return STORY + ROLL + SCENE
            return [(float(t), 2, 20.0) for t in range(math.ceil(start_s), math.floor(start_s + length_s) + 1)]

        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        coarse = rule_j.coarse_start(STORY + ROLL + SCENE)
        assert result.start_s == coarse.pts_s - rule_j.REFINE_BEFORE_S
        assert result.end_s == rule_j.coarse_end_s(STORY + ROLL + SCENE, coarse) + rule_j.REFINE_END_AFTER_S

    @pytest.mark.parametrize(
        ("keep_every", "drop_non_key"), [(48, False), (None, True), (48, True)], ids=["intra-only", "vp9", "vp9-intra-only"]
    )  # fmt: skip
    @pytest.mark.parametrize(("gpu", "device"), [("NVIDIA", "cuda:0"), (None, None)], ids=["gpu", "cpu"])
    def test_only_the_keyframe_pass_is_thinned(self, monkeypatch, probes, keep_every, drop_non_key, gpu, device):
        # An intra-only stream's keyframe pass decodes one packet in the stride (one per 2 s, the spacing rule J was
        # measured at); a VP9 stream's drops the packets that aren't keyframes, as its decoder ignores -skip_frame. The
        # refine windows must see every frame and stay as they are: thinned, a 1 fps window of a 24 fps file would keep
        # one frame in 48 seconds, and a VP9 one only its keyframes.
        probes.thinning = frames.KeyframeThinning(keep_every, drop_non_key)
        decodes = Decodes(STORY + ROLL + SCENE, FINE, END)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu=gpu, gpu_device_path=device)  # fmt: skip
        assert (result.start_s, result.end_s) == (5690.0, 5899.0)
        assert decodes.calls[0] == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                                    "keyframes_only": True, "fps": None, "gpu": gpu, "gpu_device_path": device,
                                    "count_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S,
                                    "keep_every": keep_every, "drop_non_key": drop_non_key}  # fmt: skip
        refine = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "keyframes_only": False, "fps": 1, "gpu": gpu,
                  "gpu_device_path": device, "count_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S}  # fmt: skip
        assert decodes.calls[1:] == [{**refine, "start_s": 5680.0, "length_s": 21.0},
                                     {**refine, "start_s": 5897.0, "length_s": 21.0}]  # fmt: skip
        assert probes.thinning_calls == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": None}]

    def test_a_keyframe_pass_with_no_frames_is_no_roll_and_nothing_more_is_decoded(self, monkeypatch, probes):
        # A VP9 file whose container flags no packet in the tail as a keyframe: every packet is dropped before the
        # decoder and ffmpeg exits 0 with no frames (measured on 8.1.2). On the CPU that is a tail without a roll, like
        # any file without a keyframe in its tail; on the GPU run_decode calls it a GPU failure, so the worker's CPU
        # rerun reaches this (TestDetect.test_a_gpu_decode_failure_is_a_codec_error_for_the_workers_cpu_rerun).
        probes.thinning = frames.KeyframeThinning(None, True)
        decodes = Decodes([])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       count_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert result == detector.CreditsTextResult(None, None, (), (), ())
        assert len(decodes.calls) == 1 and decodes.calls[0]["drop_non_key"] is True

    def test_a_cancelled_job_is_not_probed_or_decoded(self, monkeypatch):
        monkeypatch.setattr(detector.frames, "probe_media", lambda path, **kwargs: pytest.fail("probed anyway"))
        monkeypatch.setattr(detector.frames, "decode_rows", lambda path, **kwargs: pytest.fail("decoded anyway"))
        with pytest.raises(frames.DecodeCancelledError, match="cancelled before decoding"):
            detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                  count_boxes=count, gpu=None, gpu_device_path=None, cancel_check=lambda: True)  # fmt: skip

    def test_an_episode_reads_the_last_450_s(self, monkeypatch, probes):
        decodes = Decodes([])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                              count_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert decodes.calls[0]["start_s"] == 870.0

    def test_a_roll_less_than_30_s_into_the_tail_gets_no_answer(self, monkeypatch, probes):
        # A file shorter than its tail whose roll starts 10 s in. With under 30 s of the tail before the run, rule J
        # can't tell it from text on screen from the first frame (rule_j.STORY_BEFORE_RUN_S), so there is no answer and
        # no refine window; version 1 refined it from 0 s.
        roll = [(10.0 + 2 * i, 2, 12.0) for i in range(20)]
        decodes = Decodes(roll, [])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(
            "/m/short.mkv",
            duration_ms=50_000,
            is_episode=False,
            ffmpeg="/ff",
            count_boxes=count,
            gpu=None,
            gpu_device_path=None,
        )
        assert rule_j.coarse_start(roll).pts_s == 10.0
        assert (result.start_s, result.end_s) == (None, None)
        assert len(decodes.calls) == 1


class FakePool:
    def __init__(self):
        self.calls = []

    def count_boxes(self, planes, *, gpu, gpu_device_path):
        self.calls.append((gpu, gpu_device_path))
        return [0] * len(planes)


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(detector, "get_textdet_pool", lambda: fake)
    return fake


NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path):
    store = MarkerStore(str(tmp_path / "markers.db"))
    clock = SimpleNamespace(now=NOW)
    # run_memo as PipelineContext answers outside a run of the file: a fresh dict every call, nothing kept.
    yield SimpleNamespace(config=SimpleNamespace(ffmpeg_path="/usr/lib/jellyfin-ffmpeg/ffmpeg"), force=False, store=store,
                          now=lambda: clock.now, clock=clock, run_memo=lambda path: {})  # fmt: skip
    store.close()


class TestDetect:
    def _find(self, monkeypatch, answer):
        seen: list[dict] = []

        def find(path, **kwargs):
            seen.append({"path": path, **kwargs})
            if isinstance(answer, BaseException):
                raise answer
            start, end = answer if isinstance(answer, tuple) else (answer, None)
            return detector.CreditsTextResult(start, end, (), (), ())

        monkeypatch.setattr(detector, "find_credits", find)
        return seen

    def test_a_roll_to_the_end_becomes_one_credits_candidate_without_an_end(self, monkeypatch, pool, ctx):
        seen = self._find(monkeypatch, 5690.4996)
        phase = []
        cancel = lambda: False  # noqa: E731 — identity is asserted: the detector must forward this exact callable
        assert detector.detect_credits_text(
            MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0", phase_callback=phase.append, cancel_check=cancel
        ) == [Candidate(MarkerType.CREDITS, 5_690_500, None, Source.CREDITS_TEXT)]
        (call,) = seen
        assert (call["path"], call["duration_ms"], call["is_episode"], call["ffmpeg"], call["gpu"], call["gpu_device_path"]) == (
            MOVIE.canonical_path, 6_000_000, False, "/usr/lib/jellyfin-ffmpeg/ffmpeg", "NVIDIA", "cuda:0")  # fmt: skip
        assert call["cancel_check"] is cancel
        # Call it rather than checking it is set: a phase callback wired to something else would leave the worker row
        # showing the pipeline's last text for the whole decode. Equality, not identity — ``phase.append`` is a fresh
        # bound method on every access.
        call["phase"](detector.READING_PHASE)
        assert phase == [detector.READING_PHASE]
        call["count_boxes"](frames.np.zeros((2, 180, 320), frames.np.uint8))
        assert pool.calls == [("NVIDIA", "cuda:0")]

    def test_a_scene_after_the_roll_gives_the_candidate_its_end(self, monkeypatch, pool, ctx):
        self._find(monkeypatch, (5690.4996, 5899.0004))
        assert detector.detect_credits_text(MOVIE, ctx=ctx) == [
            Candidate(MarkerType.CREDITS, 5_690_500, 5_899_000, Source.CREDITS_TEXT)
        ]

    @pytest.mark.parametrize(
        ("rec", "episode"),
        [
            (EPISODE, True),
            (MOVIE, False),
            # Neither a movie nor in a season (a "Pilot" file on its own): read as a movie. Only this cell tells
            # ``season_key is not None`` from ``not is_movie``.
            (FileRecord(9, "/m/Some Show - Pilot.mkv", 100, 1, 1_320_000, None, False), False),
        ],
    )
    def test_only_a_file_in_a_season_is_read_as_an_episode(self, monkeypatch, pool, ctx, rec, episode):
        seen = self._find(monkeypatch, None)
        assert detector.detect_credits_text(rec, ctx=ctx) == []
        assert seen[0]["is_episode"] is episode

    def test_a_gpu_decode_failure_is_a_codec_error_for_the_workers_cpu_rerun(self, monkeypatch, pool, ctx):
        self._find(monkeypatch, frames.GpuDecodeError("the GPU decoded no frames from Movie (2020).mkv"))
        with pytest.raises(CodecNotSupportedError, match="no frames"):
            detector.detect_credits_text(MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0")

    @pytest.mark.parametrize(
        ("error", "message"),
        [
            (frames.FrameDecodeError("ffmpeg exited 1 decoding Movie (2020).mkv on the CPU"), "exited 1"),
            (frames.DecodeCancelledError("cancelled while decoding"), "cancelled"),
            (TextDetUnavailableError("Text detection failed: the helper exited"), "Text detection failed"),
        ],
    )
    def test_other_failures_are_no_answer_this_time_and_not_remembered(self, monkeypatch, pool, ctx, error, message):
        self._find(monkeypatch, error)
        with pytest.raises(DetectorUnavailableError, match=message):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert (
            ctx.store.credits_text_timed_out_at(FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)) is None
        )

    def test_a_timed_out_decode_waits_a_day_unless_forced_or_the_file_changes(self, monkeypatch, pool, ctx):
        seen = self._find(monkeypatch, frames.DecodeTimeoutError("decoding Movie (2020).mkv timed out after 600 s"))
        with pytest.raises(DetectorUnavailableError, match="timed out after 600 s"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert (
            ctx.store.credits_text_timed_out_at(FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)) == NOW
        )
        # A second path that timed out around the same time: recording MOVIE's next timeout must not sweep it away
        # (``forget_before`` is what the SUT controls here — passing ``now`` instead of ``now - TIMEOUT_RETRY`` would
        # delete every other path's still-live entry on every new timeout).
        other = FileIdentity("/media/movies/Other (2019)/Other (2019).mkv", 200, 2)
        ctx.store.record_credits_text_timeout(other, NOW, forget_before=NOW - detector.TIMEOUT_RETRY)
        ctx.clock.now = NOW + timedelta(hours=23)
        with pytest.raises(DetectorUnavailableError, match="less than a day ago"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert len(seen) == 1  # not decoded again
        changed = FileRecord(
            MOVIE.id, MOVIE.canonical_path, MOVIE.size + 1, MOVIE.mtime_ns, MOVIE.duration_ms, None, True
        )
        for rec, force in ((changed, False), (MOVIE, True)):
            ctx.force = force
            with pytest.raises(DetectorUnavailableError, match="timed out after"):
                detector.detect_credits_text(rec, ctx=ctx)
        assert len(seen) == 3  # each timed out again, so the latest entry is from 23 h
        assert ctx.store.credits_text_timed_out_at(other) == NOW  # the other path's entry survived
        ctx.force, ctx.clock.now = False, NOW + timedelta(hours=23) + timedelta(days=1, minutes=1)
        with pytest.raises(DetectorUnavailableError, match="timed out after"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert len(seen) == 4

    def test_a_start_time_probe_that_times_out_waits_a_day_like_a_decode(self, monkeypatch, pool, ctx):
        # The real find_credits: only ffprobe is replaced, stalled on a mount.
        def stalled(path, **kwargs):
            raise ProbeTimeoutError(f"ffprobe failed for {path}: TimeoutExpired")

        monkeypatch.setattr(frames, "probe_media", stalled)
        monkeypatch.setattr(frames, "decode_rows", lambda path, **kwargs: pytest.fail("decoded anyway"))
        with pytest.raises(DetectorUnavailableError, match="reading the start time of Movie \\(2020\\).mkv timed out"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        identity = FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)
        assert ctx.store.credits_text_timed_out_at(identity) == NOW
        assert detector.credits_text_needs_worker(MOVIE, ctx) is False  # settled on the checking thread for a day

    def test_a_packet_probe_that_times_out_waits_a_day_like_a_decode(self, monkeypatch, pool, ctx):
        # The real find_credits and intra-only check: the start time is read, then ffprobe stalls on the packets.
        def packets_stalled(path, **kwargs):
            raise ProbeTimeoutError(f"ffprobe failed for {path}: TimeoutExpired")

        monkeypatch.setattr(frames, "probe_media", lambda path, **kwargs: MediaProbe(6_000_000, ()))
        monkeypatch.setattr(frames, "video_packets", packets_stalled)
        monkeypatch.setattr(frames, "decode_rows", lambda path, **kwargs: pytest.fail("decoded anyway"))
        with pytest.raises(
            DetectorUnavailableError, match="reading the video packets of Movie \\(2020\\).mkv timed out"
        ):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        identity = FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)
        assert ctx.store.credits_text_timed_out_at(identity) == NOW
        assert detector.credits_text_needs_worker(MOVIE, ctx) is False

    def test_a_packet_probe_not_started_for_earlier_stuck_ones_records_nothing(self, monkeypatch, pool, ctx):
        # The mount's fault, not this file's: no answer this run, and the file isn't left alone for a day.
        def gated(path, **kwargs):
            raise ProbeStalledError(f"Not reading {path}: 2 earlier ffprobes are still stuck reading their files")

        monkeypatch.setattr(frames, "probe_media", lambda path, **kwargs: MediaProbe(6_000_000, ()))
        monkeypatch.setattr(frames, "video_packets", gated)
        monkeypatch.setattr(frames, "decode_rows", lambda path, **kwargs: pytest.fail("decoded anyway"))
        with pytest.raises(DetectorUnavailableError, match="could not read the video packets of Movie \\(2020\\).mkv"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        identity = FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)
        assert ctx.store.credits_text_timed_out_at(identity) is None

    def test_an_unknown_duration_is_no_answer(self, pool, ctx):
        rec = FileRecord(9, "/m/x.mkv", 1, 1, None, None, True)
        with pytest.raises(DetectorUnavailableError, match="duration"):
            detector.detect_credits_text(rec, ctx=ctx)


def test_spec_carries_the_version_and_asks_whether_a_worker_is_needed():
    spec = detector.credits_text_spec()
    assert (spec.source, spec.types, spec.version) == (
        Source.CREDITS_TEXT,
        frozenset({MarkerType.CREDITS}),
        detector.CREDITS_TEXT_VERSION,
    )
    assert (spec.stored_sources, spec.due, spec.needs_worker, spec.followups) == (
        frozenset({Source.CREDITS_TEXT}),
        None,
        detector.credits_text_needs_worker,
        None,
    )


class TestNeedsWorker:
    """A file the detector would give up on at once is answered on the checking thread: a worker (one by default) is
    held only for a real decode, not for a day of ``DetectorUnavailableError`` after a timeout."""

    def _timed_out(self, ctx, rec, at):
        ctx.store.record_credits_text_timeout(
            FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns), at, forget_before=at - detector.TIMEOUT_RETRY
        )

    @pytest.mark.parametrize(
        ("duration_ms", "timed_out_ago", "force", "expected"),
        [
            (6_000_000, None, False, True),  # never timed out: decode on a worker
            (None, None, False, False),  # no duration: gives up at once
            (6_000_000, timedelta(hours=23), False, False),  # timed out lately: gives up at once
            (6_000_000, timedelta(hours=23), True, True),  # a forced re-detect decodes again
            (6_000_000, timedelta(days=1), False, True),  # a day on, it is decoded again
        ],
        ids=["fresh", "no-duration", "timed-out-lately", "forced", "a-day-later"],
    )
    def test_only_a_file_that_will_be_decoded_takes_a_worker(self, ctx, duration_ms, timed_out_ago, force, expected):
        rec = FileRecord(MOVIE.id, MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns, duration_ms, None, True)
        if timed_out_ago is not None:
            self._timed_out(ctx, rec, NOW - timed_out_ago)
        ctx.force = force
        assert detector.credits_text_needs_worker(rec, ctx) is expected

    def test_another_identity_of_the_path_still_takes_a_worker(self, ctx):
        self._timed_out(ctx, MOVIE, NOW - timedelta(hours=1))
        replaced = FileRecord(MOVIE.id, MOVIE.canonical_path, MOVIE.size + 1, 2, MOVIE.duration_ms, None, True)
        assert detector.credits_text_needs_worker(replaced, ctx) is True


class TestCheckStage:
    """The pipeline's check stage with the real spec: the hand-off decision is ``needs_worker``'s."""

    def _ctx(self, store, media, *, force=False):
        return test_pipeline._ctx(
            store,
            test_pipeline._registry(media, ServerType.PLEX),
            settings_raw={
                "detect": {"intro": False, "credits": True},
                "publish_when": "medium",
                "sources": [{"id": "credits_text", "enabled": True}],
            },
            detectors=(detector.credits_text_spec(),),
            force=force,
        )

    def _time_out(self, store, media, ctx):
        st = os.stat(media)
        at = ctx.now() - timedelta(hours=1)
        store.record_credits_text_timeout(
            FileIdentity(media, st.st_size, st.st_mtime_ns), at, forget_before=at - detector.TIMEOUT_RETRY
        )

    def test_a_file_that_timed_out_lately_is_settled_without_a_worker(self, store, media, monkeypatch):
        decoded: list[str] = []
        monkeypatch.setattr(detector, "find_credits", lambda path, **kwargs: decoded.append(path))
        ctx = self._ctx(store, media)
        self._time_out(store, media, ctx)
        out, _ = test_pipeline._run(ctx, media, {"plex-1": ready_publisher()}, stage="check")
        assert out is not None  # None would hand the file to a worker only to give up there
        assert decoded == []

    def test_the_hand_off_and_the_detector_read_one_verdict(self, store, media, monkeypatch):
        # The day after a timeout can end between the hand-off check and the detector's own guard. Read twice, the
        # second read would say "decode" on the checking thread; the verdict is read once per run of the file.
        answers = iter([True, False])
        monkeypatch.setattr(detector, "_timed_out_lately", lambda rec, ctx: next(answers))
        monkeypatch.setattr(detector, "find_credits", lambda path, **kwargs: pytest.fail("decoded on the check thread"))
        out, _ = test_pipeline._run(self._ctx(store, media), media, {"plex-1": ready_publisher()}, stage="check")
        assert out is not None

    def test_a_forced_run_of_it_still_goes_to_a_worker(self, store, media, monkeypatch):
        monkeypatch.setattr(detector, "find_credits", lambda path, **kwargs: pytest.fail("decoded on the check thread"))
        ctx = self._ctx(store, media, force=True)
        self._time_out(store, media, ctx)
        out, _ = test_pipeline._run(ctx, media, {"plex-1": ready_publisher()}, stage="check")
        assert out is None


FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@pytest.fixture
def real_model(monkeypatch):
    """ffmpeg, with the text detection model set for the helper; skips without either or the font."""
    import shutil

    from media_preview_generator.markers.credits import textdet_helper

    model = os.environ.get(textdet_helper.MODEL_ENV, textdet_helper.DEFAULT_MODEL_PATH)
    ffmpeg = shutil.which("ffmpeg")
    if not (ffmpeg and os.path.isfile(model) and os.path.isfile(FONT)):
        pytest.skip("needs ffmpeg, the DejaVuSans font and the text detection model")
    monkeypatch.setenv(textdet_helper.MODEL_ENV, model)
    return ffmpeg


H264 = ["-c:v", "libx264", "-preset", "veryfast"]
VP9 = ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1", "-b:v", "1M"]


def _generated_roll(path, ffmpeg: str, *, scene_s: int, gop: int, encoder: list[str] = H264) -> str:
    """420 s of text-free gradients, then 120 s of names scrolling up over black (a new line every 3 s, 20 px/s), then
    optionally a scene after the credits (Q3); a keyframe every ``gop`` frames at 24 fps."""
    import subprocess as sp

    roll = ",".join(
        f"drawtext=fontfile={FONT}:text='NAME {i}':fontcolor=white:fontsize=24:x=(w-tw)/2:y=h-20*t+{i * 60}"
        for i in range(40)
    )
    inputs = ["-f", "lavfi", "-i",
              "gradients=size=640x360:rate=24:speed=0.02:seed=3,trim=duration=420,setpts=PTS-STARTPTS",  # fmt: skip
              "-f", "lavfi", "-i", f"color=c=black:size=640x360:rate=24:duration=120,{roll}"]  # fmt: skip
    if scene_s:
        inputs += [
            "-f",
            "lavfi",
            "-i",
            f"gradients=size=640x360:rate=24:speed=0.05:seed=9,trim=duration={scene_s},setpts=PTS-STARTPTS",
        ]
    streams = "".join(f"[{i}:v]" for i in range(len(inputs) // 4))
    sp.run([ffmpeg, "-v", "error", *inputs, "-filter_complex", f"{streams}concat=n={len(inputs) // 4}:v=1:a=0[v]", "-map", "[v]",
            *encoder, "-g", str(gop), "-pix_fmt", "yuv420p", str(path)], check=True)  # fmt: skip
    return str(path)


def _find_credits_on_the_cpu(clip: str, ffmpeg: str, *, duration_s: int, frames_read: list[int] | None = None):
    from media_preview_generator.markers.credits import textdet_helper

    # A throwaway pool of this test's own, closed here: the app's singleton (``get_textdet_pool``) is never closed,
    # since ``close_all`` ends a pool permanently.
    pool = textdet_helper.TextDetectorPool()

    def count_boxes(planes):
        if frames_read is not None:
            frames_read.append(len(planes))
        return pool.count_boxes(planes, gpu=None, gpu_device_path=None)

    try:
        return detector.find_credits(clip, duration_ms=duration_s * 1000, is_episode=False, ffmpeg=ffmpeg,
                                     count_boxes=count_boxes, gpu=None, gpu_device_path=None)  # fmt: skip
    finally:
        pool.close_all()


@pytest.mark.integration
@pytest.mark.timeout(600)
@pytest.mark.parametrize(("scene_s", "expected_end"), [(0, None), (60, 540.0)])
def test_real_decode_and_detection_on_a_generated_roll(tmp_path, real_model, scene_s, expected_end):
    clip = _generated_roll(tmp_path / "movie.mkv", real_model, scene_s=scene_s, gop=48)
    result = _find_credits_on_the_cpu(clip, real_model, duration_s=540 + scene_s)
    assert result.start_s is not None and abs(result.start_s - 420.0) <= 10.0
    if expected_end is None:
        assert result.end_s is None and result.end_rows == ()
    else:
        assert result.end_s is not None and abs(result.end_s - expected_end) <= 3.0


@pytest.mark.integration
@pytest.mark.timeout(900)
def test_an_intra_only_encode_answers_like_a_normal_one(tmp_path, real_model):
    # Every frame a keyframe: read in full, the keyframe pass would put all 14400 frames of this tail through text
    # detection. Thinned before decode it reads about what the 2 s GOP encode of the same picture does, and answers
    # the same start and end.
    import itertools

    answers = {}
    frames_read: dict[int, list[int]] = {48: [], 1: []}
    for gop in (48, 1):
        clip = _generated_roll(tmp_path / f"gop{gop}.mkv", real_model, scene_s=60, gop=gop)
        answers[gop] = _find_credits_on_the_cpu(clip, real_model, duration_s=600, frames_read=frames_read[gop])
    normal, intra = answers[48], answers[1]
    assert normal.start_s is not None and intra.start_s is not None and abs(intra.start_s - normal.start_s) <= 2.0
    assert normal.end_s is not None and intra.end_s is not None and abs(intra.end_s - normal.end_s) <= 2.0
    assert min(later[0] - earlier[0] for earlier, later in itertools.pairwise(intra.key_rows)) >= 2.0
    assert len(intra.key_rows) <= 600 / frames.INTRA_ONLY_SPACING_S + 1
    assert sum(frames_read[1]) <= 1.1 * sum(frames_read[48])


@pytest.mark.integration
@pytest.mark.timeout(900)
def test_a_vp9_encode_is_read_from_its_keyframes_like_an_h264_one(tmp_path, real_model):
    # VP9's decoder ignores -skip_frame: without its non-key packets dropped before the decoder, the keyframe pass would
    # put all 14400 frames of this tail through text detection (24 a second). With the drop it reads the keyframes
    # the H.264 encode of the same picture has, and answers the same start and end.
    answers = {}
    frames_read: dict[str, list[int]] = {"h264": [], "vp9": []}
    for name, encoder, suffix in (("h264", H264, "mkv"), ("vp9", VP9, "webm")):
        clip = _generated_roll(tmp_path / f"{name}.{suffix}", real_model, scene_s=60, gop=48, encoder=encoder)
        answers[name] = _find_credits_on_the_cpu(clip, real_model, duration_s=600, frames_read=frames_read[name])
    h264, vp9 = answers["h264"], answers["vp9"]
    assert h264.start_s is not None and vp9.start_s is not None and abs(vp9.start_s - h264.start_s) <= 2.0
    assert h264.end_s is not None and vp9.end_s is not None and abs(vp9.end_s - h264.end_s) <= 2.0
    assert len(vp9.key_rows) <= 600 / 2 + 5  # a keyframe every 2 s (and one at each cut the encoder chose)
    assert sum(frames_read["vp9"]) <= 1.1 * sum(frames_read["h264"])
