"""Which season groups of the regression sets mix playback speeds (read from the harness's cached frame rates only)."""

import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.audio.season import season_clock, season_group  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402
from tools.markers_eval.data import load_v3_results  # noqa: E402

HERE = Path(__file__).parent
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
sets = {
    "heldout175": list(json.load(open(HERE / "heldout_truth.json"))),
    "accused": list(
        json.load(
            open(
                "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/"
                "evidence/e"
                "val/accused_truth.json"
            )
        )
    ),
}
by_season = collections.defaultdict(list)
for e in load_v3_results():
    by_season[e.season].append(e.file)
sets["lab118 (eval lists)"] = [f for files in by_season.values() for f in files]
for name, files in sets.items():
    groups = {
        season_group(f).episodes if name != "lab118 (eval lists)" else tuple(by_season[str(Path(f).parent)])
        for f in files
    }
    rates = collections.Counter()
    mixed = []
    for group in groups:
        speeds = {f: cache.speed(f) for f in group}
        rates.update(speeds.values())
        clock = season_clock(speeds)
        if clock.factors:
            mixed.append((str(Path(group[0]).parent)[-60:], len(clock.factors)))
    print(name, "groups", len(groups), "speeds", dict(rates), "mixed", mixed)
