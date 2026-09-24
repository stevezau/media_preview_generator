import os
import pickle

D = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh"
res = pickle.load(open(f"{D}/live_check_replay.pkl", "rb"))
want = (
    "Physical 100 Mexico (2026) - S01E01",
    "Somebody Somewhere (2022) - S03E02",
    "S07E09 - Deadly Ex-marine",
    "A Scanner Darkly",
    "Accused Guilty or Innocent (2020) - S03E09",
)
for r in res:
    if not r["agree"] and any(w in r["path"] for w in want):
        print(r["type"], r["plex"], r["ours"], "exists:", os.path.exists(r["path"]), r["path"])
        for e in r["evidence"]:
            print("    ", e[0], e[1], e[2], e[3][:90])
