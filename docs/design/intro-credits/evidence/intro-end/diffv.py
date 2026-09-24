import json, os, sys
a, b = (json.load(open(f"verdicts_{n}.json")) for n in sys.argv[1:3])
for s in a:
    ev = json.load(open(f"evidence_{s}.json"))
    for f in a[s]:
        va, sa = a[s][f]; vb, sb = b[s][f]
        if sa != sb:
            r = ev[f]
            print(f"{s:10s} {os.path.basename(f)[:60]:60s} {va}->{vb} truth={r['truth']} {sa}->{sb} audio={r['audio'] and [round(x,2) for x in r['audio'][:2]]} idb={r['introdb']} plex={r['plex_server']}")
