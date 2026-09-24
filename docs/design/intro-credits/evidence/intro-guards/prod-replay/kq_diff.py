import pickle, sys, os, re
d = pickle.load(open("kq_variants_details.pkl", "rb"))
base = sys.argv[1] if len(sys.argv) > 1 else "shipped"
want = sys.argv[2:] 
for mode, dets in d.items():
    for n, det in dets.items():
        if n == base or (want and n not in want):
            continue
        rows = [(f, det[f], dets[base][f]) for f in det if det[f][2] != dets[base][f][2] or (det[f][0] and dets[base][f][0] and abs(det[f][0][1]-dets[base][f][0][1])>0.5)]
        if not rows: continue
        print(f"== {mode} / {n} vs {base}: {len(rows)}")
        for f, mine, b in rows:
            fmt = lambda s: f"{s[0]:.1f}-{s[1]:.1f}" if s else "None"
            tr = mine[1]
            print(f"   {os.path.basename(os.path.dirname(os.path.dirname(f)))[:28]:28} {(re.search(r'S\d+E\d+', f) or re.search(r'.{12}$', f)).group(0):8} truth={fmt(tr) if tr else None:13} {base}={fmt(b[0]):13} {b[2]:7} -> {fmt(mine[0]):13} {mine[2]}")
