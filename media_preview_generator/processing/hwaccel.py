"""FFmpeg hardware-decode arguments for a worker's GPU, shared by preview thumbnails and Intro & Credits frame decoding.

The Dolby Vision Profile 5 device-init paths (VAAPI → OpenCL / Vulkan) stay in ``ffmpeg_runner``: only previews use
them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HwDecode:
    """Input-side hwaccel arguments.

    Attributes:
        args: Tokens that go before ``-i``.
        active: Whether decode runs on the GPU.
    """

    args: tuple[str, ...]
    active: bool


def hwaccel_decode_args(gpu: str | None, gpu_device_path: str | None, *, keep_on_gpu: bool) -> HwDecode:
    """The ``-hwaccel`` arguments for a worker's GPU.

    Args:
        gpu: The worker's GPU type (``NVIDIA``, ``INTEL``, ``AMD``, ``WINDOWS_GPU``, ``APPLE``…), None on a CPU worker.
        gpu_device_path: ``cuda:<index>`` for NVIDIA, a ``/dev/dri`` render node for VAAPI GPUs.
        keep_on_gpu: Keep decoded frames as GPU surfaces for the filter graph: a GPU scale filter (``scale_cuda`` /
            ``scale_vaapi``) shrinks them before download, or ``hwdownload`` takes them whole after ``fps`` has picked
            the ones kept (Intro & Credits' frames, ``markers.credits.frames``).

    Returns:
        The arguments and whether decode runs on the GPU; no arguments for a CPU worker or a GPU without a usable
        device.
    """
    if gpu is None:
        return HwDecode((), False)
    if gpu == "NVIDIA":
        args = ["-hwaccel", "cuda"]
        # Multi-GPU hosts register each NVIDIA card as cuda:<index>; without -hwaccel_device work lands on GPU 0
        # (issue #221).
        if gpu_device_path and gpu_device_path.startswith("cuda:"):
            index = gpu_device_path.split(":", 1)[1]
            if index:
                args += ["-hwaccel_device", index]
        if keep_on_gpu:
            # Without it ffmpeg silently downloads every full-size frame to host RAM (~990 MB RSS per worker on 4K
            # HDR10, issue #218).
            args += ["-hwaccel_output_format", "cuda"]
        return HwDecode(tuple(args), True)
    if gpu == "WINDOWS_GPU":
        return HwDecode(("-hwaccel", "d3d11va"), True)
    if gpu == "APPLE":
        return HwDecode(("-hwaccel", "videotoolbox"), True)
    if gpu_device_path and gpu_device_path.startswith("/dev/dri/"):
        # -hwaccel_device (not the deprecated -vaapi_device) pairs with -hwaccel_output_format vaapi (issue #218).
        args = ["-hwaccel", "vaapi", "-hwaccel_device", gpu_device_path]
        if keep_on_gpu:
            args += ["-hwaccel_output_format", "vaapi"]
        return HwDecode(tuple(args), True)
    return HwDecode((), False)
