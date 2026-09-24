"""Task 3 fix 1 on the four intro sets (lab118 = lists, held-out = scale_clean, accused, library chapters = chap_clean),
scored with THIS WORKTREE's season step (season.season_intro) before (every stretch needs its dense core) and after
(the exemptions), on the #310 fingerprints and cached end-picture shares (new shares decoded on the NVIDIA GPU).

Read-only on /data. Writes only t3_shares.pkl / t3_sets_details.pkl next to this script.
"""

import collections
import os
import pickle
import sys
import time

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
OLD = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKTREE)
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.markers.audio import end_picture  # noqa: E402

assert S.__file__.startswith(WORKTREE), S.__file__
assert S.SEASON_AUDIO_VERSION == 7
sys.path.insert(1, OLD)
from evaluate import eval_groups  # noqa: E402
from lab import Season, judge  # noqa: E402

reader = end_picture.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
CACHE = os.path.join(HERE, "t3_shares.pkl")
SEED = os.path.join(HERE, "kq_shares.pkl")
shares = pickle.load(open(CACHE if os.path.exists(CACHE) else SEED, "rb"))
new_decodes = 0


def ep_oracle(target):
    def check(c):
        global new_decodes
        members = [h for h in c.members if os.path.exists(h.partner)]
        got = []
        for hit in end_picture.partners(members):
            off = hit.partner_start_s - hit.start_s
            key = (target, hit.partner, round(c.segment.start_s, 3), round(c.segment.end_s, 3), round(off, 3))
            if key not in shares:
                try:
                    shares[key] = reader.share(target, hit.partner, c.segment.start_s, c.segment.end_s, off)
                except end_picture.ReadFailedError:
                    shares[key] = None
                new_decodes += 1
            got.append(shares[key])
        return end_picture.passes(got)

    return check


REAL_NEEDS_CORE = S.needs_dense_core


def answer(variant, target, files, fps, runs):
    S.needs_dense_core = (lambda _segment: True) if variant == "before" else REAL_NEEDS_CORE
    return S.season_intro(target, files, fps, runs, end_picture_passes=ep_oracle(target))


def run(mode):
    tallies = {v: collections.Counter() for v in ("before", "after")}
    details = {}
    for _season, files, fps, truth in eval_groups(mode):
        sn = Season(files, fps)
        for f, tr in truth.items():
            row = {}
            for variant in ("before", "after"):
                seg = answer(variant, f, sn.files, fps, sn.runs) if len(sn.files) >= 2 and f in sn.files else None
                v = ("wrong" if seg else "none-ok") if tr is None else judge(seg[:2] if seg else None, tr)
                tallies[variant][v] += 1
                row[variant] = (tuple(seg) if seg else None, v)
            details[f] = (tr, row)
    return tallies, details


if __name__ == "__main__":
    out = {}
    for mode in sys.argv[1:]:
        t0 = time.time()
        tallies, details = run(mode)
        out[mode] = details
        pickle.dump(shares, open(CACHE, "wb"))
        for variant, t in tallies.items():
            print(
                f"{mode:12} {variant:7} useful {t['useful']:3} wrong {t['wrong']:3} missed {t['missed']:3} none-ok {t['none-ok']}"
            )
        for f, (tr, row) in sorted(details.items()):
            if row["before"][1] != row["after"][1] or row["before"][0] != row["after"][0]:
                print(f"   {os.path.basename(f)[:70]:70} truth {tr} {row['before']} -> {row['after']}")
        print(f"   ({time.time() - t0:.0f}s, new decodes so far {new_decodes})", flush=True)
    pickle.dump(out, open(os.path.join(HERE, "t3_sets_details.pkl"), "wb"))
