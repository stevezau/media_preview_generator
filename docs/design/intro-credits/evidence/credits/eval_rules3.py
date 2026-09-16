"""Credits-start rule evaluation on GPU features (features3.py): keyframes + 1 fps fine window.

Modes (how the marker job gets its frames):
  key      : every keyframe in the tail (job samples the file itself)
  prev6    : preview frames at 6 s (owner's setting) — grid time i*6, content = latest keyframe <= grid time
  prev10   : preview frames at 10 s (app default)
Each mode coarse, and refined with a short full decode (1 fps) just before the coarse answer.

Rule "credit state": boxes >= dense, or (boxes >= min_boxes and dark screen). Runs joined over gaps; start of the
chosen run, stepped back over the fade to black. Tune on even items, report held-out odd items.
"""

import bisect
import itertools
import json
import sys

FADE_LUMA = 12


def load(path):
    # Chapter truth corrected by frame checks (credits/fc_*.jpg); each entry records why.
    try:
        fixes = json.load(open("credits/adjudicated.json"))
    except FileNotFoundError:
        fixes = {}
    items = [json.loads(line) for line in open(path)]
    for it in items:
        name = it["file"].split("/")[-1]
        if name in fixes:
            it["chapter_truth"], it["truth"] = it["truth"], fixes[name]["truth"]
    return items


def preview_rows(it, interval):
    keys = it["key"]
    pts = [k[0] for k in keys]
    rows, t = [], it["window_start"]
    while t <= it["duration"]:
        j = bisect.bisect_right(pts, t + 1e-6) - 1
        if j >= 0:
            rows.append([round(t, 3)] + keys[j][1:])
        t += interval
    return rows


def is_credit(r, p):
    # Bright frames need more text than dark ones: static signage in a lit scene gave 3+ boxes for minutes
    # (Checkin It Twice, -864 s), while real cards on black often show only 1-2 boxes at 320 px.
    if r[3] < p["dark"]:
        return r[1] >= p["min_boxes"]
    return r[1] >= p["dense"]


def runs(rows, p):
    out = []
    last_bright = -1.0  # time of the latest non-credit frame that is NOT a dark screen
    for i, r in enumerate(rows):
        if is_credit(r, p):
            # bridge_dark: black/dark empty screens between credit blocks don't break the run (Summit of the Gods:
            # 24 s of dark keyframes with no readable text split the roll in two).
            gap_ok = out and r[0] - rows[out[-1][1]][0] <= p["gap"]
            if p.get("bridge_dark") and out and last_bright < rows[out[-1][1]][0]:
                gap_ok = True
            if gap_ok:
                out[-1][1] = i
            else:
                out.append([i, i])
        elif r[3] >= p["dark"]:
            last_bright = r[0]
    return [x for x in out if rows[x[1]][0] - rows[x[0]][0] >= p["run"]]


def fade_back(rows, i, floor_t):
    while i > 0 and rows[i - 1][0] >= floor_t and rows[i - 1][3] < FADE_LUMA and rows[i][0] - rows[i - 1][0] <= 4:
        i -= 1
    return rows[i][0]


def detect(rows, fine, p, refine_span):
    rs = runs(rows, p)
    if not rs:
        return None
    run = max(rs, key=lambda x: rows[x[1]][0] - rows[x[0]][0]) if p["pick"] == "longest" else rs[-1]
    if p.get("anchor"):
        # The big join gap lets one story-scene text frame glue itself onto the credits (Undisputed: a hoodie +
        # bin numbers 24 s before the roll). Start only where two credit samples sit next to each other.
        spacing = sorted(rows[i + 1][0] - rows[i][0] for i in range(len(rows) - 1))[len(rows) // 2]
        a, b = run
        while a < b:
            nxt = next(k for k in range(a + 1, b + 1) if is_credit(rows[k], p))
            if rows[nxt][0] - rows[a][0] <= 1.5 * spacing:
                break
            a = nxt
        run = [a, b]
    t = rows[run[0]][0]
    if refine_span is None:
        return fade_back(rows, run[0], -1)
    lo = t - refine_span
    window = [j for j, r in enumerate(fine) if lo <= r[0] <= t + 1]
    if not window:  # fine decode doesn't cover it: coarse answer is far from truth anyway
        return fade_back(rows, run[0], -1)
    # Walk back from the coarse answer through contiguous credit frames (gaps <= 2 s between cards). Taking the
    # first credit-like frame anywhere in the window fired on story-scene text (Undisputed, -32 s).
    credit_js = [j for j in window if is_credit(fine[j], p)]
    if not credit_js:
        return t
    j = credit_js[-1]
    while True:
        prev = [k for k in credit_js if k < j and fine[j][0] - fine[k][0] <= 2.5]
        if not prev:
            break
        j = prev[0]
    return fade_back(fine, j, lo)


def mode_rows(it, mode):
    if mode == "key":
        return it["key"], 10.0  # refine span covers the longest keyframe gap seen (10 s)
    iv = int(mode[4:])
    cache = it.setdefault("_cache", {})
    if mode not in cache:
        cache[mode] = preview_rows(it, iv)
    return cache[mode], iv + 10.0


def score(items, p, mode, refine, show=False):
    s = {"n": len(items), "ok5": 0, "ok10": 0, "ok30": 0, "early": 0, "late": 0, "none": 0}
    for it in items:
        rows, span = mode_rows(it, mode)
        d = detect(rows, it["fine"], p, span if refine else None)
        if d is None:
            s["none"] += 1
            tag = "none"
        else:
            e = d - it["truth"]
            s["ok5"] += abs(e) <= 5
            s["ok10"] += abs(e) <= 10
            s["ok30"] += abs(e) <= 30
            if abs(e) > 30:
                s["early" if e < 0 else "late"] += 1
            tag = f"{e:+.0f}s"
        if show:
            print(f"      {tag:>7}  {it['file'].split('/')[-1][:70]}")
    return s


def objective(s):
    # Early = viewer skips into the story: weigh hardest. A miss just means no marker (or another source covers it).
    return s["ok10"] + 0.5 * (s["ok30"] - s["ok10"]) - 3 * s["early"] - 1.5 * s["late"]


GRID = [
    dict(dense=d, min_boxes=mb, dark=dk, gap=g, run=r, pick=pk, anchor=an, bridge_dark=bd)
    for d, mb, dk, g, r, pk, an, bd in itertools.product(
        [3, 5, 8, 12], [1, 2], [30, 40, 50], [8, 12, 24], [15, 30], ["longest", "last"], [False, True], [False, True]
    )
]

items = load(sys.argv[1])
show = "--show" in sys.argv
for kind in ("movie", "tv"):
    its = [i for i in items if i["kind"] == kind]
    if len(its) < 4:
        continue
    tune, val = its[0::2], its[1::2]
    ranked = sorted(GRID, key=lambda p: -objective(score(tune, p, "prev6", True)))
    best = ranked[0]
    print(f"== {kind}: n={len(its)} tuned on {len(tune)} (prev6 refined): {best}")
    for p in ranked[1:5]:
        print("   next-best", p, "held-out", score(val, p, "prev6", True))
    for mode in ("key", "prev6", "prev10"):
        print(f"   {mode:6} held-out coarse ", score(val, best, mode, False))
        print(f"   {mode:6} held-out refined", score(val, best, mode, True, show=show and mode == "prev6"))
    print("   prev6 refined, all items     ", score(its, best, "prev6", True))
