"""Does snapping detected intro END to silence / black improve accuracy? Uses v3 detections + chapter truth."""
import json, re, subprocess, statistics as st
from concurrent.futures import ThreadPoolExecutor
R=json.load(open("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/eval/eval_results_v3.json"))
def silences(f, a, b, noise="-50dB", d="0.1"):
    out=subprocess.run(["ffmpeg","-nostdin","-v","info","-ss",f"{a:.2f}","-t",f"{b-a:.2f}","-i",f,"-vn","-af",f"silencedetect=noise={noise}:d={d}","-f","null","-"],capture_output=True,text=True).stderr
    s=[a+float(x) for x in re.findall(r"silence_start: ([\d.]+)",out)]; e=[a+float(x) for x in re.findall(r"silence_end: ([\d.]+)",out)]
    return s, e
def blacks(f, a, b):
    out=subprocess.run(["ffmpeg","-nostdin","-v","info","-ss",f"{a:.2f}","-t",f"{b-a:.2f}","-i",f,"-an","-vf","scale=320:-2,blackdetect=d=0.05:pix_th=0.10:pic_th=0.90","-f","null","-"],capture_output=True,text=True).stderr
    return [a+float(x) for x in re.findall(r"black_start:([\d.]+)",out)]
def work(r):
    t=r["truth"].get("intro"); d=r["intro"]["segment"]
    if not t or not d: return None
    end=d[1]; a=max(0,end-4); b=end+6
    ss,se=silences(r["file"],a,b); bs=blacks(r["file"],a,b)
    def near(c): return min(c, key=lambda x: abs(x-end)) if c else None
    cand={"raw":end,"silence_start":near(ss) or end,"silence_end":near(se) or end,"black_start":near(bs) or end}
    cand["black_else_silence"]= near(bs) if bs else (near(ss) or end)
    return {k: v-t[1] for k,v in cand.items()}
with ThreadPoolExecutor(12) as ex: res=[x for x in ex.map(work, R) if x]
for k in res[0]:
    errs=[abs(x[k]) for x in res]
    print(f"{k:20} n={len(errs)} median|err|={st.median(errs):.2f}s  within1s={sum(e<=1 for e in errs)}  within2s={sum(e<=2 for e in errs)}  within5s={sum(e<=5 for e in errs)}")
