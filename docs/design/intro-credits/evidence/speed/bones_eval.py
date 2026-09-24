"""Bones S05-S08: the app's season step and decide() before and after the speed fix, beside Plex's own intro markers
and IntroDB (raw and scaled). Media is only read (ffprobe/ffmpeg, nice 19); fingerprints go to the harness cache.

Writes answers.json: per episode, the season audio answer before/after, the decision before/after, Plex's first intro,
IntroDB's intro and the file's frame rate.
"""

import glob
import json
import os
import re
import sys
from pathlib import Path

WORKTREE = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-ab7b211236d423de3"
sys.path.insert(0, WORKTREE)

from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.decide import DecisionContext, DecisionStatus, decide  # noqa: E402
from media_preview_generator.markers.models import Candidate, MarkerType, Source  # noqa: E402
from media_preview_generator.markers.probe import probe_media  # noqa: E402
from tools.markers_eval.cache import FingerprintCache  # noqa: E402
from tools.markers_eval.data import EvalEpisode  # noqa: E402
from tools.markers_eval.intros import DecodedEndPictures, ReproductionReport, SeasonStep  # noqa: E402

HERE = Path(__file__).parent
ROOT = "/data/TV Shows/Bones (2005) {tvdb-75682}"
ORDER = (
    "chapters",
    "introdb",
    "skipdb",
    "season_audio",
    "season_audio_previous",
    "credits_text",
    "server_markers",
    "server_markers_imported",
)  # the app's default order
FFMPEG = "/usr/bin/ffmpeg"

cache = FingerprintCache(Path.home() / ".cache/markers_eval", ffmpeg=FFMPEG, ffprobe="/usr/bin/ffprobe")
end_pictures = DecodedEndPictures(end_picture.Reader(ffmpeg=FFMPEG, gpu="NVIDIA", gpu_device_path="cuda:0"))
introdb = json.load(open(HERE / "introdb.json"))


def plex_intros() -> dict[str, tuple[int, int]]:
    out = {}
    for line in open(HERE / "plex_bones.txt"):
        parts = line.rstrip("\n").split("|")
        name = os.path.basename(parts[0])
        if parts[1] == "intro" and name not in out:
            out[name] = (int(parts[2]), int(parts[3]))
    return out


def code(path: str) -> str:
    return re.search(r"S\d\dE\d\d", path).group(0)


def decision(candidates, duration_ms, frame_rate):
    ctx = DecisionContext(duration_ms, False, "medium", frozenset({MarkerType.INTRO}), ORDER, frame_rate=frame_rate)
    d = decide(candidates, ctx, {})[MarkerType.INTRO]
    marker = d.marker if d.status is DecisionStatus.DECIDED else None
    return {
        "status": d.status.value,
        "reason": d.reason,
        "marker": [marker.start_ms, marker.end_ms] if marker else None,
        "decided_by": list(marker.decided_by) if marker else None,
    }


def main() -> None:
    plex = plex_intros()
    out = {}
    for season in ("05", "06", "07", "08"):
        files = sorted(glob.glob(f"{ROOT}/Season {season}/*.mkv"))
        points = {f: cache.points(f) for f in files}
        steps = {
            "before": SeasonStep(season, points, ReproductionReport(), end_pictures),
            "after": SeasonStep(
                season, points, ReproductionReport(), end_pictures, speed=cache.speed, retimed=cache.retimed
            ),
        }
        for f in files:
            probe = probe_media(f, ffprobe="/usr/bin/ffprobe")
            episode = EvalEpisode(season, f, None, None, None)
            row = {
                "file": os.path.basename(f),
                "frame_rate": probe.frame_rate,
                "duration_ms": probe.duration_ms,
                "plex": plex.get(os.path.basename(f)),
                "retimed": f in steps["after"].clock.factors,
            }
            online = [c for c in introdb.get(code(f), {}).get("candidates", []) if c[0] == "intro"]
            row["introdb"] = online[0][1:] if online else None
            base = []
            if row["introdb"]:
                base.append(Candidate(MarkerType.INTRO, online[0][1], online[0][2], Source.INTRODB))
            if row["plex"]:
                base.append(Candidate(MarkerType.INTRO, *row["plex"], Source.SERVER_MARKERS, origin="plex"))
            for label, step in steps.items():
                answer = step.answer(episode)
                row[f"audio_{label}"] = list(answer) if answer else None
                cands = list(base)
                if answer:
                    others = len(step.files) - 1
                    cands.append(
                        Candidate(
                            MarkerType.INTRO,
                            round(answer[0] * 1000),
                            round(answer[1] * 1000),
                            Source.SEASON_AUDIO,
                            answer[2] / others,
                            f"{answer[2]}/{others}",
                        )
                    )
                rate = probe.frame_rate if label == "after" else None
                row[f"decision_{label}"] = decision(cands, probe.duration_ms, rate)
            out[code(f)] = row
            print(
                code(f),
                json.dumps({k: row[k] for k in ("frame_rate", "audio_before", "audio_after", "introdb", "plex")}),
                flush=True,
            )
        json.dump(out, open(HERE / "answers.json", "w"), indent=1)
    print("done", len(out))


main()
