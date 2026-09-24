"""Read-only raw GETs against Plex with the app's configured URL/token; prints only marker counts (never the token)."""

import json
import sys

import requests

settings = json.load(open(sys.argv[1]))
sid = "6c1e1100d1cf4024aad165698b356e11"
entry = next(s for s in settings["media_servers"] if s.get("id") == sid)
auth = entry.get("auth") or {}
token = auth.get("token") or entry.get("token") or settings.get("plex_token")
url = entry.get("url") or settings.get("plex_url")
print("have token:", bool(token), "auth keys:", sorted(auth.keys()), "entry keys:", sorted(entry.keys()))
item = sys.argv[2]
variants = [
    (f"/library/metadata/{item}?includeMarkers=1", {}),
    (
        f"/library/metadata/{item}?includeMarkers=1",
        {"X-Plex-Client-Identifier": "probe-ro", "X-Plex-Product": "Plex Web"},
    ),
    (f"/library/metadata/{item}?includeMarkers=1&includeChapters=1", {}),
    (f"/library/metadata/{item}", {}),
]
for path, extra in variants:
    headers = {"X-Plex-Token": token, "Accept": "application/xml", **extra}
    r = requests.get(url.rstrip("/") + path, headers=headers, timeout=30, verify=False)
    body = r.text
    print(path, sorted(extra), "status", r.status_code, "Marker count", body.count("<Marker"), "len", len(body))
    if "<Marker" in body:
        i = body.index("<Marker")
        print("   ", body[i : i + 200])
headers = {"X-Plex-Token": token, "Accept": "application/xml"}
r = requests.get(url.rstrip("/") + "/", headers=headers, timeout=30, verify=False)
import re

m = re.search(r'version="([^"]+)"', r.text)
print("PMS version:", m.group(1) if m else None)
r = requests.get(url.rstrip("/") + "/myplex/account", headers=headers, timeout=30, verify=False)
print("account status", r.status_code, re.findall(r'(subscriptionActive|subscriptionState)="([^"]*)"', r.text))
