"""tvdb -> imdb for every show of the intro sets (Sonarr, read-only GET; the key is read from ~/.variables.yml and never
printed or written). Saves imdb_by_tvdb.json."""

from paths import EVIDENCE_DIR, HERE, LOCAL, REPO  # noqa: F401
import json
import os
import re

import requests
import yaml

R = str(LOCAL)
with open(os.path.expanduser("~/.variables.yml")) as fh:
    sonarr = yaml.safe_load(fh)["sonarr"]
series = requests.get(f"{sonarr['url']}/api/v3/series", headers={"X-Api-Key": sonarr["key"]}, timeout=60).json()
del sonarr
by_tvdb = {str(s.get("tvdbId")): s.get("imdbId") for s in series if s.get("imdbId")}
wanted = set()
for name in ("lab118", "heldout175", "accused", "libchap"):
    for path in json.load(open(f"{EVIDENCE_DIR}/intro-end/evidence_{name}.json")):
        m = re.search(r"\{tvdb-(\d+)\}", path)
        if m:
            wanted.add(m.group(1))
import sqlite3
details = json.load(open(f"{R}/ae3_gpu.json"))["details"]
for row in details["tv40"]:
    m = re.search(r"\{tvdb-(\d+)\}", row["file"])
    if m:
        wanted.add(m.group(1))
db = sqlite3.connect(f"file:{R}/audit0925/post.db?mode=ro", uri=True)
for (path,) in db.execute("select canonical_path from files"):
    m = re.search(r"\{tvdb-(\d+)\}", path)
    if m:
        wanted.add(m.group(1))
found = {t: by_tvdb.get(t) for t in sorted(wanted)}
json.dump(found, open(f"{R}/imdb_by_tvdb.json", "w"), indent=0)
print("shows", len(found), "with imdb", sum(1 for v in found.values() if v))
