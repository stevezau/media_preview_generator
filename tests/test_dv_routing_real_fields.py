"""Dolby Vision routing driven by the MediaInfo fields real library files carry.

Every row except ``p84_hlg`` and ``hdr10`` is copied from a file in the owner's library, as read by libmediainfo
24.12 (the version pymediainfo 7.0.1 bundles, and the one in the Docker image).  Those two are synthetic: MediaInfo's
shape for formats the library survey has no file of.  MediaInfo keeps the profile tag in
``HDR_Format_Profile``; ``hdr_format`` never contains ``dvhe.05``.  The composite text
``"Dolby Vision, Version 1.0, dvhe.05.06, BL+RPU"`` is what MediaInfo prints for humans, not what the app reads.

The routing rule under test: a file takes the Profile 5 path only when it is Dolby Vision and its stream declares
no PQ or HLG transfer.  An ``SMPTE ST 2086`` entry only means HDR10 metadata is present, so it must not decide.
"""

from typing import NamedTuple
from unittest.mock import MagicMock, mock_open, patch

import pytest

from media_preview_generator.gpu import VulkanProbeResult
from media_preview_generator.processing import ffmpeg_runner, generate_images
from media_preview_generator.processing.filter_chain import DV5_PATH_LIBPLACEBO, build_dv5_vf

HW_VULKAN = VulkanProbeResult(device="Quadro P5000 (discrete) (0x1bb0)", is_software=False)
SW_VULKAN = VulkanProbeResult(device="llvmpipe (LLVM 18.1.3, 256 bits) (software) (0x0)", is_software=True)

FPS = "fps=fps=0.2:round=up"
SCALE = "scale=w=320:h=240:force_original_aspect_ratio=decrease"
TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,"
    "zscale=p=bt709,tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)
SDR_VF = f"{FPS},{SCALE}"
HDR_VF = f"{FPS},{TONEMAP},{SCALE}"
DV5_VF = build_dv5_vf(path_kind=DV5_PATH_LIBPLACEBO, tonemap_algorithm="hable", fps_value=0.2, base_scale=SCALE)

DV5 = "dv5"
HDR = "hdr"
SDR = "sdr"


class Row(NamedTuple):
    hdr_format: str | None
    hdr_format_profile: str | None
    hdr_format_compatibility: str | None
    transfer_characteristics: str | None
    route: str


REAL_ROWS = [
    # Profile 8.1 (dv_bl_signal_compatibility_id=1) without the ST 2086 SEI: The Lion King (2019) DSNP WEB-DL.
    # One of the 12 library files that used to take the Profile 5 path.
    pytest.param(Row("Dolby Vision", "dvhe.08", None, "PQ", HDR), id="p8_no_st2086"),
    pytest.param(Row("Dolby Vision / SMPTE ST 2086", "dvhe.08 / ", "HDR10 / HDR10", "PQ", HDR), id="p8_st2086"),
    # HDR10+ with no ST 2086 compat entry: Tomorrowland (2015).
    pytest.param(Row("Dolby Vision / SMPTE ST 2094 App 4", "dvhe.08 / ", None, "PQ", HDR), id="p8_hdr10plus"),
    # Profile 8.4 (HLG base, phone recordings).  No library file has it; the fields are MediaInfo's shape for
    # dv_bl_signal_compatibility_id=4.
    pytest.param(Row("Dolby Vision", "dvhe.08", "HLG", "HLG", HDR), id="p84_hlg"),
    # Profile 7 (BL+EL+RPU): Hamnet (2025) Blu-ray.
    pytest.param(Row("Dolby Vision / SMPTE ST 2086", "dvhe.07 / ", "Blu-ray / HDR10", "PQ", HDR), id="p7"),
    # AV1 Profile 10 with an HDR10 base: The Truman Show (1998).
    pytest.param(
        Row("Dolby Vision / SMPTE ST 2086 / SMPTE ST 2086", "dav1.10", "HDR10 / HDR10 / HDR10", "PQ", HDR),
        id="av1_p10",
    ),
    # Mislabelled Profile 5: Falling for Christmas (2022) WEB-DL.  The base layer is plain HDR10 (VUI PQ, limited
    # range) and the HDR10 chain gives the right colours; the Profile 5 reshaping turns reds yellow.
    pytest.param(Row("Dolby Vision / SMPTE ST 2086", "dvhe.05 / ", " / HDR10", "PQ", HDR), id="p5_hdr10_base"),
    # True Profile 5 (IPTPQc2, VUI unspecified, full range): Subservience (2024) Blu-ray.
    pytest.param(Row("Dolby Vision", "dvhe.05", None, None, DV5), id="p5_true"),
    # True Profile 5 listed twice by MediaInfo: One Last Adventure (2026) WEB-DL.
    pytest.param(Row("Dolby Vision / Dolby Vision", "dvhe.05 / dvhe.05", None, None, DV5), id="p5_true_twice"),
    # Non-DV HDR10, synthetic (MediaInfo's shape for a mastering-display HDR10 stream).
    pytest.param(Row("SMPTE ST 2086", None, "HDR10", "PQ", HDR), id="hdr10"),
    # SDR: Spring Forward (2000) DVD.
    pytest.param(Row(None, None, None, "BT.709", SDR), id="sdr"),
]


def _run(row: Row, temp_dir: str, mock_config: MagicMock, vulkan: VulkanProbeResult) -> tuple[list[str], dict]:
    """Run ``generate_images`` on CPU and return the first FFmpeg argv and the kwargs of the runner factory."""
    track = MagicMock(
        hdr_format=row.hdr_format,
        hdr_format_profile=row.hdr_format_profile,
        hdr_format_compatibility=row.hdr_format_compatibility,
        transfer_characteristics=row.transfer_characteristics,
    )
    info = MagicMock()
    info.video_tracks = [track]

    proc = MagicMock()
    proc.poll.side_effect = [None, 0]
    proc.returncode = 0

    def fake_glob(pattern: str) -> list[str]:
        if "img*.jpg" in pattern:
            return [f"{temp_dir}/img-000001.jpg"]
        if pattern.endswith("*.jpg"):
            return [f"{temp_dir}/0000000000.jpg"]
        return []

    with (
        patch("media_preview_generator.processing.generator.MediaInfo") as mi,
        patch("subprocess.Popen", return_value=proc) as popen,
        patch("subprocess.run", return_value=MagicMock(returncode=0)),
        patch("media_preview_generator.processing.generator.os.rename"),
        patch("media_preview_generator.processing.generator.os.remove"),
        patch("os.path.exists", return_value=True),
        patch("builtins.open", new_callable=mock_open),
        patch("time.sleep"),
        patch("media_preview_generator.processing.generator.glob.glob", side_effect=fake_glob),
        patch("media_preview_generator.processing.generator._detect_codec_error", return_value=False),
        # Keyframes every second: the keyframe-only fast path stays on unless the route forbids it.
        patch("media_preview_generator.processing.generator._probe_max_keyframe_gap", return_value=1.0),
        patch("media_preview_generator.gpu.vulkan_probe.get_vulkan_device_info", return_value=vulkan),
        patch.object(ffmpeg_runner, "create_ffmpeg_runner", wraps=ffmpeg_runner.create_ffmpeg_runner) as runner,
    ):
        mi.parse.return_value = info
        success, *_ = generate_images("/test/video.mkv", temp_dir, None, None, mock_config)
    assert success is True
    runner.assert_called_once()
    return popen.call_args_list[0][0][0], runner.call_args.kwargs


class TestDvRoutingOnRealMediaInfoFields:
    @pytest.mark.parametrize("row", REAL_ROWS)
    def test_route_when_hardware_vulkan(self, row: Row, temp_dir: str, mock_config: MagicMock) -> None:
        args, runner_kwargs = _run(row, temp_dir, mock_config, HW_VULKAN)
        vf = args[args.index("-vf") + 1]

        if row.route == DV5:
            assert runner_kwargs["path_kind"] == DV5_PATH_LIBPLACEBO
            assert runner_kwargs["use_libplacebo"] is True
            assert runner_kwargs["dv5_software_fallback"] is False
            assert runner_kwargs["libplacebo_vf"] == DV5_VF
            assert vf == DV5_VF
            assert "-skip_frame:v" not in args
        else:
            assert runner_kwargs["path_kind"] == ("hdr10_zscale" if row.route == HDR else "sdr")
            assert runner_kwargs["use_libplacebo"] is False
            assert runner_kwargs["dv5_software_fallback"] is False
            assert runner_kwargs["libplacebo_vf"] is None
            assert vf == (HDR_VF if row.route == HDR else SDR_VF)
            assert args[args.index("-skip_frame:v") + 1] == "nokey"

    @pytest.mark.parametrize("row", REAL_ROWS)
    def test_route_when_software_vulkan(self, row: Row, temp_dir: str, mock_config: MagicMock) -> None:
        # No hardware Vulkan: a true Profile 5 gets the untonemapped DV-safe chain; anything with an HDR10 or HLG
        # base must still be tone mapped, or its thumbnails come out grey and washed out.
        args, runner_kwargs = _run(row, temp_dir, mock_config, SW_VULKAN)
        vf = args[args.index("-vf") + 1]

        assert runner_kwargs["use_libplacebo"] is False
        assert runner_kwargs["libplacebo_vf"] is None
        if row.route == DV5:
            assert runner_kwargs["path_kind"] == "sdr"
            assert runner_kwargs["dv5_software_fallback"] is True
            assert vf == SDR_VF
            assert "-skip_frame:v" not in args
        else:
            assert runner_kwargs["path_kind"] == ("hdr10_zscale" if row.route == HDR else "sdr")
            assert runner_kwargs["dv5_software_fallback"] is False
            assert vf == (HDR_VF if row.route == HDR else SDR_VF)
            assert args[args.index("-skip_frame:v") + 1] == "nokey"
