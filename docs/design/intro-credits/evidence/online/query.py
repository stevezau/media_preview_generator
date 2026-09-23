import json, time, requests
S="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
cases=json.load(open("cases.json"))
sk=json.load(open(f"{S}/online/skipdb-dump.json"))["segments"]
out=[]
for c in cases:
    r={"case":c}
    try:
        resp=requests.get("https://api.theintrodb.org/v3/media", params={"tmdb_id":c["tmdb"],"season":c["season"],"episode":c["episode"],"duration_ms":int(c["dur"]*1000)}, timeout=20)
        r["tidb_status"]=resp.status_code; r["tidb"]=resp.json() if resp.headers.get("content-type","").startswith("application/json") else None
    except Exception as ex: r["tidb_status"]=str(ex)
    time.sleep(0.5)
    try:
        resp=requests.get("https://api.introdb.app/segments", params={"imdb_id":c["imdb"],"season":c["season"],"episode":c["episode"]}, timeout=20)
        r["idb_status"]=resp.status_code; r["idb"]=resp.json() if resp.headers.get("content-type","").startswith("application/json") else None
    except Exception as ex: r["idb_status"]=str(ex)
    time.sleep(0.5)
    rows=[x for x in sk if x["imdb_id"]==c["imdb"] and x.get("season")==c["season"] and x.get("episode")==c["episode"]]
    def nearest(t):
        cand=[x for x in rows if x["segment_type"]==t and x.get("start_ms") is not None]
        if not cand: return None
        return min(cand, key=lambda x: abs((x.get("duration_ms") or 0)/1000 - c["dur"]))
    r["skipdb"]={"intro":nearest("intro"),"outro":nearest("outro")}
    out.append(r)
json.dump(out, open("online_results.json","w"), indent=1); print("done", len(out))
