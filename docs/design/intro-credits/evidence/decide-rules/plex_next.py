"""What the next run's server-marker read would store for each prod file (from the audit's Plex pull in rows.json): Plex's
own markers of every type, stale-flagged as the audit's "made for an earlier file" check says, for files whose Plex item
shows none of ours (a server showing ours is never read). Writes plex_next.json {file_id: [[type, s, e, stale], ...]}."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import collections
import json

A = str(LOCAL)
R = str(LOCAL)
rows = json.load(open(f"{A}/rows.json"))
by_file = collections.defaultdict(list)
for r in rows:
    by_file[r["fid"]].append(r)
out, counts = {}, collections.Counter()
for fid, rs in by_file.items():
    if any(p["ours"] for r in rs for p in r["plex_rows"]):
        counts["shows ours"] += 1
        continue
    found = [[r["type"], p["s"], p["e"], bool(r["stale"])] for r in rs for p in r["plex_rows"] if not p["ours"]]
    if found:
        out[str(fid)] = found
        counts["own markers"] += 1
        counts["stale"] += any(f[3] for f in found)
json.dump(out, open(f"{R}/plex_next.json", "w"))
print(dict(counts))
