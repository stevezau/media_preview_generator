import json, os, sys, glob
vs = sys.argv[2].split(",")
for line in open(sys.argv[1]):
    tag = line.split("\t")[0]
    cells = []
    for v in vs:
        f = f"{tag}_{v}.json"
        if os.path.exists(f):
            d = json.load(open(f)); cells.append(f"{v}={d['start']}/{d['end']}")
        else:
            cells.append(f"{v}=-")
    print(f"{tag:10s} " + "  ".join(cells))
