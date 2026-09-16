"""Filesystem type lookup for the Plex-database-must-be-local rule (spec §6.3)."""

import os

import pytest

from media_preview_generator.markers.fs import filesystem_type, is_local_filesystem, is_network_filesystem

MOUNTINFO = """\
22 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw
100 22 0:50 / /config rw,relatime - zfs pool/config rw
101 22 0:51 / /config/plex rw,relatime - nfs4 plex:/config rw
102 22 0:52 / /mnt/My\\040Share rw - cifs //nas/share rw
103 22 0:53 / /mnt/user rw - fuse.shfs shfs rw
104 22 0:54 / /host_mnt rw - fakeowner /dev rw
"""


@pytest.fixture
def mountinfo(tmp_path):
    p = tmp_path / "mountinfo"
    p.write_text(MOUNTINFO)
    return str(p)


@pytest.mark.parametrize(
    ("path", "fs"),
    [
        ("/config/Library/db", "zfs"),
        ("/config/plex/Library/Application Support", "nfs4"),
        ("/config/plexish", "zfs"),  # prefix must be folder-bounded
        ("/mnt/My Share/Plex", "cifs"),
        ("/mnt/user/appdata/plex", "fuse.shfs"),
        ("/host_mnt/Users/me", "fakeowner"),
        ("/srv", "ext4"),
    ],
)
def test_longest_mount_point_wins(mountinfo, path, fs):
    assert filesystem_type(path, mountinfo_path=mountinfo) == fs


def test_missing_mountinfo_returns_none(tmp_path):
    assert filesystem_type("/x", mountinfo_path=str(tmp_path / "nope")) is None


@pytest.mark.parametrize(
    ("fs", "network"),
    [
        ("nfs", True),
        ("nfs4", True),
        ("cifs", True),
        ("smb3", True),
        ("9p", True),
        ("fuse.sshfs", True),
        ("virtiofs", True),
        ("fakeowner", True),
        ("fuse.grpcfuse", True),
        ("fuse.rclone", True),
        ("ext4", False),
        ("zfs", False),
        ("xfs", False),
        ("btrfs", False),
        ("fuse.shfs", False),
        ("overlay", False),
        (None, False),
    ],
)
def test_network_matrix(fs, network):
    assert is_network_filesystem(fs) is network


# Stacks: an overmount's parent id is the mount it covers (same mount point). A mount attached inside a covered mount
# is hidden too (122 covers 120, so 121 on /lower/child is no longer reachable).
STACKED = """\
22 1 259:2 / / rw - ext4 /dev/root rw
100 22 0:50 / /config rw - ext4 /dev/sda1 rw
101 100 0:51 / /config rw - nfs4 nas:/config rw
110 22 0:60 / /auto rw - autofs systemd-1 rw
111 110 0:61 / /auto rw - nfs4 nas:/auto rw
120 22 0:70 / /lower rw - ext4 /dev/sdb1 rw
121 120 0:71 / /lower/child rw - ext4 /dev/sdc1 rw
122 120 0:72 / /lower rw - nfs4 nas:/lower rw
130 22 0:80 / /two rw - ext4 /dev/sdd1 rw
131 130 0:81 / /two rw - xfs /dev/sde1 rw
132 131 0:82 / /two rw - cifs //nas/two rw
140 22 0:90 / /plain rw - xfs /dev/sdf1 rw
"""


@pytest.mark.parametrize(
    ("path", "fs"),
    [
        ("/config/Library/db", "nfs4"),
        ("/auto/plex", "nfs4"),
        ("/lower/child/db", "nfs4"),
        ("/lower/other", "nfs4"),
        ("/two/db", "cifs"),
        ("/plain/db", "xfs"),
        ("/srv", "ext4"),
    ],
    ids=[
        "ext4-then-nfs4",
        "autofs-then-nfs4",
        "child-of-covered-mount",
        "covered-root",
        "three-deep",
        "no-stack",
        "root",
    ],
)
def test_top_mount_of_a_stack_wins(tmp_path, path, fs):
    p = tmp_path / "mountinfo"
    p.write_text(STACKED)
    assert filesystem_type(path, mountinfo_path=str(p)) == fs


def test_mount_points_that_are_not_utf8_are_matched_and_never_raise(tmp_path):
    p = tmp_path / "mountinfo"
    p.write_bytes(b"22 1 259:2 / / rw - ext4 /dev/root rw\n150 22 0:91 / /mnt/caf\xe9 rw - nfs4 nas:/x rw\n")
    assert filesystem_type(os.fsdecode(b"/mnt/caf\xe9/plex"), mountinfo_path=str(p)) == "nfs4"
    assert filesystem_type("/srv", mountinfo_path=str(p)) == "ext4"


@pytest.mark.parametrize(
    ("fs", "local"),
    [
        ("ext4", True),
        ("xfs", True),
        ("btrfs", True),
        ("zfs", True),
        ("f2fs", True),
        ("bcachefs", True),
        ("overlay", True),
        ("tmpfs", True),
        ("fuse.shfs", True),
        ("fuse.mergerfs", True),
        ("nfs4", False),
        ("cifs", False),
        ("fakeowner", False),
        ("autofs", False),
        ("vboxsf", False),
        ("drvfs", False),
        ("prl_fs", False),
        ("fuse.vmhgfs-fuse", False),
        ("fuse.gcsfuse", False),
        ("beegfs", False),
        ("gpfs", False),
        ("", False),
        (None, False),
    ],
)
def test_only_known_local_filesystems_are_local(fs, local):
    # Anything unexpected stops writes: unlisted types count as not local.
    assert is_local_filesystem(fs) is local


def test_same_mount_point_without_a_parent_chain_later_line_wins(tmp_path):
    # Parents outside this mount namespace can't be chained; the kernel lists the top mount later.
    p = tmp_path / "mountinfo"
    p.write_text("200 900 0:1 / /orphan rw - ext4 /dev/a rw\n201 901 0:2 / /orphan rw - nfs4 nas:/o rw\n")
    assert filesystem_type("/orphan/db", mountinfo_path=str(p)) == "nfs4"
