"""The one held-out decision the frame rate changed: its candidates and both decisions."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
EVIDENCE = "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
from loguru import logger  # noqa: E402

from tools.markers_eval.plex import load_baseline, server_candidates  # noqa: E402
from media_preview_generator.markers.models import MarkerType  # noqa: E402

logger.remove()
HERE = Path(__file__).parent
truth = json.load(open(HERE / "heldout_truth.json"))
introdb = json.load(open(HERE / "introdb_sets.json"))
plex = load_baseline(Path(EVIDENCE) / "lab/results/scale/prod_plex_markers.json")
for f in truth:
    if "Food Wars" in f and "S01E05" in f:
        print(os.path.basename(f))
        print("truth", truth[f])
        print("introdb", introdb[f])
        print("plex", server_candidates(plex.get(f, []), MarkerType.INTRO))
