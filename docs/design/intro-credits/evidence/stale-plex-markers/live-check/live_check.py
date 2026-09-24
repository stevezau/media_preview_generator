"""Read-only live-data check of the stale rule: prod Plex export (fresh) + prod markers.db snapshot."""

import collections
import csv
import json
import pickle
import sqlite3
import sys
from datetime import datetime, timezone

WT = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-af00f7652b351cf9c"
sys.path.insert(0, WT)
from media_preview_generator.markers.models import MarkerType  # noqa: E402
from media_preview_generator.markers.publishers import plex_db  # noqa: E402

SP = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad"
D = f"{SP}/stale_fresh"
csv.field_size_limit(10**9)
TOL = {MarkerType.INTRO: 5_000, MarkerType.CREDITS: 10_000}


def rows(name):
    with open(f"{D}/{name}", newline="") as f:
        yield from csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE)


def intn(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


parts_by_mid = collections.defaultdict(list)
part_info = {}
unreadable_parts = 0
for p in rows("parts.tsv"):
    part_info[p["file"]] = dict(
        mid=intn(p["mid"]),
        fps=float(p["fps"]) if p["fps"] else None,
        dur=intn(p["dur"]),
        show=p["show"],
        mtime=intn(p["mp_updated"]),
    )
for p in json.load(open(f"{D}/parts_raw.json")):
    parts_by_mid[p["mid"]].append(plex_db._Part(p["part_id"], 0, p["file"], p["extra"], None, p["mp_updated"]))

tags_by_mid = collections.defaultdict(list)
for t in json.load(open(f"{D}/taggings_raw.json")):
    tags_by_mid[t["mid"]].append(
        plex_db._TaggingRow(
            t["id"], t["index"] or 0, t["text"], t["time_offset"], t["end_time_offset"], None, t["extra_data"],
            t["created_at"],
        )
    )

con = sqlite3.connect(f"file:{SP}/prod_markers.db?mode=ro", uri=True)
our_items = {
    int(r[0])
    for r in con.execute(
        "select item_id from item_publish_state where markers_json != '[]' "
        "union select item_id from publish_state where item_id is not null and markers_json != '[]'"
    )
}
files = {path: fid for fid, path in con.execute("select id, canonical_path from files")}
decisions = {}
STATUS = sys.argv[1] if len(sys.argv) > 1 else 'decided'
for fid, typ, s, e, reason in con.execute(
    "select file_id, type, proposed_start_ms, proposed_end_ms, reason from decisions where status=?", (STATUS,)
):
    if typ in ("intro", "credits") and s is not None:
        decisions[(fid, MarkerType(typ))] = (s, e, reason)
evidence = collections.defaultdict(list)
for fid, src, typ, s, e, detail in con.execute(
    "select file_id, source, type, start_ms, end_ms, detail from evidence where type in ('intro','credits')"
):
    evidence[(fid, MarkerType(typ))].append((src, s, e, detail))

if STATUS == "replay":
    import shutil
    from dataclasses import replace

    from media_preview_generator.markers import pipeline as mp
    from media_preview_generator.markers.decide import DecisionContext, decide
    from media_preview_generator.markers.settings import load_global
    from media_preview_generator.markers.store import MarkerStore

    shutil.copy(f"{SP}/prod_markers.db", f"{D}/markers_copy.db")
    store = MarkerStore(f"{D}/markers_copy.db")
    gs = load_global(json.load(open(f"{D}/prod_markers_settings.json")))
    order = mp._decision_order(gs)
    frec = {
        fid: (dur, bool(movie), season)
        for fid, dur, movie, season in con.execute("select id, duration_ms, is_movie, season_key from files")
    }
    TYPES = frozenset({MarkerType.INTRO, MarkerType.CREDITS})

    def replay(fid, mtype, stale_flag):
        ev = store.get_evidence(fid)
        if stale_flag:
            ev = [replace(c, stale=True) if c.source.value == "server_markers" and c.type is mtype else c for c in ev]
        dur, is_movie, season_key = frec[fid]
        cw, cap = mp.credits_limits_ms(
            is_episode=season_key is not None, tv_window_s=gs.credits_tv_s, movie_window_s=gs.credits_movie_s
        )
        known, limit = store.get_intro_chapter_limit(fid)
        dctx = DecisionContext(
            dur or 0, is_movie, mp.APP_PUBLISH_WHEN, TYPES, order, limit if known else None,
            movie_credits_max_from_end_ms=cap, credits_window_ms=cw,
        )
        return decide([c for c in ev if c.source.value in set(order)], dctx, store.get_locked(fid))[mtype]


# Sonarr imports: latest import date per path, and earlier imports of the same episode under another name
imports_by_path = collections.defaultdict(list)
imports_by_ep = collections.defaultdict(list)
for line in open(f"{SP}/stale/sonarr_imports.jsonl"):
    d = json.loads(line)
    ts = int(datetime.fromisoformat(d["date"].replace("Z", "+00:00")).timestamp())
    imports_by_path[d["importedPath"]].append(ts)
    imports_by_ep[d["episodeId"]].append((ts, d["importedPath"]))
def tv_key(path):
    i = path.find("/TV Shows/")
    return path[i:] if i >= 0 else path


imports_by_path = {tv_key(a): b for a, b in imports_by_path.items()}
imports_by_ep = {ep: [(ts, tv_key(pth)) for ts, pth in lst] for ep, lst in imports_by_ep.items()}
ep_of_path = {}
for ep, lst in imports_by_ep.items():
    for _ts, path in lst:
        ep_of_path[path] = ep
SONARR_FROM = min(min(v) for v in imports_by_path.values())

OUR_START = 1790071200  # 2026-09-22 10:00 UTC: first writes of this app to prod Plex


def is_ours(mid, row):
    return mid in our_items and (row.created_at or 0) >= OUR_START


def fps_class(x):
    if x is None:
        return "?"
    for name, v, tol in (
        ("25", 25, 0.05),
        ("23.976", 23.976, 0.01),
        ("24", 24, 0.05),
        ("29.97", 29.97, 0.02),
        ("50", 50, 0.1),
        ("59.94", 59.94, 0.05),
        ("30", 30, 0.05),
    ):
        if abs(x - v) < tol:
            return name
    return f"{x:.2f}"


def how_replaced(path, marker_at, mtime):
    """sonarr-new-name / sonarr-same-name / in-place (Tdarr or other) / unknown (outside Sonarr history or a movie)."""
    if "/Movies/" in path:
        return "movie"
    path = tv_key(path)
    after = [ts for ts in imports_by_path.get(path, []) if ts > marker_at]
    if after:
        ep = ep_of_path.get(path)
        earlier_names = {p for ts, p in imports_by_ep.get(ep, []) if p != path}
        return "sonarr-new-name" if earlier_names else "sonarr-import"
    if marker_at >= SONARR_FROM:
        return "in-place (no Sonarr import since)"
    if imports_by_path.get(path):
        return "in-place (no Sonarr import since)"
    return "unknown (before Sonarr history)"


results = []
transitions = collections.Counter()
flag_counts = collections.Counter()
for mid, trows in tags_by_mid.items():
    parts = parts_by_mid.get(mid)
    if not parts:
        continue
    stale = plex_db._types_not_made_for_file(trows, parts)
    for mtype in stale:
        text = mtype.value
        of_type = [r for r in trows if r.text == text]
        own = [r for r in of_type if not is_ours(mid, r)]
        if len(own) != len(of_type):
            flag_counts["stale-but-rows-ours"] += 1
            continue
        in_scope = [p for p in parts if p.file in files]
        flag_counts[("stale", text, "in-markers.db" if in_scope else "not-in-markers.db")] += 1
        for part in in_scope:
            fid = files[part.file]
            before = None
            if STATUS == "replay":
                after, before = replay(fid, mtype, True), replay(fid, mtype, False)
                transitions[(text, before.status.value, after.status.value)] += 1
                m = after.marker if after.marker is not None else after.proposed
                dec = (m.start_ms, m.end_ms, after.reason + f" [{after.status.value}]") if m is not None else None
                if dec is not None and after.marker is None:
                    dec = None  # proposed only: not an answer of ours that would be published
            else:
                dec = decisions.get((fid, mtype))
            if dec is None:
                flag_counts[("stale-in-scope-no-decided-answer", text)] += 1
                continue
            served = [
                plex_db._served_times(mtype, r.time_offset, r.end_time_offset, plex_db._row_is_final(r.extra_data))
                for r in own
            ]
            ps, pe = min(served, key=lambda se: abs(se[0] - dec[0]) + abs(se[1] - dec[1]))
            ds, de = abs(ps - dec[0]), abs(pe - dec[1])
            info = part_info[part.file]
            marker_at = max(r.created_at for r in own)
            final_credit_end = [
                r.end_time_offset for r in trows if r.text == "credits" and plex_db._row_is_final(r.extra_data)
            ]
            old_dur_ratio = max(final_credit_end) / info["dur"] if final_credit_end and info["dur"] else None
            speed = "?"
            if old_dur_ratio is not None:
                if abs(old_dur_ratio - 25 / 23.976) < 0.006 or abs(old_dur_ratio - 25 / 24) < 0.006:
                    speed = "speed-change (old longer ~4%)"
                elif abs(old_dur_ratio - 23.976 / 25) < 0.006 or abs(old_dur_ratio - 24 / 25) < 0.006:
                    speed = "speed-change (old shorter ~4%)"
                elif abs(old_dur_ratio - 1) < 0.006:
                    speed = "same-speed"
                else:
                    speed = f"other ({old_dur_ratio:.3f})"
            ratio = ps / dec[0] if dec[0] else None
            results.append(
                dict(
                    mid=mid,
                    fid=fid,
                    path=part.file,
                    type=text,
                    plex=(ps, pe),
                    ours=dec[:2],
                    reason=dec[2],
                    d_start=ds,
                    d_end=de,
                    agree=max(ds, de) <= TOL[mtype],
                    agree_start=ds <= TOL[mtype],
                    fps=fps_class(info["fps"]),
                    dur=info["dur"],
                    speed=speed,
                    old_dur_ratio=old_dur_ratio,
                    start_ratio=ratio,
                    replaced=how_replaced(part.file, marker_at, info["mtime"]),
                    marker_at=marker_at,
                    mtime=info["mtime"],
                    evidence=evidence.get((fid, mtype), []),
                    show=info["show"],
                    before=None if before is None else (before.status.value, before.reason,
                                                        before.marker and (before.marker.start_ms, before.marker.end_ms)),
                )
            )

pickle.dump(results, open(f"{D}/live_check_{STATUS}.pkl", "wb"))
print("unreadable parts (pv key not JSON):", unreadable_parts)
for k, v in sorted(transitions.items()):
    print("transition before->after", k, v)
for k, v in sorted(flag_counts.items(), key=str):
    print(k, v)
print("stale type x file with a decided answer of ours:", len(results))
by = collections.Counter((r["type"], "agree" if r["agree"] else "disagree") for r in results)
print(by)
print("agree on start only:", collections.Counter((r["type"], r["agree_start"]) for r in results))
for key in ("speed", "fps", "replaced"):
    c = collections.Counter((r["type"], r[key], "agree" if r["agree"] else "disagree") for r in results)
    print(f"-- by {key}")
    for k, v in sorted(c.items(), key=str):
        print("  ", k, v)
print(
    "-- reasons of ours that lean on server_markers:",
    collections.Counter(
        (r["type"], r["agree"]) for r in results if "server_markers" in r["reason"] or "server's own" in r["reason"]
    ),
)
