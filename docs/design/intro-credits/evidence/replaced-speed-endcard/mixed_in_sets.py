"""Which season groups of the four intro sets mix 25 fps and film-rate files (the only groups fix 1 can change).
Frame rates from the harness probe cache (ffprobe, nice 19, read-only)."""

import json
import sys
from pathlib import Path

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, WORKTREE)
from media_preview_generator.markers.audio.season import season_clock  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

EV = Path("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/intro-end")
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
for name in ("lab118", "heldout175", "accused", "libchap"):
    data = json.load(open(EV / f"evidence_{name}.json"))
    groups = {tuple(row["group"]) for row in data.values() if row.get("group")}
    mixed = []
    for group in groups:
        speeds = {f: cache.speed(f) for f in group}
        clock = season_clock(speeds)
        if clock.factors:
            mixed.append((group[0].rsplit("/", 2)[-3:-1], len(group), len(clock.factors)))
    print(name, "groups", len(groups), "mixed", mixed, flush=True)
