"""Replay the prod season step from prod fingerprints (read-only), listing the ranked clusters and guard verdicts."""
import os, re, sqlite3, sys, collections, pickle
import numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.audio import POINT_S, end_picture
from media_preview_generator.markers.audio.matcher import intro_candidates, file_hits, meets_quorum
from media_preview_generator.markers.audio import season as S

HERE = os.path.dirname(os.path.abspath(__file__))
db = sqlite3.connect(os.path.join(HERE, "prod_markers.db"))
ids, pts = {}, {}
for fid, p, blob in db.execute("select f.id, f.canonical_path, fp.points from fingerprints fp join files f on f.id=fp.file_id where fp.window='intro'"):
    ids[p] = fid; pts[p] = np.frombuffer(blob, dtype="<u4").copy()
path_of = {v: k for k, v in ids.items()}
shares = {}
for a, b, s, e, o, v, sh in db.execute("select file_a,file_b,start_ms,end_ms,offset_ms,check_version,share from season_end_pictures"):
    shares[(a, b, s, e, o)] = sh
DECODE = os.environ.get("DECODE")
reader = end_picture.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0") if DECODE else None
dcache_path = os.path.join(HERE, "kq_decoded_shares.pkl")
dcache = pickle.load(open(dcache_path, "rb")) if os.path.exists(dcache_path) else {}

pair_cache = {}
def runs_between(x, y):
    if (x, y) not in pair_cache:
        pair_cache[(x, y)] = S.season_pair_runs(pts[x], pts[y])
    return pair_cache[(x, y)]

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
def group_of(p):
    if os.environ.get("MERGE"):
        root = next(r for r in ROOTS if p.startswith(r + "/"))
        rel = os.path.dirname(p)[len(root):]
        videos = []
        for r in ROOTS:
            if os.path.isdir(r + rel):
                videos.extend(S.folder_videos(r + rel))
        g = S.season_group(p, videos)
    else:
        g = S.season_group(p)
    return sorted(f for f in g.episodes if f in pts and len(pts[f]))

def ep_shares(target, cand):
    out = []
    for hit in end_picture.partners(cand.members):
        off = hit.partner_start_s - hit.start_s
        key = (ids[target], ids[hit.partner], round(cand.segment.start_s * 1000), round(cand.segment.end_s * 1000), round(off * 1000))
        if key in shares:
            out.append(("prod", shares[key]))
        elif key in dcache:
            out.append(("dec", dcache[key]))
        elif reader is not None:
            try:
                v = reader.share(target, hit.partner, cand.segment.start_s, cand.segment.end_s, off)
            except Exception as exc:
                v = f"ERR {exc}"[:40]
            dcache[key] = v
            out.append(("dec", v))
        else:
            out.append(("?", None))
    return out

def describe(target, limit=8, quiet=False):
    files = group_of(target)
    if target not in files or len(files) < 2:
        return f"  group too small ({len(files)})", None
    others = len(files) - 1
    lines, seen, pick = [], set(), None
    walking = True
    for c in intro_candidates(file_hits(target, files, runs_between)):
        seg = c.segment
        k = (round(seg.start_s, 2), round(seg.end_s, 2))
        if k in seen:
            continue
        seen.add(k)
        q = meets_quorum(seg.support, others)
        fs = not (seg.start_s < S.FILE_START_S and seg.end_s - seg.start_s < S.MIN_FILE_START_LENGTH_S)
        core = S.dense_core_s(target, c, pts)
        early = end_picture.is_early(seg.start_s)
        sh = ep_shares(target, c) if (early and q and fs and core >= S.MIN_DENSE_CORE_S) else []
        known = [v for _, v in sh if isinstance(v, float)]
        ep = (not early) or (end_picture.passes(known) if sh and all(t != "?" for t, _ in sh) else None)
        sil = S._mostly_silence(pts[target], seg)
        verdict = "PICK" if (walking and q and fs and core >= 8 and ep) else ""
        if walking and not q:
            verdict = "STOP(no quorum)"; walking = False
        elif walking and verdict == "PICK":
            pick = seg; walking = False
        lines.append(f"  {seg.start_s:7.1f}-{seg.end_s:7.1f} len={seg.end_s-seg.start_s:5.1f} sup={seg.support}/{others} q={int(q)} fs={int(fs)} core={core:4.1f} early={int(early)} sil={int(sil)} ep={[(t, round(v,2) if isinstance(v,float) else v) for t,v in sh]} {verdict}")
        if len(lines) >= limit:
            break
    if pick is not None and S._mostly_silence(pts[target], pick):
        lines.append("  -> dropped by silence guard"); pick = None
    return "\n".join(lines), pick

if __name__ == "__main__":
    pat = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    targets = sorted(p for p in pts if re.search(pat, p) and os.path.exists(p))
    for t in targets:
        txt, pick = describe(t, limit)
        se = re.search(r"S\d+E\d+", t)
        print(se.group(0) if se else os.path.basename(t)[:40], "pick=", (round(pick.start_s,1), round(pick.end_s,1)) if pick else None)
        print(txt)
    if reader is not None:
        pickle.dump(dcache, open(dcache_path, "wb"))
