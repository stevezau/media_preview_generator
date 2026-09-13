"""Sweep intro matcher params on cached fingerprints; seasons split into tune/validate halves."""
import itertools, json, sys, functools
from collections import defaultdict
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/detect")
import fp, fp3
fp.duration_s = functools.lru_cache(None)(fp.duration_s)
R=json.load(open("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/eval/eval_results_v3.json"))
seasons=defaultdict(list); truth={}
for r in R: seasons[r["season"]].append(r["file"]); truth[r["file"]]=r["truth"].get("intro")
names=sorted(seasons); tune=names[0::2]; val=names[1::2]
def score(ss):
    n=u=w=m=0
    for sn in ss:
        res=fp3.analyse(seasons[sn],"intro")
        for f in seasons[sn]:
            t=truth[f]
            if not t: continue
            n+=1; d=res[f]["segment"]
            if not d: m+=1
            elif abs(d[1]-t[1])<=5 and abs(d[0]-t[0])<=15: u+=1
            else: w+=1
    return n,u,w,m
out=[]
grid=list(itertools.product([4,6,8,10],[2.0,3.5,5.0],[8,12,15],[0.34,0.5,0.67]))
for bits,gap,mins,q in grid:
    fp.MAX_BIT_DIFF=bits; fp.MAX_GAP_S=gap; fp3.MIN_S=mins; fp3.QUORUM=q
    tn,tu,tw,tm=score(tune)
    out.append({"bits":bits,"gap":gap,"min":mins,"quorum":q,"tune":[tn,tu,tw,tm]})
    print(json.dumps(out[-1]), flush=True)
json.dump(out, open("sweep_intro.json","w"))
best=sorted(out, key=lambda o:(o["tune"][1]-2*o["tune"][2]), reverse=True)[:5]
print("TOP (useful-2*wrong) on tune:"); 
for b in best:
    fp.MAX_BIT_DIFF=b["bits"]; fp.MAX_GAP_S=b["gap"]; fp3.MIN_S=b["min"]; fp3.QUORUM=b["quorum"]
    b["val"]=score(val); print(json.dumps(b))
fp.MAX_BIT_DIFF=6; fp.MAX_GAP_S=3.5; fp3.MIN_S=8; fp3.QUORUM=0.5
print("BASELINE v3 val:", score(val), "tune:", score(tune))
