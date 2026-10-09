"""Retry accounting independent of the Files panel's truncated history."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any


def previous_result(path: str, outcome: str, servers: list[dict] | None) -> dict:
    """Retain only the per-file fields needed to replace aggregate counts on a retry."""
    return {
        "file": path,
        "outcome": outcome,
        "servers": [
            {
                "id": row.get("server_id", row.get("id", "")),
                "name": row.get("server_name", row.get("name", "")),
                "type": row.get("server_type", row.get("type", "")),
                "status": row.get("status", ""),
            }
            for row in servers or []
        ],
    }


def snapshot(head, previous: dict[str, dict]) -> dict:
    """Capture immutable totals and the files this attempt will replace."""
    return {
        "outcome": dict(head.progress.outcome or {}),
        "publishers": deepcopy(head.publishers or []),
        "files": deepcopy(previous),
    }


def replace_results(baseline: dict, rows: list[dict], sender_paths: dict[str, str]) -> tuple[dict, list[dict]]:
    """Apply each latest retry result once to its immutable pre-attempt totals.

    Original sender paths identify files when a previously missing source resolves through a different mount.
    Rows outside the recorded retry batch cannot change the original job's counts.
    """
    outcome = Counter(baseline["outcome"])
    publishers = {row["server_id"]: deepcopy(row) for row in baseline["publishers"]}
    previous = baseline["files"]
    # Current attempts persist the original file identity even when a mount moves.
    # The explicit mapping remains for retry rows written by earlier versions.
    original_paths = {result["file"]: sender for sender, result in previous.items()}
    latest = {}
    for row in rows:
        path: Any = row.get("file")
        key = sender_paths.get(path, original_paths.get(path, path))
        if path and key in previous:
            latest[key] = row
    for key, current in latest.items():
        before = previous[key]
        outcome[before["outcome"]] -= 1
        outcome[current["outcome"]] += 1
        for sign, result in [(-1, before), (1, current)]:
            for server in result.get("servers", []):
                sid = server["id"]
                aggregate = publishers.setdefault(
                    sid,
                    {"server_id": sid, "server_name": server["name"], "server_type": server["type"], "counts": {}},
                )
                counts = aggregate["counts"]
                status = server["status"]
                counts[status] = counts.get(status, 0) + sign
    for publisher in publishers.values():
        publisher["counts"] = {key: count for key, count in publisher["counts"].items() if count > 0}
    return (
        {key: count for key, count in outcome.items() if count > 0},
        [row for row in publishers.values() if row["counts"]],
    )
