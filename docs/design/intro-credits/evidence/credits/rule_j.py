"""Reproduce spec §5.4 credits numbers (rule J). Run from the evidence folder: python credits/rule_j.py"""
src = open("credits/eval_rules3.py").read().split("items = load(sys.argv[1])")[0]
exec(src)
RULE_J = dict(dense=3, min_boxes=1, dark=30, gap=24, run=15, pick="last", anchor=True, bridge_dark=True)
items = load("credits/f3.jsonl")
for mode, label in (("key", "keyframes of the tail"), ("prev6", "preview frames 6 s"), ("prev10", "preview frames 10 s")):
    print(f"{label:24}", score(items, RULE_J, mode, True))
