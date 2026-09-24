"""imdb ids of the regression-set shows from Sonarr (read-only GET; the key is read from ~/.variables.yml, never printed
or written)."""

import json
import os
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).parent
with open(os.path.expanduser("~/.variables.yml")) as f:
    sonarr = yaml.safe_load(f)["sonarr"]
series = requests.get(f"{sonarr['url']}/api/v3/series", headers={"X-Api-Key": sonarr["key"]}, timeout=60).json()
shows = json.load(open(HERE / "shows.json"))
by_tvdb = {str(s.get("tvdbId")): s.get("imdbId") for s in series}
found = {tvdb: by_tvdb.get(tvdb) for tvdb in shows}
json.dump(found, open(HERE / "shows_imdb.json", "w"), indent=1)
print("with imdb:", sum(1 for v in found.values() if v), "of", len(found))
print("missing:", [shows[t] for t, v in found.items() if not v])
