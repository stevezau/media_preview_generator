import sys
p = sys.argv[1]
s = open(p).read()
def rep(old, new):
    global s
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)
rep('''from collections.abc import Callable, Iterable, Sequence
''', '''from collections.abc import Callable, Iterable, Iterator, Sequence
''')
rep('''def disk_roots(canonical_path: str, configs: Iterable[ServerConfig]) -> tuple[str, ...]:
    """The disk roots a local file lies under: each enabled server's library folder holding it, and the path mapping
    folder that library folder came from.

    Args:
        canonical_path: Local path of the file.
        configs: The servers' configs.

    Returns:
        The roots, each once, in server and library order; empty when no library holds the file. ``/`` is never one:
        it holds entries whatever is mounted.
    """
    roots: list[str] = []
    for cfg in configs:
        if not cfg.enabled:
            continue
        mappings = list(cfg.path_mappings or [])
        for library in cfg.libraries:
            for remote in library.remote_paths:
                if not (remote or "").strip():
                    continue
                for local, mapping_root in path_mapping_candidates(remote, mappings):
                    folder = local.rstrip("/")
                    if folder and canonical_path.startswith(folder + "/"):
                        roots.extend(root for root in (mapping_root, folder) if root and root != "/")
    return tuple(dict.fromkeys(roots))
''', '''def library_folders(configs: Iterable[ServerConfig]) -> Iterator[list[tuple[str, str | None]]]:
    """Each library of every enabled server, as its local folders.

    Args:
        configs: The servers' configs.

    Yields:
        Per library, in server and library order: each of its folders on this side of the server's path mappings (no
        trailing ``/``), with the mapping folder it came from (None when no mapping applies).
    """
    for cfg in configs:
        if not cfg.enabled:
            continue
        mappings = list(cfg.path_mappings or [])
        for library in cfg.libraries:
            yield [
                (local.rstrip("/"), mapping_root)
                for remote in library.remote_paths
                if (remote or "").strip()
                for local, mapping_root in path_mapping_candidates(remote, mappings)
            ]


def disk_roots(canonical_path: str, configs: Iterable[ServerConfig]) -> tuple[str, ...]:
    """The disk roots a local file lies under: each enabled server's library folder holding it, and the path mapping
    folder that library folder came from.

    Args:
        canonical_path: Local path of the file.
        configs: The servers' configs.

    Returns:
        The roots, each once, in server and library order; empty when no library holds the file. ``/`` is never one:
        it holds entries whatever is mounted.
    """
    roots: list[str] = []
    for folders in library_folders(configs):
        for folder, mapping_root in folders:
            if folder and canonical_path.startswith(folder + "/"):
                roots.extend(root for root in (mapping_root, folder) if root and root != "/")
    return tuple(dict.fromkeys(roots))
''')
open(p, "w").write(s)
