"""Which truth files of the four intro sets sit in a season folder split across the library's disks (so fix 3 changes
their group), and how many extra members the merged groups bring. Read-only listing."""

import collections
import os
import sys

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
OLD = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick"
sys.path.insert(0, WORKTREE)
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.servers.base import Library, ServerConfig, ServerType  # noqa: E402

sys.path.insert(1, OLD)
from evaluate import eval_groups  # noqa: E402

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
LIBRARY = ServerConfig(
    id="plex", type=ServerType.PLEX, name="Plex", enabled=True, url="http://plex", auth={},
    libraries=[Library("2", "TV Shows", tuple(f"{root}/TV Shows" for root in ROOTS))],
)  # fmt: skip
for mode in ("lists", "scale_clean", "accused", "chap_clean"):
    truth_files = split_files = 0
    extra = set()
    seasons = collections.Counter()
    for _season, files, fps, truth in eval_groups(mode):
        for f in truth:
            truth_files += 1
            if not os.path.exists(f):
                continue
            own = set(S.season_group(f).episodes)
            merged = set(S.season_group(f, S.season_videos(f, [LIBRARY])).episodes)
            if merged != own:
                split_files += 1
                seasons[os.path.dirname(f).split("/TV Shows/")[-1]] += 1
                extra |= merged - set(fps)
    print(f"{mode:12} truth files {truth_files:4} in split seasons {split_files:4} ({len(seasons)} seasons), "
          f"merged members without a fingerprint {len(extra)}")  # fmt: skip
    for s, n in seasons.most_common(8):
        print("     ", s, n)
