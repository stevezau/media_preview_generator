"""Score a few principled rule configs on all 80 items (movies + TV), prev6 refined + prev10 refined, with failures."""
import json, sys
src = open("credits/eval_rules3.py").read().split("items = load(sys.argv[1])")[0]
exec(src)
items = load("credits/f3.jsonl")
base = dict(min_boxes=1, dark=30, gap=24, run=15, pick="longest", anchor=False, bridge_dark=True)
cands = {
    "A dense3": dict(base, dense=3),
    "B dense8": dict(base, dense=8),
    "C dense8 anchor": dict(base, dense=8, anchor=True),
    "D dense8 anchor dark40": dict(base, dense=8, anchor=True, dark=40),
    "E dense8 anchor gap12": dict(base, dense=8, anchor=True, gap=12),
    "F dense8 last": dict(base, dense=8, pick="last"),
    "G dense8 anchor dark40 last": dict(base, dense=8, anchor=True, dark=40, pick="last"),
}
for name, p in cands.items():
    out = []
    for kind in ("movie", "tv"):
        its = [i for i in items if i["kind"] == kind]
        out.append(f"{kind}: {score(its, p, 'prev6', True)}")
    print(name, "|", " | ".join(out))
if len(sys.argv) > 1:
    p = cands[sys.argv[1]]
    for it in items:
        rows, span = mode_rows(it, "prev6")
        d = detect(rows, it["fine"], p, span)
        e = None if d is None else round(d - it["truth"])
        if e is None or abs(e) > 10:
            print(json.dumps({"kind": it["kind"], "err": e, "truth": it["truth"], "det": d, "file": it["file"]}))
