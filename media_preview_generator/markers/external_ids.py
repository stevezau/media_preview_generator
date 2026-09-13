"""External ids for online lookups: parsed from the path, completed from server metadata."""

from __future__ import annotations

import os
import re
from typing import Any

from .models import MediaIds

# tmdb/tvdb are bare digits; imdb requires the "tt" prefix. The optional "(?:tt)?" on the
# shared value group lets `_ids_in` detect and reject a scheme/value mismatch (e.g. a stray
# "{tmdb-tt0114709}") instead of silently accepting a malformed id (spec §5.2: invalid -> ignored).
# "[-=]" also accepts Emby-style "[tmdbid=862]".
_ID_RE = re.compile(r"[\{\[](tmdb|tvdb|imdb)(?:id)?[-=]((?:tt)?\d+)[\}\]]", re.IGNORECASE)
# Season/episode width widened to 4 digits for daily/absolute numbering conventions
# (e.g. "S2005E01", "S2024E13"); still anchored so a bare digit run never spills over.
_SXXEYY_RE = re.compile(r"(?<![a-z0-9])s(\d{1,4})e(\d{1,4})(?![0-9])", re.IGNORECASE)
_SEASON_DIR_RE = re.compile(r"^(?:season|series|staffel|saison)\s*\d{1,4}$|^specials$", re.IGNORECASE)
# Daily/talk-show dated episodes ("Show - 2024-01-15.mkv"): no SxxEyy to parse, and the
# filename shape itself (not a movie convention) rules out guessing "movie" from a nearby id.
_DATE_EPISODE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
# Plex "extra" suffixes (immediately before the extension) and an extras-folder name as the
# immediate PARENT folder never get ids — they're not the primary feature file the online
# sources describe, and Sonarr/Radarr never carry markers for them either. Only the parent is
# checked (not every ancestor): ids themselves only ever come from the parent or the grandparent
# above a season folder, so a same-named ancestor further up (e.g. a library category folder
# literally called "Other" or "Shorts") can never contribute an id anyway — checking it for
# "is this an extras folder" would only produce false positives, never real protection.
_EXTRA_SUFFIX_RE = re.compile(
    r"-(trailer|featurette|behindthescenes|deleted|interview|scene|short|other|sample)$", re.IGNORECASE
)
_EXTRAS_FOLDER_NAMES = frozenset(
    {
        "trailers",
        "featurettes",
        "extras",
        "behind the scenes",
        "deleted scenes",
        "interviews",
        "scenes",
        "shorts",
        "other",
        "samples",
    }
)


def _ids_in(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for scheme, value in _ID_RE.findall(text):
        scheme = scheme.lower()
        is_tt = value.lower().startswith("tt")
        if (scheme == "imdb") != is_tt:
            continue  # scheme/value shape mismatch (e.g. "{tmdb-tt...}", "{imdb-123}") -> ignore
        digits = value[2:] if is_tt else value
        if int(digits) == 0:
            continue  # "0" is not a real id (matches ids_from_server_dict's rejection of "0")
        found.setdefault(scheme, value.lower() if scheme == "imdb" else value)
    return found


def _is_extra(stem: str, folders: list[str]) -> bool:
    if _EXTRA_SUFFIX_RE.search(stem):
        return True
    return any(folder.strip().lower() in _EXTRAS_FOLDER_NAMES for folder in folders[-1:])


def ids_from_path(canonical_path: str) -> MediaIds:
    """Parse ids, season and episode from a TRaSH/Jellyfin-style path.

    Episodes read ids ONLY from the immediate show folder — the parent, or the grandparent when
    the parent is a season-like folder (``Season NN``, ``Series NN``, ``Specials``). An id further
    up the tree (e.g. a franchise-level folder above a spin-off's own show folder) is never used:
    every online source wants THIS show's id, not an ancestor's. Ids in the episode's own filename
    belong to the episode, not the series, and are never used either.

    Movies read the filename and its immediate parent folder only, and only become "movie" when a
    tmdb or imdb token is present AND no tvdb token was seen in either place — a tvdb token usually
    means this is actually a TV file whose SxxEyy just didn't parse (absolute/anime numbering, a
    daily dated episode, ...), and guessing "movie" there would route the wrong online source.

    Files recognised as Plex "extra" content (trailers, featurettes, behind-the-scenes, ... by
    filename suffix, or an extras name as the immediate parent folder) never get ids — they
    aren't the primary feature/episode file the online sources describe.
    """
    parts = [p for p in canonical_path.replace("\\", "/").split("/") if p]
    if not parts:
        return MediaIds()
    filename = parts[-1]
    folders = parts[:-1]
    stem = os.path.splitext(filename)[0]
    if _is_extra(stem, folders):
        return MediaIds()

    m = _SXXEYY_RE.search(stem)
    if m:
        season, episode = int(m.group(1)), int(m.group(2))
        show_folder: str | None = None
        if folders:
            if _SEASON_DIR_RE.match(folders[-1]):
                show_folder = folders[-2] if len(folders) >= 2 else None
            else:
                show_folder = folders[-1]
        found = _ids_in(show_folder) if show_folder else {}
        return MediaIds("episode", found.get("tmdb"), found.get("imdb"), found.get("tvdb"), season, episode)

    if _DATE_EPISODE_RE.search(stem):
        return MediaIds()

    found = _ids_in(filename)
    if folders:
        for k, v in _ids_in(folders[-1]).items():
            found.setdefault(k, v)
    if (found.get("tmdb") or found.get("imdb")) and not found.get("tvdb"):
        return MediaIds("movie", found.get("tmdb"), found.get("imdb"))  # movies: tvdb is a different id space
    return MediaIds()


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ids_from_server_dict(raw: dict | None) -> MediaIds:
    """Normalise a ``MediaServer.get_external_ids`` result.

    Only ``movie``/``episode`` kinds carry ids through — an unrecognised kind (a show/series item
    itself, a trailer, ...) returns a fully empty :class:`MediaIds` rather than a partial one, so a
    caller can never accidentally publish ids for the wrong kind of item.
    """
    if not isinstance(raw, dict):
        return MediaIds()
    if raw.get("kind") not in ("movie", "episode"):
        return MediaIds()

    def _s(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text if text and text != "0" else None

    return MediaIds(raw["kind"], _s("tmdb"), _s("imdb"), _s("tvdb"), _int(raw.get("season")), _int(raw.get("episode")))


def merge_ids(primary: MediaIds, fallback: MediaIds) -> MediaIds:
    """Field-wise merge: primary wins; kind comes from whichever side knows it (primary first)."""
    kind = primary.kind if primary.kind != "unknown" else fallback.kind
    return MediaIds(
        kind,
        primary.tmdb or fallback.tmdb,
        primary.imdb or fallback.imdb,
        primary.tvdb or fallback.tvdb,
        primary.season if primary.season is not None else fallback.season,
        primary.episode if primary.episode is not None else fallback.episode,
    )
