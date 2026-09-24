"""Season folders of the prod TV Shows library that are split across its disks, found with THIS WORKTREE's
season.season_folders (read-only listing of /data*)."""

import os
import sys

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
sys.path.insert(0, WORKTREE)
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.servers.base import Library, ServerConfig, ServerType  # noqa: E402

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
LIBRARY = ServerConfig(
    id="plex", type=ServerType.PLEX, name="Plex", enabled=True, url="http://plex", auth={},
    libraries=[Library("2", "TV Shows", tuple(f"{root}/TV Shows" for root in ROOTS))],
)  # fmt: skip
split = {}
for root in ROOTS:
    tv = f"{root}/TV Shows"
    if not os.path.isdir(tv):
        continue
    for show in sorted(os.listdir(tv)):
        show_dir = os.path.join(tv, show)
        if not os.path.isdir(show_dir):
            continue
        for season in sorted(os.listdir(show_dir)):
            folder = os.path.join(show_dir, season)
            if not os.path.isdir(folder):
                continue
            videos = S.folder_videos(folder)
            if not videos:
                continue
            folders = S.season_folders(videos[0].path, [LIBRARY])
            if len(folders) > 1:
                key = os.path.join(show, season)
                split[key] = {f: len(S.folder_videos(f)) for f in folders}
files = sum(sum(v.values()) for v in split.values())
print("split season folders", len(split), "files", files)
for key, where in sorted(split.items()):
    print(f"  {key}: " + ", ".join(f"{f.split('/')[1]}={n}" for f, n in sorted(where.items())))
