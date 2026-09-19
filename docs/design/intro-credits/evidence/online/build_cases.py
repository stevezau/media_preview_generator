import json, re
S="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
cases=[]
# Rick and Morty S01: truth = Plex prod values (ours+Plex+SkipDB agree within 2s on intros; ours matched first credits seg)
plex={}
for line in open(f"{S}/lab/prod_plex_truth.txt"):
    f,t,s,e,_,dur,_h = line.rstrip("\n").split("|")
    if not t: continue
    m=re.search(r"S(\d+)E(\d+)",f)
    if not m: continue
    plex.setdefault(f,{"dur":int(dur)/1000,"se":(int(m[1]),int(m[2]))}).setdefault(t,[]).append((int(s)/1000,int(e)/1000))
for f,v in plex.items():
    if "Rick and Morty" in f:
        cases.append({"show":"Rick and Morty","tmdb":60625,"imdb":"tt2861424","season":v["se"][0],"episode":v["se"][1],"dur":v["dur"],
                      "intro":v["intro"][0] if "intro" in v else None,"credits_start":min(c[0] for c in v["credits"]) if "credits" in v else None,"truth_src":"3-source agreement"})
# South Park S01 chapter truth (visually confirmed E03)
sp={1:(8,34,1295,1320),3:(11,37,1299,1323),10:(18,43,1312,1323),11:(9,36,1271,1322),12:(9,35,1293,1322),13:(9,36,1292,1322)}
for e,(a,b,c,d) in sp.items():
    cases.append({"show":"South Park","tmdb":2190,"imdb":"tt0121955","season":1,"episode":e,"dur":d,"intro":(a,b),"credits_start":c,"truth_src":"chapters"})
ids={"The Simpsons":(456,"tt0096697"),"Marvels Daredevil":(61889,"tt3322312"),"Outlander":(56570,"tt3006802"),"How I Met Your Mother":(1100,"tt0460649")}
for r in json.load(open(f"{S}/eval/eval_results_v3.json")):
    show=[k for k in ids if k in r["season"]]
    if not show: continue
    m=re.search(r"S(\d+)E(\d+)", r["file"]); t=r["truth"]
    cases.append({"show":show[0],"tmdb":ids[show[0]][0],"imdb":ids[show[0]][1],"season":int(m[1]),"episode":int(m[2]),"dur":r["intro"]["duration"],
                  "intro":tuple(t["intro"]) if "intro" in t else None,"credits_start":t["credits"][0] if "credits" in t else None,"truth_src":"chapters"})
json.dump(cases, open("cases.json","w"), indent=1); print(len(cases), "cases")
