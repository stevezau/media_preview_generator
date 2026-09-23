import json, statistics as st, sys
from collections import defaultdict
R=json.load(open(sys.argv[1] if len(sys.argv)>1 else "eval_results.json"))
TOL=5.0
agg=defaultdict(lambda: defaultdict(int)); errs=defaultdict(list); per=defaultdict(lambda: defaultdict(int))
for r in R:
    sn=r["season"].split("TV Shows/")[1][:45]
    for kind in ("intro","credits"):
        t=r["truth"].get(kind); d=r[kind]["segment"]
        if not t:
            agg[kind]["no_truth"]+=1; 
            if d: agg[kind]["det_without_truth"]+=1
            continue
        agg[kind]["truth"]+=1; per[sn][kind+"_truth"]+=1
        if not d: agg[kind]["missed"]+=1; continue
        ds, de = d[0], d[1]
        if kind=="credits":
            ok = abs(ds-t[0])<=TOL
            err=ds-t[0]
        else:
            ok = abs(ds-t[0])<=TOL and abs(de-t[1])<=TOL
            err=max(abs(ds-t[0]),abs(de-t[1]))
        agg[kind]["correct" if ok else "wrong"]+=1; per[sn][kind+("_ok" if ok else "_bad")]+=1
        if ok: errs[kind].append(abs(err) if kind=="credits" else err)
for k,v in agg.items():
    print(k, dict(v), "median_err_when_ok=%.1fs"%st.median(errs[k]) if errs[k] else "")
print()
for sn,v in sorted(per.items()): print(f"{sn:46} intro {v['intro_ok']}/{v['intro_truth']}  credits {v['credits_ok']}/{v['credits_truth']}")
