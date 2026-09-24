"""The #310 library chapter set: which of its season groups mix playback speeds (ffprobe once per file, cached), and a
truth file of their episodes' intro chapters for season-truth. Read only on /data."""

import json
import os
import pickle
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.audio.season import season_clock, season_group  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

HERE = Path(__file__).parent
PKL = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick/data/chap.pkl"
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
d = pickle.load(open(PKL, "rb"))
truth = {f: t for f, t in d["truth"].items() if t}
print("seasons", len(d["seasons"]), "files with truth", len(truth), flush=True)
mixed_truth = {}
for season, files in sorted(d["seasons"].items()):
    files = [f for f in files if os.path.exists(f)]
    if not files:
        continue
    group = season_group(files[0]).episodes
    try:
        speeds = {f: cache.speed(f) for f in group}
    except Exception as exc:  # noqa: BLE001 - a survey: report and go on
        print("skip", season[-50:], exc)
        continue
    clock = season_clock(speeds)
    if clock.factors:
        rows = {f: list(truth[f]) for f in files if f in truth}
        print("MIXED", season[-70:], "retimed", len(clock.factors), "of", len(group), "truth", len(rows), flush=True)
        mixed_truth.update(rows)
json.dump(mixed_truth, open(HERE / "chap_mixed_truth.json", "w"), indent=1)
print("mixed truth files", len(mixed_truth))
