#!/usr/bin/env python3
"""Set up the Open Films library on the lab servers and make its previews with this branch's app.

    ./lab_setup.py libraries   # create "Open Films" on mlab-plex, mlab-jellyfin and mlab-emby (once)
    ./lab_setup.py servers     # register the three servers in mlab-site-app, Open Films only
    ./lab_setup.py generate    # send every film through the custom webhook and wait for the job
    ./lab_setup.py verify      # check each server shows OUR previews; writes results/outputs.json

Plex's own preview generation is switched off for the library (enableBIFGeneration=0), and Jellyfin
and Emby see the films read-only, so any preview those servers show came from this app. Existing
libraries are never touched. Tokens come from the lab env file and are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = Path(
    os.environ.get(
        "MLAB_ENV", "/home/data/workspace/plex_generate_vid_previews/docs/design/intro-credits/evidence/lab/env"
    )
)
MEDIA_HOST = Path(os.environ.get("OPENFILMS_DIR", "/home/data/mlab-openfilms")) / "Movies"
MEDIA = "/media/openfilms/Movies"
LIBRARY = "Open Films"
PLEX = "http://127.0.0.1:32402"
JELLYFIN = "http://127.0.0.1:18097"
EMBY = "http://127.0.0.1:18096"
APP = "http://127.0.0.1:18083"
PLEX_CONFIG = "/plexcfg/Library/Application Support/Plex Media Server"
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".webm"}
NO_FETCHERS = [{"Type": "Movie", "MetadataFetchers": [], "ImageFetchers": []}]


def load_env() -> dict[str, str]:
    env = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            env[key.strip()] = value.strip()
    return env


ENV = load_env()


def http(method: str, url: str, *, headers: dict | None = None, body: object = None, timeout: int = 120) -> object:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - fixed http://127.0.0.1 lab URLs
        raw = response.read()
    return json.loads(raw) if raw.strip() else None


def plex(method: str, path: str, **params: object) -> dict:
    query = urllib.parse.urlencode(params)
    return http(method, f"{PLEX}{path}{'?' + query if query else ''}", headers={"X-Plex-Token": ENV["PLEX_TOKEN"]})


def jellyfin(method: str, path: str, body: object = None) -> object:
    return http(
        method, f"{JELLYFIN}{path}", headers={"Authorization": f'MediaBrowser Token="{ENV["JF_TOKEN"]}"'}, body=body
    )


def emby(method: str, path: str, body: object = None) -> object:
    separator = "&" if "?" in path else "?"
    return http(method, f"{EMBY}/emby{path}{separator}api_key={ENV['EMBY_TOKEN']}", body=body)


def app(method: str, path: str, body: object = None) -> object:
    return http(method, f"{APP}{path}", headers={"X-Auth-Token": ENV["MLAB_SITE_APP_TOKEN"]}, body=body)


def films() -> list[str]:
    """Container paths of every film in the library folder."""
    return sorted(
        f"{MEDIA}/{path.parent.name}/{path.name}"
        for path in MEDIA_HOST.glob("*/*")
        if path.suffix.lower() in VIDEO_SUFFIXES
    )


def wait_until(what: str, check: Callable[[], object], timeout: float = 600, every: float = 5) -> object:
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out waiting for {what}")
        time.sleep(every)


def plex_section() -> str | None:
    for section in plex("GET", "/library/sections")["MediaContainer"].get("Directory", []):
        if any(location["path"] == MEDIA for location in section.get("Location", [])):
            return section["key"]
    return None


def _virtual_folder(call: Callable[..., object]) -> dict | None:
    return next(
        (folder for folder in call("GET", "/Library/VirtualFolders") if MEDIA in folder.get("Locations", [])), None
    )


def _count(call: Callable[..., object], parent_id: str) -> int:
    return call("GET", f"/Items?ParentId={parent_id}&Recursive=true&IncludeItemTypes=Movie")["TotalRecordCount"]


def libraries() -> None:
    count = len(films())
    if not plex_section():
        plex("POST", "/library/sections", name=LIBRARY, type="movie", agent="tv.plex.agents.none",
             scanner="Plex Movie", language="xn", location=MEDIA)  # fmt: skip
    key = plex_section()
    plex("PUT", f"/library/sections/{key}/prefs", enableBIFGeneration=0)
    plex("GET", f"/library/sections/{key}/refresh")
    wait_until("Plex to list every film",
               lambda: len(plex("GET", f"/library/sections/{key}/all")["MediaContainer"].get("Metadata", [])) >= count)  # fmt: skip
    print(f"Plex: section {key} '{LIBRARY}', {count} films, Plex's own preview generation off for it")

    for name, call, options in (
        ("Jellyfin", jellyfin, {"EnableTrickplayImageExtraction": True, "ExtractTrickplayImagesDuringLibraryScan": False,
                                "SaveTrickplayWithMedia": True, "EnableRealtimeMonitor": False, "TypeOptions": NO_FETCHERS}),
        ("Emby", emby, {"EnableRealtimeMonitor": False, "TypeOptions": NO_FETCHERS}),
    ):  # fmt: skip
        if not _virtual_folder(call):
            query = urllib.parse.urlencode(
                {"name": LIBRARY, "collectionType": "movies", "paths": MEDIA, "refreshLibrary": "true"}
            )
            call("POST", f"/Library/VirtualFolders?{query}", {"LibraryOptions": options})
        folder = wait_until(f"{name} to create the library", lambda call=call: _virtual_folder(call))
        wait_until(
            f"{name} to list every film", lambda call=call, folder=folder: _count(call, folder["ItemId"]) >= count
        )
        print(f"{name}: '{LIBRARY}' lists {count} films (metadata and image fetchers off: local posters only)")


SERVERS = [
    {"id": "site-plex", "type": "plex", "name": "Home Plex", "url": "http://mlab-plex:32400",
     "auth": {"method": "token", "token": ENV["PLEX_TOKEN"]}, "output": {"plex_config_folder": PLEX_CONFIG}},
    {"id": "site-jellyfin", "type": "jellyfin", "name": "Home Jellyfin", "url": "http://mlab-jellyfin:8096",
     "auth": {"method": "api_key", "api_key": ENV["JF_TOKEN"]}},
    {"id": "site-emby", "type": "emby", "name": "Home Emby", "url": "http://mlab-emby:8096",
     "auth": {"method": "api_key", "api_key": ENV["EMBY_TOKEN"], "user_id": ENV["EMBY_UID"]}},
]  # fmt: skip


def servers() -> None:
    app("POST", "/api/setup/complete")
    listed = app("GET", "/api/servers")
    existing = {server["id"] for server in (listed.get("servers", []) if isinstance(listed, dict) else listed)}
    for entry in SERVERS:
        if entry["id"] in existing:
            app("PUT", f"/api/servers/{entry['id']}", {k: v for k, v in entry.items() if k != "id"})
        else:
            app("POST", "/api/servers", entry)
        app("POST", f"/api/servers/{entry['id']}/refresh-libraries")
        stored = app("GET", f"/api/servers/{entry['id']}")
        libraries = (stored.get("server", stored) or {}).get("libraries", [])
        for library in libraries:
            library["enabled"] = (library.get("title") or library.get("name")) == LIBRARY
        if not any(library["enabled"] for library in libraries):
            raise SystemExit(f"{entry['id']}: no library named {LIBRARY!r} after refresh")
        app("PUT", f"/api/servers/{entry['id']}", {"libraries": libraries})
        print(f"{entry['id']}: registered, only '{LIBRARY}' enabled")


def parse_timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def generate() -> None:
    paths = films()
    sent_at = time.time()
    app("POST", "/api/webhooks/custom", {"file_paths": paths, "title": "Open films"})

    def finished() -> list[dict] | None:
        jobs = [
            job
            for job in app("GET", "/api/jobs?page=1&per_page=50")["jobs"]
            if parse_timestamp(job["created_at"]) >= sent_at - 5
        ]
        done = [job for job in jobs if job["status"] in ("completed", "failed", "cancelled")]
        return done if jobs and len(done) == len(jobs) else None

    for job in wait_until("the generation job(s)", finished, timeout=3600, every=10):
        print(f"job {job['id']}: {job['status']} ({job.get('library_name')})")


def verify() -> None:
    record: dict = {
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "plex": {},
        "jellyfin": {},
        "emby": {},
    }
    key = plex_section()
    for item in plex("GET", f"/library/sections/{key}/all")["MediaContainer"]["Metadata"]:
        meta = plex("GET", f"/library/metadata/{item['ratingKey']}")["MediaContainer"]["Metadata"][0]
        record["plex"][meta["title"]] = meta["Media"][0]["Part"][0].get("indexes") == "sd"
    folder = _virtual_folder(jellyfin)
    items = jellyfin(
        "GET", f"/Items?ParentId={folder['ItemId']}&Recursive=true&IncludeItemTypes=Movie&Fields=Trickplay"
    )["Items"]
    for item in items:
        trickplay = item.get("Trickplay") or {}
        record["jellyfin"][item["Name"]] = bool(next(iter(trickplay.values()), {}))
    # Only the film folders: the Movies folder on storage also holds the download script.
    for film in sorted(path for path in MEDIA_HOST.iterdir() if path.is_dir()):
        record["emby"][film.name] = any(film.glob("*.bif"))
    out = HERE / "results" / "outputs.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failures = [
        f"{server}: {title}"
        for server in ("plex", "jellyfin", "emby")
        for title, ok in record[server].items()
        if not ok
    ]
    print("all previews present" if not failures else "missing previews:\n  " + "\n  ".join(failures))
    raise SystemExit(1 if failures else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["libraries", "servers", "generate", "verify"])
    {"libraries": libraries, "servers": servers, "generate": generate, "verify": verify}[parser.parse_args().command]()


if __name__ == "__main__":
    main()
