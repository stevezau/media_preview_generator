"""Throwaway v2: multi-run candidates + quorum consensus. Reuses fingerprints cached by path."""
import concurrent.futures as cf, hashlib, os
import numpy as np
import fp
CACHE = os.path.join(os.path.dirname(__file__), "fpcache"); os.makedirs(CACHE, exist_ok=True)
MIN_S = 8
QUORUM = 0.5
def cached_fp(f, start, length):
    k = hashlib.sha1(f"{f}|{start:.1f}|{length:.1f}".encode()).hexdigest()
    p = os.path.join(CACHE, k + ".npy")
    if os.path.exists(p): return np.load(p)
    a = fp.fingerprint(f, start, length); np.save(p, a); return a
def runs(a, b, min_pts):
    index = {}
    for i, v in enumerate(b.tolist()): index.setdefault(v, []).append(i)
    shifts = {}
    for i, v in enumerate(a.tolist()):
        for d in (-2,-1,0,1,2):
            for j in index.get(v+d, ()): shifts[j-i] = shifts.get(j-i, 0) + 1
    gap = int(fp.MAX_GAP_S / fp.POINT_S); out = []
    for s, c in sorted(shifts.items(), key=lambda kv: -kv[1])[:40]:
        a0, b0 = (0, s) if s >= 0 else (-s, 0); n = min(len(a)-a0, len(b)-b0)
        if n <= 0: continue
        ok = fp.popcount32(a[a0:a0+n] ^ b[b0:b0+n]) <= fp.MAX_BIT_DIFF
        idx = np.flatnonzero(ok)
        if len(idx) < 2: continue
        br = np.flatnonzero(np.diff(idx) > gap)
        st = np.concatenate(([idx[0]], idx[br+1])); en = np.concatenate((idx[br], [idx[-1]]))
        for x, y in zip(st, en):
            if y - x >= min_pts: out.append(((a0+x)*fp.POINT_S, (a0+y)*fp.POINT_S, (b0+x)*fp.POINT_S, (b0+y)*fp.POINT_S))
    # dedupe overlapping runs on side a (keep longest)
    out.sort(key=lambda r: -(r[1]-r[0])); keep = []
    for r in out:
        if all(not (r[0] < k[1] and k[0] < r[1]) for k in keep): keep.append(r)
    return keep
def analyse(files, window, max_len=120):
    durs = {f: fp.duration_s(f) for f in files}
    def get(f):
        if window == "intro": return cached_fp(f, 0, min(fp.INTRO_WINDOW_S, durs[f]*0.25 if durs[f] >= 300 else durs[f]))
        s = max(0.0, durs[f]-fp.CREDITS_WINDOW_S); return cached_fp(f, s, durs[f]-s)
    with cf.ThreadPoolExecutor(8) as ex: fps = dict(zip(files, ex.map(get, files)))
    hits = {f: [] for f in files}; min_pts = int(MIN_S / fp.POINT_S)
    for i, fa in enumerate(files):
        for fb in files[i+1:]:
            for a0, a1, b0, b1 in runs(fps[fa], fps[fb], min_pts):
                if window == "intro" and a1 - a0 > max_len: continue
                oa = 0 if window == "intro" else max(0.0, durs[fa]-fp.CREDITS_WINDOW_S)
                ob = 0 if window == "intro" else max(0.0, durs[fb]-fp.CREDITS_WINDOW_S)
                hits[fa].append((a0+oa, a1+oa, fb)); hits[fb].append((b0+ob, b1+ob, fa))
    out = {}
    others = len(files) - 1
    for f in files:
        best = None
        for s, e, _ in hits[f]:
            sup = {p for s2, e2, p in hits[f] if abs(s2-s) <= 4 and abs(e2-e) <= 4}
            if best is None or len(sup) > best[2] or (len(sup) == best[2] and (e - s) > (best[1] - best[0])):
                grp = [(s2, e2) for s2, e2, p in hits[f] if abs(s2-s) <= 4 and abs(e2-e) <= 4]
                best = (float(np.median([g[0] for g in grp])), float(np.median([g[1] for g in grp])), len(sup))
        if best and best[2] < max(1, QUORUM * others): best = None
        out[f] = {"duration": durs[f], "pairs": len(hits[f]), "segment": best}
    return out
