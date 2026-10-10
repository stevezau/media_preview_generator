"""Read-only access to markers.db for the health check."""

from __future__ import annotations

import os
import sqlite3
import urllib.parse

from loguru import logger

from .models import Feature

_NOTHING_FOUND_SQL = (
    "SELECT f.canonical_path, d.type FROM files f JOIN decisions d ON d.file_id = f.id "
    "WHERE d.type IN ('intro','credits') AND d.status IN ('no_evidence','needs_review')"
)


def markers_db_path() -> str:
    """Where the live app keeps markers.db (same rule as ``get_marker_store``)."""
    from ..web.auth import get_config_dir

    return os.path.join(get_config_dir(), "markers.db")


def nothing_found(markers_db_path: str) -> dict[str, set[Feature]]:
    """Files whose intro/credits search ended with no marker.

    Args:
        markers_db_path: Path to markers.db.

    Returns:
        Canonical path -> the INTRO and/or CREDITS features with a no_evidence/needs_review decision.
        Empty when the file is missing or unreadable.
    """
    if not os.path.isfile(markers_db_path):
        logger.debug("markers.db not found at {}; no 'nothing found' counts", markers_db_path)
        return {}
    found: dict[str, set[Feature]] = {}
    try:
        # mode=ro so the health check can never write to or lock-upgrade the live markers database
        conn = sqlite3.connect(
            f"file:{urllib.parse.quote(os.path.abspath(markers_db_path))}?mode=ro", uri=True, timeout=5
        )
        try:
            for path, kind in conn.execute(_NOTHING_FOUND_SQL):
                found.setdefault(path, set()).add(Feature(kind))
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.debug("Could not read markers.db at {}: {}", markers_db_path, exc)
        return {}
    return found
