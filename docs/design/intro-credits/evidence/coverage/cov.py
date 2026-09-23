import json, re, subprocess, time, requests
from concurrent.futures import ThreadPoolExecutor
S="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence"
tv=json.load(open("q_sample.json")); mv=json.load(open("q_sample_mv.json"))
sk=json.load(open(f"{S}/online/skipdb-dump.json"))["segments"]
skidx={}
for r in sk: skidx.setdefault((r["imdb_id"], r.get("season"), r.get("episode")), []).append(r)
INTRO=re.compile(r"(?i)\b(intro|opening|title sequence|main title|op)\b"); CRED=re.compile(r"(?i)\b(credits|outro|ending|ed)\b")
def gid(g, p):
    m=re.search(p+r"://([\w]+)", g or ""); return m.group(1) if m else None
def chapters(f):
    try:
        out=subprocess.run(["ffprobe","-v","error","-show_chapters","-of","json",f],capture_output=True,text=True,timeout=40).stdout
        names=[c.get("tags",{}).get("title","") for c in json.loads(out).get("chapters",[])]
        return {"intro": any(INTRO.search(n) for n in names), "credits": any(CRED.search(n) for n in names), "n": len(names)}
    except Exception as e: return {"err": str(e)[:80]}
items=tv+mv
with ThreadPoolExecutor(12) as ex: chs=list(ex.map(lambda x: chapters(x["file"]), items))
out=[]
for x, ch in zip(items, chs):
    tmdb=gid(x["guids"],"tmdb"); imdb=gid(x["guids"],"imdb"); dur=x.get("file_ms") or x.get("duration")
    p={"tmdb_id":tmdb}
    if x["kind"]=="tv": p.update(season=x["season"], episode=x["episode"])
    if dur: p["duration_ms"]=dur
    try:
        r=requests.get("https://api.theintrodb.org/v3/media", params=p, timeout=20); tidb={"status":r.status_code, "body": r.json() if "json" in r.headers.get("content-type","") else None}
    except Exception as e: tidb={"status":str(e)[:60]}
    time.sleep(0.45)
    idb=None
    if x["kind"]=="tv" and imdb:
        try:
            r=requests.get("https://api.introdb.app/segments", params={"imdb_id":imdb,"season":x["season"],"episode":x["episode"]}, timeout=20); idb={"status":r.status_code,"body": r.json() if "json" in r.headers.get("content-type","") else None}
        except Exception as e: idb={"status":str(e)[:60]}
        time.sleep(0.2)
    skr=skidx.get((imdb, x["season"] if x["kind"]=="tv" else None, x["episode"] if x["kind"]=="tv" else None), [])
    out.append({"item":x, "chapters":ch, "tidb":tidb, "idb":idb, "skipdb":[{k:s.get(k) for k in ("segment_type","start_ms","end_ms","duration_ms")} for s in skr]})
json.dump(out, open("coverage_results.json","w"), indent=1); print("done", len(out))
