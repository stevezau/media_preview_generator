"""The #310 library chapter set (intro-pick/data/chap.pkl: 60 season folders, 311 files, the intro chapter as truth)
dumped as decide-level evidence for the intro-end rule: each file's chapter candidates (the app's chapter rules), the
season's intro-chapter limit (finding F1), season audio (the app's step, one speed), IntroDB's raw intro (the app's
client, anonymous, paced; imdb ids from Sonarr, read-only), and the probed frame rate. Media is only read (nice 19).
Plex's markers aren't in it (the plex host is down); they never supply an edge.

Writes evidence_libchap.json (local only).
"""

import collections
import json
import os
import pickle
import re
import sys
from pathlib import Path

WT = "/home/data/workspace/plex_generate_vid_previews/.claude/worktrees/agent-a0d2f9fd9105568a3"
sys.path.insert(0, WT)
os.environ.setdefault("MARKERS_EVAL_EVIDENCE", f"{WT}/docs/design/intro-credits/evidence")
import requests  # noqa: E402
import yaml  # noqa: E402
from loguru import logger  # noqa: E402

from media_preview_generator.markers.audio import end_picture  # noqa: E402
from media_preview_generator.markers.audio.season import season_group  # noqa: E402
from media_preview_generator.markers.decide import intro_chapter_length_ms, intro_chapter_limit_ms  # noqa: E402
from media_preview_generator.markers.external_ids import ids_from_path  # noqa: E402
from media_preview_generator.markers.models import MediaIds  # noqa: E402
from media_preview_generator.markers.sources.chapters import chapter_candidates  # noqa: E402
from media_preview_generator.markers.sources.introdb import IntroDbClient  # noqa: E402
from tools.markers_eval.cache import FingerprintCache, ProbeCache  # noqa: E402
from tools.markers_eval.data import EvalEpisode  # noqa: E402
from tools.markers_eval.intros import DecodedEndPictures, ReproductionReport, SeasonStep  # noqa: E402

logger.remove()
HERE = Path(__file__).parent
PKL = Path(
    "/tmp/claude-1000/-home-data-workspace-plex-generate-vid-previews/a98735d3-c717-46ce-a02b-854ad26af8ae/scratchpad"
    "/intro-pick/data/chap.pkl"
)
FFMPEG = "/usr/bin/ffmpeg"
ROOT = Path.home() / ".cache/markers_eval"
cache = FingerprintCache(ROOT, ffmpeg=FFMPEG, ffprobe="/usr/bin/ffprobe")
probes = ProbeCache(ROOT, ffprobe="/usr/bin/ffprobe")
end_pictures = DecodedEndPictures(end_picture.Reader(ffmpeg=FFMPEG, gpu="NVIDIA", gpu_device_path="cuda:0"))

d = pickle.load(open(PKL, "rb"))
truth = {f: (tuple(t) if t else None) for f, t in d["truth"].items() if os.path.exists(f)}
print("files", len(truth), "with an intro chapter", sum(1 for t in truth.values() if t), flush=True)

# imdb ids for IntroDB, from Sonarr (the key is read here and never printed or written).
with open(os.path.expanduser("~/.variables.yml")) as fh:
    sonarr = yaml.safe_load(fh)["sonarr"]
series = requests.get(f"{sonarr['url']}/api/v3/series", headers={"X-Api-Key": sonarr["key"]}, timeout=60).json()
imdb_by_tvdb = {str(s.get("tvdbId")): s.get("imdbId") for s in series}
del sonarr

IDB_OUT = HERE / "introdb_libchap.json"
idb = json.load(open(IDB_OUT)) if IDB_OUT.exists() else {}
client = IntroDbClient()
for n, f in enumerate(sorted(truth)):
    if f in idb:
        continue
    m = re.search(r"\{tvdb-(\d+)\}", f)
    ids = ids_from_path(f)
    imdb = imdb_by_tvdb.get(m.group(1)) if m else None
    if not imdb or ids.season is None or ids.episode is None:
        idb[f] = {"status": "no_ids", "candidates": []}
        continue
    result = client.lookup(
        MediaIds(kind="episode", imdb=imdb, tvdb=m.group(1), season=ids.season, episode=ids.episode),
        duration_ms=None,
        priority=2,
    )
    idb[f] = {"status": result.status, "candidates": [[c.type.value, c.start_ms, c.end_ms] for c in result.candidates]}
    if n % 20 == 0:
        json.dump(idb, open(IDB_OUT, "w"), indent=1)
        print("introdb", n, result.status, flush=True)
json.dump(idb, open(IDB_OUT, "w"), indent=1)
print("introdb done:", collections.Counter(v["status"] for v in idb.values()), flush=True)

grouped = collections.defaultdict(list)
for f in truth:
    grouped[season_group(f).episodes].append(f)

rows_out = {}
for episodes, members in grouped.items():
    files = sorted(set(episodes) | set(members))
    chapters, lengths = {}, {}
    for f in files:
        try:
            probe = probes.probe(f)
        except Exception as exc:  # noqa: BLE001 - a survey: an unreadable sibling has no chapter
            print("probe failed", os.path.basename(f), type(exc).__name__, flush=True)
            continue
        chapters[f] = (probe.duration_ms, chapter_candidates(probe, is_episode=True))
        lengths[f] = intro_chapter_length_ms(chapters[f][1], probe.duration_ms)
    step = SeasonStep(
        os.path.dirname(files[0]),
        {f: cache.points(f) for f in files},
        ReproductionReport(),
        end_pictures,
        speed=cache.speed,
        retimed=cache.retimed,
    )
    for f in members:
        if f not in chapters:
            continue
        duration, cands = chapters[f]
        others = [length for g, length in lengths.items() if g != f and length is not None]
        answer = step.answer(EvalEpisode(os.path.dirname(f), f, truth[f], None, None))
        rows_out[f] = {
            "truth": list(truth[f]) if truth[f] else None,
            "chapters": [[c.type.value, c.start_ms, c.end_ms] for c in cands],
            "intro_chapter_limit_ms": intro_chapter_limit_ms(others),
            "audio": list(answer) if answer else None,
            "frame_rate": cache.frame_rate(f),
            "duration_ms": duration,
            "introdb": [[c[1], c[2]] for c in idb.get(f, {}).get("candidates", []) if c[0] == "intro"][:1],
        }
    print("group done", os.path.dirname(files[0])[-60:], len(rows_out), flush=True)
json.dump(rows_out, open(HERE / "evidence_libchap.json", "w"), indent=1, default=str)
print("libchap", len(rows_out), "with IntroDB", sum(1 for r in rows_out.values() if r["introdb"]),
      "with season audio", sum(1 for r in rows_out.values() if r["audio"]))  # fmt: skip
