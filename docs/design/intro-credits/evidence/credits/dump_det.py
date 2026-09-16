"""Print truth vs detection (prev6 refined) per item for given params; used to pick frame checks."""
import json, sys
src = open("credits/eval_rules3.py").read().split("items = load(sys.argv[1])")[0]
exec(src)
p = json.loads(sys.argv[2])
for it in load(sys.argv[1]):
    rows, span = mode_rows(it, "prev6")
    d = detect(rows, it["fine"], p, span)
    e = None if d is None else round(d - it["truth"])
    print(json.dumps({"kind": it["kind"], "err": e, "truth": round(it["truth"], 1), "det": d, "file": it["file"]}))
