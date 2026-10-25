"""Test access: can a node see the app's media through its folder mappings (spec §10.4)?

The app samples a few real video files from every enabled library, as the app sees them, and asks the node to find
each one through the mappings (same size and ``mtime_ns``, so a different file at the same path doesn't pass). When
files are missing, the folder listing the node sends back is used to suggest a corrected mapping row.
"""

from __future__ import annotations

import os
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from .client import NodeClient
from .protocol import NOT_VISIBLE_MISSING, PathMapping, map_app_path
from .settings import MAX_MAPPINGS

SAMPLES_PER_ROOT = 3
# Directory entries looked at per library folder before giving up: a library whose first 500 entries hold no video
# reports "no video files found" rather than walking a whole NAS.
WALK_LIMIT = 500
# Subfolders queued per folder: a library of thousands of film folders is sampled from its first ones instead of
# spending the whole walk listing the top folder.
SUBFOLDERS_PER_FOLDER = 16
# NAS housekeeping folders (Synology, QNAP, Windows), compared casefolded; they hold no library files.
NAS_FOLDERS = frozenset(
    {
        "#recycle",
        "#snapshot",
        "@eadir",
        "@recycle",
        "@recently-snapshot",
        "$recycle.bin",
        "lost+found",
        "system volume information",
    }
)
# Files per access-check request; the node checks each one on disk.
CHECK_BATCH = 30
# The longest Test access spends reading this app's own library folders. A stalled network mount blocks a directory
# read in the kernel where no timeout reaches it, so each library is read on its own thread and one that isn't done
# by then is reported as timed out; the request answers with what the others found.
SAMPLE_BUDGET_S = 20.0
# The longest Add node waits to learn which library folders this app can open, before it suggests rows.
SUGGEST_BUDGET_S = 3.0


def _video_extensions() -> frozenset[str]:
    from ..plex_client import VIDEO_EXTENSIONS

    return VIDEO_EXTENSIONS


@dataclass(frozen=True)
class LibraryRoot:
    """One library folder as this app sees it."""

    server: str
    server_id: str
    library: str
    library_id: str
    app_root: str
    # The server's folder this one was mapped from: a multi-disk setup maps one to several of this app's folders.
    remote: str = ""


def library_roots(servers: list[dict]) -> list[LibraryRoot]:
    """Every enabled library folder of every enabled server, resolved to this app's own paths.

    Args:
        servers: The stored ``media_servers`` list.

    Returns:
        One entry per distinct (server, library, folder), in settings order.
    """
    from ..servers.ownership import apply_path_mappings
    from ..servers.registry import UnsupportedServerTypeError, server_config_from_dict

    out: list[LibraryRoot] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in servers or []:
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        try:
            cfg = server_config_from_dict(entry)
        except UnsupportedServerTypeError:
            continue
        for lib in cfg.libraries:
            if not lib.enabled:
                continue
            for remote in lib.remote_paths:
                for local in apply_path_mappings(remote, cfg.path_mappings):
                    root = local.rstrip("/") or "/"
                    key = (cfg.id, lib.id, root)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(LibraryRoot(cfg.name, cfg.id, lib.name, lib.id, root, remote))
    return out


@dataclass
class _Sampling:
    """One library folder's sampling, shared with the thread doing it (which may still be stuck in a read)."""

    found: list[dict] = field(default_factory=list)
    folder: str = ""
    timed_out: bool = False
    opened: bool = False
    done: threading.Event = field(default_factory=threading.Event)


def sample_files(root: str, *, limit: int = SAMPLES_PER_ROOT, walk_limit: int = WALK_LIMIT) -> list[dict]:
    """Up to ``limit`` video files under ``root``, breadth first, with their size and ``mtime_ns``.

    Args:
        root: A folder as this app sees it.
        limit: Files wanted.
        walk_limit: Directory entries looked at before stopping.

    Returns:
        ``[{"path", "size", "mtime_ns"}]``; empty when the folder is missing or holds no video in reach.
    """
    sampling = _Sampling()
    _sample_into(sampling, root, limit=limit, walk_limit=walk_limit, deadline=None)
    return sampling.found


def _sample_into(sampling: _Sampling, root: str, *, limit: int, walk_limit: int, deadline: float | None) -> None:
    """:func:`sample_files` into ``sampling``, stopping (``timed_out``) at ``deadline`` between directory reads."""
    extensions = _video_extensions()
    found = sampling.found
    seen = 0
    queue: deque[str] = deque([root])
    # Libraries are often built from symlinked show folders, so links are followed; a link back into a folder already
    # walked through a link is skipped, which ends a loop after one turn (walk_limit bounds the rest).
    linked: set[str] = {os.path.realpath(root)}
    while queue and len(found) < limit and seen < walk_limit:
        if deadline is not None and time.monotonic() >= deadline:
            sampling.timed_out = True
            return
        folder = queue.popleft()
        sampling.folder = folder
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        sampling.opened = True
        queued_here = 0
        for entry in entries:
            if len(found) >= limit:
                break
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            # Hidden folders and the NAS housekeeping ones hold no library files; "#Alive (2020)" is a film.
            if is_dir and (
                entry.name.startswith(".")
                or entry.name.casefold() in NAS_FOLDERS
                or queued_here >= SUBFOLDERS_PER_FOLDER
            ):
                continue
            seen += 1
            if seen > walk_limit:
                break
            try:
                if is_dir:
                    queued_here += 1
                    if entry.is_symlink():
                        real = os.path.realpath(entry.path)
                        if real in linked:
                            continue
                        linked.add(real)
                    queue.append(entry.path)
                    continue
                if os.path.splitext(entry.name)[1].lower() not in extensions:
                    continue
                stat = entry.stat()
            except OSError:
                continue
            found.append({"path": entry.path, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})


def _sample_folders(folders: list[str], budget_s: float, *, limit: int) -> list[_Sampling]:
    """Sample every folder at once, giving up on the ones not done within ``budget_s``; one result per folder.

    Daemon threads, not a pool: a thread stuck in a read on a stalled mount must neither hold up the answer nor the
    app's exit.
    """
    deadline = time.monotonic() + budget_s
    samplings: list[_Sampling] = []
    for folder in folders:
        sampling = _Sampling()
        samplings.append(sampling)

        def run(sampling: _Sampling = sampling, folder: str = folder) -> None:
            try:
                _sample_into(sampling, folder, limit=limit, walk_limit=WALK_LIMIT, deadline=deadline)
            finally:
                sampling.done.set()

        threading.Thread(target=run, name="test-access-sample", daemon=True).start()
    for sampling in samplings:
        if not sampling.done.wait(max(0.0, deadline - time.monotonic())):
            sampling.timed_out = True
    return samplings


def _rows_inside(app_root: str, mappings: list[PathMapping]) -> list[str]:
    """The folders of rows inside ``app_root`` but not covering it: a node with only some of a library's folders.

    The node runs those folders' files (a file is matched by its own path), so they are the ones to test.
    """
    if _covering_row(app_root, mappings):
        return []
    base = app_root.rstrip("/") + "/"
    return sorted({row.app_prefix.rstrip("/") for row in mappings if row.app_prefix.rstrip("/").startswith(base)})


def _inner_row_problem(folder: str, sampling: _Sampling) -> str | None:
    """Why a row inside a library fails it before the node is asked: this app read no file from the row's folder.

    Test access and the quick access check share this rule, so they agree on such a library.
    """
    if sampling.found:
        return None
    if sampling.timed_out:
        return f"Timed out reading {folder} (no answer within {SAMPLE_BUDGET_S:g} s)"
    if not sampling.opened:
        return f"This app can't open {folder}"
    return None


def _interleave(lists: list[list[dict]], limit: int) -> list[dict]:
    """Files taken from each list in turn, each path once, up to ``limit``: every row inside a library gets tested."""
    picked: dict[str, dict] = {}
    for rank in range(max((len(files) for files in lists), default=0)):
        for files in lists:
            if rank < len(files) and len(picked) < limit:
                picked.setdefault(files[rank]["path"], files[rank])
    return list(picked.values())


def _covering_row(app_root: str, mappings: list[PathMapping]) -> PathMapping | None:
    best: PathMapping | None = None
    for row in mappings:
        prefix = row.app_prefix.rstrip("/") or "/"
        covers = app_root == prefix or app_root.startswith(prefix.rstrip("/") + "/")
        if covers and (best is None or len(prefix) > len(best.app_prefix.rstrip("/"))):
            best = row
    return best


def _node_sep(path: str) -> str:
    return "\\" if "\\" in path or (len(path) >= 2 and path[1] == ":") else "/"


def _join(folder: str, name: str) -> str:
    sep = _node_sep(folder)
    return folder if not name else (folder if folder.endswith(sep) else folder + sep) + name


def _parent(path: str, levels: int) -> str:
    sep = _node_sep(path)
    for _ in range(levels):
        trimmed = path.rstrip(sep)
        cut = trimmed.rfind(sep)
        if cut <= 0:
            return trimmed[: cut + 1] if cut == 0 else trimmed
        path = trimmed[:cut]
        if len(path) == 2 and path[1] == ":":
            path += sep
    return path


def _caseless(name: str) -> str:
    """``name`` for a case- and Unicode-form-insensitive compare (macOS and SMB often store names decomposed)."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", name).casefold())


def _closest_name(wanted: str, names: list) -> str | None:
    """The name in ``names`` that is ``wanted``: exactly, then ignoring case, then ignoring case and Unicode form."""
    candidates = [n for n in names if isinstance(n, str)]
    for key in (lambda n: n, str.casefold, _caseless):
        target = key(wanted)
        match = next((n for n in candidates if key(n) == target), None)
        if match is not None:
            return match
    return None


def _same_node_path(a: str, b: str) -> bool:
    """Whether two node paths name one folder (Windows ignores case)."""
    sep = _node_sep(a)
    a, b = a.rstrip("/\\") or a, b.rstrip("/\\") or b
    return a.casefold() == b.casefold() if sep == "\\" else a == b


def _node_root(app_root: str, mappings: list[PathMapping]) -> str | None:
    """Where the rows put ``app_root`` on the node."""
    row = _covering_row(app_root, mappings)
    return None if row is None else map_app_path(app_root, mappings, node_sep=_node_sep(row.node_prefix))


def suggest_row(
    app_root: str, mappings: list[PathMapping], nearby: object, *, taken: Collection[str] = ()
) -> dict | None:
    """A corrected ``node_prefix`` for the row covering ``app_root``, from the node's folder listings.

    When a listing on the node holds a folder named like the app root's last component (ignoring case and Unicode
    form), that folder is where the node sees the root, so the row's ``node_prefix`` moves to match. The moved row
    must then put ``app_root`` exactly on that folder: when the root sits below the row's folder, the folders between
    keep the app's names (and their case, except on Windows), or the row would point the library somewhere else.

    Args:
        app_root: The library folder as this app sees it.
        mappings: The rows tested.
        nearby: The node's ``{node_dir: [names]}`` listings.
        taken: Other libraries' folders on the node; a library is never found in another one's folder.

    Returns:
        ``{"from", "to"}`` (the row's current and suggested ``node_prefix``), or None.
    """
    row = _covering_row(app_root, mappings)
    if row is None or not isinstance(nearby, dict):
        return None
    prefix = row.app_prefix.rstrip("/") or "/"
    rest = [part for part in app_root[len(prefix) :].split("/") if part]
    last = rest[-1] if rest else app_root.rstrip("/").rsplit("/", 1)[-1]
    if not last:
        return None
    for node_dir, names in nearby.items():
        if not isinstance(node_dir, str) or not isinstance(names, list):
            continue
        match = _closest_name(last, names)
        if match is None:
            continue
        seen_root = _join(node_dir, match)
        if any(_same_node_path(seen_root, other) for other in taken):
            continue
        # The row maps app_prefix, not app_root: step back up as many folders as app_root sits below app_prefix.
        new_prefix = _parent(seen_root, len(rest)) if rest else seen_root
        mapped = new_prefix
        for part in rest:
            mapped = _join(mapped, part)
        if not _same_node_path(mapped, seen_root):
            continue
        if new_prefix.rstrip("/\\") != row.node_prefix.rstrip("/\\"):
            return {"from": row.node_prefix, "to": new_prefix}
    return None


def _under(path: str, folder: str) -> bool:
    sep = _node_sep(folder)
    base = folder.rstrip(sep)
    return path == base or path.startswith(base + sep)


def _nearby_for(rows: list[dict], nearby: dict) -> dict[str, list]:
    """The node's folder listings above this library's missing files (the request's listings cover every library)."""
    missing = [r["node_path"] for r in rows if not r["ok"] and isinstance(r.get("node_path"), str)]
    return {d: names for d, names in nearby.items() if any(_under(p, d) for p in missing)}


def _check(client: NodeClient, mappings: list[PathMapping], files: list[dict]) -> tuple[dict[str, dict], dict]:
    results: dict[str, dict] = {}
    nearby: dict[str, Any] = {}
    for start in range(0, len(files), CHECK_BATCH):
        answer = client.access_check(mappings, files[start : start + CHECK_BATCH])
        for result in answer.get("results") or []:
            if isinstance(result, dict) and isinstance(result.get("path"), str):
                results[result["path"]] = result
        if isinstance(answer.get("nearby"), dict):
            nearby.update(answer["nearby"])
    return results, nearby


def _suggest_for(
    library: dict, mappings: list[PathMapping], libraries: list[dict], node_name: str
) -> tuple[dict | None, str | None]:
    """The suggestion for a library the node can't see, or the plain reason when there is none to make.

    A row that also maps a library that passed is right for that one, so changing it would break it: the node just
    lacks this library's folder.
    """
    app_root = library["app_root"]
    row = _covering_row(app_root, mappings)
    passed_rows = {id(_covering_row(lib["app_root"], mappings)) for lib in libraries if lib["ok"]}
    if row is not None and id(row) in passed_rows:
        if all(f["kind"] == NOT_VISIBLE_MISSING for f in library["files"] if not f["ok"]):
            return None, (
                f"{node_name} doesn't have this library's folder. Mount it on the node, or give each library its own "
                "row and leave this one out, so other workers take its files."
            )
        return None, None
    taken = {
        node_root
        for lib in libraries
        if lib["app_root"] != app_root and (node_root := _node_root(lib["app_root"], mappings)) is not None
    }
    return suggest_row(app_root, mappings, library["nearby"], taken=taken), None


def run_test_access(
    client: NodeClient, mappings: list[PathMapping], servers: list[dict], *, node_name: str = "This node"
) -> dict:
    """Sample every enabled library and ask the node to find each file through ``mappings``.

    Args:
        client: The node.
        mappings: The rows to test (saved or just edited).
        servers: The stored ``media_servers`` list.
        node_name: The node's name, for the reason given when it lacks a library's folder.

    Returns:
        ``{"libraries": [{"server", "library", "app_root", "mapped", "checked", "ok", "files": [{"path", "ok",
        "kind", "message", "node_path"}], "nearby": {node_dir: [names]}, "suggestion": {"from", "to"} | None,
        "error": str | None, "empty": bool, "unreadable": bool}]}``. ``nearby`` lists what the node does see above
        the library's missing files; ``suggestion`` is the corrected ``node_prefix`` of the row covering the library
        (never of a row that also maps a library that passed); ``mapped`` is False for a library no row covers nor
        holds a row inside (not tested: other workers take its files). A library with rows inside it is tested through
        those rows' folders only, and fails (``unreadable``) when this app can't open one of them or reads no file from
        it in time. ``empty`` says this app opened the folder and found no video near its top. A disk of a multi-disk
        library that a row covers but lacks the folder is left out while another disk holds it.

    Raises:
        NodeError: The node couldn't be asked.
    """
    roots = library_roots(servers)
    inside = {root: folders for root in roots if (folders := _rows_inside(root.app_root, mappings))}
    # A library tested through its inner rows isn't walked itself: a stalled root would only burn the budget.
    whole = [root for root in roots if root not in inside]
    nested = list(dict.fromkeys(folder for folders in inside.values() for folder in folders))
    sampled = _sample_folders([root.app_root for root in whole] + nested, SAMPLE_BUDGET_S, limit=SAMPLES_PER_ROOT)
    samplings = dict(zip(whole, sampled[: len(whole)], strict=True))
    by_folder = dict(zip(nested, sampled[len(whole) :], strict=True))
    # An inner row this app can't read isn't tested, so the library can't pass on its other rows' files.
    unread: dict[LibraryRoot, str] = {}
    for root, folders in inside.items():
        parts = [by_folder[folder] for folder in folders]
        for folder, part in zip(folders, parts, strict=True):
            problem = _inner_row_problem(folder, part)
            if problem:
                unread.setdefault(root, problem)
        samplings[root] = _Sampling(
            found=_interleave([list(part.found) for part in parts], max(SAMPLES_PER_ROOT, len(parts))),
            folder=next((part.folder for part in parts if part.folder), ""),
            timed_out=any(part.timed_out for part in parts),
            opened=any(part.opened for part in parts),
        )

    def covered(root: LibraryRoot) -> bool:
        return root in inside or _covering_row(root.app_root, mappings) is not None

    # A copy: a thread that timed out may still be adding to its list.
    samples = {root: list(samplings[root].found) for root in roots}
    # A library no row covers is never asked about: the node doesn't run its files, so it can't fail on them.
    files = [f for root, picked in samples.items() if covered(root) for f in picked]
    results, nearby = _check(client, mappings, files) if files else ({}, {})
    # One server folder mapped to several disks: a disk without it is normal while another disk holds it.
    held = {(r.server_id, r.library_id, r.remote) for r in roots if samplings[r].opened}
    libraries = []
    for root in roots:
        picked = samples[root]
        sampling = samplings[root]
        if not covered(root):
            libraries.append(
                {
                    "server": root.server,
                    "library": root.library,
                    "app_root": root.app_root,
                    "mapped": False,
                    "checked": 0,
                    "ok": False,
                    "files": [],
                    "nearby": {},
                    "suggestion": None,
                    "error": f"Not mapped on {node_name}. Other workers take its files.",
                    "empty": False,
                    "unreadable": False,
                }
            )
            continue
        if root not in inside and not picked and not sampling.opened and not sampling.timed_out:
            if (root.server_id, root.library_id, root.remote) in held:
                continue
        rows = []
        for f in picked:
            result = results.get(f["path"]) or {}
            rows.append(
                {
                    "path": f["path"],
                    "ok": bool(result.get("ok")),
                    "kind": result.get("kind") or ("" if result else "no_answer"),
                    "message": str(result.get("message") or ("" if result else "The node didn't answer for this file")),
                    "node_path": result.get("node_path") or map_app_path(f["path"], mappings),
                }
            )
        ok = bool(rows) and all(r["ok"] for r in rows)
        seen_nearby = _nearby_for(rows, nearby)
        error = None
        empty = False
        if root in unread:
            ok = False
            error = unread[root]
        elif not picked and sampling.timed_out:
            error = f"Timed out reading {sampling.folder or root.app_root} (no answer within {SAMPLE_BUDGET_S:g} s)"
        elif not picked and not sampling.opened:
            error = f"This app can't open {root.app_root}"
        elif not picked:
            empty = True
            error = f"No video files found near the top of {root.app_root}"
        libraries.append(
            {
                "server": root.server,
                "library": root.library,
                "app_root": root.app_root,
                "mapped": True,
                "checked": len(rows),
                "ok": ok,
                "files": rows,
                "nearby": seen_nearby,
                "suggestion": None,
                "error": error,
                "empty": empty,
                "unreadable": root in unread,
            }
        )
    for library in libraries:
        if library["files"] and not library["ok"]:
            library["suggestion"], reason = _suggest_for(library, mappings, libraries, node_name)
            library["error"] = library["error"] or reason
    return {"libraries": libraries}


def quick_access_check(client: NodeClient, mappings: list[PathMapping], servers: list[dict]) -> bool:
    """One file per library folder: True when the node sees every one (and there was at least one to check).

    Every library a row covers, as Test access samples them: one file per mapping row passes while a library under
    the same row is out of the node's sight. A library holding rows inside it is checked through those rows' folders,
    and fails as in Test access when this app can't open one of them or reads no file from it in time. Reading this
    app's folders has the same budget as Test access, so a stalled mount fails the check instead of holding up the
    node's check.

    Args:
        client: The node.
        mappings: Its saved rows.
        servers: The stored ``media_servers`` list.

    Returns:
        Whether every sampled file was visible (from every library a row covers or holds a row inside).
    """
    roots = library_roots(servers)
    inner = list(dict.fromkeys(folder for root in roots for folder in _rows_inside(root.app_root, mappings)))
    whole = [root.app_root for root in roots if _covering_row(root.app_root, mappings)]
    folders = list(dict.fromkeys(whole + inner))
    samplings = _sample_folders(folders, SAMPLE_BUDGET_S, limit=1)
    by_folder = dict(zip(folders, samplings, strict=True))
    if any(by_folder[folder].timed_out for folder in whole):
        logger.debug("Quick access check gave up: this app's own library folder didn't answer")
        return False
    if any(_inner_row_problem(folder, by_folder[folder]) for folder in inner):
        logger.debug("Quick access check failed: a mapped folder inside a library can't be read here")
        return False
    files = [f for sampling in samplings for f in sampling.found]
    if not files:
        return False
    try:
        results, _ = _check(client, mappings, files)
    except Exception as exc:
        logger.debug("Quick access check failed: {}", exc)
        return False
    return all(bool((results.get(f["path"]) or {}).get("ok")) for f in files)


def files_visible(client: NodeClient, mappings: list[PathMapping], paths: list[str]) -> bool:
    """True when the node now sees each of ``paths`` this app still has, as this app sees it.

    The files a node couldn't see, checked again before its pause is lifted: one file per library passing says
    nothing about the folder that failed, and lifting the pause on it alone paused the node again minutes later.

    Args:
        client: The node.
        mappings: Its saved rows.
        paths: The files it couldn't see, by this app's paths.

    Returns:
        Whether every one this app can still read was visible (True when none is left to check).
    """
    files = []
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            # Gone here too (a replaced file's old name): there is nothing left for the node to see.
            continue
        files.append({"path": path, "size": st.st_size, "mtime_ns": st.st_mtime_ns})
    if not files:
        return True
    try:
        results, _ = _check(client, mappings, files)
    except Exception as exc:
        logger.debug("Re-check of the files a node couldn't see failed: {}", exc)
        return False
    return all(bool((results.get(f["path"]) or {}).get("ok")) for f in files)


def _folders_this_app_cant_open(folders: list[str], budget_s: float) -> set[str]:
    """The folders whose read failed outright. One still stuck after ``budget_s`` (a stalled mount) isn't counted:
    it may be fine, and it shouldn't hold up the answer."""
    failed: set[str] = set()
    threads = []
    for folder in folders:

        def run(folder: str = folder) -> None:
            try:
                with os.scandir(folder):
                    pass
            except OSError:
                failed.add(folder)

        thread = threading.Thread(target=run, name="add-node-suggest", daemon=True)
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + budget_s
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return set(failed)


def default_mappings_for(servers: list[dict], *, budget_s: float = SUGGEST_BUDGET_S) -> list[dict[str, str]]:
    """Pre-filled rows for Add node: one per distinct library folder this app can open, mapped to the same path on
    the node.

    A folder inside another listed folder is covered by that one's row, so it gets none of its own. A folder this app
    can't open itself would only fail Test access on this app's side, so it isn't suggested.

    Args:
        servers: The stored ``media_servers`` list.
        budget_s: The longest spent checking that this app can open the folders.
    """
    folders = sorted({root.app_root for root in library_roots(servers)}, key=lambda p: (len(p), p))
    unopenable = _folders_this_app_cant_open(folders, budget_s)
    roots = [folder for folder in folders if folder not in unopenable]
    kept: list[str] = []
    for root in roots:
        if any(root == k or root.startswith(k.rstrip("/") + "/") for k in kept):
            continue
        kept.append(root)
    # Saving more rows than this is refused, so a pre-fill past it would fail the first save.
    return [{"app_prefix": root, "node_prefix": root} for root in sorted(kept)[:MAX_MAPPINGS]]
