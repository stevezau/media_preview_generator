"""Emby sidecar BIF output adapter.

Emby's "Save preview video thumbnails into media folders" feature drops
``{basename}-{width}-{interval}.bif`` next to the source video; the
client picks them up automatically on library scan. Reproducing that
naming exactly means our generated BIFs slot into Emby installations as
if Emby had produced them itself ([forum discussion](
https://emby.media/community/topic/112001-what-is-a-bif-file-and-why-do-all-have-320-10-at-end-of-filename/)).

Unlike :class:`PlexBundleAdapter`, this adapter doesn't need any
server-side metadata — the output path is derived purely from the
canonical media path plus the configured width and interval.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import time
from pathlib import Path

from loguru import logger

from ..servers.base import MediaServer
from .base import BifBundle, OutputAdapter

# The exact name ``processing.generator._bif_temp_path`` writes a sidecar BIF under before renaming it into place:
# a dot, the first 16 hex digits of the sidecar name's SHA-1, ``.bif-tmp``. Match it with ``fullmatch``.
_BIF_TEMP_NAME = re.compile(r"\.[0-9a-f]{16}\.bif-tmp")

# A write updates its temp file's mtime as it goes and finishes in seconds, so one untouched this long was left by a
# crash. Younger ones may belong to another worker writing a sibling video's BIF in the same folder right now.
_STALE_WRITE_TEMP_SECONDS = 3600


class EmbyBifAdapter(OutputAdapter):
    """Publish Emby-style sidecar BIF files alongside the media file.

    Args:
        width: BIF thumbnail width in pixels (Emby's default is 320).
            Encoded into the filename so multiple resolutions can coexist.
        frame_interval: Seconds between frames. Encoded into the filename
            (Emby uses this on its own generation runs too).
    """

    def __init__(self, *, width: int = 320, frame_interval: int = 10) -> None:
        self._width = int(width)
        self._frame_interval = int(frame_interval)

    @property
    def name(self) -> str:
        return "emby_sidecar"

    def needs_server_metadata(self) -> bool:
        # Sidecar path is derived purely from the canonical media path;
        # no API calls needed before publishing.
        return False

    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Return ``[<media_dir>/<basename>-<width>-<interval>.bif]``."""
        media_path = Path(bundle.canonical_path)
        basename = media_path.stem  # without extension
        sidecar = media_path.parent / f"{basename}-{self._width}-{self._frame_interval}.bif"
        return [sidecar]

    def publish(self, bundle: BifBundle, output_paths: list[Path], item_id: str | None = None) -> None:
        """Pack ``bundle.frame_dir`` into a BIF at the sidecar path.

        Reuses the existing ``generate_bif`` helper so the BIF byte layout
        stays in lockstep with the Plex publisher. The media folder must
        already exist (it does — we only got here because the source file
        is present); we only create missing parent directories defensively
        for unusual mount setups.
        """
        if not output_paths:
            raise ValueError("EmbyBifAdapter.publish requires at least one output path")

        from ..processing.generator import generate_bif
        from .plex_bundle import BifIntervalConfig

        sidecar = output_paths[0]
        # The media folder normally exists, so mkdir succeeds even on a
        # read-only mount; the write error surfaces when the .bif is opened.
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            generate_bif(
                str(sidecar),
                str(bundle.frame_dir),
                BifIntervalConfig(self._frame_interval, server_display_name=bundle.server_display_name),
            )
        except OSError as exc:
            # Only the sidecar, the file it's written to beside it, or a folder
            # above it gets media-mount advice; a failure reading the frames
            # folder is a different problem.
            failed_path = Path(exc.filename) if exc.filename is not None else None
            if failed_path is None or (
                failed_path not in (sidecar, *sidecar.parents) and failed_path.parent != sidecar.parent
            ):
                raise
            if exc.errno == errno.EROFS:
                logger.error(
                    "Cannot save Emby preview file next to media at {}: the media folder is mounted "
                    "read-only. Emby reads .bif previews from beside the video, so this tool needs the "
                    "media folder mounted read-write (remove :ro from the Docker volume). "
                    "Original error: {}",
                    sidecar.parent,
                    exc,
                )
            elif exc.errno in (errno.EACCES, errno.EPERM):
                logger.error(
                    "Cannot save Emby preview file next to media at {}: permission denied. "
                    "The Emby BIF format requires writing the .bif file alongside the source video. "
                    "Verify the media folder is mounted read-write (not :ro in Docker) and that "
                    "the user running this tool has write permission. "
                    "Original error: {}",
                    sidecar.parent,
                    exc,
                )
            raise

        # Sanity: filename must follow Emby's <basename>-<w>-<i>.bif pattern.
        # If a future caller misuses compute_output_paths and passes a
        # custom path, Emby won't pick it up — log a warning so it's
        # diagnosable in the field.
        if not sidecar.name.endswith(f"-{self._width}-{self._frame_interval}.bif"):
            logger.warning(
                "Emby preview file saved with an unexpected name: {}. "
                "The file is on disk and is valid, but Emby looks for the pattern "
                "'<video-name>-<width>-<interval>.bif' to auto-discover previews — "
                "with this name Emby will likely ignore it. This is a configuration "
                "bug worth reporting; previews on other servers are unaffected.",
                sidecar.name,
            )

        logger.debug("Emby sidecar BIF written to {}", sidecar)

    @staticmethod
    def sidecar_path(canonical_path: str, *, width: int, frame_interval: int) -> Path:
        """Public helper used by tests + future read-side code (BIF viewer).

        Returns the Emby sidecar path that *would* be written for the given
        media path / width / interval, without instantiating an adapter.
        """
        media_path = Path(canonical_path)
        basename = media_path.stem
        return media_path.parent / f"{basename}-{int(width)}-{int(frame_interval)}.bif"

    def list_orphans_in_folder(self, folder: Path, live_basenames: set[str]) -> list[Path]:
        """Enumerate ``<basename>-<W>-<I>.bif`` (and ``.bif.meta``) sidecars
        whose source video is gone.

        Emby's auto-discovery looks for ``<basename>-<width>-<interval>.bif``
        next to each media file. When Radarr/Sonarr upgrades a file with a
        new release-group / quality suffix, the old ``.bif`` (and the
        journal ``.bif.meta`` next to it) sit next to the new file
        unowned. This helper returns both for the caller to delete.

        Returns paths grouped sidecar-then-meta so the caller's removal
        log line order reads naturally. Returns ``[]`` if the folder
        cannot be read. Temp files a crashed write left are swept by
        :meth:`sweep_stale_write_temps`, never by the generic removal.
        """
        try:
            entries = list(folder.glob("*.bif"))
        except OSError:
            return []
        orphans: list[Path] = []
        for path in entries:
            if not path.is_file():
                continue
            # Filename pattern: ``<basename>-<W>-<I>.bif``. Strip the
            # ``-<W>-<I>.bif`` suffix to recover the original media
            # basename. Two trailing ``-NUMBER`` segments before ``.bif``
            # signal a managed sidecar; anything else is a foreign file
            # and we leave it alone.
            stem = path.stem  # ``<basename>-<W>-<I>``
            parts = stem.rsplit("-", 2)
            if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
                continue
            base = parts[0]
            if not base or base in live_basenames:
                continue
            orphans.append(path)
            # Sibling .meta journal — only return it if it actually
            # exists (the journal is best-effort and may be missing).
            meta = path.with_suffix(path.suffix + ".meta")
            if meta.exists():
                orphans.append(meta)
        return orphans

    def sweep_stale_write_temps(self, folder: Path) -> list[Path]:
        """Remove the temp files crashed sidecar writes left in ``folder``; return the ones removed.

        Only :meth:`_stale_write_temps` candidates, each checked again right before its ``os.unlink`` (never a
        recursive or generic removal): anything that has since become a folder, a link or a file written in the last
        hour is left alone. A temp of a live video's sidecar goes too: its next write starts by removing it anyway.

        Args:
            folder: A folder of media files this adapter writes sidecars into.

        Returns:
            The temp files removed.
        """
        removed: list[Path] = []
        for path in self._stale_write_temps(folder):
            if not _is_stale_write_temp(path):
                continue
            try:
                os.unlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                logger.warning("Couldn't remove {}, left by an interrupted preview write ({}); leaving it", path, exc)
                continue
            removed.append(path)
        return removed

    @staticmethod
    def _stale_write_temps(folder: Path) -> list[Path]:
        """Return the regular files in ``folder`` named exactly like a sidecar write's temp and untouched for an hour.

        Symlinks and folders are skipped whatever their name, as is anything that can't be read.
        """
        temps: list[Path] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if _BIF_TEMP_NAME.fullmatch(entry.name) and _is_stale_write_temp(Path(entry.path)):
                        temps.append(Path(entry.path))
        except OSError:
            return []
        return temps


def _is_stale_write_temp(path: Path) -> bool:
    """Whether ``path`` (not followed) is a regular file untouched for ``_STALE_WRITE_TEMP_SECONDS``."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_mtime < time.time() - _STALE_WRITE_TEMP_SECONDS
