"""Task 3 fix 2 on prod: every prod file with an IntroDB/TheIntroDB intro starting in the first 2 s and shorter than
10 s, decided again from its stored evidence (prod_markers.db snapshot, read-only) with THIS WORKTREE's decide(),
without and with the online-logo rule. Medium, the app's order of sources, intro only."""

import os
import re
import sqlite3
import sys

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKTREE)
from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402

assert D.__file__.startswith(WORKTREE)
ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
         "server_markers", "server_markers_imported")  # fmt: skip
db = sqlite3.connect(f"file:{os.path.join(HERE, 'prod_markers.db')}?mode=ro", uri=True)
files = db.execute(
    "select distinct f.id, f.canonical_path, f.duration_ms from evidence e join files f on f.id = e.file_id "
    "where e.source in ('introdb', 'theintrodb') and e.type = 'intro' and e.start_ms < 2000 "
    "and e.end_ms - e.start_ms < 10000 and f.missing_since is null order by f.canonical_path"
).fetchall()
REAL = D._is_online_logo
for fid, path, duration in files:
    cands = [
        Candidate(MarkerType.INTRO, s, e, Source(src), conf if conf is not None else 1.0, origin or "")
        for src, s, e, conf, origin in db.execute(
            "select source, start_ms, end_ms, confidence, origin from evidence where file_id = ? and type = 'intro' "
            "and start_ms is not null",
            (fid,),
        )
    ]
    ctx = D.DecisionContext(duration, False, "medium", frozenset({MarkerType.INTRO}), ORDER)
    out = {}
    for label, rule in (("before", lambda _c, _d: False), ("after", REAL)):
        D._is_online_logo = rule
        d = D.decide(cands, ctx, {})[MarkerType.INTRO]
        m = d.marker or d.proposed
        out[label] = (d.status.value, d.reason, None if m is None else (m.start_ms, m.end_ms, m.decided_by))
    D._is_online_logo = REAL
    name = re.search(r"S\d\dE\d\d", path).group(0)
    print(name, "| before", out["before"], "| after", out["after"])
