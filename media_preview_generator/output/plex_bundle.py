"""Plex bundle BIF output adapter.

Translates a frame :class:`BifBundle` into Plex's bundle-hash on-disk layout
and packs the BIF at that location. Plex's expected path structure:

    {plex_config_folder}/Media/localhost/<h0>/<h[1:]>.bundle/Contents/Indexes/index-sd.bif

where ``<h0>`` is the first character of the bundle hash. The hash is calculated
from the local media file, except in bulk scans, where Plex's own per-part hash is
used after a file name and size match. Publishing does not require Plex to be
online or to have indexed the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from loguru import logger

from ..servers.base import MediaServer
from ..utils import sanitize_path
from .base import BifBundle, OutputAdapter
from .plex_hash import SourceFileChangedError, calculate_plex_hash, get_source_fingerprint

_BUNDLE_HASH_RE = re.compile(r"[0-9a-f]{40}")


class PlexBundleAdapter(OutputAdapter):
    """Publish into Plex's bundle-hash directory structure.

    Args:
        plex_config_folder: Absolute path to the Plex Media Server data root
            (the directory that contains ``Media/localhost/...``).
        frame_interval: BIF frame interval in seconds. Persisted in the BIF
            header so Plex spaces preview frames correctly during scrubbing.
    """

    def __init__(self, plex_config_folder: str, frame_interval: int) -> None:
        self._plex_config_folder = plex_config_folder
        self._frame_interval = int(frame_interval)

    @property
    def name(self) -> str:
        return "plex_bundle"

    def needs_server_metadata(self) -> bool:
        return False

    def compute_output_paths(
        self,
        bundle: BifBundle,
        server: MediaServer | None,
        item_id: str | None,
    ) -> list[Path]:
        """Return the bundle destination derived from the current source file.

        The hash is read from the file, except for bulk scans
        (``bundle.trust_server_hash``): Plex's own hash saves two disk reads per
        file, and is used only when exactly one of its parts has this file's name
        and byte size. Anything else, including a Plex that has no hash yet,
        falls back to reading the file.

        Raises:
            OSError: The source is unreadable or changes during hashing.
        """
        self._validate_source(bundle)
        bundle_hash = self._server_hash(bundle, server, item_id) or calculate_plex_hash(bundle.canonical_path)
        return [self.bundle_bif_path(self._plex_config_folder, bundle_hash)]

    @staticmethod
    def _server_hash(bundle: BifBundle, server: MediaServer | None, item_id: str | None) -> str | None:
        """Plex's bundle hash for this exact file, or ``None`` when it can't be trusted."""
        if not (bundle.trust_server_hash and server is not None and item_id):
            return None
        get_parts = getattr(server, "get_bundle_parts", None)
        if get_parts is None:
            return None
        size = (bundle.source_fingerprint or get_source_fingerprint(bundle.canonical_path))[2]
        name = os.path.basename(bundle.canonical_path)
        hashes = {
            h
            for h, remote, part_size in get_parts(item_id, quiet=True)
            if part_size == size and os.path.basename(remote.replace("\\", "/")) == name
        }
        # The hash becomes a directory name under the Plex config folder, so anything but 40 hex digits is distrusted.
        server_hash = hashes.pop() if len(hashes) == 1 else None
        return server_hash if server_hash and _BUNDLE_HASH_RE.fullmatch(server_hash) else None

    def publish(self, bundle: BifBundle, output_paths: list[Path], item_id: str | None = None) -> None:
        """Pack ``bundle.frame_dir`` into a BIF file at the first output path.

        Plex stores exactly one ``index-sd.bif`` per bundle, so we expect
        ``output_paths`` to have a single entry. Parent directories are
        created if missing.
        """
        if not output_paths:
            raise ValueError("PlexBundleAdapter.publish requires at least one output path")

        self._validate_source(bundle)
        index_bif = output_paths[0]
        indexes_dir = index_bif.parent
        try:
            indexes_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            logger.error(
                "Cannot create Plex bundle folder at {}: permission denied. "
                "Plex previews live under your Plex config folder — make sure that folder is "
                "mounted read-write (not :ro in Docker), the path in Settings → Media Servers "
                "matches your actual mount, and the user running this tool can write to it. "
                "Original error: {}",
                indexes_dir,
                exc,
            )
            raise

        # Delegate the actual byte-packing to the existing helper so we keep a
        # single source of truth for the BIF header layout.
        from ..processing.generator import generate_bif

        generate_bif(
            str(index_bif),
            str(bundle.frame_dir),
            BifIntervalConfig(self._frame_interval, server_display_name=bundle.server_display_name),
            before_publish=lambda: self._validate_source(bundle),
        )
        logger.debug("Plex BIF written to {}", index_bif)

    @staticmethod
    def bundle_bif_path(plex_config_folder: str, bundle_hash: str) -> Path:
        """Compute ``{plex}/Media/localhost/<h0>/<h[1:]>.bundle/Contents/Indexes/index-sd.bif``.

        Callers that already hold a bundle hash can compute its destination
        without constructing an adapter or contacting Plex.
        """
        bundle_file = sanitize_path(f"{bundle_hash[0]}/{bundle_hash[1:]}.bundle")
        bundle_path = sanitize_path(os.path.join(plex_config_folder, "Media", "localhost", bundle_file))
        indexes_path = sanitize_path(os.path.join(bundle_path, "Contents", "Indexes"))
        return Path(sanitize_path(os.path.join(indexes_path, "index-sd.bif")))

    @staticmethod
    def _validate_source(bundle: BifBundle) -> None:
        if (
            bundle.source_fingerprint is not None
            and get_source_fingerprint(bundle.canonical_path) != bundle.source_fingerprint
        ):
            raise SourceFileChangedError(f"Media source changed before publishing: {bundle.canonical_path}")


class BifIntervalConfig:
    """Minimal shim exposing :attr:`plex_bif_frame_interval` for ``generate_bif``.

    ``generate_bif`` consumes only ``plex_bif_frame_interval`` and (optionally)
    ``server_display_name`` from its config parameter; rather than passing a
    full :class:`Config` object we hand it this tiny adapter so the BIF
    packing helper can be reused without forcing callers to materialise an
    entire config.
    """

    def __init__(self, frame_interval: int, *, server_display_name: str | None = None) -> None:
        self.plex_bif_frame_interval = int(frame_interval)
        self.server_display_name = server_display_name
