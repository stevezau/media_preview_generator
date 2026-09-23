#!/usr/bin/env python3
"""Lab proof: ``turn_off_library_marker_detection`` really flips Plex's per-library switches, and only those.

    LAB_ENV_FILE=/path/to/env ./plex_detection_turnoff_proof.py

Builds a real ``PlexServer`` the way ``tests/test_servers_plex.py``'s ``plex_server_under_test`` fixture does
(a duck-typed legacy ``Config`` namespace with ``plex_url``/``plex_token``), against the throwaway lab Plex
(``mlab-plex``) — never the production ``plex`` host. Turns credits detection off on a movie section and
intro+credits off on a show section through the real method, reads ``/library/sections/<id>/prefs`` back to
confirm both are now off (the same value Plex's Edit library -> Advanced tab would show), then restores both
to their prior values with a direct PUT and reads back again to confirm the restore.

Prints only section keys and pref values — never the Plex token.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import requests  # noqa: E402

from media_preview_generator.servers.plex import PlexServer  # noqa: E402

LAB_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.environ.get("LAB_ENV_FILE", str(LAB_DIR / "env")))
PLEX_URL = os.environ.get("PLEX_URL", "http://127.0.0.1:32402")

# One movie section (credits only applies), one show section (intro + credits).
MOVIE_SECTION = "2"
SHOW_SECTION = "1"
INTRO_PREF = "enableIntroMarkerGeneration"
CREDITS_PREF = "enableCreditsMarkerGeneration"
OFF_VALUES = (False, "false", 0, "0")


def _load_token() -> str:
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("PLEX_TOKEN="):
            return line.split("=", 1)[1]
    raise SystemExit(f"PLEX_TOKEN not found in {ENV_FILE}")


def _read_prefs(token: str, section: str) -> dict[str, object]:
    resp = requests.get(
        f"{PLEX_URL}/library/sections/{section}/prefs",
        headers={"X-Plex-Token": token, "Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    settings = resp.json().get("MediaContainer", {}).get("Setting", [])
    return {s.get("id"): s.get("value") for s in settings if isinstance(s, dict)}


def _restore(token: str, section: str, prefs: dict[str, int]) -> None:
    """Direct PUT restore — deliberately not the method under test, so the restore doesn't depend on it."""
    query = urlencode(prefs)
    resp = requests.put(
        f"{PLEX_URL}/library/sections/{section}/prefs?{query}",
        headers={"X-Plex-Token": token},
        timeout=15,
    )
    resp.raise_for_status()


def main() -> None:
    token = _load_token()
    config = SimpleNamespace(plex_url=PLEX_URL, plex_token=token, plex_verify_ssl=True, plex_timeout=15)
    server = PlexServer(config, server_id="lab-plex", name="Lab Plex")

    before_movie = _read_prefs(token, MOVIE_SECTION)
    before_show = _read_prefs(token, SHOW_SECTION)
    print(f"before: movie[{CREDITS_PREF}]={before_movie.get(CREDITS_PREF)!r}")
    print(
        f"before: show[{INTRO_PREF}]={before_show.get(INTRO_PREF)!r} show[{CREDITS_PREF}]={before_show.get(CREDITS_PREF)!r}"
    )

    err_movie = server.turn_off_library_marker_detection(MOVIE_SECTION, [CREDITS_PREF])
    err_show = server.turn_off_library_marker_detection(SHOW_SECTION, [INTRO_PREF, CREDITS_PREF])
    print(f"turn_off_library_marker_detection(movie) error={err_movie!r}")
    print(f"turn_off_library_marker_detection(show) error={err_show!r}")

    after_movie = _read_prefs(token, MOVIE_SECTION)
    after_show = _read_prefs(token, SHOW_SECTION)
    print(f"after:  movie[{CREDITS_PREF}]={after_movie.get(CREDITS_PREF)!r}")
    print(
        f"after:  show[{INTRO_PREF}]={after_show.get(INTRO_PREF)!r} show[{CREDITS_PREF}]={after_show.get(CREDITS_PREF)!r}"
    )

    movie_off = after_movie.get(CREDITS_PREF) in OFF_VALUES
    show_off = after_show.get(INTRO_PREF) in OFF_VALUES and after_show.get(CREDITS_PREF) in OFF_VALUES
    print(f"movie credits now off: {movie_off}")
    print(f"show intro+credits now off: {show_off}")

    # Restore to whatever was there before (1 == the Plex boolean-pref "on" wire value).
    _restore(token, MOVIE_SECTION, {CREDITS_PREF: 1 if before_movie.get(CREDITS_PREF) else 0})
    _restore(
        token,
        SHOW_SECTION,
        {
            INTRO_PREF: 1 if before_show.get(INTRO_PREF) else 0,
            CREDITS_PREF: 1 if before_show.get(CREDITS_PREF) else 0,
        },
    )

    restored_movie = _read_prefs(token, MOVIE_SECTION)
    restored_show = _read_prefs(token, SHOW_SECTION)
    print(f"restored: movie[{CREDITS_PREF}]={restored_movie.get(CREDITS_PREF)!r}")
    print(
        f"restored: show[{INTRO_PREF}]={restored_show.get(INTRO_PREF)!r} "
        f"show[{CREDITS_PREF}]={restored_show.get(CREDITS_PREF)!r}"
    )

    if not (movie_off and show_off):
        raise SystemExit("turn-off did not take effect — see 'after' values above")
    if bool(restored_movie.get(CREDITS_PREF)) != bool(before_movie.get(CREDITS_PREF)):
        raise SystemExit("movie credits pref did not restore to its prior value")
    if bool(restored_show.get(INTRO_PREF)) != bool(before_show.get(INTRO_PREF)) or bool(
        restored_show.get(CREDITS_PREF)
    ) != bool(before_show.get(CREDITS_PREF)):
        raise SystemExit("show intro/credits prefs did not restore to their prior values")
    print("OK: turn-off confirmed, restore confirmed.")


if __name__ == "__main__":
    main()
