"""Cross-check "which shows are anime" against Sonarr's own `seriesType`.

Reads the Sonarr export `compare.py` already needs (`sonarr_series.json`) and the anime show list
`resolve.py` writes; holds no credentials and makes no request. The app cannot use Sonarr's answer —
this only says how far the id-based method is from a human-maintained one.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    """Compare Sonarr's anime series with the TVDB ids the id-based method calls anime."""
    series = json.loads((HERE / "sonarr_series.json").read_text())
    anime = [s for s in series if s.get("seriesType") == "anime"]
    files = sum(s.get("statistics", {}).get("episodeFileCount", 0) for s in anime)
    sonarr_ids = {s["tvdbId"] for s in anime if s.get("tvdbId")}
    print(f"Sonarr: {len(series)} series, {len(anime)} tagged anime, {files} episode files")

    # resolve.py writes every anime show folder, including the ones that resolved to no MAL id.
    list_ids = {int(t) for t in json.loads((HERE / "anime_shows.json").read_text())}
    print(f"id-based: {len(list_ids)} distinct TVDB ids")
    print(
        f"  in both {len(sonarr_ids & list_ids)} | Sonarr-only {len(sonarr_ids - list_ids)} | "
        f"id-based-only {len(list_ids - sonarr_ids)}"
    )


if __name__ == "__main__":
    main()
