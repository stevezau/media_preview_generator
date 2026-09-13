"""Find TV seasons whose files carry studio-named intro/credits chapters (eval ground truth)."""
import json, re, subprocess, sys, random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
INTRO = re.compile(r"(?i)\b(intro|opening( credits| titles)?|title sequence|main title|op)\b")
CRED = re.compile(r"(?i)\b(end credits|closing credits|credits|outro|ending|ed)\b")
def chapters(f):
    try:
        out = subprocess.run(["ffprobe","-v","error","-show_chapters","-of","json",str(f)],capture_output=True,text=True,timeout=30).stdout
        return [(c.get("tags",{}).get("title",""), float(c["start_time"]), float(c["end_time"])) for c in json.loads(out).get("chapters",[])]
    except Exception: return []
seasons=[]
for root in ["/data_16tb/TV Shows","/data_16tb2/TV Shows","/data_16tb3/TV Shows"]:
    for show in Path(root).iterdir():
        if not show.is_dir(): continue
        for season in show.iterdir():
            if season.is_dir() and season.name.startswith("Season"): seasons.append(season)
random.seed(7); random.shuffle(seasons)
def probe(season):
    files = sorted(p for p in season.iterdir() if p.suffix in (".mkv",".mp4"))
    if len(files) < 4: return None
    ch = chapters(files[len(files)//2])
    names=[c[0] for c in ch]
    if any(INTRO.search(n) for n in names) or any(CRED.search(n) for n in names):
        return str(season), len(files), names[:8]
    return None
hits=[]
with ThreadPoolExecutor(16) as ex:
    for i,r in enumerate(ex.map(probe, seasons[:1500])):
        if r: hits.append(r)
print(len(seasons), "seasons total; probed 1500; named-chapter seasons:", len(hits), file=sys.stderr)
json.dump(hits, open("named_seasons.json","w"), indent=1)
for h in hits[:60]: print(h[1], h[0].split("TV Shows/")[1], h[2])
