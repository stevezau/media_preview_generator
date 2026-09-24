"""Per-type staleness categories on the prod snapshot: does the pv key hold the rows' times, and are rows older."""

import collections
import json

from load import by_mid, OUR_START, our_items, tags


def ents(val):
    if not val:
        return None
    try:
        d = json.loads(val)
    except Exception:
        return None
    arr = d.get("MediaPartMarkersArray", {}).get("MediaPartMarker", [])
    if isinstance(arr, dict):
        arr = [arr]
    return {(int(m["startTimeOffset"]), int(m["endTimeOffset"])) for m in arr}


c = collections.Counter()
for mid, trows in tags.items():
    ps = by_mid.get(mid)
    if not ps:
        continue
    plex = [t for t in trows if not (mid in our_items and t["created_at"] >= OUR_START)]
    for text, key in (("intro", "pv_intros"), ("credits", "pv_credits")):
        rows_ = [t for t in plex if t["text"] == text]
        if not rows_:
            continue
        times = {(t["time_offset"], t["end_time_offset"]) for t in rows_}
        haskey = any(ents(p[key]) is not None for p in ps)
        holds = any((e := ents(p[key])) and times <= e for p in ps)
        older = all(t["created_at"] < (p["mp_updated"] or 0) for t in rows_ for p in ps)
        cat = "holds" if holds else ("key_other" if haskey else "nokey")
        c[(text, cat, older)] += 1
for k, v in sorted(c.items()):
    print(k, v)
