import json, os, collections, statistics
for s in ("heldout175", "lab118"):
    ev = json.load(open(f"evidence_{s}.json"))
    a = json.load(open("verdicts_base.json"))[s]; b = json.load(open("verdicts_file_edge.json"))[s]
    ea, eb = [], []
    for f in a:
        tr = ev[f]["truth"]
        if tr and a[f][1] and b[f][1] and a[f][1] != b[f][1]:
            ea.append(abs(a[f][1][1] - tr[1])); eb.append(abs(b[f][1][1] - tr[1]))
    print(s, "changed", len(ea), "mean |end err| base %.2f file_edge %.2f" % (statistics.mean(ea), statistics.mean(eb)),
          "median %.2f %.2f" % (statistics.median(ea), statistics.median(eb)),
          ">3s", sum(e > 3 for e in ea), sum(e > 3 for e in eb), ">4s", sum(e > 4 for e in ea), sum(e > 4 for e in eb))
    # season audio single-source decisions by support
    tab = collections.defaultdict(collections.Counter)
    for f, row in ev.items():
        if row["audio"] and not row["introdb"] and a[f][1] and abs(a[f][1][1] - row["audio"][1]) < 0.01:
            tab[row["audio"][2]][a[f][0]] += 1
    print("  season audio deciding alone, by support:", {k: dict(v) for k, v in sorted(tab.items())})
