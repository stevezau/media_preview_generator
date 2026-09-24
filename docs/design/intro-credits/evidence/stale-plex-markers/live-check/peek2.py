import collections
import json

SP = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad"
dates = []
keys = collections.Counter()
n = 0
for line in open(f"{SP}/stale/sonarr_imports.jsonl"):
    d = json.loads(line)
    keys.update(d.keys())
    dates.append(d["date"])
    n += 1
print(n, min(dates), max(dates))
print(keys)
by_ep = collections.Counter(json.loads(line)["episodeId"] for line in open(f"{SP}/stale/sonarr_imports.jsonl"))
print("episodes", len(by_ep), "with >1 import", sum(1 for v in by_ep.values() if v > 1))
