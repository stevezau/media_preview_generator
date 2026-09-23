#!/usr/bin/env python3
"""Row 13 step 1: take our markers off the lab servers, and nothing else, before the phase 1-3 matrices re-run.

    MLAB_DIR=/path/to/lab-folder ./phase4_row13_reset.py

This is the "Lab reset used" step of phase1-results.md and phase2-results.md, as a script: turn intro and credits
detection off, run one normal Intro & Credits job over the lab's own libraries (the phase 1 scale mounts are left
alone), and every marker of ours the job finds is removed. Plex's own rows stay, and so does the marker file an earlier
lab test left on Synth Show S01E01 (plugin store). The job cannot see store entries an earlier app instance wrote (this
markers.db has no record of them), so the script deletes those for lab items through the plugin's own DELETE. It exits 1
when a lab item on Jellyfin or Emby still holds markers of ours: a matrix started from that state fails for the wrong
reason (a fresh markers.db can't tell them from the server's own).

Run it BEFORE removing mlab-app and mlab_app_config: the app's markers.db is what tells our markers from Plex's.
Afterwards: docker rm -f mlab-app; docker volume rm mlab_app_config; MLAB_APP_GPU=nvidia ./app.sh recreate;
./phase2_matrix.py configure. Detection is left off in the old config, which the volume removal discards.
"""

from __future__ import annotations

import sys

import phase1_matrix as p1
import phase2_matrix as p2

# Roots of the lab's own libraries, as the app sees them (up.sh's MV list, without the scale mounts).
CLEANUP_ROOTS = [
    p1.SYNTH_SHOW,
    p1.RICK_SEASON,
    "/media/tv/South Park (1997)/Season 01",
    "/media/movies/Toy Story (1995)",
    "/media/movies/Up (2009)",
    p2.AUDIO_SHOW,
    p2.MOVIE_FOLDER,
    "/media/synth-credits/Synth Credits (2024)",
    "/media/synth-credits/Synth Credits Open (2025)",
    "/media/synth/Synth Show (2020)",
]
LAB_PREFIXES = ("/media/synth", "/media/movies/Toy Story", "/media/movies/Up (2009)", "/media/tv/Rick and Morty (2013)/Season 01", "/media/tv/South Park (1997)/Season 01")  # fmt: skip


def is_lab_path(path: str) -> bool:
    return path.startswith(LAB_PREFIXES)


def set_detection(intro: bool, credits: bool) -> None:
    p1.app_ok("POST", "/api/settings", {"markers": {"detect": {"intro": intro, "credits": credits}}})
    stored = p1.app_ok("GET", "/api/settings")["markers"]["detect"]
    assert stored["intro"] is intro and stored["credits"] is credits, stored


KEPT_STORE_FILE = "Synth Show (2020) - S01E01.webm"


def jellyfin_lab_store_entries(server_id: str) -> dict[str, str]:
    """File path -> item id of every lab item with a file in the Bridge plugin's marker store."""
    store = p1.plugin_marker_files(server_id)
    return {
        path: item["item_id"]
        for path, item in p1.jf_items(server_id).items()
        if is_lab_path(path) and f"{item['item_id']}.json" in store
    }


def emby_lab_stored(server_id: str) -> list[str]:
    """Paths of the lab items the Emby Bridge plugin holds markers for (chapter markers from the file don't count)."""
    stored = []
    for path, item in p2.emby_items(server_id).items():
        if is_lab_path(path):
            _, body = p2.emby_bridge(server_id, "GET", item["id"])
            if body.get("Stored"):
                stored.append(path)
    return stored


def drop_store_entries_the_job_could_not_see() -> dict[str, list[str]]:
    """Delete Jellyfin store entries a previous app instance wrote: this markers.db has no record of them, so the job
    reads them as the server's own and leaves them (the phase 1 finding on a fresh config). Only lab items, and not
    the one file an earlier lab test left in the store (phase1-results.md)."""
    dropped: dict[str, list[str]] = {}
    for sid in p1.JELLYFINS:
        dropped[sid] = []
        for path, item_id in jellyfin_lab_store_entries(sid).items():
            if not path.endswith(KEPT_STORE_FILE):
                p1.jf(sid, "DELETE", f"/MediaPreviewBridge/Markers/{item_id}")
                dropped[sid].append(path)
    return dropped


def check_lists_are_not_empty() -> None:
    """A stopped container or a path prefix that matches nothing would make "nothing left" true of a lab never read."""
    for sid in p1.JELLYFINS:
        assert p1.plugin_marker_files(sid), f"{sid}: the plugin's marker store lists no files"
        assert any(is_lab_path(path) for path in p1.jf_items(sid)), f"{sid}: no lab item under {LAB_PREFIXES}"
    for sid in p2.EMBY_SERVERS:
        assert any(is_lab_path(path) for path in p2.emby_items(sid)), f"{sid}: no lab item under {LAB_PREFIXES}"


def remaining() -> dict[str, list[str]]:
    """Lab items each Jellyfin and Emby still holds markers of ours for (Plex's own rows can't be told from ours)."""
    left = {sid: sorted(jellyfin_lab_store_entries(sid)) for sid in p1.JELLYFINS}
    left.update({sid: sorted(emby_lab_stored(sid)) for sid in p2.EMBY_SERVERS})
    return left


def main() -> int:
    check_lists_are_not_empty()
    set_detection(intro=False, credits=False)
    job = p1.start_markers_job({"file_paths": CLEANUP_ROOTS, "library_name": "Row 13 reset: our markers off"})
    job = p1.wait_job(job["id"], timeout=3600)
    if job["status"] != "completed":
        p1.say(f"reset job ended {job['status']}")
        return 1
    dropped = drop_store_entries_the_job_could_not_see()
    p1.say("store entries dropped by hand:", {sid: len(paths) for sid, paths in dropped.items()})
    left = remaining()
    expected = {sid: [] for sid in left}
    for sid in p1.JELLYFINS:
        expected[sid] = [path for path in left[sid] if path.endswith(KEPT_STORE_FILE)]
    p1.say("still holding markers on lab items:", {sid: len(paths) for sid, paths in left.items()})
    return 0 if left == expected else 1


if __name__ == "__main__":
    sys.exit(main())
