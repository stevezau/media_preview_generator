import json, sys, time
sys.path.insert(0, sys.argv[1])
import credits_ocr2 as c
truth=[l.rstrip("\n").split("|") for l in open(sys.argv[2])]
def credits_start(rows, dur):
    rows=sorted(rows)
    dense=[i for i,(t,n,l) in enumerate(rows) if n>=10]
    if not dense: return None, "no dense text"
    # last long dense run (gaps <= 3 samples)
    runs=[]; 
    for i in dense:
        if runs and i-runs[-1][1]<=3: runs[-1][1]=i
        else: runs.append([i,i])
    runs=[r for r in runs if (rows[r[1]][0]-rows[r[0]][0])>=30]
    if not runs: return None, "no long dense run"
    r=max(runs, key=lambda r: rows[r[1]][0]-rows[r[0]][0])
    i=r[0]; misses=0
    while i-1>=0:
        t,n,l=rows[i-1]
        if n>=3 or l<15: i-=1; misses=0
        elif misses==0: i-=1; misses=1
        else: break
    if misses: i+=1
    dark=[j for j in range(max(0,i-2), r[0]+1) if rows[j][2]<15]
    j=dark[0] if dark else i
    return rows[j][0], "ok"
out=[]
for f, tms, dms in truth:
    t0=time.time(); a=c.analyse(f); cs,why=credits_start(a["rows"], a["dur"])
    tr=int(tms)/1000
    rec={"file":f.split("/")[-1][:45],"truth":tr,"det":cs,"err":None if cs is None else round(cs-tr,1),"why":why,"secs":round(time.time()-t0)}
    out.append(rec); print(json.dumps(rec), flush=True)
    json.dump({"results":out,"rows":{f:a["rows"]}}, open(sys.argv[3]+"_"+str(len(out))+".json","w"))
