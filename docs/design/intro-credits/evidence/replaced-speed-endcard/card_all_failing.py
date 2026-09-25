"""The end-card rule on every cached end-picture check that fails today (t3_shares.pkl share < 0.75: the #310/#312
checks of the four sets, the clusters the guards' exploration reached, idents and music beds included). For each check
the card rule would pass: is its cluster the file's intro (judged against the set's truth)? NVIDIA decode, read-only.
"""

import collections
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ep_variants_sets as V  # noqa: E402

truth = {}
for mode in ("lists", "scale_clean", "accused", "chap_clean"):
    for _season, _files, _fps, tr in V.eval_groups(mode):
        truth.update(tr)
failing = [k for k, share in V.base.items() if share is not None and share < V.EP.MIN_SHARE and os.path.exists(k[0])]
tally = collections.Counter()
for n, key in enumerate(sorted(failing)):
    target, partner, start, end, off = key
    if not os.path.exists(partner):
        continue
    card = V.variant_share(key, "card")
    tally["checks"] += 1
    if card == 1.0:
        tr = truth.get(target, "no truth")
        verdict = "no truth" if tr == "no truth" else ("no intro" if tr is None else V.judge((start, end), tr))
        tally[f"card passes: {verdict}"] += 1
        print(
            f"PASSES {os.path.basename(target)[:60]:60} {start:6.1f}-{end:6.1f} base {V.base[key]:.2f} truth {tr} -> {verdict}"
        )
    if n % 25 == 0:
        pickle.dump(V.frames, open(V.FRAMES, "wb"))
        print(f"   {n}/{len(failing)} {dict(tally)}", flush=True)
pickle.dump(V.frames, open(V.FRAMES, "wb"))
print(dict(tally))
