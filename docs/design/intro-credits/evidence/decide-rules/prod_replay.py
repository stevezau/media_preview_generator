"""Re-decide every present file of the prod markers.db snapshot (post.db, the audit's copy after #312) with one tree's
decide(), the way pipeline._decide builds its context (Medium, prod's order with TheIntroDB on, stored intro-chapter
limit and frame rate, Automatic credits windows, enabled types from the stored decisions). Writes JSON per file/type.

Usage: prod_replay.py <tree: base|work> <out.json>
"""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import shutil
import sys

R = str(LOCAL)
A = str(LOCAL)
TREES = {
    "base": f"{R}/base",
    "work": str(REPO),
}
tree = TREES[sys.argv[1]]
sys.path.insert(0, tree)
from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import MarkerType  # noqa: E402
from media_preview_generator.markers.store import MarkerStore  # noqa: E402

assert D.__file__.startswith(tree), D.__file__
if "--variant" in sys.argv:
    sys.path.insert(
        0,
        str(LOCAL),
    )
    import variants  # noqa: E402

    variants.apply(D, sys.argv[sys.argv.index("--variant") + 1])
ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
         "server_markers", "server_markers_imported")  # fmt: skip
copy = f"{R}/post_copy_{os.getpid()}.db"
shutil.copy(f"{A}/post.db", copy)
store = MarkerStore(copy)
conn = store._conn
from media_preview_generator.markers.models import Candidate, Source  # noqa: E402

NEXT = json.load(open(f"{R}/plex_next.json")) if "--next" in sys.argv else None
TEXT = json.load(open(f"{R}/prod_text.json")) if "--text" in sys.argv else {}
out = {}
for fid, path in conn.execute("select id, canonical_path from files where missing_since is null order by id"):
    rec = store.get_file(path)
    rows = store.get_decisions(fid)
    types = frozenset(
        t for t in (MarkerType.INTRO, MarkerType.CREDITS) if t in rows and rows[t].reason != "detection off"
    )
    if not types:
        continue
    ev = [c for c in store.get_evidence(fid) if c.source.value in ORDER]
    if NEXT is not None or TEXT:
        # The next run: Plex's own markers as the current reader stores them, credit text read where it was computed.
        if NEXT is not None and str(fid) in NEXT:
            ev = [c for c in ev if c.source is not Source.SERVER_MARKERS]
            ev += [Candidate(MarkerType(t), s, e, Source.SERVER_MARKERS, origin="plex", stale=st)
                   for t, s, e, st in NEXT[str(fid)]]  # fmt: skip
        if path in TEXT and not any(c.source is Source.CREDITS_TEXT for c in ev) and TEXT[path][0] is not None:
            s, e = TEXT[path]
            ev.append(Candidate(MarkerType.CREDITS, round(s * 1000), None if e is None else round(e * 1000),
                                Source.CREDITS_TEXT))  # fmt: skip
    window, cap = D.credits_limits_ms(is_episode=rec.season_key is not None, tv_window_s=None, movie_window_s=None)
    # As pipeline._decide now orders them: a local detector with no candidate stays only while it hasn't answered.
    answered = {c.source.value for c in ev}
    local = {"season_audio", "season_audio_previous", "credits_text"}
    order = tuple(
        s for s in ORDER if s in answered or s not in local or store.evidence_fetched_at(fid, Source(s)) is None
    )
    ctx = D.DecisionContext(
        rec.duration_ms or 0,
        rec.is_movie,
        D.APP_PUBLISH_WHEN,
        types,
        order,
        store.get_intro_chapter_limit(fid)[1],
        movie_credits_max_from_end_ms=cap,
        credits_window_ms=window,
        frame_rate=store.get_frame_rate(fid)[1],
    )
    decisions = D.decide(ev, ctx, store.get_locked(fid))
    for t in types:
        d = decisions[t]
        m = d.marker or d.proposed
        stored = rows[t]
        out[f"{fid}:{t.value}"] = {
            "path": path,
            "status": d.status.value,
            "reason": d.reason,
            "marker": None if m is None else [m.start_ms, m.end_ms, list(m.decided_by)],
            "stored_status": stored.status.value if hasattr(stored.status, "value") else stored.status,
            "stored_reason": stored.reason,
        }
json.dump(out, open(sys.argv[2], "w"), indent=0)
store._conn.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(copy + suffix):
        os.remove(copy + suffix)
print(sys.argv[1], "rows", len(out))
