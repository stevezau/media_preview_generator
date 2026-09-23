"""Filesystem type of a path, for the "Plex DB must be on this host" rule (SQLite WAL needs a local filesystem), and
whether a file is gone from disk."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import NamedTuple

# Docker Desktop shares (fakeowner, grpcfuse, virtiofs) and WSL 9p are network-backed even when they look local.
NETWORK_FS_TYPES: frozenset[str] = frozenset(
    {
        "nfs",
        "nfs4",
        "cifs",
        "smb3",
        "smbfs",
        "9p",
        "fuse.sshfs",
        "sshfs",
        "fuse.rclone",
        "virtiofs",
        "fakeowner",
        "fuse.grpcfuse",
        "ceph",
        "fuse.ceph",
        "glusterfs",
        "fuse.glusterfs",
        "afs",
        "davfs",
        "fuse.davfs",
        "fuse.s3fs",
        "lustre",
        "fuse.juicefs",
    }
)

# Local disks where SQLite's POSIX locks and shared memory work. unRAID's shfs and mergerfs pools are local but can
# show the same file under two paths (two lock domains); the publisher's lock probe catches that case.
LOCAL_FS_TYPES: frozenset[str] = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "btrfs",
        "zfs",
        "f2fs",
        "bcachefs",
        "jfs",
        "reiserfs",
        "overlay",
        "tmpfs",
        "fuse.shfs",
        "fuse.mergerfs",
    }
)


def _unescape(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\012", "\n").replace("\\134", "\\")


class _Mount(NamedTuple):
    mount_id: str
    parent_id: str
    mount_point: str
    fs_type: str | None


def _parse_mountinfo(lines: list[str]) -> list[_Mount]:
    mounts = []
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5:
            continue
        fs_type = right.split()[0] if right.split() else None
        mounts.append(_Mount(fields[0], fields[1], os.path.normpath(_unescape(fields[4])), fs_type))
    return mounts


def _visible_mounts(mounts: list[_Mount]) -> list[_Mount]:
    """Drop mounts hidden by a later mount on the same mount point, and everything attached inside them."""
    by_id = {m.mount_id: m for m in mounts}
    # An overmount's parent is the mount it covers: same mount point, parent id = covered mount's id.
    covered = {m.parent_id for m in mounts if m.parent_id in by_id and by_id[m.parent_id].mount_point == m.mount_point}

    def reachable(mount: _Mount) -> bool:
        seen = set()
        while mount.parent_id in by_id and mount.mount_id not in seen:
            seen.add(mount.mount_id)
            parent = by_id[mount.parent_id]
            if parent.mount_point != mount.mount_point and parent.mount_id in covered:
                return False
            mount = parent
        return True

    return [m for m in mounts if m.mount_id not in covered and reachable(m)]


def filesystem_type(path: str, *, mountinfo_path: str = "/proc/self/mountinfo") -> str | None:
    """Return the filesystem type of the visible mount holding ``path``, or None.

    Args:
        path: Absolute path to look up.
        mountinfo_path: The mountinfo file to read (tests pass their own).

    Returns:
        The type of the longest visible mount point that contains ``path``; None when mountinfo can't be read.
    """
    try:
        # surrogateescape: mount points are bytes; a non-UTF-8 name must still match os.fsdecode()'d paths.
        with open(mountinfo_path, encoding="utf-8", errors="surrogateescape") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    target = os.path.normpath(path)
    best_len, best_fs = -1, None
    for mount in _visible_mounts(_parse_mountinfo(lines)):
        point = mount.mount_point
        bounded = target == point or target.startswith(point.rstrip("/") + "/")
        if bounded and len(point) >= best_len:
            best_len, best_fs = len(point), mount.fs_type
    return best_fs


def is_network_filesystem(fs_type: str | None) -> bool:
    """Whether SQLite locking over this filesystem type is unsafe."""
    return bool(fs_type) and fs_type in NETWORK_FS_TYPES


def is_local_filesystem(fs_type: str | None) -> bool:
    """Whether a filesystem type is known to be a local disk (unknown types are not)."""
    return bool(fs_type) and fs_type in LOCAL_FS_TYPES


def _holds_entries(folder: str) -> bool:
    try:
        with os.scandir(folder) as entries:
            return next(entries, None) is not None
    except OSError:
        return False


def gone_from_disk(
    paths: Iterable[str],
    folders: dict[str, bool] | None = None,
    *,
    roots: Mapping[str, Iterable[str]] | None = None,
) -> bool:
    """Whether a file is gone: on none of the local paths it can be at, while a folder of one of them is still there.

    An unmounted library must never look gone: its folders are missing too. A disk whose mount went stale shows an empty
    mount point instead, so with ``roots`` a path whose disk root is missing or empty, or whose folder is that root
    itself (a flat library at a bare mount point), makes the file not gone. A read that fails with another error (a
    stale network file handle) doesn't count as gone either. A hard-mounted share that stalls makes these calls block
    instead of fail.

    Args:
        paths: The file's local paths, one per mapped disk (a single path where there is one disk).
        folders: Each folder's answer, cached across calls.
        roots: Per path, the disk roots it lies under (its path mapping's local folder, its library's folder); a path
            without an entry is judged by its folder alone.

    Returns:
        True when no path holds the file, no disk root of theirs looks unmounted, and at least one of their folders
        exists.
    """
    folders = {} if folders is None else folders
    folder_there = False
    for path in paths:
        folder = os.path.dirname(path)
        for root in (roots or {}).get(path, ()):
            if os.path.normpath(folder) == os.path.normpath(root) or not _holds_entries(root):
                return False
        if folders.get(folder) is False:
            continue
        try:
            os.stat(path)
            return False
        except FileNotFoundError:
            pass
        except OSError:
            return False
        if folder not in folders:
            folders[folder] = os.path.isdir(folder)
        folder_there = folder_there or folders[folder]
    return folder_there
