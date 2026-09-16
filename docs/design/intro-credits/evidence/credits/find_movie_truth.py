import json, random, re, subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
CRED=re.compile(r"(?i)^(end credits|credits|closing credits|end titles)$")
files=[]
for root in ["/data_16tb/Movies","/data_16tb2/Movies","/data_16tb3/Movies"]:
    for d in Path(root).iterdir():
        if d.is_dir():
            for f in d.iterdir():
                if f.suffix in (".mkv",".mp4") and "trailer" not in f.name.lower(): files.append(str(f))
random.seed(3); random.shuffle(files)
def probe(f):
    try:
        j=json.loads(subprocess.run(["ffprobe","-v","error","-show_chapters","-show_entries","format=duration","-of","json",f],capture_output=True,text=True,timeout=40).stdout)
        ch=[(c.get("tags",{}).get("title","").strip(), float(c["start_time"])) for c in j.get("chapters",[])]
        dur=float(j["format"]["duration"])
        hits=[s for n,s in ch if CRED.match(n)]
        if hits and dur*0.75 < hits[-1] < dur: return {"file":f,"duration":dur,"credits_start":hits[-1],"chapters":[n for n,_ in ch][-6:]}
    except Exception: return None
with ThreadPoolExecutor(16) as ex: res=[r for r in ex.map(probe, files[:1200]) if r]
print(len(files),"movies; probed 1200; with credits chapter:",len(res))
json.dump(res, open("movie_credit_truth.json","w"), indent=1)
for r in res[:12]: print(round(r["credits_start"]), round(r["duration"]), r["file"].split("/")[-1][:70], r["chapters"][-3:])
