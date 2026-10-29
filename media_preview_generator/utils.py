"""Utility functions for Plex Video Preview Generator.

Contains general-purpose utility functions that can be reused across
different modules in the application.
"""

import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

# A secret's name, then its value up to a separator. The name ends in token, api key, password or secret (so
# ``plex_token``, ``PlexToken``, ``X-Emby-Token`` and ``client_secret`` count). Three ways to write one:
#   name=value         a URL query, a form, a MediaBrowser header's ``Token="…"``
#   'name': value      a quoted key: a dict's repr or JSON
#   Name: value        an HTTP header (Authorization, X-…-Token, X-…-Api-Key)
# Only those take ``:``, so prose such as "Invalid token: 401" or "The Secret: …" is left alone; a quoted word or a
# header's name before ``:`` still counts ("'Secret': 3" → "'Secret': ****"): masking a word beats leaking a token.
# The value may be quoted, a bytes literal (``b'…'``) or follow a Bearer/Basic scheme. "MediaBrowser" is no scheme
# here: its header's ``Token="…"`` is matched by its own name.
# A name starts only where a run of word characters and dashes starts, and its prefix is capped: starting at every
# ``\b`` inside a long dash-joined run (UUIDs, base64url) made each match attempt rescan the run, quadratic per line.
_NAME_START = r"(?<![\w-])"
_SECRET_NAME = r"[\w-]{0,64}?(?:token|api[_-]?key|passw(?:or)?d|secret)"
_SECRET_RE = re.compile(
    r"(?i)("
    rf"{_NAME_START}{_SECRET_NAME}['\"]?\s*=\s*"
    rf"|['\"](?:{_SECRET_NAME}|authorization)['\"]\s*:\s*"
    rf"|{_NAME_START}(?:(?:proxy-)?authorization|x-[\w-]{{0,64}}?(?:token|authorization|api-?key))\s*:\s*"
    r")((?:b?['\"])?(?:(?:bearer|basic)\s+)?)"
    r"[^\s&'\",;)\]}\x1b]+"  # \x1b: a colour code after the value (a coloured console line) stays
)
# A password in a URL's userinfo: ``scheme://user:password@host``.
_URL_PASSWORD_RE = re.compile(r"(://[^\s/:@]+:)[^\s/@]+(@)")
# Every job log line is masked: most carry none of these words, and skip the two patterns above.
_SECRET_HINTS = ("token", "key", "passw", "secret", "authorization", "://")


def redact_secrets(text: str) -> str:
    """Mask tokens, API keys and passwords in text shown on a job or written to a log.

    Exception text from an HTTP client can carry the request's URL or headers (``?X-Plex-Token=…``, ``api_key=…``,
    ``http://user:password@host``).

    Args:
        text: Any text.

    Returns:
        The text with each secret's value replaced by ``****``.
    """
    if not text:
        return ""
    lowered = text.lower()
    if not any(hint in lowered for hint in _SECRET_HINTS):
        return text
    masked = _SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}****", text)
    return _URL_PASSWORD_RE.sub(r"\1****\2", masked)


def redacted_traceback(exc: BaseException) -> str:
    """An exception's traceback for the log, secrets masked (:func:`redact_secrets`).

    Formatted by the standard library rather than loguru's ``logger.exception``, whose traceback repeats the
    exception's text unmasked and, with loguru's ``diagnose``, shows each frame's local values.

    Args:
        exc: The exception.

    Returns:
        The traceback text, without a trailing newline.
    """
    return redact_secrets("".join(traceback.format_exception(exc))).rstrip()


def to_utc_naive(value: datetime) -> datetime:
    """Convert a datetime to a UTC-naive value safe for comparison.

    plexapi returns ``addedAt`` as a *naive local time* (via
    ``datetime.fromtimestamp()`` with no tz arg), which silently
    miscompares against a naive UTC cutoff on any non-UTC server. On a
    PDT (UTC-7) host:

        added_at     = 2026-04-27 22:03 (naive local PDT)
        cutoff_naive = 2026-04-27 23:33 (naive UTC, ~30 min ago)
        22:03 < 23:33 → True → item incorrectly dropped

    This helper normalises both sides to UTC-naive so comparisons work
    regardless of host timezone. For tz-aware inputs it converts to UTC
    first; for naive inputs it assumes system local time (matching
    plexapi's convention) and converts via the timestamp() round-trip.

    See GitHub #226 for the silent zero-items repro on UTC-behind hosts.
    """
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return datetime.fromtimestamp(value.timestamp(), tz=UTC).replace(tzinfo=None)


def format_display_title(title: str, media_type: str, title_max_width: int) -> str:
    """Format and truncate display title based on media type.

    Args:
        title: The media title to format
        media_type: 'episode' or 'movie'
        title_max_width: Maximum width for the title

    Returns:
        str: Formatted and truncated title

    """
    if media_type == "episode":
        # For episodes, ensure S01E01 format is always visible
        if len(title) > title_max_width:
            # Simple truncation: keep last 6 chars (S01E01) + show title
            season_episode = title[-6:]  # Last 6 characters (S01E01)
            available_space = title_max_width - 6 - 3  # 6 for S01E01, 3 for "..."
            if available_space > 0:
                show_title = title[:-6].strip()  # Everything except last 6 chars
                if len(show_title) > available_space:
                    show_title = show_title[:available_space]
                display_title = f"{show_title}...{season_episode}"
            else:
                # Not enough space, just show the season/episode
                display_title = f"...{season_episode}"
        else:
            display_title = title
    else:
        # For movies, use the title as-is
        display_title = title

        # Regular truncation for movies
        if len(display_title) > title_max_width:
            display_title = display_title[: title_max_width - 3] + "..."  # Leave room for "..."

    # Add padding to prevent progress bar jumping (only if not already truncated)
    if len(display_title) <= title_max_width:
        padding_needed = title_max_width - len(display_title)
        display_title = display_title + " " * padding_needed

    return display_title


def is_docker_environment() -> bool:
    """Check if running inside a Docker container."""
    return (
        os.path.exists("/.dockerenv")
        or os.environ.get("container") == "docker"
        or os.environ.get("DOCKER_CONTAINER") == "true"
        or "docker" in os.environ.get("HOSTNAME", "").lower()
    )


def is_windows() -> bool:
    """Check if running on Windows operating system."""
    return os.name == "nt"


def is_macos() -> bool:
    """Check if running on macOS operating system."""
    import platform

    return platform.system() == "Darwin"


# A node runs the same GPU, FFmpeg and logging code as the app but has no screens of its own: its log is read on the
# app's Nodes page, so a hint in a line it logs must name the app's screen for it, or the node's own setting.
def is_node_mode() -> bool:
    """Whether this process is a remote node (``MODE=node``), not the app."""
    return os.environ.get("MODE", "").strip().lower() == "node"


# What each FFmpeg run's log in scratch_dir() is named from; a node removes the ones a killed process left, at its start.
FFMPEG_LOG_PREFIX = "ffmpeg_output_"


def node_temp_root() -> str:
    """A node's temp folder: ``NODE_TEMP_DIR``, or ``<system temp>/mpg-node``."""
    return os.environ.get("NODE_TEMP_DIR", "").strip() or os.path.join(tempfile.gettempdir(), "mpg-node")


def cause_chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and the exceptions it was raised from (``__cause__``), each once.

    Stops where the chain loops back, as Python's own traceback printing does: an exception raised from itself made a
    plain ``while`` walk spin forever, holding the GIL (sflix, 2026-10-28).
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__


def scratch_dir() -> str:
    """The folder for this process's short-lived files (FFmpeg's log, a helper's stderr).

    On a node, its temp folder (:func:`node_temp_root`), the one it makes and checks at start and sweeps for what a
    killed node left: a node run read-only may have no writable system temp at all, and every file sent to it would
    fail. Else the system's temp folder.
    """
    return node_temp_root() if is_node_mode() else tempfile.gettempdir()


def gpu_settings_hint() -> str:
    """Where a GPU is turned off: the app's GPU settings, or for a node's GPUs, the app's worker groups."""
    return "the app's Settings → Workers" if is_node_mode() else "Settings → Processing Options → GPU device tuning"


def cpu_workers_hint() -> str:
    """Where the CPU worker count is set: the app's worker groups, for a node's CPU too."""
    return "the app's Settings → Workers" if is_node_mode() else "Settings → Workers"


def gpu_details_hint() -> str:
    """Where each GPU's test result is shown."""
    if is_node_mode():
        return "this node's Overview tab on the app's Nodes page"
    return "Settings → Processing Options → GPU device tuning"


def log_viewer_hint() -> str:
    """Where the live log is read."""
    return "this node's Log tab on the app's Nodes page" if is_node_mode() else "the live web log viewer"


def working_folder_hint(tmp_folder: str | None = None) -> tuple[str, str]:
    """The folder FFmpeg writes thumbnails to, and how to give it another disk: ``(name, move)``.

    ``move`` follows "or ". The app has no setting for its working folder (``tmp_folder``, seeded once from
    ``TMP_FOLDER``), so a bigger or writable one is a disk mounted where it is: the container's temp folder, or the
    folder ``TMP_FOLDER`` seeded.

    Args:
        tmp_folder: The app's working folder (``Config.tmp_folder``); None or the system's temp folder for the default.
    """
    if is_node_mode():
        return "the node's temp folder", "choose another folder (NODE_TEMP_DIR)"
    if tmp_folder and os.path.normpath(tmp_folder) != os.path.normpath(tempfile.gettempdir()):
        return "the working folder", f"mount a bigger, writable disk at {tmp_folder}"
    return "the working folder", "mount a bigger, writable disk at the container's temp folder"


_PR_SET_PDEATHSIG = 1  # linux/prctl.h
_PDEATHSIG_KILL = ("--pdeathsig", "KILL", "--")


@functools.cache
def _setpriv() -> str | None:
    """The ``setpriv`` that sets a parent death signal (util-linux 2.33 and newer), or None.

    Tried once: an older ``setpriv`` refuses ``--pdeathsig``, and putting it in front of every FFmpeg would fail them
    all.
    """
    path = shutil.which("setpriv")
    if path is None:
        return None
    try:
        tried = subprocess.run([path, *_PDEATHSIG_KILL, "true"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return path if tried.returncode == 0 else None


def niced(args: list[str], level: int = 10) -> list[str]:
    """``args`` behind ``nice -n level`` when ``nice`` is on PATH, for FFmpeg that should yield the CPU to previews.

    Preview and Intro & Credits FFmpeg run at the app's own niceness, so under load they get most of the CPU while
    this command still runs. GNU ``nice`` execs the command in place, so the process is the command's own pid.
    Not on Windows: a Cygwin/MSYS ``nice.exe`` there starts the command as a child, so killing the pid would
    orphan FFmpeg.

    Args:
        args: The command (FFmpeg's).
        level: The nice increment; 0 or less leaves the command as it is.

    Returns:
        The command behind ``nice``, or as it is when ``level`` is 0 or less, on Windows, or with no ``nice``.
    """
    if level <= 0 or os.name == "nt":
        return args
    nice = shutil.which("nice")
    return [nice, "-n", str(level), *args] if nice else args


def dies_with_node(args: list[str]) -> tuple[list[str], Callable[[], None] | None]:
    """The command and ``preexec_fn`` that have a node's FFmpeg killed (SIGKILL) when the node dies, on Linux.

    In Docker the container's end takes every process with it; a node run outside Docker that exits or is killed
    left its FFmpeg decoding. Linux sends the signal when the *thread* that started the child ends, so use it only
    for a child that thread waits for (each FFmpeg run, never a long-lived helper process).

    ``setpriv --pdeathsig KILL`` sets the signal and then becomes FFmpeg (same process), so Python can start it the
    cheap way. A ``preexec_fn`` is only the fallback where there is no such ``setpriv``: it makes every start a full
    fork that runs Python in the child, which isn't safe beside threads holding locks (loguru's handlers). Unlike
    the fallback, ``setpriv`` can't tell a node that died before the signal was set: that FFmpeg runs on.

    Args:
        args: The command (FFmpeg's).

    Returns:
        ``(args, preexec_fn)`` for ``subprocess.Popen``: on a Linux node, the command behind ``setpriv`` and None,
        or without that ``setpriv`` the command and a function that sets the signal in the child; for the app and
        off Linux, the command as it is and None.
    """
    if not sys.platform.startswith("linux") or not is_node_mode():
        return args, None
    setpriv = _setpriv()
    if setpriv is not None:
        return [setpriv, *_PDEATHSIG_KILL, *args], None
    return args, _set_parent_death_signal()


def _set_parent_death_signal() -> Callable[[], None] | None:
    """A ``preexec_fn`` that sets SIGKILL as the child's parent death signal; None without ``prctl``."""
    import ctypes

    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl  # loaded here: the child only calls it
    except (OSError, AttributeError):
        return None
    parent = os.getpid()

    def set_parent_death_signal() -> None:
        prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent:  # the node died before the signal was set: nothing would send it
            os._exit(1)

    return set_parent_death_signal


def debug_logging_hint() -> str:
    """How to see FFmpeg's detailed output (a node keeps no DEBUG lines for the app to read; its console has them)."""
    if is_node_mode():
        return "set LOG_LEVEL=DEBUG on the node and read its docker logs"
    return "enable Debug logging (Settings → Logging)"


def sanitize_path(path: str) -> str:
    """Sanitize file path for cross-platform compatibility.

    On Windows:
    - Converts forward slashes to backslashes
    - Handles UNC paths (\\\\server\\share)
    - Normalizes path separators

    On Linux/macOS:
    - Returns path as-is with normalization

    Args:
        path: The file path to sanitize

    Returns:
        str: Sanitized file path

    """
    if os.name == "nt":
        # Handle UNC paths: //server/share -> \\server\share
        if path.startswith("//"):
            path = "\\\\" + path[2:].replace("/", "\\")
        else:
            path = path.replace("/", "\\")

    # Normalize path (removes redundant separators and up-level references)
    return os.path.normpath(path)


def _fsync_dir(path: str) -> None:
    """Best-effort fsync of a directory so a just-renamed file survives power loss."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_json_save(filepath: str, data: Any, *, permissions: int | None = None) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory first, then replaces
    the target. This prevents corruption if the process is killed mid-write.

    Args:
        filepath: Destination file path.
        data: JSON-serializable data to write.
        permissions: Optional octal file permissions (e.g. 0o600).

    Raises:
        IOError: If the write or replace fails.

    """
    parent = os.path.dirname(filepath) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        _fsync_dir(parent)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    if permissions is not None:
        try:
            os.chmod(filepath, permissions)
        except OSError:
            pass


def _backup_retention() -> int:
    """How many timestamped backups to keep per file.

    Resolution order: settings.json `config_backup_keep` → env
    `CONFIG_BACKUP_KEEP` → default 10. Imported lazily to avoid an
    import cycle (utils → settings_manager → utils via atomic save).

    Uses :func:`peek_settings_manager` (a lock-free accessor) rather than
    :func:`get_settings_manager`. The standard accessor acquires
    ``_settings_lock``, which ``get_settings_manager`` holds around
    ``SettingsManager.__init__``, so a save made while the singleton is
    being built would deadlock re-entering it (every 3.7.5 upgrader hit
    this once, when ``_load`` saved the ``plex_webhook_*`` move). When the
    singleton isn't built yet, fall through to env / default — same value
    a brand-new install gets.
    """
    raw = None
    try:
        from .web.settings_manager import peek_settings_manager

        sm = peek_settings_manager()
        if sm is not None:
            raw = sm.get("config_backup_keep")
    except Exception:
        raw = None
    if raw in (None, ""):
        raw = os.environ.get("CONFIG_BACKUP_KEEP", "10")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(1, min(n, 100))


def _backup_max_age_days() -> int:
    """Drop backups older than this many days. 0 disables age-based pruning.

    Resolution order: settings.json `config_backup_max_age_days` → env
    `CONFIG_BACKUP_MAX_AGE_DAYS` → default 0 (disabled).

    Uses :func:`peek_settings_manager` for the same reason as
    :func:`_backup_retention` — avoids the first-boot deadlock.
    """
    raw = None
    try:
        from .web.settings_manager import peek_settings_manager

        sm = peek_settings_manager()
        if sm is not None:
            raw = sm.get("config_backup_max_age_days")
    except Exception:
        raw = None
    if raw in (None, ""):
        raw = os.environ.get("CONFIG_BACKUP_MAX_AGE_DAYS", "0")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 365))


_BACKUP_SUFFIX_RE = re.compile(r"\.[0-9]{8}-[0-9]{6}\.bak")


def timestamped_backups(filepath: str) -> list[str]:
    """Return the app's own backups of ``filepath`` (``{filepath}.YYYYMMDD-HHMMSS.bak``), oldest first.

    Any other ``.bak`` beside the file — the legacy ``{filepath}.bak``, a deploy script's
    ``{filepath}.pre-upgrade.bak`` — is someone else's: it is never listed here, so never counted or pruned.
    Name order is age order because the stamp is fixed-width.

    Args:
        filepath: The live file the backups were taken of.

    Returns:
        Paths of the app's own backups, oldest first.
    """
    import glob

    return sorted(
        path
        for path in glob.glob(glob.escape(filepath) + ".*.bak")
        if _BACKUP_SUFFIX_RE.fullmatch(path[len(filepath) :])
    )


def backup_file(filepath: str) -> str:
    """Copy ``filepath`` to ``{filepath}.{YYYYMMDD-HHMMSS}.bak`` unless this second already has a backup.

    Args:
        filepath: The file to back up; it must exist.

    Returns:
        The backup's path for this second (just written, or the one already there).

    Raises:
        OSError: If the copy fails.
    """
    bak_path = f"{filepath}.{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.bak"
    # Several saves can land in one second (a settings migration makes about seven). The first copy is the file
    # before any of them; overwriting it would leave only a half-changed state to roll back to.
    if not os.path.exists(bak_path):
        shutil.copy2(filepath, bak_path)
        # The copy carries the file's modified time; pruning by age (``_prune_old_backups``) would delete the backup
        # of a file last changed before the cutoff as soon as it was made. A backup's time is when it was made.
        os.utime(bak_path)
    return bak_path


def _prune_old_backups(filepath: str, keep: int, max_age_days: int = 0) -> None:
    """Drop the app's own backups of ``filepath`` beyond the newest ``keep`` AND any older than ``max_age_days``.

    Best-effort — failures are swallowed (a stale backup never blocks the
    next save). Only :func:`timestamped_backups` are counted or deleted.
    ``max_age_days=0`` disables age-based pruning so existing installs keep
    behaving exactly as before until the user opts in.
    """
    import time

    backups = timestamped_backups(filepath)
    to_delete: set[str] = set()

    excess = len(backups) - keep
    if excess > 0:
        to_delete.update(backups[:excess])

    if max_age_days > 0:
        cutoff = time.time() - (max_age_days * 86400)
        for path in backups:
            try:
                if os.path.getmtime(path) < cutoff:
                    to_delete.add(path)
            except OSError:
                pass

    for stale in to_delete:
        try:
            os.unlink(stale)
        except OSError:
            pass


def atomic_json_save_with_backup(filepath: str, data: Any, *, permissions: int | None = None) -> None:
    """Atomic JSON write that keeps the last N timestamped backups of prior contents.

    Same atomicity guarantees as ``atomic_json_save``, plus: before writing,
    if ``filepath`` already exists, copy it to ``filepath.{YYYYMMDD-HHMMSS}.bak``
    (see :func:`backup_file`) and prune oldest beyond ``CONFIG_BACKUP_KEEP``
    (default 10). Backup is best-effort — failures are logged but never block
    the primary write, since the caller's data is more important than the
    recovery copy.

    The legacy single ``filepath.bak`` (from previous app versions) is left
    in place: the inventory + restore endpoints recognise it as a
    "previous version" entry alongside the new timestamped backups.

    Designed for the small, hand-editable JSON files this app owns
    (settings.json, schedules.json, webhook_history.json, setup_state.json).
    For high-write-rate state, prefer SQLite (see web/jobs.py).

    Args:
        filepath: Destination file path.
        data: JSON-serializable data to write.
        permissions: Optional octal file permissions (e.g. 0o600).

    Raises:
        IOError: If the primary write or replace fails. Backup failures do not raise.
    """
    if os.path.exists(filepath):
        try:
            backup_file(filepath)
            _prune_old_backups(filepath, _backup_retention(), _backup_max_age_days())
        except OSError as exc:
            # Don't import loguru at module load — keep this dep-light. Log
            # via stderr so we don't pull in the logging stack just for a
            # backup hiccup.
            import sys

            print(
                f"[atomic_json_save_with_backup] Could not write a backup of {filepath}: {exc}",
                file=sys.stderr,
            )
    atomic_json_save(filepath, data, permissions=permissions)


def setup_working_directory(tmp_folder: str) -> str:
    """Create and set up a unique working temporary directory.

    Args:
        tmp_folder: Base temporary folder path

    Returns:
        str: Path to the created working directory

    Raises:
        OSError: If directory creation fails

    """
    # Create a unique subfolder for this run to avoid conflicts
    unique_id = f"plex_previews_{int(time.time())}_{str(uuid.uuid4())[:8]}"
    working_tmp_folder = os.path.join(tmp_folder, unique_id)

    # Create our specific working directory
    os.makedirs(working_tmp_folder, exist_ok=True)

    return working_tmp_folder
