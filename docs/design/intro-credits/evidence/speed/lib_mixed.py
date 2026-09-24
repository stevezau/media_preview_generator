"""Mixed-speed season folders among the season folders prod's markers.db knows (ffprobe once per file, cached)."""

import collections
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.audio.season import folder_videos, season_clock, season_group  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

HERE = Path(__file__).parent
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
db = sqlite3.connect(f"file:{HERE.parent / 'prod_markers.db'}?mode=ro", uri=True)
folders = sorted({row[0] for row in db.execute("select season_key from files where season_key is not null")})
print("season folders", len(folders), flush=True)
counts = collections.Counter()
for folder in folders:
    if not os.path.isdir(folder):
        counts["gone"] += 1
        continue
    videos = folder_videos(folder)
    groups = {season_group(v.path, videos).episodes for v in videos}
    for group in groups:
        try:
            speeds = {f: cache.speed(f) for f in group}
        except Exception as exc:  # noqa: BLE001 - a survey
            counts["unreadable"] += 1
            print("skip", folder[-60:], type(exc).__name__, flush=True)
            continue
        counts[tuple(sorted(collections.Counter(s and round(s, 3) for s in speeds.values()).items(), key=str))] += 0
        clock = season_clock(speeds)
        counts["groups"] += 1
        if clock.factors:
            counts["mixed"] += 1
            print(
                "MIXED",
                folder[-70:],
                "clock",
                round(clock.speed, 3),
                "retimed",
                len(clock.factors),
                "of",
                len(group),
                flush=True,
            )
print(dict((k, v) for k, v in counts.items() if v))
