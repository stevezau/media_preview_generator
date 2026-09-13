"""Throwaway: movie credits via exact-seek frame samples + text detection + darkness."""
import json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from rapidocr_onnxruntime import RapidOCR
W, H, STEP, WINDOW = 320, 180, 5.0, 900
ocr = RapidOCR()
def duration(p): return float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",p],capture_output=True,text=True).stdout)
def grab(p, t):
    r = subprocess.run(["ffmpeg","-v","error","-ss",f"{t:.2f}","-i",p,"-frames:v","1","-an","-sn","-vf",f"scale={W}:{H},format=gray","-f","rawvideo","-"],capture_output=True)
    return t, (np.frombuffer(r.stdout,dtype=np.uint8).reshape(H,W) if len(r.stdout)==W*H else None)
def analyse(p):
    t0=time.time(); d=duration(p); ts=np.arange(max(0,d-WINDOW), d-1, STEP)
    with ThreadPoolExecutor(6) as ex: imgs=list(ex.map(lambda t: grab(p,t), ts))
    t1=time.time(); rows=[]
    for t,g in imgs:
        if g is None: continue
        b,_=ocr.text_det(np.stack([g]*3,axis=-1)); rows.append((float(t), 0 if b is None else len(b), float(g.mean())))
    return {"file":p.split("/")[-1][:50],"dur":round(d),"decode_s":round(t1-t0),"ocr_s":round(time.time()-t1),"rows":rows}
if __name__=="__main__":
    out=[analyse(f) for f in sys.argv[1:]]
    json.dump(out, open("ocr320_rows.json","w"))
    for o in out: print(o["file"], o["dur"], o["decode_s"], o["ocr_s"], " ".join(f"{t:.0f}:{n}/{l:.0f}" for t,n,l in o["rows"][-80:]))
