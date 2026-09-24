"""The shows (tvdb ids) of the three regression sets, for an imdb-id lookup."""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
os.environ.setdefault(
    "MARKERS_EVAL_EVIDENCE", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
)
from tools.markers_eval.data import load_v3_results  # noqa: E402

HERE = Path(__file__).parent
files = [e.file for e in load_v3_results() if e.truth_intro]
files += list(json.load(open(HERE / "heldout_truth.json")))
files += list(
    json.load(
        open(
            "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/eval/accused_truth.json"
        )
    )
)
shows = {}
for f in files:
    m = re.search(r"/TV Shows/([^/]+) \{tvdb-(\d+)\}/", f)
    if m:
        shows[m.group(2)] = m.group(1)
json.dump(shows, open(HERE / "shows.json", "w"), indent=1)
print(len(shows))
print(" ".join(sorted(shows)))
