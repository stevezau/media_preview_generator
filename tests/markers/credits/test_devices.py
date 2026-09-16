"""Worker device → PCI address → WebGPU EP device (spec §6.4 item 7, T-R3)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from media_preview_generator.markers.credits import devices


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00000000:02:00.0", "0000:02:00.0"),
        ("0000:0A:1F.3", "0000:0a:1f.3"),
        ("02:00.0", "0000:02:00.0"),
        ("0001:65:00.1", "0001:65:00.1"),
        ("", None),
        (None, None),
        ("pci-0000:02:00.0", None),
        ("0000:02:00.8", None),
    ],
)
def test_normalise_pci_bus_id(value, expected):
    assert devices.normalise_pci_bus_id(value) == expected


class TestWorkerPci:
    def setup_method(self):
        devices.nvidia_pci_bus_ids.cache_clear()

    def teardown_method(self):
        devices.nvidia_pci_bus_ids.cache_clear()

    def _smi(self, stdout, returncode=0):
        return patch.object(
            devices.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout=stdout)
        )

    def test_nvidia_index_maps_through_nvidia_smi(self):
        with self._smi("0, 00000000:02:00.0\n1, 00000000:65:00.0\n") as run:
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:1") == "0000:65:00.0"
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") == "0000:02:00.0"
        assert run.call_args.args[0] == ["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"]
        # This runs inside the pool's per-device lock, so a wedged nvidia-smi would park that worker thread and
        # queue every other worker on the same GPU behind it.
        assert run.call_args.kwargs["timeout"] == 5
        assert run.call_args.kwargs["capture_output"] is True and run.call_args.kwargs["text"] is True
        assert run.call_count == 1  # cached for the process

    @pytest.mark.parametrize(
        ("stdout", "expected"),
        [("0, 00000000:02:00.0\n", "0000:02:00.0"), ("0, 00000000:02:00.0\n1, 00000000:65:00.0\n", None)],
    )
    def test_nvidia_without_an_index_is_known_only_on_a_one_gpu_host(self, stdout, expected):
        with self._smi(stdout):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda") == expected

    def test_nvidia_smi_failing_gives_none(self):
        with patch.object(devices.subprocess, "run", side_effect=OSError("nope")):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") is None

    def test_nvidia_smi_exiting_non_zero_gives_none(self):
        with self._smi("0, 00000000:02:00.0\n", returncode=9):
            assert devices.worker_pci_bus_id("NVIDIA", "cuda:0") is None

    @pytest.mark.parametrize("address", ["0000:00:02.0", "0000:0a:00.0", "0000:c1:00.0"])
    def test_render_node_maps_through_sysfs_with_hex_buses(self, tmp_path, monkeypatch, address):
        device = tmp_path / "devices" / "pci0000:00" / "0000:00:01.1" / address
        device.mkdir(parents=True)
        node = tmp_path / "drm" / "renderD129"
        node.mkdir(parents=True)
        (node / "device").symlink_to(device)
        monkeypatch.setattr(devices, "SYSFS_DRM", str(tmp_path / "drm"))
        assert devices.worker_pci_bus_id("AMD", "/dev/dri/renderD129") == address
        assert devices.worker_pci_bus_id("INTEL", "/dev/dri/renderD128") is None  # no such node in the fake tree

    @pytest.mark.parametrize(("gpu", "path"), [(None, None), ("APPLE", "videotoolbox"), ("WINDOWS_GPU", "d3d11va")])
    def test_others_have_none(self, gpu, path):
        assert devices.worker_pci_bus_id(gpu, path) is None


# Storage's EP list, measured 2026-09-16: the P5000 and the board's ASPEED BMC VGA.
P5000 = {"Discrete": "1", "card_idx": "0", "pci_bus_id": "0000:02:00.0"}
BMC = {"card_idx": "1", "pci_bus_id": "0000:07:00.0"}
IGPU = {"card_idx": "0", "pci_bus_id": "0000:00:02.0"}


@pytest.mark.parametrize(
    ("metadatas", "pci", "expected"),
    [
        ([], "0000:02:00.0", None),
        ([P5000, BMC], "0000:02:00.0", 0),  # storage
        ([BMC, P5000], "0000:02:00.0", 1),
        ([P5000, IGPU], "0000:00:02.0", 1),  # plex: the Intel worker's helper
        ([P5000, BMC], "0000:65:00.0", None),  # no device is this worker's GPU
        ([P5000, BMC], None, None),  # address unknown, several devices
        ([P5000], None, 0),  # address unknown, one device
        ([P5000], "0000:65:00.0", None),  # the only device is another GPU
        ([{}], "0000:02:00.0", 0),  # one device without an address
        ([{}, {}], "0000:02:00.0", None),
        ([{"pci_bus_id": "0000:02:00.0"}, {"pci_bus_id": "0000:02:00.0"}], "0000:02:00.0", None),
    ],
)
def test_choose_ep_device(metadatas, pci, expected):
    assert devices.choose_ep_device(metadatas, pci) == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [(P5000, "0000:02:00.0"), ({}, None), ({"pci_bus_id": "not a bus id"}, None)],
)
def test_ep_device_pci_bus_id(metadata, expected):
    assert devices.ep_device_pci_bus_id(metadata) == expected


def test_a_render_node_whose_sysfs_path_holds_no_address_gives_none(tmp_path, monkeypatch):
    device = tmp_path / "devices" / "platform" / "gpu"  # an SoC GPU: no PCI address anywhere in the path
    device.mkdir(parents=True)
    node = tmp_path / "drm" / "renderD128"
    node.mkdir(parents=True)
    (node / "device").symlink_to(device)
    monkeypatch.setattr(devices, "SYSFS_DRM", str(tmp_path / "drm"))
    assert devices.drm_pci_bus_id("/dev/dri/renderD128") is None


@pytest.mark.parametrize(
    ("pci_bus_id", "expected"),
    [
        ("0000:02:00.0", "pci-0000_02_00_0"),
        ("0001:0a:1f.3", "pci-0001_0a_1f_3"),
        ("02:00.0", "pci-0000_02_00_0"),
        ("00000000:65:00.0", "pci-0000_65_00_0"),
        ("not a bus id", None),
        (None, None),
    ],
)
def test_dri_prime_tag(pci_bus_id, expected):
    assert devices.dri_prime_tag(pci_bus_id) == expected


class TestPinEnvToGpu:
    """The only thing that puts a helper process on one physical GPU (controller note N1)."""

    def test_an_address_sets_the_tag_and_the_force_flag(self):
        pinned = devices.pin_env_to_gpu({"PATH": "/usr/bin"}, "0000:65:00.0")
        assert pinned == {
            "PATH": "/usr/bin",
            "DRI_PRIME": "pci-0000_65_00_0",
            "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1",
        }

    def test_two_addresses_give_two_different_pins(self):
        first = devices.pin_env_to_gpu({}, "0000:02:00.0")
        second = devices.pin_env_to_gpu({}, "0000:65:00.0")
        assert first["DRI_PRIME"] != second["DRI_PRIME"]

    @pytest.mark.parametrize("pci_bus_id", ["0000:02:00.0", None])
    def test_an_inherited_selection_is_always_dropped(self, pci_bus_id):
        inherited = {
            "MESA_VK_DEVICE_SELECT": "10005:0",
            "NODEVICE_SELECT": "1",
            "DRI_PRIME": "pci-0000_99_00_0",
            "MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE": "1",
            "KEEP": "me",
        }
        pinned = devices.pin_env_to_gpu(inherited, pci_bus_id)
        assert "MESA_VK_DEVICE_SELECT" not in pinned and "NODEVICE_SELECT" not in pinned
        assert pinned["KEEP"] == "me"
        assert pinned.get("DRI_PRIME") == (None if pci_bus_id is None else "pci-0000_02_00_0")
        assert pinned.get("MESA_VK_DEVICE_SELECT_FORCE_DEFAULT_DEVICE") == (None if pci_bus_id is None else "1")

    def test_the_caller_s_environment_is_left_alone(self):
        original = {"MESA_VK_DEVICE_SELECT": "10005:0"}
        devices.pin_env_to_gpu(original, "0000:02:00.0")
        assert original == {"MESA_VK_DEVICE_SELECT": "10005:0"}
