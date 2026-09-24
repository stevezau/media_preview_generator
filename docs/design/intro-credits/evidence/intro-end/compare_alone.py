import json, os, sys
sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3")
from redecide import run
from tools.markers_eval.score import judge_intro
name = sys.argv[1]
ev = json.load(open(f"evidence_{name}.json"))
res = run(name, quiet=True)
for f, row in ev.items():
    tr = tuple(row["truth"]) if row["truth"] else None
    a = row["audio"]
    alone = judge_intro((a[0], a[1]) if a else None, tr) if tr else ("wrong" if a else "none-ok")
    pf = row["plex_first"]
    plexv = judge_intro((pf[0]/1000, pf[1]/1000) if pf else None, tr) if tr else ("wrong" if pf else "none-ok")
    v, seg, d = res[f]
    if v != alone or "South Park" in f or plexv == "wrong":
        print(f"{os.path.basename(f)[:70]:70s} alone={alone:7s} decide={v:7s} plex={plexv:7s} tr={tr} seg={seg} idb={row['introdb']} plex={row['plex_server']} audio={a and [round(a[0],2), round(a[1],2), a[2]]} reason={d.reason}")
