"""The credit text detector: which decodes it asks for, what it stores, and how failures leave the detector."""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from media_preview_generator.markers.credits import decode_check, detector, frames, rule_j
from media_preview_generator.markers.credits.textdet_helper import (
    TextDetShuttingDownError,
    TextDetUnavailableError,
)
from media_preview_generator.markers.freeze import Freeze
from media_preview_generator.markers.models import Candidate, FileIdentity, MarkerType, Source
from media_preview_generator.markers.pipeline import DetectorAnswer, DetectorUnavailableError
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
STORY = [(5100.0 + 2 * i, 0, 120.0, ()) for i in range(300)]  # 5100–5698 s, bright, no text
# Two cards, one over the other, as a dark credit frame reads them; a lit frame reads none.
CARDS = ((40, 24, 128, 44), (41, 55, 130, 75))
ROLL = [(5700.0 + 2 * i, 2, 12.0, CARDS) for i in range(100)]  # 5700–5898 s, dark cards
FINE = [(float(t), 0, 120.0, ()) for t in range(5680, 5690)] + [(float(t), 2, 12.0, CARDS) for t in range(5690, 5702)]
SCENE = [(5900.0 + 2 * i, 0, 120.0, ()) for i in range(50)]  # 5900–5998 s: a scene after the roll
END = [(5897.0, 2, 12.0, CARDS), (5898.0, 2, 12.0, CARDS), (5899.0, 1, 12.0, CARDS[:1])] + [
    (float(t), 0, 120.0, ()) for t in range(5900, 5918)
]
# The two cards as the 640x360 reading boxes text too small for 320x180: 9 px tall in 320x180 pixels. CARDS are 21 px,
# text the 320x180 reading boxes, and the larger reading drops a box that tall it didn't (SMALL_TEXT_MAX_HEIGHT_PX).
SMALL_CARDS = ((40, 24, 128, 32), (41, 55, 130, 63))


def small(rows: list) -> list:
    """The rows with CARDS' boxes as the 640x360 reading's small-text boxes."""
    swap = dict(zip(CARDS, SMALL_CARDS, strict=True))
    return [(row[0], row[1], row[2], tuple(swap.get(box, box) for box in row[3])) for row in rows]


def count(planes):
    return [()] * len(planes)


class Decodes:
    """Each decode's answer in turn. Once they run out, a 640x360 read (scale 2) gets the answer of the 320x180 read in
    the same place, decode for decode: a file whose text reads the same at either size."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.given: list = []
        self.worker: list[dict] = []  # each decode's pause check and threads, kept apart from its window

    def __call__(self, path, **kwargs):
        self.worker.append({key: kwargs.pop(key, None) for key in ("pause_check", "ffmpeg_threads")})
        self.calls.append({"path": path, **kwargs})
        if self.answers or kwargs.get("scale") != 2:
            answer = self.answers.pop(0)
        else:
            answer = self.given[sum(1 for call in self.calls[:-1] if call.get("scale") == 2)]
        self.given.append(answer)
        if isinstance(answer, BaseException):
            raise answer
        return answer


START_TIME_S = 1.4  # the container's own first timestamp, as ffprobe gives it
# The format an 8-bit 4:2:0 stream's GPU surfaces are downloaded in (frames.keyframe_thinning).
DOWNLOAD = "nv12"


class _Probes(list):
    """Every probe call's arguments; the start time probe answers ``START_TIME_S``, the packet probe ``thinning``."""

    thinning = frames.KeyframeThinning(None, False, DOWNLOAD)
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
    @pytest.mark.parametrize(("gpu", "device"), [("NVIDIA", "cuda:0"), (None, None)], ids=["gpu", "cpu"])
    def test_a_file_without_a_roll_reads_its_tail_again_at_640x360_and_nothing_more(
        self, monkeypatch, probes, gpu, device
    ):
        # No answer at 320x180 may be a roll whose text is too small to box there (Accused (2020)), so the same tail
        # is read once more at twice the size. Nothing there either: no answer, the 320x180 reading kept, and no
        # refine window decoded. The file is probed once for both reads.
        decodes = Decodes(STORY, STORY)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=gpu, gpu_device_path=device, phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.fine_rows, result.end_rows) == (None, None, (), ())
        assert (result.key_rows, result.scale, result.run_rows) == (tuple(STORY), 1, ())
        tail = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                "keyframes_only": True, "fps": None, "gpu": gpu, "gpu_device_path": device, "detect_boxes": count,
                "cancel_check": None, "start_time_s": START_TIME_S, "download_format": DOWNLOAD, "keep_every": None, "drop_non_key": False,
                "scale": 1}  # fmt: skip
        assert decodes.calls == [tail, {**tail, "scale": 2}]
        assert phases == ["Reading the credits…"]
        assert probes == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": None}]
        assert probes.thinning_calls == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": None}]

    @pytest.mark.parametrize(("gpu", "device", "threads"), [("NVIDIA", "cuda:0", 3), (None, None, None)],
                             ids=["gpu", "cpu"])  # fmt: skip
    def test_the_pause_and_the_workers_threads_reach_every_decode_of_both_readings(
        self, monkeypatch, probes, gpu, device, threads
    ):
        # One freeze for the file's every decode, so the time it holds adds up across them (the look-back's limit).
        paused = threading.Event()
        decodes = Decodes(ROLL, FINE)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                              detect_boxes=count, gpu=gpu, gpu_device_path=device, pause_check=paused.is_set,
                              ffmpeg_threads=threads)  # fmt: skip
        assert len(decodes.worker) >= 2  # the tail and its refine window at least
        freezes = {id(call["pause_check"]) for call in decodes.worker}
        assert len(freezes) == 1
        freeze = decodes.worker[0]["pause_check"]
        assert isinstance(freeze, Freeze) and not freeze()
        paused.set()
        assert freeze()
        assert {call["ffmpeg_threads"] for call in decodes.worker} == {threads}

    @pytest.mark.parametrize(("gpu", "device"), [("NVIDIA", "cuda:0"), (None, None)], ids=["gpu", "cpu"])
    def test_a_roll_too_small_to_box_at_320x180_is_found_at_640x360(self, monkeypatch, probes, gpu, device):
        # Accused (2020) S01E01: its credit cards box nothing at 320x180 and 4-11 boxes a frame at 640x360. The larger
        # read is the same tail through the same keyframe pass (an intra-only stride carried over), and every decode
        # of its answer -- the start's and the end's refine windows -- is read at its size too: at 320x180 they would
        # see no text and walk nowhere. Every decode downloads the stream's GPU surfaces in its own format.
        probes.thinning = frames.KeyframeThinning(48, False, "p010le")  # a 10-bit stream's GPU surfaces
        unboxed_roll = [(pts, 0, luma, ()) for pts, _, luma, _ in ROLL]
        decodes = Decodes(STORY + unboxed_roll + SCENE, STORY + small(ROLL) + SCENE, small(FINE), small(END))
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=gpu, gpu_device_path=device, phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5690.0, 5899.0, 2)
        assert (result.key_rows, result.fine_rows, result.end_rows) == (
            tuple(STORY + small(ROLL) + SCENE), tuple(small(FINE)), tuple(small(END))
        )  # fmt: skip
        tail = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                "keyframes_only": True, "fps": None, "gpu": gpu, "gpu_device_path": device, "detect_boxes": count,
                "cancel_check": None, "start_time_s": START_TIME_S, "download_format": "p010le", "keep_every": 48, "drop_non_key": False}  # fmt: skip
        refine = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "keyframes_only": False, "fps": 1, "gpu": gpu,
                  "gpu_device_path": device, "detect_boxes": count, "cancel_check": None,
                  "start_time_s": START_TIME_S, "download_format": "p010le", "scale": 2}  # fmt: skip
        assert decodes.calls == [{**tail, "scale": 1}, {**tail, "scale": 2},
                                 {**refine, "start_s": 5680.0, "length_s": 21.0},
                                 {**refine, "start_s": 5897.0, "length_s": 21.0}]  # fmt: skip
        assert phases == ["Reading the credits…", "Refining the credits start…", "Finding where the credits end…"]
        assert (len(probes), len(probes.thinning_calls)) == (1, 1)

    def test_the_larger_read_steps_back_before_the_tail_at_its_own_size(self, monkeypatch, probes):
        # The roll the 640x360 read finds opens the tail, so the 120 s before the tail are read -- at 640x360, like
        # the tail they are joined to: a 320x180 step would box none of the roll's cards and stop the join.
        tail = [(870.0 + 2 * i, 3, 10.0) for i in range(225)]
        before = [(750.0 + 2 * i, 0, 120.0) for i in range(54)] + [(858.0 + 2 * i, 3, 10.0) for i in range(7)]
        fine = [(float(t), 0, 120.0) for t in range(838, 858)] + [(float(t), 3, 10.0) for t in range(858, 860)]
        unboxed_tail = [(pts, 0, luma) for pts, _, luma in tail]
        decodes = Decodes(unboxed_tail, tail, before, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0")  # fmt: skip
        keyframe_pass = {"path": EPISODE.canonical_path, "ffmpeg": "/ff", "keyframes_only": True, "fps": None,
                         "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "detect_boxes": count, "cancel_check": None,
                         "start_time_s": START_TIME_S, "download_format": DOWNLOAD, "keep_every": None, "drop_non_key": False}  # fmt: skip
        assert decodes.calls[:3] == [{**keyframe_pass, "start_s": 870.0, "length_s": None, "scale": 1},
                                     {**keyframe_pass, "start_s": 870.0, "length_s": None, "scale": 2},
                                     {**keyframe_pass, "start_s": 750.0, "length_s": 120.0, "scale": 2}]  # fmt: skip
        assert (decodes.calls[3]["start_s"], decodes.calls[3]["length_s"], decodes.calls[3]["scale"]) == (
            838.0,
            21.0,
            2,
        )
        assert (result.start_s, result.end_s, result.scale) == (858.0, None, 2)

    @pytest.mark.parametrize(
        "error",
        [frames.GpuDecodeError("ffmpeg exited 1 decoding Movie (2020).mkv on the GPU"),
         frames.DecodeCancelledError("cancelled while decoding Movie (2020).mkv"),
         TextDetShuttingDownError("Text detection is shutting down"),
         frames.DecodeTimeoutError("decoding Movie (2020).mkv timed out after 600 s"),
         # The look-back's shared deadline raises the same error from a later step (_step_rows): the same cell.
         frames.DecodeTimeoutError("reading before the tail of the credits of Movie (2020).mkv ran past 600 s")],
        ids=["gpu-failure", "cancel", "shutting-down", "timeout", "look-back-deadline"],
    )  # fmt: skip
    def test_a_gpu_failure_a_cancel_a_shutdown_or_a_timeout_in_the_larger_read_is_the_files(
        self, monkeypatch, probes, error
    ):
        # A GPU failure goes to the worker's CPU rerun, which reads both sizes on the CPU; a cancel is a cancel; the
        # app stopping mid-read (worker threads are daemons, so the pool closes under them) is no answer, as it is at
        # 320x180; and a timeout is a timeout like one at 320x180 (T-R7): a mount that stalled between the readings
        # is asked again in a day. Kept as "nothing found", the file would never be read at 640x360 again.
        decodes = Decodes(STORY, error)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        with pytest.raises(type(error)):
            detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                  detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0")  # fmt: skip
        assert [call["scale"] for call in decodes.calls] == [1, 2]

    @pytest.mark.parametrize(
        "error",
        [frames.FrameDecodeError("ffmpeg wrote 3 timestamps for 4 frames of Movie (2020).mkv"),
         TextDetUnavailableError("the text detection helper didn't answer in 60 s")],
        ids=["decode-error", "text-detection"],
    )  # fmt: skip
    def test_any_other_failure_of_the_larger_read_stores_the_320x180_nothing_found(
        self, monkeypatch, probes, loguru_caplog, error
    ):
        # The 320x180 reading of the same tail has just completed, so a larger reading that can't be decoded or have
        # its text detected fails on its own account. Raised, it would store nothing and both readings would run and
        # fail again every day, forever; the file keeps "nothing found".
        decodes = Decodes(STORY, error)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0")  # fmt: skip
        assert result == detector.CreditsTextResult(None, None, tuple(STORY), (), (), (), 1)
        assert [call["scale"] for call in decodes.calls] == [1, 2]
        assert 'Stored "nothing found" for the credits of Movie (2020).mkv: reading its tail at 640x360 failed' in (
            loguru_caplog.text
        )

    def test_a_cancel_during_the_larger_read_propagates_and_is_never_nothing_found(self, monkeypatch, probes):
        # A job cancelled while its 640x360 request waited for a CPU helper: the file wasn't read, so neither the
        # 320x180 "nothing found" nor anything else may be kept for it.
        from media_preview_generator.markers.credits.textdet_helper import TextDetCancelledError

        decodes = Decodes(STORY, TextDetCancelledError("cancelled"))
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        with pytest.raises(TextDetCancelledError):
            detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                  detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert [call["scale"] for call in decodes.calls] == [1, 2]

    # A verdict card on black at 5600-5617 s (boxed at both sizes), story, then from 5700 s a roll over footage whose
    # names box only at 640x360: three small boxes a frame, to the end of the file. At 320x180 the card is the last run,
    # and more than 30 s of the file follows it, so the answer ends at 5617 s (Accused (2020) S05E02, S07E09 ...).
    VERDICT = (100, 80, 220, 96)
    NAMES = ((40, 140, 90, 148), (100, 140, 150, 148), (40, 152, 120, 160))

    def _verdict_then_roll(
        self, *, roll_at_640=True, fail=None, captions=False, roll_from=5700, fine_from=None, verdict_until=5618
    ):
        calls = []

        def row(t, scale, keyframe=True):
            if fine_from is not None and t >= fine_from and scale == 2 and not keyframe:
                return (float(t), 3, 110.0, self.NAMES)
            if 5600 <= t < verdict_until:
                return (float(t), 1, 12.0, (self.VERDICT,))
            if captions and 5640 <= t < 5660:  # story captions, boxed at both sizes: text 320x180 already read
                return (float(t), 1, 110.0, ((60, 150, 260, 164),))
            if t >= roll_from and scale == 2 and roll_at_640:
                return (float(t), 3, 110.0, self.NAMES)
            return (float(t), 0, 110.0, ())

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            calls.append((start_s, keyframes_only, scale))
            if fail is not None and scale == 2:
                raise fail
            end_s = 6000.0 if length_s is None else start_s + length_s
            step = 2 if keyframes_only else 1
            first = int(start_s) + (int(start_s) % 2 if keyframes_only else 0)
            return [row(t, scale, keyframes_only) for t in range(max(5100, first), 6000, step) if start_s <= t <= end_s]

        return calls, decode

    @pytest.mark.parametrize(("gpu", "device"), [("NVIDIA", "cuda:0"), (None, None)], ids=["gpu", "cpu"])
    def test_an_answer_that_ends_in_a_scene_has_the_rest_of_its_file_read_at_640x360(
        self, monkeypatch, probes, gpu, device
    ):
        # The roll that follows the scene is where the credits are: the rest of the file, from the answer's end, is
        # read again at 640x360, and its run is the answer, refined at that size. Nothing before the end is decoded
        # again, and the 320x180 reading's text stays out of the runs (the card is seen text).
        calls, decode = self._verdict_then_roll()
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=gpu, gpu_device_path=device)  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5700.0, None, 2)
        assert calls == [(5100.0, True, 1), (5580.0, False, 1), (5615.0, False, 1), (5617.0, True, 2),
                         (5680.0, False, 2)]  # fmt: skip
        # The keyframes before the end are the 320x180 reading's, the rest the 640x360 one's.
        assert [row[0] for row in result.key_rows] == [float(t) for t in range(5100, 6000, 2)]
        assert result.key_rows[250] == (5600.0, 1, 12.0, (self.VERDICT,))
        assert result.key_rows[300] == (5700.0, 3, 110.0, self.NAMES)
        assert result.run_rows[250] == (5600.0, 0, 12.0, ())  # the card: seen text

    @pytest.mark.parametrize(
        ("keep_every", "drop_non_key"),
        [(None, False), (None, True), (48, False), (48, True)],
        ids=["plain", "vp9", "intra-only", "vp9-intra-only"],
    )
    @pytest.mark.parametrize(
        ("verdict_until", "end_s"), [(5618, 5617.0), (5619, 5618.0)], ids=["end-between-keyframes", "end-on-a-keyframe"]
    )
    def test_the_rest_of_the_file_is_read_on_the_keyframes_the_320x180_reading_read(
        self, monkeypatch, probes, keep_every, drop_non_key, verdict_until, end_s
    ):
        # A stride counts packets from the seek: read from the answer's end, an intra-only stream's kept packets are
        # others than the 320x180 reading's (0 of 13 shared on a real all-I H.264), and the text the 320x180 reading
        # boxed would go unmatched. So a strided pass seeks where the tail's did, and its rows before the end are
        # dropped; any other pass decodes keyframes, whatever the seek, and starts at the end. A keyframe on the end
        # itself is the larger reading's (the 320x180 rows kept stop before it), and is neither lost nor doubled.
        probes.thinning = frames.KeyframeThinning(keep_every, drop_non_key, DOWNLOAD)
        calls, decode = self._verdict_then_roll(verdict_until=verdict_until)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert calls[3] == (5100.0 if keep_every else end_s, True, 2)
        assert (result.start_s, result.scale) == (5700.0, 2)
        assert [row[0] for row in result.key_rows] == [float(t) for t in range(5100, 6000, 2)]  # each keyframe once
        assert result.key_rows[250] == (5600.0, 1, 12.0, (self.VERDICT,))  # the 320x180 reading's
        assert result.key_rows[300] == (5700.0, 3, 110.0, self.NAMES)  # the 640x360 one's

    def test_an_answer_that_ends_in_a_scene_stays_when_640x360_finds_nothing_after_it(self, monkeypatch, probes):
        calls, decode = self._verdict_then_roll(roll_at_640=False)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5600.0, 5617.0, 1)
        assert calls[-1] == (5617.0, True, 2)  # read, and nothing refined at that size

    def test_a_roll_that_starts_where_the_answer_ends_is_the_answer(self, monkeypatch, probes):
        # The names follow the card at once: the "scene" 320x180 saw after it was the roll it couldn't box. The roll is
        # the credits, the card before it is not (Accused (2020) S07E09's plea card, one of the epilogue shapes), and
        # the card is seen text, so the larger reading's start doesn't walk back onto it.
        calls, decode = self._verdict_then_roll(roll_from=5618)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5618.0, None, 2)
        assert calls[3] == (5617.0, True, 2)

    def test_a_larger_start_that_walks_back_past_the_answers_end_keeps_the_answer(self, monkeypatch, probes):
        # The names show in the 1 fps frames from 5610 s, under the card, so the larger reading's start walks back from
        # its first keyframe (5618 s) to 5610 s -- before the 320x180 answer's end. That run doesn't begin after the
        # scene the answer ended on, so it is no roll after it: the answer stays.
        calls, decode = self._verdict_then_roll(roll_from=5618, fine_from=5610)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5600.0, 5617.0, 1)
        assert calls[3:] == [(5617.0, True, 2), (5598.0, False, 2)]  # read, refined, and not kept

    def test_seen_text_in_the_scene_after_an_answer_makes_no_run_at_640x360(self, monkeypatch, probes):
        # Captions boxed at both sizes in the scene: the 320x180 reading read them and found no roll there, so they
        # don't start the larger reading's run either; the roll only 640x360 shows does.
        calls, decode = self._verdict_then_roll(captions=True)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale) == (5700.0, 2)

    # After the verdict card's answer (5600-5617 s), a roll over footage from 5700 to 5720 s. Its titles box at 320x180
    # as blocks on its first 12 s and one block after: lit frames with three boxes are credit frames, but 12 s is no
    # run. At 640x360 the titles box as lines inside those blocks, and the names under them box only there.
    TITLE_BLOCKS = ((40, 120, 140, 140), (160, 120, 260, 140), (100, 145, 220, 165))
    TITLE_LINES = ((42, 122, 138, 130), (42, 131, 138, 139), (162, 122, 258, 130), (102, 147, 218, 155))
    NAME_LINES = ((42, 150, 98, 158), (162, 150, 218, 158))
    EPILOGUE_320, EPILOGUE_640 = (60, 80, 260, 100), (64, 82, 256, 97)

    def _half_boxed_roll(self, *, epilogue: bool = False):
        # With ``epilogue``, a card on black at 5680-5684 s and a roll 320x180 boxes nothing of.
        calls = []

        def row(t, scale):
            if 5600 <= t < 5618:
                return (float(t), 1, 12.0, (self.VERDICT,))
            if epilogue and 5680 <= t <= 5684:  # a card on black, boxed at both sizes
                card = self.EPILOGUE_640 if scale == 2 else self.EPILOGUE_320
                return (float(t), 1, 12.0, (card,))
            if 5700 <= t <= 5720 and scale == 2:
                boxes = (*self.TITLE_LINES, *self.NAME_LINES)
                return (float(t), len(boxes), 110.0, boxes)
            if 5700 <= t <= 5712 and not epilogue:
                return (float(t), 3, 110.0, self.TITLE_BLOCKS)
            if 5714 <= t <= 5720 and not epilogue:
                return (float(t), 1, 110.0, self.TITLE_BLOCKS[:1])
            return (float(t), 0, 110.0, ())

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            calls.append((start_s, keyframes_only, scale))
            end_s = 6000.0 if length_s is None else start_s + length_s
            step = 2 if keyframes_only else 1
            first = int(start_s) + (int(start_s) % 2 if keyframes_only else 0)
            return [row(t, scale) for t in range(max(5100, first), 6000, step) if start_s <= t <= end_s]

        return calls, row, decode

    def test_a_roll_the_320x180_reading_half_boxed_after_the_answers_end_is_read_whole(self, monkeypatch, probes):
        # I Survived a Serial Killer S01E04: the roll after the scene is a frame short of a 15 s run at 320x180, so story
        # before it is the last run, 92 s early, and ends in a scene. Its frames at 640x360 hold its titles, lines
        # inside the blocks 320x180 boxed, and names only the larger frame boxes. Without the titles as seen text, two
        # names are no lit credit frame, no run, and the early answer stayed. After the answer's end a frame showing
        # text only the larger frame boxes is read whole: the roll is the answer.
        calls, row, decode = self._half_boxed_roll()
        assert rule_j.coarse_start([row(t, 1) for t in range(5100, 6000, 2)]).pts_s == 5600.0  # no roll at 320x180
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5700.0, 5720.0, 2)
        assert calls[3:] == [(5617.0, True, 2), (5680.0, False, 2), (5719.0, False, 2)]
        assert result.run_rows[300] == (5700.0, 6, 110.0, (*self.TITLE_LINES, *self.NAME_LINES))  # read whole
        assert result.run_rows[250] == (5600.0, 0, 12.0, ())  # the answer's own card: seen text

    def test_a_card_after_the_answers_end_the_320x180_reading_boxed_all_of_stays_seen_text(self, monkeypatch, probes):
        # Accused (2020) S04E05 and S07E02: an epilogue card on black after the answer's end, 16-20 s before a roll
        # only 640x360 boxes, and boxed whole at 320x180 (a lone card: no run there). Read as text at 640x360, the 24 s
        # join glues it onto the roll: 25.5 and 17.5 s early. A frame holding no text only the larger frame boxes
        # stays without the text 320x180 boxed, so the start is the roll's.
        calls, _row, decode = self._half_boxed_roll(epilogue=True)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale) == (5700.0, 2)
        assert result.run_rows[290] == (5680.0, 0, 12.0, ())
        assert result.key_rows[290] == (5680.0, 1, 12.0, (self.EPILOGUE_640,))  # as decoded

    def test_an_answer_that_runs_to_the_end_of_the_file_is_not_read_again(self, monkeypatch, probes):
        # No scene after it: there is nowhere after the answer for a roll to be, and every answered file would pay for
        # the larger reading otherwise.
        decodes = Decodes(STORY + ROLL, FINE)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.end_s, result.scale) == (None, 1)
        assert [call["scale"] for call in decodes.calls] == [1, 1]

    @pytest.mark.parametrize(
        "error",
        [frames.GpuDecodeError("ffmpeg exited 1 decoding Movie (2020).mkv on the GPU"),
         frames.DecodeCancelledError("cancelled while decoding Movie (2020).mkv"),
         TextDetShuttingDownError("Text detection is shutting down")],
        ids=["gpu-failure", "cancel", "shutting-down"],
    )  # fmt: skip
    def test_a_gpu_failure_a_cancel_or_a_shutdown_reading_after_an_answer_is_the_files(
        self, monkeypatch, probes, error
    ):
        calls, decode = self._verdict_then_roll(fail=error)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        with pytest.raises(type(error)):
            detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                  detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0")  # fmt: skip
        assert calls[-1] == (5617.0, True, 2)

    @pytest.mark.parametrize(
        "error",
        [frames.DecodeTimeoutError("decoding Movie (2020).mkv timed out after 600 s"),
         frames.FrameDecodeError("ffmpeg wrote 3 timestamps for 4 frames of Movie (2020).mkv"),
         TextDetUnavailableError("the text detection helper didn't answer in 60 s")],
        ids=["timeout", "decode-error", "text-detection"],
    )  # fmt: skip
    def test_any_other_failure_reading_after_an_answer_keeps_the_answer(
        self, monkeypatch, probes, loguru_caplog, error
    ):
        calls, decode = self._verdict_then_roll(fail=error)
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0")  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5600.0, 5617.0, 1)
        assert (
            "Kept the 320x180 answer for the credits of Movie (2020).mkv: reading the rest of its file at 640x360 failed"
            in loguru_caplog.text
        )

    # I Survived a Serial Killer S01E14: court footage over the last minute before the roll, whose date line, logo
    # and caption only 640x360 boxes (4-10 px tall): four small boxes a lit frame, on and off. A keyframe without text,
    # then the roll, a dozen names a frame. 320x180 boxes none of it.
    COURT = ((30, 19, 86, 29), (247, 145, 266, 154), (282, 163, 308, 171), (0, 173, 26, 179))
    BUG = (280, 8, 312, 24)
    NAMES_640 = tuple((40 + 90 * (n % 3), 115 + 9 * (n // 3), 110 + 90 * (n % 3), 122 + 9 * (n // 3)) for n in range(9))

    def _court_then_roll(self, scale: int, t: int, keyframe: bool = True, bug: bool = False) -> tuple:
        row = self._court_then_roll_without_bug(scale, t)
        if bug and keyframe and t % 4 == 0 and t < 5720:  # a logo over busy footage, boxed every other keyframe
            return (row[0], row[1] + 1, row[2], (*row[3], self.BUG))
        return row

    def _court_then_roll_without_bug(self, scale: int, t: int) -> tuple:
        if scale == 1 or t < 5640 or t >= 5720:
            return (float(t), 0, 110.0, ())
        if t < 5700:
            court = self.COURT if (t // 2) % 5 else self.COURT[:1]  # on and off with the shots
            return (float(t), len(court), 100.0, court)
        if t < 5702:
            return (float(t), 0, 118.0, ())  # the cut to the roll's footage
        return (float(t), len(self.NAMES_640), 120.0, self.NAMES_640)

    def test_small_print_on_story_before_a_roll_only_640x360_reads_is_not_its_start(self, monkeypatch, probes):
        # Read as credit frames, the court footage's frames join the roll over the 24 s join: 62 s early. Its text
        # isn't across the story before the run, so it is no overlay. At 640x360 the roll starts on its first dense
        # frame, and walks back over keyframes with text only as far as the cut: the start is the roll's.
        calls = []

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            calls.append((start_s, keyframes_only, scale))
            end_s = 6000.0 if length_s is None else start_s + length_s
            step = 2 if keyframes_only else 1
            first = int(start_s) + (int(start_s) % 2 if keyframes_only else 0)
            times = range(max(5100, first), 6000, step)
            return [self._court_then_roll(scale, t, keyframes_only) for t in times if start_s <= t <= end_s]

        at_640 = [self._court_then_roll(2, t) for t in range(5100, 6000, 2)]
        assert rule_j.coarse_start(at_640).pts_s == 5642.0 and rule_j.overlay_boxes(at_640) == ()
        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale) == (5702.0, 2)
        assert calls[2] == (5682.0, False, 2)  # refined before the roll, not before the court footage

    def test_a_channel_bug_on_the_cut_before_the_roll_doesnt_carry_the_start_back(self, monkeypatch, probes):
        # The same file under a channel logo boxed at both sizes on every other keyframe, the cut in front of the roll
        # included. The walk back from the roll's first dense frame reads what each keyframe shows without the
        # overlays: with the logo counted, the cut shows text and the start walks back onto the court footage.
        calls = []

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            calls.append((start_s, keyframes_only, scale))
            end_s = 6000.0 if length_s is None else start_s + length_s
            step = 2 if keyframes_only else 1
            first = int(start_s) + (int(start_s) % 2 if keyframes_only else 0)
            times = range(max(5100, first), 6000, step)
            return [self._court_then_roll(scale, t, keyframes_only, bug=True) for t in times if start_s <= t <= end_s]

        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale, result.overlays) == (5702.0, 2, (self.BUG,))
        assert result.key_rows[300] == (5700.0, 1, 118.0, (self.BUG,))  # the cut, as decoded

    def test_the_320x180_reading_keeps_its_start_on_sparse_text(self, monkeypatch, probes):
        # The same frames boxed at 320x180, the roll running to the end of the file: the dense start is the larger
        # reading's alone, where small print is read, and the 320x180 reading starts on the court footage as before.
        at_320 = [self._court_then_roll(2, t) for t in range(5100, 5720, 2)]
        at_320 += [(float(t), len(self.NAMES_640), 120.0, self.NAMES_640) for t in range(5720, 6000, 2)]
        fine = [(float(t), 0, 110.0, ()) for t in range(5622, 5640)] + [
            (float(t), 4, 100.0, self.COURT) for t in range(5640, 5644)
        ]  # fmt: skip
        decodes = Decodes(at_320, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.end_s, result.scale) == (5640.0, None, 1)
        assert [call["scale"] for call in decodes.calls] == [1, 1]

    def test_text_the_320x180_reading_boxed_makes_no_run_in_the_larger_reading(self, monkeypatch, probes):
        # A Season to Remember (2024), decoded on the CPU: an interview's lower third (a name, a station) over lit
        # story. At 320x180 it boxes as two lines: a lit frame's two boxes are no credit frame, so no run. At 640x360
        # its words box apart, three or more on every frame from 5604 s to 5640 s: a 36 s run of rows as decoded, which
        # answered 347 s before the credits. Only two of those frames, 36 s apart, hold text the 320x180 reading didn't
        # box. The larger reading is for text too small to box at 320x180, so its runs are found on that text alone,
        # and two frames too far apart to join are no run: no answer.
        name_320, station_320 = (40, 140, 140, 160), (40, 118, 100, 136)
        seen_640 = ((42, 142, 90, 152), (95, 142, 138, 152), (42, 120, 98, 132))  # inside the two 320x180 lines
        new_640 = ((160, 142, 200, 152), (205, 142, 240, 152), (160, 120, 230, 132))
        lower_third = range(5604, 5644, 4)

        def row(t, scale):
            if t not in lower_third:
                return (float(t), 0, 120.0, ())
            if scale == 1:
                return (float(t), 2, 110.0, (name_320, station_320))
            boxes = (*seen_640, *new_640) if t in (5604, 5640) else seen_640
            return (float(t), len(boxes), 110.0, boxes)

        calls = []

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            calls.append((start_s, keyframes_only, scale))
            end_s = 6000.0 if length_s is None else start_s + length_s
            step = 2 if keyframes_only else 1
            return [row(t, scale) for t in range(5100, 6000, step) if start_s <= t < end_s]

        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale) == (None, 1)
        assert calls == [(5100.0, True, 1), (5100.0, True, 2)]  # no refine window at either size

    def test_text_the_320x180_reading_already_boxed_is_not_read_again_at_640x360(self, monkeypatch, probes):
        # Accused (2020) S02E03: an epilogue card on black ("The day before Brian must report to jail...") 22 s before
        # the roll, lit story between. At 320x180 the card is boxed and the roll isn't: no run. At 640x360 both are,
        # and the 24 s join glues the card onto the roll, a start 14 s of story early. The card was read at 320x180
        # already; the larger reading is for text too small to box there, so the card's boxes don't count in it and
        # the start is the roll's.
        epilogue_320, epilogue_640 = (100, 80, 220, 96), (104, 82, 216, 94)
        story = [(5100.0 + 2 * i, 0, 120.0, ()) for i in range(288)]  # 5100-5674 s
        gap = [(5680.0 + 2 * i, 0, 120.0, ()) for i in range(10)]  # 5680-5698 s: lit story without text
        unboxed_roll = [(pts, 0, luma, ()) for pts, _, luma, _ in ROLL]
        at_320 = story + [(5676.0, 1, 5.0, (epilogue_320,)), (5678.0, 1, 5.0, (epilogue_320,))] + gap + unboxed_roll
        at_640 = story + [(5676.0, 1, 5.0, (epilogue_640,)), (5678.0, 1, 5.0, (epilogue_640,))] + gap + small(ROLL)
        assert rule_j.coarse_start(at_320) is None
        assert rule_j.coarse_start(at_640).pts_s == 5676.0  # the card, read as decoded
        decodes = Decodes(at_320, at_640, small(FINE))
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (decodes.calls[2]["start_s"], decodes.calls[2]["length_s"]) == (5680.0, 21.0)  # refined at the roll
        assert (result.start_s, result.end_s, result.scale) == (5690.0, None, 2)
        assert result.key_rows == tuple(at_640)  # handed back as decoded, the card's boxes included
        # ... and beside them the rows rule J found the run on, without the card, which the harness reads it from.
        assert result.run_rows[288:290] == ((5676.0, 0, 5.0, ()), (5678.0, 0, 5.0, ()))
        assert result.run_rows[300:] == tuple(small(ROLL))

    def test_a_bug_boxed_at_both_sizes_is_still_the_larger_readings_overlay(self, monkeypatch, probes):
        # A channel bug boxed on every keyframe at both sizes, a dark story scene under it before the roll, and roll
        # names only 640x360 boxes. The bug is text the 320x180 reading already read, but it is gathered as an overlay
        # from the 640x360 rows as decoded: gathered from the rows without the 320x180 text it would have no sightings,
        # the 1 fps rows would keep it, and the refine would walk the start back over the dark scene, 20 s early.
        # The bug is boxed on every other lit keyframe (a logo over busy footage), so text isn't on screen all through
        # the story, and on every frame of the dark scene.
        bug = (280, 10, 310, 24)
        names = ((120, 60, 200, 64), (120, 80, 200, 84))

        def row(t: float, larger: bool) -> tuple:
            boxes = (bug,) if t >= 5670 or t % 4 == 0 else ()
            boxes += names if larger and t >= 5700 else ()
            return (t, len(boxes), 10.0 if t >= 5700 else 20.0 if t >= 5670 else 120.0, boxes)

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            if keyframes_only:
                return [row(5100.0 + 2 * i, scale == 2) for i in range(400)]
            return [row(float(t), scale == 2) for t in range(int(start_s), int(start_s + length_s) + 1)]

        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale, result.overlays) == (5700.0, 2, (bug,))

    def test_the_larger_reading_judges_text_all_through_on_its_rows_as_decoded(self, monkeypatch, probes):
        # A running timecode boxed at both sizes and a lower third only 640x360 boxes from 5700 s. As decoded, the
        # larger reading has text all through the story, so there is no answer, exactly as at 320x180: the share is
        # counted on the rows as decoded at either size, never on the text the smaller reading missed alone.
        timecode = (130, 160, 190, 170)
        lower_third = ((40, 20, 80, 26), (90, 20, 120, 26), (40, 30, 100, 36))
        at_320 = [(5100.0 + 2 * i, 1, 120.0, (timecode,)) for i in range(450)]
        at_640 = [(t, 1 + 3 * (t >= 5700), luma, (timecode, *(lower_third if t >= 5700 else ())))
                  for t, _, luma, _ in at_320]  # fmt: skip
        coarse = rule_j.coarse_start(at_640)
        assert coarse.pts_s == 5700.0 and rule_j.text_all_through(at_640, coarse)
        decodes = Decodes(at_320, at_640)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.fine_rows) == (None, ())
        assert [call["scale"] for call in decodes.calls] == [1, 2]

    def test_a_run_the_larger_frame_makes_of_boxes_too_tall_for_small_text_is_no_answer(self, monkeypatch, probes):
        # Frankenstein: The Anatomy Lesson (2025), a making-of: at 640x360 the model boxes dark set footage as text --
        # blobs 30-110 px tall in 320x180 pixels, no line of text too small for 320x180 -- on keyframe after keyframe,
        # and a camera monitor's readout (small text) twice in the middle. Read as text, the blobs make a 40 s run
        # and the readout its two credit frames: an answer 400 s before the credits. Text the 320x180 reading can't
        # box is at most SMALL_TEXT_MAX_HEIGHT_PX tall there; taller boxes it didn't find are not text, and are
        # dropped from the larger reading's rows as decoded, so they make no run: no answer, as at 320x180.
        blob = (166, 24, 237, 104)
        readout = ((21, 139, 63, 150), (87, 134, 139, 146), (65, 48, 87, 58))
        dark_set = [(5600.0 + 2 * i, 1, 25.0, (blob,)) for i in range(21)]  # 5600-5640 s
        dark_set = [(t, 3, 40.0, readout) if t in (5620.0, 5622.0) else row for t, *_, row in
                    ((row[0], row) for row in dark_set)]  # fmt: skip
        story = [(5100.0 + 2 * i, 0, 120.0, ()) for i in range(250)]  # 5100-5598 s
        after = [(5642.0 + 2 * i, 0, 120.0, ()) for i in range(179)]  # 5642-5998 s: story to the end
        at_320 = story + [(t, 0, luma, ()) for t, _, luma, _ in dark_set] + after
        at_640 = story + dark_set + after
        assert rule_j.coarse_start(at_640).pts_s == 5600.0  # the blobs, read as text
        decodes = Decodes(at_320, at_640)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.fine_rows, result.scale) == (None, (), 1)
        assert [call["scale"] for call in decodes.calls] == [1, 2]

    def test_boxes_too_tall_for_small_text_dont_start_the_roll_early_in_any_window(self, monkeypatch, probes):
        # Lisa Ann Walter: It Was an Accident (2026): a dark audience before the roll, which the 640x360 model boxes as
        # 45-110 px blobs on its keyframes and 1 fps frames. Dark frames need one box to be credit frames, so read as
        # text they join the roll and the refine walks back over them: a start 12 s early, on the audience. Dropped
        # from every window of the larger reading, the start is the roll's.
        blob = (156, 31, 258, 137)
        audience = [(5680.0 + 2 * i, 1, 25.0, (blob,)) for i in range(10)]  # 5680-5698 s, dark, blobs
        at_320 = STORY + [(t, 0, luma, ()) for t, _, luma, _ in audience + ROLL]
        at_640 = STORY + audience + small(ROLL)
        fine = [(float(t), 1, 25.0, (blob,)) for t in range(5680, 5700)]
        fine += [(float(t), 2, 12.0, SMALL_CARDS) for t in range(5700, 5702)]
        decodes = Decodes(at_320, at_640, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (decodes.calls[2]["start_s"], decodes.calls[2]["scale"]) == (5680.0, 2)
        assert (result.start_s, result.scale) == (5700.0, 2)
        assert all(row[1] == 0 for row in result.fine_rows if row[0] < 5700)  # the audience's blobs dropped there too

    def test_every_window_of_the_larger_reading_is_read_without_boxes_too_tall_for_small_text(
        self, monkeypatch, probes
    ):
        # The roll opens the tail at 640x360, so a step before the tail is read, and a scene follows it, so the end
        # window is read too. Every one of those decodes gives a blob beside the names; every row the result carries
        # -- the joined keyframes, the start's window and the end's -- is without it.
        blob = (156, 31, 258, 137)

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            end_s = 1320.0 if length_s is None else start_s + length_s
            if keyframes_only:
                times = [t for t in range(800, 1320, 2) if start_s <= t < end_s]
            else:
                times = [t for t in range(800, 1320) if start_s <= t <= end_s]
            rows = []
            for t in times:
                if t < 858 or t >= 1100:  # lit story before the roll, a lit scene after it
                    rows.append((float(t), 0, 120.0, ()))
                elif scale == 2:
                    rows.append((float(t), 3, 10.0, (*SMALL_CARDS, blob)))
                else:
                    rows.append((float(t), 0, 10.0, ()))
            return rows

        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale) == (858.0, 2)
        assert result.key_rows[0][0] == 800.0 and result.fine_rows and result.end_rows  # the step, both windows
        rows = result.key_rows + result.fine_rows + result.end_rows
        assert {box for row in rows for box in row[3]} == set(SMALL_CARDS)

    def test_a_tall_box_the_320x180_reading_boxed_is_kept_on_that_keyframe_only_not_on_a_1fps_frame_at_its_time(
        self, monkeypatch, probes
    ):
        # The 320x180 reading boxed the tall shape on the roll's first keyframe (910 s, 40 s into the tail) alone: one
        # frame, no run, no answer. The larger reading keeps it on that keyframe, which is matched to the 320x180 one
        # by its time. The start's 1 fps window has a frame at 910 s too, but 1 fps frames aren't matched to keyframes:
        # it goes there as on every other 1 fps frame, or one frame of the window would read differently from its
        # neighbours.
        blob = (156, 31, 258, 137)

        def decode(path, *, start_s, length_s, keyframes_only, scale, **kwargs):
            end_s = 1320.0 if length_s is None else start_s + length_s
            if keyframes_only:
                times = [t for t in range(800, 1320, 2) if start_s <= t < end_s]
            else:
                times = [t for t in range(800, 1320) if start_s <= t <= end_s]
            rows = []
            for t in times:
                if t < 910 or t >= 1100:
                    rows.append((float(t), 0, 120.0, ()))
                elif scale == 2:
                    rows.append((float(t), 3, 10.0, (*SMALL_CARDS, blob)))
                else:
                    rows.append((float(t), 1, 10.0, (blob,)) if t == 910 else (float(t), 0, 10.0, ()))
            return rows

        monkeypatch.setattr(detector.frames, "decode_rows", decode)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.scale) == (910.0, 2)
        assert [row for row in result.key_rows if row[0] == 910.0] == [(910.0, 3, 10.0, (*SMALL_CARDS, blob))]
        assert [row for row in result.fine_rows if row[0] == 910.0] == [(910.0, 2, 10.0, SMALL_CARDS)]

    def test_a_tall_box_the_320x180_reading_boxed_too_still_counts_as_text_at_640x360(self, monkeypatch, probes):
        # A channel logo 20 px tall, boxed on every keyframe at both sizes: text all through the story, so no answer
        # at 320x180. It is text, not a blob, so the larger reading keeps it where rule J reads the rows as decoded,
        # and a lower third only 640x360 boxes from 5700 s gets no answer either. Dropped as too tall for small text,
        # the logo would leave the story blank and the lower third would answer.
        logo = (270, 8, 310, 27)
        lower_third = ((40, 20, 80, 26), (90, 20, 120, 26), (40, 30, 100, 36))
        at_320 = [(5100.0 + 2 * i, 1, 120.0, (logo,)) for i in range(450)]
        at_640 = [(t, 1 + 3 * (t >= 5700), luma, ((269, 9, 309, 26), *(lower_third if t >= 5700 else ())))
                  for t, _, luma, _ in at_320]  # fmt: skip
        decodes = Decodes(at_320, at_640)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.fine_rows) == (None, ())
        assert [call["scale"] for call in decodes.calls] == [1, 2]

    def test_only_the_boxes_320x180_read_are_dropped_not_their_frames(self, monkeypatch, probes):
        # The roll's first card holds a title large enough to box at 320x180 and names only 640x360 boxes. The title's
        # box goes; the names keep the frame a credit frame, so the run still starts on it. Dropping whole frames
        # instead would start the run a keyframe late.
        title_320, title_640 = (38, 22, 131, 36), (40, 24, 128, 34)
        at_320 = STORY + [(5700.0, 1, 12.0, (title_320,))] + [(pts, 0, luma, ()) for pts, _, luma, _ in ROLL[1:]]
        at_640 = STORY + [(5700.0, 2, 12.0, (title_640, SMALL_CARDS[1]))] + small(ROLL[1:])
        decodes = Decodes(at_320, at_640, small(FINE))
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (decodes.calls[2]["start_s"], decodes.calls[2]["length_s"]) == (5680.0, 21.0)  # 5682 without 5700
        assert (result.start_s, result.scale) == (5690.0, 2)

    def test_a_step_before_the_tail_the_320x180_reading_read_but_didnt_join_is_seen_text_too(self, monkeypatch, probes):
        # At 320x180 the tail opens on a caption run on a night scene, so the 120 s before it are read; captions there
        # at 846-848 s are 32 s from the run over lit story, so that step isn't joined and there is no answer. At
        # 640x360 the roll's names box from the tail's first keyframe, 22 s from those captions. The captions were
        # read at 320x180 all the same, so they are seen text, and the join is judged on the same own text as the
        # start: the run starts on the tail's first keyframe, a join at the tail's edge is refused (spec §14
        # 2026-09-23), and there is no answer. Seen only from the rows the smaller reading joined -- or the join
        # judged on the captions -- they would start it at 846 s, on story.
        caption = (100, 150, 220, 160)
        tail_320 = [(870.0 + 2 * i, 0, 10.0, ()) for i in range(5)]  # 870-878 s: dark, nothing boxed
        tail_320 += [(880.0 + 2 * i, 1, 10.0, (caption,)) for i in range(10)]  # 880-898 s: the captions
        tail_320 += [(900.0 + 2 * i, 0, 10.0, ()) for i in range(210)]  # 900-1318 s: dark, the names unboxed
        tail_640 = [(t, n + 2, luma, (*boxes, *SMALL_CARDS)) for t, n, luma, boxes in tail_320]
        before = [(750.0 + 2 * i, 0, 120.0, ()) for i in range(48)]  # 750-844 s: lit story
        before += [(846.0, 1, 10.0, (caption,)), (848.0, 1, 10.0, (caption,))]
        before += [(850.0 + 2 * i, 0, 120.0, ()) for i in range(10)]  # 850-868 s: lit story
        fine = [(float(t), 0, 120.0, ()) for t in range(826, 850)] + [(846.0, 1, 10.0, (caption,))]
        decodes = Decodes(tail_320, before, tail_640, before, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert [(call["start_s"], call["scale"]) for call in decodes.calls] == [
            (870.0, 1), (750.0, 1), (870.0, 2), (750.0, 2)
        ]  # fmt: skip
        assert (result.start_s, result.scale) == (None, 1)

    def test_a_roll_to_the_end_of_the_file_is_refined_before_it_and_left_open_ended(self, monkeypatch, probes):
        cancel = lambda: False  # noqa: E731
        decodes = Decodes(STORY + ROLL, FINE)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        phases: list[str] = []
        # The roll's last keyframe is at 5898 s and the file ends at 5910 s: no scene follows, no end decode (Q3).
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None, cancel_check=cancel,
                                       phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s) == (rule_j.credits_start(STORY + ROLL, FINE), None) == (5690.0, None)
        assert len(decodes.calls) == 2
        # Whole-dict equality, like the tail decode above: every kwarg the refine decode forwards is the detector's to
        # get right. Every decode gets the start time probed once in this run: a stale or defaulted one would put a
        # recording's refine rows tens of thousands of seconds out and silently drop the refinement (Task 6).
        assert decodes.calls[1] == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5680.0,
                                    "length_s": 21.0, "keyframes_only": False, "fps": 1, "gpu": None,
                                    "gpu_device_path": None, "detect_boxes": count, "cancel_check": cancel,
                                    "start_time_s": START_TIME_S, "download_format": DOWNLOAD, "scale": 1}  # fmt: skip
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
                                       detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0", phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.end_rows) == (5690.0, 5899.0, tuple(END))
        # Both refine decodes by whole-dict equality: they stay on the worker's GPU (a refine decode quietly dropped to
        # the CPU would raise FrameDecodeError instead of the GpuDecodeError the worker's CPU rerun needs), and all three
        # decodes read the one start time this run probed.
        gpu_kwargs = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "keyframes_only": False, "fps": 1,
                      "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "detect_boxes": count, "cancel_check": None,
                      "start_time_s": START_TIME_S, "download_format": DOWNLOAD, "scale": 1}  # fmt: skip
        assert decodes.calls[1] == {**gpu_kwargs, "start_s": 5680.0, "length_s": 21.0}
        assert decodes.calls[2] == {**gpu_kwargs, "start_s": 5897.0, "length_s": 21.0}
        assert phases == ["Reading the credits…", "Refining the credits start…", "Finding where the credits end…"]
        assert len(probes) == 1

    def test_text_on_screen_all_through_the_tail_is_no_answer_and_nothing_more_is_decoded(self, monkeypatch, probes):
        # A running timecode on every keyframe (1 box on a lit frame), reading 3 boxes now and then after the first
        # 100 s: rule J finds a run 100 s into the tail, but every keyframe before it carries text, so there is no roll
        # and no refine window is decoded. It is read again at 640x360 like any tail without an answer -- Accused
        # (2020) S03E04 is this shape at 320x180 over story captions, with a roll only the larger read boxes -- and
        # reads the same there.
        timecode = [(5100.0 + 2 * i, 3 if i >= 50 and i % 5 == 0 else 1, 120.0) for i in range(450)]
        decodes = Decodes(timecode, timecode)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert rule_j.coarse_start(timecode).pts_s == 5200.0
        assert (result.start_s, result.end_s, result.fine_rows, result.end_rows) == (None, None, (), ())
        assert [(call["start_s"], call["keyframes_only"], call["scale"]) for call in decodes.calls] == [
            (5100.0, True, 1), (5100.0, True, 2)
        ]  # fmt: skip

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
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
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
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
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
        # one frame in 48 seconds, and a VP9 one only its keyframes. The answer ends in a scene, so the rest of the file
        # is read again at 640x360 -- a keyframe pass, thinned the same way, and seeking where the tail's pass did when
        # it keeps one packet in a stride (the stride counts from the seek).
        probes.thinning = frames.KeyframeThinning(keep_every, drop_non_key, DOWNLOAD)
        decodes = Decodes(STORY + ROLL + SCENE, FINE, END, STORY + ROLL + SCENE)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=gpu, gpu_device_path=device)  # fmt: skip
        assert (result.start_s, result.end_s) == (5690.0, 5899.0)
        assert decodes.calls[0] == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                                    "keyframes_only": True, "fps": None, "gpu": gpu, "gpu_device_path": device,
                                    "detect_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S, "download_format": DOWNLOAD,
                                    "keep_every": keep_every, "drop_non_key": drop_non_key, "scale": 1}  # fmt: skip
        refine = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "keyframes_only": False, "fps": 1, "gpu": gpu,
                  "gpu_device_path": device, "detect_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S, "download_format": DOWNLOAD,
                  "scale": 1}  # fmt: skip
        assert decodes.calls[1:3] == [{**refine, "start_s": 5680.0, "length_s": 21.0},
                                      {**refine, "start_s": 5897.0, "length_s": 21.0}]  # fmt: skip
        assert decodes.calls[3:] == [{**decodes.calls[0], "start_s": 5100.0 if keep_every else 5899.0, "scale": 2}]
        assert probes.thinning_calls == [{"path": MOVIE.canonical_path, "ffmpeg": "/ff", "cancel_check": None}]

    def test_a_keyframe_pass_with_no_frames_is_no_roll_and_nothing_more_is_decoded(self, monkeypatch, probes):
        # A VP9 file whose container flags no packet in the tail as a keyframe: every packet is dropped before the
        # decoder and ffmpeg exits 0 with no frames (measured on 8.1.2). On the CPU that is a tail without a roll, like
        # any file without a keyframe in its tail; on the GPU run_decode calls it a GPU failure, so the worker's CPU
        # rerun reaches this (TestDetect.test_a_gpu_decode_failure_is_a_codec_error_for_the_workers_cpu_rerun).
        probes.thinning = frames.KeyframeThinning(None, True, DOWNLOAD)
        decodes = Decodes([])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert result == detector.CreditsTextResult(None, None, (), (), ())
        assert len(decodes.calls) == 1 and decodes.calls[0]["drop_non_key"] is True

    def test_a_cancelled_job_is_not_probed_or_decoded(self, monkeypatch):
        monkeypatch.setattr(detector.frames, "probe_media", lambda path, **kwargs: pytest.fail("probed anyway"))
        monkeypatch.setattr(detector.frames, "decode_rows", lambda path, **kwargs: pytest.fail("decoded anyway"))
        with pytest.raises(frames.DecodeCancelledError, match="cancelled before decoding"):
            detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                  detect_boxes=count, gpu=None, gpu_device_path=None, cancel_check=lambda: True)  # fmt: skip

    def test_an_episode_reads_the_last_450_s(self, monkeypatch, probes):
        decodes = Decodes([])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                              detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert decodes.calls[0]["start_s"] == 870.0

    @pytest.mark.parametrize(
        ("path", "duration_ms", "is_episode", "tail_s", "start_s"),
        [
            (MOVIE.canonical_path, 6_000_000, False, None, 5100.0),  # Automatic: 900 s
            (MOVIE.canonical_path, 6_000_000, False, 1800.0, 4200.0),
            (MOVIE.canonical_path, 6_000_000, False, 300.0, 5700.0),
            (EPISODE.canonical_path, 1_320_000, True, None, 870.0),  # Automatic: 450 s
            (EPISODE.canonical_path, 1_320_000, True, 1200.0, 120.0),
            (EPISODE.canonical_path, 1_320_000, True, 300.0, 1020.0),
        ],
    )
    def test_the_tail_starts_tail_s_before_the_end_or_by_kind_when_none(
        self, monkeypatch, probes, path, duration_ms, is_episode, tail_s, start_s
    ):
        decodes = Decodes([])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        detector.find_credits(path, duration_ms=duration_ms, is_episode=is_episode, tail_s=tail_s, ffmpeg="/ff",
                              detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert decodes.calls[0]["start_s"] == start_s

    @pytest.mark.parametrize(
        "thinning",
        [
            frames.KeyframeThinning(None, False, "p010le"),
            frames.KeyframeThinning(48, False, DOWNLOAD),
            frames.KeyframeThinning(None, True, None),
        ],
    )
    def test_a_roll_the_tail_opens_on_is_read_from_before_the_tail(self, monkeypatch, probes, thinning):
        # An episode's 450 s tail starts at 870 s inside a roll that began at 858 s. The keyframes of the 120 s before
        # the tail are read through the same keyframe pass (an intra-only stride or VP9's packet drop included), the
        # rows the two windows share are kept once, and rule J runs on both: 108 s of story now precede the roll.
        probes.thinning = thinning
        tail = [(870.0 + 2 * i, 3, 10.0) for i in range(225)]  # 870-1318 s, the roll to the end of the file
        before = [(750.0 + 2 * i, 0, 120.0) for i in range(54)] + [(858.0 + 2 * i, 3, 10.0) for i in range(7)]
        fine = [(float(t), 0, 120.0) for t in range(838, 858)] + [(float(t), 3, 10.0) for t in range(858, 860)]
        decodes = Decodes(tail, before, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0")  # fmt: skip
        keyframe_pass = {"path": EPISODE.canonical_path, "ffmpeg": "/ff", "keyframes_only": True, "fps": None,
                         "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "detect_boxes": count, "cancel_check": None,
                         "start_time_s": START_TIME_S, "download_format": thinning.download_format,
                         "keep_every": thinning.keep_every,
                         "drop_non_key": thinning.drop_non_key, "scale": 1}  # fmt: skip
        assert decodes.calls[0] == {**keyframe_pass, "start_s": 870.0, "length_s": None}
        assert decodes.calls[1] == {**keyframe_pass, "start_s": 750.0, "length_s": 120.0}
        assert result.key_rows == (*before[:-1], *tail)  # the 870 s row of the window before is the tail's own
        assert (decodes.calls[2]["start_s"], decodes.calls[2]["length_s"]) == (838.0, 21.0)
        assert (result.start_s, result.end_s) == (858.0, None)

    def test_a_bug_over_the_story_doesnt_start_the_roll_early(self, monkeypatch, probes):
        # Spec §13 item 15, the shape this rule is for: a channel logo boxed now and then over lit story, and on every
        # keyframe of the night scene that ends it. A dark frame needs one box, so version 2 reads that scene as the
        # roll's opening and starts 30 s of story early. The logo is in the same place right across the story, so
        # version 3 doesn't count it inside the run and the start lands on the roll's first card -- in the keyframes
        # and in the 1 fps rows, which are read without it too.
        bug = (4, 6, 46, 24)
        story = [
            (5100.0 + 2 * i, 1, 120.0, (bug,)) if i % 4 == 0 else (5100.0 + 2 * i, 0, 120.0, ()) for i in range(285)
        ]
        night = [(5670.0 + 2 * i, 1, 12.0, (bug,)) for i in range(15)]  # 5670-5698 s: credit frames to version 2
        roll = [(5700.0 + 2 * i, 3, 12.0, (*CARDS, bug)) for i in range(100)]
        fine = [(float(t), 1, 12.0, (bug,)) for t in range(5680, 5700)]
        fine += [(float(t), 3, 12.0, (*CARDS, bug)) for t in range(5700, 5702)]
        decodes = Decodes(story + night + roll, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=5_910_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        rows = story + night + roll
        assert rule_j.overlay_boxes(rows) == (bug,)
        assert rule_j.coarse_start(rows).pts_s == 5670.0  # what version 2 answers
        assert result.start_s == 5700.0
        # The rows the result carries are the ones that were decoded, bug boxes and all; the overlays it read them
        # without ride along, because anything reading the rows back has to be handed them (`epilogue_like`).
        assert (result.key_rows, result.fine_rows, result.end_rows) == (tuple(rows), tuple(fine), ())
        assert result.overlays == (bug,)
        assert [(c["start_s"], c["length_s"]) for c in decodes.calls] == [(5010.0, None), (5680.0, 21.0)]

    def test_the_end_window_is_read_and_walked_without_the_bug(self, monkeypatch, probes):
        # A dark scene after the roll, under the same channel bug: to the rows as they were decoded every one of its
        # frames is a credit frame (dark, one box), so the end would walk 19 s into the scene -- or, with a longer
        # scene, stop being an end at all and let the skip run to the end of the file. The end keyframe, the end
        # window and the 1 fps walk all read the run without the bug.
        bug = (4, 6, 46, 24)
        story = [
            (5100.0 + 2 * i, 1, 120.0, (bug,)) if i % 4 == 0 else (5100.0 + 2 * i, 0, 120.0, ()) for i in range(300)
        ]
        roll = [(5700.0 + 2 * i, 3, 12.0, (*CARDS, bug)) for i in range(35)]  # 5700-5768 s
        # ffmpeg's keyframe rows aren't always increasing (T-R5). This one is the scene's first frame, emitted inside
        # the roll: a credit frame to the rows as they were decoded, and later than every card, so reading them would
        # put the run's latest credit frame 2 s into the scene and stretch the end window with it.
        roll.insert(30, (5770.0, 1, 12.0, (bug,)))
        scene = [(5772.0 + 2 * i, 1, 12.0, (bug,)) for i in range(59)]  # 5772-5888 s, dark, the bug its only text
        fine = [(float(t), 1, 120.0, (bug,)) for t in range(5680, 5700)]
        fine += [(float(t), 3, 12.0, (*CARDS, bug)) for t in range(5700, 5702)]
        end = [(float(t), 3, 12.0, (*CARDS, bug)) for t in range(5767, 5770)]
        end += [(float(t), 1, 12.0, (bug,)) for t in range(5770, 5792)]
        decodes = Decodes(story + roll + scene, fine, end)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        rows = story + roll + scene
        without = rule_j.without_overlays(rows, (bug,))
        coarse = rule_j.coarse_start(rows, without=without)
        assert rule_j.credits_end(without, coarse, end, 6000.0) == 5788.0  # reading the 1 fps rows as they are
        assert (result.start_s, result.end_s) == (5700.0, 5769.0)  # the roll's last 1 fps credit frame
        assert result.overlays == (bug,)  # the return path that carries an end keeps them too
        # The end window runs from the roll's last card to 20 s past it, not from the scene's last bug frame.
        assert (decodes.calls[2]["start_s"], decodes.calls[2]["length_s"]) == (5767.0, 21.0)

    def test_the_end_steps_back_over_scene_text_only_because_the_bug_is_gone(self, monkeypatch, probes):
        # Version 2's end step-back (§13 item 13) needs a lit keyframe with *no* text between the roll's last card and
        # the scene text the 24 s join glued on. Under a channel bug no keyframe ever reads zero boxes, so the step
        # back can only ever fire on the run read without it.
        bug = (4, 6, 46, 24)
        story = [
            (5100.0 + 2 * i, 1, 120.0, (bug,)) if i % 4 == 0 else (5100.0 + 2 * i, 0, 120.0, ()) for i in range(300)
        ]
        roll = [(5700.0 + 2 * i, 3, 12.0, (*CARDS, bug)) for i in range(21)]  # 5700-5740 s
        glued = [(5750.0, 1, 120.0, (bug,)), (5760.0, 4, 120.0, (*CARDS, CARDS[0], bug))]  # scene, then its text
        glued += [(5770.0 + 2 * i, 1, 120.0, (bug,)) for i in range(60)]
        fine = [(float(t), 1, 120.0, (bug,)) for t in range(5680, 5700)]
        fine += [(float(t), 3, 12.0, (*CARDS, bug)) for t in range(5700, 5702)]
        end = [(float(t), 3, 12.0, (*CARDS, bug)) for t in range(5739, 5742)]
        end += [(float(t), 1, 120.0, (bug,)) for t in range(5742, 5781)]
        decodes = Decodes(story + roll + glued, fine, end)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        rows = story + roll + glued
        coarse = rule_j.coarse_start(rows, without=rule_j.without_overlays(rows, (bug,)))
        assert rule_j.end_keyframe_s(rows, coarse) == 5760.0  # reading the run as it was decoded: no step back
        assert (decodes.calls[2]["start_s"], decodes.calls[2]["length_s"]) == (5739.0, 5760.0 + 20.0 - 5739.0)
        assert (result.start_s, result.end_s) == (5700.0, 5741.0)  # the skip stops on the roll, not in the scene

    def test_the_run_is_the_one_the_decoded_rows_give_not_the_suppressed_ones(self, monkeypatch, probes):
        # A night scene under the bug after the roll. Version 2 answers on it; version 3 keeps it as the run the rows
        # as decoded give, finds no credit frame of its own in it, and answers nothing. Dropping the bug's boxes
        # *before* the runs are found would hand the answer to the earlier roll instead -- on Mayday S11E11 that moved
        # a wrong answer 209 s earlier, onto story (evidence/eval/broadcast-tv.md).
        bug = (4, 6, 46, 24)
        lit = lambda t, i: (t, 1, 120.0, (bug,)) if i % 4 == 0 else (t, 0, 120.0, ())  # noqa: E731
        rows = [lit(5100.0 + 2 * i, i) for i in range(150)]
        rows += [(5400.0 + 2 * i, 2, 12.0, CARDS) for i in range(50)]  # a roll, with no bug on it
        rows += [lit(5500.0 + 2 * i, i) for i in range(100)]
        rows += [(5700.0 + 2 * i, 1, 12.0, (bug,)) for i in range(20)]  # the night scene the bug makes a run of
        decodes = Decodes(rows)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert rule_j.overlay_boxes(rows) == (bug,)
        assert rule_j.coarse_start(rows).pts_s == 5700.0  # what version 2 answers
        without = rule_j.without_overlays(rows, (bug,))
        assert rule_j.coarse_start(without).pts_s == 5400.0  # what suppressing before the runs would answer
        assert (result.start_s, result.fine_rows) == (None, ())
        assert result.overlays == (bug,)  # the no-answer return path keeps them as well
        assert [call["scale"] for call in decodes.calls] == [1, 2]  # the tail once at each size, nothing more

    def test_a_run_the_bug_leaves_too_short_asks_the_decoded_rows_for_the_runs(self, monkeypatch, probes):
        # The run starts 16 s into the tail, so opens_on_the_run is asked; without the bug it holds two credit frames
        # 4 s apart, which is no run at all. Everything that looks for the runs reads the rows as they were decoded --
        # asking opens_on_the_run for the suppressed ones is a ValueError on the empty run list.
        bug = (4, 6, 46, 24)
        tail = [(870.0 + 2 * i, 1, 120.0, (bug,)) if i % 2 == 0 else (870.0 + 2 * i, 0, 120.0, ()) for i in range(8)]
        tail += [(886.0 + 2 * i, 1, 12.0, (bug,)) for i in range(16)]  # 886-916 s under the bug
        tail = [(row[0], 2, 12.0, (bug, CARDS[0])) if row[0] in (890.0, 894.0) else row for row in tail]
        tail += [(918.0 + 2 * i, 0, 120.0, ()) for i in range(200)]
        decodes = Decodes(tail)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        without = rule_j.without_overlays(tail, rule_j.overlay_boxes(tail))
        coarse = rule_j.coarse_start(tail, without=without)
        assert rule_j.overlay_boxes(tail) == (bug,) and rule_j.credit_runs(without) == []
        assert coarse.pts_s == 890.0  # two frames left: a start, but no run
        with pytest.raises(ValueError, match="hold no run"):
            rule_j.opens_on_the_run(without, coarse)  # what asking the suppressed rows for the runs would do
        assert rule_j.opens_on_the_run(tail, coarse) is False  # story came first: nothing before the tail is read
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        # 20 s of tail before the run is under the 30 s floor, so there is no answer and no refine window either.
        assert (result.start_s, result.fine_rows) == (None, ())
        assert [call["scale"] for call in decodes.calls] == [1, 2]  # the tail once at each size, nothing more

    def test_a_roll_that_began_before_the_tail_keeps_the_tails_overlays(self, monkeypatch, probes):
        # The tail opens on a roll under the channel bug, so the 120 s before it are read. The tail holds under 30 s
        # of story, so it has no overlay of its own; the bug is boxed on the story the join just decoded, and the
        # overlays are not re-gathered from it -- a roll that began before the tail is exactly the shape that must not
        # be read as its own overlay. The answer is the roll's first card, as it is without the bug.
        bug = (4, 6, 46, 24)
        tail = [(870.0 + 2 * i, 4, 10.0, (*CARDS, CARDS[0], bug)) for i in range(225)]  # the roll to the end
        before = [
            (750.0 + 2 * i, 1, 120.0, (bug,)) if i % 4 == 0 else (750.0 + 2 * i, 0, 120.0, ()) for i in range(54)
        ]  # 750-856 s of story, the bug boxed on one keyframe in four
        before += [(858.0 + 2 * i, 4, 10.0, (*CARDS, CARDS[0], bug)) for i in range(7)]
        fine = [(float(t), 1, 120.0, (bug,)) if t % 4 == 0 else (float(t), 0, 120.0, ()) for t in range(838, 858)]
        fine += [(float(t), 4, 10.0, (*CARDS, CARDS[0], bug)) for t in range(858, 860)]
        decodes = Decodes(tail, before, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert rule_j.overlay_boxes(tail) == ()  # the tail's own story is 0 rows
        assert rule_j.overlay_boxes([*before[:-1], *tail]) == (bug,)  # re-gathering would call the roll's bug one
        assert (result.start_s, result.end_s) == (858.0, None)
        assert result.key_rows == (*before[:-1], *tail)
        assert result.overlays == ()  # the tail's own, which is none here -- not the joined rows' (bug,)
        assert [c["start_s"] for c in decodes.calls] == [870.0, 750.0, 838.0]

    def test_the_joined_rows_are_read_with_the_tails_own_overlays(self, monkeypatch, probes):
        # The one shape where the tail both takes the join and holds an overlay: four keyframes emitted after the
        # whole roll yet timestamped before its earliest frame, so they fall in the story `overlay_boxes` takes by
        # time while `opens_on_the_run`'s darkness check reads decode order. Nothing measured reorders that far, but
        # the branch has to apply the tail's overlays to the joined rows, not read them as they were decoded.
        bug = (4, 6, 46, 24)
        tail = [(870.0 + 2 * i, 4, 10.0, (*CARDS, CARDS[0], bug)) for i in range(225)]
        tail += [(t, 1, 120.0, (bug,)) for t in (862.0, 864.0, 866.5, 868.5)]
        before = [(750.0 + 2 * i, 0, 12.0, ()) for i in range(21)]
        before += [(792.0 + 2 * i, 1, 12.0, (bug,)) if i % 4 == 0 else (792.0 + 2 * i, 0, 12.0, ()) for i in range(33)]
        before += [(858.0 + 2 * i, 4, 10.0, (*CARDS, CARDS[0], bug)) for i in range(7)]
        fine = [(float(t), 0, 12.0, ()) for t in range(838, 858)]
        fine += [(float(t), 4, 10.0, (*CARDS, CARDS[0], bug)) for t in range(858, 860)]
        decodes = Decodes(tail, before, fine)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        assert rule_j.overlay_boxes(tail) == (bug,)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        joined = [*(row for row in before if row[0] < 862.0), *tail]
        assert rule_j.coarse_start(joined).pts_s == 800.0  # reading the joined rows as they were decoded
        assert result.start_s == 858.0
        assert result.overlays == (bug,)  # the tail's own, carried onto the joined rows

    def test_the_window_before_the_tail_is_judged_without_the_bug_too(self, monkeypatch, probes):
        # Version 3's two halves, at the join. The tail holds an overlay of its own, and the window the join decodes
        # is lit story carrying nothing but that same bug -- in the roll's band, on every keyframe. Read as decoded,
        # `joined_before` walks the start out of the tail and into that story, so the join is kept and the answer is
        # 108 s early. The tail's overlays are passed down, so the walk has nothing to step onto, the run stays inside
        # the tail, and the join is refused: the tail is judged alone and there is no answer.
        bug = (65, 160, 105, 175)  # a lower-third, in the cards' own band
        tail = [(870.0 + 2 * i, 3, 10.0, (*CARDS, bug)) for i in range(225)]
        tail += [(t, 1, 120.0, (bug,)) for t in (862.0, 864.0, 866.5, 868.5)]
        before = [(750.0 + 2 * i, 1, 120.0, (bug,)) for i in range(56)]  # 750-860 s of story under the bug
        decodes = Decodes(tail, before)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        overlays = rule_j.overlay_boxes(tail)
        assert overlays == (bug,)
        joined = [*(row for row in before if row[0] < 862.0), *tail]
        assert rule_j.joined_before(before, tail) is not None  # what reading the joined rows as decoded would do
        assert rule_j.coarse_start(joined).pts_s == 750.0  # the walk crossed the whole window on the bug
        assert rule_j.joined_before(before, tail, overlays=overlays) is None
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (result.start_s, result.end_s) == (None, None)
        assert result.key_rows == tuple(tail)

    def test_a_run_after_story_in_the_tails_first_30_s_reads_nothing_before_the_tail(self, monkeypatch, probes):
        # Story, then a 24 s run of captions 28 s into the tail, then story (a broadcast episode's CPU decode): story
        # came first, so nothing before the tail is read and the run is too close to the tail's start to answer.
        tail = [(870.0 + 2 * i, 0, 120.0) for i in range(14)] + [(898.0 + 2 * i, 2, 10.0) for i in range(13)]
        tail += [(924.0 + 2 * i, 0, 120.0) for i in range(170)]
        decodes = Decodes(tail)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert rule_j.coarse_start(tail).pts_s == 898.0
        assert (result.start_s, result.end_s) == (None, None)
        assert [call["scale"] for call in decodes.calls] == [1, 2]  # the tail once at each size, nothing more

    def test_rows_before_the_tail_that_dont_carry_the_run_on_are_dropped(self, monkeypatch, probes):
        # Captions on a night scene 10 s into the tail: nothing lit before them, so the window before the tail is read,
        # but it is lit story: the run didn't begin before the tail, so it is judged on the tail alone -- no answer.
        tail = [(870.0 + 2 * i, 0, 22.0) for i in range(5)] + [(880.0 + 2 * i, 1, 22.0) for i in range(12)]
        tail += [(904.0 + 2 * i, 0, 120.0) for i in range(200)]
        before = [(750.0 + 2 * i, 0, 120.0) for i in range(60)]
        decodes = Decodes(tail, before)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (decodes.calls[1]["start_s"], decodes.calls[1]["length_s"]) == (750.0, 120.0)
        assert (result.start_s, result.end_s, result.key_rows) == (None, None, tuple(tail))
        assert [(call["start_s"], call["scale"]) for call in decodes.calls] == [(870.0, 1), (750.0, 1), (870.0, 2),
                                                                               (750.0, 2)]  # fmt: skip

    def test_a_tail_less_than_120_s_into_the_file_reads_from_its_start(self, monkeypatch, probes):
        # A 500 s episode: the tail starts at 50 s, so the window before it is the file's first 50 s. It holds no
        # keyframe, so ffmpeg gives the first one after it -- the tail's own first row, which the join drops.
        tail = [(50.0 + 2 * i, 3, 10.0) for i in range(224)]
        decodes = Decodes(tail, tail[:1])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=500_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (decodes.calls[1]["start_s"], decodes.calls[1]["length_s"]) == (0.0, 50.0)
        assert (result.start_s, [call["scale"] for call in decodes.calls]) == (None, [1, 1, 2, 2])

    def test_a_roll_longer_than_the_rows_before_the_tail_too_gets_no_answer(self, monkeypatch, probes):
        # The window before the tail is the roll as well: the run still starts under 30 s after the first row read.
        # The 22 min episode's tail already starts before the last 25 % (990 s), so any start further back is one
        # the decision refuses, and no step after the first is read.
        tail = [(870.0 + 2 * i, 3, 10.0) for i in range(225)]
        before = [(750.0 + 2 * i, 3, 10.0) for i in range(60)]
        decodes = Decodes(tail, before)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=1_320_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None, earliest_start_s=990.0)  # fmt: skip
        assert (result.start_s, result.end_s, result.fine_rows) == (None, None, ())
        assert [(call["start_s"], call["scale"]) for call in decodes.calls] == [(870.0, 1), (750.0, 1), (870.0, 2),
                                                                               (750.0, 2)]  # fmt: skip

    def test_a_roll_less_than_30_s_into_the_tail_gets_no_answer(self, monkeypatch, probes):
        # A file shorter than its tail whose roll starts 10 s in. With under 30 s of the tail before the run, rule J
        # can't tell it from text on screen from the first frame (rule_j.STORY_BEFORE_RUN_S), and the tail starts at
        # the start of the file, so there is nothing before it to read: no answer and no refine window; version 1
        # refined it from 0 s.
        roll = [(10.0 + 2 * i, 2, 12.0) for i in range(20)]
        decodes = Decodes(roll, [])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(
            "/m/short.mkv",
            duration_ms=50_000,
            is_episode=False,
            ffmpeg="/ff",
            detect_boxes=count,
            gpu=None,
            gpu_device_path=None,
        )
        assert rule_j.coarse_start(roll).pts_s == 10.0
        assert (result.start_s, result.end_s) == (None, None)
        assert [call["scale"] for call in decodes.calls] == [1, 2]  # the tail once at each size, nothing more


LONG_EPISODE_S = 2640  # 44 min: the 450 s tail starts at 2190 s, the last 25 % at 1980 s


class FileDecodes:
    """Every decode answered from one file's rows cut to the window asked for, ``[start_s, start_s + length_s)`` (to the
    end with no length): lit story without text, then the roll from ``roll_from`` to the end, a keyframe every
    ``keyframe_s`` except in ``no_keyframes`` (a ``[from, to)`` span, as in a long GOP), and a 1 fps frame every second.
    A keyframe pass over a window with no keyframe in it gives the first keyframe after the window, as ffmpeg does
    (``evidence/credits/empty-window-decode.md``). Keyframe windows (by start) in ``timeouts`` run out of time, and in
    ``gpu_fails`` fail on the GPU."""

    def __init__(self, roll_from: float, *, duration_s: int = LONG_EPISODE_S, keyframe_s: int = 2,
                 no_keyframes: tuple[float, float] = (0.0, 0.0), timeouts: tuple[float, ...] = (),
                 gpu_fails: tuple[float, ...] = ()):  # fmt: skip
        self.roll_from, self.duration_s, self.keyframe_s = roll_from, duration_s, keyframe_s
        self.no_keyframes, self.timeouts, self.gpu_fails = no_keyframes, timeouts, gpu_fails
        self.calls: list[dict] = []
        self.worker: list[dict] = []  # each decode's pause check and threads, kept apart from its window
        self.on_decode = None  # called with each keyframe window's start and its pause check

    def row(self, t: float) -> tuple:
        return (t, 3, 10.0) if t >= self.roll_from else (t, 0, 120.0)

    def __call__(self, path, *, start_s, length_s, keyframes_only, **kwargs):
        self.worker.append({key: kwargs.pop(key, None) for key in ("pause_check", "ffmpeg_threads")})
        self.calls.append({"start_s": start_s, "length_s": length_s, "keyframes_only": keyframes_only, **kwargs})
        if keyframes_only and self.on_decode is not None:
            self.on_decode(start_s, self.worker[-1]["pause_check"])
        name = os.path.basename(path)
        if keyframes_only and start_s in self.timeouts:
            raise frames.DecodeTimeoutError(f"decoding {name} timed out")
        if keyframes_only and start_s in self.gpu_fails:
            raise frames.GpuDecodeError(f"ffmpeg exited 1 decoding {name} on the GPU")
        end_s = self.duration_s if length_s is None else start_s + length_s
        if not keyframes_only:
            return [self.row(float(t)) for t in range(0, self.duration_s) if start_s <= t < end_s]
        keyframes = [
            float(t) for t in range(0, self.duration_s, self.keyframe_s)
            if not self.no_keyframes[0] <= t < self.no_keyframes[1]
        ]  # fmt: skip
        inside = [t for t in keyframes if start_s <= t < end_s]
        after = [t for t in keyframes if t >= end_s][:1]
        return [self.row(t) for t in (inside or after)]

    def windows(self, *, keyframes_only: bool | None = None, scale: int = 1) -> list[tuple[float, float | None]]:
        """The windows decoded at ``scale`` (the 320x180 reading by default; 2 is the larger one after no answer)."""
        return [
            (c["start_s"], c["length_s"])
            for c in self.calls
            if keyframes_only in (None, c["keyframes_only"]) and c["scale"] == scale
        ]


class TestStepsBeforeTheTail:
    """Once the run has crossed the tail's edge, the step before the rows read so far is read and put in front while
    the run still starts under 30 s after their first row -- more of the roll, or the story its start needs -- no
    further back than 30 s before the earliest start the decision keeps, and all within one decode's time limit."""

    def _find(self, monkeypatch, decodes, tail_s=None, *, is_episode=True, earliest_start_s=None, gpu=None):
        # An episode on Automatic keeps a credits start from the last 25 % on (decide.earliest_credits_start_ms).
        earliest_s = 0.75 * decodes.duration_s if earliest_start_s is None else earliest_start_s
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        return detector.find_credits(EPISODE.canonical_path, duration_ms=decodes.duration_s * 1000,
                                     is_episode=is_episode, tail_s=tail_s, ffmpeg="/ff", detect_boxes=count, gpu=gpu,
                                     gpu_device_path="cuda:0" if gpu else None, earliest_start_s=earliest_s)  # fmt: skip

    def test_a_roll_that_began_200_s_before_the_tail_is_found_on_the_second_step(self, monkeypatch, probes):
        # The tail and the first step are all roll; the second step holds 40 s of story before the roll's first card.
        decodes = FileDecodes(1990.0)
        result = self._find(monkeypatch, decodes)
        assert (result.start_s, result.end_s) == (1990.0, None)
        assert decodes.windows() == [(2190.0, None), (2070.0, 120.0), (1950.0, 120.0), (1970.0, 21.0)]
        assert decodes.windows(scale=2) == []  # answered at 320x180: nothing is read larger
        assert result.key_rows[0][0] == 1950.0
        # The first step keeps the decode's own limit, exactly as before; a later one gets what is left of the shared one.
        assert "timeout_s" not in decodes.calls[0] and "timeout_s" not in decodes.calls[1]
        assert 0 < decodes.calls[2]["timeout_s"] <= detector.LOOK_BACK_TIMEOUT_S
        step = {k: v for k, v in decodes.calls[2].items() if k not in ("start_s", "length_s", "timeout_s")}
        assert step == {k: v for k, v in decodes.calls[1].items() if k not in ("start_s", "length_s")}

    def test_a_roll_that_began_before_the_earliest_start_kept_is_read_no_further_back(self, monkeypatch, probes):
        # Every step is the roll. The second reaches 1950 s, 30 s before the last 25 % (1980 s): a start further back
        # would be refused, so nothing more is read, and the run still starts at the first row read.
        decodes = FileDecodes(1200.0)
        result = self._find(monkeypatch, decodes)
        assert (result.start_s, result.end_s, result.fine_rows) == (None, None, ())
        assert decodes.windows() == [(2190.0, None), (2070.0, 120.0), (1950.0, 120.0)]
        assert result.key_rows[0][0] == 1950.0
        # No answer, so the 640x360 read goes through the same steps, bounded the same way.
        assert decodes.windows(scale=2) == decodes.windows()

    @pytest.mark.parametrize(
        ("roll_from", "start_s", "windows"),
        [
            (2400.0, 2400.0, [(2190.0, None)]),  # inside the tail after story: no step
            (2220.0, 2220.0, [(2190.0, None)]),  # 30 s into the tail after story: no step
            (2160.0, 2160.0, [(2190.0, None), (2070.0, 120.0)]),  # 30 s before the tail
            (2100.0, 2100.0, [(2190.0, None), (2070.0, 120.0)]),  # 90 s before: 30 s of story, just enough
            # Kept as it was: on the tail's first keyframe the step before is story, which the run doesn't carry on
            # into, so the join at the tail's edge is refused (a caption run on a night scene at the tail's start has
            # this shape too).
            (2190.0, None, [(2190.0, None), (2070.0, 120.0)]),
        ],
    )  # fmt: skip
    def test_where_the_tail_or_one_step_answers_nothing_more_is_read_and_the_result_is_the_same(
        self, monkeypatch, probes, roll_from, start_s, windows
    ):
        # Byte for byte what the single step gave: the whole result is compared against the same code reading the
        # first step only, and the only keyframe windows read are the tail and at most that step.
        decodes = FileDecodes(roll_from)
        result = self._find(monkeypatch, decodes)
        assert decodes.windows(keyframes_only=True) == windows
        assert result.start_s == start_s
        one_step = FileDecodes(roll_from)
        monkeypatch.setattr(detector, "_next_step_start", lambda read_from_s, earliest_start_s: None)
        assert self._find(monkeypatch, one_step) == result
        assert one_step.calls == decodes.calls

    @pytest.mark.parametrize(
        ("duration_s", "roll_from", "windows"),
        [
            # 20 s of story at the start of the first step: the story the start needs is the whole step before it.
            (LONG_EPISODE_S, 2090.0, [(2190.0, None), (2070.0, 120.0), (1950.0, 120.0)]),
            # 10 s into the second step of a 66 min episode (tail at 3510 s, last 25 % at 2970 s).
            (3960, 3280.0, [(3510.0, None), (3390.0, 120.0), (3270.0, 120.0), (3150.0, 120.0)]),
        ],
    )
    def test_a_roll_that_starts_in_a_steps_first_30_s_after_story_is_found(
        self, monkeypatch, probes, duration_s, roll_from, windows
    ):
        decodes = FileDecodes(roll_from, duration_s=duration_s)
        result = self._find(monkeypatch, decodes)
        assert result.start_s == roll_from
        assert decodes.windows(keyframes_only=True) == windows

    @pytest.mark.parametrize(
        ("credits_s", "start_s", "windows"),
        [
            # A 300 s window and 450 s of credits: the first step is all roll, the second holds 90 s of story first.
            (450, 2190.0, [(2340.0, None), (2220.0, 120.0), (2100.0, 120.0), (2170.0, 21.0)]),
            # The roll's first card is the first step's first keyframe: the second step is all story, and that story is
            # what the start needs before it.
            (420, 2220.0, [(2340.0, None), (2220.0, 120.0), (2100.0, 120.0), (2200.0, 21.0)]),
            # 20 s of story at the start of the first step: too little, so the second is read for more.
            (400, 2240.0, [(2340.0, None), (2220.0, 120.0), (2100.0, 120.0), (2220.0, 21.0)]),
        ],
    )  # fmt: skip
    def test_a_users_window_shorter_than_the_credits_steps_back_to_them(
        self, monkeypatch, probes, credits_s, start_s, windows
    ):
        decodes = FileDecodes(LONG_EPISODE_S - credits_s)
        result = self._find(monkeypatch, decodes, tail_s=300.0)
        assert result.start_s == start_s
        assert decodes.windows() == windows

    def test_a_movie_on_automatic_reads_no_step_after_the_first(self, monkeypatch, probes):
        # A 2 h movie: the 900 s tail starts where the 900 s cap does (6300 s), so the first step -- read as it always
        # was -- is already all before any start the decision keeps. A roll filling both gets no answer.
        decodes = FileDecodes(5000.0, duration_s=7200)
        result = self._find(monkeypatch, decodes, is_episode=False, earliest_start_s=6300.0)
        assert (result.start_s, result.fine_rows) == (None, ())
        assert decodes.windows() == [(6300.0, None), (6180.0, 120.0)]

    @pytest.mark.parametrize(
        ("roll_from", "start_s"),
        [
            (1500.0, None),  # began long before the line: read back to 30 s before it, no answer
            (2020.0, None),  # 5 s before the line: 24 s of story read before it, and none further back is read
            (2030.0, 2030.0),  # 5 s after it: the 30 s read before the line is its story
        ],
    )
    def test_a_chosen_tv_window_on_a_45_min_episode_steps_back_only_to_the_25_percent_line(
        self, monkeypatch, probes, roll_from, start_s
    ):
        # A 300 s window: the tail starts at 2400 s. A start before the last 25 % (2025 s) is refused -- it is outside
        # the window, so the window doesn't exempt it -- and the steps stop at 1995 s, the 30 s of story a start on
        # the line needs.
        decodes = FileDecodes(roll_from, duration_s=2700)
        result = self._find(monkeypatch, decodes, tail_s=300.0, earliest_start_s=2025.0)
        assert result.start_s == start_s
        assert decodes.windows(keyframes_only=True) == [
            (2400.0, None), (2280.0, 120.0), (2160.0, 120.0), (2040.0, 120.0), (1995.0, 45.0)
        ]  # fmt: skip

    def test_a_step_that_would_start_past_the_time_limit_is_a_timeout(self, monkeypatch, probes):
        # The first step used up the look-back's time (a stalled mount): the second is never started, and the file is
        # a timeout -- asked again the next day (T-R7) -- not a "nothing found" stored for good.
        monkeypatch.setattr(detector, "LOOK_BACK_TIMEOUT_S", 0.0)
        decodes = FileDecodes(1990.0)
        with pytest.raises(frames.DecodeTimeoutError, match="ran past"):
            self._find(monkeypatch, decodes)
        assert decodes.windows() == [(2190.0, None), (2070.0, 120.0)]

    def test_time_paused_during_a_step_doesnt_use_up_the_look_backs_time(self, monkeypatch, probes):
        # Everything is paused for longer than the whole look-back's limit while the first step decodes (its ffmpeg is
        # frozen, frames.run_decode): the second step still gets the time the pause didn't use.
        monkeypatch.setattr(detector, "LOOK_BACK_TIMEOUT_S", 1.0)
        paused = threading.Event()
        decodes = FileDecodes(1990.0)

        def pause_during_the_first_step(start_s, freeze):
            if start_s == 2070.0:
                paused.set()
                threading.Timer(1.5, paused.clear).start()
                freeze.hold()  # what run_decode does while the step's ffmpeg is frozen

        decodes.on_decode = pause_during_the_first_step
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        started = time.monotonic()
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=decodes.duration_s * 1000, is_episode=True,
                                       ffmpeg="/ff", detect_boxes=count, gpu=None, gpu_device_path=None,
                                       earliest_start_s=0.75 * decodes.duration_s, pause_check=paused.is_set)  # fmt: skip
        assert time.monotonic() - started >= 1.4
        assert (result.start_s, result.end_s) == (1990.0, None)
        assert decodes.windows() == [(2190.0, None), (2070.0, 120.0), (1950.0, 120.0), (1970.0, 21.0)]
        assert 0 < decodes.calls[2]["timeout_s"] <= 1.0

    def test_a_later_step_that_runs_out_of_time_is_a_timeout(self, monkeypatch, probes):
        decodes = FileDecodes(1990.0, timeouts=(1950.0,))
        with pytest.raises(frames.DecodeTimeoutError):
            self._find(monkeypatch, decodes)
        assert decodes.windows() == [(2190.0, None), (2070.0, 120.0), (1950.0, 120.0)]

    def test_a_step_under_30_s_is_never_read(self, monkeypatch, probes):
        # A 36 min 8 s episode with a keyframe every 5 s, the roll filling the tail and the first step. The last 25 %
        # is at 1626 s, so a later step could only read 1596-1598 s. That window holds no keyframe, so its decode would
        # give back the 1600 s keyframe the first step already read: a decode for nothing, and it isn't made.
        decodes = FileDecodes(1000.0, duration_s=2168, keyframe_s=5)
        result = self._find(monkeypatch, decodes, earliest_start_s=1626.0)
        assert (result.start_s, result.fine_rows) == (None, ())
        assert decodes.windows() == [(1718.0, None), (1598.0, 120.0)]
        assert result.key_rows[0] == decodes.row(1600.0)
        assert decodes(EPISODE.canonical_path, start_s=1596.0, length_s=2.0, keyframes_only=True) == [
            decodes.row(1600.0)
        ]

    def test_a_remainder_under_30_s_is_read_with_the_step_before_it(self, monkeypatch, probes):
        # A 45 min episode on Automatic: the tail starts at 2250 s and the last 25 % at 2025 s, so the steps may reach
        # 1995 s. A 120 s step from 2130 s would leave 15 s; the step takes it (135 s) instead of a sliver after it.
        decodes = FileDecodes(1500.0, duration_s=2700)
        result = self._find(monkeypatch, decodes)
        assert result.start_s is None
        assert decodes.windows() == [(2250.0, None), (2130.0, 120.0), (1995.0, 135.0)]

    def test_a_later_step_with_no_keyframe_in_it_adds_nothing(self, monkeypatch, probes):
        # No keyframe from 1940 s to 2076 s (a long GOP). The second step, 1950-2070 s, holds none, so ffmpeg gives the
        # 2076 s keyframe after it -- exit 0, on the GPU as on the CPU -- which is the first row the first step already
        # read, and the join drops it.
        decodes = FileDecodes(1990.0, no_keyframes=(1940.0, 2076.0))
        result = self._find(monkeypatch, decodes, gpu="NVIDIA")
        assert (result.start_s, result.fine_rows) == (None, ())
        assert decodes.windows() == [(2190.0, None), (2070.0, 120.0), (1950.0, 120.0)]
        times = [row[0] for row in result.key_rows]
        assert times[0] == 2076.0 and times.count(2076.0) == 1

    def test_a_gpu_failure_on_a_later_step_still_reaches_the_worker(self, monkeypatch, probes):
        decodes = FileDecodes(1990.0, gpu_fails=(1950.0,))
        with pytest.raises(frames.GpuDecodeError, match="exited 1"):
            self._find(monkeypatch, decodes, gpu="NVIDIA")

    def test_the_first_step_running_out_of_time_is_still_a_timeout(self, monkeypatch, probes):
        # As it always was: the file is left alone for a day (T-R7).
        decodes = FileDecodes(1990.0, timeouts=(2070.0,))
        with pytest.raises(frames.DecodeTimeoutError):
            self._find(monkeypatch, decodes)


class FakePool:
    def __init__(self):
        self.calls = []
        self.gpu_workers = []
        self.hooks = []

    def detect_boxes(self, planes, *, gpu, gpu_device_path, gpu_worker=None, on_cpu=None, cancel_check=None):
        self.calls.append((gpu, gpu_device_path))
        self.gpu_workers.append(gpu_worker)
        self.hooks.append((on_cpu, cancel_check))
        return [()] * len(planes)


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
                          now=lambda: clock.now, clock=clock, run_memo=lambda path: {},
                          settings=SimpleNamespace(credits_tv_s=None, credits_movie_s=None))  # fmt: skip
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
        ) == DetectorAnswer(
            (Candidate(MarkerType.CREDITS, 5_690_500, None, Source.CREDITS_TEXT),),
            "steps back to the earliest kept start",
        )
        (call,) = seen
        assert (call["path"], call["duration_ms"], call["is_episode"], call["ffmpeg"], call["gpu"], call["gpu_device_path"]) == (
            MOVIE.canonical_path, 6_000_000, False, "/usr/lib/jellyfin-ffmpeg/ffmpeg", "NVIDIA", "cuda:0")  # fmt: skip
        assert call["cancel_check"] is cancel
        # The GPU decode check is no worker's work: it runs in the background when a job builds the pool.
        assert "check_gpu_decode" not in call
        # Call it rather than checking it is set: a phase callback wired to something else would leave the worker row
        # showing the pipeline's last text for the whole decode. Equality, not identity — ``phase.append`` is a fresh
        # bound method on every access.
        call["phase"](detector.READING_PHASE)
        assert phase == [detector.READING_PHASE]
        call["detect_boxes"](frames.np.zeros((2, 180, 320), frames.np.uint8))
        assert pool.calls == [("NVIDIA", "cuda:0")]

    @pytest.mark.parametrize(
        ("which", "tv_s", "movie_s", "tail_s"),
        [
            ("movie", None, None, 900.0),
            ("episode", None, None, 450.0),
            ("movie", 1800, None, 900.0),
            ("episode", None, 1800, 450.0),
            ("movie", None, 1800, 1800.0),
            ("episode", 300, None, 300.0),
            ("movie", 300, 600, 600.0),
            ("episode", 300, 600, 300.0),
        ],
    )
    def test_the_tail_it_asks_for_is_the_users_window_for_the_files_kind(
        self, monkeypatch, pool, ctx, which, tv_s, movie_s, tail_s
    ):
        seen = self._find(monkeypatch, None)
        ctx.settings.credits_tv_s, ctx.settings.credits_movie_s = tv_s, movie_s
        rec = MOVIE if which == "movie" else EPISODE
        detector.detect_credits_text(rec, ctx=ctx)
        assert seen[0]["tail_s"] == tail_s
        assert seen[0]["is_episode"] is (which == "episode")

    @pytest.mark.parametrize(
        ("rec", "tv_s", "movie_s", "earliest_s"),
        [
            (MOVIE, None, None, 5100.0),  # a 100 min movie on Automatic: the 900 s cap
            (MOVIE, None, 1800, 4200.0),  # a 30 min movie window raises the cap to its own reach
            (MOVIE, None, 300, 5100.0),  # a small window never lowers the cap
            (EPISODE, None, None, 990.0),  # a 22 min episode: the last 25 %
            (EPISODE, 600, None, 720.0),  # a 10 min TV window reaches further, never before the middle
            # Neither a movie nor in a season: the movie window's tail, but no movie cap.
            (FileRecord(9, "/m/Some Show - Pilot.mkv", 100, 1, 6_000_000, None, False), None, None, 4500.0),
        ],
    )
    def test_the_steps_are_bounded_by_the_earliest_start_the_decision_keeps(
        self, monkeypatch, pool, ctx, rec, tv_s, movie_s, earliest_s
    ):
        seen = self._find(monkeypatch, None)
        ctx.settings.credits_tv_s, ctx.settings.credits_movie_s = tv_s, movie_s
        detector.detect_credits_text(rec, ctx=ctx)
        assert seen[0]["earliest_start_s"] == earliest_s

    def test_a_scene_after_the_roll_gives_the_candidate_its_end(self, monkeypatch, pool, ctx):
        self._find(monkeypatch, (5690.4996, 5899.0004))
        assert detector.detect_credits_text(MOVIE, ctx=ctx).candidates == (
            Candidate(MarkerType.CREDITS, 5_690_500, 5_899_000, Source.CREDITS_TEXT),
        )

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
        # "Nothing found" is stored with this build's basis too, or credits_text_due would ask it again every run.
        assert detector.detect_credits_text(rec, ctx=ctx) == DetectorAnswer((), "steps back to the earliest kept start")
        assert seen[0]["is_episode"] is episode

    def test_the_pause_and_the_workers_threads_are_handed_on(self, monkeypatch, pool, ctx):
        seen = self._find(monkeypatch, None)

        def paused():
            return False

        detector.detect_credits_text(MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0", pause_check=paused,
                                     ffmpeg_threads=3, fallback_callback=lambda _reason: None)  # fmt: skip
        assert seen[0]["pause_check"] is paused and seen[0]["ffmpeg_threads"] == 3

    def test_a_gpu_decode_failure_is_a_codec_error_for_the_workers_cpu_rerun(self, monkeypatch, pool, ctx):
        self._find(monkeypatch, frames.GpuDecodeError("the GPU decoded no frames from Movie (2020).mkv"))
        with pytest.raises(CodecNotSupportedError, match="no frames"):
            detector.detect_credits_text(MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0")

    @staticmethod
    def _checks(monkeypatch, *, matches=True, error=None):
        """The process's decode checks, run on fakes: every check decode is recorded, and the GPU's frames match the
        CPU's unless ``matches`` is False (or each decode raises ``error``)."""
        checked: list[dict] = []

        def decode_clip(ffmpeg, clip, *, scale, gpu, gpu_device_path, cancel_check, timeout_s):
            checked.append({"ffmpeg": ffmpeg, "clip": clip.name, "scale": scale, "gpu": gpu,
                            "device": gpu_device_path, "cancel_check": cancel_check})  # fmt: skip
            if error is not None:
                raise error
            return ((0.0, "the CPU's pixels" if matches or gpu is None else "other pixels"),)

        monkeypatch.setattr(decode_check, "_checks", decode_check.DecodeChecks(decode=decode_clip))
        return checked

    @staticmethod
    def _commands(monkeypatch):
        commands: list[dict] = []

        def run_decode(command, **kwargs):
            commands.append({"command": command, **kwargs})
            kwargs["detect_boxes"](frames.np.zeros((1, 180, 320), frames.np.uint8))
            return []

        monkeypatch.setattr(detector.frames, "run_decode", run_decode)
        return commands

    @pytest.mark.parametrize("pix_fmt_surfaces", [DOWNLOAD, None], ids=["4:2:0-surfaces", "ffmpeg-downloads"])
    def test_the_files_decodes_run_on_the_workers_gpu_with_its_threads_and_no_decode_check(
        self, monkeypatch, pool, ctx, probes, pix_fmt_surfaces
    ):
        # The worker model: a GPU worker decodes on its GPU, every codec included, with its GPU's ffmpeg_threads. The
        # decode check is a diagnostic run in the background when a job builds the pool (decode_check.start_checks),
        # never on a worker.
        probes.thinning = frames.KeyframeThinning(None, False, pix_fmt_surfaces)
        check_decodes = self._checks(monkeypatch, matches=False)
        commands = self._commands(monkeypatch)
        detector.detect_credits_text(MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0", ffmpeg_threads=3)
        assert check_decodes == []
        hwaccel = ["-hwaccel", "cuda", "-hwaccel_device", "0"]
        if pix_fmt_surfaces:
            hwaccel += ["-hwaccel_output_format", "cuda"]
            video_filter = "hwdownload,format=nv12,scale=320:180:flags=neighbor,format=nv12,showinfo"
        else:
            video_filter = "scale=320:180:flags=neighbor,format=nv12,showinfo"
        # The tail's keyframe pass, on the GPU either way.
        (call,) = commands
        assert call["command"] == [
            "/usr/lib/jellyfin-ffmpeg/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info",
            "-threads", "3", "-filter_threads", "3", *hwaccel, "-skip_frame", "nokey", "-ss", "5100.000", "-copyts",
            "-i", MOVIE.canonical_path, "-an", "-sn", "-dn", "-fps_mode", "passthrough", "-vf", video_filter,
            "-f", "rawvideo", "-",
        ]  # fmt: skip
        assert call["hw_active"] is True
        # Text detection is not the decode's: it stays on the worker's GPU (its own self-test decides it).
        assert pool.calls == [("NVIDIA", "cuda:0")]

    @pytest.mark.parametrize(
        ("gpu", "gpu_worker", "asks_as_gpu_worker"),
        [
            ("NVIDIA", True, True),
            (None, True, True),  # a GPU worker's CPU rerun after its GPU decode failed
            (None, False, False),  # a CPU worker
            ("NVIDIA", False, True),  # a caller that didn't say: a GPU is a GPU worker's
        ],
        ids=["gpu-worker", "gpu-workers-cpu-rerun", "cpu-worker", "gpu-unsaid"],
    )
    def test_text_detection_is_asked_as_the_workers_own_kind(self, monkeypatch, pool, ctx, gpu, gpu_worker,
                                                             asks_as_gpu_worker):  # fmt: skip
        seen = self._find(monkeypatch, None)
        device = "cuda:0" if gpu else None
        detector.detect_credits_text(MOVIE, ctx=ctx, gpu=gpu, gpu_device_path=device, gpu_worker=gpu_worker)
        seen[0]["detect_boxes"](frames.np.zeros((2, 180, 320), frames.np.uint8))
        assert pool.calls == [(gpu, device)]
        assert pool.gpu_workers == [asks_as_gpu_worker]

    def test_text_detection_reports_a_cpu_fallback_to_the_worker_row_and_stops_waiting_on_a_cancel(
        self, monkeypatch, pool, ctx
    ):
        # The pool calls on_cpu when a GPU worker's request is read on the CPU, and cancel_check ends a wait for a CPU
        # helper: both are the worker's own callbacks.
        seen = self._find(monkeypatch, None)
        shown = lambda reason: None  # noqa: E731 — identity is asserted
        cancel = lambda: False  # noqa: E731 — identity is asserted
        detector.detect_credits_text(
            MOVIE, ctx=ctx, gpu="NVIDIA", gpu_device_path="cuda:0", cancel_check=cancel, fallback_callback=shown
        )
        seen[0]["detect_boxes"](frames.np.zeros((2, 180, 320), frames.np.uint8))
        assert pool.hooks == [(shown, cancel)]

    def test_a_cancel_while_waiting_for_a_cpu_helper_is_no_answer_this_time(self, monkeypatch, ctx):
        from media_preview_generator.markers.credits.textdet_helper import TextDetCancelledError

        class CancelledPool:
            def detect_boxes(self, planes, **kwargs):
                raise TextDetCancelledError("cancelled")

        monkeypatch.setattr(detector, "get_textdet_pool", lambda: CancelledPool())
        seen = self._find(monkeypatch, None)
        detector.detect_credits_text(MOVIE, ctx=ctx, gpu=None, gpu_device_path=None)
        with pytest.raises(TextDetCancelledError):
            seen[0]["detect_boxes"](frames.np.zeros((2, 180, 320), frames.np.uint8))

    @pytest.mark.parametrize("scale", [1, 2], ids=["320x180", "640x360"])
    def test_a_cancel_while_text_detection_waits_is_no_answer_and_stores_nothing(
        self, monkeypatch, pool, ctx, probes, scale
    ):
        from media_preview_generator.markers.credits.textdet_helper import TextDetCancelledError

        answers = (TextDetCancelledError("cancelled"),) if scale == 1 else (STORY, TextDetCancelledError("cancelled"))
        monkeypatch.setattr(detector.frames, "decode_rows", Decodes(*answers))
        rec = ctx.store.upsert_file(FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns),
                                    duration_ms=MOVIE.duration_ms, season_key=None, is_movie=True)  # fmt: skip
        with pytest.raises(DetectorUnavailableError, match="^cancelled$"):
            detector.detect_credits_text(rec, ctx=ctx, gpu=None, gpu_device_path=None)
        assert ctx.store.get_detector_failure(rec.id, Source.CREDITS_TEXT) is None
        assert ctx.store.credits_text_timed_out_at(FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)) is None
        assert ctx.store.evidence_version(rec.id, Source.CREDITS_TEXT) is None

    def test_a_cpu_workers_decode_has_ffmpegs_own_thread_count(self, monkeypatch, pool, ctx, probes):
        commands = self._commands(monkeypatch)
        detector.detect_credits_text(MOVIE, ctx=ctx, gpu=None, gpu_device_path=None, ffmpeg_threads=None)
        (call,) = commands
        assert "-threads" not in call["command"] and "-filter_threads" not in call["command"]
        assert call["hw_active"] is False

    def test_the_harness_decodes_on_the_gpu_it_is_given_without_a_check(self, monkeypatch, probes):
        # find_credits alone (tools/markers_eval) measures the decode path it is handed.
        probes.thinning = frames.KeyframeThinning(None, False, DOWNLOAD)
        check_decodes = self._checks(monkeypatch, matches=False)
        decodes = Decodes([], [])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="ffmpeg",
                              detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0")  # fmt: skip
        assert check_decodes == []
        assert {(call["gpu"], call["gpu_device_path"]) for call in decodes.calls} == {("NVIDIA", "cuda:0")}

    @pytest.mark.parametrize(
        ("error", "message", "failed_here"),
        [
            # The file's own: rule 3 stops waiting for credit text on it (spec §5.5); it is still read on later runs.
            (frames.FrameDecodeError("ffmpeg exited 1 decoding Movie (2020).mkv on the CPU"), "exited 1", True),
            # The mount's, or the helper's: nothing about the file.
            (frames.ReadStalledError("2 earlier ffprobes are still stuck"), "still stuck", False),
            (frames.DecodeCancelledError("cancelled while decoding"), "cancelled", False),
            (TextDetUnavailableError("Text detection failed: the helper exited"), "Text detection failed", False),
        ],
    )
    def test_other_failures_are_no_answer_this_time_and_only_the_files_own_is_remembered(
        self, monkeypatch, pool, ctx, error, message, failed_here
    ):
        identity = FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)
        rec = ctx.store.upsert_file(identity, duration_ms=MOVIE.duration_ms, season_key=None, is_movie=True)
        self._find(monkeypatch, error)
        with pytest.raises(DetectorUnavailableError, match=message):
            detector.detect_credits_text(rec, ctx=ctx)
        assert ctx.store.credits_text_timed_out_at(identity) is None
        assert detector.credits_text_failed_here(rec, ctx) is failed_here
        assert detector.credits_text_needs_worker(rec, ctx) is True  # asked again on the next run either way

        replaced = ctx.store.upsert_file(
            FileIdentity(MOVIE.canonical_path, MOVIE.size + 1, MOVIE.mtime_ns), duration_ms=MOVIE.duration_ms,
            season_key=None, is_movie=True,
        )  # fmt: skip
        assert detector.credits_text_failed_here(replaced, ctx) is False

    def test_a_timed_out_640x360_reading_of_a_tail_with_no_answer_waits_a_day(self, monkeypatch, pool, ctx, probes):
        decodes = Decodes(STORY, frames.DecodeTimeoutError("decoding Movie (2020).mkv timed out after 600 s"))
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        with pytest.raises(DetectorUnavailableError, match="timed out after 600 s"):
            detector.detect_credits_text(MOVIE, ctx=ctx)
        assert [call["scale"] for call in decodes.calls] == [1, 2]
        identity = FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)
        assert ctx.store.credits_text_timed_out_at(identity) == NOW
        assert detector.credits_text_needs_worker(MOVIE, ctx) is False  # not decoded again for a day

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

    def test_a_later_step_before_the_tail_that_runs_out_of_time_waits_a_day_and_stores_nothing(
        self, monkeypatch, pool, ctx, probes
    ):
        # The real find_credits on a 44 min episode whose roll fills the tail and the first step; the second step
        # stalls. A stored "nothing found" would never be asked again; a timeout is, the next day (T-R7).
        rec = FileRecord(8, EPISODE.canonical_path, 100, 1, LONG_EPISODE_S * 1000, EPISODE.season_key, False)
        monkeypatch.setattr(detector.frames, "decode_rows", FileDecodes(1990.0, timeouts=(1950.0,)))
        with pytest.raises(DetectorUnavailableError, match="timed out"):
            detector.detect_credits_text(rec, ctx=ctx)
        identity = FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)
        assert ctx.store.credits_text_timed_out_at(identity) == NOW

    def test_a_cancel_between_steps_before_the_tail_is_a_cancel_not_a_timeout(self, monkeypatch, pool, ctx, probes):
        # The job is cancelled while the first step decodes, and by the time the second would start the look-back's
        # time is up too. The cancel is what happened: nothing is recorded, and the file isn't kept back for a day.
        monkeypatch.setattr(detector, "LOOK_BACK_TIMEOUT_S", 0.0)
        rec = FileRecord(8, EPISODE.canonical_path, 100, 1, LONG_EPISODE_S * 1000, EPISODE.season_key, False)
        decodes = FileDecodes(1990.0)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        with pytest.raises(DetectorUnavailableError, match="cancelled"):
            detector.detect_credits_text(rec, ctx=ctx, cancel_check=lambda: len(decodes.calls) >= 2)
        assert decodes.windows() == [(2190.0, None), (2070.0, 120.0)]
        identity = FileIdentity(rec.canonical_path, rec.size, rec.mtime_ns)
        assert ctx.store.credits_text_timed_out_at(identity) is None

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
        identity = FileIdentity(MOVIE.canonical_path, MOVIE.size, MOVIE.mtime_ns)
        rec = ctx.store.upsert_file(identity, duration_ms=MOVIE.duration_ms, season_key=None, is_movie=True)
        with pytest.raises(DetectorUnavailableError, match="could not read the video packets of Movie \\(2020\\).mkv"):
            detector.detect_credits_text(rec, ctx=ctx)
        assert ctx.store.credits_text_timed_out_at(identity) is None
        assert detector.credits_text_failed_here(rec, ctx) is False

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
        detector.credits_text_due,
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


def _generated_roll(path, ffmpeg: str, *, scene_s: int, gop: int, encoder: list[str] = H264, fontsize: int = 24) -> str:
    """420 s of text-free gradients, then 120 s of names scrolling up over black (a new line every 3 s, 20 px/s), then
    optionally a scene after the credits (Q3); a keyframe every ``gop`` frames at 24 fps, names ``fontsize`` px high
    on the 640x360 picture. The gradients' colours are given: left "random", ffmpeg draws them from a source its
    ``seed`` doesn't set, so every run made another picture (grey 94-170 on the first frame) and the small-text test
    answered at 320x180 on some of them."""
    import subprocess as sp

    roll = ",".join(
        f"drawtext=fontfile={FONT}:text='NAME {i}':fontcolor=white:fontsize={fontsize}:x=(w-tw)/2:y=h-20*t+{i * 60}"
        for i in range(40)
    )
    inputs = ["-f", "lavfi", "-i",
              "gradients=size=640x360:rate=24:speed=0.02:seed=3:c0=0x3050a0:c1=0xc08040,trim=duration=420,"
              "setpts=PTS-STARTPTS",  # fmt: skip
              "-f", "lavfi", "-i", f"color=c=black:size=640x360:rate=24:duration=120,{roll}"]  # fmt: skip
    if scene_s:
        inputs += [
            "-f",
            "lavfi",
            "-i",
            f"gradients=size=640x360:rate=24:speed=0.05:seed=9:c0=0x406030:c1=0xa0a0c0,trim=duration={scene_s},"
            "setpts=PTS-STARTPTS",
        ]
    streams = "".join(f"[{i}:v]" for i in range(len(inputs) // 4))
    sp.run([ffmpeg, "-v", "error", *inputs, "-filter_complex", f"{streams}concat=n={len(inputs) // 4}:v=1:a=0[v]", "-map", "[v]",
            *encoder, "-g", str(gop), "-pix_fmt", "yuv420p", str(path)], check=True)  # fmt: skip
    return str(path)


def _find_credits_on_the_cpu(clip: str, ffmpeg: str, *, duration_s: int, frames_read: dict[int, int] | None = None):
    """``find_credits`` with the CPU helper; ``frames_read`` counts the frames text detection read, by frame height."""
    from media_preview_generator.markers.credits import textdet_helper

    # A throwaway pool of this test's own, closed here: the app's singleton (``get_textdet_pool``) is never closed,
    # since ``close_all`` ends a pool permanently.
    pool = textdet_helper.TextDetectorPool()

    def detect_boxes(planes):
        if frames_read is not None:
            frames_read[planes.shape[1]] = frames_read.get(planes.shape[1], 0) + len(planes)
        return pool.detect_boxes(planes, gpu=None, gpu_device_path=None)

    try:
        return detector.find_credits(clip, duration_ms=duration_s * 1000, is_episode=False, ffmpeg=ffmpeg,
                                     detect_boxes=detect_boxes, gpu=None, gpu_device_path=None)  # fmt: skip
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
    # detection. Thinned before decode it reads about what the 2 s GOP encode of the same picture does at 320x180, and
    # answers the same start and end. The answer ends in a scene, so the rest of the file is read again at 640x360:
    # the GOP encode from the end, the intra-only one from the tail's own start (its stride counts from the seek),
    # which is its whole tail again, thinned the same way.
    import itertools

    answers = {}
    frames_read: dict[int, dict[int, int]] = {48: {}, 1: {}}
    for gop in (48, 1):
        clip = _generated_roll(tmp_path / f"gop{gop}.mkv", real_model, scene_s=60, gop=gop)
        answers[gop] = _find_credits_on_the_cpu(clip, real_model, duration_s=600, frames_read=frames_read[gop])
    normal, intra = answers[48], answers[1]
    assert normal.start_s is not None and intra.start_s is not None and abs(intra.start_s - normal.start_s) <= 2.0
    assert normal.end_s is not None and intra.end_s is not None and abs(intra.end_s - normal.end_s) <= 2.0
    assert min(later[0] - earlier[0] for earlier, later in itertools.pairwise(intra.key_rows)) >= 2.0
    assert len(intra.key_rows) <= 600 / frames.INTRA_ONLY_SPACING_S + 1
    assert frames_read[1][180] <= 1.1 * frames_read[48][180]
    assert frames_read[1][360] <= 600 / frames.INTRA_ONLY_SPACING_S + 1


@pytest.mark.integration
@pytest.mark.timeout(900)
def test_the_rest_of_an_intra_only_file_is_read_on_the_keyframes_the_320x180_reading_read(
    tmp_path, real_model, monkeypatch
):
    # The thinning keeps one packet in so many counted from the seek. Read again at 640x360 from the answer's end, an
    # all-I encode would keep other frames than the 320x180 reading did, and the text that reading boxed would match
    # none of them; seeking where the tail's pass did, the frames after the end are the same ones.
    clip = _generated_roll(tmp_path / "gop1.mkv", real_model, scene_s=60, gop=1)
    keyframe_times: dict[int, list[float]] = {1: [], 2: []}
    decode_rows = detector.frames.decode_rows

    def recording(path, *, keyframes_only, scale, **kwargs):
        rows = decode_rows(path, keyframes_only=keyframes_only, scale=scale, **kwargs)
        if keyframes_only:
            keyframe_times[scale].extend(round(row[0], 3) for row in rows)
        return rows

    monkeypatch.setattr(detector.frames, "decode_rows", recording)
    result = _find_credits_on_the_cpu(clip, real_model, duration_s=600)
    assert result.end_s is not None  # the 60 s scene follows it, so the rest of the file was read again
    after_the_end = [t for t in keyframe_times[2] if t >= result.end_s]
    assert len(after_the_end) >= 60 / frames.INTRA_ONLY_SPACING_S - 1
    assert set(after_the_end) <= set(keyframe_times[1])


@pytest.mark.integration
@pytest.mark.timeout(900)
def test_a_vp9_encode_is_read_from_its_keyframes_like_an_h264_one(tmp_path, real_model):
    # VP9's decoder ignores -skip_frame: without its non-key packets dropped before the decoder, the keyframe pass would
    # put all 14400 frames of this tail through text detection (24 a second). With the drop it reads the keyframes
    # the H.264 encode of the same picture has, and answers the same start and end.
    answers = {}
    frames_read: dict[str, dict[int, int]] = {"h264": {}, "vp9": {}}
    for name, encoder, suffix in (("h264", H264, "mkv"), ("vp9", VP9, "webm")):
        clip = _generated_roll(tmp_path / f"{name}.{suffix}", real_model, scene_s=60, gop=48, encoder=encoder)
        answers[name] = _find_credits_on_the_cpu(clip, real_model, duration_s=600, frames_read=frames_read[name])
    h264, vp9 = answers["h264"], answers["vp9"]
    assert h264.start_s is not None and vp9.start_s is not None and abs(vp9.start_s - h264.start_s) <= 2.0
    assert h264.end_s is not None and vp9.end_s is not None and abs(vp9.end_s - h264.end_s) <= 2.0
    assert len(vp9.key_rows) <= 600 / 2 + 5  # a keyframe every 2 s (and one at each cut the encoder chose)
    assert sum(frames_read["vp9"].values()) <= 1.1 * sum(frames_read["h264"].values())


@pytest.mark.integration
@pytest.mark.timeout(900)
def test_a_roll_too_small_to_box_at_320x180_is_found_at_640x360_on_a_real_decode(tmp_path, real_model):
    # Names 7 px high on the 640x360 picture: 3.5 px at 320x180, which boxes nothing (measured: 0 boxes at 6-7 px, 3 at
    # 640x360), so the 320x180 reading has no answer and the tail is read again at 640x360, which answers on the roll.
    clip = _generated_roll(tmp_path / "movie.mkv", real_model, scene_s=0, gop=48, fontsize=7)
    result = _find_credits_on_the_cpu(clip, real_model, duration_s=540)
    assert result.scale == 2
    assert result.start_s is not None and abs(result.start_s - 420.0) <= 10.0
