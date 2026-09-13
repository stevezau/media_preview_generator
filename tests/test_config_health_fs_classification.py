"""Filesystem classification for the config-health advisory.

The first cut treated *every* FUSE mount as a network share:

    if fstype in _NETWORK_FS_TYPES or fstype.startswith("fuse"):

That is wrong for the two most common self-hosted setups:

* ``fuse.shfs``     — Unraid user shares. A FUSE union over **local** disks, so
  calling it a "network filesystem" is simply false. Running SQLite on
  ``/mnt/user`` *is* discouraged on Unraid, so it still warrants an advisory —
  just an accurate, actionable one that names the real fix (``/mnt/cache``).
* ``fuse.mergerfs`` — a local union. No network, no SQLite risk, no warning.

Genuinely network-backed FUSE (sshfs, rclone, glusterfs, s3fs) keeps the
original "network share" warning, because there the locking risk is real.
"""

import pytest

from media_preview_generator.web.config_health import _classify_fs


class TestNetworkFilesystems:
    """Real network mounts — SQLite's POSIX locking is genuinely unreliable."""

    @pytest.mark.parametrize("fstype", ["nfs", "nfs4", "cifs", "smbfs", "smb3", "ncpfs", "9p"])
    def test_kernel_network_filesystems_are_network(self, fstype):
        assert _classify_fs(fstype) == "network"

    @pytest.mark.parametrize(
        "fstype",
        ["fuse.sshfs", "fuse.rclone", "fuse.glusterfs", "fuse.s3fs", "fuse.davfs", "fuse.ceph-fuse"],
    )
    def test_network_backed_fuse_is_network(self, fstype):
        assert _classify_fs(fstype) == "network"


class TestUnraidUserShare:
    """Unraid's shfs is LOCAL — it must not be reported as a network share."""

    def test_shfs_is_classified_as_unraid_share_not_network(self):
        assert _classify_fs("fuse.shfs") == "unraid_share"

    def test_shfs_is_not_network(self):
        # The regression: Unraid users were told they were on a network share.
        assert _classify_fs("fuse.shfs") != "network"


class TestLocalFilesystemsAreQuiet:
    """Local filesystems — including local union FUSE — get no advisory."""

    @pytest.mark.parametrize("fstype", ["ext4", "xfs", "btrfs", "zfs", "overlay", "tmpfs"])
    def test_plain_local_filesystems_are_quiet(self, fstype):
        assert _classify_fs(fstype) is None

    def test_mergerfs_is_quiet(self):
        # A local union. Warning about it is pure noise — and the maintainer's
        # own /data is mergerfs.
        assert _classify_fs("fuse.mergerfs") is None

    @pytest.mark.parametrize("fstype", ["fuse", "fusectl", "fuse.portal", "fuse.gvfsd-fuse"])
    def test_bare_and_kernel_fuse_are_quiet(self, fstype):
        # The old blanket startswith("fuse") even matched fusectl, the kernel
        # control filesystem. Pin the decision so it can't regress.
        assert _classify_fs(fstype) is None

    @pytest.mark.parametrize("fstype", ["fuse.juicefs", "fuse.gcsfuse", "fuse.blobfuse", "fuse.seaweedfs"])
    def test_cloud_backed_fuse_is_network(self, fstype):
        assert _classify_fs(fstype) == "network"

    @pytest.mark.parametrize("fstype", ["ceph", "lustre", "beegfs"])
    def test_kernel_cluster_filesystems_are_network(self, fstype):
        assert _classify_fs(fstype) == "network"

    def test_unknown_fuse_is_quiet(self):
        # Default to silence: an unrecognised FUSE mount is more likely a local
        # union than a network share, and a false alarm erodes trust in the
        # warnings that matter.
        assert _classify_fs("fuse.somethingelse") is None
