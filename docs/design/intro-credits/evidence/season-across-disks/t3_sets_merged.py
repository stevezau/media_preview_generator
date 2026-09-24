"""Task 3 fix 3 on the four intro sets: most of their seasons are split across the library's disks, so the season
group changes. THIS WORKTREE's season step (fix 1 on, speed clock on) on each truth file, with the group from its own
folder vs from the same season folder on every disk of the TV library (season.season_videos).

Every member's fingerprint comes from the eval cache (~/.cache/markers_eval; missing ones are computed there: CPU
ffmpeg, read-only on /data), so both groupings use the same fingerprints. End-picture shares: t3_shares.pkl, then
decoded on the NVIDIA GPU. Writes only t3_merged_* files here.
"""

import collections
import os
import pickle
import sys
import time
from pathlib import Path

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
OLD = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad/intro-pick"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKTREE)
import media_preview_generator.markers.audio.season as S  # noqa: E402
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.servers.base import Library, ServerConfig, ServerType  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

assert S.__file__.startswith(WORKTREE)
sys.path.insert(1, OLD)
from evaluate import eval_groups  # noqa: E402
from lab import judge  # noqa: E402

ROOTS = ["/data_16tb", "/data_16tb2", "/data_16tb3", "/data_28tb"]
LIBRARY = ServerConfig(
    id="plex", type=ServerType.PLEX, name="Plex", enabled=True, url="http://plex", auth={},
    libraries=[Library("2", "TV Shows", tuple(f"{root}/TV Shows" for root in ROOTS))],
)  # fmt: skip
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
reader = end_picture.Reader(ffmpeg="/usr/bin/ffmpeg", gpu="NVIDIA", gpu_device_path="cuda:0")
SHARES = os.path.join(HERE, "t3_shares.pkl")
shares = pickle.load(open(SHARES, "rb"))
unreadable: set[str] = set()
runs_cache = {}


def points(path):
    if path in unreadable:
        return None
    try:
        found = cache.points(path)
    except Exception as exc:  # noqa: BLE001 - a member ffmpeg can't read is left out, as the app does
        print("   unreadable", os.path.basename(path)[:60], type(exc).__name__, flush=True)
        unreadable.add(path)
        return None
    return found if len(found) else None


def passes(target, candidate):
    got = []
    for hit in end_picture.partners([h for h in candidate.members if os.path.exists(h.partner)]):
        off = hit.partner_start_s - hit.start_s
        seg = candidate.segment
        key = (target, hit.partner, round(seg.start_s, 3), round(seg.end_s, 3), round(off, 3))
        if key not in shares:
            try:
                shares[key] = reader.share(target, hit.partner, seg.start_s, seg.end_s, off)
            except end_picture.ReadFailedError:
                shares[key] = None
        got.append(shares[key])
    return end_picture.passes(got)


def answer(target, episodes):
    fps = {f: p for f in episodes if (p := points(f)) is not None}
    files = sorted(fps)
    if target not in files or len(files) < 2:
        return None, len(files)
    clock = S.season_clock({f: cache.speed(f) for f in files})
    for f, factor in clock.factors.items():
        fps[f] = cache.retimed(f, factor)

    def runs_between(a, b):
        key = (a, b, clock.factors.get(a), clock.factors.get(b))
        if key not in runs_cache:
            runs_cache[key] = S.season_pair_runs(fps[a], fps[b])
        return runs_cache[key]

    picked = S.guarded_pick(
        target, files, fps, runs_between,
        end_picture_passes=lambda c: passes(target, S.in_own_times(c, target, clock.factors)),
    )  # fmt: skip
    if picked is not None and S._mostly_silence(fps[target], picked):
        picked = None
    return (None if picked is None else S.in_own_time(picked, clock.factors.get(target))), len(files)


def verdict(seg, truth):
    if truth is None:
        return "wrong" if seg else "none-ok"
    return judge((seg.start_s, seg.end_s) if seg else None, truth)


if __name__ == "__main__":
    details = {}
    for mode in sys.argv[1:]:
        t0 = time.time()
        tallies = {"own folder": collections.Counter(), "every disk": collections.Counter()}
        for _season, _files, _fps, truth in eval_groups(mode):
            for f, tr in truth.items():
                if not os.path.exists(f):
                    continue
                own_eps = S.season_group(f).episodes
                merged_eps = S.season_group(f, S.season_videos(f, [LIBRARY])).episodes
                own, n_own = answer(f, own_eps)
                merged, n_merged = (own, n_own) if merged_eps == own_eps else answer(f, merged_eps)
                v_own, v_merged = verdict(own, tr), verdict(merged, tr)
                tallies["own folder"][v_own] += 1
                tallies["every disk"][v_merged] += 1
                details[(mode, f)] = (tr, own, n_own, merged, n_merged)
                if v_own != v_merged:
                    show = [
                        None if s is None else (round(s.start_s, 1), round(s.end_s, 1), s.support)
                        for s in (own, merged)
                    ]
                    print(f"   {mode:11} {os.path.basename(f)[:60]:60} truth {tr} {v_own} {show[0]} ({n_own}) -> "
                          f"{v_merged} {show[1]} ({n_merged})", flush=True)  # fmt: skip
        for name, t in tallies.items():
            print(f"{mode:12} {name:10} useful {t['useful']:3} wrong {t['wrong']:3} missed {t['missed']:3} "
                  f"none-ok {t['none-ok']}", flush=True)  # fmt: skip
        print(f"   ({time.time() - t0:.0f}s)", flush=True)
        pickle.dump(shares, open(SHARES, "wb"))
    pickle.dump(details, open(os.path.join(HERE, "t3_merged_details.pkl"), "wb"))
