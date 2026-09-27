"""Re-decide every present file of the 2026-09-27 audit's markers.db copy with one tree's decide(), the way
pipeline._decide builds its context (Medium, prod's source order with TheIntroDB on, stored intro-chapter limit and
frame rate, Automatic credits windows, enabled types from the stored decisions), then the rule-change keep
(keep_published) as pipeline._keep_published_before_rule_change applies it.

Credit text: the stored answer, or with --text <vtext.json> the tree's own answer for every file the tree's pipeline
reads credit text on (base: files whose credits aren't decided by chapters alone and already have an answer; work:
also chapter-decided ones). Season audio: stored, or with --audio <answers.json> {path: [s, e] | null} for files whose
intro waits for season audio.

The base tree is $CREDFIX_BASE (default ./base), the work tree $CREDFIX_WORK (default this repo).

Usage: replay.py <base|work> <out.json> [--text vtext.json] [--audio answers.json]
"""

import json
import os
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREES = {
    "base": os.environ.get("CREDFIX_BASE", str(HERE / "base")),
    "work": os.environ.get("CREDFIX_WORK", str(HERE.parents[4])),
}
which = sys.argv[1]
tree = TREES[which]
sys.path.insert(0, tree)
from media_preview_generator.markers import decide as D  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from media_preview_generator.markers.store import MarkerStore  # noqa: E402

assert D.__file__.startswith(tree), D.__file__
# A tree whose credit text reads chapter files too (version 6 on): the work tree of #320, and every tree since.
READS_CHAPTERS = hasattr(D, "chapter_hint")
ORDER = ("chapters", "theintrodb", "introdb", "skipdb", "season_audio", "season_audio_previous", "credits_text",
         "server_markers", "server_markers_imported")  # fmt: skip
LOCAL = {"season_audio", "season_audio_previous", "credits_text"}


def arg(name):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None


TEXT = json.load(open(arg("--text"))) if arg("--text") else {}
AUDIO = json.load(open(arg("--audio"))) if arg("--audio") else {}
copy = str(HERE / f"replay_copy_{os.getpid()}.db")
shutil.copy(HERE / "local/audit/db/markers.db", copy)
store = MarkerStore(copy)
conn = store._conn


def ctx_for(rec, fid, types, order):
    window, cap = D.credits_limits_ms(is_episode=rec.season_key is not None, tv_window_s=None, movie_window_s=None)
    return D.DecisionContext(
        rec.duration_ms or 0, rec.is_movie, D.APP_PUBLISH_WHEN, types, order, store.get_intro_chapter_limit(fid)[1],
        movie_credits_max_from_end_ms=cap, credits_window_ms=window, frame_rate=store.get_frame_rate(fid)[1],
    )  # fmt: skip


def text_candidates(path, ev, rec):
    r = TEXT.get(path)
    if not r or "error" in r or r.get("start_s") is None:
        return []
    start_ms, origin = round(r["start_s"] * 1000), ""
    if READS_CHAPTERS:
        from media_preview_generator.markers.credits import rule_j
        from tools.markers_eval.decode_cache import rows_from_json

        window, cap = D.credits_limits_ms(is_episode=rec.season_key is not None, tv_window_s=None, movie_window_s=None)
        chapter_ms = D.credits_chapter_start_ms(
            ev,
            duration_ms=rec.duration_ms,
            is_movie=rec.is_movie,
            credits_window_ms=window,
            movie_credits_max_from_end_ms=cap,
        )
        if chapter_ms is not None:
            from media_preview_generator.markers.credits import detector

            overlays = tuple(tuple(b) for b in r["overlays"])
            if hasattr(detector, "chapter_origin"):
                # The tree's own label, exactly as the detector builds it.
                found = detector.CreditsTextResult(r["start_s"], r["end_s"], tuple(rows_from_json(r["key"])), (), (),
                                                   overlays)  # fmt: skip
                origin = detector.chapter_origin(found, chapter_ms)
            else:
                rows = rule_j.without_overlays(rows_from_json(r["key"]), overlays)
                origin = D.chapter_hint(chapter_ms, moves=rule_j.moves_chapter(rows, r["start_s"], chapter_ms / 1000))
    end = None if r["end_s"] is None else round(r["end_s"] * 1000)
    return [Candidate(MarkerType.CREDITS, start_ms, end, Source.CREDITS_TEXT, origin=origin)]


out = {}
for fid, path in conn.execute("select id, canonical_path from files where missing_since is null order by id"):
    if not os.path.exists(path):
        continue
    rec = store.get_file(path)
    rows = store.get_decisions(fid)
    types = frozenset(
        t for t in (MarkerType.INTRO, MarkerType.CREDITS) if t in rows and rows[t].reason != "detection off"
    )
    if not types:
        continue
    ev = [c for c in store.get_evidence(fid) if c.source.value in ORDER]
    fetched = {s for s in LOCAL if store.evidence_fetched_at(fid, Source(s)) is not None}
    # Credit text as this tree reads it.
    text_read = "credits_text" in fetched
    if MarkerType.CREDITS in types and path in TEXT and "error" not in TEXT[path]:
        stored_by = (
            set(
                json.loads(
                    conn.execute(
                        "select decided_by from markers where file_id=? and type='credits'", (fid,)
                    ).fetchone()[0]
                    or "[]"
                )
            )
            if conn.execute("select 1 from markers where file_id=? and type='credits'", (fid,)).fetchone()
            else set()
        )
        chapter_only = bool(stored_by) and stored_by <= {"chapters", "server_markers", "server_markers_imported"}
        if text_read or (READS_CHAPTERS and chapter_only):
            ev = [c for c in ev if c.source is not Source.CREDITS_TEXT] + text_candidates(path, ev, rec)
            text_read = True
            fetched.add("credits_text")
    if path in AUDIO:
        ev = [c for c in ev if c.source not in (Source.SEASON_AUDIO, Source.SEASON_AUDIO_PREVIOUS)]
        a = AUDIO[path]
        if a is not None:
            ev.append(Candidate(MarkerType.INTRO, round(a[0] * 1000), round(a[1] * 1000), Source.SEASON_AUDIO))
        fetched.add("season_audio")
    answered = {c.source.value for c in ev}
    order = tuple(s for s in ORDER if s in answered or s not in LOCAL or s not in fetched)
    decisions = D.decide(ev, ctx_for(rec, fid, types, order), store.get_locked(fid))
    markers = store.get_markers(fid)
    for t in types:
        d = decisions[t]
        raw_reason = d.reason
        stored = rows[t]
        pub = markers.get(t)
        if (
            d.status in (D.DecisionStatus.NEEDS_REVIEW, D.DecisionStatus.NO_EVIDENCE)
            and pub is not None
            and stored.status.value == "decided"
            and hasattr(D, "keep_published")
        ):
            cands = [c for c in ev if c.type is t and not c.stale]
            kwargs = {}
            if READS_CHAPTERS:
                kwargs["read_by"] = [
                    Source(s)
                    for s in ("season_audio", "credits_text")
                    if s in fetched and ((s == "season_audio") == (t is MarkerType.INTRO))
                ]
            d = D.keep_published(d, pub, candidates=cands, changed=[], duration_ms=rec.duration_ms or 0, **kwargs)
        m = d.marker or d.proposed
        out[f"{fid}:{t.value}"] = {
            "path": path,
            "status": d.status.value,
            "reason": d.reason,
            "marker": None if m is None else [m.start_ms, m.end_ms, list(m.decided_by)],
            "stored_status": stored.status.value,
            "stored_reason": stored.reason,
            "stored_marker": None if pub is None else [pub.start_ms, pub.end_ms, list(pub.decided_by)],
            "text_read": text_read,
            "raw_reason": raw_reason,
        }
json.dump(out, open(sys.argv[2], "w"), indent=0)
store._conn.close()
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(copy + suffix):
        os.remove(copy + suffix)
print(which, "rows", len(out))
