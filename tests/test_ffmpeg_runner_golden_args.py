"""Preview ffmpeg command lines stay byte for byte what they were before the hwaccel arguments moved to hwaccel.py.

The golden file was written from the runner at 7f86d3d, before the move. It covers the first run's matrix (vendor ×
device path × path kind × Vulkan/DV5 flags × keyframe skip, plus thread caps and the DEBUG log level) and the retries
``generator.py`` runs on top of it: the DV-safe retry (``path_kind_override="sdr"``) and the software libplacebo retry
(``vf_override``). The GPU, device path and thread overrides get one cell per call site.
"""

from __future__ import annotations

import functools
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from media_preview_generator.processing import ffmpeg_runner
from media_preview_generator.processing.filter_chain import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
)

GOLDEN = Path(__file__).parent / "fixtures" / "ffmpeg_runner_golden_args.json"
DEVICES = (
    (None, None),
    ("NVIDIA", "cuda:0"),
    ("NVIDIA", "cuda:1"),
    ("NVIDIA", "cuda:"),
    ("NVIDIA", None),
    ("INTEL", "/dev/dri/renderD128"),
    ("INTEL", None),
    ("AMD", "/dev/dri/renderD129"),
    ("WINDOWS_GPU", None),
    ("APPLE", None),
    ("OTHER", "/dev/dri/renderD130"),
)
KINDS = ("sdr", "hdr10_zscale", DV5_PATH_LIBPLACEBO, DV5_PATH_VAAPI_VULKAN, DV5_PATH_INTEL_OPENCL)
DV_SAFE_RETRY_DEVICES = (
    (None, None),
    ("NVIDIA", "cuda:0"),
    ("NVIDIA", "cuda:1"),
    ("INTEL", "/dev/dri/renderD128"),
    ("AMD", "/dev/dri/renderD128"),
    ("OTHER", "/dev/dri/renderD130"),
)
# What generator.py passes as vf_override on the software libplacebo retry: build_dv5_vf(DV5_PATH_LIBPLACEBO) with
# the default hable tonemap, a 10 s frame interval and the production base scale.
SW_LIBPLACEBO_VF = (
    "fps=fps=0.1:round=up,hwupload,libplacebo=tonemapping=hable:format=yuv420p:contrast=1.3:saturation=1.3,"
    "hwdownload,format=yuv420p,scale=w=320:h=240:force_original_aspect_ratio=decrease"
)


def _case(gpu: str | None, device: str | None, kind: str, **changes: object) -> dict:
    case = dict(
        gpu=gpu, device=device, kind=kind, init_vulkan=False, use_skip=True, no_dv5=False, threads=2,
        log_level="INFO", threads_override=None, gpu_override=None, device_override=None, kind_override=None,
        vf_override=None,
    )  # fmt: skip
    assert changes.keys() <= case.keys(), changes
    case.update(changes)
    return case


def _cases() -> dict[str, dict]:
    cases: dict[str, dict] = {}
    for (gpu, device), kind, init_vulkan, use_skip, no_dv5 in itertools.product(
        DEVICES, KINDS, (False, True), (False, True), (False, True)
    ):
        cases[f"{gpu}|{device}|{kind}|vulkan={init_vulkan}|skip={use_skip}|no_vaapi_dv5={no_dv5}"] = _case(
            gpu, device, kind, init_vulkan=init_vulkan, use_skip=use_skip, no_dv5=no_dv5
        )
    for gpu, device in DEVICES:
        for threads, level in ((0, "INFO"), (2, "DEBUG")):
            cases[f"{gpu}|{device}|sdr|threads={threads}|{level}"] = _case(
                gpu, device, "sdr", threads=threads, log_level=level
            )
    # generator.py's DV-safe retry: run(use_skip=False, path_kind_override="sdr") after an HDR10 or DV5 first run.
    for (gpu, device), kind in itertools.product(
        DV_SAFE_RETRY_DEVICES, ("hdr10_zscale", DV5_PATH_LIBPLACEBO, DV5_PATH_VAAPI_VULKAN, DV5_PATH_INTEL_OPENCL)
    ):
        cases[f"{gpu}|{device}|{kind}|dv_safe_retry"] = _case(gpu, device, kind, use_skip=False, kind_override="sdr")
    # generator.py's software libplacebo retry after a VAAPI DV5 failure. Intel's OpenCL trigger collapses to the same
    # argv: disable_vaapi_dv5 turns off both hardware DV5 paths, and vf_override replaces the vendor filter chain.
    cases[f"AMD|/dev/dri/renderD129|{DV5_PATH_VAAPI_VULKAN}|sw_libplacebo_retry"] = _case(
        "AMD", "/dev/dri/renderD129", DV5_PATH_VAAPI_VULKAN, init_vulkan=True, use_skip=False, no_dv5=True,
        vf_override=SW_LIBPLACEBO_VF,
    )  # fmt: skip
    # One override cell per hwaccel call site: the NVIDIA branch, then the non-Vulkan VAAPI branch.
    cases["INTEL|/dev/dri/renderD128|sdr|override=NVIDIA,cuda:1"] = _case(
        "INTEL", "/dev/dri/renderD128", "sdr", gpu_override="NVIDIA", device_override="cuda:1"
    )
    cases["NVIDIA|cuda:0|sdr|override=AMD,/dev/dri/renderD129"] = _case(
        "NVIDIA", "cuda:0", "sdr", gpu_override="AMD", device_override="/dev/dri/renderD129"
    )
    cases["NVIDIA|cuda:0|sdr|ffmpeg_threads_override=4"] = _case("NVIDIA", "cuda:0", "sdr", threads_override=4)
    return cases


CASES = _cases()


def _argv(case: dict) -> dict:
    config = SimpleNamespace(
        ffmpeg_path="/usr/bin/ffmpeg", thumbnail_quality=4, log_level=case["log_level"], ffmpeg_threads=case["threads"]
    )
    runner = ffmpeg_runner.create_ffmpeg_runner(
        video_file="/media/Movie (2020)/Movie.mkv",
        output_folder="/tmp/golden-out",
        gpu=case["gpu"],
        gpu_device_path=case["device"],
        config=config,
        progress_callback=None,
        ffmpeg_threads_override=case["threads_override"],
        cancel_check=None,
        pause_check=None,
        path_kind=case["kind"],
        libplacebo_vf="LIBPLACEBO_VF",
        use_libplacebo=case["init_vulkan"],
        dv5_software_fallback=False,
        base_scale="scale=w=320:h=240:force_original_aspect_ratio=decrease",
        fps_filter="fps=fps=0.1:round=up",
        hdr10_zscale_chain="ZSCALE_CHAIN",
    )
    proc = MagicMock(returncode=0, pid=4242)
    proc.poll.return_value = 0
    with (
        patch.object(ffmpeg_runner.subprocess, "Popen", return_value=proc) as popen,
        patch.object(ffmpeg_runner.time, "sleep"),
        patch(
            "media_preview_generator.gpu.vulkan_probe.get_vulkan_env_overrides",
            return_value={"VK_DRIVER_FILES": "/icd.json"},
        ),
    ):
        runner(
            use_skip=case["use_skip"],
            gpu_override=case["gpu_override"],
            gpu_device_path_override=case["device_override"],
            vf_override=case["vf_override"],
            init_vulkan=case["init_vulkan"],
            disable_vaapi_dv5=case["no_dv5"],
            path_kind_override=case["kind_override"],
        )
    env = popen.call_args.kwargs["env"]
    return {
        "args": list(popen.call_args.args[0]),
        "vk_driver_files": None if env is None else env.get("VK_DRIVER_FILES"),
    }


@functools.lru_cache(maxsize=1)
def _golden() -> dict:
    return json.loads(GOLDEN.read_text())


def test_golden_file_covers_every_case() -> None:
    assert sorted(_golden()) == sorted(CASES)


@pytest.mark.parametrize("case_id", sorted(CASES))
def test_preview_argv_is_unchanged(case_id: str) -> None:
    assert _argv(CASES[case_id]) == _golden()[case_id]


def write_golden_file() -> None:
    """Rewrite the golden file from the runner this interpreter imports. Not part of the test suite.

    Run ``python -m tests.test_ffmpeg_runner_golden_args`` from the root of a checkout whose runner is known good, and
    check ``ffmpeg_runner.__file__`` in the output is that checkout. Then diff the new fixture by eye against the
    previous one before staging it: every changed entry is a changed production ffmpeg command line.
    """
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps({case_id: _argv(case) for case_id, case in sorted(CASES.items())}, indent=1) + "\n")
    print(f"wrote {len(CASES)} cases to {GOLDEN} from {ffmpeg_runner.__file__}")


if __name__ == "__main__":
    write_golden_file()
