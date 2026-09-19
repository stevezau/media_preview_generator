"""Intro eval with fingerprint input variants (algorithm / downmix). v3 matcher params fixed."""
import json, sys, functools, subprocess, hashlib, os
from collections import defaultdict
import numpy as np
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/detect")
import fp, fp3
fp.duration_s = functools.lru_cache(None)(fp.duration_s)
R=json.load(open("/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/eval/eval_results_v3.json"))
seasons=defaultdict(list); truth={}
for r in R: seasons[r["season"]].append(r["file"]); truth[r["file"]]=r["truth"].get("intro")
VARIANTS={
 "alg1_ac2":  ["-ac","2","-f","chromaprint","-algorithm","1"],
 "alg4_ac2":  ["-ac","2","-f","chromaprint","-algorithm","4"],
 "alg1_front":["-af","pan=stereo|c0=FL|c1=FR","-f","chromaprint","-algorithm","1"],
 "alg0_ac2":  ["-ac","2","-f","chromaprint","-algorithm","0"],
}
CACHE="/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/detect/fpcache_var"; os.makedirs(CACHE, exist_ok=True)
def make_fp(opts):
    def fingerprint(path, start, length):
        k=hashlib.sha1(f"{path}|{start}|{length}|{opts}".encode()).hexdigest(); pth=os.path.join(CACHE,k+".npy")
        if os.path.exists(pth): return np.load(pth)
        cmd=["ffmpeg","-v","error","-ss",f"{start:.3f}","-i",path,"-t",f"{length:.3f}","-vn","-sn","-dn",*opts,"-fp_format","raw","-"]
        r=subprocess.run(cmd,capture_output=True)
        a=np.frombuffer(r.stdout,dtype="<u4"); np.save(pth,a); return a
    return fingerprint
for name, opts in VARIANTS.items():
    fp.POINT_S = (2048/2/11025) if "alg4" in name else 4096/11025/3
    fp3.cached_fp = make_fp(opts)
    n=u=w=m=0
    for sn, files in seasons.items():
        res=fp3.analyse(files,"intro")
        for f in files:
            t=truth[f]
            if not t: continue
            n+=1; d=res[f]["segment"]
            if not d: m+=1
            elif abs(d[1]-t[1])<=5 and abs(d[0]-t[0])<=15: u+=1
            else: w+=1
    print(json.dumps({"variant":name,"n":n,"useful":u,"wrong":w,"missed":m,"precision":round(u/max(1,u+w),3)}), flush=True)
