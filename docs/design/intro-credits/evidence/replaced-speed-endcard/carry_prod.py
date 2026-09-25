"""Replay the carry-over on production's markers.db (post-#312 snapshot, audit0925/post.db, COPIED first: opening it
with the store adds tables). For every file on disk with a type in "no evidence", the files it replaced (its own
earlier identity, or files last published to its server items that are gone from disk) and what would be carried.
Read-only on /data (existence checks only)."""

import json
import os
import shutil
import sys

W = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, W)
from media_preview_generator.markers import carry_over as co  # noqa: E402
from media_preview_generator.markers.decide import DecisionStatus, TypeDecision  # noqa: E402
from media_preview_generator.markers.models import Marker, MarkerType  # noqa: E402
from media_preview_generator.markers.store import MarkerStore  # noqa: E402

SCR = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad"
copy = os.path.join(SCR, "laneL", "post_copy.db")
shutil.copyfile(os.path.join(SCR, "audit0925", "post.db"), copy)
store = MarkerStore(copy)
conn = store._conn
rows = conn.execute(
    "SELECT DISTINCT f.id FROM decisions d JOIN files f ON f.id=d.file_id WHERE d.status='no_evidence' "
    "AND d.reason='no evidence' AND f.missing_since IS NULL"
).fetchall()
carried, candidates = [], 0
for (file_id,) in rows:
    rec = store.get_file_by_id(file_id)
    if not os.path.exists(rec.canonical_path):
        continue
    items = [
        (r["server_id"], r["item_id"])
        for r in conn.execute(
            "SELECT server_id, item_id FROM publish_state WHERE file_id=? AND item_id IS NOT NULL", (file_id,)
        )
    ]
    decisions = {}
    for mtype, row in store.get_decisions(file_id).items():
        marker = store.get_markers(file_id).get(mtype)
        decisions[mtype] = TypeDecision(
            mtype, row.status, marker if row.status is DecisionStatus.DECIDED else None, None, row.reason
        )
    for mtype in MarkerType:
        decisions.setdefault(mtype, TypeDecision(mtype, DecisionStatus.DISABLED, None, None, "detection off"))
    asked = []

    def previous(wanted=frozenset(MarkerType)):
        asked.append(wanted)
        return co.previous_decisions(
            store,
            rec,
            items,
            wanted=wanted,
            gone=lambda other: other.missing_since is not None or not os.path.exists(other.canonical_path),
        )

    out = co.carry_over(decisions, rec.duration_ms or 0, previous, kept=store.get_markers(file_id))
    found = previous(asked[0]) if asked else {}
    if found:
        candidates += 1
    for mtype, d in out.items():
        if d != decisions[mtype]:
            prev = found[mtype]
            carried.append(
                (
                    os.path.basename(rec.canonical_path)[:80],
                    mtype.value,
                    d.marker.start_ms,
                    d.marker.end_ms,
                    (rec.duration_ms or 0) - prev.duration_ms,
                )
            )
    for mtype, prev in found.items():
        d = decisions.get(mtype)
        if d and d.status is DecisionStatus.NO_EVIDENCE and prev and prev.marker and out[mtype] == d:
            print(
                "NOT CARRIED",
                os.path.basename(rec.canonical_path)[:70],
                mtype.value,
                prev,
                "length change",
                (rec.duration_ms or 0) - prev.duration_ms,
            )
print(f"files on disk with a no-evidence type: {len(rows)}; with a replaced file found: {candidates}")
for row in carried:
    print("CARRIED", row)
store.close()
