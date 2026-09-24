import collections
import csv

D = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad/stale_fresh"
csv.field_size_limit(10**9)
c = collections.Counter()
shown = 0
with open(f"{D}/parts.tsv", newline="") as f:
    for p in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
        for col, has in (("pv_intros", "has_intros_key"), ("pv_credits", "has_credits_key")):
            c[(col, has, p[has], bool(p[col]))] += 1
            if p[has] == "1" and not p[col] and shown < 2:
                shown += 1
                print(
                    col,
                    repr(p[has]),
                    repr(p[col])[:200],
                    "| other:",
                    repr(p["pv_intros" if col == "pv_credits" else "pv_credits"])[:200],
                )
for k, v in sorted(c.items()):
    print(k, v)
