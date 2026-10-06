"""Immutable selected-file lists kept out of per-file job updates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

INLINE_FILE_LIMIT = 500


def _path(config_dir: str | Path, reference: str) -> Path:
    if not isinstance(reference, str) or not re.fullmatch(r"[0-9a-f]{64}\.json", reference):
        raise ValueError("Invalid loudness file-list reference")
    return Path(config_dir) / "loudness_inputs" / reference


def store_file_paths(config_dir: str | Path, config: dict) -> dict:
    """Save a large selection once; callers serialize creation with job deletion.

    Content-addressed inputs survive job clones and retries without becoming
    mutable shared state. Small selections retain the existing inline format.
    """
    paths = config.get("file_paths") or []
    if config.get("file_paths_ref") or len(paths) <= INLINE_FILE_LIMIT:
        return config
    if not isinstance(paths, list) or any(not isinstance(path, str) or not path for path in paths):
        raise ValueError("Invalid loudness file selection")
    data = {"version": 1, "paths": paths}
    if config.get("retry_baseline") is not None:
        data["retry_baseline"] = config["retry_baseline"]
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    reference = hashlib.sha256(payload).hexdigest() + ".json"
    destination = _path(config_dir, reference)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
            temporary = output.name
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        temporary = None
        directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    stored = {**config, "file_paths": [], "file_paths_ref": reference, "file_paths_count": len(paths)}
    if "retry_baseline" in data:
        stored.pop("retry_baseline")
        stored["retry_baseline_in_input"] = True
    return stored


def _read_input(config_dir: str | Path, config: dict) -> dict:
    reference = config.get("file_paths_ref")
    path = _path(config_dir, reference)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        payload = source.read()
    if hashlib.sha256(payload).hexdigest() + ".json" != reference:
        raise ValueError("Loudness file selection has changed")
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Invalid loudness file selection format")
    paths = data.get("paths")
    count = config.get("file_paths_count")
    if (
        not isinstance(paths, list)
        or any(not isinstance(path, str) or not path for path in paths)
        or type(count) is not int
        or count != len(paths)
    ):
        raise ValueError("Loudness file selection does not match its job")
    return data


def read_file_paths(config_dir: str | Path, config: dict) -> list[str]:
    """Read the exact selection, failing closed if its durable input is missing or corrupt."""
    if config.get("file_paths_ref") is None:
        return list(config.get("file_paths") or [])
    return _read_input(config_dir, config)["paths"]


def load_file_input(config_dir: str | Path, config: dict) -> dict:
    """Expand immutable input into a runner-local config, never a persisted progress update."""
    if config.get("file_paths_ref") is None:
        return config
    data = _read_input(config_dir, config)
    expanded = {**config, "file_paths": data["paths"]}
    if config.get("retry_baseline_in_input"):
        baseline = data.get("retry_baseline")
        if (
            not isinstance(baseline, dict)
            or not isinstance(baseline.get("outcome"), dict)
            or not isinstance(baseline.get("publishers"), list)
            or not isinstance(baseline.get("files"), dict)
        ):
            raise ValueError("Invalid loudness retry accounting")
        expanded["retry_baseline"] = baseline
    return expanded


def delete_file_paths(config_dir: str | Path, reference: str) -> None:
    """Remove an input only after its last referencing job was durably deleted."""
    _path(config_dir, reference).unlink(missing_ok=True)
