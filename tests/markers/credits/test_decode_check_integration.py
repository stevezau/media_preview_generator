"""Real ffmpeg on the packaged reference clips: the CPU's side, and the check on this host's GPUs."""

from __future__ import annotations

import hashlib
import re
import shutil
import time

import pytest

from media_preview_generator.markers.credits import decode_check, frames
from tests.markers.credits.test_frames_integration import vaapi_node

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ffmpeg():
    found = shutil.which("ffmpeg")
    if found is None:
        pytest.skip("no ffmpeg")
    return found


@pytest.mark.parametrize("clip", decode_check.REFERENCE_CLIPS, ids=lambda clip: clip.name)
def test_each_clips_download_format_is_the_one_its_probe_gives(ffmpeg, clip):
    # The check hands decode_rows the format a file of the clip's pixel format is decoded with.
    assert frames.keyframe_thinning(clip.path, ffmpeg).download_format == clip.download_format


@pytest.mark.parametrize("scale", decode_check.SCALES)
@pytest.mark.parametrize("clip", decode_check.REFERENCE_CLIPS, ids=lambda clip: clip.name)
def test_the_cpu_decodes_every_frame_of_each_clip_and_no_two_alike(ffmpeg, clip, scale):
    found = decode_check.decode_clip(ffmpeg, clip, scale=scale, gpu=None, gpu_device_path=None, cancel_check=None,
                                     timeout_s=30.0)  # fmt: skip
    assert [pts for pts, _ in found] == [float(i) for i in range(9)]
    assert len({digest for _, digest in found}) == 9


def _check(gpu, device, ffmpeg, loguru_caplog, decode=decode_check.decode_clip):
    started = time.monotonic()
    answer = decode_check.DecodeChecks(decode=decode).check_device(gpu, device, ffmpeg=ffmpeg)
    elapsed_ms = (time.monotonic() - started) * 1000
    lines = [m for m in loguru_caplog.messages if m.startswith("Credits decoding on")]
    return answer, lines, elapsed_ms


@pytest.mark.gpu
def test_this_hosts_nvidia_gpu_passes_the_check(ffmpeg, loguru_caplog):
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")
    answer, lines, elapsed_ms = _check("NVIDIA", "cuda:0", ffmpeg, loguru_caplog)
    assert answer is True, lines
    (line,) = lines
    assert re.fullmatch(r"Credits decoding on cuda:0 matches the reference decode \(.+; checked in \d+ ms\)", line)
    print(f"\n{line} [wall {elapsed_ms:.0f} ms]")


@pytest.mark.gpu
def test_this_hosts_vaapi_gpu_passes_the_check(ffmpeg, loguru_caplog):
    node = vaapi_node()
    if node is None:
        pytest.skip("no Intel or AMD render node here (storage's only GPU is NVIDIA); runs in the lab image")
    device, vendor = node
    answer, lines, _ = _check(vendor, device, ffmpeg, loguru_caplog)
    assert answer is True, lines


@pytest.mark.gpu
def test_a_gpu_path_whose_pixels_differ_is_caught_on_real_frames(ffmpeg, loguru_caplog):
    # The check has teeth on real hardware: the same GPU decode, scaled by bilinear instead of the nearest pixel, gives
    # the same timestamps but other pixels, and the warning says so.
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU")

    def blurred(ffmpeg, clip, *, scale, gpu, gpu_device_path, cancel_check, timeout_s):
        if gpu is None:
            return decode_check.decode_clip(ffmpeg, clip, scale=scale, gpu=gpu, gpu_device_path=gpu_device_path,
                                            cancel_check=cancel_check, timeout_s=timeout_s)  # fmt: skip
        command, hw_active = frames.decode_command(
            ffmpeg, clip.path, start_s=0.0, length_s=None, keyframes_only=False, fps=1, gpu=gpu,
            gpu_device_path=gpu_device_path, scale=scale, download_format=clip.download_format,
        )  # fmt: skip
        command = [arg.replace("flags=neighbor", "flags=bilinear") for arg in command]
        digests: list[str] = []

        def digest(planes):
            digests.extend(hashlib.sha256(plane.tobytes()).hexdigest() for plane in planes)
            return [()] * len(planes)

        rows = frames.run_decode(command, hw_active=hw_active, detect_boxes=digest, pts_offset_s=0.0,
                                 timeout_s=timeout_s, scale=scale)  # fmt: skip
        return tuple((row[0], found) for row, found in zip(rows, digests, strict=True))

    answer, lines, _ = _check("NVIDIA", "cuda:0", ffmpeg, loguru_caplog, decode=blurred)
    assert answer is False
    (line,) = lines
    assert line.startswith("Credits decoding on cuda:0 doesn't match the reference decode: h264-8bit.mkv at 320x180: ")
    assert re.search(r"\d of 9 frames differ from the CPU's", line)
    assert line.endswith("Credits detection keeps decoding on this GPU.")
