"""What the Jobs page shows about a job beyond its own fields: the libraries it covers and the files it lists.

Both come from saved settings, job selections and file results. Large loudness
selections are read from their private input manifests; media files are not opened.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Iterable, Sequence

from loguru import logger

from ..servers.base import ServerConfig
from ..servers.ownership import OwnershipMatch, apply_path_mappings, find_library_matches, webhook_path_candidates
from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict
from .jobs import Job, JobStatus, is_user_visible_job

# A job's files looked up for its libraries: a webhook batch is one show or film, so its first files name them all.
_PATHS_FOR_LIBRARIES = 20
LibraryIndex = list[list[tuple[str, OwnershipMatch]]]


def job_has_chapter_warning(job: Job) -> bool:
    """Identify finished preview heads that reported incomplete chapter outputs."""
    if (
        job.kind != "previews"
        or job.status not in (JobStatus.COMPLETED, JobStatus.FAILED)
        or not is_user_visible_job(job)
    ):
        return False
    for publisher in job.publishers or []:
        if not isinstance(publisher, dict):
            continue
        chapters = publisher.get("chapter_counts")
        if isinstance(chapters, dict) and chapters:
            counts = (chapters.get(key) for key in ("failed", "waiting", "incomplete"))
        else:
            legacy = publisher.get("counts")
            if not isinstance(legacy, dict):
                continue
            counts = (legacy.get(key) for key in ("published_chapters_failed", "published_pending_chapters"))
        if any(type(count) is int and count > 0 for count in counts):
            return True
    return False


def job_file_paths(job: Job) -> list[str]:
    """Read inline or durable selected paths for job details and Inspector matching."""
    cfg = job.config or {}
    if not cfg.get("file_paths_ref"):
        return list(cfg.get("file_paths") or [])
    from ..loudness.inputs import read_file_paths
    from .jobs import get_job_manager

    try:
        return read_file_paths(get_job_manager().config_dir, cfg)
    except (OSError, ValueError):
        logger.warning("Could not read the selected files for loudness job {}", job.id)
        return []


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


def job_library_names(
    job: Job, configs: Sequence[ServerConfig], requested_paths: Sequence[str] | None = None
) -> list[str]:
    """The names of the libraries a job covers, where its config says: the libraries it was started on, else the
    libraries holding the files it lists.

    Args:
        job: The job.
        configs: The saved server configs.
        requested_paths: The job's file paths when the caller already loaded them; read from the job when ``None``.

    Returns:
        Distinct library names in the order found; empty when the job names neither libraries nor files (a whole-
        server scan, a re-check that lists its files when it runs) or none of them is in a saved library.
    """
    cfg = job.config or {}
    pin = str(cfg.get("server_id") or "") or None
    named = _started_on(cfg, job, configs, pin)
    if named:
        return _distinct(named)
    given = job_file_paths(job) if requested_paths is None else requested_paths
    paths = [str(p) for p in [*given, *(cfg.get("webhook_paths") or [])] if p]
    index = _library_index(configs)
    held = [
        match.library_name
        for path in paths[:_PATHS_FOR_LIBRARIES]
        for match in _library_matches(path, list(configs), index)
        if pin is None or match.server_id == pin
    ]
    return _distinct(held)


def job_library_scope(
    job: Job,
    configs: Sequence[ServerConfig],
    path_cache: dict[str, list[OwnershipMatch]] | None = None,
    requested_paths: Sequence[str] | None = None,
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
        if (server_id, library_id) in pairs:
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
            name_matches = [
                (server, lib)
                for server in configs
                if not selection_server or server.id == selection_server
                for lib in server.libraries
                if lib.name == name
            ]
            if selection_server or len(name_matches) == 1:
                for server, library in name_matches:
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
    given = job_file_paths(job) if requested_paths is None else requested_paths
    paths = [*given, *(cfg.get("webhook_paths") or [])]
    paths.extend(
        (cfg.get("version_rerun_files") or {}).keys() if isinstance(cfg.get("version_rerun_files"), dict) else []
    )
    index = _library_index(configs)
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
            cache[key] = _library_matches(path, list(configs), index)
        for match in cache[key]:
            add(match.server_id, match.library_id, match.library_name)
    return list(pairs.values())


def _library_index(configs: Sequence[ServerConfig]) -> LibraryIndex:
    """Translate library roots once instead of repeating that work for every file."""
    index: LibraryIndex = []
    for server in configs:
        if not server.enabled:
            continue
        for library in server.libraries:
            prefixes = []
            for remote in library.remote_paths:
                if not (remote or "").strip():
                    continue
                for local in apply_path_mappings(remote, server.path_mappings):
                    if (local or "").strip():
                        prefix = unicodedata.normalize("NFC", local.replace("\\", "/").rstrip("/")) + "/"
                        prefixes.append((prefix, OwnershipMatch(server.id, library.id, library.name, local)))
            index.append(prefixes)
    return index


def _library_matches(path: str, configs: list[ServerConfig], index: LibraryIndex | None = None) -> list[OwnershipMatch]:
    """The libraries holding a file, whether the path is this app's or the sender's (Sonarr's ``/data/...``): the
    first of its local forms (``webhook_path_candidates``) that any library holds."""
    if index is not None:
        matches = _indexed_matches(path, index)
        if matches:
            return matches
    candidates = webhook_path_candidates(path, configs)
    for candidate in candidates if index is None else candidates[1:]:
        matches = find_library_matches(candidate, configs) if index is None else _indexed_matches(candidate, index)
        if matches:
            return matches
    return []


def _indexed_matches(path: str, index: LibraryIndex) -> list[OwnershipMatch]:
    """Match canonical paths before constructing unnecessary sender aliases."""
    path = unicodedata.normalize("NFC", path).replace("\\", "/")
    normalized = os.path.dirname(path).rstrip("/") + "/" + os.path.basename(path)
    matches = []
    for prefixes in index:
        for prefix, match in prefixes:
            if normalized.startswith(prefix):
                matches.append(match)
                break
    return matches


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
    given = [str(p) for p in job_file_paths(job) if p]
    given += [str(p) for p in (held if isinstance(held, dict) else {})]
    ran = [str(r.get("file") or "") for r in results]
    ordered = [p for p in dict.fromkeys([*given, *ran]) if p]
    stored_count = cfg.get("file_paths_count")
    total = max(len(ordered), int(job.progress.total_items or 0), stored_count if type(stored_count) is int else 0)
    files = []
    for path in ordered[: max(0, limit)]:
        server_title, year = TITLE_CACHE.get(path) or (None, None)
        name = os.path.basename(path.rstrip("/")) or path
        files.append({"title": file_title(path, server_title, year), "name": name, "path": path})
    return files, total
