"""Per minority-speed file: how many of the group's majority-speed files its own-speed and its retimed fingerprint
match (any run; runs not mostly silence), and the matched seconds. Bones S05-S08 (sped-up WEB files, the stretch is
right) and RPDR UK S08 from prod fingerprints (the stretch is wrong). Read-only; fingerprints from the harness cache.
"""

import glob
import sqlite3
import sys
from pathlib import Path

import numpy as np

W = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, W)
from media_preview_generator.markers.audio import season as S  # noqa: E402
from media_preview_generator.markers.speed import playback_speed  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402

SCR = "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/221c1481-5d66-4e86-8c40-a12113e50030/scratchpad"


def score(x_points, refs):
    anyrun = nonsilent = 0
    secs = 0.0
    for r in refs:
        runs = S.season_pair_runs(x_points, r)
        if runs:
            anyrun += 1
        loud = [
            run
            for run in runs
            if S.silence_share(x_points[round(run.a_start_s / 0.1238) : round(run.a_end_s / 0.1238) + 1])
            <= S.MAX_INTRO_SILENCE
        ]
        if loud:
            nonsilent += 1
        secs += sum(run.a_end_s - run.a_start_s for run in loud)
    return anyrun, nonsilent, round(secs)


def report(name, pts, speeds, retimed_of):
    clock = S.season_clock(speeds)
    refs = [p for p in pts if speeds[p] == clock.speed]
    print(
        f"== {name}: group speed {clock.speed}, {len(clock.factors)} of {len(pts)} at the other speed, {len(refs)} refs"
    )
    for path, factor in sorted(clock.factors.items()):
        own = score(pts[path], [pts[r] for r in refs])
        ret = score(retimed_of(path, factor), [pts[r] for r in refs])
        print(
            f"   {Path(path).name[:48]:48} own(any,loud,s)={own} retimed={ret} -> {'retime' if ret[1] > own[1] else 'own'}"
        )


def main():
    cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
    for season in ("05", "06", "07", "08"):
        files = sorted(glob.glob(f"/data/TV Shows/Bones (2005) {{tvdb-75682}}/Season {season}/*.mkv"))
        pts = {f: cache.points(f) for f in files}
        report(f"Bones S{season}", pts, {f: cache.speed(f) for f in files}, cache.retimed)

    con = sqlite3.connect(f"file:{SCR}/audit0925/post.db?mode=ro", uri=True)
    rows = con.execute(
        "select f.id, f.canonical_path, p.window, p.points, r.frame_rate from files f join fingerprints p on p.file_id=f.id "
        "left join frame_rates r on r.file_id=f.id where f.canonical_path like '%RuPauls Drag Race UK%Season 08%'"
    ).fetchall()
    pts, ret, speeds = {}, {}, {}
    for fid, path, window, blob, rate in rows:
        key = f"{fid}:{Path(path).name}"
        arr = np.frombuffer(blob, dtype="<u4").copy()
        if window == "intro":
            pts[key], speeds[key] = arr, playback_speed(rate)
        else:
            ret[(key, window)] = arr
    print("RPDR windows:", sorted({w for _, w in ret}), {k[:40]: v for k, v in speeds.items()})
    report("RPDR UK S08 (prod)", pts, speeds, lambda p, f: ret[(p, f"intro@{f:.6f}")])


if __name__ == "__main__":
    main()
