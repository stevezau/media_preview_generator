"""Every end-picture share of a list, decoded once, under the check's variants: check 3 (a flat frame never matches
one that isn't), check 4 (compared by correlation) and check 4 with the end card needing a picture on both sides
(``--card-both``). Usage: CODE=tree share_variants.py <keys.json> <out.json>, keys ``[target, partner, start_ms,
end_ms, offset_ms]``; decodes on storage's GPU, nice 19. Resumes from ``out.json``."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import CODE  # noqa: E402

sys.path.insert(0, CODE)
import numpy as np  # noqa: E402

from media_preview_generator.markers.audio import end_picture as E  # noqa: E402


def alike_v3(x, y):
    sx, sy = float(x.std()), float(y.std())
    if sx < E.FLAT_STD and sy < E.FLAT_STD:
        return abs(float(x.mean()) - float(y.mean())) < E.FLAT_MEAN_DIFF
    if sx < E.FLAT_STD or sy < E.FLAT_STD:
        return False
    zx, zy = (x - x.mean()).ravel(), (y - y.mean()).ravel()
    return float(zx @ zy / (np.linalg.norm(zx) * np.linalg.norm(zy))) > E.MIN_CORRELATION


def alike_v4(x, y):
    sx, sy = float(x.std()), float(y.std())
    if sx < E.FLAT_STD and sy < E.FLAT_STD:
        return abs(float(x.mean()) - float(y.mean())) < E.FLAT_MEAN_DIFF
    zx, zy = (x - x.mean()).ravel(), (y - y.mean()).ravel()
    norms = float(np.linalg.norm(zx)) * float(np.linalg.norm(zy))
    return norms > 0.0 and float(zx @ zy) / norms > E.MIN_CORRELATION


def share(times, own, own_shift, theirs, their_shift, alike, card_both):
    verdicts, cards = [], []
    for t in times:
        x, y = E._nearest(own, t + own_shift), E._nearest(theirs, t + their_shift)
        if x is None or y is None:
            verdicts.append(None)
            cards.append(False)
            continue
        verdicts.append(alike(x, y))
        cards.append(verdicts[-1] and E._card_picture(x) and (not card_both or E._card_picture(y)))
    known = [v for v in verdicts if v is not None]
    if not known:
        return None
    if len(cards) >= E.END_CARD_INSTANTS and all(cards[-E.END_CARD_INSTANTS :]):
        return 1.0
    return sum(known) / len(known)


keys = json.load(open(sys.argv[1]))
out_path = sys.argv[2]
out = json.load(open(out_path)) if os.path.exists(out_path) else {}
reader = E.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
for n, (target, partner, s_ms, e_ms, off_ms) in enumerate(keys, 1):
    key = json.dumps([target, partner, s_ms, e_ms, off_ms])
    if key in out:
        continue
    try:
        times = E.sample_times(s_ms / 1000, e_ms / 1000)
        a, b = reader._read_starts(target), reader._read_starts(partner)
        own_shift, their_shift = a.audio_offset_s, off_ms / 1000 + b.audio_offset_s
        own = reader._decoded(target, a, times, own_shift)
        theirs = reader._decoded(partner, b, times, their_shift)
        out[key] = {
            "v3": share(times, own, own_shift, theirs, their_shift, alike_v3, False),
            "v4": share(times, own, own_shift, theirs, their_shift, alike_v4, False),
            "v4_card_both": share(times, own, own_shift, theirs, their_shift, alike_v4, True),
        }
    except Exception as exc:  # noqa: BLE001 - scratch runner: record and go on
        out[key] = {"error": f"{type(exc).__name__}: {exc}"}
    if n % 25 == 0:
        print(n, len(keys), flush=True)
        json.dump(out, open(out_path, "w"))
json.dump(out, open(out_path, "w"))
print("done", len(out))
