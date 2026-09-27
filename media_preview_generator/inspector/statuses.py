"""What the Inspector's search rows and episode picker say about a file: its Intro & Credits state from markers.db, its
quality from its name, and a show's seasons and episodes from its folders on disk."""

from __future__ import annotations

import os
import re

from ..markers.audio.season import folder_videos
from ..markers.decide import DecisionStatus
from ..markers.external_ids import ids_from_path, is_season_folder, season_folder_number
from ..markers.models import MarkerType
from ..markers.store import MarkerStore

NOT_CHECKED = "Not checked yet"
NEEDS_REVIEW = "Needs review"
BOTH = "Intro + credits"
CREDITS_SET = "Credits set"
CREDITS_ONLY = "Credits only"
INTRO_ONLY = "Intro only"
NOTHING_FOUND = "Nothing found"

# Release names carry the quality between brackets or dots ("[Bluray-2160p][DV HDR10]", ".1080p.WEB-DL."); a token
# only counts between non-alphanumerics, so "DVD" or "HDRip" aren't read as Dolby Vision or HDR.
_TOKEN = r"(?<![a-z0-9]){}(?![a-z0-9])"
_RESOLUTIONS = (
    (re.compile(_TOKEN.format(r"(?:2160p|4k|uhd)"), re.IGNORECASE), "2160p"),
    (re.compile(_TOKEN.format(r"1080[pi]"), re.IGNORECASE), "1080p"),
    (re.compile(_TOKEN.format(r"720p"), re.IGNORECASE), "720p"),
    (re.compile(_TOKEN.format(r"576[pi]"), re.IGNORECASE), "576p"),
    (re.compile(_TOKEN.format(r"480[pi]"), re.IGNORECASE), "480p"),
)
_DYNAMIC_RANGES = (
    (re.compile(_TOKEN.format(r"(?:dv|dovi|dolby[ ._-]?vision)"), re.IGNORECASE), "Dolby Vision"),
    (re.compile(r"(?<![a-z0-9])hdr10(?:\+|plus)", re.IGNORECASE), "HDR10+"),
    (re.compile(_TOKEN.format(r"hdr10"), re.IGNORECASE), "HDR10"),
    (re.compile(_TOKEN.format(r"hdr"), re.IGNORECASE), "HDR"),
    (re.compile(_TOKEN.format(r"hlg"), re.IGNORECASE), "HLG"),
)


def quality_from_name(path: str) -> str:
    """The resolution and dynamic range a file's name states, e.g. "2160p Dolby Vision"; "" when it states neither.

    Only the name is read (the search rows can't afford a probe per file), so a file named without them says nothing.

    Args:
        path: The file's path.

    Returns:
        The words, resolution first.
    """
    name = os.path.basename(path)
    parts = [next((label for rx, label in table if rx.search(name)), "") for table in (_RESOLUTIONS, _DYNAMIC_RANGES)]
    return " ".join(p for p in parts if p)


def markers_state(store: MarkerStore, canonical_path: str) -> dict:
    """A file's Intro & Credits state in the search rows' words, from markers.db only.

    Args:
        store: The markers store.
        canonical_path: The file's local path.

    Returns:
        ``state`` (``not_checked``, ``needs_review``, ``both``, ``credits``, ``intro`` or ``none``) and its
        ``label``. Needs review wins over whatever else was decided: that file is waiting on the user.
    """
    rec = store.get_file(canonical_path)
    if rec is None:
        return {"state": "not_checked", "label": NOT_CHECKED}
    decisions = store.get_decisions(rec.id)
    if not decisions:
        return {"state": "not_checked", "label": NOT_CHECKED}
    if any(d.status is DecisionStatus.NEEDS_REVIEW for d in decisions.values()):
        return {"state": "needs_review", "label": NEEDS_REVIEW}
    decided = {t for t, d in decisions.items() if d.status is DecisionStatus.DECIDED}
    intro, credits = MarkerType.INTRO in decided, MarkerType.CREDITS in decided
    if intro and credits:
        return {"state": "both", "label": BOTH}
    if credits:
        # A film never has an intro to find, so its credits are all there is; an episode's intro is missing.
        return {"state": "credits", "label": CREDITS_SET if rec.is_movie else CREDITS_ONLY}
    if intro:
        return {"state": "intro", "label": INTRO_ONLY}
    return {"state": "none", "label": NOTHING_FOUND}


def file_kind(canonical_path: str) -> str:
    """``episode`` when the path names a season and episode, else ``movie``."""
    return "episode" if ids_from_path(canonical_path).is_episode else "movie"


def _clean(name: str) -> str:
    # "The Matrix (1999) {tmdb-603}" and "Show (2024) [imdb-tt1]" read as their title and year.
    text = re.sub(r"\{[^}]*\}|\[[^\]]*\]", "", name)
    return re.sub(r"\s+", " ", text).strip(" -._")


def file_title(canonical_path: str) -> str:
    """A film's or episode's title from its folders: "The Matrix (1999)", "Blood Legacy (2024) · S01E01".

    Args:
        canonical_path: The file's local path.

    Returns:
        The title; the file's own name (tags removed) when no folder names it.
    """
    folder = os.path.dirname(canonical_path)
    ids = ids_from_path(canonical_path)
    stem = _clean(os.path.splitext(os.path.basename(canonical_path))[0])
    if ids.is_episode:
        show_folder = os.path.dirname(folder) if is_season_folder(os.path.basename(folder)) else folder
        show = _clean(os.path.basename(show_folder)) or stem
        if ids.season is not None and ids.episode is not None:
            return f"{show} · S{ids.season:02d}E{ids.episode:02d}"
        return show
    return _clean(os.path.basename(folder)) or stem


def _season_label(season: int | None) -> str:
    if season is None:
        return "Episodes"
    return "Specials" if season == 0 else f"Season {season}"


def show_seasons(folders: list[str], store: MarkerStore) -> list[dict]:
    """A show's seasons and episodes from its folders on disk (every disk it is spread over), each episode's Intro &
    Credits state from markers.db.

    A show folder's season folders (``Season 01``, ``S2``, ``Specials``) hold its episodes; a show kept without them
    holds the episodes itself. Extras are left out, as the season grouping does.

    Args:
        folders: The show's folders (already validated by the caller).
        store: The markers store.

    Returns:
        ``[{"season", "label", "episodes": [{"path", "code", "episode", "markers"}]}]``, seasons in order with Specials
        last; an episode's ``code`` is "E01" (its file name when the name carries no episode number).
    """
    seasons: dict[int | None, dict[str, dict]] = {}

    def add(video_path: str, season: int | None, episode: int | None) -> None:
        episodes = seasons.setdefault(season, {})
        if video_path in episodes:
            return
        code = f"E{episode:02d}" if episode is not None else os.path.splitext(os.path.basename(video_path))[0]
        episodes[video_path] = {
            "path": video_path,
            "code": code,
            "episode": episode,
            "markers": markers_state(store, video_path),
        }

    for folder in folders:
        for video in folder_videos(folder):
            add(video.path, video.season, video.episode)
        try:
            with os.scandir(folder) as entries:
                subfolders = [e.path for e in entries if e.is_dir() and is_season_folder(e.name)]
        except OSError:
            subfolders = []
        for sub in subfolders:
            number = season_folder_number(os.path.basename(sub))
            for video in folder_videos(sub):
                add(video.path, video.season if video.season is not None else number, video.episode)

    def season_order(key: int | None) -> tuple[int, int]:
        if key is None:
            return (2, 0)
        return (1, 0) if key == 0 else (0, key)

    out = []
    for season in sorted(seasons, key=season_order):
        episodes = sorted(
            seasons[season].values(),
            key=lambda e: (e["episode"] is None, e["episode"] or 0, e["path"]),
        )
        out.append({"season": season, "label": _season_label(season), "episodes": episodes})
    return out
