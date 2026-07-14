"""Config-directory health probe.

At startup (and on demand from the dashboard) we check that the mounted
config directory (``/config`` by default) is actually usable: writable by
the process user, not a read-only mount, on a filesystem where SQLite's WAL
locking is reliable, and not out of space.

A read-only ``/config`` is the single most common "nothing works" support
report (issue #278): every write to ``jobs.db`` / ``scheduler.db`` /
``settings.json`` fails with SQLite's opaque ``attempt to write a readonly
database`` and the UI looks frozen while the log floods with one persist
failure per item. Surfacing ONE clear, actionable message — is it an
ownership mismatch, an explicit ``:ro`` mount, a full disk, or a flaky
network share? — beats that failure mode entirely.
"""

import os
import uuid

from loguru import logger

# Warn when free space on the config filesystem drops below this. SQLite
# needs headroom for the WAL + rollback journal; under a few MB, writes
# start failing with "database or disk is full" — a different error with
# the same "nothing saves" symptom.
_LOW_SPACE_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB

# Filesystem types where SQLite's POSIX file-locking / WAL is unreliable.
# Pointing /config at an NFS/SMB share is a common NAS setup that produces
# intermittent "database is locked" errors — we warn, never block, because
# it often works well enough that a hard failure would be wrong.
_NETWORK_FS_TYPES = {
    "nfs",
    "nfs4",
    "cifs",
    "smbfs",
    "smb3",
    "ncpfs",
    "9p",
    # Kernel-side network/cluster filesystems — no ``fuse.`` prefix.
    "ceph",
    "lustre",
    "beegfs",
    "afs",
}

# Network- and cloud-backed FUSE drivers, matched on the ``<name>`` in
# ``fuse.<name>``. The locking risk is the same as NFS/SMB, so they get the
# same warning. Kept as an allowlist rather than "any FUSE is remote": that
# assumption is what produced the false alarm on Unraid and mergerfs.
_NETWORK_FUSE_TYPES = {
    "sshfs",
    "rclone",
    "glusterfs",
    "s3fs",
    "davfs",
    "davfs2",
    "ftpfs",
    "curlftpfs",
    "smbnetfs",
    "juicefs",
    "gcsfuse",
    "blobfuse",
    "moosefs",
    "mfs",
    "seaweedfs",
    "ossfs",
    "ceph",
    "ceph-fuse",
}

# Unraid's user-share layer. This is a FUSE union over LOCAL disks — calling it
# a network share is simply wrong, and it's what most Unraid users see. But
# running the app's SQLite databases on /mnt/user is still discouraged on
# Unraid (the standard advice is /mnt/cache/appdata or a specific disk), so it
# earns its own advisory with wording that names the actual fix.
_UNRAID_SHARE_FUSE_TYPE = "shfs"

# Advisories the user may permanently dismiss. These are opinions about the
# setup, not failures — the app works. Note the absence of the writability
# statuses: ``writable=False`` means nothing saves, so hiding it would only
# hide the reason the app appears broken.
DISMISSIBLE_WARNING_KINDS = frozenset({"network_fs", "unraid_share", "low_space"})

#: settings.json key holding the list of dismissed advisory kinds.
DISMISSED_WARNINGS_SETTING = "dismissed_config_warnings"


def _classify_fs(fstype: str) -> str | None:
    """Classify a filesystem for the SQLite-reliability advisory.

    Returns ``"network"`` (POSIX locking genuinely unreliable),
    ``"unraid_share"`` (local, but running a DB there is discouraged), or
    ``None`` for anything we shouldn't nag about.

    Local union FUSE — ``fuse.mergerfs`` and, deliberately, any FUSE driver we
    don't recognise — classifies as ``None``. Defaulting an unknown FUSE mount
    to "network share" is what produced the false alarm this function exists to
    prevent: a warning that's wrong on the user's actual setup teaches them to
    ignore the warnings that aren't.
    """
    if fstype in _NETWORK_FS_TYPES:
        return "network"
    if fstype.startswith("fuse"):
        driver = fstype.partition(".")[2]
        if driver in _NETWORK_FUSE_TYPES:
            return "network"
        if driver == _UNRAID_SHARE_FUSE_TYPE:
            return "unraid_share"
    return None


def filter_dismissed_warnings(warnings: list[dict], dismissed: object) -> list[dict]:
    """Drop advisories the user has dismissed.

    ``dismissed`` comes from user-editable settings.json, so anything that
    isn't a list is treated as "nothing dismissed" rather than raising — a
    mangled value must not take the health banner down with it.
    """
    if not isinstance(dismissed, list):
        return list(warnings)
    dropped = {d for d in dismissed if isinstance(d, str)}
    return [w for w in warnings if w.get("kind") not in dropped]


def _proc_mounts() -> list[tuple[str, str, set[str]]]:
    """Parse ``/proc/mounts`` into ``(mountpoint, fstype, options)`` tuples.

    Returns an empty list on any platform without ``/proc/mounts`` (macOS,
    Windows) so callers degrade to "no mount info" rather than raising.
    """
    entries: list[tuple[str, str, set[str]]] = []
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                # /proc/mounts octal-escapes spaces in the mountpoint.
                mountpoint = parts[1].replace("\\040", " ")
                entries.append((mountpoint, parts[2], set(parts[3].split(","))))
    except OSError:
        return []
    return entries


def _mount_for_path(path: str) -> tuple[str, set[str]] | None:
    """Return ``(fstype, options)`` of the mount that contains ``path``.

    Picks the longest matching mountpoint (most specific) so a bind-mount
    at ``/config`` wins over the root filesystem mounted at ``/``. Returns
    ``None`` when no mount info is available.
    """
    real = os.path.realpath(path)
    best: tuple[int, str, set[str]] | None = None
    for mountpoint, fstype, options in _proc_mounts():
        mp = os.path.realpath(mountpoint)
        if real == mp or real.startswith(mp.rstrip("/") + "/"):
            score = len(mp)
            if best is None or score > best[0]:
                best = (score, fstype, options)
    if best is None:
        return None
    return best[1], best[2]


def probe_config_health(config_dir: str) -> dict:
    """Probe ``config_dir`` for the conditions that silently break persistence.

    The authoritative check is an actual create+delete of a probe file — it
    catches every reason a write can fail (ownership, ``:ro`` mount, full
    disk, ACLs) in one go. The mount/space lookups only exist to turn that
    boolean into a *specific* fix ("remove :ro" vs "chown" vs "free space").

    Args:
        config_dir: The configuration directory (e.g. ``/config``).

    Returns:
        A wire-friendly dict the dashboard renders directly:

        * ``writable`` (bool) — the load-bearing field; ``False`` means
          settings, schedules, and job history cannot be saved.
        * ``status`` — ``"ok"`` | ``"not_writable"`` | ``"read_only_mount"``.
        * ``detail`` / ``hint`` — human message + the exact host-side fix.
        * ``warnings`` — list of non-fatal advisories (network fs, Unraid user
          share, low space), each ``{"kind", "message"}``. Dismissible; see
          :data:`DISMISSIBLE_WARNING_KINDS`.
        * diagnostics: ``process_user``, ``dir_owner``, ``dir_mode``,
          ``read_only_mount``, ``fs_type``, ``fs_advisory``, ``free_bytes``,
          ``low_space``. ``network_fs`` is legacy: it is now set ONLY for
          genuinely networked mounts (it used to be set for every FUSE mount,
          which mislabelled Unraid and mergerfs as network shares).
    """
    process_user = f"{os.getuid()}:{os.getgid()}"
    result: dict = {
        "path": config_dir,
        "writable": True,
        "status": "ok",
        "read_only_mount": False,
        "network_fs": None,
        "fs_type": None,
        "fs_advisory": None,
        "free_bytes": None,
        "low_space": False,
        "process_user": process_user,
        "dir_owner": None,
        "dir_mode": None,
        "detail": "",
        "hint": "",
        "warnings": [],
    }

    # Best-effort: create the directory so a first-run probe of a
    # not-yet-created /config still reports a meaningful writability result
    # (a parent that's read-only makes this fail, which is itself the answer).
    try:
        os.makedirs(config_dir, exist_ok=True)
    except OSError:
        pass

    try:
        st = os.stat(config_dir)
        result["dir_owner"] = f"{st.st_uid}:{st.st_gid}"
        result["dir_mode"] = oct(st.st_mode & 0o777)
    except OSError:
        pass

    mount = _mount_for_path(config_dir)
    if mount is not None:
        fstype, options = mount
        result["fs_type"] = fstype
        result["fs_advisory"] = _classify_fs(fstype)
        # Retained for API compatibility: only set for genuinely networked
        # mounts now, so it no longer mislabels Unraid/mergerfs as "network".
        if result["fs_advisory"] == "network":
            result["network_fs"] = fstype
        if "ro" in options:
            result["read_only_mount"] = True

    try:
        stv = os.statvfs(config_dir)
        free = stv.f_bavail * stv.f_frsize
        result["free_bytes"] = free
        result["low_space"] = free < _LOW_SPACE_THRESHOLD_BYTES
    except OSError:
        pass

    # Authoritative writability check — create a probe file. The name is
    # unique per attempt: pid alone is NOT enough because the deployment runs
    # gunicorn gthread with one worker (one PID), so concurrent probes (every
    # open tab polls /config-health, and create_job/create_schedule probe too)
    # would race on a shared path — a losing racer's cleanup unlink would raise
    # and spuriously report the dir unwritable (false 503 / red banner).
    probe_path = os.path.join(config_dir, f".config-write-probe-{os.getpid()}-{uuid.uuid4().hex}")
    write_ok = False
    try:
        fd = os.open(probe_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        write_ok = True
    except OSError as exc:
        result["writable"] = False
        result["error_type"] = type(exc).__name__
        result["error_message"] = str(exc)
        if result["read_only_mount"]:
            result["status"] = "read_only_mount"
            result["detail"] = f"Config folder {config_dir} is mounted read-only."
            result["hint"] = (
                f"Remove the ':ro' flag from the volume mapped to {config_dir} "
                "(or set the dataset/share to read-write), then restart the container."
            )
        else:
            result["status"] = "not_writable"
            owner = result["dir_owner"] or "another user"
            result["detail"] = f"Config folder {config_dir} isn't writable by this container."
            result["hint"] = (
                f"The app runs as {process_user} but {config_dir} is owned by {owner}. "
                f"On the host run `chown -R {process_user} <your config folder>` "
                "(or set PUID/PGID to match the owner), then restart the container."
            )

    # Best-effort cleanup — a probe that WROTE already proved writability, so a
    # cleanup hiccup must never flip the verdict back to unwritable.
    if write_ok:
        try:
            os.unlink(probe_path)
        except OSError:
            logger.debug("Could not remove config write-probe {}", probe_path)

    # Non-fatal advisories only make sense when writes actually work; a
    # read-only mount already dominates the message.
    if result["writable"]:
        if result["fs_advisory"] == "network":
            result["warnings"].append(
                {
                    "kind": "network_fs",
                    "message": (
                        f"Config folder {config_dir} is on a '{result['fs_type']}' network "
                        "filesystem. SQLite's file locking is unreliable over network shares and "
                        "can cause intermittent 'database is locked' errors — a local disk/volume "
                        "for the config folder is strongly recommended."
                    ),
                }
            )
        elif result["fs_advisory"] == "unraid_share":
            result["warnings"].append(
                {
                    "kind": "unraid_share",
                    "message": (
                        f"Config folder {config_dir} is on an Unraid user share (/mnt/user). That's "
                        "local storage, not a network share — but running the app's database on a "
                        "user share can still cause intermittent 'database is locked' errors. "
                        "Pointing the config volume at /mnt/cache/appdata (or a specific disk) is "
                        "the usual fix. If it's working fine for you, you can dismiss this."
                    ),
                }
            )
        if result["low_space"] and result["free_bytes"] is not None:
            free_mb = result["free_bytes"] // (1024 * 1024)
            result["warnings"].append(
                {
                    "kind": "low_space",
                    "message": (
                        f"Only {free_mb} MB free on the config filesystem. Settings, schedules, and "
                        "job history will start failing to save when it fills up — free up space."
                    ),
                }
            )

    return result


def log_config_health(config_dir: str) -> dict:
    """Probe ``config_dir`` and log ONE actionable message on any problem.

    Called once at startup so a broken mount is visible in ``docker logs``
    immediately — instead of being discovered as a flood of per-item
    "attempt to write a readonly database" persist failures. Returns the
    probe result so the caller can reuse it.
    """
    health = probe_config_health(config_dir)
    if not health["writable"]:
        logger.error(
            "{} {} Until this is fixed, settings, schedules, and job history cannot be "
            "saved and the dashboard will appear frozen while jobs run.",
            health["detail"],
            health["hint"],
        )
    for warning in health["warnings"]:
        logger.warning(warning["message"])
    return health
