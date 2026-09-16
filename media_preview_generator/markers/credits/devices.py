"""Which physical GPU a worker's device is, and which WebGPU EP device matches it (spec §6.4 item 7)."""

from __future__ import annotations

import functools
import os
import re
import subprocess
from collections.abc import Mapping, Sequence

_PCI_RE = re.compile(r"^(?:([0-9a-fA-F]{1,8}):)?([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$")
# The metadata key onnxruntime-ep-webgpu 0.3.0 puts a device's PCI address under (measured on storage, 2026-09-16).
PCI_METADATA_KEYS: tuple[str, ...] = ("pci_bus_id",)
SYSFS_DRM = "/sys/class/drm"

# Which physical GPU a Vulkan process runs on is decided by Mesa's device-select implicit layer
# (mesa-vulkan-drivers, in the app image), which the Vulkan loader applies to every ICD including NVIDIA's
# proprietary one. Measured on storage 2026-09-16, see
# docs/design/intro-credits/evidence/credits/bench/t5-gpu-pinning.txt:
# DRI_PRIME's PCI tag plus MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE moved Dawn between the P5000 and llvmpipe
# (12.2 vs 156.1 ms per frame); without the force flag, or with the layer off (NODEVICE_SELECT=1), nothing moved.
DRI_PRIME_ENV = "DRI_PRIME"
FORCE_DEFAULT_DEVICE_ENV = "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE"
DEVICE_SELECT_ENV = "MESA_VK_DEVICE_SELECT"
DISABLE_DEVICE_SELECT_ENV = "NODEVICE_SELECT"


def normalise_pci_bus_id(value: str | None) -> str | None:
    """A PCI address in one spelling: ``0000:02:00.0`` (domain optional on input, lower-case hex).

    Args:
        value: ``00000000:02:00.0`` (nvidia-smi), ``0000:02:00.0`` (sysfs) or ``02:00.0``.

    Returns:
        The normalised address, or None when the value isn't one.
    """
    if not value:
        return None
    match = _PCI_RE.match(value.strip())
    if match is None:
        return None
    domain, bus, device, function = match.groups()
    return f"{int(domain or '0', 16):04x}:{bus.lower()}:{device.lower()}.{function}"


def drm_pci_bus_id(render_node: str, *, sysfs_drm: str | None = None) -> str | None:
    """The PCI address of a ``/dev/dri`` render node, from its sysfs device link.

    Resolved here rather than with ``gpu.enumeration``'s helper, whose pattern takes decimal digits only and misses
    buses such as ``0a:00.0`` or ``c1:00.0``.

    Args:
        render_node: ``/dev/dri/renderD128``.
        sysfs_drm: The sysfs DRM class folder (default ``SYSFS_DRM``).

    Returns:
        The normalised address, or None when sysfs doesn't say.
    """
    try:
        link = os.path.join(sysfs_drm or SYSFS_DRM, os.path.basename(render_node), "device")
        real = os.path.realpath(link, strict=True)
    except OSError:
        return None
    for part in reversed(real.split(os.sep)):
        found = normalise_pci_bus_id(part)
        if found:
            return found
    return None


@functools.lru_cache(maxsize=1)
def nvidia_pci_bus_ids() -> dict[str, str]:
    """NVIDIA GPU index → PCI address from nvidia-smi, read once per process ({} when it can't answer)."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        pci = normalise_pci_bus_id(parts[1]) if len(parts) == 2 else None
        if pci:
            found[parts[0]] = pci
    return found


def worker_pci_bus_id(gpu: str | None, gpu_device_path: str | None) -> str | None:
    """The PCI address of a worker's GPU.

    Args:
        gpu: The worker's GPU type.
        gpu_device_path: ``cuda:<index>`` or a ``/dev/dri`` render node.

    Returns:
        The address, or None when it can't be told (a CPU worker, an NVIDIA device without an index on a multi-GPU
        host, nvidia-smi or sysfs not answering).
    """
    if gpu == "NVIDIA":
        ids = nvidia_pci_bus_ids()
        index = gpu_device_path.split(":", 1)[1] if gpu_device_path and gpu_device_path.startswith("cuda:") else ""
        if index:
            return ids.get(index)
        return next(iter(ids.values())) if len(ids) == 1 else None
    if gpu_device_path and gpu_device_path.startswith("/dev/dri/"):
        return drm_pci_bus_id(gpu_device_path)
    return None


def ep_device_pci_bus_id(metadata: Mapping[str, str]) -> str | None:
    """The PCI address an EP device's metadata carries, normalised (None when it carries none)."""
    for key in PCI_METADATA_KEYS:
        found = normalise_pci_bus_id(metadata.get(key))
        if found:
            return found
    return None


def choose_ep_device(metadatas: Sequence[Mapping[str, str]], pci_bus_id: str | None) -> int | None:
    """Which of the WebGPU EP's devices to run on for a worker's GPU (T-R3).

    The EP lists every display PCI device from sysfs, Vulkan-capable or not (a server's BMC VGA too), so the worker's
    PCI address picks the device; several devices are normal.

    Args:
        metadatas: Each EP device's metadata, in the EP's order.
        pci_bus_id: The worker GPU's PCI address (``worker_pci_bus_id``), or None when unknown.

    Returns:
        The device's index; None means "use the CPU": no device, no device with the worker's address, or an unknown
        address on a host with several devices.
    """
    if not metadatas:
        return None
    if pci_bus_id is not None:
        matches = [i for i, metadata in enumerate(metadatas) if ep_device_pci_bus_id(metadata) == pci_bus_id]
        if len(matches) == 1:
            return matches[0]
        if matches or any(ep_device_pci_bus_id(metadata) is not None for metadata in metadatas):
            return None
    return 0 if len(metadatas) == 1 else None


def dri_prime_tag(pci_bus_id: str | None) -> str | None:
    """The device-select layer's tag for a PCI address: ``0000:02:00.0`` becomes ``pci-0000_02_00_0``.

    A PCI address is the only selector that tells two identical GPUs apart; the layer's other form,
    ``MESA_VK_DEVICE_SELECT=<vendor>:<device>``, cannot (and its ``pci:`` spelling is not parsed by Mesa 25.2).

    Args:
        pci_bus_id: A normalised address from :func:`worker_pci_bus_id`, or None.

    Returns:
        The tag, or None when there is no address to pin to.
    """
    found = normalise_pci_bus_id(pci_bus_id)
    if found is None:
        return None
    domain, bus, rest = found.split(":")
    device, function = rest.split(".")
    return f"pci-{domain}_{bus}_{device}_{function}"


def pin_env_to_gpu(env: Mapping[str, str], pci_bus_id: str | None) -> dict[str, str]:
    """A copy of ``env`` whose Vulkan adapter is pinned to the GPU at ``pci_bus_id``.

    Passing a WebGPU EP device object does not choose the GPU (controller note N1): Dawn takes whichever adapter the
    process's Vulkan environment leaves it. Only an environment set before the process starts moves the work, so a
    helper per GPU needs its own.

    Two inherited variables would undo the pin and are dropped: ``MESA_VK_DEVICE_SELECT`` outranks the tag, and
    ``NODEVICE_SELECT`` turns the layer off altogether.

    Args:
        env: The environment the helper would otherwise inherit.
        pci_bus_id: The worker GPU's PCI address, or None when it can't be told.

    Returns:
        The helper's environment. With no address it is only scrubbed: an inherited pin for another GPU is worse
        than no pin at all.
    """
    pinned = dict(env)
    for name in (DEVICE_SELECT_ENV, DISABLE_DEVICE_SELECT_ENV):
        pinned.pop(name, None)
    tag = dri_prime_tag(pci_bus_id)
    if tag is None:
        pinned.pop(DRI_PRIME_ENV, None)
        pinned.pop(FORCE_DEFAULT_DEVICE_ENV, None)
        return pinned
    pinned[DRI_PRIME_ENV] = tag
    # Without this the layer only sorts the device list and Dawn still picks its own favourite.
    pinned[FORCE_DEFAULT_DEVICE_ENV] = "1"
    return pinned
