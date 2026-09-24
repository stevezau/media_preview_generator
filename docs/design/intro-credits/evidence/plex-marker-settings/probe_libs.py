"""Read-only: for sampled items with DB markers, how many does Plex's API serve markers for (per library)."""

import json
import sys

from media_preview_generator.servers.registry import ServerRegistry

settings = json.load(open(sys.argv[1]))
reg = ServerRegistry.from_settings(settings.get("media_servers") or [])
server = reg.get("6c1e1100d1cf4024aad165698b356e11")
for line in open(sys.argv[2]):
    lib, ids = line.strip().split(":")
    served = sum(1 for i in ids.split(",") if server.get_markers(i))
    print(f"library {lib}: {served}/{len(ids.split(','))} items with DB markers served markers via includeMarkers=1")
