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


def count(planes):
    return [()] * len(planes)


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
                                       detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0", phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.fine_rows, result.end_rows) == (None, None, (), ())
        (call,) = decodes.calls
        assert call == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                        "keyframes_only": True, "fps": None, "gpu": "NVIDIA", "gpu_device_path": "cuda:0",
                        "detect_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S,
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
                                       detect_boxes=count, gpu="NVIDIA", gpu_device_path="cuda:0", phase=phases.append)  # fmt: skip
        assert (result.start_s, result.end_s, result.end_rows) == (5690.0, 5899.0, tuple(END))
        # Both refine decodes by whole-dict equality: they stay on the worker's GPU (a refine decode quietly dropped to
        # the CPU would raise FrameDecodeError instead of the GpuDecodeError the worker's CPU rerun needs), and all three
        # decodes read the one start time this run probed.
        gpu_kwargs = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "keyframes_only": False, "fps": 1,
                      "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "detect_boxes": count, "cancel_check": None,
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
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
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
        # one frame in 48 seconds, and a VP9 one only its keyframes.
        probes.thinning = frames.KeyframeThinning(keep_every, drop_non_key)
        decodes = Decodes(STORY + ROLL + SCENE, FINE, END)
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(MOVIE.canonical_path, duration_ms=6_000_000, is_episode=False, ffmpeg="/ff",
                                       detect_boxes=count, gpu=gpu, gpu_device_path=device)  # fmt: skip
        assert (result.start_s, result.end_s) == (5690.0, 5899.0)
        assert decodes.calls[0] == {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "start_s": 5100.0, "length_s": None,
                                    "keyframes_only": True, "fps": None, "gpu": gpu, "gpu_device_path": device,
                                    "detect_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S,
                                    "keep_every": keep_every, "drop_non_key": drop_non_key}  # fmt: skip
        refine = {"path": MOVIE.canonical_path, "ffmpeg": "/ff", "keyframes_only": False, "fps": 1, "gpu": gpu,
                  "gpu_device_path": device, "detect_boxes": count, "cancel_check": None, "start_time_s": START_TIME_S}  # fmt: skip
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
        [frames.KeyframeThinning(), frames.KeyframeThinning(48, False), frames.KeyframeThinning(None, True)],
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
                         "start_time_s": START_TIME_S, "keep_every": thinning.keep_every,
                         "drop_non_key": thinning.drop_non_key}  # fmt: skip
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
        assert len(decodes.calls) == 1

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
        assert len(decodes.calls) == 1

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
        assert len(decodes.calls) == 1

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
        assert len(decodes.calls) == 2

    def test_a_tail_less_than_120_s_into_the_file_reads_from_its_start(self, monkeypatch, probes):
        # A 500 s episode: the tail starts at 50 s, so the window before it is the file's first 50 s. It holds no
        # keyframe, so ffmpeg gives the first one after it -- the tail's own first row, which the join drops.
        tail = [(50.0 + 2 * i, 3, 10.0) for i in range(224)]
        decodes = Decodes(tail, tail[:1])
        monkeypatch.setattr(detector.frames, "decode_rows", decodes)
        result = detector.find_credits(EPISODE.canonical_path, duration_ms=500_000, is_episode=True, ffmpeg="/ff",
                                       detect_boxes=count, gpu=None, gpu_device_path=None)  # fmt: skip
        assert (decodes.calls[1]["start_s"], decodes.calls[1]["length_s"]) == (0.0, 50.0)
        assert (result.start_s, len(decodes.calls)) == (None, 2)

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
        assert len(decodes.calls) == 2

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
        assert len(decodes.calls) == 1


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

    def row(self, t: float) -> tuple:
        return (t, 3, 10.0) if t >= self.roll_from else (t, 0, 120.0)

    def __call__(self, path, *, start_s, length_s, keyframes_only, **kwargs):
        self.calls.append({"start_s": start_s, "length_s": length_s, "keyframes_only": keyframes_only, **kwargs})
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

    def windows(self, *, keyframes_only: bool | None = None) -> list[tuple[float, float | None]]:
        return [(c["start_s"], c["length_s"]) for c in self.calls if keyframes_only in (None, c["keyframes_only"])]


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

    def detect_boxes(self, planes, *, gpu, gpu_device_path):
        self.calls.append((gpu, gpu_device_path))
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
        # "Nothing found" is stored with the look-back's basis too, or credits_text_due would ask it again every run.
        assert detector.detect_credits_text(rec, ctx=ctx) == DetectorAnswer((), "steps back to the earliest kept start")
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

    def detect_boxes(planes):
        if frames_read is not None:
            frames_read.append(len(planes))
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
