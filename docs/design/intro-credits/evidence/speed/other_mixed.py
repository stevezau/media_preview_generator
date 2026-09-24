"""The two other mixed-speed seasons on the library (30 for 30 S04, Sort Of S03): the season step before and after,
per episode, with the frame rates. Read only on /data (nice 19, fingerprints to the harness cache)."""

import glob
import sys
from pathlib import Path

sys.path.insert(0, "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3")
from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio.season import season_group  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402
from tools.markers_eval.data import EvalEpisode  # noqa: E402
from tools.markers_eval.intros import DecodedEndPictures, ReproductionReport, SeasonStep  # noqa: E402

FFMPEG = "/usr/bin/ffmpeg"
cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg=FFMPEG, ffprobe="/usr/bin/ffprobe")
end_pictures = DecodedEndPictures(end_picture.Reader(ffmpeg=FFMPEG, gpu="NVIDIA", gpu_device_path="cuda:0"))
for folder in (
    "/data_16tb2/TV Shows/30 for 30 (2009) {tvdb-128051}/Season 04",
    "/data_16tb3/TV Shows/Sort Of (2021) {tvdb-407554}/Season 03",
):
    first = sorted(glob.glob(f"{glob.escape(folder)}/*.mkv") + glob.glob(f"{glob.escape(folder)}/*.mp4"))[0]
    files = list(season_group(first).episodes)
    points = {f: cache.points(f) for f in files}
    before = SeasonStep(folder, points, ReproductionReport(), end_pictures)
    after = SeasonStep(folder, points, ReproductionReport(), end_pictures, speed=cache.speed, retimed=cache.retimed)
    print(folder, "retimed", {Path(f).name[:40]: round(x, 4) for f, x in after.clock.factors.items()})
    for f in files:
        episode = EvalEpisode(folder, f, None, None, None)
        b, a = before.answer(episode), after.answer(episode)
        fmt = lambda s: "-" if s is None else f"{s[0]:7.1f}-{s[1]:7.1f} ({s[2]})"  # noqa: E731
        print(f"  {Path(f).name[:55]:55s} {cache.speed(f)} before {fmt(b)} after {fmt(a)}", flush=True)
