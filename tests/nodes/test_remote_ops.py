"""nodes.remote_ops: each op run on a node gives back, or raises, exactly what the local function does.

The node is a loopback (``executor_fakes.LoopbackClient``): ``nodes.server.ops`` run in this process on the same path,
so the whole executor path -- spec, progress, artifact, errors -- runs between the two. The real-media tests run the
real functions both ways on a small clip made with FFmpeg and compare the answers.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess

import numpy as np
import pytest

from media_preview_generator.loudness import analyze
from media_preview_generator.markers.audio import end_picture, fingerprint
from media_preview_generator.markers.credits import detector, frames, textdet_helper
from media_preview_generator.nodes import executor, protocol, remote_ops
from media_preview_generator.nodes.executor import Placement, placed_on
from media_preview_generator.nodes.registry import set_registry
from media_preview_generator.nodes.server import ops
from media_preview_generator.processing import generator
from media_preview_generator.processing.generator import (
    FALLBACK_HWACCEL,
    FALLBACK_STALL,
    CancellationError,
    CodecNotSupportedError,
    WorkingFolderError,
    failure_scope,
    get_failures,
)

from .executor_fakes import NODE_ID, LoopbackClient, identity_registry, write_file

FFMPEG = "/usr/bin/ffmpeg"
GPU = ("NVIDIA", "cuda:0")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(executor, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(ops, "_node_ffmpeg", lambda: FFMPEG)
    yield
    set_registry(None)


@pytest.fixture
def media(tmp_path):
    return write_file(str(tmp_path / "data" / "Show" / "S01E01.mkv"), b"0123456789")


def _node(gpu=None):
    client = LoopbackClient(gpu)
    registry = identity_registry(client)
    set_registry(registry)
    return client, registry


def _on_node():
    return placed_on(Placement(NODE_ID, "NAS"))


class _Config:
    plex_bif_frame_interval = 5
    tonemap_algorithm = "hable"
    thumbnail_quality = 4
    ffmpeg_threads = 2
    log_level = "INFO"
    server_display_name = "Plex A"
    ffmpeg_path = FFMPEG


class TestGenerateImages:
    def test_frames_land_in_the_slot_and_the_tuple_progress_and_failures_match(self, media, tmp_path, monkeypatch):
        client, _ = _node(GPU)
        slot = tmp_path / "slot"
        slot.mkdir()
        (slot / "stale.jpg").write_bytes(b"old")

        def fake(video, out, gpu, device, config, progress, **kwargs):
            assert config.server_display_name == "Plex A" and kwargs["ffmpeg_threads_override"] == 3
            for second in (0, 5):
                write_file(os.path.join(out, f"{second:010d}.jpg"), f"frame {second}".encode())
            progress(40.0, 4.0, 10.0, "1.5x", 4.0, 2, 1.0, 2.0, 10, "00:00:04.00", 9.0, media_file=video)
            generator.record_failure(video, 234, "retry on the CPU", "GPU")
            return (True, 2, True, 3.25, "1.5x", "")

        monkeypatch.setattr(generator, "generate_images", fake)
        progress = []
        with _on_node(), failure_scope("job-x"):
            result = remote_ops.generate_images(
                media,
                str(slot),
                "NVIDIA",
                "cuda:0",
                _Config(),
                lambda *a, **k: progress.append((a, k)),
                ffmpeg_threads_override=3,
            )
            failures = get_failures()

        assert result == (True, 2, True, 3.25, "1.5x", "")
        assert sorted(os.listdir(slot)) == ["0000000000.jpg", "0000000005.jpg"]
        assert (slot / "0000000005.jpg").read_bytes() == b"frame 5"
        assert progress == [
            ((40.0, 4.0, 10.0, "1.5x", 4.0, 2, 1.0, 2.0, 10, "00:00:04.00", 9.0), {"media_file": media})
        ]
        assert failures == [{"file": media, "exit_code": 234, "reason": "retry on the CPU", "worker_type": "GPU"}]
        spec = client.specs[0]
        assert spec["op"] == protocol.OP_PREVIEW_FRAMES and spec["gpu"] == {"type": "NVIDIA", "device": "cuda:0"}
        assert spec["params"] == {
            "frame_interval": 5,
            "tonemap_algorithm": "hable",
            "thumbnail_quality": 4,
            "ffmpeg_threads": 2,
            "log_level": "INFO",
            "server_display_name": "Plex A",
            "ffmpeg_threads_override": 3,
        }

    @pytest.mark.parametrize(
        ("kind", "reported"), [(FALLBACK_STALL, []), (FALLBACK_HWACCEL, [("gpu_error", NODE_ID, "cuda:0")])]
    )
    def test_a_gpu_hand_off_raises_codec_not_supported_for_the_workers_cpu_rerun(
        self, media, tmp_path, monkeypatch, kind, reported
    ):
        _, registry = _node(GPU)

        def fake(*args, **kwargs):
            raise CodecNotSupportedError("GPU processing failed (stall)", kind=kind)

        monkeypatch.setattr(generator, "generate_images", fake)
        with _on_node(), pytest.raises(CodecNotSupportedError) as raised:
            remote_ops.generate_images(media, str(tmp_path), "NVIDIA", "cuda:0", _Config())
        assert raised.value.kind == kind and str(raised.value) == "GPU processing failed (stall)"
        assert registry.reports == reported

    def test_the_apps_own_folder_filling_up_while_the_frames_unpack_keeps_none_of_them(
        self, media, tmp_path, monkeypatch
    ):
        _node()
        slot = tmp_path / "slot"
        slot.mkdir()

        def fake(video, out, *args, **kwargs):
            for second in (0, 5, 10):
                write_file(os.path.join(out, f"{second:010d}.jpg"), b"frame")
            return (True, 3, False, 1.0, "1x", "")

        def unpack_until_full(data, dest):
            write_file(os.path.join(dest, "0000000000.jpg"), b"fr")
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), os.path.join(dest, "0000000005.jpg"))

        monkeypatch.setattr(generator, "generate_images", fake)
        monkeypatch.setattr(executor, "extract_artifact", unpack_until_full)
        with _on_node(), pytest.raises(WorkingFolderError) as raised:
            remote_ops.generate_images(media, str(slot), None, None, _Config())

        assert (raised.value.errno, raised.value.filename) == (errno.ENOSPC, str(slot))
        assert list(slot.iterdir()) == []

    def test_an_apps_folder_its_user_may_not_write_keeps_none_of_the_frames(self, media, tmp_path, monkeypatch):
        _node()
        slot = tmp_path / "slot"
        slot.mkdir()

        def fake(video, out, *args, **kwargs):
            write_file(os.path.join(out, "0000000000.jpg"), b"frame")
            return (True, 1, False, 1.0, "1x", "")

        def unpack_refused(data, dest):
            write_file(os.path.join(dest, "0000000000.jpg"), b"fr")
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), os.path.join(dest, "0000000005.jpg"))

        monkeypatch.setattr(generator, "generate_images", fake)
        monkeypatch.setattr(executor, "extract_artifact", unpack_refused)
        with _on_node(), pytest.raises(WorkingFolderError) as raised:
            remote_ops.generate_images(media, str(slot), None, None, _Config())

        assert (raised.value.errno, raised.value.filename) == (errno.EACCES, str(slot))
        assert list(slot.iterdir()) == []

    def test_a_video_this_app_may_not_read_is_not_the_folders_failure(self, media, tmp_path, monkeypatch):
        def unreadable(*args, **kwargs):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), media)

        slot = tmp_path / "slot"
        slot.mkdir()
        monkeypatch.setattr(remote_ops, "run_on_node", unreadable)
        with pytest.raises(PermissionError) as raised:
            remote_ops.generate_images(media, str(slot), None, None, _Config())

        assert not isinstance(raised.value, WorkingFolderError)

    def test_cancellation_raises_cancellation_error(self, media, tmp_path, monkeypatch):
        _node()

        def fake(*args, **kwargs):
            raise CancellationError("Processing cancelled")

        monkeypatch.setattr(generator, "generate_images", fake)
        with _on_node(), pytest.raises(CancellationError, match="Processing cancelled"):
            remote_ops.generate_images(media, str(tmp_path), None, None, _Config())

    def test_an_unexpected_node_failure_stays_a_runtime_error(self, media, tmp_path, monkeypatch):
        _node()

        def fake(*args, **kwargs):
            raise ValueError("MediaInfo exploded")

        monkeypatch.setattr(generator, "generate_images", fake)
        with _on_node(), pytest.raises(RuntimeError, match="MediaInfo exploded"):
            remote_ops.generate_images(media, str(tmp_path), None, None, _Config())


class TestLoudness:
    def test_run_returns_the_fields_and_reports_progress_in_seconds(self, media, monkeypatch):
        client, _ = _node()

        def fake_run(ffmpeg, path, index, **kwargs):
            kwargs["on_progress"](12.0, 3.0)
            return {"ln:loudness": "-20.00", "ln:peak": "-1.00"}

        monkeypatch.setattr(analyze, "run", fake_run)
        seen = []
        with _on_node():
            fields = remote_ops.loudness_run(
                "/app/ffmpeg", media, 2, duration_ms=24000, codec="aac", on_progress=lambda *a: seen.append(a)
            )
        assert fields == {"ln:loudness": "-20.00", "ln:peak": "-1.00"}
        assert seen == [(12.0, 3.0)]
        assert client.specs[0]["gpu"] is None
        assert client.specs[0]["params"] == {"tracks": [[2, "aac", 24000]], "batch": False, "nice_level": 10}

    def test_the_nice_level_goes_to_the_node_for_one_track_and_for_a_batch(self, media, monkeypatch):
        client, _ = _node()
        monkeypatch.setattr(analyze, "run", lambda *a, **k: {"ln:loudness": "-20.00"})
        monkeypatch.setattr(analyze, "run_many", lambda *a, **k: {1: {"ln:lra": "5.00"}})
        with _on_node():
            remote_ops.loudness_run("/app/ffmpeg", media, 2, duration_ms=24000, codec="aac", nice_level=19)
            remote_ops.loudness_run_many("/app/ffmpeg", media, [(1, "aac", 1000)], nice_level=0)
        assert [spec["params"]["nice_level"] for spec in client.specs] == [19, 0]

    def test_run_many_gives_back_each_streams_fields_or_its_own_error_class(self, media, monkeypatch):
        _node()
        monkeypatch.setattr(
            analyze,
            "run_many",
            lambda *a, **k: {1: {"ln:lra": "5.00"}, 4: analyze.LoudnessNoDecoder("no decoder for 4")},
        )
        with _on_node():
            results = remote_ops.loudness_run_many("/app/ffmpeg", media, [(1, "aac", 1000), (4, "av3a", 1000)])
        assert results[1] == {"ln:lra": "5.00"}
        assert type(results[4]) is analyze.LoudnessNoDecoder and str(results[4]) == "no decoder for 4"

    @pytest.mark.parametrize(
        "cls",
        [
            analyze.LoudnessTimeout,
            analyze.LoudnessNoDecoder,
            analyze.LoudnessFormatChanged,
            analyze.LoudnessTooManyDropped,
            analyze.LoudnessError,
        ],
    )
    def test_a_raised_error_comes_back_as_the_same_class(self, media, monkeypatch, cls):
        _node()

        def fake_run(*args, **kwargs):
            raise cls("the message")

        monkeypatch.setattr(analyze, "run", fake_run)
        with _on_node(), pytest.raises(cls, match="the message") as raised:
            remote_ops.loudness_run("/app/ffmpeg", media, 1, duration_ms=1000)
        assert type(raised.value) is cls

    def test_a_missing_app_file_is_a_loudness_error(self, tmp_path):
        _node()
        with _on_node(), pytest.raises(analyze.LoudnessError, match="Couldn't read"):
            remote_ops.loudness_run("/app/ffmpeg", str(tmp_path / "gone.mkv"), 1, duration_ms=1000)


class TestFingerprint:
    def test_points_and_progress_come_back_as_locally(self, media, monkeypatch):
        client, _ = _node()
        monkeypatch.setattr(fingerprint, "chromaprint_ffmpeg", lambda configured: "chroma")

        def fake(path, duration_ms, **kwargs):
            step = kwargs["progress"](120.0, lambda: 0.0)
            step.update(60.0)
            return np.array([7, 8, 9], dtype="<u4")

        monkeypatch.setattr(fingerprint, "compute_fingerprint", fake)
        made = []

        class Step:
            def __init__(self, total_s):
                self.total_s, self.done = total_s, []

            def update(self, done_s):
                self.done.append(done_s)

        def factory(total_s, clock):
            made.append(Step(total_s))
            return made[-1]

        with _on_node():
            points = remote_ops.compute_fingerprint(media, 343000, ffmpeg="/app/ffmpeg", retime=0.96, progress=factory)

        assert points.dtype == np.dtype("uint32") and points.tolist() == [7, 8, 9]
        assert [(s.total_s, s.done) for s in made] == [(120.0, [60.0])]
        assert client.specs[0]["params"] == {"duration_ms": 343000, "retime": 0.96, "progress": True}
        assert client.specs[0]["gpu"] is None

    @pytest.mark.parametrize("cls", [fingerprint.FingerprintError, fingerprint.FingerprintStalledError])
    def test_errors_come_back_as_the_same_class(self, media, monkeypatch, cls):
        _node()
        monkeypatch.setattr(fingerprint, "chromaprint_ffmpeg", lambda configured: "chroma")

        def fake(*args, **kwargs):
            raise cls("Fingerprinting S01E01.mkv failed")

        monkeypatch.setattr(fingerprint, "compute_fingerprint", fake)
        with _on_node(), pytest.raises(cls) as raised:
            remote_ops.compute_fingerprint(media, 1000, ffmpeg="/app/ffmpeg")
        assert type(raised.value) is cls


class TestEndPicture:
    def test_frames_and_fallbacks_come_back_as_locally(self, media, monkeypatch):
        _node(GPU)
        frame = np.linspace(0, 255, 64 * 36, dtype=np.float32).reshape(36, 64)

        def fake(path, start_s, length_s, **kwargs):
            kwargs["fallback_callback"]("CPU this time")
            return [(1.0, frame), (1.5, frame * 0.5)]

        monkeypatch.setattr(end_picture, "decode_frames", fake)
        fell_back = []
        with _on_node():
            decoded = remote_ops.decode_frames(
                media,
                0.5,
                2.0,
                ffmpeg="/app/ffmpeg",
                gpu="NVIDIA",
                gpu_device_path="cuda:0",
                container_start_s=0.0,
                download_format=None,
                fallback_callback=fell_back.append,
            )
        assert [t for t, _ in decoded] == [1.0, 1.5]
        assert decoded[1][1].dtype == np.float32 and np.array_equal(decoded[1][1], frame * 0.5)
        assert fell_back == ["CPU this time"]

    @pytest.mark.parametrize(
        ("error", "reported"),
        [
            (end_picture.GpuAttemptFailedError("GPU timed out"), [("gpu_error", NODE_ID, "cuda:0")]),
            (frames.DecodeCancelledError("cancelled"), []),
            (frames.NoDecoderError("no decoder"), []),
        ],
    )
    def test_errors_come_back_as_the_same_class(self, media, monkeypatch, error, reported):
        _, registry = _node(GPU)

        def fake(*args, **kwargs):
            raise error

        monkeypatch.setattr(end_picture, "decode_frames", fake)
        with _on_node(), pytest.raises(type(error)) as raised:
            remote_ops.decode_frames(
                media,
                0.0,
                1.0,
                ffmpeg="f",
                gpu="NVIDIA",
                gpu_device_path="cuda:0",
                container_start_s=0.0,
                download_format=None,
            )
        assert type(raised.value) is type(error)
        assert registry.reports == reported


class TestFindCredits:
    def test_result_phase_and_cpu_notes_come_back_as_locally(self, media, monkeypatch):
        client, _ = _node(GPU)
        result = detector.CreditsTextResult(
            start_s=100.0,
            end_s=130.5,
            key_rows=((90.0, 0, 1.0, ()), (100.0, 3, 40.0, ((1, 2, 30, 40), (5, 6, 7, 8)))),
            fine_rows=((99.0, 1, 30.0),),
            end_rows=(),
            overlays=(),
            scale=1,
        )

        class Pool:
            def detect_boxes(self, planes, **kwargs):
                kwargs["on_cpu"]("read on the CPU")
                return []

        def fake(path, **kwargs):
            kwargs["phase"]("Reading the credits…")
            kwargs["detect_boxes"](None)
            return result

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", Pool)
        monkeypatch.setattr(detector, "find_credits", fake)
        phases, notes = [], []
        with _on_node():
            got = remote_ops.find_credits(
                media,
                duration_ms=200_000,
                is_episode=False,
                ffmpeg="/app/ffmpeg",
                gpu="NVIDIA",
                gpu_device_path="cuda:0",
                phase=phases.append,
                gpu_worker=True,
                on_cpu=notes.append,
            )
        assert got == result
        assert phases == ["Reading the credits…"] and notes == ["read on the CPU"]
        assert client.specs[0]["params"]["gpu_worker"] is True

    def test_the_nodes_step_text_and_cpu_notes_stay_one_line_each(self, media, monkeypatch):
        _node(GPU)
        result = detector.CreditsTextResult(
            start_s=None, end_s=None, key_rows=(), fine_rows=(), end_rows=(), overlays=(), scale=1
        )

        class Pool:
            def detect_boxes(self, planes, **kwargs):
                kwargs["on_cpu"]("read on the CPU\n12:00 | ERROR | forged")
                return []

        def fake(path, **kwargs):
            kwargs["phase"]("Reading the credits\u2028forged")
            kwargs["detect_boxes"](None)
            return result

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", Pool)
        monkeypatch.setattr(detector, "find_credits", fake)
        phases, notes = [], []
        with _on_node():
            remote_ops.find_credits(
                media,
                duration_ms=200_000,
                is_episode=False,
                ffmpeg="/app/ffmpeg",
                gpu="NVIDIA",
                gpu_device_path="cuda:0",
                phase=phases.append,
                gpu_worker=True,
                on_cpu=notes.append,
            )
        assert phases == ["Reading the credits\\u2028forged"]
        assert notes == ["read on the CPU\\n12:00 | ERROR | forged"]

    def test_ffmpegs_lines_from_a_node_are_as_many_as_the_job_log_shows_one_line_each(self, media, monkeypatch):
        _node(GPU)
        said = tuple(f"Error {n}\n12:00 | ERROR | forged" for n in range(50))

        def fake(path, **kwargs):
            raise frames.GpuDecodeError("cuda failed", stderr_tail=said, fallback_kind="hwaccel")

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", lambda: None)
        monkeypatch.setattr(detector, "find_credits", fake)
        with _on_node(), pytest.raises(frames.GpuDecodeError) as raised:
            remote_ops.find_credits(
                media, duration_ms=1000, is_episode=True, ffmpeg="f", gpu="NVIDIA", gpu_device_path="cuda:0"
            )
        assert raised.value.stderr_tail == tuple(
            f"Error {n}\\n12:00 | ERROR | forged" for n in range(50 - frames.STDERR_LINES, 50)
        )

    @pytest.mark.parametrize(
        ("fallback_kind", "reports"),
        [
            ("hwaccel", [("gpu_error", NODE_ID, "cuda:0")]),
            # A codec the GPU can't decode: the CPU rerun reads the file, and nobody re-checks a GPU that is fine.
            ("codec", []),
        ],
    )
    def test_a_gpu_decode_failure_keeps_ffmpegs_lines_and_only_a_gpu_fault_is_reported(
        self, media, monkeypatch, fallback_kind, reports
    ):
        _, registry = _node(GPU)

        def fake(path, **kwargs):
            raise frames.GpuDecodeError("cuda failed", stderr_tail=("Error 1", "Error 2"), fallback_kind=fallback_kind)

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", lambda: None)
        monkeypatch.setattr(detector, "find_credits", fake)
        with _on_node(), pytest.raises(frames.GpuDecodeError) as raised:
            remote_ops.find_credits(
                media, duration_ms=1000, is_episode=True, ffmpeg="f", gpu="NVIDIA", gpu_device_path="cuda:0"
            )
        assert type(raised.value) is frames.GpuDecodeError
        assert raised.value.stderr_tail == ("Error 1", "Error 2") and raised.value.fallback_kind == fallback_kind
        assert frames.gpu_failure_lines(raised.value) == ("Error 1", "Error 2")
        assert registry.reports == reports

    def test_read_nothing_on_the_gpu_is_not_reported_as_a_gpu_failure(self, media, monkeypatch):
        _, registry = _node(GPU)

        def fake(path, **kwargs):
            raise frames.GpuReadNothingError()

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", lambda: None)
        monkeypatch.setattr(detector, "find_credits", fake)
        with _on_node(), pytest.raises(frames.GpuReadNothingError):
            remote_ops.find_credits(
                media, duration_ms=1000, is_episode=True, ffmpeg="f", gpu="NVIDIA", gpu_device_path="cuda:0"
            )
        assert registry.reports == []

    def test_an_unexpected_node_failure_is_not_its_own_cause(self, media, monkeypatch):
        # A self-caused error sent gpu_failure_lines round its cause chain forever, holding the GIL (sflix, 2026-10-28).
        _node(GPU)

        def fake(path, **kwargs):
            raise ValueError("OCR model exploded")

        monkeypatch.setattr(textdet_helper, "get_textdet_pool", lambda: None)
        monkeypatch.setattr(detector, "find_credits", fake)
        with _on_node(), pytest.raises(RuntimeError, match="OCR model exploded") as raised:
            remote_ops.find_credits(
                media, duration_ms=1000, is_episode=True, ffmpeg="f", gpu="NVIDIA", gpu_device_path="cuda:0"
            )
        assert raised.value.__cause__ is not raised.value
        assert frames.gpu_failure_lines(raised.value) == ()


# --- the real functions both ways on a real clip -------------------------------------------------------------------

_HAS_FFMPEG = os.path.isfile(FFMPEG) and shutil.which("ffprobe") is not None


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    path = tmp_path_factory.mktemp("clip") / "Clip (2026).mkv"
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=12:size=320x240:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=12", "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )  # fmt: skip
    return str(path)


@pytest.mark.skipif(not _HAS_FFMPEG, reason="needs ffmpeg and ffprobe")
class TestRealMedia:
    def test_preview_frames_on_the_node_are_byte_identical_to_local_ones(self, clip, tmp_path):
        _node()
        local_dir, remote_dir = tmp_path / "local", tmp_path / "remote"
        local_dir.mkdir()
        remote_dir.mkdir()
        config = ops.PreviewConfig(5, "hable", 4, 2, "INFO", None, FFMPEG)

        with failure_scope("job-real"):
            local = generator.generate_images(clip, str(local_dir), None, None, config)
            with _on_node():
                remote = remote_ops.generate_images(clip, str(remote_dir), None, None, config)

        assert local[0] is True and local[1] > 0
        assert (remote[0], remote[1], remote[2], remote[5]) == (local[0], local[1], local[2], local[5])
        names = sorted(os.listdir(local_dir))
        assert names == sorted(os.listdir(remote_dir))
        assert all((local_dir / n).read_bytes() == (remote_dir / n).read_bytes() for n in names)

    def test_a_node_temp_folder_removed_under_it_does_not_fail_the_file(self, clip, tmp_path, monkeypatch):
        monkeypatch.setenv("MODE", "node")
        monkeypatch.setenv("NODE_TEMP_DIR", str(tmp_path / "gone"))
        out = tmp_path / "out"
        out.mkdir()
        config = ops.PreviewConfig(5, "hable", 4, 2, "INFO", None, FFMPEG)

        with failure_scope("job-gone"):
            result = generator.generate_images(clip, str(out), None, None, config)

        assert result[0] is True and result[1] > 0

    def test_fingerprint_on_the_node_equals_the_local_one(self, clip):
        ffmpeg = fingerprint.chromaprint_ffmpeg(FFMPEG)
        if ffmpeg is None:
            pytest.skip("ffmpeg has no chromaprint muxer")
        _node()
        local = fingerprint.compute_fingerprint(clip, 12000, ffmpeg=ffmpeg)
        with _on_node():
            remote = remote_ops.compute_fingerprint(clip, 12000, ffmpeg=ffmpeg)
        assert local.size > 0
        assert remote.dtype == local.dtype and np.array_equal(remote, local)

    def test_loudness_on_the_node_equals_the_local_one(self, clip, monkeypatch):
        monkeypatch.setattr(analyze, "resolve_ffmpeg", lambda fallback: fallback)
        _node()
        local = analyze.run(FFMPEG, clip, 1, duration_ms=12000, codec="aac")
        with _on_node():
            remote = remote_ops.loudness_run(FFMPEG, clip, 1, duration_ms=12000, codec="aac")
        assert remote == local
