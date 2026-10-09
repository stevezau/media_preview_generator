"""
GPU tests designed for CI environments without GPU hardware.
These tests use mocking to verify GPU detection logic without requiring actual hardware.
"""

from media_preview_generator.gpu import format_gpu_info


class TestGPUFormattingCI:
    """Test GPU information formatting in CI."""

    def test_format_gpu_info_nvidia_without_acceleration_defaults_to_cuda(self):
        """Omitting the acceleration argument still resolves NVIDIA to CUDA."""
        assert format_gpu_info("NVIDIA", "0", "NVIDIA GeForce RTX 3080") == "NVIDIA GeForce RTX 3080 (CUDA)"

    def test_format_gpu_info_nvidia(self):
        """NVIDIA + CUDA renders as ``<name> (CUDA)``."""
        info = format_gpu_info("NVIDIA", "cuda", "NVIDIA GeForce RTX 3080", "CUDA")
        assert info == "NVIDIA GeForce RTX 3080 (CUDA)"

    def test_format_gpu_info_amd(self):
        """AMD + VAAPI on a DRM render node renders as ``<name> (VAAPI - <device>)``."""
        info = format_gpu_info("AMD", "/dev/dri/renderD128", "AMD Radeon RX 6800 XT", "VAAPI")
        assert info == "AMD Radeon RX 6800 XT (VAAPI - /dev/dri/renderD128)"

    def test_format_gpu_info_intel(self):
        """Intel + VAAPI on a DRM render node renders as ``<name> (VAAPI - <device>)``."""
        info = format_gpu_info("INTEL", "/dev/dri/renderD128", "Intel UHD Graphics 770", "VAAPI")
        assert info == "Intel UHD Graphics 770 (VAAPI - /dev/dri/renderD128)"
