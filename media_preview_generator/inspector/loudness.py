"""Read native Plex loudness metadata for the Inspector's exact file, without a database or analysis job."""

from __future__ import annotations

import math
import os
import time
import unicodedata
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, cast

from loguru import logger

from ..loudness.analyze import ANALYSIS_VERSION, valid_measurements
from ..loudness.settings import loudness_matches
from ..output.plex_hash import get_source_fingerprint
from ..servers.base import ServerConfig, ServerType
from ..servers.ownership import OwnershipMatch, apply_path_mappings
from . import previews

_FIELDS = {
    "loudness": "integrated_lufs",
    "peak": "true_peak_dbtp",
    "lra": "lra_lu",
    "threshold": "threshold_lufs",
    "gainOffset": "gain_offset_db",
}


def _row(cfg: ServerConfig, state: str, note: str | None = None) -> dict:
    return {
        "server_id": cfg.id,
        "server_name": cfg.name,
        "server_type": cfg.type.value,
        "enabled": False,
        "source": "plex_api" if cfg.type is ServerType.PLEX else None,
        "source_note": "Reported by Plex" if cfg.type is ServerType.PLEX else None,
        "state": state,
        "note": note,
        "streams": [],
    }


def _path(path: str) -> str:
    return unicodedata.normalize("NFC", os.path.normpath(path.replace("\\", "/")))


def _number(raw: Any, *, allow_infinity: bool) -> float | str | None:
    try:
        number = float(raw)
    except (ValueError, TypeError):
        return None
    if math.isfinite(number):
        return number
    if allow_infinity and math.isinf(number):
        return "-inf" if number < 0 else "inf"
    return None


def _integer(raw: Any) -> int | None:
    try:
        value = int(raw)
        return value if value >= 0 else None
    except (ValueError, TypeError):
        return None


def _stream(audio: Any, part: Any, item_id: str) -> dict:
    fields = {f"ln:{key}": audio.get(key) for key in _FIELDS}
    valid = valid_measurements(fields)
    version = audio.get("loudnessAnalysisVersion")
    flag = audio.get("canNormalizeLoudness")
    normalization = {"1": True, "0": False}.get(flag)
    present = any(value is not None for value in fields.values()) or version is not None or flag not in (None, "0")
    state: str
    note: str | None
    state, note = "not_analysed", "Plex has no loudness measurements for this audio stream."
    if version and version != ANALYSIS_VERSION:
        state, note = "unsupported", "Plex reports an unsupported loudness analysis version."
    elif present:
        state, note = (
            "partial",
            "Plex has incomplete or invalid loudness metadata, or Plex cannot normalize this stream.",
        )
        if valid and version == ANALYSIS_VERSION and normalization is True:
            state, note = "available", None
            if any(not math.isfinite(float(value)) for value in fields.values()):
                note = "Silent or very short audio: Plex reports infinite loudness or gain values."
    row = {
        "item_id": item_id,
        "part_id": part.get("id"),
        "part_file": (part.get("file") or "").replace("\\", "/").rsplit("/", 1)[-1],
        "stream_id": audio.get("id"),
        "index": _integer(audio.get("index")),
        "codec": audio.get("codec"),
        "language": audio.get("language") or audio.get("languageCode"),
        "channels": _integer(audio.get("channels")),
        "title": audio.get("displayTitle") or audio.get("title"),
        "default": {"1": True, "0": False}.get(audio.get("default")),
        "state": state,
        "note": note,
        "analysis_version": version,
        "normalization_available": normalization,
    }
    row.update({name: _number(audio.get(key), allow_infinity=valid) for key, name in _FIELDS.items()})
    return row


def _server_loudness(
    cfg: ServerConfig, server: Any, path: str, matches: list[OwnershipMatch], item_id: str | None
) -> dict:
    try:
        before = get_source_fingerprint(path)
    except OSError:
        return _row(cfg, "unavailable", "This file can't be read from disk.")
    if not item_id:
        item_id = server.resolve_remote_path_to_item_id(path, library_ids=[m.library_id for m in matches])
    # Only a Plex rating key can form the metadata URL, including when resolution returned its API path.
    item_id = str(item_id or "").rstrip("/").rsplit("/", 1)[-1]
    if not item_id.isascii() or not item_id.isdigit():
        return _row(cfg, "unavailable", "Plex does not list this file yet.")
    metadata = server._connect().query(f"/library/metadata/{item_id}")
    items = [item for item in metadata.findall("Video") if item.get("ratingKey") == item_id]
    if len(items) != 1:
        return _row(cfg, "unavailable", "Plex did not return the requested item.")
    parts = [
        part
        for part in items[0].findall("Media/Part")
        if part.get("file")
        and any(_path(local) == _path(path) for local in apply_path_mappings(part.get("file"), cfg.path_mappings or []))
    ]
    if not parts:
        return _row(cfg, "unavailable", "Plex did not return an exact matching file part.")
    if any(part.get("size") is not None and _integer(part.get("size")) != before[2] for part in parts):
        return _row(cfg, "unavailable", "Plex's indexed file size differs from this file. Rescan it in Plex.")
    streams = []
    stream_ids = set()
    part_indexes = set()
    for part in parts:
        for audio in part.findall("Stream"):
            if audio.get("streamType") != "2":
                continue
            identity = (part.get("id"), audio.get("id"), _integer(audio.get("index")))
            if (
                not all(value is not None and value != "" for value in identity)
                or identity[1] in stream_ids
                or (identity[0], identity[2]) in part_indexes
            ):
                return _row(cfg, "unavailable", "Plex returned missing or ambiguous audio stream identities.")
            stream_ids.add(identity[1])
            part_indexes.add((identity[0], identity[2]))
            streams.append(_stream(audio, part, item_id))
    if not streams:
        return _row(cfg, "unavailable", "Plex has not exposed audio streams for this file.")
    try:
        after = get_source_fingerprint(path)
    except OSError:
        after = None
    if after != before:
        return _row(cfg, "unavailable", "This file changed while its loudness metadata was being read.")
    states = {stream["state"] for stream in streams}
    state = next(iter(states)) if len(states) == 1 else "partial"
    row = _row(cfg, state)
    row["streams"] = streams
    return row


def file_loudness(
    canonical_path: str,
    owners: list[tuple[ServerConfig, Any, list[OwnershipMatch]]],
    preview_rows: list[dict],
    *,
    timeout_s: float = previews.LOOKUP_TIMEOUT_S,
) -> list[dict]:
    """Read each owning server with one shared deadline and the Inspector's existing bounded lookup pool.

    Args:
        canonical_path: Validated local file path.
        owners: Live clients and matching libraries already resolved by the Inspector.
        preview_rows: Existing preview lookups, reused for their item IDs and connection failures.
        timeout_s: Total wait budget for all servers together.

    Returns:
        Server rows with exact audio stream identities, native values and explicit verification states.
        Infinity uses JSON strings only for Plex's recognized silent/short audio representation.
    """
    by_server = {row["server_id"]: row for row in preview_rows}
    pending: list[tuple[ServerConfig, Any, dict | None]] = []
    for cfg, server, matches in owners:
        preview = by_server.get(cfg.id, {})
        if cfg.type is not ServerType.PLEX:
            pending.append((cfg, None, _row(cfg, "unsupported", "Loudness measurements are available for Plex only.")))
        elif previews.is_resting(cfg.id):
            pending.append((cfg, None, _row(cfg, "unavailable", "Could not reach Plex to read loudness measurements.")))
        else:
            future = previews.submit_lookup(
                _server_loudness, cfg, server, canonical_path, matches, preview.get("item_id")
            )
            pending.append((cfg, future, None))
    deadline = time.monotonic() + timeout_s
    rows: list[dict] = []
    for cfg, future, ready in pending:
        if future is None:
            rows.append(cast(dict, ready))
            continue
        try:
            rows.append(future.result(timeout=max(0.0, deadline - time.monotonic())))
        except FutureTimeoutError:
            future.cancel()
            previews.mark_resting(cfg.id)
            rows.append(_row(cfg, "unavailable", "Plex took too long to return loudness measurements."))
        except Exception as exc:
            logger.debug("Inspector loudness lookup on {} failed: {}", cfg.name, type(exc).__name__)
            rows.append(_row(cfg, "unavailable", "Could not read loudness measurements from Plex."))
    for row, (cfg, _server, _matches) in zip(rows, owners, strict=True):
        row["enabled"] = bool(loudness_matches(canonical_path, [cfg]))
    return rows
