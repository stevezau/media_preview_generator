import sys

p = sys.argv[1]
s = open(p).read()


def rep(old, new):
    global s
    assert s.count(old) == 1, old[:90]
    s = s.replace(old, new)


rep(
    """A season group is the episodes of a file's folder with the same season number in their names, at most the 40 nearest by
episode number (a flat folder can hold hundreds).""",
    """A season group is the episodes of a file's season folders with the same season number in their names, at most the 40
nearest by episode number (a flat folder can hold hundreds). The season folders are the file's own and, in a library
spread over several disks, the same folder under the library's other folders (``season_folders``).""",
)
rep(
    """from collections.abc import Callable, Mapping, Sequence
""",
    """from collections.abc import Callable, Iterable, Mapping, Sequence
""",
)
rep(
    """from ..external_ids import ids_from_path, is_extra
""",
    """from ..external_ids import ids_from_path, is_extra
from ..missing import library_folders
""",
)
rep(
    """if TYPE_CHECKING:
    from ..pipeline import DetectorAnswer, LocalDetectorSpec, PipelineContext
    from ..store import FileRecord
""",
    """if TYPE_CHECKING:
    from ...servers.base import ServerConfig
    from ..pipeline import DetectorAnswer, LocalDetectorSpec, PipelineContext
    from ..store import FileRecord
""",
)
rep(
    """                and entry.is_file()
            )
    except OSError:
        return ()
""",
    '''                and entry.is_file()
            )
    except OSError:
        return ()


def season_folders(canonical_path: str, configs: Iterable[ServerConfig]) -> tuple[str, ...]:
    """The folders a file's season is kept in: its own, and, for a library spread over several disks, the same folder
    under each other folder of every enabled server's library holding the file, where one is there.

    The season folder is taken relative to the deepest library folder holding the file, at least one folder down.
    Library folders inside that one or around it are left out (the same relative folder there is another show's), as
    are ``/`` and a folder that is the same directory as one already listed (a linked disk). A path that isn't in
    normal form (``..``, ``.``, doubled or trailing slashes) keeps its own folder only, so every folder listed lies
    inside a library folder.

    Args:
        canonical_path: Local path of one episode.
        configs: The servers' configs.

    Returns:
        The file's own folder first, then the others sorted.
    """
    own = os.path.dirname(canonical_path)
    if not os.path.isabs(own) or os.path.normpath(own) != own:
        return (own,)
    elsewhere: set[str] = set()
    for folders in library_folders(configs):
        roots = {os.path.normpath(folder) for folder, _mapping_root in folders if folder} - {"/"}
        holding = [root for root in roots if own.startswith(root + "/")]
        if not holding:
            continue
        home = max(holding, key=len)
        relative = own[len(home) + 1 :]
        elsewhere.update(
            os.path.join(root, relative)
            for root in roots
            if root != home and not root.startswith(home + "/") and not home.startswith(root + "/")
        )
    found = [own]
    seen = {os.path.realpath(own)}
    for folder in sorted(elsewhere):
        if not os.path.isdir(folder):
            continue
        real = os.path.realpath(folder)
        if real not in seen:
            seen.add(real)
            found.append(folder)
    return tuple(found)


def season_videos(canonical_path: str, configs: Iterable[ServerConfig]) -> tuple[FolderVideo, ...]:
    """:func:`folder_videos` of each of a file's :func:`season_folders`: what :func:`season_group` picks from.

    Args:
        canonical_path: Local path of one episode.
        configs: The servers' configs.

    Returns:
        The videos, folder by folder.
    """
    return tuple(video for folder in season_folders(canonical_path, configs) for video in folder_videos(folder))


def _server_configs(ctx: PipelineContext) -> list[ServerConfig]:
    return list(ctx.registry.configs())
''',
)
rep(
    '''    """The episodes a file is matched with (spec §5.3: the season on disk, server-agnostic).

    Files of its folder with the same season number in their names (files without one are a group of their own); in a
    folder with more than 40 of them, the 40 nearest by episode number, ties by name (by position in name order when a
    name carries no episode number).

    Args:
        canonical_path: Local path of one episode.
        videos: The folder's :func:`folder_videos`, when the caller already read them.

    Returns:
        The folder and the group's paths, sorted, the file itself included.
    """''',
    '''    """The episodes a file is matched with (spec §5.3: the season on disk, server-agnostic).

    Files of its folder (of its season's folders on every disk, given ``videos`` from :func:`season_videos`) with the
    same season number in their names (files without one are a group of their own); with more than 40 of them, the 40
    nearest by episode number, ties by path (by position in path order when a name carries no episode number).

    Args:
        canonical_path: Local path of one episode.
        videos: The files to pick from (:func:`season_videos`); None reads the file's own folder only.

    Returns:
        The file's own folder and the group's paths, sorted, the file itself included.
    """''',
)
rep(
    """    Args:
        canonical_path: Local path of one episode.
        videos: The folder's :func:`folder_videos`, when the caller already read them.

    Returns:
        The count, the file itself included.""",
    """    Args:
        canonical_path: Local path of one episode.
        videos: The files to count from (:func:`season_videos`); None reads the file's own folder only.

    Returns:
        The count, the file itself included.""",
)
rep(
    '''    """The other files of a folder whose own season group holds this file (in a flat folder, not only its group's).''',
    '''    """The other files of a season whose own season group holds this file (in a flat folder, not only its group's).''',
)
rep(
    """        videos: The folder's :func:`folder_videos`.
        group_of: :func:`season_group` of another file of the folder, when the caller keeps them.""",
    """        videos: The season's files (:func:`season_videos`).
        group_of: :func:`season_group` of another file of the season, when the caller keeps them.""",
)
rep(
    '''class _SeasonView:
    """What one run of an episode reads of its season: the folder listing, groups, previous-season files, and each''',
    '''class _SeasonView:
    """What one run of an episode reads of its season: its folders' listing, groups, previous-season files, and each''',
)
rep(
    """        self.videos = folder_videos(os.path.dirname(canonical_path))""",
    """        self.videos = season_videos(canonical_path, _server_configs(ctx))""",
)
rep(
    '''        """The season group of this run's episode, or of another file of its folder."""''',
    '''        """The season group of this run's episode, or of another file of its season."""''',
)
rep(
    """def _request_redecide(
    ctx: PipelineContext,
    rec: FileRecord,
    members: dict[str, FileRecord],
    signature: str,
    *,
    matched: Mapping[str, FileRecord],
) -> None:""",
    """def _request_redecide(
    ctx: PipelineContext,
    rec: FileRecord,
    members: dict[str, FileRecord],
    signature: str,
    *,
    matched: Mapping[str, FileRecord],
    group: SeasonGroup,
) -> None:""",
)
rep(
    '''    since its answer never equals this run's even when nothing it used changed.
    """
    folder = os.path.dirname(rec.canonical_path)
    videos: tuple[FolderVideo, ...] | None = None
    own_group: SeasonGroup | None = None
    on_disk: dict[str, list] = {}
    stale = []
    for path, member in members.items():
        if member.id == rec.id or os.path.dirname(path) != folder:
            continue''',
    '''    since its answer never equals this run's even when nothing it used changed. Only ``group``'s episodes are asked
    (``members`` can hold the previous season's files).
    """
    videos: tuple[FolderVideo, ...] | None = None
    own_group: SeasonGroup | None = None
    on_disk: dict[str, list] = {}
    stale = []
    for path, member in members.items():
        if member.id == rec.id or path not in group.episodes:
            continue''',
)
rep(
    """        if videos is None:
            videos = folder_videos(folder)
            own_group = season_group(rec.canonical_path, videos)""",
    """        if videos is None:
            videos = season_videos(rec.canonical_path, _server_configs(ctx))
            own_group = season_group(rec.canonical_path, videos)""",
)
rep(
    """    group = season_group(rec.canonical_path)

    def fingerprint_of(""",
    """    group = season_group(rec.canonical_path, season_videos(rec.canonical_path, _server_configs(ctx)))

    def fingerprint_of(""",
)
rep(
    """    _request_redecide(ctx, rec, records, signature, matched=matched)""",
    """    _request_redecide(ctx, rec, records, signature, matched=matched, group=group)""",
)
rep(
    """    signature = _signature(ctx, _signature_paths(canonical_path, season_group(canonical_path)))
    return answer != signature""",
    """    group = season_group(canonical_path, season_videos(canonical_path, _server_configs(ctx)))
    signature = _signature(ctx, _signature_paths(canonical_path, group))
    return answer != signature""",
)
open(p, "w").write(s)
