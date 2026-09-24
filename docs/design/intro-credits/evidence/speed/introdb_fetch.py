"""IntroDB answers for Bones S05-S08 through the app's own client (anonymous, paced). Saves introdb.json."""

import json
import os
import sys

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.models import MediaIds  # noqa: E402
from media_preview_generator.markers.sources.introdb import IntroDbClient  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "introdb.json")
EPISODES = {5: 22, 6: 23, 7: 13, 8: 24}
client = IntroDbClient()
found = json.load(open(OUT)) if os.path.exists(OUT) else {}
for season, count in EPISODES.items():
    for episode in range(1, count + 1):
        key = f"S{season:02d}E{episode:02d}"
        if key in found:
            continue
        ids = MediaIds(kind="episode", imdb="tt0460627", tvdb="75682", season=season, episode=episode)
        result = client.lookup(ids, duration_ms=None, priority=2)
        found[key] = {
            "status": result.status,
            "candidates": [[c.type.value, c.start_ms, c.end_ms] for c in result.candidates],
        }
        print(key, found[key], flush=True)
        json.dump(found, open(OUT, "w"), indent=1)
print(len(found))
