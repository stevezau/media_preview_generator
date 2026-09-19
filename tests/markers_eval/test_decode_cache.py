"""The credits text decode cache: one decode per file, command and decode code; the app's detector runs through it."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from media_preview_generator.markers.credits import detector, frames
from media_preview_generator.markers.probe import MediaProbe
from tools.markers_eval import credits_text as ct
from tools.markers_eval import decode_cache
from tools.markers_eval.decode_cache import DecodeCache

DUR = 6_000_000


class _Decoder:
    """Stands in for ffmpeg + text detection: records every real decode and probe, answers rows per window."""

    def __init__(self, monkeypatch, *, gpu_fails=False):
        self.decodes, self.probes = [], []
        self.gpu_fails = gpu_fails
        monkeypatch.setattr(frames, "decode_rows", self.decode_rows)
        monkeypatch.setattr(frames, "container_start_s", self.container_start_s)
        monkeypatch.setattr(frames, "keyframe_thinning", lambda *args, **kwargs: frames.KeyframeThinning())

    def container_start_s(self, path, ffmpeg, *, cancel_check=None, timeout_s=frames.PROBE_TIMEOUT_S):
        self.probes.append(path)
        return 0.0

    def decode_rows(self, path, **kwargs):
        self.decodes.append(kwargs)
        if self.gpu_fails and kwargs["gpu"] is not None:
            raise frames.GpuDecodeError("ffmpeg exited 69 on the GPU")
        if kwargs["keyframes_only"]:  # the tail: story, then a dark roll from 5700 s
            return [(float(t), 0, 120.0) for t in range(5100, 5700, 4)] + [
                (float(t), 2, 10.0) for t in range(5700, 5990, 2)
            ]
        start = kwargs["start_s"]
        window = range(int(start), int(start + (kwargs["length_s"] or 21)))
        return [(float(t), 2, 10.0) if t >= 5700 else (float(t), 0, 120.0) for t in window]


def _kwargs(**overrides):
    kwargs = {"ffmpeg": "/ff", "start_s": 5100.0, "length_s": None, "keyframes_only": True, "fps": None, "gpu": "NVIDIA",
              "gpu_device_path": "cuda:0", "count_boxes": lambda p: [0] * len(p), "start_time_s": 0.0}  # fmt: skip
    return {**kwargs, **overrides}


@pytest.fixture
def media(tmp_path):
    path = tmp_path / "A (2001).mkv"
    path.write_bytes(b"x")
    return path


def test_another_ffmpeg_build_is_another_decode_for_rows_and_probes(tmp_path, monkeypatch, media):
    decoder = _Decoder(monkeypatch)
    asked = []

    def build(version):
        def answer(ffmpeg):
            asked.append((version, ffmpeg))
            return f"ffmpeg version {version}"

        return answer

    for version in ("8.0.1", "8.0.1", "8.1"):
        cache = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu", build=build(version))
        cache.decode_rows(str(media), **_kwargs(start_time_s=None))
        cache.decode_rows(str(media), **_kwargs(start_time_s=None))
    # One decode and one start-time probe per build; each cache asks its binary's build once.
    assert (len(decoder.decodes), len(decoder.probes)) == (2, 2)
    assert asked == [("8.0.1", "/ff"), ("8.0.1", "/ff"), ("8.1", "/ff")]


def test_the_build_is_the_first_line_ffmpeg_prints_for_its_version(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout="ffmpeg version 8.0.1-3ubuntu2 Copyright (c) 2000-2025\nbuilt with gcc 15\n")

    monkeypatch.undo()  # the real ffmpeg_build, not the autouse stand-in
    monkeypatch.setattr(decode_cache.subprocess, "run", run)
    assert decode_cache.ffmpeg_build("/usr/bin/ffmpeg") == "ffmpeg version 8.0.1-3ubuntu2 Copyright (c) 2000-2025"
    assert calls == [(["/usr/bin/ffmpeg", "-version"], {"capture_output": True, "text": True, "check": True,
                                                        "timeout": 30})]  # fmt: skip


def test_a_decode_runs_once_per_file_command_and_decode_code(tmp_path, monkeypatch, media):
    decoder = _Decoder(monkeypatch)
    cache = DecodeCache(tmp_path / "cache", digest="code-1", backend=lambda: "gpu cuda:0")
    first = cache.decode_rows(str(media), **_kwargs())
    assert cache.decode_rows(str(media), **_kwargs()) == first
    assert (
        DecodeCache(tmp_path / "cache", digest="code-1", backend=lambda: "gpu cuda:0").decode_rows(
            str(media), **_kwargs()
        )
        == first
    )
    assert len(decoder.decodes) == 1
    # Every part of the command is in the key: the window, the frame choice, the decode path.
    for changed in ({"start_s": 5101.0}, {"length_s": 21.0}, {"keyframes_only": False, "fps": 1}, {"gpu": None},
                    {"start_time_s": 30000.0}, {"keep_every": 48}, {"drop_non_key": True},
                    {"keep_every": 48, "drop_non_key": True}):  # fmt: skip
        cache.decode_rows(str(media), **_kwargs(**changed))
    assert len(decoder.decodes) == 9
    DecodeCache(tmp_path / "cache", digest="code-2", backend=lambda: "gpu cuda:0").decode_rows(str(media), **_kwargs())
    assert len(decoder.decodes) == 10
    os.utime(media, ns=(1, 1))  # a replaced file is another file
    cache.decode_rows(str(media), **_kwargs())
    assert len(decoder.decodes) == 11
    assert (cache.decoded, cache.reused) == (10, 1)


def test_the_backend_that_counted_the_boxes_is_in_the_key(tmp_path, monkeypatch, media):
    # A GPU run's CPU rerun of a file the card can't decode builds exactly the CPU run's command, but counts its boxes
    # on the GPU helper: the CPU run must not be handed those rows, nor the GPU run the CPU's.
    decoder = _Decoder(monkeypatch)
    cpu_command = _kwargs(gpu=None, gpu_device_path=None)
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "webgpu cuda:0").decode_rows(str(media), **cpu_command)
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu").decode_rows(str(media), **cpu_command)
    assert len(decoder.decodes) == 2
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu").decode_rows(str(media), **cpu_command)
    assert len(decoder.decodes) == 2


def test_a_gpu_worker_whose_self_test_chose_the_cpu_shares_the_cpu_rows(tmp_path, monkeypatch, media):
    # The key is the backend the helper really used, not the one asked for: a GPU run whose self-test picked the CPU
    # counted exactly as a CPU run does, and a later run on a GPU the self-test kept doesn't get those rows.
    decoder = _Decoder(monkeypatch)
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu").decode_rows(str(media), **_kwargs())
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu").decode_rows(str(media), **_kwargs())
    assert len(decoder.decodes) == 1
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "webgpu cuda:0").decode_rows(str(media), **_kwargs())
    assert len(decoder.decodes) == 2


@pytest.mark.parametrize(
    ("before", "after", "kept_under"),
    [
        (None, "webgpu cuda:0", "webgpu cuda:0"),  # the helper's first request: kept under what it chose
        ("webgpu cuda:0", "cpu", None),            # demoted mid-decode: counted by both, never kept
        (None, None, None),                        # nothing counted: no backend to keep it under
    ],
)  # fmt: skip
def test_rows_are_kept_only_under_the_one_backend_that_counted_them(tmp_path, monkeypatch, media, before, after,
                                                                    kept_under):  # fmt: skip
    decoder = _Decoder(monkeypatch)
    answers = iter([before, after])
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: next(answers)).decode_rows(str(media), **_kwargs())
    served_from_disk = []
    for backend in ("webgpu cuda:0", "cpu"):
        cache = DecodeCache(tmp_path / "cache", digest="d", backend=lambda b=backend: b)
        cache.decode_rows(str(media), **_kwargs())
        if cache.reused:
            served_from_disk.append(backend)
    # A decode kept under one backend is reused by that backend's run only; every other run decodes again.
    assert served_from_disk == ([kept_under] if kept_under else [])
    assert len(decoder.decodes) == (2 if kept_under else 3)


def test_the_packet_probe_runs_once_per_file_and_its_failures_are_not_kept(tmp_path, monkeypatch, media):
    calls = []
    vp9_intra = frames.KeyframeThinning(48, True)

    def thinning(path, ffmpeg, *, cancel_check=None, timeout_s=frames.PROBE_TIMEOUT_S):
        calls.append((path, ffmpeg, cancel_check, timeout_s))
        if len(calls) == 1:
            raise frames.DecodeTimeoutError("reading the video packets timed out")
        return vp9_intra

    monkeypatch.setattr(frames, "keyframe_thinning", thinning)
    cache = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu")
    with pytest.raises(frames.DecodeTimeoutError):
        cache.keyframe_thinning(str(media), "/ff")
    cancelled = object()
    assert cache.keyframe_thinning(str(media), "/ff", cancel_check=cancelled, timeout_s=12.0) == vp9_intra
    # Both fields come back from disk, as the type the detector reads them from.
    stored = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu").keyframe_thinning(str(media), "/ff")
    assert stored == vp9_intra and isinstance(stored, frames.KeyframeThinning)
    assert calls == [(str(media), "/ff", None, frames.PROBE_TIMEOUT_S), (str(media), "/ff", cancelled, 12.0)]
    monkeypatch.setattr(frames, "keyframe_thinning", lambda *args, **kwargs: frames.KeyframeThinning())
    other = tmp_path / "B (2002).mkv"
    other.write_bytes(b"y")
    fresh = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu")
    assert fresh.keyframe_thinning(str(other), "/ff") == frames.KeyframeThinning()
    monkeypatch.setattr(frames, "keyframe_thinning", lambda *args, **kwargs: pytest.fail("probed again"))
    fresh = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu")
    assert fresh.keyframe_thinning(str(other), "/ff") == frames.KeyframeThinning()


def test_the_probes_take_exactly_the_arguments_the_real_ones_take():
    for name in ("container_start_s", "keyframe_thinning"):
        real = inspect.signature(getattr(frames, name)).parameters
        served = inspect.signature(getattr(DecodeCache, name)).parameters
        assert [p for p in served if p != "self"] == list(real), name
        assert [served[p].default for p in real] == [real[p].default for p in real], name


def test_a_write_cut_short_leaves_no_entry_behind(tmp_path, monkeypatch, media):
    # A run killed while writing must not leave half a JSON file that every later run fails to read.
    from tools.markers_eval import decode_cache

    decoder = _Decoder(monkeypatch)
    real_dump = decode_cache.json.dump

    def dies(data, handle):
        handle.write('{"rows": [[5100.0, 0')
        raise KeyboardInterrupt

    monkeypatch.setattr(decode_cache.json, "dump", dies)
    with pytest.raises(KeyboardInterrupt):
        DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu").decode_rows(str(media), **_kwargs())
    assert list((tmp_path / "cache/credits_decodes").glob("*.json")) == []
    monkeypatch.setattr(decode_cache.json, "dump", real_dump)
    rows = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "cpu").decode_rows(str(media), **_kwargs())
    assert rows == decoder.decode_rows(str(media), **_kwargs()) and len(decoder.decodes) == 3


def test_the_real_decode_gets_every_argument_it_was_asked_for(tmp_path, monkeypatch, media):
    decoder = _Decoder(monkeypatch)
    count_boxes = object()
    DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "gpu cuda:0").decode_rows(
        str(media),
        **_kwargs(
            start_s=5680.0,
            length_s=21.0,
            keyframes_only=False,
            fps=1,
            count_boxes=count_boxes,
            start_time_s=12.5,
            timeout_s=90.0,
            keep_every=48,
            drop_non_key=True,
        ),  # fmt: skip
    )
    (kwargs,) = decoder.decodes
    assert kwargs == {"ffmpeg": "/ff", "start_s": 5680.0, "length_s": 21.0, "keyframes_only": False, "fps": 1,
                      "gpu": "NVIDIA", "gpu_device_path": "cuda:0", "count_boxes": count_boxes, "cancel_check": None,
                      "timeout_s": 90.0, "start_time_s": 12.5, "keep_every": 48, "drop_non_key": True}  # fmt: skip


def test_the_cache_takes_exactly_the_arguments_the_real_decode_takes():
    # The cache stands in for frames.decode_rows while the detector runs: an argument the detector passes that the
    # cache doesn't take crashes every harness run, and one it takes but drops decodes the wrong thing.
    real = inspect.signature(frames.decode_rows).parameters
    served = inspect.signature(DecodeCache.decode_rows).parameters
    assert [name for name in served if name != "self"] == list(real)
    assert [served[name].default for name in real] == [real[name].default for name in real]


def test_the_start_time_is_probed_once_per_file_when_not_given(tmp_path, monkeypatch, media):
    decoder = _Decoder(monkeypatch)
    cache = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "gpu cuda:0")
    cache.decode_rows(str(media), **_kwargs(start_time_s=None))
    cache.decode_rows(str(media), **_kwargs(start_time_s=None))
    assert decoder.probes == [str(media)]
    assert decoder.decodes[0]["start_time_s"] == 0.0


def test_a_gpu_decode_failure_is_kept_and_raised_again_without_decoding(tmp_path, monkeypatch, media):
    decoder = _Decoder(monkeypatch, gpu_fails=True)
    cache = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "gpu cuda:0")
    for _ in range(2):
        with pytest.raises(frames.GpuDecodeError, match="exited 69"):
            cache.decode_rows(str(media), **_kwargs())
    assert len(decoder.decodes) == 1


def test_a_failure_that_is_not_the_gpus_is_never_kept(tmp_path, monkeypatch, media):
    # A timeout or a cancel is "no answer this time": the next run must decode again.
    calls = []

    def times_out(path, **kwargs):
        calls.append(kwargs)
        raise frames.DecodeTimeoutError("decoding timed out after 600 s")

    monkeypatch.setattr(frames, "decode_rows", times_out)
    cache = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "gpu cuda:0")
    for _ in range(2):
        with pytest.raises(frames.DecodeTimeoutError):
            cache.decode_rows(str(media), **_kwargs())
    assert len(calls) == 2


def test_serving_puts_the_cache_in_front_of_the_frames_module_and_always_restores_it(tmp_path, monkeypatch):
    _Decoder(monkeypatch)
    names = ("decode_rows", "container_start_s", "keyframe_thinning")
    real = [getattr(frames, name) for name in names]
    cache = DecodeCache(tmp_path / "cache", digest="d", backend=lambda: "gpu cuda:0")
    with cache.serving():
        assert [getattr(frames, name) for name in names] == [getattr(cache, name) for name in names]
    assert [getattr(frames, name) for name in names] == real
    with pytest.raises(RuntimeError), cache.serving():
        raise RuntimeError("the detector failed")
    assert [getattr(frames, name) for name in names] == real


def test_the_cache_refuses_data_folders():
    with pytest.raises(ValueError, match="/data"):
        DecodeCache(Path("/data/cache"), digest="d", backend=lambda: "cpu")


def test_a_rule_change_reruns_the_apps_detector_on_stored_decodes(tmp_path, monkeypatch, media):
    # The point of the cache: the answer cache misses on a new detector digest, the app's own find_credits runs again,
    # and every window it asks for is one it asked for before.
    decoder = _Decoder(monkeypatch)
    decodes = DecodeCache(tmp_path / "cache", digest="decode-code", backend=lambda: "gpu cuda:0")

    def cache():
        return ct.CreditsTextCache(tmp_path / "cache", ffmpeg="/ff", decode="gpu", gpu_device="cuda:0",
                                   count_boxes=lambda p: [0] * len(p), backend=lambda: "gpu cuda:0",
                                   probe=lambda p: MediaProbe(DUR, ()), decodes=decodes)  # fmt: skip

    monkeypatch.setattr(ct, "detector_digest", lambda: "rule-1")
    first = cache().result(str(media), is_episode=False)
    windows = len(decoder.decodes)
    assert first["start_s"] == 5700.0 and windows == 2  # the tail, then the refine window
    monkeypatch.setattr(ct, "detector_digest", lambda: "rule-2")
    assert cache().result(str(media), is_episode=False) == first
    assert len(decoder.decodes) == windows
    assert frames.decode_rows == decoder.decode_rows  # served only while the detector ran
    # A rule that asks for another window decodes just that one.
    monkeypatch.setattr(detector.rule_j, "REFINE_BEFORE_S", 30.0)
    monkeypatch.setattr(ct, "detector_digest", lambda: "rule-3")
    cache().result(str(media), is_episode=False)
    assert len(decoder.decodes) == windows + 1
    assert decoder.decodes[-1]["start_s"] == 5670.0


def test_the_decode_digest_ignores_the_rule_and_follows_everything_else(tmp_path):
    patterns = ("markers/credits/*.py", "processing/hwaccel.py")
    for path in ("markers/credits/rule_j.py", "markers/credits/detector.py", "markers/credits/frames.py",
                 "processing/hwaccel.py"):  # fmt: skip
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text("A = 1\n")
    before = ct.decode_digest(tmp_path, patterns)
    for rule_file in ct.RULE_FILES:
        (tmp_path / rule_file).write_text("A = 2\n")
    assert ct.decode_digest(tmp_path, patterns) == before
    assert ct.detector_digest(tmp_path, patterns) != ct.detector_digest(tmp_path, patterns, leave_out=ct.RULE_FILES)
    for decode_file in ("markers/credits/frames.py", "processing/hwaccel.py"):
        (tmp_path / decode_file).write_text("A = 3\n")
        assert ct.decode_digest(tmp_path, patterns) != before
        before = ct.decode_digest(tmp_path, patterns)
