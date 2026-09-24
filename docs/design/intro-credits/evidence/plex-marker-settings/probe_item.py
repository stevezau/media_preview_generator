"""Read-only: what the app's Plex reader returns for given item ids (no token printed)."""

import json
import sys

from media_preview_generator.markers.sources.server_markers import read_server_markers
from media_preview_generator.servers.registry import ServerRegistry

settings = json.load(open(sys.argv[1]))
reg = ServerRegistry.from_settings(settings.get("media_servers") or [])
sid = "6c1e1100d1cf4024aad165698b356e11"
server = reg.get(sid)
cfg = reg.get_config(sid)
print("server type", cfg.type, "url host redacted")
for arg in sys.argv[2:]:
    item_id, dur, path = arg.split("|", 2)
    rows = server.get_markers(item_id)
    parts = server.get_part_durations(item_id)
    found = read_server_markers(server, cfg, item_id, duration_ms=int(dur), canonical_path=path)
    print(item_id, "get_markers=", rows, "parts=", parts, "read_server_markers=", found)
    root = server._connect().query(f"/library/metadata/{item_id}?includeMarkers=1")
    node = next(iter(root), None)
    print("  children tags:", sorted({c.tag for c in node}) if node is not None else None)
    for m in node.iter("Marker") if node is not None else []:
        print("  Marker attrs:", dict(m.attrib))
