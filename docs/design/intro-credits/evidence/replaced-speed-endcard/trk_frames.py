"""Tomb Raider King E12 end picture: per-instant verdicts against E03 and E05 (CPU decode, the app's reader), and a
wider per-instant scan of the last 10 s of the cluster. Read-only on /data."""

import sys

import numpy as np

W = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a247053d59620fedb"
sys.path.insert(0, W)
from media_preview_generator.markers.audio import end_picture as EP  # noqa: E402
from media_preview_generator.markers.probe import ffprobe_path_for, stream_starts  # noqa: E402

D = "/data_16tb/TV Shows/Tomb Raider King (2026) {tvdb-452039}/Season 01/"
E12 = D + "Tomb Raider King (2026) - S01E12 - TBA [WEBRip-1080p][AAC 2.0][x265].mkv"
E03 = D + "Tomb Raider King (2026) - S01E03 - One Suited for Domination [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
E05 = (
    D + "Tomb Raider King (2026) - S01E05 - The Owner of the Tree of Life [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
)
E02 = D + "Tomb Raider King (2026) - S01E02 - Those Who Seek to Own Relics [WEBRip-2160p][AAC 2.0][HEVC]-Feibanyama.mkv"
FF = "/usr/bin/ffmpeg"
start_s, end_s = 0.3096, 89.2265
starts = {p: stream_starts(p, ffprobe=ffprobe_path_for(FF), timeout_s=30) for p in (E12, E03, E05, E02)}
for p, s in starts.items():
    print(p[-60:], "audio_offset", round(s.audio_offset_s, 3), "container", s.container_s)


def frames(path, times, shift):
    lo, length = EP.window(times, shift)
    return EP.decode_frames(
        path, lo, length, ffmpeg=FF, gpu=None, gpu_device_path=None, container_start_s=starts[path].container_s
    )


def corr(x, y):
    zx, zy = (x - x.mean()).ravel(), (y - y.mean()).ravel()
    return float(zx @ zy / (np.linalg.norm(zx) * np.linalg.norm(zy) + 1e-9))


for name, partner, off in (("E03", E03, -0.372), ("E05", E05, -0.248), ("E02", E02, -0.37)):
    times = [float(t) for t in np.arange(end_s - 10.0, end_s, 0.5)]
    own_shift = starts[E12].audio_offset_s
    p_shift = off + starts[partner].audio_offset_s
    a, b = frames(E12, times, own_shift), frames(partner, times, p_shift)
    print(f"--- E12 vs {name} (offset {off:+.3f}); the app's instants are the last 6")
    for t in times:
        x, y = EP._nearest(a, t + own_shift), EP._nearest(b, t + p_shift)
        if x is None or y is None:
            print(f"  t={t:6.2f} missing frame")
            continue
        print(
            f"  t={t:6.2f} alike={EP.frames_alike(x, y)!s:5} corr={corr(x, y):+.2f} std {x.std():5.1f} {y.std():5.1f} "
            f"mean {x.mean():5.1f} {y.mean():5.1f}"
        )
