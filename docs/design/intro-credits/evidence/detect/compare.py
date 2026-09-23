import json, re, sys
from collections import defaultdict
S="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
res=json.load(open(sys.argv[1]))
plex=defaultdict(lambda: {"intro":[], "credits":[]})
for line in open(f"{S}/lab/prod_plex_truth.txt"):
    f,t,s,e,*_ = line.rstrip("\n").split("|")
    if t: plex[f.split("/")[-1]][t].append((int(s)/1000, int(e)/1000))
sk=json.load(open(f"{S}/online/skipdb-dump.json"))["segments"]
ids={"Rick and Morty":"tt2861424","South Park":"tt0121955"}
skip=defaultdict(dict)
for r in sk:
    for n,i in ids.items():
        if r["imdb_id"]==i: skip[(n,r["season"],r["episode"])][r["segment_type"]]=((r["start_ms"] or 0)/1000,(r["end_ms"] or 0)/1000,(r.get("duration_ms") or 0)/1000)
fmt=lambda x: "-" if not x else f"{x[0]:7.1f}-{x[1]:7.1f}"
for d,kinds in res.items():
    show=[n for n in ids if n in d][0]
    print(f"\n### {show}")
    print(f"{'ep':6} {'dur':>6} | {'OURS intro':17} {'n':>2} | {'PLEX intro':17} | {'SKIPDB intro':17} || {'OURS credits':17} {'n':>2} | {'PLEX credits (all)':40} | {'SKIPDB outro':17}")
    for f in kinds["intro"]:
        name=f.split("/")[-1]; m=re.search(r"S(\d+)E(\d+)",name); se=(show,int(m[1]),int(m[2]))
        oi=kinds["intro"][f]; oc=kinds["credits"][f]
        pi=plex[name]["intro"]; pc=plex[name]["credits"]
        si=skip[se].get("intro"); so=skip[se].get("outro")
        print(f"E{se[2]:02d}    {oi['duration']:6.0f} | {fmt(oi['segment'])} {oi['pairs']:2} | {fmt(pi[0] if pi else None)} | {fmt(si)} || {fmt(oc['segment'])} {oc['pairs']:2} | {', '.join(fmt(x) for x in pc):40} | {fmt(so)}")
