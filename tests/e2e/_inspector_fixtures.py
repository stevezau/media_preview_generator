"""Payloads and a mocked API for the Inspector's e2e tests.

Every Inspector call is answered here through one ``page.route`` dispatcher, in the shapes the real routes return
(``api_inspector``, ``api_markers.marker_item``, ``api_jobs.media_search``). Frames are small JPEGs drawn with Pillow:
scenes in colour, an intro's title card, credits as white lines on black, so a screenshot reads like the design.
Writes (save, unlock, re-detect, regenerate) are captured, appended before they are answered (see ``_mocks``).
``InspectorApi.answers`` swaps a route's answer (an error, a different save result), ``hold_writes`` keeps a write
unanswered until ``release_writes()``, and ``hold_paths`` does the same for one file's ``GET /api/inspector/file``.
"""

from __future__ import annotations

import base64
import copy
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw
from playwright.sync_api import Page, Route

from ._mocks import _fulfill_json

SCREENSHOT_DIR = os.environ.get("INSPECTOR_SCREENSHOT_DIR", "")

SERVERS = [
    {"id": "plex-1", "name": "Plex", "type": "plex", "enabled": True},
    {"id": "jf-1", "name": "Jellyfin", "type": "jellyfin", "enabled": True},
]

EPISODE = "/data/tv/Blood Legacy (2024) {tvdb-436915}/Season 01/Blood Legacy (2024) - S01E01 - [WEBDL-1080p][EAC3 5.1][h264].mkv"
EPISODE_MS = 1_656_000
FILM = "/data/movies/The Matrix (1999) {tmdb-603}/The Matrix (1999) {imdb-tt0133093} - [Bluray-2160p][EAC3 7.1][DV HDR10][x265]-hallowed.mkv"
FILM_1080 = (
    "/data/movies/The Matrix (1999) {tmdb-603}/The Matrix (1999) {imdb-tt0133093} - [Bluray-1080p][DTS 5.1][x264].mkv"
)
FILM_MS = 8_178_000
UNDECIDED = "/data/movies/10 Things I Hate About You (1999)/10 Things I Hate About You (1999) - [Bluray-1080p].mkv"
UNDECIDED_MS = 5_820_000
SHOW_FOLDER = "/data/tv/Blood Legacy (2024) {tvdb-436915}"
PLEX_BIF = "/plex/Media/localhost/a/bc123.bundle/Contents/Indexes/index-sd.bif"

_SCENE = [(59, 74, 92), (77, 63, 55), (47, 74, 68), (81, 71, 94), (61, 70, 82), (74, 64, 50)]


def screenshot(page: Page, name: str) -> None:
    """Save a full-page screenshot when ``INSPECTOR_SCREENSHOT_DIR`` is set (for checking against the design)."""
    if not SCREENSHOT_DIR:
        return
    Path(SCREENSHOT_DIR).mkdir(parents=True, exist_ok=True)
    # The design's canvas width; the page lays the strip out again for it before the picture is taken.
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(500)
    page.screenshot(path=str(Path(SCREENSHOT_DIR) / f"{name}.png"), full_page=True)


def frame_jpeg(kind: str, seed: int, width: int = 320) -> bytes:
    """A small JPEG: ``scene`` (a coloured shot), ``title`` (an intro card) or ``credits`` (white lines on black)."""
    height = width * 9 // 16
    if kind == "credits":
        img = Image.new("RGB", (width, height), (11, 12, 18))
        draw = ImageDraw.Draw(img)
        for i, share in enumerate((0.44, 0.30, 0.38)):
            y = height // 2 - 12 + i * 12
            half = int(width * share / 2)
            draw.rectangle([width // 2 - half, y, width // 2 + half, y + 2], fill=(207, 210, 222))
    elif kind == "title":
        img = Image.new("RGB", (width, height), (15, 16, 26))
        draw = ImageDraw.Draw(img)
        draw.rectangle([int(width * 0.22), height // 2 - 8, int(width * 0.78), height // 2 - 2], fill=(223, 226, 236))
        draw.rectangle([int(width * 0.35), height // 2 + 6, int(width * 0.65), height // 2 + 8], fill=(223, 226, 236))
    else:
        base = _SCENE[seed % len(_SCENE)]
        img = Image.new("RGB", (width, height), base)
        draw = ImageDraw.Draw(img)
        light = tuple(min(255, c + 40) for c in base)
        draw.rectangle([0, 0, width, height // 3], fill=light)
        draw.ellipse(
            [width // 3, height // 3, width // 3 + height // 3, height // 3 + height // 3], fill=(200, 170, 120)
        )
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def _type(status: str | None, marker: dict | None = None, *, reason: str = "", proposed: dict | None = None) -> dict:
    return {"status": status, "reason": reason, "marker": marker, "proposed": proposed, "shortened_by": None}


def _marker(mtype: str, start: int, end: int, decided_by: list[str], *, locked: bool = False) -> dict:
    return {
        "type": mtype,
        "start_ms": start,
        "end_ms": end,
        "decided_by": decided_by,
        "locked": locked,
        "locked_at": "2026-09-28T00:50:00+00:00" if locked else None,
    }


def _server(sid: str, name: str, stype: str, **over: Any) -> dict:
    row = {
        "server_id": sid,
        "server_name": name,
        "server_type": stype,
        "markers_enabled": True,
        "capability_state": "ready",
        "can_show": ["intro", "credits"] if stype != "jellyfin" else ["intro", "credits", "recap", "preview"],
        "current": [],
        "duration_ms": None,
        "keeps_server_markers": False,
        "published": [],
        "publish_status": None,
        "publish_message": "",
        "item_status": None,
        "plan": "nothing_to_publish",
        "plan_reason": "",
        "version_count": 1,
        "error": None,
    }
    row.update(over)
    return row


def _evidence(source: str, mtype: str | None, start: int | None, end: int | None, **over: Any) -> dict:
    row = {
        "source": source,
        "origin": "",
        "type": mtype,
        "start_ms": start,
        "end_ms": end,
        "confidence": 1.0,
        "detail": "",
        "fetched_at": "2026-09-28T00:50:00+00:00",
        "label": "",
    }
    row.update(over)
    return row


def _preview_row(sid: str, name: str, stype: str, **over: Any) -> dict:
    row = {
        "server_id": sid,
        "server_name": name,
        "server_type": stype,
        "kind": "trickplay" if stype == "jellyfin" else "bif",
        "path": "",
        "exists": False,
        "note": "",
        "error": "",
        "versions": [],
    }
    row.update(over)
    return row


def checked_episode() -> tuple[dict, dict]:
    """Board 2: an episode with a decided intro and credits; Plex shows them, Jellyfin gets them on the next job."""
    intro = _marker("intro", 165_000, 179_000, ["season_audio"])
    credits = _marker("credits", 1_499_000, EPISODE_MS, ["chapters", "credits_text"])
    plex_preview = _preview_row(
        "plex-1",
        "Plex",
        "plex",
        path=PLEX_BIF,
        exists=True,
        frame_count=828,
        interval_ms=2000,
        file_size=17_400_000,
        created_at="2026-09-27T14:19:00+00:00",
        item_id="557676",
    )
    jf_preview = _preview_row(
        "jf-1",
        "Jellyfin",
        "jellyfin",
        path=f"{os.path.dirname(EPISODE)}/trickplay/Blood Legacy (2024) - S01E01 - [WEBDL-1080p]/320 - 10x10",
        exists=True,
        frame_count=166,
        interval_ms=10000,
        file_size=2_100_000,
        created_at="2026-09-27T14:20:00+00:00",
        tile_width=10,
        tile_height=10,
        sheets_dir=f"{os.path.dirname(EPISODE)}/trickplay/Blood Legacy (2024) - S01E01 - [WEBDL-1080p]/320 - 10x10",
    )
    file = {
        "canonical_path": EPISODE,
        "exists": True,
        "in_library": True,
        "known": True,
        "title": "Blood Legacy (2024) · S01E01",
        "kind": "episode",
        "quality": "1080p",
        "duration_ms": EPISODE_MS,
        "previews": [plex_preview, jf_preview],
        "preview": dict(plex_preview),
        "versions": [],
        "job": None,
    }
    item = {
        "known": True,
        "canonical_path": EPISODE,
        "duration_ms": EPISODE_MS,
        "is_movie": False,
        "decisions": {
            "intro": _type("decided", intro),
            "credits": _type("decided", credits),
            "recap": _type("disabled", reason="detection off"),
            "preview": _type("disabled", reason="detection off"),
        },
        "evidence": [
            _evidence("season_audio", "intro", 165_000, 179_000, label="20/20"),
            _evidence("chapters", "credits", 1_499_000, None, label="Credits"),
            _evidence("credits_text", "credits", 1_502_000, None, detail="Read on the GPU (NVIDIA TITAN RTX) in 4.9 s"),
            _evidence("introdb", None, None, None, detail="IntroDB has no entry for this episode"),
            _evidence("skipdb", None, None, None, detail="SkipDB has no entry for this episode"),
            _evidence(
                "server_markers",
                "credits",
                1_499_000,
                None,
                origin="plex-1",
                detail="Made for an earlier file at this path (the file was replaced since); not a second opinion",
            ),
        ],
        "servers": [
            _server(
                "plex-1",
                "Plex",
                "plex",
                current=[
                    {"type": "intro", "start_ms": 165_000, "end_ms": 179_000, "ours": True, "stale": False},
                    {"type": "credits", "start_ms": 1_499_000, "end_ms": None, "ours": True, "stale": False},
                ],
                plan="up_to_date",
                publish_status="written",
                published=[
                    {"type": "intro", "start_ms": 165_000, "end_ms": 179_000},
                    {"type": "credits", "start_ms": 1_499_000, "end_ms": EPISODE_MS},
                ],
            ),
            _server("jf-1", "Jellyfin", "jellyfin", plan="will_add"),
        ],
    }
    return file, item


def unchecked_film() -> tuple[dict, dict]:
    """Board 3: a film Intro & Credits hasn't checked; Plex shows two credits markers near the end."""
    plex_preview = _preview_row(
        "plex-1",
        "Plex",
        "plex",
        path="/plex/Media/localhost/b/matrix01.bundle/Contents/Indexes/index-sd.bif",
        exists=True,
        frame_count=4089,
        interval_ms=2000,
        file_size=17_400_000,
        created_at="2025-08-30T14:19:00+00:00",
    )
    file = {
        "canonical_path": FILM,
        "exists": True,
        "in_library": True,
        "known": False,
        "title": "The Matrix (1999)",
        "kind": "movie",
        "quality": "2160p Dolby Vision",
        "duration_ms": FILM_MS,
        "previews": [plex_preview],
        "preview": dict(plex_preview),
        "versions": [],
        "job": None,
    }
    item = {
        "known": False,
        "canonical_path": FILM,
        "duration_ms": None,
        "is_movie": None,
        "decisions": {t: _type(None) for t in ("intro", "credits", "recap", "preview")},
        "evidence": [],
        "servers": [
            _server(
                "plex-1",
                "Plex",
                "plex",
                keeps_server_markers=True,
                current=[
                    {"type": "credits", "start_ms": 7_680_000, "end_ms": 7_710_000, "ours": False, "stale": False},
                    {"type": "credits", "start_ms": 7_768_000, "end_ms": None, "ours": False, "stale": False},
                ],
                duration_ms=FILM_MS,
            )
        ],
    }
    return file, item


def undecided_film() -> tuple[dict, dict]:
    """Board 4: the credits weren't found; the chapter and the on-screen text disagree by 28 s."""
    plex_preview = _preview_row(
        "plex-1",
        "Plex",
        "plex",
        path="/plex/Media/localhost/c/tenthings.bundle/Contents/Indexes/index-sd.bif",
        exists=True,
        frame_count=1164,
        interval_ms=5000,
        file_size=9_100_000,
        created_at="2026-09-20T09:00:00+00:00",
    )
    file = {
        "canonical_path": UNDECIDED,
        "exists": True,
        "in_library": True,
        "known": True,
        "title": "10 Things I Hate About You (1999)",
        "kind": "movie",
        "quality": "1080p",
        "duration_ms": UNDECIDED_MS,
        "previews": [plex_preview],
        "preview": dict(plex_preview),
        "versions": [],
        "job": None,
    }
    item = {
        "known": True,
        "canonical_path": UNDECIDED,
        "duration_ms": UNDECIDED_MS,
        "is_movie": True,
        "decisions": {
            "intro": _type("disabled", reason="detection off"),
            "credits": _type("no_evidence", reason="sources disagree"),
            "recap": _type("disabled", reason="detection off"),
            "preview": _type("disabled", reason="detection off"),
        },
        "evidence": [
            _evidence("chapters", "credits", 5_529_000, None, label="Credits"),
            _evidence("credits_text", "credits", 5_557_000, None),
            _evidence("server_markers", "credits", 5_557_000, None, origin="plex-1"),
        ],
        "servers": [
            _server(
                "plex-1",
                "Plex",
                "plex",
                current=[{"type": "credits", "start_ms": 5_557_000, "end_ms": None, "ours": False, "stale": False}],
                plan="nothing_to_publish",
            )
        ],
    }
    return file, item


FILM_CREDITS_MS = 7_765_000
EMBY_FILM_BIF = f"{os.path.dirname(FILM)}/The Matrix (1999) {{imdb-tt0133093}} - [Bluray-2160p]-320-10.bif"
LONG_FILM = "/data/movies/The Long Film (2020)/The Long Film (2020) - [Bluray-1080p].mkv"
LONG_FILM_MS = 10_800_000


def checked_film() -> tuple[dict, dict]:
    """Board 5: The Matrix, checked. SkipDB puts the credits at 2:09:25; Plex keeps its own two credits markers, and
    Jellyfin and Emby show ours."""
    file, item = unchecked_film()
    jf_dir = f"{os.path.dirname(FILM)}/trickplay/The Matrix (1999)/320 - 10x10"
    file["known"] = True
    file["previews"] += [
        _preview_row(
            "jf-1",
            "Jellyfin",
            "jellyfin",
            path=jf_dir,
            sheets_dir=jf_dir,
            exists=True,
            frame_count=818,
            interval_ms=10_000,
            tile_width=10,
            tile_height=10,
        ),
        _preview_row("emby-1", "Emby", "emby", path=EMBY_FILM_BIF, exists=True, frame_count=818, interval_ms=10_000),
    ]
    credits = _marker("credits", FILM_CREDITS_MS, FILM_MS, ["skipdb"])
    ours = [{"type": "credits", "start_ms": FILM_CREDITS_MS, "end_ms": None, "ours": True, "stale": False}]
    item.update(
        known=True,
        duration_ms=FILM_MS,
        is_movie=True,
        decisions={
            "intro": _type("disabled", reason="films have no intro"),
            "credits": _type("decided", credits),
            "recap": _type("disabled", reason="detection off"),
            "preview": _type("disabled", reason="detection off"),
        },
        evidence=[
            _evidence("chapters", None, None, None, detail="Chapter names inside the file"),
            _evidence("skipdb", "credits", FILM_CREDITS_MS, None),
            _evidence("server_markers", "credits", 7_680_000, 7_710_000, origin="plex-1"),
            _evidence("server_markers", "credits", 7_768_000, None, origin="plex-1"),
        ],
    )
    item["servers"][0].update(plan="keeps_plex", markers_enabled=True)
    item["servers"] += [
        _server(
            "jf-1", "Jellyfin", "jellyfin", current=copy.deepcopy(ours), plan="up_to_date", publish_status="written"
        ),
        _server("emby-1", "Emby", "emby", current=copy.deepcopy(ours), plan="up_to_date", publish_status="written"),
    ]
    return file, item


def mixed_film() -> tuple[dict, dict]:
    """Board 5 with the servers mixed: Jellyfin couldn't be read, and Emby gets ours on the next job."""
    file, item = checked_film()
    item["servers"][1].update(
        current=None,
        plan="unknown",
        publish_status=None,
        error="Couldn't read this server's Intro & Credits state (ConnectionError)",
    )
    item["servers"][2].update(current=[], plan="will_add", publish_status=None)
    return file, item


def long_film() -> tuple[dict, dict]:
    """A three-hour film with a preview frame every 2 s (5,400 frames): the strip must stay light on it."""
    preview = _preview_row(
        "plex-1",
        "Plex",
        "plex",
        path="/plex/Media/localhost/d/longfilm.bundle/Contents/Indexes/index-sd.bif",
        exists=True,
        frame_count=5400,
        interval_ms=2000,
        file_size=60_000_000,
        created_at="2026-09-01T10:00:00+00:00",
    )
    file = {
        "canonical_path": LONG_FILM,
        "exists": True,
        "in_library": True,
        "known": True,
        "title": "The Long Film (2020)",
        "kind": "movie",
        "quality": "1080p",
        "duration_ms": LONG_FILM_MS,
        "previews": [preview],
        "preview": dict(preview),
        "versions": [],
        "job": None,
    }
    credits = _marker("credits", 10_500_000, LONG_FILM_MS, ["chapters"])
    item = {
        "known": True,
        "canonical_path": LONG_FILM,
        "duration_ms": LONG_FILM_MS,
        "is_movie": True,
        "decisions": {
            "intro": _type("disabled"),
            "credits": _type("decided", credits),
            "recap": _type("disabled"),
            "preview": _type("disabled"),
        },
        "evidence": [_evidence("chapters", "credits", 10_500_000, None, label="Credits")],
        "servers": [
            _server(
                "plex-1",
                "Plex",
                "plex",
                current=[{"type": "credits", "start_ms": 10_500_000, "end_ms": None, "ours": True, "stale": False}],
                plan="up_to_date",
            )
        ],
    }
    return file, item


# Public names for the builders above, for tests that assemble their own payloads.
decision = _type
marker = _marker
server_row = _server
evidence_row = _evidence
preview_row = _preview_row


def save_row(sid: str, name: str, stype: str, result: str = "written", **over: Any) -> dict:
    """One server's row in the ``POST /api/markers/item/markers`` answer (``api_markers`` save's shape)."""
    row = {
        "server_id": sid,
        "server_name": name,
        "server_type": stype,
        "result": result,
        "message": "",
        "can_show": ["intro", "credits"],
        "cant_show": [],
        "notes": [],
        "replaced_own": [],
    }
    row.update(over)
    return row


def save_answer(path: str | None, rows: list[dict]) -> dict:
    return {"canonical_path": path, "duration_ms": 0, "markers": {}, "servers": rows, "queued_job_id": None}


def search_results() -> list[dict]:
    """``GET /api/media/search?q=matrix`` plus a show, as ``api_jobs.media_search`` merges them."""
    plex = [{"id": "plex-1", "name": "Plex", "type": "plex"}]
    both = plex + [{"id": "jf-1", "name": "Jellyfin", "type": "jellyfin"}]
    return [
        {
            "kind": "show",
            "title": "Blood Legacy",
            "year": 2024,
            "paths": [SHOW_FOLDER],
            "child_count": 18,
            "servers": both,
        },
        {"kind": "movie", "title": "The Matrix", "year": 1999, "paths": [FILM], "child_count": None, "servers": plex},
        {
            "kind": "movie",
            "title": "The Matrix Reloaded",
            "year": 2003,
            "paths": ["/data/movies/The Matrix Reloaded (2003)/The Matrix Reloaded (2003) - [Bluray-2160p][HDR10].mkv"],
            "child_count": None,
            "servers": plex,
        },
        {
            "kind": "movie",
            "title": "The Matrix Resurrections",
            "year": 2021,
            "paths": [
                "/data/movies/The Matrix Resurrections (2021)/The Matrix Resurrections (2021) - [WEBDL-2160p][DV].mkv"
            ],
            "child_count": None,
            "servers": plex,
        },
    ]


def statuses() -> dict:
    items = {
        FILM: ("2160p Dolby Vision", {"state": "ready", "frames": 4089}, ("not_checked", "Not checked yet")),
        search_results()[2]["paths"][0]: (
            "2160p HDR10",
            {"state": "ready", "frames": None},
            ("credits", "Credits set"),
        ),
        search_results()[3]["paths"][0]: (
            "2160p Dolby Vision",
            {"state": "missing", "frames": None},
            ("none", "Nothing found"),
        ),
    }
    return {
        path: {
            "in_library": True,
            "exists": True,
            "kind": "movie",
            "quality": quality,
            "servers": ["Plex"],
            "preview": {**preview, "servers": ["Plex"] if preview["state"] == "ready" else []},
            "markers": {"state": m[0], "label": m[1]},
        }
        for path, (quality, preview, m) in items.items()
    }


def show_seasons() -> list[dict]:
    states = ["both", "both", "both", "credits", "both", "both", "not_checked", "both", "both", "none"]
    labels = {
        "both": "Intro + credits",
        "credits": "Credits only",
        "not_checked": "Not checked yet",
        "none": "Nothing found",
    }

    def season(n: int, count: int) -> dict:
        eps = []
        for i in range(count):
            s = states[i % len(states)]
            path = f"{SHOW_FOLDER}/Season {n:02d}/Blood Legacy (2024) - S{n:02d}E{i + 1:02d} - [WEBDL-1080p].mkv"
            if n == 1 and i == 0:
                path = EPISODE
            eps.append(
                {"path": path, "code": f"E{i + 1:02d}", "episode": i + 1, "markers": {"state": s, "label": labels[s]}}
            )
        return {"season": n, "label": f"Season {n}", "episodes": eps}

    return [season(1, 10), season(2, 8)]


def season_payload(*, ready: int | None = None, total: int | None = None, markers_on: bool = True) -> dict:
    """``GET /api/markers/season`` for Blood Legacy season 1 (``markers.inspect.season_payload``'s shape)."""
    folder = f"{SHOW_FOLDER}/Season 01"
    durations = [
        EPISODE_MS,
        1_710_000,
        1_590_000,
        1_650_000,
        1_620_000,
        1_680_000,
        None,
        1_640_000,
        1_700_000,
        1_660_000,
    ]
    episodes = []
    for i, dur in enumerate(durations):
        n = i + 1
        path = EPISODE if n == 1 else f"{folder}/Blood Legacy (2024) - S01E{n:02d} - [WEBDL-1080p].mkv"
        intro = _type("decided", _marker("intro", 165_000 + i * 2000, 179_000 + i * 2000, ["season_audio"]))
        credits = _type(
            "decided", _marker("credits", (dur or EPISODE_MS) - 157_000, dur or EPISODE_MS, ["chapters"], locked=n == 2)
        )
        if n == 4:
            intro = _type("no_evidence")
        if n == 7:
            intro, credits = _type(None), _type(None)
        if n == 10:
            credits = _type("no_evidence", reason="Sources disagree: chapters, credits_text")
        for d in (intro, credits):
            d.pop("shortened_by", None)
        dots = {
            "plex-1": {"state": "ok" if n not in (7, 10) else "none", "message": ""},
            "jf-1": {"state": "waiting" if n == 3 else "none", "message": "Waiting for Jellyfin to add the file"},
        }
        episodes.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "episode": f"E{n:02d}",
                "known": n != 7,
                "duration_ms": dur,
                "intro": intro,
                "credits": credits,
                "evidence": []
                if n == 7
                else [{"source": "season_audio", "label": "9/10"}, {"source": "chapters", "label": ""}],
                "servers": dots,
            }
        )
    decided = sum(1 for e in episodes if e["known"])
    return {
        "folder": folder,
        "show": "Blood Legacy (2024) {tvdb-436915}",
        "season": "Season 1",
        "servers": [
            {"server_id": "plex-1", "server_name": "Plex", "server_type": "plex", "markers_enabled": markers_on},
            {"server_id": "jf-1", "server_name": "Jellyfin", "server_type": "jellyfin", "markers_enabled": markers_on},
        ],
        "episodes": episodes,
        "counts": {
            "episodes": len(episodes),
            "total_episodes": total if total is not None else len(episodes),
            "ready": ready if ready is not None else decided,
        },
    }


@dataclass
class InspectorApi:
    """The mocked Inspector API for one page: payloads per path, and every write it was sent."""

    files: dict[str, dict] = field(default_factory=dict)
    items: dict[str, Any] = field(default_factory=dict)
    kinds: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)
    saves: list[dict] = field(default_factory=list)
    unlocks: list[dict] = field(default_factory=list)
    redetects: list[dict] = field(default_factory=list)
    season: dict | None = None
    season_requests: list[str] = field(default_factory=list)
    season_publishes: list[dict] = field(default_factory=list)
    manual_jobs: list[dict] = field(default_factory=list)
    frame_requests: list[dict] = field(default_factory=list)
    # While True, GET /api/inspector/file isn't answered until ``release()``: the page's loading state stays up.
    hold_files: bool = False
    held: list[tuple[Route, dict]] = field(default_factory=list)
    after_save: dict[str, dict] = field(default_factory=dict)
    after_unlock: dict[str, dict] = field(default_factory=dict)
    # A route's answer in place of the default, as (status, body). Keys: save, unlock, redetect, manual, publish,
    # search, status, show, season.
    answers: dict[str, tuple[int, Any]] = field(default_factory=dict)
    # Writes named here (save, unlock, redetect, manual, publish) are captured but not answered until
    # ``release_writes()``, so the page's in-flight state stays up.
    hold_writes: set[str] = field(default_factory=set)
    held_writes: list[tuple[Route, Any, int]] = field(default_factory=list)
    # GET /api/inspector/file for these paths isn't answered until ``release_path(path)``.
    hold_paths: set[str] = field(default_factory=set)
    held_paths: dict[str, list[tuple[Route, Any, int]]] = field(default_factory=dict)
    servers_list: list[dict] = field(default_factory=lambda: copy.deepcopy(SERVERS))
    # POST /api/inspector/status rows per path, over ``statuses()``.
    status_items: dict[str, dict] = field(default_factory=dict)
    # GET /api/bif/info answers per BIF path (``?bif=`` opens); any other path is a 400.
    bif_info: dict[str, dict] = field(default_factory=dict)
    # GET /api/inspector/frames: how many 503s (server busy) a read starting at start_ms gets before its frames.
    frames_busy: dict[int, int] = field(default_factory=dict)
    # GET /api/inspector/frames: every read answers this (status, body) instead of frames.
    frames_error: tuple[int, dict] | None = None
    file_requests: list[str] = field(default_factory=list)
    item_requests: list[str] = field(default_factory=list)
    # GET /api/bif/frame and /api/bif/trickplay/frame: the frame index of every image the page asked for.
    image_requests: list[int] = field(default_factory=list)
    search_requests: list[dict] = field(default_factory=list)

    def release(self) -> None:
        self.hold_files = False
        while self.held:
            route, body = self.held.pop(0)
            _fulfill_json(route, body)

    def release_writes(self) -> None:
        self.hold_writes = set()
        while self.held_writes:
            route, body, status = self.held_writes.pop(0)
            _fulfill_json(route, body, status=status)

    def release_path(self, path: str) -> None:
        self.hold_paths.discard(path)
        for route, body, status in self.held_paths.pop(path, []):
            _fulfill_json(route, body, status=status)

    def add(self, file: dict, item: Any, kinds: list[tuple[int, int, str]] | None = None) -> None:
        path = file["canonical_path"]
        self.files[path] = copy.deepcopy(file)
        self.items[path] = copy.deepcopy(item)
        self.kinds[path] = kinds or []

    def kind_at(self, path: str, t_ms: int) -> str:
        for start, end, kind in self.kinds.get(path, []):
            if start <= t_ms < end:
                return kind
        return "scene"


def default_kinds(path: str) -> list[tuple[int, int, str]]:
    if path == EPISODE:
        return [(165_000, 179_000, "title"), (1_499_000, EPISODE_MS + 1, "credits")]
    if path == FILM:
        return [(7_680_000, 7_710_000, "credits"), (FILM_CREDITS_MS, FILM_MS + 1, "credits")]
    if path == LONG_FILM:
        return [(10_500_000, LONG_FILM_MS + 1, "credits")]
    if path == UNDECIDED:
        return [(5_557_000, UNDECIDED_MS + 1, "credits")]
    return []


def install(page: Page, api: InspectorApi | None = None) -> InspectorApi:
    """Route every Inspector call on ``page`` to ``api`` (a fresh one with the three boards when None)."""
    if api is None:
        api = InspectorApi()
        for make in (checked_episode, unchecked_film, undecided_film):
            file, item = make()
            api.add(file, item, default_kinds(file["canonical_path"]))

    def json_body(route: Route) -> dict:
        try:
            return route.request.post_data_json or {}
        except Exception:
            return {}

    def answer(route: Route, key: str, body: Any, status: int = 200) -> None:
        status, body = api.answers.get(key, (status, body))
        if key in api.hold_writes:
            api.held_writes.append((route, body, status))
        else:
            _fulfill_json(route, body, status=status)

    def handler(route: Route) -> None:
        url = urlparse(route.request.url)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        method = route.request.method
        p = url.path
        if p == "/api/servers" and method == "GET":
            _fulfill_json(route, {"servers": api.servers_list})
        elif p == "/api/media/search":
            api.search_requests.append(q)
            query = q.get("q", "").lower()
            results = [r for r in search_results() if query in r["title"].lower()] if query != "zzz" else []
            answer(route, "search", {"query": query, "results": results, "error": None})
        elif p == "/api/inspector/status":
            body = json_body(route)
            known = {**statuses(), **api.status_items}
            answer(route, "status", {"items": {x: known.get(x, {"in_library": False}) for x in body.get("paths", [])}})
        elif p == "/api/inspector/show":
            answer(route, "show", {"seasons": show_seasons()})
        elif p == "/api/inspector/file":
            path = q.get("path", "")
            api.file_requests.append(path)
            status = 200
            if path in api.files:
                body = api.files[path]
                if isinstance(body, tuple):
                    status, body = body
            else:
                body = {
                    "canonical_path": path,
                    "exists": None,
                    "in_library": False,
                    "known": False,
                    "title": path.split("/")[-1],
                    "kind": "movie",
                    "quality": "",
                    "duration_ms": None,
                    "previews": [],
                    "preview": None,
                    "versions": [],
                    "job": None,
                }
            if api.hold_files:
                api.held.append((route, body))
            elif path in api.hold_paths:
                api.held_paths.setdefault(path, []).append((route, body, status))
            else:
                _fulfill_json(route, body, status=status)
        elif p == "/api/markers/item" and method == "GET":
            path = q.get("path", "")
            api.item_requests.append(path)
            item = api.items.get(path)
            if isinstance(item, tuple):
                _fulfill_json(route, item[1], status=item[0])
            elif item is None:
                _fulfill_json(route, {"error": "Path is not a file inside any server library"}, status=400)
            else:
                _fulfill_json(route, item)
        elif p == "/api/markers/item/markers" and method == "POST":
            body = json_body(route)
            api.saves.append(body)
            if body.get("path") in api.after_save and "save" not in api.answers:
                api.items[body["path"]] = api.after_save[body["path"]]
            answer(route, "save", save_answer(body.get("path"), [save_row("plex-1", "Plex", "plex")]))
        elif p == "/api/markers/item/markers" and method == "DELETE":
            body = json_body(route)
            api.unlocks.append(body)
            if body.get("path") in api.after_unlock and "unlock" not in api.answers:
                api.items[body["path"]] = api.after_unlock[body["path"]]
            answer(
                route,
                "unlock",
                {"canonical_path": body.get("path"), "unlocked": ["credits"], "markers": {}, "decisions": {}},
            )
        elif p == "/api/markers/item/redetect":
            api.redetects.append(json_body(route))
            answer(route, "redetect", {"job_id": "job-redetect-1"}, status=202)
        elif p == "/api/markers/season" and method == "GET":
            api.season_requests.append(q.get("path", ""))
            answer(route, "season", api.season if api.season is not None else season_payload())
        elif p == "/api/markers/season/publish" and method == "POST":
            api.season_publishes.append(json_body(route))
            answer(route, "publish", {"job_id": "job-season-1"}, status=202)
        elif p == "/api/jobs/manual":
            api.manual_jobs.append(json_body(route))
            answer(
                route,
                "manual",
                {"id": "job-preview-1", "kind": "previews", "status": "pending", "library_name": "Manual: file"},
                status=201,
            )
        elif p == "/api/bif/info":
            info = api.bif_info.get(q.get("path", ""))
            if info is None:
                _fulfill_json(route, {"error": "Not a preview file"}, status=400)
            else:
                _fulfill_json(route, info)
        elif p == "/api/inspector/frames":
            path = q.get("path", "")
            start = int(q.get("start_ms", "0"))
            count = int(q.get("count", "7"))
            api.frame_requests.append({"path": path, "start_ms": start, "count": count})
            if api.frames_error is not None:
                _fulfill_json(route, api.frames_error[1], status=api.frames_error[0])
                return
            if api.frames_busy.get(start):
                api.frames_busy[start] -= 1
                _fulfill_json(route, {"error": "Busy reading other frames; try again"}, status=503)
                return
            frames = [
                {
                    "t_ms": start + i * 1000,
                    "src": "data:image/jpeg;base64,"
                    + base64.b64encode(
                        frame_jpeg(api.kind_at(path, start + i * 1000), (start // 1000 + i) // 7)
                    ).decode(),
                }
                for i in range(count)
            ]
            _fulfill_json(route, {"path": path, "start_ms": start, "step_ms": 1000, "frames": frames})
        elif p == "/api/bif/frame" or p == "/api/bif/trickplay/frame":
            index = int(q.get("index", "0"))
            api.image_requests.append(index)
            files = {f: file for f, file in api.files.items() if isinstance(file, dict)}
            path = next(
                (f for f, file in files.items() if (file.get("preview") or {}).get("path") == q.get("path")), ""
            )
            if not path:
                path = next(iter(files), "")
            step = ((files.get(path) or {}).get("preview") or {}).get("interval_ms") or 2000
            route.fulfill(
                status=200,
                content_type="image/jpeg",
                body=frame_jpeg(api.kind_at(path, index * step), index // 30, 240),
            )
        else:
            route.continue_()

    page.route(
        re.compile(
            r".*/api/(servers|media/search|inspector/.*|markers/item.*|markers/season.*|jobs/manual|bif/info|bif/frame|bif/trickplay/frame)(\?.*)?$"
        ),
        handler,
    )
    return api
