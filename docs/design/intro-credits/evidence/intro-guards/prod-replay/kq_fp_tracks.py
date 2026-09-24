import subprocess, sys, numpy as np, os, glob
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews")
from media_preview_generator.markers.audio.matcher import pair_runs
def fp(path, stream, length=int(os.environ.get("LEN", 400))):
    cmd = ["nice","-n","19","/usr/bin/ffmpeg","-nostdin","-v","error","-ss","0","-t",str(length),"-i",path,"-map",f"0:a:{stream}" if stream is not None else "0:a?","-vn","-sn","-dn","-ac","2","-f","chromaprint","-algorithm","1","-fp_format","raw","-"]
    if stream is None: cmd = [c for c in cmd if c not in ("-map","0:a?")]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype="<u4")
files = sys.argv[1:3]
for label, st in (("default", None), ("a:0", 0), ("a:1", 1)):
    try:
        a, b = fp(files[0], st), fp(files[1], st)
    except Exception as e:
        print(label, "ERR", e); continue
    runs = pair_runs(a, b)
    print(label, [(round(r.a_start_s,1), round(r.a_end_s,1), round(r.b_start_s,1), round(r.b_end_s,1)) for r in runs[:6]])
