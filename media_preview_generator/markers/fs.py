"""Filesystem type of a path, for the "Plex DB must be on this host" rule (SQLite WAL needs a local filesystem), and
whether a file is gone from disk."""

from __future__ import annotations

import os
import stat
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


def _nearest_folder_holds_entries(folder: str, floor: str) -> bool:
    """Whether the nearest folder at or above ``folder`` that exists, going no higher than ``floor``, holds entries.

    A deleted series leaves its library folder with the other series in it. A disk mounted inside the library whose
    mount went away leaves an empty mount point where its folders were, a symlinked folder whose target went away is a
    link rather than a folder, and a stale network handle answers with an error: none of them counts.
    """
    current, floor = os.path.normpath(folder), os.path.normpath(floor)
    while True:
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            if current == floor or not current.startswith(floor.rstrip("/") + "/"):
                return False
            current = os.path.dirname(current)
            continue
        except OSError:
            return False
        return stat.S_ISDIR(st.st_mode) and _holds_entries(current)


def gone_from_disk(
    paths: Iterable[str],
    folders: dict[str, bool] | None = None,
    *,
    roots: Mapping[str, Iterable[str]] | None = None,
    trust_roots: bool = False,
) -> bool:
    """Whether a file is gone: on none of the local paths it can be at, while a folder of one of them is still there.

    An unmounted library must never look gone: its folders are missing too. A disk whose mount went stale shows an empty
    mount point instead, so with ``roots`` a path whose disk root is missing or empty, or whose folder is that root
    itself (a flat library at a bare mount point), makes the file not gone. A read that fails with another error (a
    stale network file handle) doesn't count as gone either. A hard-mounted share that stalls makes these calls block
    instead of fail.

    With ``trust_roots``, a path whose disk roots all hold entries is gone even when its folder is missing too (a series
    deleted whole takes its season folder with it), as long as the nearest folder above it that still exists, up to its
    deepest root, holds entries. A path without roots is still judged by its folder. Anything at a path is then there,
    a dangling symlink included (``os.lstat``): a library of symlinks into a remote mount (rclone, zurg) that dropped
    keeps its links, and marking missing files must not take them for deleted. Without it a dangling link is gone, as a
    Plex version's file (``plex_db``) and a cached fingerprint are judged: a version behind a dangling link can't be
    decided, and waiting for it would hold its item's markers back.

    Args:
        paths: The file's local paths, one per mapped disk (a single path where there is one disk).
        folders: Each folder's answer, cached across calls.
        roots: Per path, the disk roots it lies under (its path mapping's local folder, its library's folder); a path
            without an entry is judged by its folder alone.
        trust_roots: Let mounted, non-empty roots stand in for a missing folder (markers.db forgetting deleted files).

    Returns:
        True when no path holds the file, no disk root of theirs looks unmounted, and at least one of their folders
        exists (with ``trust_roots``, or the nearest existing folder above one holds entries).
    """
    folders = {} if folders is None else folders
    folder_there = False
    for path in paths:
        folder = os.path.dirname(path)
        path_roots = tuple((roots or {}).get(path, ()))
        for root in path_roots:
            if os.path.normpath(folder) == os.path.normpath(root) or not _holds_entries(root):
                return False
        trusted = trust_roots and bool(path_roots)
        if folders.get(folder) is False and not trusted:
            continue
        try:
            (os.lstat if trust_roots else os.stat)(path)
            return False
        except FileNotFoundError:
            pass
        except OSError:
            return False
        if folder not in folders:
            folders[folder] = os.path.isdir(folder)
        if folders[folder] or (trusted and _nearest_folder_holds_entries(folder, max(path_roots, key=len))):
            folder_there = True
    return folder_there
