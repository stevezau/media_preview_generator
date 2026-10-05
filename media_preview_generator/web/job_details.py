"""What the Jobs page shows about a job beyond its own fields: the libraries it covers and the files it lists.

Both come from the saved server configs, the job's config and its file results; nothing here asks a server or touches
the disk.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable, Sequence

from ..servers.base import ServerConfig
from ..servers.ownership import OwnershipMatch, find_library_matches, webhook_path_candidates
from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict
from .jobs import Job

# A job's files looked up for its libraries: a webhook batch is one show or film, so its first files name them all.
_PATHS_FOR_LIBRARIES = 20


def saved_server_configs(entries: object) -> list[ServerConfig]:
    """The server configs in the ``media_servers`` setting, skipping a server type this app can't run.

    Args:
        entries: The setting's value.

    Returns:
        The configs, in settings order.
    """
    configs: list[ServerConfig] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            configs.append(server_config_from_dict(entry))
        except UnsupportedServerTypeError:
            continue
    return configs


def job_library_names(job: Job, configs: Sequence[ServerConfig]) -> list[str]:
    """The names of the libraries a job covers, where its config says: the libraries it was started on, else the
    libraries holding the files it lists.

    Args:
        job: The job.
        configs: The saved server configs.

    Returns:
        Distinct library names in the order found; empty when the job names neither libraries nor files (a whole-
        server scan, a re-check that lists its files when it runs) or none of them is in a saved library.
    """
    cfg = job.config or {}
    pin = str(cfg.get("server_id") or "") or None
    named = _started_on(cfg, job, configs, pin)
    if named:
        return _distinct(named)
    paths = [str(p) for p in [*(cfg.get("file_paths") or []), *(cfg.get("webhook_paths") or [])] if p]
    held = [
        match.library_name
        for path in paths[:_PATHS_FOR_LIBRARIES]
        for match in _library_matches(path, list(configs))
        if pin is None or match.server_id == pin
    ]
    return _distinct(held)


def job_library_scope(
    job: Job,
    configs: Sequence[ServerConfig],
    path_cache: dict[str, list[OwnershipMatch]] | None = None,
) -> list[dict[str, str]]:
    """Resolve saved job selections or requested paths to exact server/library pairs.

    This describes configured library association, not publication success. Source
    attribution on ``job.server_id`` is not a publication pin. Unknown or ambiguous
    legacy selections stay unknown. Only saved config and string paths are consulted.
    """
    cfg = job.config or {}
    pin = str(cfg.get("server_id") or "")
    servers = {server.id: server for server in configs}
    pairs: dict[tuple[str, str], dict[str, str]] = {}

    def add(server_id: str, library_id: str, library_name: str = "") -> None:
        if not server_id or not library_id or (pin and server_id != pin):
            return
        server = servers.get(server_id)
        library = next((lib for lib in server.libraries if str(lib.id) == library_id), None) if server else None
        pairs[(server_id, library_id)] = {
            "server_id": server_id,
            "server_name": server.name
            if server
            else (job.server_name if job.server_id == server_id else server_id) or server_id,
            "server_type": server.type.value
            if server
            else (job.server_type if job.server_id == server_id else "") or "",
            "library_id": library_id,
            "library_name": library.name if library else library_name or library_id,
        }

    selections = cfg.get("libraries") or []
    for pair in selections:
        if isinstance(pair, dict):
            add(str(pair.get("server_id") or ""), str(pair.get("library_id") or ""))
    if selections:
        return list(pairs.values())

    ids = [str(value) for value in cfg.get("selected_library_ids") or [] if value]
    if not ids and job.library_id:
        ids = [str(job.library_id)]
    selected_names = cfg.get("selected_libraries") or []
    selection_server = pin or str(job.server_id or "")
    for library_id in ids:
        if selection_server:
            add(selection_server, library_id)
        else:
            matches = [server for server in configs if any(str(lib.id) == library_id for lib in server.libraries)]
            if len(matches) == 1:
                add(matches[0].id, library_id)
    if ids:
        return list(pairs.values())
    if selected_names:
        for name in selected_names:
            matches = [
                (server, lib)
                for server in configs
                if not selection_server or server.id == selection_server
                for lib in server.libraries
                if lib.name == name
            ]
            if selection_server or len(matches) == 1:
                for server, library in matches:
                    add(server.id, str(library.id))
        return list(pairs.values())

    cache = path_cache if path_cache is not None else {}
    mapping_boundaries = {
        unicodedata.normalize("NFC", str(prefix)).replace("\\", "/").rstrip("/")
        for server in configs
        for mapping in server.path_mappings
        for prefix in [
            mapping.get("remote_prefix"),
            mapping.get("plex_prefix"),
            mapping.get("local_prefix"),
            *(mapping.get("webhook_prefixes") or []),
        ]
        if prefix
    }
    paths = [*(cfg.get("file_paths") or []), *(cfg.get("webhook_paths") or [])]
    paths.extend(
        (cfg.get("version_rerun_files") or {}).keys() if isinstance(cfg.get("version_rerun_files"), dict) else []
    )
    for path in dict.fromkeys(path for path in paths if isinstance(path, str) and path):
        # Sibling files share prefix matches. A requested folder that is itself a
        # mapping boundary may translate differently, so keep its exact key.
        normalized = unicodedata.normalize("NFC", path).replace("\\", "/")
        key = (
            "path:" + normalized
            if normalized.rstrip("/") in mapping_boundaries
            else "directory:" + os.path.dirname(normalized)
        )
        if key not in cache:
            cache[key] = _library_matches(path, list(configs))
        for match in cache[key]:
            add(match.server_id, match.library_id, match.library_name)
    return list(pairs.values())


def _library_matches(path: str, configs: list[ServerConfig]) -> list[OwnershipMatch]:
    """The libraries holding a file, whether the path is this app's or the sender's (Sonarr's ``/data/...``): the
    first of its local forms (``webhook_path_candidates``) that any library holds."""
    for candidate in webhook_path_candidates(path, configs):
        matches = find_library_matches(candidate, configs)
        if matches:
            return matches
    return []


def _started_on(cfg: dict, job: Job, configs: Sequence[ServerConfig], pin: str | None) -> list[str]:
    """Names of the libraries a job was started on: Intro & Credits ``libraries`` pairs, or a preview job's library
    ids on its server. Library ids repeat across servers, so an id with no known server names a library only when one
    server has it."""
    names: list[str] = []
    for pair in cfg.get("libraries") or []:
        if isinstance(pair, dict):
            names.extend(_names_of(configs, str(pair.get("library_id") or ""), str(pair.get("server_id") or "")))
    if names:
        return names
    ids = [str(i) for i in cfg.get("selected_library_ids") or [] if i]
    if not ids and job.library_id:
        ids = [str(job.library_id)]
    server_id = pin or str(job.server_id or "")
    for library_id in ids:
        names.extend(_names_of(configs, library_id, server_id))
    return names


def _names_of(configs: Sequence[ServerConfig], library_id: str, server_id: str) -> list[str]:
    if not library_id:
        return []
    found = [
        lib.name
        for cfg in configs
        if not server_id or cfg.id == server_id
        for lib in cfg.libraries
        if str(lib.id) == library_id and lib.name
    ]
    return found if server_id or len(found) == 1 else []


def _distinct(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        key = name.strip().casefold()
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())
    return out


def job_file_list(job: Job, results: Iterable[dict], limit: int) -> tuple[list[dict], int]:
    """The first files of a job for its row on the Jobs page, each with the title its job log names it by.

    The files the job was given come first, in order (a webhook batch, a Season, the batch a re-check holds), then the
    files it has run that it wasn't given (a library scan lists its files as it goes).

    Args:
        job: The job.
        results: Its file results (``JobManager.get_file_results``).
        limit: The most files returned.

    Returns:
        ``([{"title", "name", "path"}], total)``: the total counts every file the job lists or has run, or its
        progress total when that is larger.
    """
    from ..markers.job_log import file_title
    from ..markers.titles import TITLE_CACHE

    cfg = job.config or {}
    held = cfg.get("version_rerun_files")
    given = [str(p) for p in cfg.get("file_paths") or [] if p]
    given += [str(p) for p in (held if isinstance(held, dict) else {})]
    ran = [str(r.get("file") or "") for r in results]
    ordered = [p for p in dict.fromkeys([*given, *ran]) if p]
    total = max(len(ordered), int(job.progress.total_items or 0))
    files = []
    for path in ordered[: max(0, limit)]:
        server_title, year = TITLE_CACHE.get(path) or (None, None)
        name = os.path.basename(path.rstrip("/")) or path
        files.append({"title": file_title(path, server_title, year), "name": name, "path": path})
    return files, total
