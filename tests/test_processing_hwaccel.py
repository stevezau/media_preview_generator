"""hwaccel_decode_args: every vendor × device path × keep-on-GPU cell (the same branches the preview runner had)."""

import pytest

from media_preview_generator.processing.hwaccel import HwDecode, hwaccel_decode_args

RENDER = "/dev/dri/renderD128"


@pytest.mark.parametrize(
    ("gpu", "device", "keep", "expected"),
    [
        (None, None, True, HwDecode((), False)),
        (None, RENDER, True, HwDecode((), False)),
        ("NVIDIA", "cuda:0", True, HwDecode(("-hwaccel", "cuda", "-hwaccel_device", "0", "-hwaccel_output_format", "cuda"), True)),
        ("NVIDIA", "cuda:3", False, HwDecode(("-hwaccel", "cuda", "-hwaccel_device", "3"), True)),
        ("NVIDIA", "cuda:", True, HwDecode(("-hwaccel", "cuda", "-hwaccel_output_format", "cuda"), True)),
        ("NVIDIA", None, False, HwDecode(("-hwaccel", "cuda"), True)),
        ("NVIDIA", RENDER, True, HwDecode(("-hwaccel", "cuda", "-hwaccel_output_format", "cuda"), True)),
        ("INTEL", RENDER, True, HwDecode(("-hwaccel", "vaapi", "-hwaccel_device", RENDER, "-hwaccel_output_format", "vaapi"), True)),
        ("AMD", "/dev/dri/renderD129", False, HwDecode(("-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD129"), True)),
        ("INTEL", None, True, HwDecode((), False)),
        ("INTEL", "cuda:0", True, HwDecode((), False)),
        ("AMD", "/dev/video0", True, HwDecode((), False)),
        ("WINDOWS_GPU", RENDER, True, HwDecode(("-hwaccel", "d3d11va"), True)),
        ("APPLE", None, False, HwDecode(("-hwaccel", "videotoolbox"), True)),
        ("OTHER", RENDER, False, HwDecode(("-hwaccel", "vaapi", "-hwaccel_device", RENDER), True)),
    ],
)  # fmt: skip
def test_hwaccel_decode_args(gpu, device, keep, expected):
    assert hwaccel_decode_args(gpu, device, keep_on_gpu=keep) == expected
