"""IntroDB answers for every file of the three regression sets, through the app's own client (anonymous, paced).
Saves introdb_sets.json keyed by file path (local only)."""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
os.environ.setdefault(
    "MARKERS_EVAL_EVIDENCE", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
)
from loguru import logger  # noqa: E402

from media_preview_generator.markers.external_ids import ids_from_path  # noqa: E402
from media_preview_generator.markers.models import MediaIds  # noqa: E402
from media_preview_generator.markers.sources.introdb import IntroDbClient  # noqa: E402
from tools.markers_eval.data import load_v3_results  # noqa: E402

logger.remove()
HERE = Path(__file__).parent
OUT = HERE / "introdb_sets.json"
imdb = json.load(open(HERE / "shows_imdb.json"))
files = [e.file for e in load_v3_results() if e.truth_intro]
files += list(json.load(open(HERE / "heldout_truth.json")))
files += list(
    json.load(
        open(
            "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/eval/accused_truth.json"
        )
    )
)
client = IntroDbClient()
found = json.load(open(OUT)) if OUT.exists() else {}
for n, f in enumerate(sorted(set(files))):
    if f in found:
        continue
    tvdb = re.search(r"\{tvdb-(\d+)\}", f).group(1)
    ids = ids_from_path(f)
    result = client.lookup(
        MediaIds(kind="episode", imdb=imdb[tvdb], tvdb=tvdb, season=ids.season, episode=ids.episode),
        duration_ms=None,
        priority=2,
    )
    found[f] = {
        "status": result.status,
        "candidates": [[c.type.value, c.start_ms, c.end_ms] for c in result.candidates],
    }
    if n % 20 == 0:
        json.dump(found, open(OUT, "w"), indent=1)
        print(n, result.status, flush=True)
json.dump(found, open(OUT, "w"), indent=1)
print(
    "done", len(found), "with an intro", sum(1 for v in found.values() if any(c[0] == "intro" for c in v["candidates"]))
)
