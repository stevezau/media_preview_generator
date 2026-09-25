"""Mixed-speed season groups in the #310 library chapter set (chap.pkl seasons)."""
import pickle
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb")
from media_preview_generator.markers.audio.season import season_clock  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

SP = Path("/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick/data")
d = pickle.load(open(SP / "chap.pkl", "rb"))
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
mixed = []
for season, files in sorted(d["seasons"].items()):
    clock = season_clock({f: cache.speed(f) for f in files})
    if clock.factors:
        mixed.append((season[-60:], len(files), len(clock.factors)))
print("chap seasons", len(d["seasons"]), "mixed", mixed)
