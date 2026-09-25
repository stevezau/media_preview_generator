"""Season audio's end-picture check (spec §5.3): frame comparison, the verdict, which partners, and the decode it reuses
from credit text (GPU then CPU, cancel, stalls), with the audio stream's start offset applied."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from media_preview_generator.markers.audio import end_picture as ep
from media_preview_generator.markers.audio.matcher import Hit
from media_preview_generator.markers.credits import frames
from media_preview_generator.markers.probe import ProbeError, ProbeStalledError, ProbeTimeoutError, StreamStarts


def _textured(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).uniform(0, 255, size=(ep.FRAME_H, ep.FRAME_W)).astype(np.float32)


def _correlated(x: np.ndarray, r: float, seed: int = 99) -> np.ndarray:
    """A frame whose correlation with ``x`` is ``r`` (up to sampling noise, removed by projecting it out)."""
    zx = (x - x.mean()).ravel()
    zx /= np.linalg.norm(zx)
    noise = np.random.default_rng(seed).normal(size=zx.shape)
    noise -= (noise @ zx) * zx
    noise /= np.linalg.norm(noise)
    return (128 + 60 * (r * zx + np.sqrt(1 - r * r) * noise) * np.sqrt(zx.size)).reshape(x.shape).astype(np.float32)


class TestFramesAlike:
    def test_the_same_picture_matches_and_another_does_not(self):
        x = _textured(1)
        assert ep.frames_alike(x, x + 3.0) is True
        assert ep.frames_alike(x, _textured(2)) is False

    @pytest.mark.parametrize(("r", "alike"), [(0.65, True), (0.55, False)])
    def test_pictures_match_above_a_correlation_of_0_6(self, r, alike):
        x = _textured(3)
        y = _correlated(x, r)
        assert np.corrcoef(x.ravel(), y.ravel())[0, 1] == pytest.approx(r, abs=1e-3)
        assert ep.frames_alike(x, y) is alike

    @pytest.mark.parametrize(("difference", "alike"), [(11.9, True), (12.0, False)])
    def test_two_flat_frames_match_by_brightness(self, difference, alike):
        dark = np.full((ep.FRAME_H, ep.FRAME_W), 16.0, dtype=np.float32)
        assert ep.frames_alike(dark, dark + difference) is alike

    def test_a_flat_frame_never_matches_a_textured_one(self):
        flat = np.full((ep.FRAME_H, ep.FRAME_W), 128.0, dtype=np.float32)
        assert ep.frames_alike(flat, _textured(4)) is False
        assert ep.frames_alike(_textured(4), flat) is False


class TestVerdict:
    @pytest.mark.parametrize(
        ("shares", "passes"),
        [
            ([1.0, 0.5], True),  # median 0.75
            ([0.8, 0.5], False),  # median 0.65
            ([0.74], False),
            ([None, 0.9], True),  # a pair with no frames doesn't count
            ([None, 0.5], False),
            ([None, None], True),  # nothing could be compared: not checked, as before the guard
            ([], True),
        ],
    )
    def test_the_median_share_of_the_pairs_with_frames_must_be_at_least_75_percent(self, shares, passes):
        assert ep.passes(shares) is passes

    def test_the_last_3_s_are_compared_every_half_second(self):
        assert ep.sample_times(0.12, 36.29) == pytest.approx([33.29, 33.79, 34.29, 34.79, 35.29, 35.79])
        assert ep.sample_times(10.0, 11.2) == pytest.approx([10.0, 10.5, 11.0])  # all of a shorter one

    @pytest.mark.parametrize(("start", "early"), [(0.0, True), (30.0, True), (30.1, False), (95.0, False)])
    def test_only_a_candidate_starting_in_the_first_30_s_is_checked(self, start, early):
        assert ep.is_early(start) is early

    def test_the_two_partners_with_the_longest_hits_are_compared(self):
        members = [
            Hit(0.0, 20.0, "b", 1.0),
            Hit(0.5, 30.0, "c", 2.0),
            Hit(1.0, 26.0, "e", 5.0),  # as long as b's longest: b comes first in the cluster, so b is compared
            Hit(0.0, 25.0, "b", 3.0),  # b's longest
            Hit(0.1, 24.8, "d", 4.0),
        ]
        assert ep.partners(members) == [members[1], members[3]]
        assert ep.partners(members[2:]) == [members[2], members[3]]


def _frames_at(times, picture_at):
    return [(t, picture_at(t)) for t in times]


class TestShareAlike:
    def test_each_instant_pairs_the_nearest_frames_at_both_files_shifts(self):
        times = [10.0, 10.5, 11.0]
        target = _frames_at([10.5, 11.0, 11.5], lambda t: _textured(int(t * 10)))
        # The partner shows the same pictures 20.0 s later: aligned only with the shifts applied.
        partner = _frames_at([30.5, 31.0, 31.5], lambda t: _textured(int((t - 20.0) * 10)))
        assert ep.share_alike(times, target, 0.5, partner, 20.5) == 1.0
        assert ep.share_alike(times, target, 0.5, partner, 21.0) == 0.0

    def test_the_same_end_card_after_a_re_cut_lead_in_is_the_same_end_picture(self):
        # Tomb Raider King S01E12: the opening's last 1.5 s before its end card are re-cut (and ~0.4 s out of sync)
        # against the earlier episodes'; the card it ends on is the same.
        times = [86.0, 86.5, 87.0, 87.5, 88.0, 88.5]
        card = _textured(50)
        target = _frames_at(times, lambda t: card if t >= 87.5 else _textured(int(t * 10)))
        partner = _frames_at(times, lambda t: card if t >= 87.5 else _textured(int(t * 10) + 1))
        assert ep.share_alike(times, target, 0.0, partner, 0.0) == 1.0

    @pytest.mark.parametrize(
        ("same_from", "flat", "share"),
        [
            (88.0, False, 2 / 6),  # the last second only: no card held for 1.5 s
            (87.5, True, 3 / 6),  # a shared fade to black is no card
        ],
        ids=["too-short", "black"],
    )
    def test_no_end_card_is_the_share_of_matching_instants(self, same_from, flat, share):
        times = [86.0, 86.5, 87.0, 87.5, 88.0, 88.5]
        card = np.full((ep.FRAME_H, ep.FRAME_W), 8.0, dtype=np.float32) if flat else _textured(51)
        target = _frames_at(times, lambda t: card if t >= same_from else _textured(int(t * 10)))
        partner = _frames_at(times, lambda t: card if t >= same_from else _textured(int(t * 10) + 1))
        assert ep.share_alike(times, target, 0.0, partner, 0.0) == pytest.approx(share)

    def test_black_with_a_channel_logo_in_the_corner_is_no_end_card(self):
        # A TV recording's cold-open music bed ending on a black act break: the channel's logo alone isn't a card.
        times = [86.0, 86.5, 87.0, 87.5, 88.0, 88.5]
        black = np.full((ep.FRAME_H, ep.FRAME_W), 8.0, dtype=np.float32)
        black[1:4, 57:63] = _textured(53)[1:4, 57:63]  # the logo, in the top right corner
        assert float(black.std()) >= ep.FLAT_STD  # not flat as a whole frame
        target = _frames_at(times, lambda t: black if t >= 87.5 else _textured(int(t * 10)))
        partner = _frames_at(times, lambda t: black if t >= 87.5 else _textured(int(t * 10) + 1))
        assert ep.share_alike(times, target, 0.0, partner, 0.0) == pytest.approx(3 / 6)

    def test_an_end_card_needs_a_frame_at_each_of_its_instants(self):
        times = [86.0, 86.5, 87.0, 87.5, 88.0, 88.5]
        card = _textured(52)
        target = _frames_at(times, lambda t: card if t >= 87.5 else _textured(int(t * 10)))
        partner = [(t, card if t >= 87.5 else _textured(int(t * 10) + 1)) for t in times if t != 88.0]
        assert ep.share_alike(times, target, 0.0, partner, 0.0) == pytest.approx(2 / 5)

    def test_an_instant_with_no_frame_within_a_quarter_second_is_left_out(self):
        picture = _textured(5)
        target = [(10.0, picture), (11.0, picture)]
        partner = [(10.0, picture), (10.6, _textured(6))]
        # 10.0 pairs alike; 10.5 has no target frame (0.5 s away); 11.0's partner frame is 0.4 s away.
        assert ep.share_alike([10.0, 10.5, 11.0], target, 0.0, partner, 0.0) == 1.0
        assert ep.share_alike([10.5], target, 0.0, partner, 0.0) is None


class _Decoder:
    """Stands in for the decode: every file shows picture ``seed(t)`` at file time t."""

    def __init__(self, seed_of=lambda path, t: int(round(t * 2))):
        self.seed_of = seed_of
        self.calls: list[tuple] = []
        self.download_formats: dict[str, str | None] = {}

    def __call__(self, path, start_s, length_s, *, ffmpeg, gpu, gpu_device_path, container_start_s, cancel_check,
                 download_format):  # fmt: skip
        self.calls.append((path, round(start_s, 3), round(length_s, 3), gpu, gpu_device_path, container_start_s))
        self.download_formats[path] = download_format
        times = np.arange(np.ceil(start_s * 2) / 2, start_s + length_s, 0.5)
        return [(float(t), _textured(self.seed_of(path, float(t)))) for t in times]


class TestReader:
    def _reader(self, starts, decoder, **kwargs):
        reader = ep.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0", **kwargs)
        return reader, patch.multiple(ep, stream_starts=starts, decode_frames=decoder)

    def test_the_audio_offsets_move_each_files_window_and_the_pairs_line_up(self):
        # "a" is an HBO Max release (audio 0.976 s in), "b" isn't: their title cards line up in file time only once
        # each fingerprint time is moved to its file's audio start.
        offsets = {"a": 0.976, "b": 0.0}
        decoder = _Decoder(lambda path, t: int(round((t - offsets[path] + (20.0 if path == "a" else 0.0)) * 2)))
        seen = []

        def starts(path, *, ffprobe, timeout_s):
            seen.append((path, ffprobe, timeout_s))
            return StreamStarts(0.0, offsets[path])

        reader, patched = self._reader(starts, decoder)
        with patched, patch.object(ep, "ffprobe_path_for", return_value="/usr/bin/ffprobe"):
            share = reader.share("a", "b", 0.0, 36.0, 20.0)
        assert share == 1.0
        assert seen == [("a", "/usr/bin/ffprobe", ep.PROBE_TIMEOUT_S), ("b", "/usr/bin/ffprobe", ep.PROBE_TIMEOUT_S)]
        # a: instants 33.0-35.5 at +0.976; b: at +20.0, each padded by half a second.
        assert decoder.calls == [
            ("a", 33.476, 3.5, "NVIDIA", "cuda:0", 0.0),
            ("b", 52.5, 3.5, "NVIDIA", "cuda:0", 0.0),
        ]

    def test_without_the_offsets_the_same_files_would_not_line_up(self):
        offsets = {"a": 0.976, "b": 0.0}
        decoder = _Decoder(lambda path, t: int(round((t - offsets[path] + (20.0 if path == "a" else 0.0)) * 2)))
        reader, patched = self._reader(lambda path, **_kw: StreamStarts(0.0, 0.0), decoder)
        with patched:
            assert reader.share("a", "b", 0.0, 36.0, 20.0) == 0.0

    @pytest.mark.parametrize(
        ("pix_fmt", "download_format"),
        [("yuv420p", "nv12"), ("yuv420p10le", "p010le"), ("yuv422p10le", None), ("yuvj420p", None), (None, None)],
        ids=["8-bit", "10-bit", "4:2:2", "mjpeg-full-range", "unnamed"],
    )
    def test_each_file_is_decoded_as_surfaces_of_its_own_pixel_format(self, pix_fmt, download_format):
        # The credit text decode's own rule (frames.DOWNLOAD_FORMATS): a format the GPU's surfaces aren't known in is
        # left to ffmpeg to download, since a wrong guess fails the GPU decode.
        formats = {"a": "yuv420p", "b": pix_fmt}
        decoder = _Decoder()
        reader, patched = self._reader(lambda path, **_kw: StreamStarts(0.0, None, True, formats[path]), decoder)
        with patched:
            reader.share("a", "b", 0.0, 30.0, 0.0)
        assert decoder.download_formats == {"a": "nv12", "b": download_format}

    def test_each_file_is_probed_once_and_each_window_decoded_once(self):
        decoder = _Decoder()
        probed = []
        reader, patched = self._reader(lambda path, **_kw: probed.append(path) or StreamStarts(0.0, None), decoder)
        with patched:
            reader.share("a", "b", 0.0, 30.0, 0.0)
            reader.share("a", "c", 0.0, 30.0, 0.0)
        assert probed == ["a", "b", "c"]
        assert [call[0] for call in decoder.calls] == ["a", "b", "c"]

    def test_stalled_ffprobes_give_no_verdict_this_time_and_blame_no_file(self):
        def stalled(path, **_kw):
            raise ProbeStalledError("2 earlier ffprobes are still stuck")

        reader, patched = self._reader(stalled, _Decoder())
        with patched, pytest.raises(ep.CheckUnavailableError):
            reader.share("a", "b", 0.0, 30.0, 0.0)

    @pytest.mark.parametrize(
        "error", [ProbeError("ffprobe exited 1 for b"), ProbeTimeoutError("timed out")], ids=["error", "timed-out"]
    )
    def test_a_file_ffprobe_cant_read_is_a_failed_read_never_no_frames(self, error):
        def starts(path, **_kw):
            if path == "b":
                raise error
            return StreamStarts(0.0, None)

        decoder = _Decoder()
        reader, patched = self._reader(starts, decoder)
        with patched, pytest.raises(ep.ReadFailedError) as failed:
            reader.share("a", "b", 0.0, 30.0, 0.0)
        assert failed.value.path == "b" and decoder.calls == []

    @pytest.mark.parametrize(
        "error",
        [
            frames.DecodeTimeoutError("decoding b timed out after 120 s"),
            frames.FrameDecodeError("ffmpeg exited 1 decoding b on the CPU"),  # an NFS read error, say
        ],
        ids=["timed-out", "non-zero-exit"],
    )
    def test_a_decode_that_fails_is_a_failed_read_of_that_file_and_it_isnt_read_again_this_run(self, error):
        calls = []

        def decoder(path, *_args, **_kwargs):
            calls.append(path)
            if path == "b":
                raise error
            return []

        reader, patched = self._reader(lambda path, **_kw: StreamStarts(0.0, None), decoder)
        with patched:
            for _ in range(2):
                with pytest.raises(ep.ReadFailedError) as failed:
                    reader.share("a", "b", 0.0, 30.0, 0.0)
                assert failed.value.path == "b"
        assert calls == ["a", "b"]

    def test_a_cancelled_decode_gives_no_verdict_and_blames_no_file(self):
        def decoder(*_args, **_kwargs):
            raise frames.DecodeCancelledError("cancelled while decoding a")

        reader, patched = self._reader(lambda path, **_kw: StreamStarts(0.0, None), decoder)
        with patched, pytest.raises(ep.CheckUnavailableError):
            reader.share("a", "b", 0.0, 30.0, 0.0)

    @pytest.mark.parametrize(
        ("has_video", "frames_at", "share"),
        [
            ((True, False), None, None),  # the partner has no picture: certainly nothing to compare
            ((True, True), [], None),  # ffmpeg exited cleanly with no frames there
            ((True, True), "all", 1.0),
        ],
        ids=["no-video-stream", "clean-exit-no-frames", "frames"],
    )
    def test_certainly_no_frames_is_no_share(self, has_video, frames_at, share):
        videos = dict(zip("ab", has_video, strict=True))
        decoder = _Decoder() if frames_at == "all" else (lambda *_a, **_k: frames_at)
        reader, patched = self._reader(lambda path, **_kw: StreamStarts(0.0, None, videos[path]), decoder)
        with patched:
            assert reader.share("a", "b", 0.0, 30.0, 0.0) == share

    def test_a_cancelled_job_probes_nothing(self):
        probed = []
        reader = ep.Reader(ffmpeg="ffmpeg", cancel_check=lambda: True)
        with (
            patch.object(ep, "stream_starts", side_effect=lambda path, **_kw: probed.append(path)),
            pytest.raises(ep.CheckUnavailableError),
        ):
            reader.share("a", "b", 0.0, 30.0, 0.0)
        assert probed == []


def _planes(n: int, seed: int = 0) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, size=(n, frames.FRAME_H, frames.FRAME_W), dtype=np.uint8)


class TestDecodeFrames:
    def _run_decode(self, planes, pts, fail_on_gpu=False):
        calls = []

        def run_decode(command, *, hw_active, detect_boxes, pts_offset_s, cancel_check, timeout_s, name):
            calls.append({"command": command, "hw_active": hw_active, "pts_offset_s": pts_offset_s,
                          "timeout_s": timeout_s, "cancel_check": cancel_check})  # fmt: skip
            if fail_on_gpu and hw_active:
                raise frames.GpuDecodeError("the GPU decoded no frames")
            boxes = detect_boxes(planes)
            assert boxes == [()] * len(planes)
            return [(t, 0, 0.0, ()) for t in pts]

        return calls, run_decode

    def test_frames_come_back_at_their_file_times_as_64x36_block_means(self):
        planes = _planes(2)
        calls, run_decode = self._run_decode(planes, [33.5, 34.0])
        cancel = lambda: False  # noqa: E731
        with patch.object(frames, "run_decode", side_effect=run_decode):
            got = ep.decode_frames("/m/a.mkv", 33.476, 3.5, ffmpeg="ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0",
                                   container_start_s=1.5, cancel_check=cancel, download_format="nv12")  # fmt: skip
        assert [t for t, _ in got] == [33.5, 34.0]
        expected = planes[0].astype(np.float32).reshape(36, 5, 64, 5).mean(axis=(1, 3))
        assert got[0][1].shape == (36, 64) and np.allclose(got[0][1], expected)
        (call,) = calls
        assert (call["hw_active"], call["pts_offset_s"], call["timeout_s"], call["cancel_check"]) == (
            True, 1.5, ep.DECODE_TIMEOUT_S, cancel,
        )  # fmt: skip
        command = call["command"]
        assert command[command.index("-ss") + 1] == "33.476" and command[command.index("-t") + 1] == "3.500"
        assert "-skip_frame" not in command and "-copyts" in command

    CUDA = ["-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"]
    VAAPI = ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128", "-hwaccel_output_format", "vaapi",
             "-extra_hw_frames", "8"]  # fmt: skip
    NEIGHBOR = "scale=320:180:flags=neighbor,format=nv12"

    @pytest.mark.parametrize(
        ("gpu", "device", "download_format", "hw_args", "video_filter"),
        [
            ("NVIDIA", "cuda:0", "nv12", CUDA, f"fps=2,hwdownload,format=nv12,{NEIGHBOR}"),
            ("NVIDIA", "cuda:0", "p010le", CUDA, f"fps=2,hwdownload,format=p010le,{NEIGHBOR}"),
            ("INTEL", "/dev/dri/renderD128", "nv12", VAAPI, f"fps=2,hwdownload,format=nv12,{NEIGHBOR}"),
            ("AMD", "/dev/dri/renderD128", "p010le", VAAPI, f"fps=2,hwdownload,format=p010le,{NEIGHBOR}"),
            # A pixel format the surfaces aren't known in: ffmpeg downloads each frame itself, the same scaler follows.
            ("NVIDIA", "cuda:0", None, ["-hwaccel", "cuda", "-hwaccel_device", "0"], f"fps=2,{NEIGHBOR}"),
            (None, None, "nv12", [], f"fps=2,{NEIGHBOR}"),
        ],
        ids=["cuda", "cuda-10-bit", "vaapi-intel", "vaapi-amd-10-bit", "cuda-unknown-format", "cpu"],
    )  # fmt: skip
    def test_every_vendor_ends_in_the_one_neighbor_scaler(self, gpu, device, download_format, hw_args, video_filter):
        # The whole decoded frame, downloaded in the stream's own format, through the one software scaler the credit
        # text decode uses (spec §5.3): the same frames on every vendor, so a partner decoded by another worker's GPU
        # can't change the answer. scale_cuda, scale_vaapi and swscale's bicubic each blurred differently.
        calls, run_decode = self._run_decode(_planes(1), [34.0])
        with patch.object(frames, "run_decode", side_effect=run_decode):
            ep.decode_frames("/m/a.mkv", 33.476, 3.5, ffmpeg="ffmpeg", gpu=gpu, gpu_device_path=device,
                             container_start_s=0.0, download_format=download_format)  # fmt: skip
        assert calls[0]["command"] == [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "info", "-threads", "2", *hw_args, "-ss", "33.476",
            "-t", "3.500", "-copyts", "-i", "/m/a.mkv", "-an", "-sn", "-dn", "-fps_mode", "passthrough",
            "-vf", f"{video_filter},showinfo", "-f", "rawvideo", "-",
        ]  # fmt: skip
        assert calls[0]["hw_active"] is (gpu is not None)

    def test_a_failed_gpu_decode_is_run_again_on_the_cpu(self):
        calls, run_decode = self._run_decode(_planes(1), [34.0], fail_on_gpu=True)
        with patch.object(frames, "run_decode", side_effect=run_decode):
            got = ep.decode_frames("/m/a.mkv", 33.5, 1.0, ffmpeg="ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0",
                                   container_start_s=0.0, download_format="nv12")  # fmt: skip
        assert [call["hw_active"] for call in calls] == [True, False]
        assert "-hwaccel" not in calls[1]["command"] and len(got) == 1
        cpu = calls[1]["command"]
        assert cpu[cpu.index("-vf") + 1] == f"fps=2,{self.NEIGHBOR},showinfo"

    def test_a_frame_without_a_timestamp_fails_the_decode(self):
        _calls, run_decode = self._run_decode(_planes(3), [33.5, 34.0])  # one frame's timestamp was dropped
        with (
            patch.object(frames, "run_decode", side_effect=run_decode),
            pytest.raises(frames.FrameDecodeError, match="no timestamp"),
        ):
            ep.decode_frames("/m/a.mkv", 33.5, 1.5, ffmpeg="ffmpeg", gpu=None, gpu_device_path=None,
                             container_start_s=0.0, download_format=None)  # fmt: skip

    def test_the_window_is_padded_half_a_second_and_never_before_the_file(self):
        assert ep.window([33.0, 35.5], 0.976) == pytest.approx((33.476, 3.5))
        assert ep.window([0.2, 1.2], 0.0) == pytest.approx((0.0, 1.7))
