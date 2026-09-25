"""The end-picture check with a picture sync search (Tomb Raider King E12): the partner's pictures may sit up to
SYNC_S earlier or later than the audio alignment says (one shift for the whole comparison, the best of
{0, -SYNC_S, +SYNC_S}). Scored with THIS WORKTREE's season step on the four intro sets (lab118 = lists, held-out =
scale_clean, accused, library chapters = chap_clean), baseline shares from the #310/#312 cache (t3_shares.pkl, NVIDIA
decode), variant shares decoded on the NVIDIA GPU only where the baseline fails.

Read-only on /data. Writes only laneL/sync_shares.pkl and laneL/sync_details.pkl.
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

SYNC_S = float(os.environ.get("SYNC_S", "0.5"))
reader = EP.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
base = pickle.load(open(os.path.join(SCR, "t3_shares.pkl"), "rb"))
CACHE = os.path.join(HERE, f"sync_shares_{SYNC_S}.pkl")
synced = pickle.load(open(CACHE, "rb")) if os.path.exists(CACHE) else {}
counts = collections.Counter()


def sync_share(target, partner, start, end, off):
    times = EP.sample_times(start, end)
    own_starts, p_starts = reader._read_starts(target), reader._read_starts(partner)
    if not times or not own_starts.has_video or not p_starts.has_video:
        return None
    own_shift = own_starts.audio_offset_s
    p_shift = off + p_starts.audio_offset_s
    own = reader._decoded(target, own_starts, times, own_shift)
    wide = [times[0] - SYNC_S, *times, times[-1] + SYNC_S]
    theirs = reader._decoded(partner, p_starts, wide, p_shift)
    best = None
    for d in (0.0, -SYNC_S, SYNC_S):
        s = EP.share_alike(times, own, own_shift, theirs, p_shift + d)
        if s is not None and (best is None or s > best):
            best = s
    return best


def base_share(key):
    if key not in base:
        target, partner, start, end, off = key
        try:
            base[key] = reader.share(target, partner, start, end, off)
        except EP.ReadFailedError:
            base[key] = None
        counts["base_decodes"] += 1
    return base[key]


def oracle(target, variant):
    def check(c):
        members = [h for h in c.members if os.path.exists(h.partner)]
        keys = []
        for hit in EP.partners(members):
            off = hit.partner_start_s - hit.start_s
            keys.append((target, hit.partner, round(c.segment.start_s, 3), round(c.segment.end_s, 3), round(off, 3)))
        got = [base_share(k) for k in keys]
        if variant == "before" or EP.passes(got):
            return EP.passes(got)
        out = []
        for k, share in zip(keys, got, strict=True):
            if share is not None and share >= 1.0:
                out.append(share)
                continue
            if k not in synced:
                try:
                    synced[k] = sync_share(*k)
                except EP.ReadFailedError:
                    synced[k] = None
                counts["sync_decodes"] += 1
            best = synced[k]
            out.append(best if share is None or (best is not None and best > share) else share)
        verdict = EP.passes(out)
        if verdict:
            counts["flipped_to_pass"] += 1
        return verdict

    return check


def run(mode):
    tallies = {v: collections.Counter() for v in ("before", "after")}
    details = {}
    for _season, files, fps, truth in eval_groups(mode):
        sn = Season(files, fps)
        for f, tr in truth.items():
            row = {}
            for variant in ("before", "after"):
                seg = (
                    S.season_intro(f, sn.files, fps, sn.runs, end_picture_passes=oracle(f, variant))
                    if len(sn.files) >= 2 and f in sn.files
                    else None
                )
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
        pickle.dump(synced, open(CACHE, "wb"))
        for variant, t in tallies.items():
            print(
                f"{mode:12} {variant:7} useful {t['useful']:3} wrong {t['wrong']:3} missed {t['missed']:3} "
                f"none-ok {t['none-ok']}"
            )
        for f, (tr, row) in sorted(details.items()):
            if row["before"] != row["after"]:
                print(f"   {os.path.basename(f)[:70]:70} truth {tr} {row['before']} -> {row['after']}")
        print(f"   ({time.time() - t0:.0f}s, {dict(counts)})", flush=True)
    pickle.dump(out, open(os.path.join(HERE, f"sync_details_{SYNC_S}.pkl"), "wb"))
