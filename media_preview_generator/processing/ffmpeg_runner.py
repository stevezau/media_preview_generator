"""FFmpeg subprocess runner factory for the per-file thumbnail pipeline.

This module owns the work that ``_run_ffmpeg`` used to do inside a
nested closure in :func:`generator.generate_images` — building
the FFmpeg argv, selecting the vendor-specific filter chain, invoking
the subprocess, streaming progress to the caller's callback, detecting
stalls, and diagnosing non-zero exit codes.

The public entry point is :func:`create_ffmpeg_runner`, a factory that
captures all the configuration state for ONE media file and returns a
callable with the same signature as the original nested
``_run_ffmpeg``:

    run = create_ffmpeg_runner(video_file=..., output_folder=..., ...)
    rc, seconds, speed, stderr_lines = run(
        use_skip=True, init_vulkan=use_libplacebo
    )

Keeping the factory-function shape (rather than a class) means the
call sites in ``generate_images``' retry cascade don't need to change
— every ``_run_ffmpeg(...)`` call becomes ``run(...)``.  The
``-hwaccel`` decode arguments come from ``processing/hwaccel.py``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any, cast

from loguru import logger

from ..logging_config import per_file_logger
from ..utils import (
    FFMPEG_LOG_PREFIX,
    dies_with_node,
    gpu_settings_hint,
    is_node_mode,
    node_temp_root,
    scratch_dir,
    working_folder_hint,
)
from .filter_chain import (
    DV5_PATH_INTEL_OPENCL,
    DV5_PATH_LIBPLACEBO,
    DV5_PATH_VAAPI_VULKAN,
)
from .hwaccel import hwaccel_decode_args

# Added to the stderr lines a run returns when the stall watchdog stopped it: the exit code (-9) alone reads like an OOM
# kill, and the retry cascade words its CPU hand-off by the real cause (``generator._gpu_hand_off``).
STALL_WATCHDOG_LINE = "[media_preview_generator] FFmpeg stopped making progress and was stopped by the stall watchdog"
ACTIVE_TIMEOUT_LINE = "[media_preview_generator] FFmpeg exceeded its active attempt time limit"
PROCESS_EXIT_PENDING_LINE = "[media_preview_generator] FFmpeg did not exit after termination; cleanup is pending"
DIAGNOSTIC_LIMIT_LINE = "[media_preview_generator] FFmpeg exceeded the chapter diagnostic output limit"
_CHAPTER_STDERR_LIMIT = 4 * 1024 * 1024
_CHAPTER_READ_LIMIT = 64 * 1024
_MATROSKA_CONTEXT = re.compile(r"^\[(?:in#\d+/)?matroska(?:,webm)?(?:\s+@[^\]]*)?\]\s*(.*)$", re.IGNORECASE)
_EBML_CORRUPTION = re.compile(
    r"^(?:0x[0-9a-f]+ at pos \d+(?: \(0x[0-9a-f]+\))? invalid as first byte of an EBML number"
    r"|Length \d+ indicated by an EBML number's first byte .* exceeds max length \d+)",
    re.IGNORECASE,
)
_HEVC_PARAMETER_ERROR = re.compile(
    r"^\[hevc(?:\s+@\s*(?:0x)?[0-9a-f]+)?\]\s*"
    r"(?:(?P<missing>(?:VPS|SPS) \d+ does not exist\.?)"
    r"|(?P<pps>PPS id out of range: \d+)"
    r"|(?P<nalu>Skipping invalid undecodable NALU: \d+))$",
    re.IGNORECASE,
)


# Lines that never say why ffmpeg failed: showinfo's per-frame lines ("color_space:unknown" reads as an error to an
# error summary), the end-of-run stats ("video:0KiB audio:0KiB ... muxing overhead: unknown", the progress line) and
# the "Conversion failed!" every failed run ends on, which would hide the line before it.
SAYS_NOTHING_RE = re.compile(
    r"\[Parsed_showinfo_\d+ @|^Conversion failed!\s*$|^frame=|muxing overhead|\bvideo:\d+\w*B\s+audio:\d+\w*B"
)


def is_matroska_corruption_line(line: str) -> bool:
    """Identify explicit structural errors, not optional unknown EBML elements."""
    context = _MATROSKA_CONTEXT.match(line.strip())
    return bool(context and _EBML_CORRUPTION.match(context.group(1)))


def hevc_parameter_error_kind(line: str) -> str | None:
    """Recognize decoder-owned parameter errors without matching metadata text."""
    match = _HEVC_PARAMETER_ERROR.fullmatch(line.strip())
    return match.lastgroup if match else None


def has_hevc_parameter_failure(lines: list[str]) -> bool:
    """Require failed parameter initialization and undecodable packets together.

    A single damaged packet or warning does not establish this failure. The
    full signature also occurs with intact hvcC arrays in an order FFmpeg
    cannot initialize, so it must not be described as a corrupt media file.
    """
    return {"missing", "pps", "nalu"}.issubset({hevc_parameter_error_kind(line) for line in lines})


# Added to the stderr lines of a GPU run stopped at the decoder's own verdict that it can't decode the file on this
# GPU; the retry cascade hands the file to the CPU as an unsupported codec (``generator._gpu_hand_off``).
GPU_CANT_DECODE_LINE = "[media_preview_generator] The GPU can't decode this video's codec; the GPU run was stopped"

# Printed by FFmpeg decoders that have no software path (its AV1 decoder for GPUs) when the GPU can't decode the file:
# once per packet to the end of the file, with no frame ever written. Not the generic "hwaccel initialisation returned
# error": decoders with a software path print that and carry on, or FFmpeg ends the run itself.
GPU_CANT_DECODE_VERDICT = "Your platform doesn't support hardware accelerated"

_PROGRESS_FRAME_RE = re.compile(r"^frame=\s*(\d+)")


def _signal_name(returncode: int) -> str:
    """The name of the signal an exit code reports, raw (-n) or as a shell's 128+n, e.g. ``SIGTERM``.

    Args:
        returncode: FFmpeg exit code of a signalled run.

    Returns:
        The signal's name, or ``signal <n>`` for a number this platform doesn't name.
    """
    number = -returncode if returncode < 0 else returncode - 128
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def create_ffmpeg_runner(
    *,
    video_file: str,
    output_folder: str,
    gpu: str | None,
    gpu_device_path: str | None,
    config,
    progress_callback: Callable | None,
    ffmpeg_threads_override: int | None,
    cancel_check: Callable | None,
    pause_check: Callable | None = None,
    path_kind: str,
    libplacebo_vf: str | None,
    use_libplacebo: bool,
    dv5_software_fallback: bool,
    base_scale: str,
    fps_filter: str,
    hdr10_zscale_chain: str,
    chapter_start_ms: int | None = None,
    chapter_output: str | None = None,
    active_timeout_s: float | None = None,
    last_command: list[str] | None = None,
    turn: str | None = None,
) -> Callable[..., tuple[int, float, str, list[str]]]:
    """Factory: return a configured ffmpeg-runner closure for one media item.

    The returned callable has the same signature as the original nested
    ``_run_ffmpeg`` function and captures every piece of per-file state
    (video_file, filter-chain choice, retry flags, etc.) exactly as the
    original closure did.  Callers in :func:`generator.generate_images`
    replace ``def _run_ffmpeg(...)`` with a single assignment and then
    invoke the returned callable through the full retry cascade.

    ``last_command``, when given, holds each run's FFmpeg command from the moment it starts: a tier can decode with
    another ``-hwaccel`` than the worker's GPU (the software Dolby Vision tier has none), and the CPU hand-off judges
    a run by the decoder it asked for.

    ``turn`` is the filters that show the stream as FFmpeg shows it (:func:`generator.display_turn`). FFmpeg turns
    and mirrors frames in memory by itself but not frames that stay on the GPU, so the GPU scale paths turn them after
    download.
    """
    # Import at factory-call time to avoid a circular dependency at module
    # load (orchestrator imports create_ffmpeg_runner from here). Runs
    # once per media file, not once per FFmpeg invocation.
    from .generator import (
        _FOLDER_WRITE_TEXT,
        FFMPEG_STALL_TIMEOUT_SEC,
        NO_DECODER_PHRASE,
        STRIP_H264_SEI_BSF,
        CancellationError,
        _clean_output_images,
        _crash_signal,
        _diagnose_ffmpeg_exit_code,
        _folder_write_errno,
        _gpu_lacks_the_decoder,
        _io_cut_off,
        _is_signal_killed,
        _save_ffmpeg_failure_log,
        _without_metadata_tags,
        folder_write_fix,
        h264_side_data_twice,
        hwaccel_in,
        parse_ffmpeg_progress_line,
    )

    # Set once FFmpeg refused the video for carrying its 3D layout twice (``h264_side_data_twice``).
    read_without_sei = False

    def _gpu_scale_segment(effective_gpu: str | None, hw_decode_active: bool, fmt: str) -> str | None:
        """GPU-side scale + hwdownload segment for the active vendor,
        or None to keep CPU scale in place (software decode, DV5
        libplacebo paths, unsupported vendor).  ``fmt`` is ``nv12`` for
        8-bit paths, ``p010le`` for the HDR10 zscale chain.

        scale_cuda supports ``force_divisible_by=2`` directly.
        scale_vaapi does not, so a tiny CPU ``scale=trunc(iw/2)*2:
        trunc(ih/2)*2`` runs after hwdownload — essentially free on a
        320xN frame, a no-op on already-even dims.  Letterboxed 2.4:1
        content would otherwise produce odd heights (e.g. 320x133) and
        break zscale's 4:2:0 subsampling requirement.
        """
        if not hw_decode_active:
            return None
        # A turned or mirrored stream's frames are turned after download (``_assemble_vf``) and then sized by the CPU
        # path's own scale (``base_scale``), so its thumbnails match the CPU's; the GPU first shrinks them to twice
        # that box, as stored (portrait boxes for a quarter turn).
        if turn is None:
            box = "w=320:h=240"
        elif turn.startswith("transpose"):
            box = "w=480:h=640"
        else:
            box = "w=640:h=480"
        if effective_gpu == "NVIDIA":
            return (
                f"scale_cuda={box}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2:format={fmt},hwdownload,format={fmt}"
            )
        if effective_gpu in {"INTEL", "AMD"}:
            return (
                f"scale_vaapi={box}:force_original_aspect_ratio=decrease:"
                f"format={fmt},hwdownload,format={fmt},"
                f"scale=trunc(iw/2)*2:trunc(ih/2)*2"
            )
        return None

    def _assemble_vf(
        effective_gpu: str | None,
        hw_decode_active: bool,
        effective_kind: str,
    ) -> str:
        """Build the vf chain for the current attempt.

        For SDR and HDR10/DV P7+8 paths, the chain is vendor-aware: on
        NVIDIA/VAAPI the downscale runs on the GPU and only the final
        320x240 frame is hwdownloaded, so mjpeg encode (CPU-only) works
        on a tiny frame instead of a full 4K one.  For HDR10, that also
        means the zscale tonemap chain processes 320x240 frames rather
        than source-resolution frames.

        DV Profile 5 libplacebo / OpenCL chains are pre-assembled and
        returned as-is — they already contain hwupload/hwmap/hwdownload
        (and fps for the OpenCL variant) and are not touched by the
        GPU-scale optimisation.

        ``effective_kind`` lets the DV-safe retry collapse the HDR10
        zscale path to an SDR fps+scale chain while preserving the
        GPU-scale segment (so the retry doesn't lose the perf win
        just because zscale / RPU parsing failed).
        """
        if effective_kind in {
            DV5_PATH_LIBPLACEBO,
            DV5_PATH_VAAPI_VULKAN,
            DV5_PATH_INTEL_OPENCL,
        }:
            assert libplacebo_vf is not None
            return libplacebo_vf
        fmt = "p010le" if effective_kind == "hdr10_zscale" else "nv12"
        gpu_seg = _gpu_scale_segment(effective_gpu, hw_decode_active, fmt)
        if gpu_seg is None:
            if effective_kind == "hdr10_zscale":
                return f"{fps_filter},{hdr10_zscale_chain},{base_scale}"
            # SDR (also covers dv5_software_fallback — DV-safe fps+scale).
            return f"{fps_filter},{base_scale}"
        vf = f"{fps_filter},{gpu_seg}"
        if effective_kind == "hdr10_zscale":
            vf += f",{hdr10_zscale_chain}"
        if turn is not None:
            vf += f",{turn},{base_scale}"
        return vf

    def _run_ffmpeg(
        use_skip: bool,
        gpu_override: str | None = None,
        gpu_device_path_override: str | None = None,
        vf_override: str | None = None,
        init_vulkan: bool = False,
        disable_vaapi_dv5: bool = False,
        path_kind_override: str | None = None,
    ) -> tuple[int, float, str, list[str]]:
        """Run FFmpeg once and return (returncode, seconds, speed, stderr_lines).

        A 3D video FFmpeg refuses as it is (``h264_side_data_twice``) is run again at once without its H.264 SEI, and
        so is every later run of this runner, decoding every frame: the SEI also carries the recovery points that let
        ``-skip_frame nokey`` decode an open-GOP encode's keyframes, so keyframe-only decode would keep only the first.
        """
        nonlocal read_without_sei
        if read_without_sei:
            use_skip = False
        # Build FFmpeg command with proper argument ordering
        # Hardware acceleration flags must come BEFORE the input file (-i)
        # Propagate the app's log level to FFmpeg so DEBUG reports include
        # full VAAPI / Vulkan / Mesa / libplacebo internals (thousands of
        # lines per 4K job).  INFO is the everyday default.
        ffmpeg_loglevel = "debug" if config.log_level == "DEBUG" else "info"
        args = [
            config.ffmpeg_path,
            "-loglevel",
            ffmpeg_loglevel,
        ]

        # Cap FFmpeg's global and filter-graph thread pools for GPU workers.
        # GPU decode is offloaded to hardware, so the CPU threads are mostly
        # idle overhead; capping them prevents thread oversubscription when
        # running multiple workers.  CPU paths are left uncapped so software
        # decode can use all available cores.
        effective_gpu = gpu_override if gpu_override is not None else gpu
        effective_ffmpeg_threads = (
            ffmpeg_threads_override if ffmpeg_threads_override is not None else config.ffmpeg_threads
        )
        if (effective_gpu is not None or chapter_start_ms is not None) and effective_ffmpeg_threads > 0:
            args += [
                "-threads",
                str(effective_ffmpeg_threads),
                "-filter_threads",
                str(effective_ffmpeg_threads),
            ]

        # Hardware acceleration for decoding (before -i flag).
        #
        # Non-libplacebo paths (HDR10, SDR, DV Profile 7/8 via zscale on
        # the HDR10 base layer) have always benefited from HW decode
        # across all supported vendors.
        #
        # The DV Profile 5 libplacebo path (``init_vulkan=True``) used
        # to blanket-skip HW decode on non-NVIDIA vendors (P7/8 now uses
        # zscale on the HDR10 base layer, so that gate's reason is gone).
        # A bench on an Intel iGPU (Raptor Lake-S) compared:
        #   - software decode + libplacebo:  12.9x, ~10 cores saturated
        #   - VAAPI decode + drm→va@dr→vk@dr: 16.1x,  ~0 cores (1s CPU)
        # Output was pixel-identical (PSNR=inf) across dark, mid, and
        # bright scenes.  So on Linux VAAPI GPUs (Intel iGPU/Arc + AMD
        # Radeon) we now use zero-copy VAAPI→Vulkan DMA-BUF interop.
        # NVIDIA keeps CUDA.  Non-Linux platforms don't reach the
        # libplacebo branch and are unaffected.
        effective_gpu_device_path = (
            gpu_device_path_override if gpu_device_path_override is not None else gpu_device_path
        )
        use_gpu = effective_gpu is not None
        use_intel_opencl_dv5 = (
            init_vulkan
            and use_gpu
            and effective_gpu == "INTEL"
            and effective_gpu_device_path is not None
            and effective_gpu_device_path.startswith("/dev/dri/")
            and not disable_vaapi_dv5
        )
        use_vaapi_dv5 = (
            init_vulkan
            and use_gpu
            and effective_gpu not in ("NVIDIA", "INTEL")
            and effective_gpu_device_path is not None
            and effective_gpu_device_path.startswith("/dev/dri/")
            and not disable_vaapi_dv5
        )

        # Device init for the DV5 tone-mapping context.  Intel and non-Intel
        # VAAPI GPUs take different paths because Intel's VAAPI→Vulkan DMA-BUF
        # interop is broken upstream (libplacebo's vkCreateImage returns
        # VK_ERROR_OUT_OF_DEVICE_MEMORY on Mesa ANV for the format+modifier
        # combinations used for DV5).  Intel gets VAAPI→OpenCL (via Jellyfin-
        # ffmpeg's DV RPU-aware tonemap_opencl patch).  AMD keeps VAAPI→Vulkan
        # derived from a common DRM device (drm=dr → vaapi=va@dr →
        # vulkan=vk@dr), which is Jellyfin's proven pattern for discrete AMD.
        # NVIDIA and software fallback use plain vulkan=vk for libplacebo.
        if init_vulkan:
            if use_intel_opencl_dv5:
                args += [
                    "-init_hw_device",
                    f"vaapi=va:{effective_gpu_device_path}",
                    "-init_hw_device",
                    "opencl=ocl@va",
                    "-filter_hw_device",
                    "ocl",
                ]
            elif use_vaapi_dv5:
                args += [
                    "-init_hw_device",
                    f"drm=dr:{effective_gpu_device_path}",
                    "-init_hw_device",
                    "vaapi=va@dr",
                    "-init_hw_device",
                    "vulkan=vk@dr",
                    "-filter_hw_device",
                    "vk",
                ]
            else:
                args += ["-init_hw_device", "vulkan=vk", "-filter_hw_device", "vk"]

        # Paths that can keep frames on the GPU end-to-end and use
        # scale_cuda / scale_vaapi.  DV5 libplacebo uses hwupload from
        # CPU frames (NVIDIA) or hwmap from VAAPI frames (Intel/AMD),
        # so -hwaccel_output_format is either harmful (NVIDIA — breaks
        # hwupload) or already set (VAAPI DV5 branch below).
        effective_kind = path_kind_override or path_kind
        keep_on_gpu = effective_kind in {"sdr", "hdr10_zscale"}

        hw_decode_active = False
        if use_gpu and effective_gpu == "NVIDIA":
            decode = hwaccel_decode_args(effective_gpu, effective_gpu_device_path, keep_on_gpu=keep_on_gpu)
            args += list(decode.args)
            hw_decode_active = decode.active
        elif use_intel_opencl_dv5 or use_vaapi_dv5:
            # Intel DV5 via VAAPI decode + OpenCL tonemap, OR AMD DV5 via
            # VAAPI decode + Vulkan libplacebo.  Same hwaccel flags (VAAPI
            # decode, frames stay as VAAPI surfaces); the device init
            # block above picks OpenCL vs Vulkan for the tone-map stage.
            args += [
                "-hwaccel",
                "vaapi",
                "-hwaccel_device",
                "va",
                "-hwaccel_output_format",
                "vaapi",
            ]
            hw_decode_active = True
        elif use_gpu and not init_vulkan:
            decode = hwaccel_decode_args(effective_gpu, effective_gpu_device_path, keep_on_gpu=keep_on_gpu)
            args += list(decode.args)
            hw_decode_active = decode.active
        elif use_gpu and init_vulkan:
            logger.debug(
                "Skipping HW decode for DV Profile 5 ({}) on {}: no VAAPI render device available; using software decode + Vulkan/libplacebo tone mapping",
                video_file,
                effective_gpu,
            )

        # Cap the video decoder to 1 thread ONLY when decode is offloaded
        # to a hardware accelerator.  With hwaccel the CPU thread is just
        # an orchestrator and the cap prevents thread oversubscription
        # across parallel GPU workers.  For software decode — pure CPU
        # workers, or DV Profile 5 on non-NVIDIA GPUs where the vendor
        # gate above skips hwaccel — let FFmpeg pick the default thread
        # count so 4K HEVC can saturate available cores.  Fixes issue
        # #212 (DV P5 pinned to one core at ~0.8x before this gate).
        if hw_decode_active:
            args += ["-threads:v", "1"]
        elif init_vulkan and use_gpu:
            # DV Profile 5 on non-NVIDIA GPUs: the vendor gate above
            # fell through to software decode, but the global
            # "-threads N" / "-filter_threads N" above is still in the
            # command.  FFmpeg treats "-threads N" as the default for
            # every codec pool including the video decoder, so without
            # an explicit override the HEVC 4K 10-bit decoder would
            # run on only N threads (2 by default).  Set "-threads:v 0"
            # to tell FFmpeg "pick the optimal count for this decoder",
            # which lets it saturate available cores while the global
            # "-threads N" keeps filter-graph / libplacebo threads
            # bounded.  Fixes issue #212 second-order: bfa67e2 removed
            # the explicit "-threads:v 1" cap but left the global cap
            # bleeding into the decoder.
            args += ["-threads:v", "0"]

        # Add skip_frame option for faster decoding (if safe).
        # Disabled for DV Profile 5 (init_vulkan) — RPU side-data has
        # inter-frame dependencies that break with keyframe-only decode.
        if use_skip and not init_vulkan:
            args += ["-skip_frame:v", "nokey"]

        # Assemble the vf chain now that effective_gpu / hw_decode_active
        # are known.  Explicit vf_override (software libplacebo retry)
        # is honoured verbatim.  path_kind_override lets the DV-safe
        # retry collapse HDR10 to SDR while preserving the GPU-scale
        # segment.
        if vf_override is not None:
            effective_vf = vf_override
        else:
            effective_vf = _assemble_vf(effective_gpu, hw_decode_active, effective_kind)

        if read_without_sei:
            # The first video stream, the one ``h264_side_data_twice`` read the codec of. FFmpeg maps the largest one,
            # so a file with two real video streams of different codecs could be left as it was and fail again.
            args += ["-bsf:V:0", STRIP_H264_SEI_BSF]
        # Chapter grabs reuse the managed process lifecycle but seek directly
        # and stop after one frame rather than decoding an interval sequence.
        if chapter_start_ms is not None:
            args += ["-ss", f"{chapter_start_ms / 1000:.3f}"]
        # Add input file and output options
        args += [
            "-i",
            video_file,
            "-an",
            "-sn",
            "-dn",
            "-q:v",
            str(config.thumbnail_quality),
            "-vf",
            effective_vf,
        ]
        if chapter_start_ms is not None:
            args += ["-map", "0:V:0", "-frames:v", "1", "-threads:v", str(max(1, effective_ffmpeg_threads))]
        args += [chapter_output or f"{output_folder}/img-%06d.jpg"]
        if last_command is not None:
            last_command[:] = args

        start_local = time.time()
        hw_label = "GPU" if gpu else "CPU"
        logger.info("Encoding thumbnails for {} ({})", video_file, hw_label)
        # Full argv at DEBUG only — at INFO with 4-tier retries on a 50K-item
        # library this would be 200K+ lines. Also: the -i path may include
        # vendor-side credentials in some users' setups; keep it off the
        # default log surface.
        logger.debug("FFmpeg command: {}", " ".join(args))

        # When the Layer-3 probe retry in gpu_detection succeeded only with
        # VK_DRIVER_FILES set, propagate those env overrides to the real
        # FFmpeg invocation on the libplacebo DV Profile 5 path. On every
        # other path the override dict is empty and we pass env=None so
        # the child process inherits the parent environment unchanged.
        ffmpeg_env: dict | None = None
        if init_vulkan:
            from ..gpu.vulkan_probe import get_vulkan_env_overrides

            vulkan_overrides = get_vulkan_env_overrides()
            if vulkan_overrides:
                ffmpeg_env = os.environ.copy()
                ffmpeg_env.update(vulkan_overrides)
                logger.debug(
                    "FFmpeg libplacebo path: injecting Vulkan env overrides {} into subprocess", vulkan_overrides
                )

        # Use file polling approach for non-blocking, high-frequency progress monitoring
        thread_id = threading.get_ident()
        # A node's temp folder can be swept away under a running task; FFmpeg's log must not fail the file for it.
        os.makedirs(scratch_dir(), exist_ok=True)
        output_file = os.path.join(
            scratch_dir(),
            f"{FFMPEG_LOG_PREFIX}{os.getpid()}_{thread_id}_{time.time_ns()}.log",
        )
        stderr_fh = open(output_file, "w", encoding="utf-8")
        proc = None
        stderr_reader = None
        try:
            run_args, preexec = dies_with_node(args)  # a node's FFmpeg dies with the node
            proc = subprocess.Popen(
                run_args,
                stderr=stderr_fh,
                stdout=subprocess.DEVNULL,
                env=ffmpeg_env,
                preexec_fn=preexec,
            )

            # Signal that FFmpeg process has started
            if progress_callback:
                progress_callback(0, 0, 0, "0.0x", media_file=video_file)

            # Track progress
            total_duration: float | None = None
            speed_local = "0.0x"
            ffmpeg_output_lines: Any = deque(maxlen=256) if active_timeout_s is not None else []
            sticky_diagnostics: list[str] = []
            last_progress_time = time.time()
            stalled = False
            gpu_cant_decode = False
            wrote_frame = False
            unfinished_line = ""

            def remember(line: str) -> None:
                ffmpeg_output_lines.append(line)
                if active_timeout_s is not None and (
                    is_matroska_corruption_line(line)
                    or hevc_parameter_error_kind(line) is not None
                    or line
                    in {STALL_WATCHDOG_LINE, ACTIVE_TIMEOUT_LINE, PROCESS_EXIT_PENDING_LINE, DIAGNOSTIC_LIMIT_LINE}
                ):
                    if line not in sticky_diagnostics and (
                        len(sticky_diagnostics) < 12 or line.startswith("[media_preview_generator]")
                    ):
                        sticky_diagnostics.append(line)

            def reap_killed_process() -> None:
                """A blocked mount must not hold a chapter worker after SIGKILL."""
                if active_timeout_s is None:
                    proc.wait()
                    return
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("FFmpeg did not exit after termination for {}; cleanup is pending", video_file)
                    remember(PROCESS_EXIT_PENDING_LINE)
                    threading.Thread(target=proc.wait, name=f"ffmpeg-reap-{proc.pid}", daemon=True).start()

            def read_new_stderr(*, process_exited: bool = False) -> list[str]:
                """The lines FFmpeg has added to its stderr file since the last call, stripped, blank ones dropped.

                Reads on from where the last call stopped (the file can reach tens of MB). A line FFmpeg is still
                writing is held back until its newline arrives, or the process has exited.
                """
                nonlocal stderr_reader, unfinished_line
                if stderr_reader is None:
                    if not os.path.exists(output_file):
                        return []
                    # errors="replace": FFmpeg can emit non-UTF-8 bytes in stderr (e.g. file
                    # paths or stream metadata in Latin-1). Strict decode crashed the runner
                    # mid-loop, surfaced as a misleading "corrupt video file" error to users.
                    stderr_reader = open(output_file, encoding="utf-8", errors="replace")
                if active_timeout_s is not None:
                    if process_exited:
                        size = os.path.getsize(output_file)
                        if size - stderr_reader.tell() > _CHAPTER_READ_LIMIT:
                            remember(DIAGNOSTIC_LIMIT_LINE)
                            stderr_reader.seek(size - _CHAPTER_READ_LIMIT)
                            unfinished_line = ""
                    raw_lines = stderr_reader.read(_CHAPTER_READ_LIMIT).splitlines(keepends=True)
                else:
                    raw_lines = stderr_reader.readlines()
                if unfinished_line:
                    raw_lines = [unfinished_line + (raw_lines[0] if raw_lines else ""), *raw_lines[1:]]
                    unfinished_line = ""
                if raw_lines and not process_exited and not raw_lines[-1].endswith("\n"):
                    *raw_lines, unfinished_line = raw_lines
                    if active_timeout_s is not None:
                        unfinished_line = unfinished_line[:8192]
                if active_timeout_s is not None:
                    raw_lines = [line[:8192] for line in raw_lines]
                return [line for line in (raw.strip() for raw in raw_lines) if line]

            def speed_capture_callback(
                progress_percent,
                current_duration,
                total_duration_param,
                speed_value,
                remaining_time=None,
                frame=0,
                fps=0,
                q=0,
                size=0,
                time_str="00:00:00.00",
                bitrate=0,
            ):
                nonlocal speed_local
                if speed_value and speed_value != "0.0x":
                    speed_local = speed_value
                if progress_callback:
                    progress_callback(
                        progress_percent,
                        current_duration,
                        total_duration_param,
                        speed_value,
                        remaining_time,
                        frame,
                        fps,
                        q,
                        size,
                        time_str,
                        bitrate,
                        media_file=video_file,
                    )

            time.sleep(0.02)
            paused_locally = False
            active_elapsed = 0.0
            active_tick = time.monotonic() if active_timeout_s is not None else 0.0
            while proc.poll() is None:
                if active_timeout_s is not None:
                    now_tick = time.monotonic()
                    if not paused_locally:
                        active_elapsed += max(0.0, now_tick - active_tick)
                    active_tick = now_tick
                if cancel_check and cancel_check():
                    logger.info("Cancellation requested, terminating FFmpeg for {}", video_file)
                    # If we were paused, resume first so SIGTERM can be delivered.
                    if paused_locally:
                        try:
                            proc.send_signal(signal.SIGCONT)
                        except (ProcessLookupError, OSError):
                            pass
                        paused_locally = False
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        reap_killed_process()
                    raise CancellationError(f"Processing cancelled for {video_file}")

                # Hard-pause via SIGSTOP/SIGCONT — freezes the FFmpeg process
                # in place without losing work. When pause_check returns True
                # we suspend the subprocess; when it flips back to False we
                # resume. This is the live-halt behaviour that existed before
                # the multi-server refactor: clicking Pause stops in-flight
                # FFmpeg immediately rather than waiting for the current item
                # to complete. SIGSTOP also stops the kernel from advancing
                # the stall-detection clock, so a 30-min pause won't trip the
                # FFMPEG_STALL_TIMEOUT_SEC kill below.
                if pause_check and pause_check():
                    if not paused_locally:
                        try:
                            proc.send_signal(signal.SIGSTOP)
                            paused_locally = True
                            last_progress_time = time.time()  # freeze stall clock
                            logger.info(
                                "FFmpeg paused for {} (PID {}) — global pause is active",
                                video_file,
                                proc.pid,
                            )
                        except (ProcessLookupError, OSError):
                            pass
                    time.sleep(0.2)
                    continue
                if paused_locally:
                    try:
                        proc.send_signal(signal.SIGCONT)
                        paused_locally = False
                        last_progress_time = time.time()  # restart stall clock
                        logger.info("FFmpeg resumed for {} (PID {})", video_file, proc.pid)
                    except (ProcessLookupError, OSError):
                        pass
                if active_timeout_s is not None and active_elapsed >= active_timeout_s:
                    logger.warning("FFmpeg exceeded its {}s active attempt limit for {}", active_timeout_s, video_file)
                    stalled = True
                    proc.kill()
                    reap_killed_process()
                    remember(ACTIVE_TIMEOUT_LINE)
                    break
                try:
                    new_lines = read_new_stderr()
                except OSError:
                    new_lines = []
                for line in new_lines:
                    remember(line)
                    total_duration = parse_ffmpeg_progress_line(line, total_duration, speed_capture_callback)
                    progress = _PROGRESS_FRAME_RE.match(line)
                    if progress and int(progress.group(1)) > 0:
                        wrote_frame = True
                    elif hw_decode_active and not wrote_frame and GPU_CANT_DECODE_VERDICT in line:
                        gpu_cant_decode = True
                if new_lines:
                    last_progress_time = time.time()
                if active_timeout_s is not None and os.path.getsize(output_file) > _CHAPTER_STDERR_LIMIT:
                    logger.warning("FFmpeg exceeded its chapter diagnostic output limit for {}", video_file)
                    stalled = True
                    proc.kill()
                    reap_killed_process()
                    remember(DIAGNOSTIC_LIMIT_LINE)
                    break
                if gpu_cant_decode:
                    # One verdict before any frame is enough: all 68 failed production runs that printed it wrote no
                    # frame, and FFmpeg itself ends a run at the first such failure for a decoder with a software path.
                    logger.info(
                        "The GPU can't decode the video codec of {} — stopping the GPU run now instead of letting "
                        "FFmpeg fail on every frame to the end of the file.",
                        video_file,
                    )
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    ffmpeg_output_lines.append(GPU_CANT_DECODE_LINE)
                    break
                if time.time() - last_progress_time > FFMPEG_STALL_TIMEOUT_SEC:
                    logger.warning(
                        "FFmpeg stopped making progress for {}s while processing {} — killing it. "
                        "This usually means a slow disk, a stuck hardware decoder, or a damaged file. "
                        "Other files in the queue will keep processing; this one will be marked failed. "
                        "If it happens often on the same file, try toggling hardware acceleration off "
                        "in {}.",
                        FFMPEG_STALL_TIMEOUT_SEC,
                        video_file,
                        gpu_settings_hint(),
                    )
                    stalled = True
                    proc.kill()
                    reap_killed_process()
                    remember(STALL_WATCHDOG_LINE)
                    break
                time.sleep(0.05)

            # Process any remaining data
            try:
                for line in read_new_stderr(process_exited=True):
                    remember(line)
                    total_duration = parse_ffmpeg_progress_line(line, total_duration, speed_capture_callback)
            except OSError:
                pass
        except BaseException as exc:
            # A raising callback must not leave FFmpeg running (or SIGSTOPped by a pause).
            # CancellationError is raised only after the loop has already stopped the process.
            if not isinstance(exc, CancellationError) and proc is not None and proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            raise
        finally:
            # Ensure stderr file handle is always closed
            stderr_fh.close()
            if stderr_reader is not None:
                stderr_reader.close()
            try:
                os.remove(output_file)
            except OSError:
                pass

        if active_timeout_s is not None:
            ffmpeg_output_lines = sticky_diagnostics + [
                line for line in ffmpeg_output_lines if line not in sticky_diagnostics
            ]
            if stalled or (
                proc.returncode == 0
                and (DIAGNOSTIC_LIMIT_LINE in ffmpeg_output_lines or has_hevc_parameter_failure(ffmpeg_output_lines))
            ):
                # Controller stops still need their bounded decoder evidence.
                # Keep the stop marker so SIGKILL is not mistaken for an OOM.
                _save_ffmpeg_failure_log(video_file, proc.returncode, _without_metadata_tags(ffmpeg_output_lines))
        # A chapter seek past the last video frame fails to open the encoder ("No filtered frames"). That is
        # not a crash: the chapter caller retries just before the end or reports the chapter itself.
        chapter_without_frame = (
            chapter_start_ms is not None
            and proc.returncode != 0
            and any("No filtered frames" in line for line in ffmpeg_output_lines)
            and not any("File ended prematurely" in line for line in ffmpeg_output_lines)
        )
        if chapter_without_frame and not stalled:
            logger.info(
                "No video frame at {:.3f}s in {} (exit code {}); the chapter step handles it.",
                cast(int, chapter_start_ms) / 1000,
                video_file,
                proc.returncode,
            )
        # A normal hand-off to the CPU, the same on every try: no error lines, stderr dump or failure log.
        gpu_lacks_decoder = not stalled and _gpu_lacks_the_decoder(
            proc.returncode, ffmpeg_output_lines, hwaccel_in(args), ffmpeg_output_lines
        )
        if gpu_lacks_decoder:
            logger.info(
                "The GPU can't decode the video of {} (FFmpeg exit code {}); another GPU or the CPU makes its "
                "thumbnails instead.",
                video_file,
                proc.returncode,
            )
        # A normal rerun too: the caller reads the video again without its SEI, so this run is no failure yet.
        side_data_twice = (
            not stalled and not read_without_sei and h264_side_data_twice(proc.returncode, ffmpeg_output_lines)
        )
        if side_data_twice:
            logger.info(
                "{} is a 3D video FFmpeg can't read as it is: its 3D layout is stored twice (FFmpeg exit code {}). "
                "Reading it again without the video stream's copy, decoding every frame.",
                video_file,
                proc.returncode,
            )
        # Error logging (skip generic failure log when we stopped FFmpeg ourselves; already logged above)
        if (
            proc.returncode != 0
            and not stalled
            and not gpu_cant_decode
            and not gpu_lacks_decoder
            and not side_data_twice
            and not chapter_without_frame
        ):
            exit_diagnosis = _diagnose_ffmpeg_exit_code(proc.returncode, ffmpeg_output_lines, output_folder)
            no_decoder = exit_diagnosis == "no_decoder"
            refused = _folder_write_errno(proc.returncode, ffmpeg_output_lines, output_folder)
            # A node hands such a file back to the queue: its temp folder is the node's state, which its card and bell
            # say, not a failed file, so one warning and no stderr lines. Its own rule, so the two can't disagree.
            handed_back = False
            if refused is not None and is_node_mode():
                from ..nodes.server.tasks import blames_temp_folder

                handed_back = blames_temp_folder(node_temp_root(), refused, output_folder)
            if refused is not None:
                # The folder's, not the file's or the GPU's: no hardware or codec advice.
                folder, move = working_folder_hint(getattr(config, "tmp_folder", None))
                fix = folder_write_fix(refused)
                (logger.warning if handed_back else logger.error)(
                    "FFmpeg couldn't write thumbnails for {} to {} {}: {} (exit code {}). None of them are kept. "
                    "{}, or {}. Other files in the queue will keep processing.",
                    video_file,
                    folder,
                    output_folder,
                    _FOLDER_WRITE_TEXT[refused][0],
                    proc.returncode,
                    fix,
                    move,
                )
            elif no_decoder:
                # The file's only failure line: the caller neither reruns it nor logs it again.
                logger.error(
                    "This file's video can't be decoded by any device (an unknown or protected codec, such as an "
                    "encrypted track), so it is skipped and not retried on the CPU: {}. "
                    "Other files in the queue will keep processing.",
                    video_file,
                )
            else:
                per_file_logger.info(
                    "FFmpeg failed while extracting frames from {} (exit code {}: {}). "
                    "See the FFmpeg stderr lines logged below — they usually point at the cause "
                    "(unsupported codec, broken hardware acceleration, corrupted file, full disk). "
                    "Other files in the queue will keep processing. "
                    "If this happens often on the same file, try toggling hardware acceleration off "
                    "in {}.",
                    video_file,
                    proc.returncode,
                    exit_diagnosis,
                    gpu_settings_hint(),
                )

            # How this run ended and its last stderr lines go to the job log step by step (the app log shows them at
            # DEBUG only): the app log's one ERROR for a failed file comes from the caller that knows the final outcome.
            # A full quota's exit code (134) is also a wrapper shell's SIGABRT: the write failure was said above.
            crash = _crash_signal(proc.returncode) if refused is None else None
            if crash is not None:
                per_file_logger.info(
                    "FFmpeg crashed on {} ({}, exit code {}). A crash like this comes from the file itself (damaged "
                    "data) or a bug in FFmpeg or the GPU driver, and happens at the same place on every try; the "
                    "stderr lines below usually show where. Other files in the queue will keep processing.",
                    video_file,
                    crash.name,
                    proc.returncode,
                )
            elif _is_signal_killed(proc.returncode) and _signal_name(proc.returncode) == "SIGKILL":
                per_file_logger.info(
                    "FFmpeg was killed with SIGKILL (exit code {}) while processing {}. This is usually the "
                    "out-of-memory killer: the system or the container ran out of memory. Check container memory "
                    "limits and free RAM; if nothing was short of memory, someone force-stopped the process. "
                    "Other files in the queue will keep processing.",
                    proc.returncode,
                    video_file,
                )
            elif _is_signal_killed(proc.returncode):
                per_file_logger.info(
                    "FFmpeg was stopped by {} (exit code {}) while processing {}: something outside this file "
                    "stopped it, such as the container stopping or restarting, or someone ending the process. "
                    "Other files in the queue will keep processing.",
                    _signal_name(proc.returncode),
                    proc.returncode,
                    video_file,
                )
            elif refused is None and _io_cut_off(proc.returncode, ffmpeg_output_lines):
                # Exit 251 with a GPU error in the stderr is the GPU's, not a disk's: the stderr lines below say so.
                # One the folder refused (it couldn't create a frame file) was said above.
                folder, _move = working_folder_hint()
                per_file_logger.info(
                    "FFmpeg hit an I/O error on {}: reading the video from its disk or network share, or writing "
                    "thumbnails to {} ({}, exists: {}), failed. Check that disk or share, and that "
                    "{} isn't full and is writable. "
                    "Other files in the queue will keep processing.",
                    video_file,
                    folder,
                    output_folder,
                    os.path.isdir(output_folder),
                    folder,
                )
            elif exit_diagnosis == "high_exit_non_signal":
                per_file_logger.info(
                    "FFmpeg crashed with an unusual exit code ({}) while processing {}. "
                    "This usually points to an internal FFmpeg bug, a broken hardware acceleration "
                    "driver, or a malformed video file. The stderr lines below should explain. "
                    "Other files in the queue will keep processing.",
                    proc.returncode,
                    video_file,
                )
            if no_decoder:
                # Such a run ends on option warnings and "Error opening …"; the lines naming the video say what it is.
                about_video = [
                    line
                    for line in _without_metadata_tags(ffmpeg_output_lines)
                    if "Video: " in line or NO_DECODER_PHRASE in line
                ]
                tail = about_video[-5:]
                per_file_logger.info("FFmpeg's last {} stderr lines about the video in {}:", len(tail), video_file)
                for line in tail:
                    per_file_logger.info("  {}", line)
            elif ffmpeg_output_lines and not handed_back:
                tail = _without_metadata_tags(ffmpeg_output_lines)[-5:]
                per_file_logger.info(
                    "FFmpeg's last {} stderr lines for {} (these usually identify the cause):",
                    len(tail),
                    video_file,
                )
                for line in tail:
                    per_file_logger.info("  {}", line)

            # Check for permission-related errors in FFmpeg output
            # FFmpeg outputs "Permission denied" in messages like "av_interleaved_write_frame(): Permission denied"
            # We use lowercase for case-insensitive matching
            permission_keywords = ["permission denied", "access denied"]
            permission_errors = []
            for line in ffmpeg_output_lines:
                line_lower = line.lower()
                for keyword in permission_keywords:
                    if keyword in line_lower:
                        permission_errors.append(line.strip())
                        break

            # Log permission errors at INFO level so users can see them without DEBUG
            if permission_errors:
                logger.info("Permission error detected while processing {}:", video_file)
                for error_line in permission_errors[:3]:  # Show up to 3 permission error lines
                    logger.info("  {}", error_line)
                if len(permission_errors) > 3:
                    logger.info("  ... and {} more permission-related error(s)", len(permission_errors) - 3)

            # Log full FFmpeg output at DEBUG level for detailed troubleshooting.
            # When config.log_level=DEBUG, FFmpeg itself is invoked with
            # -loglevel debug (see ffmpeg_loglevel above), so these lines
            # include VAAPI / Vulkan / Mesa / libplacebo internals — exactly
            # what's needed to diagnose hwaccel and filter-graph failures.
            logger.debug("FFmpeg output ({} lines):", len(ffmpeg_output_lines))
            for i, line in enumerate(ffmpeg_output_lines):
                logger.debug("  {:3d}: {}", i + 1, line)

            # Save full FFmpeg stderr to a per-file log for post-mortem debugging
            _save_ffmpeg_failure_log(video_file, proc.returncode, ffmpeg_output_lines)

        end_local = time.time()
        seconds_local = round(end_local - start_local, 1)
        # Calculate fallback speed if needed
        if speed_local == "0.0x" and total_duration and total_duration > 0 and seconds_local > 0:
            calculated_speed = total_duration / seconds_local
            speed_local = f"{calculated_speed:.0f}x"

        returncode = (
            -signal.SIGKILL
            if proc.returncode is None and PROCESS_EXIT_PENDING_LINE in ffmpeg_output_lines
            else proc.returncode
        )
        if side_data_twice:
            read_without_sei = True
            if chapter_output is None:
                _clean_output_images(output_folder)
            return _run_ffmpeg(
                use_skip,
                gpu_override=gpu_override,
                gpu_device_path_override=gpu_device_path_override,
                vf_override=vf_override,
                init_vulkan=init_vulkan,
                disable_vaapi_dv5=disable_vaapi_dv5,
                path_kind_override=path_kind_override,
            )
        return returncode, seconds_local, speed_local, ffmpeg_output_lines

    return _run_ffmpeg
