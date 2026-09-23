import re, subprocess, sys, time, numpy as np
f=sys.argv[1]; start=float(sys.argv[2]); mode=sys.argv[3]; W,H=320,180
if mode=="cuda":
    cmd=["ffmpeg","-nostdin","-v","info","-hide_banner","-hwaccel","cuda","-hwaccel_output_format","cuda","-skip_frame","nokey","-copyts","-ss",f"{start}","-i",f,"-an","-sn","-vf",f"scale_cuda={W}:{H},hwdownload,format=p010le,format=gray,showinfo","-fps_mode","passthrough","-f","rawvideo","-"]
else:
    cmd=["ffmpeg","-nostdin","-v","info","-hide_banner","-skip_frame","nokey","-copyts","-ss",f"{start}","-i",f,"-an","-sn","-vf",f"scale={W}:{H},format=gray,showinfo","-fps_mode","passthrough","-f","rawvideo","-"]
t0=time.time(); r=subprocess.run(cmd,capture_output=True)
pts=[float(x) for x in re.findall(rb"pts_time:([\d.]+)", r.stderr)]
n=len(r.stdout)//(W*H); frames=np.frombuffer(r.stdout[:n*W*H],dtype=np.uint8).reshape(n,H,W)
print(mode, "secs", round(time.time()-t0,1), "frames", n, "pts", len(pts), "first", pts[:2], "last", pts[-1:] if pts else None)
if r.returncode: print(r.stderr[-400:])
for p,fr in zip(pts,frames):
    if 6690 <= p <= 6735: print(f"  t={p:.1f} luma={fr.mean():.1f}")
