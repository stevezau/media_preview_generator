"""The three populations Task 15 measures on: anime TV, non-anime TV and movies.

Reads the phase-2 read-only dump of prod Plex's parts (no server is queried) and Sonarr's own
``seriesType`` for the anime/non-anime split of TV. Sonarr is only used to *define the measurement
populations* -- the shipped rule may not read it, and does not: spec §5.1 scopes on the kind the
pipeline already resolved (episode vs movie), never on "is this anime".

Writes ``chapters_population.json`` (git-ignored: it holds real library paths).
"""

from __future__ import annotations

import json
import os
import random
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
PARTS = Path(os.environ.get("MARKERS_PARTS_DUMP") or HERE / "../../lab/results/scale/prod_plex_parts.json")
SONARR = HERE / "sonarr_series_min.json"

MOVIE_SECTION, MOVIE_TYPE = 1, 1
TV_SECTION, EPISODE_TYPE = 2, 4
# The non-anime TV population is 110k files; probing all of them would take most of a day on a shared
# box. A seeded flat sample is the unbiased estimator of a per-file error rate, which is what the
# precision question asks.
TV_SAMPLE = 12_000
SEED = 20260920


def _tvdb(path: str) -> int | None:
    """The ``{tvdb-NNN}`` id on the show folder, which Sonarr's own renamer wrote."""
    bits = path.split("/")
    if len(bits) < 4:
        return None
    token = bits[3]
    start = token.lower().find("{tvdb-")
    if start < 0:
        return None
    digits = token[start + 6 :].split("}")[0]
    return int(digits) if digits.isdigit() else None


def main() -> None:
    """Write one row per file with its population."""
    anime_ids = {r["tvdbId"] for r in json.loads(SONARR.read_text()) if r["seriesType"] == "anime" and r["tvdbId"]}
    parts = json.loads(PARTS.read_text())

    movies, anime, tv = [], [], []
    for part in parts:
        if part["section"] == MOVIE_SECTION and part["mtype"] == MOVIE_TYPE:
            movies.append(part["file"])
        elif part["section"] == TV_SECTION and part["mtype"] == EPISODE_TYPE:
            (anime if _tvdb(part["file"]) in anime_ids else tv).append(part["file"])

    sampled = random.Random(SEED).sample(tv, min(TV_SAMPLE, len(tv)))
    rows = (
        [{"file": f, "pop": "movie"} for f in movies]
        + [{"file": f, "pop": "anime"} for f in anime]
        + [{"file": f, "pop": "tv"} for f in sampled]
    )
    (HERE / "chapters_population.json").write_text(json.dumps(rows))
    print(f"movies {len(movies)} (all)  anime {len(anime)} (all)  non-anime TV {len(sampled)} of {len(tv)} (sampled)")
    print(dict(Counter(r["pop"] for r in rows)))


if __name__ == "__main__":
    main()
