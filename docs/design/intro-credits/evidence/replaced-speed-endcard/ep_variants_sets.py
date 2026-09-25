"""End-picture guard variants for Tomb Raider King S01E12 (its OP's last 1.5 s are re-cut and ~0.4 s out of sync
with the partners', the final card is the same), scored with THIS WORKTREE's season step on the four intro sets
(lab118 = lists, held-out = scale_clean, accused, library chapters = chap_clean).

Variants (each only ever turns a failing check into a pass, so only failing baseline checks are decoded again):
  base  : the shipped check (median over the partners of the share of matching instants >= 0.75)
  near  : an instant matches when the partner's picture is within +-0.5 s of the aligned time (a sync difference
          or a shot re-cut by up to half a second)
  card  : as base, or the last 1.5 s (3 instants) all match on pictures whose inside (a 4-row, 7-column
          border cropped) isn't flat (the same end card; black with a corner logo is no card)

Baseline shares: the #310/#312 cache (t3_shares.pkl, NVIDIA decode). Variant frames decoded on the NVIDIA GPU.
Read-only on /data. Writes laneL/variant_frames.pkl and laneL/variant_details.pkl.
"""

import collections
import os
import pickle
import sys
import time

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
OLD = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick"
SCR = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKTREE)
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.markers.audio import end_picture as EP  # noqa: E402

assert S.__file__.startswith(WORKTREE), S.__file__
sys.path.insert(1, OLD)
from evaluate import eval_groups  # noqa: E402
from lab import Season, judge  # noqa: E402

NEAR_S = 0.5
CARD_INSTANTS = 3
GPU = os.environ.get("EP_GPU", "NVIDIA")
reader = EP.Reader(ffmpeg="/usr/bin/ffmpeg", gpu=GPU or None, gpu_device_path="cuda:0" if GPU else None)
base = pickle.load(open(os.path.join(SCR, "t3_shares.pkl"), "rb"))
FRAMES = os.path.join(HERE, "variant_frames.pkl")
frames = pickle.load(open(FRAMES, "rb")) if os.path.exists(FRAMES) else {}
counts = collections.Counter()


def decoded(key):
    """(times, target frames, own shift, partner frames, partner shift) for one check, the partner decoded 1 s wider."""
    if key not in frames:
        target, partner, start, end, off = key
        times = EP.sample_times(start, end)
        try:
            own_starts, p_starts = reader._read_starts(target), reader._read_starts(partner)
            if not times or not own_starts.has_video or not p_starts.has_video:
                frames[key] = None
            else:
                own_shift, p_shift = own_starts.audio_offset_s, off + p_starts.audio_offset_s
                own = reader._decoded(target, own_starts, times, own_shift)
                wide = [times[0] - 1.0, *times, times[-1] + 1.0]
                theirs = reader._decoded(partner, p_starts, wide, p_shift)
                frames[key] = (times, own, own_shift, theirs, p_shift)
        except EP.ReadFailedError:
            frames[key] = None
        counts["decodes"] += 1
    return frames[key]


def verdicts(data, variant):
    times, own, own_shift, theirs, p_shift = data
    out = []
    for t in times:
        x = EP._nearest(own, t + own_shift)
        if x is None:
            out.append(None)
            continue
        if variant == "near":
            ys = [y for pt, y in theirs if abs(pt - (t + p_shift)) <= NEAR_S + 0.05]
        else:
            y = EP._nearest(theirs, t + p_shift)
            ys = [] if y is None else [y]
        if not ys:
            out.append(None)
            continue
        out.append((any(EP.frames_alike(x, y) for y in ys), float(x[4:-4, 7:-7].std())))  # the card's inside
    return out


def variant_share(key, variant):
    data = decoded(key)
    if data is None:
        return None
    got = verdicts(data, variant)
    known = [v for v in got if v is not None]
    if variant == "near":
        return sum(v[0] for v in known) / len(known) if known else None
    # card: the last CARD_INSTANTS instants all match on pictures that aren't flat
    last = got[-CARD_INSTANTS:]
    return 1.0 if len(last) == CARD_INSTANTS and all(v and v[0] and v[1] >= EP.FLAT_STD for v in last) else 0.0


def oracle(target, variant):
    def check(c):
        members = [h for h in c.members if os.path.exists(h.partner)]
        keys = [(target, h.partner, round(c.segment.start_s, 3), round(c.segment.end_s, 3),
                 round(h.partner_start_s - h.start_s, 3)) for h in EP.partners(members)]  # fmt: skip
        got = [base.get(k, "missing") for k in keys]
        if "missing" in got:
            counts["missing_base"] += 1
            got = [None if g == "missing" else g for g in got]
        if variant == "base" or EP.passes(got):
            return EP.passes(got)
        if variant == "near":
            out = [max(filter(lambda s: s is not None, (g, variant_share(k, "near"))), default=None)
                   for k, g in zip(keys, got, strict=True)]  # fmt: skip
            return EP.passes(out)
        out = [1.0 if variant_share(k, "card") == 1.0 else g for k, g in zip(keys, got, strict=True)]
        return EP.passes(out)

    return check


VARIANTS = ("base", "near", "card")


def run(mode):
    tallies = {v: collections.Counter() for v in VARIANTS}
    details = {}
    for _season, files, fps, truth in eval_groups(mode):
        sn = Season(files, fps)
        for f, tr in truth.items():
            row = {}
            for variant in VARIANTS:
                seg = (
                    S.season_intro(f, sn.files, fps, sn.runs, end_picture_passes=oracle(f, variant))
                    if len(sn.files) >= 2 and f in sn.files
                    else None
                )
                v = ("wrong" if seg else "none-ok") if tr is None else judge(seg[:2] if seg else None, tr)
                tallies[variant][v] += 1
                row[variant] = (tuple(round(x, 2) for x in seg) if seg else None, v)
            details[f] = (tr, row)
    return tallies, details


if __name__ == "__main__":
    out = {}
    for mode in sys.argv[1:]:
        t0 = time.time()
        tallies, details = run(mode)
        out[mode] = details
        pickle.dump(frames, open(FRAMES, "wb"))
        for variant, t in tallies.items():
            print(f"{mode:12} {variant:5} useful {t['useful']:3} wrong {t['wrong']:3} missed {t['missed']:3} "
                  f"none-ok {t['none-ok']}")  # fmt: skip
        for f, (tr, row) in sorted(details.items()):
            if len({row[v] for v in VARIANTS}) > 1:
                print(f"   {os.path.basename(f)[:64]:64} truth {tr} " + " | ".join(f"{v} {row[v]}" for v in VARIANTS))
        print(f"   ({time.time() - t0:.0f}s, {dict(counts)})", flush=True)
    pickle.dump(out, open(os.path.join(HERE, "variant_details.pkl"), "wb"))
