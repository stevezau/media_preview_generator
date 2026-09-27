"""Offline: the app's own season step (usage: DB=<copy of markers.db> [OWN_FOLDER=1] [CODE=tree] season_repro.py
<path substring>...)

Offline: the app's own season step (_matching + _intro) for one episode, from a markers.db copy (cached fingerprints,
pair runs and end-picture shares as sflix stored them). CODE env picks the code tree. Prints each candidate cluster with
its support and why it was passed over."""

import datetime as dt
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE  # noqa: E402

sys.path.insert(0, CODE)
import media_preview_generator.markers.audio.season as S
from media_preview_generator.markers.audio import matcher as M
from media_preview_generator.markers.store import MarkerStore

assert S.__file__.startswith(CODE)
DB = os.environ["DB"]  # a copy of a markers.db: a pair not cached yet is matched and stored in it
store = MarkerStore(DB)
ctx = types.SimpleNamespace(
    store=store,
    config=types.SimpleNamespace(ffmpeg_path="/usr/bin/ffmpeg"),
    force=False,
    now=lambda: dt.datetime.now(dt.UTC),
    ffprobe="/usr/bin/ffprobe",
)
LIB = None


def group_of(path):
    from media_preview_generator.servers.base import Library, ServerConfig, ServerType

    roots = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
    lib = ServerConfig(
        id="plex",
        type=ServerType.PLEX,
        name="Plex",
        enabled=True,
        url="http://plex",
        auth={},
        libraries=[Library("2", "TV Shows", tuple(f"{r}/TV Shows" for r in roots))],
    )
    if os.environ.get("OWN_FOLDER"):
        return S.season_group(path).episodes
    return S.season_group(path, S.season_videos(path, [lib])).episodes


def run(target, verbose=True):
    rec = store.get_file(target)
    eps = group_of(target)
    records, points = {target: rec}, {target: S._cached_points(ctx, rec)}
    for p in eps:
        if p == target:
            continue
        m = store.get_file(p)
        if m is None:
            print("   no record", os.path.basename(p)[:80])
            continue
        pts = S._cached_points(ctx, m)
        if pts is None:
            print("   no fingerprint", os.path.basename(p)[:80])
            continue
        records[p], points[p] = m, pts

    def retimed(member, factor):
        return S._cached_retimed(ctx, rec, member, factor) if hasattr(S, "_cached_retimed") else None

    matching = S._matching(ctx, rec, records, points, group_size=len(eps), previous_files=None, retimed=retimed)
    if matching is None:
        print("  matching None")
        return None
    ep = S._EndPictures(ctx, rec, records, decode=bool(os.environ.get("DECODE")))
    # instrument the pick
    files = matching.files
    read = {}

    def rb(a, b):
        if (a, b) not in read:
            read[(a, b)] = S._cached_runs(
                ctx, records, a, b, matching.points[a], matching.points[b], matching.clock.pair_version(a, b)
            )
        return read[(a, b)]

    others = len(files) - 1
    cands = M.intro_candidates(M.file_hits(target, files, rb))
    memo = {}
    if verbose:
        print(f"  group {len(eps)} eps, matched {len(files)} files; quorum {max(1, M.QUORUM * others)}")
        for c in cands[:8]:
            seg = c.segment
            q = M.meets_quorum(seg.support, others)
            oq = (
                M.meets_opening_quorum(target, c, files, rb, S._season_and_episode, memo)
                if hasattr(M, "meets_opening_quorum")
                else None
            )
            try:
                g = S._passes_guards(
                    target, c, matching.points, lambda cand: ep(S.in_own_times(cand, target, matching.clock.factors))
                )
            except Exception as e:
                g = f"ERR {type(e).__name__} {e}"
            extra = ""
            try:
                dc = S.dense_core_s(target, c, matching.points) if S.needs_dense_core(seg) else None
                cw = S.cut_by_window(seg, matching.points[target]) if hasattr(S, "cut_by_window") else None
                extra = f" dense_core {dc} cut_by_window {cw}"
                if S.end_picture.is_early(seg.start_s):
                    try:
                        shares = []
                        for hit in S.end_picture.partners(S.in_own_times(c, target, matching.clock.factors).members):
                            key = S.EndPictureKey(
                                rec.id,
                                records[hit.partner].id,
                                round(seg.start_s * 1000),
                                round(seg.end_s * 1000),
                                round((hit.partner_start_s - hit.start_s) * 1000),
                            )
                            got = store.get_end_picture(key, S.end_picture.CHECK_VERSION)
                            shares.append(None if got is None else ("share", got.share))
                        extra += f" endpic {shares} passes={S.end_picture.passes([x[1] for x in shares if x])}"
                    except Exception as e:
                        extra += f" endpicERR {e}"
            except Exception as e:
                extra = f" ERR {e}"
            print(
                f"   cand {seg.start_s:8.2f}-{seg.end_s:8.2f} support {seg.support:2d} quorum {q} opening_q {oq} guards {g} floats {M.floats(c) if hasattr(M, 'floats') else None}{extra}"
            )
    seg = S._intro(ctx, target, matching, records, ep)
    print("  ANSWER", None if seg is None else (round(seg.start_s, 2), round(seg.end_s, 2), seg.support))
    return seg


if __name__ == "__main__":
    for pat in sys.argv[1:]:
        rows = store._conn.execute(
            "select canonical_path from files where canonical_path like ? and missing_since is null", (f"%{pat}%",)
        ).fetchall()
        for (p,) in rows:
            print("##", p[-100:])
            run(p)
