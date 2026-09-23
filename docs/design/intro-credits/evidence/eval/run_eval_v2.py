"""Eval chromaprint detector vs studio chapter ground truth."""
import json, re, subprocess, sys, random
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "detect"))
import fp2 as fp
INTRO = re.compile(r"(?i)^(intro|opening|opening credits|opening titles|title sequence|main titles?|op)$")
CRED = re.compile(r"(?i)^(credits|end credits|closing credits|outro|ending|ed)$")
def chapters(f):
    out = subprocess.run(["ffprobe","-v","error","-show_chapters","-of","json",str(f)],capture_output=True,text=True).stdout
    return [(c.get("tags",{}).get("title","").strip(), float(c["start_time"]), float(c["end_time"])) for c in json.loads(out).get("chapters",[])]
def truth(f):
    ch = chapters(f); t = {}
    for n,s,e in ch:
        if INTRO.match(n) and "intro" not in t: t["intro"]=(s,e)
        if CRED.match(n): t["credits"]=(s,e)
    return t
hits = json.load(open("named_seasons.json"))
random.seed(11); random.shuffle(hits)
chosen = []
for season, n, names in hits:
    if any(INTRO.match(x.strip()) for x in names) and any(CRED.match(x.strip()) for x in names): chosen.append(season)
chosen = chosen[:int(sys.argv[1])]
results = []
for season in chosen:
    files = sorted(str(p) for p in Path(season).iterdir() if p.suffix in (".mkv",".mp4"))[:8]
    gt = {f: truth(f) for f in files}
    files = [f for f in files if gt[f]]
    if len(files) < 3: continue
    try:
        det = {"intro": fp.analyse(files, "intro"), "credits": fp.analyse(files, "credits")}
    except Exception as ex:
        print("ERR", season, ex, file=sys.stderr); continue
    for f in files:
        results.append({"season": season, "file": f, "truth": gt[f], "intro": det["intro"][f], "credits": det["credits"][f]})
    print("done", season, file=sys.stderr)
    json.dump(results, open("eval_results_v2.json","w"), indent=1)
