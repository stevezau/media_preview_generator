"""Read-only: Plex root MediaContainer attributes relevant to marker serving (no token printed)."""

import json
import sys
import xml.etree.ElementTree as ET

import requests

settings = json.load(open(sys.argv[1]))
sid = "6c1e1100d1cf4024aad165698b356e11"
entry = next(s for s in settings["media_servers"] if s.get("id") == sid)
token = entry["auth"]["token"]
url = entry["url"].rstrip("/")
headers = {"X-Plex-Token": token, "Accept": "application/xml"}
root = ET.fromstring(requests.get(url + "/", headers=headers, timeout=30, verify=False).content)
keep = ("version", "myPlex", "myPlexSigninState", "myPlexSubscription", "myPlexMappingState", "claimed", "allowSync")
print({k: root.get(k) for k in keep})
feats = root.get("ownerFeatures") or ""
print(
    "ownerFeatures has intro/credits markers:",
    [f for f in feats.split(",") if "marker" in f.lower() or "intro" in f.lower() or "credit" in f.lower()],
)
ident = ET.fromstring(requests.get(url + "/identity", headers=headers, timeout=30, verify=False).content)
print("identity:", {k: ident.get(k) for k in ("claimed", "version")})
