"""Ordered import/deletion notices mapped into the selected server's paths."""

from __future__ import annotations

import os
from datetime import UTC, datetime

from ..servers.ownership import webhook_path_candidates


def _event_time(value: object) -> datetime | None:
    """Read an original webhook timestamp; unknown ordering cannot prove deletion."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def confirmed_deleted_paths(jobs, registry, server_id: str | None, *, event_times: dict[str, str]) -> frozenset[str]:
    """Retire only deletions strictly newer than every expected import of a path.

    Original preview webhooks carry the evidence, never loudness retry creation
    times. A batch may receive imports until its fire time, so imports use that
    conservative upper bound and deletions use the batch's creation lower bound.
    Once a batch fires, its start time bounds imports instead. The durable
    creation-time map is required: retry heads mutate the displayed timestamp.
    Equal or unknown import ordering keeps the missing-file retry contract.
    Callers still check that a path is absent now.
    """
    configs = [cfg for cfg in registry.configs() if not server_id or cfg.id == server_id]
    deleted: dict[str, datetime] = {}
    imported: dict[str, datetime] = {}
    for entry in jobs:
        if entry.kind != "previews":
            continue
        cfg = entry.config or {}
        if cfg.get("is_retry") and not cfg.get("is_retry_chain"):
            continue
        pin = cfg.get("server_id") or cfg.get("webhook_server_id")
        if pin and server_id and pin != server_id:
            continue
        candidates = [server for server in configs if not pin or server.id == pin]
        if not candidates:
            continue
        created = _event_time(event_times.get(entry.id))
        fire_value = cfg.get("webhook_fire_at")
        fire = _event_time(fire_value)
        started = _event_time(getattr(entry, "started_at", None))
        upper = fire if fire_value else started
        import_time = (
            max(created, upper) if created is not None and upper is not None else datetime.max.replace(tzinfo=UTC)
        )
        notices = [(cfg.get("webhook_paths"), imported, import_time)]
        if created is not None and str(cfg.get("source") or "").lower() in {"sonarr", "radarr"}:
            notices.append((cfg.get("webhook_deleted_paths"), deleted, created))
        for paths, latest, timestamp in notices:
            if not isinstance(paths, list):
                continue
            for path in paths:
                if not isinstance(path, str) or not os.path.isabs(path):
                    continue
                for value in webhook_path_candidates(path, candidates):
                    key = os.path.normpath(value)
                    latest[key] = max(timestamp, latest.get(key, timestamp))
    return frozenset(path for path, timestamp in deleted.items() if path not in imported or timestamp > imported[path])
