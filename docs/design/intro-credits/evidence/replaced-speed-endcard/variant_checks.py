"""Every end-picture check the four sets' season steps reached that fails today (the decoded ones in
variant_frames.pkl): the base share against the card and near variants' verdicts per partner."""

import collections
import os
import pickle
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["EP_GPU"] = ""
import ep_variants_sets as V  # noqa: E402

frames = pickle.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "variant_frames.pkl"), "rb"))
V.frames = frames
tally = collections.Counter()
for key in sorted(frames):
    target, partner, start, end, off = key
    base = V.base.get(key)
    card, near = V.variant_share(key, "card"), V.variant_share(key, "near")
    tally["checks"] += 1
    tally["card passes"] += card == 1.0
    tally["near raises"] += (near or 0) > (base or 0)
    print(f"{os.path.basename(target)[:48]:48} {start:6.1f}-{end:6.1f} base {base} card {card} near {near}")
print(dict(tally))
